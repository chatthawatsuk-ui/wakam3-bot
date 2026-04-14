"""
backtest_live.py — Multi-TF Walk-Forward Backtest (Time-based)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
กำหนดด้วย --days (ช่วงเวลา) ไม่ใช่ --candles
แต่ละ TF ได้ข้อมูลเท่ากันในเชิงเวลา → เปรียบเทียบ WR/Sharpe ข้าม TF ได้ยุติธรรม

Flow:
  1. ดึง OHLCV ย้อนหลัง N วันจาก OKX (pagination)
  2. Walk-forward: ทุก candle i → scan_symbol() ด้วย code จริง
  3. ถ้า signal → จำลอง trade (SL/TP/Timeout)
  4. Output: backtest_live.csv มี column "tf" → dashboard แสดง per-TF breakdown

PnL: Fixed Risk $10 ต่อ trade
  - Full LOSS      : -$10
  - TP2 (full)     : +$16  (0.5×RR1.2 + 0.5×RR2.0) × $10
  - SL-Breakeven   : +$6   (0.5×RR1.2 + 0) × $10
  - Timeout        : proportional to exit price

Usage:
  py backtest_live.py                        # 10 symbols, 90 days, 15m/30m/1H/4H/1D
  py backtest_live.py --fast                 # 3 symbols, 60 days, 1H/4H (เร็วสุด)
  py backtest_live.py --days 30              # ย้อนหลัง 30 วัน
  py backtest_live.py --days 180 --tf 1H 4H  # 6 เดือน เฉพาะ 1H + 4H
  py backtest_live.py --symbols BTC/USDT ETH/USDT --days 60
"""
import sys, os, warnings, time as time_mod, argparse
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

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
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT", "SUI/USDT",
]
# primary OKX tf, htf OKX tf, นาทีต่อ candle, warmup candles
TF_CONFIGS = {
    # primary/htf ต้องเป็น lowercase เสมอ (ccxt OKX format)
    "15m": dict(primary="15m", htf="1h",  resample="15min", mins=15,   warmup=400),
    "30m": dict(primary="30m", htf="2h",  resample="30min", mins=30,   warmup=200),
    "1H":  dict(primary="1h",  htf="4h",  resample="1h",    mins=60,   warmup=200),
    "4H":  dict(primary="4h",  htf="1d",  resample="4h",    mins=240,  warmup=200),
    "1D":  dict(primary="1d",  htf="1w",  resample="1D",    mins=1440, warmup=200),
}
# 5 TFs เท่านั้น: 15m / 30m / 1H / 4H / 1D
DEFAULT_TFS = ["15m", "30m", "1H", "4H", "1D"]

TIMEOUT_H  = 48    # ชั่วโมงก่อน timeout
TP1_R      = 1.2
TP2_R      = 2.0
RISK_USD   = 10.0
API_LIMIT  = 300
OUTPUT_CSV = "backtest_live.csv"

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════
exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})
print("Loading OKX markets...", end=" ", flush=True)
exchange.load_markets()
print(f"{len(exchange.markets)} pairs")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def days_to_candles(days: float, mins: int) -> int:
    return int(days * 24 * 60 / mins)

def timeout_candles(mins: int) -> int:
    """TIMEOUT_H hours → candle count สำหรับ TF นั้น"""
    return max(2, int(TIMEOUT_H * 60 / mins))


# ══════════════════════════════════════════════════════════════════════════════
# PAGINATED FETCH
# ══════════════════════════════════════════════════════════════════════════════
def fetch_paginated(symbol: str, tf_str: str, days: float) -> pd.DataFrame:
    """ดึง OHLCV ย้อนหลัง N วัน พร้อม pagination (OKX limit 300/call)"""
    since_dt = datetime.now(timezone.utc) - timedelta(days=days + 0.1)
    since_ms = int(since_dt.timestamp() * 1000)
    all_bars: list = []

    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, tf_str, since=since_ms, limit=API_LIMIT)
        except Exception as e:
            print(f"\n  [WARN] fetch {symbol} {tf_str}: {e}")
            break
        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < API_LIMIT:
            break
        since_ms = bars[-1][0] + 1
        time_mod.sleep(0.2)

    if not all_bars:
        return pd.DataFrame()
    df = pd.DataFrame(all_bars, columns=["ts","open","high","low","close","volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    return df[["open","high","low","close","volume"]].astype(float)


# ══════════════════════════════════════════════════════════════════════════════
# MONKEY-PATCH _htf_bias ให้ resample ตาม primary TF
# ══════════════════════════════════════════════════════════════════════════════
_ORIG_HTF_BIAS = TREND._htf_bias

def _make_htf_bias(resample_freq: str):
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
        df_htf["h7"]       = EMAIndicator(df_htf["close"], EMA_FAST).ema_indicator()
        df_htf["h30"]      = EMAIndicator(df_htf["close"], EMA_SLOW).ema_indicator()
        df_htf["h99"]      = SMAIndicator(df_htf["close"], SMA_99).sma_indicator()
        df_htf["htf_bull"] = df_htf["h7"]  > df_htf["h30"]
        df_htf["htf_sma"]  = df_htf["close"] > df_htf["h99"]
        try:
            htf_rs = df_htf[["htf_bull","htf_sma"]].resample(resample_freq).ffill()
            df_primary = df_primary.join(htf_rs, how="left")
        except Exception:
            df_primary = df_primary.copy()
        df_primary["htf_bull"] = df_primary["htf_bull"].ffill().fillna(True)
        df_primary["htf_sma"]  = df_primary["htf_sma"].ffill().fillna(True)
        return df_primary

    return _patched

def patch_htf(resample_freq: str):
    TREND._htf_bias = _make_htf_bias(resample_freq)

def restore_htf():
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
# WALK-FORWARD BACKTEST (1 symbol, 1 TF)
# ══════════════════════════════════════════════════════════════════════════════
def backtest_symbol(sym: str, tf_name: str, df_p: pd.DataFrame, df_h: pd.DataFrame):
    cfg     = TF_CONFIGS[tf_name]
    warmup  = cfg["warmup"]
    timeout = timeout_candles(cfg["mins"])
    n       = len(df_p)

    trades, in_trade, entry_data, candles_open, scanned = [], False, {}, 0, 0

    for i in range(warmup, n):
        if in_trade:
            row  = df_p.iloc[i]
            hi, lo = row["high"], row["low"]
            ep, sl   = entry_data["ep"], entry_data["sl_orig"]
            tp1, tp2 = entry_data["tp1"], entry_data["tp2"]
            side, tp1h = entry_data["side"], entry_data["tp1_hit"]
            active_sl  = ep if tp1h else sl

            hit_tp1 = (not tp1h) and ((side=="LONG" and hi>=tp1) or (side=="SHORT" and lo<=tp1))
            hit_tp2 = tp1h       and ((side=="LONG" and hi>=tp2) or (side=="SHORT" and lo<=tp2))
            hit_sl  = ((side=="LONG" and lo<=active_sl) or (side=="SHORT" and hi>=active_sl))
            timed_out = candles_open >= timeout

            if hit_tp1 and not hit_sl:
                entry_data["tp1_hit"] = True
                candles_open += 1
                continue

            if hit_tp2:
                pnl, out = calc_pnl(side, ep, sl, tp1, tp2, True)
                _record(trades, entry_data, tf_name, i, df_p, tp2, "TP2", out, pnl)
                in_trade = False
            elif hit_sl:
                exit_px   = active_sl
                exit_type = "SL_BE" if tp1h else "SL"
                pnl, out  = calc_pnl(side, ep, sl, tp1, exit_px, tp1h)
                _record(trades, entry_data, tf_name, i, df_p, exit_px, exit_type, out, pnl)
                in_trade = False
            elif timed_out:
                exit_px = row["close"]
                pnl, out = calc_pnl(side, ep, sl, tp1, exit_px, tp1h)
                _record(trades, entry_data, tf_name, i, df_p, exit_px, "TIMEOUT", out, pnl)
                in_trade = False

            candles_open += 1

        else:
            slice_p   = df_p.iloc[:i]
            last_ts   = slice_p.index[-1]
            slice_h   = df_h[df_h.index <= last_ts] if not df_h.empty else df_h

            try:
                sig, _ = SCANNER.scan_symbol(sym, slice_p, slice_h, "FUTURES")
            except Exception:
                sig = None
            scanned += 1

            if sig and abs(sig["price"] - sig["sl"]) > sig["price"] * 0.0001:
                entry_data = {
                    "sym":         sym,
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
                in_trade, candles_open = True, 0

    return pd.DataFrame(trades), scanned


def _record(trades, ed, tf_name, i, df, exit_px, exit_type, outcome, pnl):
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
        "exit_ts":     str(df.index[i]),
        "exit_px":     round(exit_px, 8),
        "exit_type":   exit_type,
        "tp1_hit":     ed["tp1_hit"],
        "outcome":     outcome,
        "pnl":         pnl,
    })


# ══════════════════════════════════════════════════════════════════════════════
# METRICS & REPORT
# ══════════════════════════════════════════════════════════════════════════════
def metrics(df: pd.DataFrame):
    if len(df) < 3:
        return None
    wins   = df["outcome"] == "WIN"
    wr     = wins.mean() * 100
    tp1r   = df["tp1_hit"].mean() * 100
    total  = df["pnl"].sum()
    avg    = df["pnl"].mean()
    eq     = df["pnl"].cumsum()
    pk     = eq.cummax()
    dd     = ((eq - pk) / pk.abs().replace(0, 1) * 100).min()
    std    = df["pnl"].std()
    sharpe = (avg / std) * (252 ** 0.5) if std > 0 else 0
    return {
        "n":      len(df),
        "wr":     round(wr, 1),
        "tp1r":   round(tp1r, 1),
        "total":  round(total, 2),
        "avg":    round(avg, 2),
        "dd":     round(dd, 2),
        "sharpe": round(sharpe, 2),
    }


def verdict(m):
    if not m:                                                         return "❌  ข้อมูลไม่พอ"
    if m["wr"]>=55 and m["sharpe"]>=1.2 and abs(m["dd"])<=20:        return "✅  STRONG PASS"
    if m["wr"]>=48 and m["sharpe"]>=0.8 and abs(m["dd"])<=30:        return "✅  PASS"
    if m["wr"]>=42 and m["sharpe"]>=0.4:                             return "⚠️   MARGINAL"
    return "❌  FAIL"


def print_tf_summary(tf_results: dict):
    """Print ตาราง TF comparison แบบเปรียบเทียบ"""
    sep = "═" * 70
    print(f"\n{sep}")
    print(f"  TF PERFORMANCE COMPARISON")
    print(sep)
    print(f"  {'TF':<6}  {'Trades':>6}  {'WR%':>6}  {'Sharpe':>7}  {'MaxDD%':>7}  {'Total$':>8}  Verdict")
    print("  " + "─" * 66)
    for tf, m in tf_results.items():
        if m:
            v = verdict(m)
            print(f"  {tf:<6}  {m['n']:>6,}  {m['wr']:>5.1f}%  {m['sharpe']:>7.2f}  "
                  f"{m['dd']:>6.1f}%  ${m['total']:>8.2f}  {v}")
        else:
            print(f"  {tf:<6}  {'—':>6}  {'—':>6}  {'—':>7}  {'—':>7}  {'—':>8}  ❌ ไม่มีข้อมูล")
    print(sep)

    # Best TF
    scored = {tf: m for tf, m in tf_results.items() if m and m["n"] >= 5}
    if scored:
        best_tf = max(scored, key=lambda t: scored[t]["sharpe"])
        bm = scored[best_tf]
        print(f"\n  🏆 Best TF: {best_tf}  "
              f"(Sharpe {bm['sharpe']:.2f}, WR {bm['wr']:.1f}%, "
              f"MaxDD {bm['dd']:.1f}%, {bm['n']} trades)")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="WAKAM3 Multi-TF Walk-Forward Backtest")
    parser.add_argument("--symbols", nargs="+",  help="เลือก symbols เช่น BTC/USDT ETH/USDT")
    parser.add_argument("--tf",      nargs="+",  help=f"เลือก TF เช่น 1H 4H (default: {DEFAULT_TFS})")
    parser.add_argument("--days",    type=float, default=None,
                        help="ย้อนหลังกี่วัน (default: 7)")
    args = parser.parse_args()

    symbols = args.symbols or DEFAULT_SYMBOLS
    tfs     = args.tf or DEFAULT_TFS
    days    = args.days or 7.0

    print("=" * 70)
    print("  WAKAM3 — Multi-TF Walk-Forward Backtest (Time-based)")
    print(f"  MIN_SCORE={SCANNER.MIN_SCORE} | TP1=RR{TP1_R} | TP2=RR{TP2_R} | Timeout={TIMEOUT_H}h")
    print(f"  Symbols : {len(symbols)} | TFs : {', '.join(tfs)} | Period : {days:.0f} days")
    print("=" * 70)

    # ── Pre-fetch data สำหรับทุก symbol × ทุก TF ที่ต้องการ ──────────────────
    # รวม TFs ที่ต้องการ (primary + htf) เพื่อ fetch ครั้งเดียว
    needed_tfs: dict[str, float] = {}
    for tf_name in tfs:
        cfg = TF_CONFIGS[tf_name]
        p_days = days + 1
        h_days = days + 3   # HTF ดึงมากกว่านิดเพื่อ warmup
        p_key  = cfg["primary"]
        h_key  = cfg["htf"]
        if p_key not in needed_tfs or needed_tfs[p_key] < p_days:
            needed_tfs[p_key] = p_days
        if h_key not in needed_tfs or needed_tfs[h_key] < h_days:
            needed_tfs[h_key] = h_days

    all_trades: list[pd.DataFrame] = []
    tf_results: dict[str, dict | None] = {}
    t_start = time_mod.time()

    for sym in symbols:
        print(f"\n{'─'*50}")
        print(f"  [{sym}]  fetching {len(needed_tfs)} TFs...")

        # ── Fetch ทุก TF ที่ sym นี้ต้องการ ──────────────────────────────────
        data_cache: dict[str, pd.DataFrame] = {}
        for tf_str, fetch_days in needed_tfs.items():
            df = fetch_paginated(sym, tf_str, fetch_days)
            data_cache[tf_str] = df
            candle_info = f"{len(df):,} candles" if not df.empty else "EMPTY"
            print(f"    {tf_str}: {candle_info}")

        # ── Backtest แต่ละ TF ────────────────────────────────────────────────
        for tf_name in tfs:
            cfg   = TF_CONFIGS[tf_name]
            df_p  = data_cache.get(cfg["primary"], pd.DataFrame())
            df_h  = data_cache.get(cfg["htf"],     pd.DataFrame())

            if df_p.empty or len(df_p) < cfg["warmup"] + 10:
                print(f"    [{tf_name}] ข้อมูลไม่พอ (ต้องการ >{cfg['warmup']} candles)")
                continue

            # patch _htf_bias ให้ resample ตาม TF นี้
            patch_htf(cfg["resample"])
            t0 = time_mod.time()

            print(f"    [{tf_name}] walking {len(df_p):,} candles...", end=" ", flush=True)
            trades_df, scanned = backtest_symbol(sym, tf_name, df_p, df_h)
            elapsed = time_mod.time() - t0
            restore_htf()

            n_t = len(trades_df)
            print(f"→ {n_t} trades  ({elapsed:.1f}s)")

            if not trades_df.empty:
                all_trades.append(trades_df)
                if tf_name not in tf_results:
                    tf_results[tf_name] = trades_df
                else:
                    tf_results[tf_name] = pd.concat([tf_results[tf_name], trades_df], ignore_index=True)

    # ── รวม trades ทั้งหมด ──────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    total_elapsed = time_mod.time() - t_start
    print(f"  เสร็จใน {total_elapsed:.0f}s")

    if not all_trades:
        print("  ไม่มี trades — ลอง --days 180 หรือตรวจสอบ MIN_SCORE")
        return

    combined = pd.concat(all_trades, ignore_index=True)
    combined.to_csv(OUTPUT_CSV, index=False)
    print(f"  บันทึก → {OUTPUT_CSV}  ({len(combined):,} trades)")

    # ── Per-TF metrics ──────────────────────────────────────────────────────
    tf_metrics: dict[str, dict | None] = {}
    for tf_name in tfs:
        if tf_name in tf_results and isinstance(tf_results[tf_name], pd.DataFrame):
            tf_metrics[tf_name] = metrics(tf_results[tf_name])
        else:
            tf_metrics[tf_name] = None

    print_tf_summary(tf_metrics)

    # ── Combined overall ────────────────────────────────────────────────────
    m_all = metrics(combined)
    if m_all:
        print(f"  COMBINED ({len(combined):,} trades)  "
              f"WR {m_all['wr']}%  Sharpe {m_all['sharpe']:.2f}  "
              f"MaxDD {m_all['dd']:.1f}%  Total ${m_all['total']:.2f}")
        print(f"  {verdict(m_all)}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
