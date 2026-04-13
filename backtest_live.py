"""
backtest_live.py — Walk-Forward Backtest ใช้ Signal Scanner + 3 Agents จริง
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Flow:
  1. ดึง OHLCV ย้อนหลังจาก OKX
  2. Walk-forward: ทุก candle i → ส่ง df[:i] ให้ scan_symbol()
  3. ถ้า signal → เปิด trade จำลอง (SL/TP/Timeout เหมือน paper_trade)
  4. Print metrics + export backtest_live.csv

PnL คำนวณแบบ Fixed Risk $10 ต่อ trade เพื่อให้เปรียบเทียบ % ได้
  - Full LOSS      : -$10
  - TP2 (full)     : +$16  (0.5×RR1.2 + 0.5×RR2.0) × $10
  - SL ที่ Breakeven: +$6   (0.5×RR1.2 + 0) × $10
  - Timeout        : proportional

Usage:
  py backtest_live.py                        # default 10 symbols, 500 candles
  py backtest_live.py --fast                 # 5 symbols, 300 candles (เร็วกว่า)
  py backtest_live.py --symbols BTC/USDT ETH/USDT --candles 700
"""
import sys, os, warnings, time, argparse
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from datetime import datetime, timezone
import pandas as pd
warnings.filterwarnings("ignore")

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt"); sys.exit(1)

import signal_scanner as SCANNER

# ── ปิด DB writes ระหว่าง backtest (ไม่ต้องการ pollute condition DB) ──────────
SCANNER.save_condition_snapshot = lambda *a, **kw: None

# ── CONFIG DEFAULT ────────────────────────────────────────────────────────────
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT", "SUI/USDT",
]
FAST_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

TF_1H       = "1h"
TF_4H       = "4h"
CANDLES_4H  = 200
WARMUP      = 250    # แท่งแรกที่ scan ได้ (ต้องการ SMA200=200 + buffer 50)
TP1_R       = 1.2
TP2_R       = 2.0
TIMEOUT_H   = 48
RISK_USD    = 10.0   # risk per trade (fixed, สำหรับ backtest)

exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

print("Loading OKX markets...", end=" ", flush=True)
exchange.load_markets()
print(f"{len(exchange.markets)} pairs")


# ── FETCH ─────────────────────────────────────────────────────────────────────
def fetch(symbol, tf, limit):
    try:
        bars = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df   = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("dt", inplace=True)
        return df[["open","high","low","close","volume"]].astype(float)
    except Exception as e:
        print(f"  [WARN] fetch {symbol} {tf}: {e}")
        return pd.DataFrame()


# ── PnL CALCULATION ───────────────────────────────────────────────────────────
def calc_pnl(side, ep, sl_orig, tp1_px, exit_px, tp1_was_hit):
    """
    คำนวณ PnL จาก fixed risk $10
    trade แบ่ง 2 halves (50%/50%):
      half1 → ออกที่ TP1 (ถ้า TP1 hit) หรือ exit_px
      half2 → ออกที่ exit_px (TP2 / SL-breakeven / timeout)
    """
    dist_sl = abs(ep - sl_orig)
    if dist_sl < ep * 0.0001:
        return 0.0, "VOID"

    def to_r(px):
        return (px - ep) / dist_sl if side == "LONG" else (ep - px) / dist_sl

    if tp1_was_hit:
        r1 = to_r(tp1_px)   # +TP1_R = +1.2
        r2 = to_r(exit_px)  # TP2 = +2.0 | BE = 0 | timeout = ?
        r  = 0.5 * r1 + 0.5 * r2
    else:
        r = to_r(exit_px)   # full position ออกที่ exit

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

    for i in range(WARMUP, n):
        if in_trade:
            row  = df_1h.iloc[i]
            hi   = row["high"]
            lo   = row["low"]
            ep   = entry_data["ep"]
            sl   = entry_data["sl_orig"]
            tp1  = entry_data["tp1"]
            tp2  = entry_data["tp2"]
            side = entry_data["side"]
            tp1h = entry_data["tp1_hit"]

            # SL ขยับเป็น Breakeven หลัง TP1
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
                # TP1 hit → mark, move SL to BE, ยังไม่ปิด
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
                    exit_type = "SL_BE"  # SL ที่ breakeven (หลัง TP1)
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
            # ── Walk-forward: ส่ง slice ให้ Signal Scanner ──────────────────
            slice_1h = df_1h.iloc[:i]
            last_ts  = slice_1h.index[-1]
            slice_4h = df_4h[df_4h.index <= last_ts] if not df_4h.empty else df_4h

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
    kz_wr   = wins[kz_mask].mean() * 100 if kz_mask.any() else float("nan")
    nkz_wr  = wins[~kz_mask].mean() * 100 if (~kz_mask).any() else float("nan")

    return {
        "n":        len(df),
        "wr":       round(wr, 1),
        "tp1r":     round(tp1r, 1),
        "total":    round(total, 2),
        "avg":      round(avg, 2),
        "dd":       round(dd, 2),
        "sharpe":   round(sharpe, 2),
        "kz_wr":    round(kz_wr,  1),
        "nkz_wr":   round(nkz_wr, 1),
        "by_exit":  by_exit,
        "by_regime":by_regime,
    }


def verdict(m):
    if not m:                                                    return "❌  ข้อมูลไม่พอ"
    if m["wr"] >= 55 and m["sharpe"] >= 1.2 and abs(m["dd"]) <= 20: return "✅  STRONG PASS"
    if m["wr"] >= 48 and m["sharpe"] >= 0.8 and abs(m["dd"]) <= 30: return "✅  PASS"
    if m["wr"] >= 42 and m["sharpe"] >= 0.4:                    return "⚠️   MARGINAL"
    return "❌  FAIL"


def report(m, label):
    sep = "─" * 54
    print(f"\n{sep}")
    print(f"  {label}")
    print(sep)
    if not m:
        print("  trades น้อยเกินไป (< 3)")
        return
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
    print(f"\n  {verdict(m)}")
    print(sep)


def score_analysis(df):
    """Win rate แยกตาม score band"""
    if "score" not in df.columns or len(df) < 5:
        return
    df2 = df.copy()
    df2["score_band"] = pd.cut(df2["score"], bins=[0,10,15,20,25,31],
                               labels=["≤10","11-15","16-20","21-25","≥26"])
    sc = df2.groupby("score_band", observed=True).agg(
        n      = ("outcome", "count"),
        wr     = ("outcome", lambda x: round((x=="WIN").mean()*100, 1)),
        avg_pnl= ("pnl", lambda x: round(x.mean(), 2)),
    )
    print("\n  SCORE BAND ANALYSIS:")
    print(sc.to_string())


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="WAKAM3 Walk-Forward Backtest")
    parser.add_argument("--fast",    action="store_true", help="5 symbols, 300 candles")
    parser.add_argument("--symbols", nargs="+",           help="เลือก symbols เช่น BTC/USDT ETH/USDT")
    parser.add_argument("--candles", type=int, default=500, help="จำนวน 1H candles (default 500)")
    args = parser.parse_args()

    if args.symbols:
        symbols   = args.symbols
        candles1h = args.candles
    elif args.fast:
        symbols   = FAST_SYMBOLS
        candles1h = 300
    else:
        symbols   = DEFAULT_SYMBOLS
        candles1h = args.candles

    print("=" * 54)
    print("  AI TRADE — Walk-Forward Backtest (Live Logic)")
    print(f"  Signal Scanner + 3 Agents | MIN_SCORE={SCANNER.MIN_SCORE}")
    print(f"  TP1=RR{TP1_R} | TP2=RR{TP2_R} | Timeout={TIMEOUT_H}h")
    print(f"  Symbols : {len(symbols)} | History : {candles1h} x 1H candles")
    print(f"  Warmup  : {WARMUP} candles | Scan window : {candles1h - WARMUP}")
    print("=" * 54)

    all_trades   = []
    total_scans  = 0
    t_start      = time.time()

    for sym in symbols:
        print(f"\n[{sym}]  fetching...", end=" ", flush=True)
        df_1h = fetch(sym, TF_1H, candles1h)
        df_4h = fetch(sym, TF_4H, CANDLES_4H)

        if df_1h.empty or len(df_1h) < WARMUP + 10:
            print("ข้อมูลไม่พอ")
            continue

        print(f"1H:{len(df_1h)}  4H:{len(df_4h)}")

        t0 = time.time()
        trades_df, scanned = backtest_symbol(sym, df_1h, df_4h)
        elapsed = time.time() - t0
        total_scans += scanned

        n_trades = len(trades_df)
        print(f"  scanned {scanned} candles → {n_trades} trades  ({elapsed:.1f}s)")

        if not trades_df.empty:
            m = metrics(trades_df)
            report(m, f"{sym}  [{n_trades} trades]")
            all_trades.append(trades_df)

    # ── Combined ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 54)
    total_elapsed = time.time() - t_start
    print(f"  เสร็จใน {total_elapsed:.0f}s | scan calls รวม {total_scans:,}")
    print("=" * 54)

    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        combined.to_csv("backtest_live.csv", index=False)

        m = metrics(combined)
        report(m, f"COMBINED — {len(symbols)} symbols  [{len(combined)} trades]")
        score_analysis(combined)

        print(f"\n  บันทึก → backtest_live.csv ({len(combined)} trades)")
    else:
        print("  ไม่มี trades เลย — ลอง --candles 700 หรือลด MIN_SCORE")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
