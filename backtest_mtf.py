"""
backtest_mtf.py — Multi-Timeframe Walk-Forward Backtest ย้อนหลัง 3 ปี
═══════════════════════════════════════════════════════════════════════
วิเคราะห์ทุก TF: 15m / 30m / 1H / 2H / 4H / 1D
แต่ละ TF ใช้ TF ที่ใหญ่กว่า ~4× เป็น HTF context สำหรับ Trend Agent

TF Pairs (primary → HTF):
  15m → 1H   (ย้อนหลัง 1 ปี — ข้อมูลมาก)
  30m → 2H   (ย้อนหลัง 2 ปี)
  1H  → 4H   (ย้อนหลัง 3 ปี)
  2H  → 8H   (ย้อนหลัง 3 ปี)
  4H  → 1D   (ย้อนหลัง 3 ปี)
  1D  → 1W   (ย้อนหลัง 3 ปี)

Output:
  backtest_mtf.csv         — ทุก trade (เพิ่ม column "tf")
  backtest_mtf_summary.csv — summary per TF

Usage:
  py backtest_mtf.py
  py backtest_mtf.py --tf 1h 4h 1d
  py backtest_mtf.py --symbols BTC/USDT ETH/USDT SOL/USDT
  py backtest_mtf.py --fast           # เฉพาะ 1H/4H/1D, 3 symbols
  py backtest_mtf.py --years 3        # override ทุก TF เป็น 3yr
"""

import sys, os, warnings, time as time_mod, argparse, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime  import datetime, timezone, timedelta
from textwrap  import indent

import numpy   as np
import pandas  as pd

warnings.filterwarnings("ignore")

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt"); sys.exit(1)

import signal_scanner as SCANNER
import agent_trend    as TREND
from ta.trend import EMAIndicator, SMAIndicator

# ── ปิด DB writes ระหว่าง backtest ──────────────────────────────────────────
SCANNER.save_condition_snapshot = lambda *a, **kw: None
SCANNER.save_specialist_history = lambda *a, **kw: None

# ══════════════════════════════════════════════════════════════════════════════
# TF CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
#
#  primary    : TF ที่ใช้ scan signal  (ส่งเป็น df_1h ให้ scanner)
#  htf        : TF ที่ใช้เป็น HTF bias (ส่งเป็น df_4h ให้ scanner)
#  resample   : pandas resample freq ของ primary (สำหรับ _htf_bias join)
#  window_p   : rolling window ของ primary ที่ส่งให้ scanner
#  window_h   : rolling window ของ HTF
#  warmup     : candles แรกที่รอ indicator warm-up
#  timeout    : candles หมดเวลา (= 48H / TF size)
#  step       : scan ทุก N primary candles
#  years      : ดึงข้อมูลย้อนหลังกี่ปี

TF_CONFIGS = {
    # primary/htf ต้องเป็น lowercase เสมอ (ccxt OKX format)
    # window_p ต้องมากกว่า SMA99 (99) + buffer → ใช้ >= 200 เสมอ
    "15m": dict(primary="15m", htf="1h",  resample="15min",
                window_p=2000, window_h=500,  warmup=400, timeout=192, step=8,  years=1),
    "30m": dict(primary="30m", htf="2h",  resample="30min",
                window_p=1000, window_h=250,  warmup=200, timeout=96,  step=4,  years=2),
    "1h":  dict(primary="1h",  htf="4h",  resample="1h",
                window_p=500,  window_h=125,  warmup=200, timeout=48,  step=4,  years=3),
    "4h":  dict(primary="4h",  htf="1d",  resample="4h",
                window_p=300,  window_h=80,   warmup=200, timeout=12,  step=1,  years=3),
    "1d":  dict(primary="1d",  htf="1w",  resample="1D",
                window_p=300,  window_h=60,   warmup=200, timeout=6,   step=1,  years=3),
}

ALL_TF_ORDER = ["15m", "30m", "1h", "4h", "1d"]

DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
]
TP1_R       = 1.2
TP2_R       = 2.0
RISK_USD    = 10.0
API_LIMIT   = 300
OUTPUT_CSV  = "backtest_mtf.csv"
SUMMARY_CSV = "backtest_mtf_summary.csv"

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════
exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

print("Loading OKX markets ...", end=" ", flush=True)
exchange.load_markets()
print(f"{len(exchange.markets)} pairs")


# ══════════════════════════════════════════════════════════════════════════════
# PAGINATED FETCH
# ══════════════════════════════════════════════════════════════════════════════
def fetch_paginated(symbol: str, tf: str, years: float) -> pd.DataFrame:
    """ดึง OHLCV ย้อนหลัง N ปี ด้วย pagination"""
    since_dt = datetime.now(timezone.utc) - timedelta(days=int(years * 365))
    since_ms = int(since_dt.timestamp() * 1000)
    all_bars, page = [], 0

    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, tf, since=since_ms, limit=API_LIMIT)
        except Exception as e:
            print(f"\n  [WARN] fetch {symbol} {tf} page {page}: {e}")
            time_mod.sleep(2)
            break

        if not bars:
            break

        all_bars.extend(bars)
        page += 1
        print(f"\r  {symbol} {tf}: {len(all_bars):,} candles", end="", flush=True)

        if len(bars) < API_LIMIT:
            break

        since_ms = bars[-1][0] + 1
        time_mod.sleep(0.25)

    print()

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df.drop_duplicates("ts", inplace=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()


# ══════════════════════════════════════════════════════════════════════════════
# MONKEY-PATCH _htf_bias ให้รองรับทุก primary TF
# ══════════════════════════════════════════════════════════════════════════════
_ORIG_HTF_BIAS = TREND._htf_bias

def _make_htf_bias(primary_freq: str):
    """สร้าง _htf_bias ที่ resample ตาม primary TF"""
    EMA_FAST = TREND.EMA_FAST
    EMA_SLOW = TREND.EMA_SLOW
    SMA_99   = TREND.SMA_99

    def _patched(df_primary, df_htf):
        if df_htf is None or df_htf.empty:
            df_primary = df_primary.copy()
            df_primary["htf_bull"] = True
            df_primary["htf_sma"]  = True
            return df_primary
        df_htf = df_htf.copy()
        df_htf["h7"]      = EMAIndicator(df_htf["close"], EMA_FAST).ema_indicator()
        df_htf["h30"]     = EMAIndicator(df_htf["close"], EMA_SLOW).ema_indicator()
        df_htf["h99"]     = SMAIndicator(df_htf["close"], SMA_99).sma_indicator()
        df_htf["htf_bull"] = df_htf["h7"]  > df_htf["h30"]
        df_htf["htf_sma"]  = df_htf["close"] > df_htf["h99"]
        try:
            htf_rs = df_htf[["htf_bull", "htf_sma"]].resample(primary_freq).ffill()
            df_primary = df_primary.join(htf_rs, how="left")
        except Exception:
            df_primary = df_primary.copy()
        df_primary["htf_bull"] = df_primary["htf_bull"].ffill().fillna(True)
        df_primary["htf_sma"]  = df_primary["htf_sma"].ffill().fillna(True)
        return df_primary

    return _patched


def patch_htf(primary_freq: str):
    """Monkey-patch agent_trend._htf_bias สำหรับ TF นี้"""
    TREND._htf_bias = _make_htf_bias(primary_freq)


def restore_htf():
    """คืน _htf_bias ต้นฉบับ"""
    TREND._htf_bias = _ORIG_HTF_BIAS


# ══════════════════════════════════════════════════════════════════════════════
# PnL CALCULATION
# ══════════════════════════════════════════════════════════════════════════════
def calc_pnl(side, ep, sl_orig, tp1_px, exit_px, tp1_was_hit):
    dist_sl = abs(ep - sl_orig)
    if dist_sl < ep * 0.0001:
        return 0.0, "VOID"

    def to_r(px):
        return (px - ep) / dist_sl if side == "LONG" else (ep - px) / dist_sl

    if tp1_was_hit:
        r = 0.5 * to_r(tp1_px) + 0.5 * to_r(exit_px)
    else:
        r = to_r(exit_px)

    pnl     = round(RISK_USD * r, 2)
    outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
    return pnl, outcome


# ══════════════════════════════════════════════════════════════════════════════
# WALK-FORWARD BACKTEST (single TF, single symbol)
# ══════════════════════════════════════════════════════════════════════════════
def backtest_symbol(sym: str, tf_name: str, df_p: pd.DataFrame, df_h: pd.DataFrame) -> pd.DataFrame:
    """
    Walk-forward backtest สำหรับ 1 symbol บน 1 TF

    df_p = primary TF dataframe (ส่งเป็น df_1h ให้ scanner)
    df_h = HTF dataframe        (ส่งเป็น df_4h ให้ scanner)
    """
    cfg          = TF_CONFIGS[tf_name]
    WARMUP       = cfg["warmup"]
    WINDOW_P     = cfg["window_p"]
    WINDOW_H     = cfg["window_h"]
    TIMEOUT_C    = cfg["timeout"]
    STEP         = cfg["step"]

    trades       = []
    in_trade     = False
    entry_data   = {}
    candles_open = 0
    n            = len(df_p)
    scanned      = 0

    for i in range(WARMUP, n):
        row = df_p.iloc[i]

        if in_trade:
            hi   = row["high"]
            lo   = row["low"]
            ep   = entry_data["ep"]
            sl   = entry_data["sl_orig"]
            tp1  = entry_data["tp1"]
            tp2  = entry_data["tp2"]
            side = entry_data["side"]
            tp1h = entry_data["tp1_hit"]

            active_sl = ep if tp1h else sl

            hit_tp1 = (not tp1h) and (
                (side == "LONG"  and hi >= tp1) or
                (side == "SHORT" and lo <= tp1))
            hit_tp2 = tp1h and (
                (side == "LONG"  and hi >= tp2) or
                (side == "SHORT" and lo <= tp2))
            hit_sl  = (
                (side == "LONG"  and lo <= active_sl) or
                (side == "SHORT" and hi >= active_sl))
            timeout = candles_open >= TIMEOUT_C

            if hit_tp1 and not hit_sl:
                entry_data["tp1_hit"] = True
                candles_open += 1
                continue

            if hit_tp2:
                pnl, outcome = calc_pnl(side, ep, sl, tp1, tp2, True)
                _record(trades, entry_data, i, df_p, tf_name, tp2, "TP2", outcome, pnl)
                in_trade = False

            elif hit_sl:
                exit_px = active_sl
                if tp1h:
                    pnl, outcome = calc_pnl(side, ep, sl, tp1, exit_px, True)
                    exit_type = "SL_BE"
                else:
                    pnl, outcome = -RISK_USD, "LOSS"
                    exit_type = "SL"
                _record(trades, entry_data, i, df_p, tf_name, exit_px, exit_type, outcome, pnl)
                in_trade = False

            elif timeout:
                exit_px = row["close"]
                pnl, outcome = calc_pnl(side, ep, sl, tp1, exit_px, tp1h)
                _record(trades, entry_data, i, df_p, tf_name, exit_px, "TIMEOUT", outcome, pnl)
                in_trade = False
            else:
                candles_open += 1
                continue

            candles_open += 1

        else:
            # ── Scan ทุก STEP candles ─────────────────────────────────────────
            if (i - WARMUP) % STEP != 0:
                continue

            # Rolling window
            win_start  = max(0, i - WINDOW_P)
            slice_p    = df_p.iloc[win_start:i]
            last_ts    = slice_p.index[-1]
            slice_h    = df_h[df_h.index <= last_ts].iloc[-WINDOW_H:] if not df_h.empty else df_h

            if len(slice_p) < WARMUP // 2:
                continue

            try:
                sig, _ = SCANNER.scan_symbol(sym, slice_p, slice_h, "FUTURES")
            except Exception:
                sig = None

            scanned += 1

            if sig and abs(sig["price"] - sig["sl"]) > sig["price"] * 0.0001:
                entry_data = {
                    "sym":         sym,
                    "tf":          tf_name,
                    "side":        sig["side"],
                    "score":       sig["score"],
                    "score_trend": sig.get("score_trend", 0),
                    "score_smc":   sig.get("score_smc",   0),
                    "score_osc":   sig.get("score_osc",   0),
                    "ep":          sig["price"],
                    "sl_orig":     sig["sl"],
                    "tp1":         sig["tp1"],
                    "tp2":         sig["tp2"],
                    "sl_pct":      sig["sl_pct"],
                    "rsi":         sig["rsi"],
                    "in_kz":       sig["in_kz"],
                    "regime":      sig.get("regime", ""),
                    "entry_ts":    str(df_p.index[i - 1]),
                    "tp1_hit":     False,
                }
                in_trade     = True
                candles_open = 0

    df_out = pd.DataFrame(trades)
    print(f"    {sym} {tf_name}: {scanned:,} scans → {len(df_out)} trades")
    return df_out


def _record(trades, ed, i, df_p, tf_name, exit_px, exit_type, outcome, pnl):
    trades.append({
        "tf":          tf_name,
        "sym":         ed["sym"],
        "side":        ed["side"],
        "score":       ed["score"],
        "score_trend": ed["score_trend"],
        "score_smc":   ed["score_smc"],
        "score_osc":   ed["score_osc"],
        "ep":          ed["ep"],
        "sl":          ed["sl_orig"],
        "tp1":         ed["tp1"],
        "tp2":         ed["tp2"],
        "sl_pct":      ed["sl_pct"],
        "rsi":         ed["rsi"],
        "in_kz":       ed["in_kz"],
        "regime":      ed["regime"],
        "entry_ts":    ed["entry_ts"],
        "exit_ts":     str(df_p.index[i]),
        "exit_px":     round(exit_px, 8),
        "exit_type":   exit_type,
        "tp1_hit":     ed["tp1_hit"],
        "outcome":     outcome,
        "pnl":         pnl,
    })


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════
def calc_metrics(df: pd.DataFrame, label: str = "") -> dict:
    if len(df) < 3:
        return {"label": label, "n": len(df), "note": "not enough trades"}

    wins   = (df["outcome"] == "WIN")
    n      = len(df)
    wr     = round(wins.mean() * 100, 1)
    tp1r   = round(df["tp1_hit"].mean() * 100, 1) if "tp1_hit" in df.columns else 0
    total  = round(df["pnl"].sum(), 2)
    avg    = round(df["pnl"].mean(), 2)
    eq     = df["pnl"].cumsum()
    pk     = eq.cummax()
    dd     = round(((eq - pk) / pk.abs().replace(0, 1) * 100).min(), 1)
    std    = df["pnl"].std()
    # Annualised Sharpe (trading days ≈ 252, กรอง TIMEOUT ออก)
    df_sig = df[df["exit_type"] != "TIMEOUT"]
    std_s  = df_sig["pnl"].std() if len(df_sig) > 1 else std
    avg_s  = df_sig["pnl"].mean() if len(df_sig) > 1 else avg
    sharpe = round((avg_s / std_s) * (252 ** 0.5), 2) if std_s and std_s > 0 else 0
    # Profit Factor
    gross_win  = df[df["pnl"] > 0]["pnl"].sum()
    gross_loss = abs(df[df["pnl"] < 0]["pnl"].sum())
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else 999

    return {
        "label":      label,
        "n":          n,
        "wr":         wr,
        "tp1_rate":   tp1r,
        "total_pnl":  total,
        "avg_pnl":    avg,
        "max_dd":     dd,
        "sharpe":     sharpe,
        "profit_factor": pf,
        "equity":     [0] + [round(v, 2) for v in eq.tolist()],
    }


def print_metrics(m: dict):
    if "note" in m:
        print(f"  {m['label']}: {m['note']}")
        return
    verdict = ""
    if m["total_pnl"] > 0 and m["sharpe"] >= 1.5 and m["max_dd"] >= -30:
        verdict = " ✅ STRONG"
    elif m["total_pnl"] > 0 and m["sharpe"] >= 0.8:
        verdict = " ✅ PASS"
    elif m["total_pnl"] > 0:
        verdict = " ⚠️  MARGINAL"
    else:
        verdict = " ❌ FAIL"

    print(f"  {m['label']:<12} | "
          f"N={m['n']:>4} | "
          f"WR={m['wr']:>5.1f}% | "
          f"PnL=${m['total_pnl']:>8.2f} | "
          f"Sharpe={m['sharpe']:>5.2f} | "
          f"MaxDD={m['max_dd']:>6.1f}% | "
          f"PF={m['profit_factor']:>5.2f}"
          f"{verdict}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf",      nargs="+", default=None,
                        help="TFs to run: 15m 30m 1h 4h 1d")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--years",   type=float, default=None,
                        help="Override years for all TFs")
    args = parser.parse_args()

    tfs     = args.tf or ALL_TF_ORDER
    symbols = args.symbols or DEFAULT_SYMBOLS

    # Override years
    if args.years:
        for k in tfs:
            TF_CONFIGS[k]["years"] = args.years

    start_total = time_mod.time()
    print(f"\n{'='*70}")
    print(f"  WAKAM3 Multi-TF Backtest  |  {len(symbols)} symbols  |  TFs: {', '.join(tfs)}")
    print(f"{'='*70}\n")

    # ── Pre-fetch ALL required TFs ────────────────────────────────────────────
    # ต้องการ TFs เพื่อ: primary + htf สำหรับแต่ละ TF config
    needed_tfs: dict[str, tuple[str, float]] = {}   # ccxt_tf → (tf_str, years)
    for tf_name in tfs:
        cfg = TF_CONFIGS[tf_name]
        p_key  = cfg["primary"]
        h_key  = cfg["htf"]
        p_yrs  = cfg["years"]
        h_yrs  = p_yrs + 0.25   # HTF ดึงมากกว่านิดหน่อยเพื่อ warmup
        if p_key not in needed_tfs or needed_tfs[p_key][1] < p_yrs:
            needed_tfs[p_key] = (p_key, p_yrs)
        if h_key not in needed_tfs or needed_tfs[h_key][1] < h_yrs:
            needed_tfs[h_key] = (h_key, h_yrs)

    # ── Run per symbol ────────────────────────────────────────────────────────
    all_trades: list[pd.DataFrame] = []
    tf_metrics: dict[str, list] = {tf: [] for tf in tfs}

    for sym in symbols:
        print(f"\n{'─'*70}")
        print(f"  SYMBOL: {sym}")
        print(f"{'─'*70}")

        # ── Fetch data ───────────────────────────────────────────────────────
        data: dict[str, pd.DataFrame] = {}
        for ccxt_tf, (_, yrs) in sorted(needed_tfs.items()):
            print(f"  Fetching {ccxt_tf} ({yrs:.1f}yr)...")
            try:
                df = fetch_paginated(sym, ccxt_tf, yrs)
                data[ccxt_tf] = df
                print(f"    → {len(df):,} candles  "
                      f"({df.index[0].strftime('%Y-%m-%d')} – {df.index[-1].strftime('%Y-%m-%d')})")
            except Exception as e:
                print(f"    [ERROR] {e}")
                data[ccxt_tf] = pd.DataFrame()

        # ── Backtest each TF ─────────────────────────────────────────────────
        for tf_name in tfs:
            cfg    = TF_CONFIGS[tf_name]
            df_p   = data.get(cfg["primary"], pd.DataFrame())
            df_h   = data.get(cfg["htf"],    pd.DataFrame())

            if df_p.empty:
                print(f"\n  [SKIP] {tf_name}: no primary data")
                continue

            t0 = time_mod.time()
            print(f"\n  [{tf_name.upper()}] Walk-Forward Backtest ...")

            # Patch HTF bias resample frequency ก่อน scan
            patch_htf(cfg["resample"])

            try:
                df_trades = backtest_symbol(sym, tf_name, df_p, df_h)
            finally:
                restore_htf()

            elapsed = time_mod.time() - t0
            print(f"    Done in {elapsed:.1f}s")

            if len(df_trades):
                all_trades.append(df_trades)
                m = calc_metrics(df_trades, label=f"{sym.replace('/USDT','')} {tf_name}")
                print_metrics(m)
                tf_metrics[tf_name].append(m)

    # ── Combine & save ────────────────────────────────────────────────────────
    if not all_trades:
        print("\n[WARN] ไม่มี trades เลย")
        return

    df_all = pd.concat(all_trades, ignore_index=True)

    # ── Filter: เฉพาะ 2023-03-01 → 2026-03-01 ─────────────────────────────────
    BT_DATE_FROM = pd.Timestamp("2023-03-01", tz="UTC")
    BT_DATE_TO   = pd.Timestamp("2026-03-01", tz="UTC")
    if "entry_ts" in df_all.columns:
        ts_col = pd.to_datetime(df_all["entry_ts"], utc=True, errors="coerce")
        mask   = (ts_col >= BT_DATE_FROM) & (ts_col <= BT_DATE_TO)
        before = len(df_all)
        df_all = df_all[mask].copy()
        print(f"  Date filter {BT_DATE_FROM.date()} → {BT_DATE_TO.date()}: "
              f"{before:,} → {len(df_all):,} trades")

    df_all.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Saved {OUTPUT_CSV}  ({len(df_all):,} trades)")

    # ── Summary per TF ────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  SUMMARY BY TIMEFRAME")
    print(f"{'='*70}")
    print(f"  {'TF':<6} | {'N':>5} | {'WR':>6} | {'Total PnL':>10} | "
          f"{'Avg PnL':>8} | {'Sharpe':>7} | {'MaxDD':>7} | {'PF':>6}")
    print(f"  {'─'*6}─┼─{'─'*5}─┼─{'─'*6}─┼─{'─'*10}─┼─"
          f"{'─'*8}─┼─{'─'*7}─┼─{'─'*7}─┼─{'─'*6}")

    summary_rows = []
    for tf_name in tfs:
        df_tf = df_all[df_all["tf"] == tf_name]
        if len(df_tf) < 3:
            print(f"  {tf_name:<6} | — not enough data —")
            continue
        m = calc_metrics(df_tf, label=tf_name)
        verdict = ""
        if m["total_pnl"] > 0 and m["sharpe"] >= 1.5:
            verdict = "✅ STRONG"
        elif m["total_pnl"] > 0 and m["sharpe"] >= 0.8:
            verdict = "✅ PASS"
        elif m["total_pnl"] > 0:
            verdict = "⚠️  MARGINAL"
        else:
            verdict = "❌ FAIL"
        print(f"  {tf_name:<6} | {m['n']:>5} | {m['wr']:>5.1f}% | "
              f"${m['total_pnl']:>9.2f} | ${m['avg_pnl']:>7.2f} | "
              f"{m['sharpe']:>6.2f} | {m['max_dd']:>6.1f}% | "
              f"{m['profit_factor']:>5.2f}  {verdict}")
        summary_rows.append({
            "tf":          tf_name,
            "n":           m["n"],
            "wr":          m["wr"],
            "total_pnl":   m["total_pnl"],
            "avg_pnl":     m["avg_pnl"],
            "sharpe":      m["sharpe"],
            "max_dd":      m["max_dd"],
            "profit_factor": m["profit_factor"],
        })

    if summary_rows:
        pd.DataFrame(summary_rows).to_csv(SUMMARY_CSV, index=False)
        print(f"\n  Saved {SUMMARY_CSV}")

    # ── Best TF ───────────────────────────────────────────────────────────────
    if summary_rows:
        best = max(summary_rows, key=lambda r: r["sharpe"])
        print(f"\n  Best TF by Sharpe: [{best['tf'].upper()}]  "
              f"Sharpe={best['sharpe']}  PnL=${best['total_pnl']}  WR={best['wr']}%")

    total_elapsed = time_mod.time() - start_total
    mins, secs = divmod(int(total_elapsed), 60)
    print(f"\n  Total time: {mins}m {secs}s")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
