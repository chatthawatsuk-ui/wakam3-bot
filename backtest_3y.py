"""
backtest_3y.py — Walk-Forward Backtest ย้อนหลัง 3 ปี
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ต่างจาก backtest_live.py:
  - Paginated OKX fetch  → 3 ปี ของ 1H candles (≈26,280 แท่ง)
  - Rolling window 500 candles ส่งให้ scanner (ไม่ใช้ growing slice)
  - Scan ทุก SCAN_STEP candles เพื่อลดรอบการคำนวณ
  - Target runtime: < 40 min สำหรับ 5 symbols บน GitHub Actions

Output: backtest_3y.csv  (columns เหมือน backtest_live.csv)

Usage:
  py backtest_3y.py                       # 5 symbols, 3 ปี
  py backtest_3y.py --years 1             # 1 ปี (เร็วกว่า)
  py backtest_3y.py --symbols BTC/USDT ETH/USDT
  py backtest_3y.py --step 1              # scan ทุก candle (ช้า แต่แม่น)
"""

import sys, os, warnings, time as time_mod, argparse
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timezone, timedelta
import pandas as pd

warnings.filterwarnings("ignore")

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt"); sys.exit(1)

import signal_scanner as SCANNER

# ── ปิด DB writes ระหว่าง backtest ──────────────────────────────────────────
SCANNER.save_condition_snapshot    = lambda *a, **kw: None
SCANNER.save_specialist_history    = lambda *a, **kw: None

# ── CONFIG ────────────────────────────────────────────────────────────────────
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
]
EXTENDED_SYMBOLS = DEFAULT_SYMBOLS + [
    "DOGE/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT", "SUI/USDT",
]

TF_1H       = "1h"
TF_4H       = "4h"
WARMUP      = 250       # candles แรกที่รอ indicator warm-up
WINDOW_1H   = 500       # rolling window ขนาดคงที่ที่ส่งให้ scanner
CANDLES_4H  = 500       # 4H window สำหรับ HTF context
SCAN_STEP   = 4         # scan ทุก N candles (4 = เทียบเท่า 4H)
TP1_R       = 1.2
TP2_R       = 2.0
TIMEOUT_H   = 48
RISK_USD    = 10.0
OUTPUT_CSV  = "backtest_3y.csv"
API_LIMIT   = 300       # max candles per OKX API call

exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

print("Loading OKX markets...", end=" ", flush=True)
exchange.load_markets()
print(f"{len(exchange.markets)} pairs")


# ── PAGINATED FETCH ───────────────────────────────────────────────────────────
def fetch_paginated(symbol, tf, years=3):
    """
    ดึงข้อมูล OHLCV ย้อนหลัง N ปี โดยใช้ pagination
    Returns: DataFrame sorted ascending
    """
    since_dt  = datetime.now(timezone.utc) - timedelta(days=int(years * 365))
    since_ms  = int(since_dt.timestamp() * 1000)
    all_bars  = []
    page      = 0

    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, tf, since=since_ms, limit=API_LIMIT)
        except Exception as e:
            print(f"\n  [WARN] fetch {symbol} {tf} page {page}: {e}")
            time_mod.sleep(1)
            break

        if not bars:
            break

        all_bars.extend(bars)
        page += 1

        if len(bars) < API_LIMIT:
            break  # last page

        since_ms = bars[-1][0] + 1  # next page start
        time_mod.sleep(0.25)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts","open","high","low","close","volume"])
    df.drop_duplicates("ts", inplace=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    df = df[["open","high","low","close","volume"]].astype(float).sort_index()
    return df


# ── PnL CALCULATION (เหมือน backtest_live.py) ─────────────────────────────────
def calc_pnl(side, ep, sl_orig, tp1_px, exit_px, tp1_was_hit):
    dist_sl = abs(ep - sl_orig)
    if dist_sl < ep * 0.0001:
        return 0.0, "VOID"

    def to_r(px):
        return (px - ep) / dist_sl if side == "LONG" else (ep - px) / dist_sl

    if tp1_was_hit:
        r1 = to_r(tp1_px)
        r2 = to_r(exit_px)
        r  = 0.5 * r1 + 0.5 * r2
    else:
        r = to_r(exit_px)

    pnl     = round(RISK_USD * r, 2)
    outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
    return pnl, outcome


# ── WALK-FORWARD BACKTEST ─────────────────────────────────────────────────────
def backtest_symbol(sym, df_1h, df_4h):
    trades       = []
    in_trade     = False
    entry_data   = {}
    candles_open = 0
    n            = len(df_1h)
    scanned      = 0
    skipped      = 0

    for i in range(WARMUP, n):
        row = df_1h.iloc[i]

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
            timeout = candles_open >= TIMEOUT_H

            if hit_tp1 and not hit_sl:
                entry_data["tp1_hit"] = True
                candles_open += 1
                continue

            if hit_tp2:
                pnl, outcome = calc_pnl(side, ep, sl, tp1, tp2, True)
                _record(trades, entry_data, i, df_1h, tp2, "TP2", outcome, pnl)
                in_trade = False

            elif hit_sl:
                exit_px = active_sl
                if tp1h:
                    pnl, outcome = calc_pnl(side, ep, sl, tp1, exit_px, True)
                    exit_type = "SL_BE"
                else:
                    pnl, outcome = -RISK_USD, "LOSS"
                    exit_type = "SL"
                _record(trades, entry_data, i, df_1h, exit_px, exit_type, outcome, pnl)
                in_trade = False

            elif timeout:
                exit_px = row["close"]
                pnl, outcome = calc_pnl(side, ep, sl, tp1, exit_px, tp1h)
                _record(trades, entry_data, i, df_1h, exit_px, "TIMEOUT", outcome, pnl)
                in_trade = False

            candles_open += 1

        else:
            # ── Scan ทุก SCAN_STEP candles ──────────────────────────────────
            if (i - WARMUP) % SCAN_STEP != 0:
                skipped += 1
                continue

            # Rolling window: ส่งแค่ WINDOW_1H candles ล่าสุด
            win_start = max(0, i - WINDOW_1H)
            slice_1h  = df_1h.iloc[win_start:i]
            last_ts   = slice_1h.index[-1]
            slice_4h  = df_4h[df_4h.index <= last_ts].iloc[-CANDLES_4H:]

            if len(slice_1h) < WARMUP:
                continue

            try:
                sig, _ = SCANNER.scan_symbol(sym, slice_1h, slice_4h, "FUTURES")
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
                    "entry_i":     i,
                    "entry_ts":    str(df_1h.index[i - 1]),
                    "tp1_hit":     False,
                }
                in_trade     = True
                candles_open = 0

    return pd.DataFrame(trades), scanned


def _record(trades, ed, i, df_1h, exit_px, exit_type, outcome, pnl):
    trades.append({
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
        "exit_ts":     str(df_1h.index[i]),
        "exit_px":     round(exit_px, 8),
        "exit_type":   exit_type,
        "tp1_hit":     ed["tp1_hit"],
        "outcome":     outcome,
        "pnl":         pnl,
    })


# ── METRICS ───────────────────────────────────────────────────────────────────
def metrics(df):
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

    by_exit = df.groupby("exit_type").agg(
        n     = ("pnl", "count"),
        wins  = ("outcome", lambda x: (x == "WIN").sum()),
        avg_p = ("pnl", "mean"),
    )
    by_exit["wr%"] = (by_exit["wins"] / by_exit["n"] * 100).round(1)

    by_regime = df.groupby("regime").agg(
        n  = ("pnl", "count"),
        wr = ("outcome", lambda x: (x == "WIN").mean() * 100),
        ap = ("pnl", "mean"),
    ).round(1)

    kz_mask = df["in_kz"].astype(bool)
    kz_wr   = wins[kz_mask].mean() * 100  if kz_mask.any()  else float("nan")
    nkz_wr  = wins[~kz_mask].mean() * 100 if (~kz_mask).any() else float("nan")

    # Yearly breakdown
    df2 = df.copy()
    df2["year"] = pd.to_datetime(df2["entry_ts"]).dt.year
    by_year = df2.groupby("year").agg(
        n   = ("pnl", "count"),
        wr  = ("outcome", lambda x: round((x=="WIN").mean()*100, 1)),
        pnl = ("pnl", "sum"),
    ).round(2)

    return {
        "n":        len(df),
        "wr":       round(wr,    1),
        "tp1r":     round(tp1r,  1),
        "total":    round(total, 2),
        "avg":      round(avg,   2),
        "dd":       round(dd,    2),
        "sharpe":   round(sharpe,2),
        "kz_wr":    round(kz_wr, 1),
        "nkz_wr":   round(nkz_wr,1),
        "by_exit":  by_exit,
        "by_regime":by_regime,
        "by_year":  by_year,
    }


def verdict(m):
    if not m:                                                         return "❌  ข้อมูลไม่พอ"
    if m["wr"] >= 55 and m["sharpe"] >= 1.2 and abs(m["dd"]) <= 20: return "✅  STRONG PASS"
    if m["wr"] >= 48 and m["sharpe"] >= 0.8 and abs(m["dd"]) <= 30: return "✅  PASS"
    if m["wr"] >= 42 and m["sharpe"] >= 0.4:                        return "⚠️   MARGINAL"
    return "❌  FAIL"


def report(m, label):
    sep = "─" * 58
    print(f"\n{sep}")
    print(f"  {label}")
    print(sep)
    if not m:
        print("  trades น้อยเกินไป (< 3)"); return
    print(f"  Trades     : {m['n']:>6,}")
    print(f"  Win Rate   : {m['wr']:>6.1f}%")
    print(f"  TP1 Hit %  : {m['tp1r']:>6.1f}%")
    print(f"  Kill Zone  : WR {m['kz_wr']:.1f}%  vs  non-KZ {m['nkz_wr']:.1f}%")
    print(f"  Avg PnL    : ${m['avg']:>7.2f}")
    print(f"  Total PnL  : ${m['total']:>8.2f}")
    print(f"  Max DD     : {m['dd']:>6.2f}%")
    print(f"  Sharpe     : {m['sharpe']:>6.2f}")
    print(f"\n  Exit Types:")
    print(m["by_exit"][["n","wr%","avg_p"]].to_string())
    print(f"\n  Regime Breakdown:")
    print(m["by_regime"].to_string())
    print(f"\n  Yearly Breakdown:")
    print(m["by_year"].to_string())
    print(f"\n  {verdict(m)}")
    print(sep)


def score_analysis(df):
    if "score" not in df.columns or len(df) < 5:
        return
    df2 = df.copy()
    df2["score_band"] = pd.cut(
        df2["score"], bins=[0,10,15,20,25,31],
        labels=["≤10","11-15","16-20","21-25","≥26"]
    )
    sc = df2.groupby("score_band", observed=True).agg(
        n       = ("outcome", "count"),
        wr      = ("outcome", lambda x: round((x=="WIN").mean()*100, 1)),
        avg_pnl = ("pnl",     lambda x: round(x.mean(), 2)),
    )
    print("\n  SCORE BAND ANALYSIS:")
    print(sc.to_string())


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="WAKAM3 3-Year Walk-Forward Backtest")
    parser.add_argument("--years",   type=float, default=3,    help="จำนวนปีย้อนหลัง (default 3)")
    parser.add_argument("--symbols", nargs="+",                help="เลือก symbols")
    parser.add_argument("--step",    type=int,   default=4,    help="scan ทุก N candles (default 4)")
    parser.add_argument("--extended",action="store_true",      help="ใช้ 10 symbols แทน 5")
    args = parser.parse_args()

    global SCAN_STEP
    SCAN_STEP = args.step

    symbols = args.symbols or (EXTENDED_SYMBOLS if args.extended else DEFAULT_SYMBOLS)

    candle_est = int(args.years * 365 * 24)

    print("=" * 58)
    print("  AI TRADE — Walk-Forward Backtest (3-Year Edition)")
    print(f"  Signal Scanner + 3 Agents | MIN_SCORE={SCANNER.MIN_SCORE}")
    print(f"  TP1=RR{TP1_R} | TP2=RR{TP2_R} | Timeout={TIMEOUT_H}h")
    print(f"  Period  : {args.years} years  (~{candle_est:,} 1H candles/symbol)")
    print(f"  Window  : rolling {WINDOW_1H} candles | Step : every {SCAN_STEP}")
    print(f"  Symbols : {', '.join(s.replace('/USDT','') for s in symbols)}")
    print("=" * 58)

    all_trades  = []
    total_scans = 0
    t_start     = time_mod.time()

    for sym in symbols:
        print(f"\n[{sym}]  fetching {args.years}y of 1H data...", end=" ", flush=True)
        t_fetch = time_mod.time()
        df_1h = fetch_paginated(sym, TF_1H, args.years)
        df_4h = fetch_paginated(sym, TF_4H, args.years)
        print(f"1H:{len(df_1h)}  4H:{len(df_4h)}  ({time_mod.time()-t_fetch:.1f}s)")

        if df_1h.empty or len(df_1h) < WARMUP + 10:
            print("  ข้อมูลไม่พอ — ข้าม")
            continue

        t0 = time_mod.time()
        trades_df, scanned = backtest_symbol(sym, df_1h, df_4h)
        elapsed = time_mod.time() - t0
        total_scans += scanned

        n_trades = len(trades_df)
        print(f"  scanned {scanned:,} windows → {n_trades} trades  ({elapsed:.1f}s)")

        if not trades_df.empty:
            m = metrics(trades_df)
            report(m, f"{sym}  [{n_trades} trades | {args.years}y]")
            all_trades.append(trades_df)

    # ── Combined ───────────────────────────────────────────────────────────────
    total_elapsed = time_mod.time() - t_start
    print("\n" + "=" * 58)
    print(f"  เสร็จใน {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
    print(f"  scan calls รวม : {total_scans:,}")
    print("=" * 58)

    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)

        # เรียงตาม entry_ts เพื่อให้ equity curve ถูกต้อง
        combined["entry_ts_dt"] = pd.to_datetime(combined["entry_ts"], utc=True, errors="coerce")
        combined = combined.sort_values("entry_ts_dt").drop(columns="entry_ts_dt")
        combined.reset_index(drop=True, inplace=True)

        combined.to_csv(OUTPUT_CSV, index=False)

        m = metrics(combined)
        report(m, f"COMBINED — {len(symbols)} symbols  [{len(combined)} trades | {args.years}y]")
        score_analysis(combined)

        print(f"\n  บันทึก → {OUTPUT_CSV}  ({len(combined)} trades)")
    else:
        print("\n  ไม่มี trades — ไม่สร้าง CSV")


if __name__ == "__main__":
    main()
