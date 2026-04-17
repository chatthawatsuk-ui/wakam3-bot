"""
backtest_3y.py — Walk-Forward Backtest ย้อนหลัง 3 ปี (Multi-TF Edition)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ทดสอบทุก TF แล้วสรุป Leaderboard ว่า TF ไหน win rate / sharpe ดีสุด

TF Pairs ที่ทดสอบ:
  15m → HTF 1h   | warmup 250  | window 500  | timeout 192c | step 16
  30m → HTF 2h   | warmup 250  | window 500  | timeout  96c | step  8
  1h  → HTF 4h   | warmup 250  | window 500  | timeout  48c | step  4
  2h  → HTF 4h   | warmup 150  | window 300  | timeout  24c | step  2
  4h  → HTF 1d   | warmup 100  | window 200  | timeout  12c | step  1
  1d  → HTF 1d   | warmup  50  | window 100  | timeout   2c | step  1

Output:
  backtest_3y.csv       — ทุก trades รวมกัน (มีคอลัมน์ tf)
  backtest_3y_tf.csv    — สรุป metrics แยกตาม TF (TF Leaderboard)

Usage:
  py backtest_3y.py                           # ทุก TF, 5 symbols, 3Y
  py backtest_3y.py --tf 1h 4h               # เลือก TF ที่ต้องการ
  py backtest_3y.py --symbols BTC/USDT        # symbol เดียว
  py backtest_3y.py --years 1                 # 1 ปี (เร็วกว่า)
  py backtest_3y.py --extended               # 10 symbols
  py backtest_3y.py --step 1                 # scan ทุก candle (ช้า แต่แม่น)
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
# ── ปิด Funding Agent (ไม่มี live funding rate ใน historical backtest) ───────
SCANNER.DISABLE_FUNDING = True

# ── SYMBOLS ───────────────────────────────────────────────────────────────────
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
]
EXTENDED_SYMBOLS = DEFAULT_SYMBOLS + [
    "DOGE/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT",
]

# ── MULTI-TF CONFIG ───────────────────────────────────────────────────────────
# (primary_tf, htf_tf, warmup_candles, window_candles, timeout_candles, scan_step)
# timeout_candles = 48h แปลงเป็น candle ตาม TF
# scan_step       = วิ่งทุกกี่ candle (ลด runtime)
TF_CONFIGS = {
    "15m": ("15m", "1h",  250, 500, 192, 16),  # 48h × 4c/h = 192c
    "30m": ("30m", "2h",  250, 500,  96,  8),  # 48h × 2c/h = 96c
    "1h":  ("1h",  "4h",  250, 500,  48,  4),  # 48h × 1c/h = 48c
    "2h":  ("2h",  "4h",  150, 300,  24,  2),  # 48h / 2h   = 24c
    "4h":  ("4h",  "1d",  100, 200,  12,  1),  # 48h / 4h   = 12c
    "1d":  ("1d",  "1d",   50, 100,   2,  1),  # 48h / 24h  =  2c
}
ALL_TFS    = ["15m", "30m", "1h", "2h", "4h", "1d"]

TP1_R            = 1.2
TP2_R            = 2.0
INITIAL_BALANCE  = 1000.0   # ยอดเริ่มต้น USD (เหมือน paper_trade.py)
RISK_PCT         = 0.01     # 1% ของ balance ณ เวลาเปิด trade (dynamic)
MAX_LEVERAGE     = 20       # 20x leverage (เหมือน paper_trade.py)
CLAUDE_MIN_SCORE = 10       # Score threshold แทน Claude filter (≥10/39 ≈ 26% — เดิม 8/31=26%)
OUTPUT_CSV       = "backtest_3y.csv"
TF_CSV           = "backtest_3y_tf.csv"
API_LIMIT  = 300
CACHE_DIR  = "historical_data"

exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

print("Loading OKX markets...", end=" ", flush=True)
exchange.load_markets()
print(f"{len(exchange.markets)} pairs")


# ── CACHE ─────────────────────────────────────────────────────────────────────
def _cache_path(symbol: str, tf: str) -> str:
    return os.path.join(CACHE_DIR, symbol.replace("/", "_") + f"_{tf}.parquet")

def _load_cache(symbol: str, tf: str, years: float) -> pd.DataFrame | None:
    fpath = _cache_path(symbol, tf)
    if not os.path.exists(fpath):
        return None
    try:
        df    = pd.read_parquet(fpath)
        since = datetime.now(timezone.utc) - timedelta(days=int(years * 365) + 1)
        df    = df[df.index >= pd.Timestamp(since)]
        return df[["open","high","low","close","volume"]].astype(float) if not df.empty else None
    except Exception:
        return None


# ── PAGINATED FETCH ───────────────────────────────────────────────────────────
def fetch_paginated(symbol, tf, years=3):
    cached = _load_cache(symbol, tf, years)
    if cached is not None:
        return cached

    since_dt = datetime.now(timezone.utc) - timedelta(days=int(years * 365))
    since_ms = int(since_dt.timestamp() * 1000)
    all_bars = []
    page     = 0

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
            break

        since_ms = bars[-1][0] + 1
        time_mod.sleep(0.25)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts","open","high","low","close","volume"])
    df.drop_duplicates("ts", inplace=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    return df[["open","high","low","close","volume"]].astype(float).sort_index()


# ── PnL ───────────────────────────────────────────────────────────────────────
def calc_pnl(side, ep, sl_orig, tp1_px, exit_px, tp1_was_hit, balance):
    """
    คำนวณ PnL แบบเดียวกับ paper_trade.py:
      margin   = balance × RISK_PCT  (1% ของยอดเงิน)
      notional = margin × MAX_LEVERAGE  (× 20x)
      pnl      = notional × (exit - entry) / entry  (LONG)
               = notional × (entry - exit) / entry  (SHORT)
    ถ้า TP1 hit → half at TP1, half at final exit
    """
    dist_sl = abs(ep - sl_orig)
    if dist_sl < ep * 0.0001:
        return 0.0, "VOID", 0.0, 0.0

    margin   = balance * RISK_PCT
    notional = margin * MAX_LEVERAGE

    def _pnl_usd(ex):
        if side == "LONG":
            return notional * (ex - ep) / ep
        else:
            return notional * (ep - ex) / ep

    if tp1_was_hit:
        raw = 0.5 * _pnl_usd(tp1_px) + 0.5 * _pnl_usd(exit_px)
    else:
        raw = _pnl_usd(exit_px)

    pnl     = round(raw, 2)
    pnl_pct = round(raw / notional * 100, 2) if notional > 0 else 0.0
    outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
    return pnl, outcome, round(notional, 2), pnl_pct


# ── WALK-FORWARD BACKTEST (generic TF) ───────────────────────────────────────
def backtest_symbol(sym, df_primary, df_htf,
                    warmup, window, timeout_candles, scan_step, tf_label,
                    df_1d=None, balance=None):
    """
    sym         — symbol เช่น BTC/USDT
    df_primary  — OHLCV ของ TF หลัก (ใช้สแกน + วัดผล)
    df_htf      — OHLCV ของ HTF (ส่งให้ scanner เป็น context)
    warmup      — candles แรกที่ข้าม (indicator warm-up)
    window      — rolling window size ที่ส่งให้ scanner
    timeout_c   — max candles ก่อน timeout (แทน 48h fixed)
    scan_step   — scan ทุก N candles
    tf_label    — "1h", "15m" ฯลฯ สำหรับบันทึกใน CSV
    df_1d       — OHLCV รายวัน สำหรับ daily Stochastic filter (เหมือน live)
    balance     — ยอดเริ่มต้น USD (dynamic sizing)
    """
    balance      = balance if balance is not None else INITIAL_BALANCE
    trades       = []
    in_trade     = False
    entry_data   = {}
    candles_open = 0
    n            = len(df_primary)
    scanned      = 0

    for i in range(warmup, n):
        row = df_primary.iloc[i]

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
            timeout = candles_open >= timeout_candles

            if hit_tp1 and not hit_sl:
                entry_data["tp1_hit"] = True
                candles_open += 1
                continue

            if hit_tp2:
                pnl, outcome, notional, pnl_pct = calc_pnl(side, ep, sl, tp1, tp2, True, entry_data["balance_at_entry"])
                _record(trades, entry_data, i, df_primary, tp2, "TP2", outcome, pnl, pnl_pct, notional, tf_label)
                balance += pnl
                balance = max(balance, 1.0)
                in_trade = False

            elif hit_sl:
                exit_px = active_sl
                if tp1h:
                    pnl, outcome, notional, pnl_pct = calc_pnl(side, ep, sl, tp1, exit_px, True, entry_data["balance_at_entry"])
                    exit_type = "SL_BE"
                else:
                    pnl, outcome, notional, pnl_pct = calc_pnl(side, ep, sl, tp1, active_sl, False, entry_data["balance_at_entry"])
                    exit_type = "SL"
                _record(trades, entry_data, i, df_primary, exit_px, exit_type, outcome, pnl, pnl_pct, notional, tf_label)
                balance += pnl
                balance = max(balance, 1.0)
                in_trade = False

            elif timeout:
                exit_px = row["close"]
                pnl, outcome, notional, pnl_pct = calc_pnl(side, ep, sl, tp1, exit_px, tp1h, entry_data["balance_at_entry"])
                _record(trades, entry_data, i, df_primary, exit_px, "TIMEOUT", outcome, pnl, pnl_pct, notional, tf_label)
                balance += pnl
                balance = max(balance, 1.0)
                in_trade = False

            candles_open += 1

        else:
            if (i - warmup) % scan_step != 0:
                continue

            win_start  = max(0, i - window)
            slice_pri  = df_primary.iloc[win_start:i]
            last_ts    = slice_pri.index[-1]
            slice_htf  = df_htf[df_htf.index <= last_ts].iloc[-500:]
            slice_1d   = df_1d[df_1d.index <= last_ts].iloc[-50:] if df_1d is not None and not df_1d.empty else None

            if len(slice_pri) < warmup:
                continue

            try:
                sig, _ = SCANNER.scan_symbol(sym, slice_pri, slice_htf, "FUTURES", slice_1d)
            except Exception:
                sig = None

            scanned += 1

            if sig and sig["score"] >= CLAUDE_MIN_SCORE and abs(sig["price"] - sig["sl"]) > sig["price"] * 0.0001:
                entry_data = {
                    "sym":              sym,
                    "tf":               tf_label,
                    "side":             sig["side"],
                    "score":            sig["score"],
                    "score_trend":      sig.get("score_trend", 0),
                    "score_smc":        sig.get("score_smc",   0),
                    "score_osc":        sig.get("score_osc",   0),
                    "score_liq":        sig.get("score_liq",   0),
                    "ep":               sig["price"],
                    "sl_orig":          sig["sl"],
                    "tp1":              sig["tp1"],
                    "tp2":              sig["tp2"],
                    "sl_pct":           sig["sl_pct"],
                    "rsi":              sig["rsi"],
                    "in_kz":            sig["in_kz"],
                    "regime":           sig.get("regime", ""),
                    "entry_i":          i,
                    "entry_ts":         str(df_primary.index[i - 1]),
                    "balance_at_entry": round(balance, 2),
                    "tp1_hit":          False,
                }
                in_trade     = True
                candles_open = 0

    return pd.DataFrame(trades), scanned, round(balance, 2)


def _record(trades, ed, i, df_primary, exit_px, exit_type, outcome, pnl, pnl_pct, notional, tf_label):
    trades.append({
        "sym":              ed["sym"],
        "tf":               tf_label,
        "side":             ed["side"],
        "score":            ed["score"],
        "score_trend":      ed["score_trend"],
        "score_smc":        ed["score_smc"],
        "score_osc":        ed["score_osc"],
        "score_liq":        ed.get("score_liq", 0),
        "ep":               ed["ep"],
        "sl":               ed["sl_orig"],
        "tp1":              ed["tp1"],
        "tp2":              ed["tp2"],
        "sl_pct":           ed["sl_pct"],
        "rsi":              ed["rsi"],
        "in_kz":            ed["in_kz"],
        "regime":           ed["regime"],
        "entry_ts":         ed["entry_ts"],
        "exit_ts":          str(df_primary.index[i]),
        "exit_px":          round(exit_px, 8),
        "exit_type":        exit_type,
        "tp1_hit":          ed["tp1_hit"],
        "outcome":          outcome,
        "pnl":              pnl,
        "pnl_pct":          pnl_pct,
        "notional":         notional,
        "balance_at_entry": ed["balance_at_entry"],
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
    kz_wr   = wins[kz_mask].mean()  * 100 if kz_mask.any()  else float("nan")
    nkz_wr  = wins[~kz_mask].mean() * 100 if (~kz_mask).any() else float("nan")

    df2 = df.copy()
    df2["year"] = pd.to_datetime(df2["entry_ts"]).dt.year
    by_year = df2.groupby("year").agg(
        n   = ("pnl", "count"),
        wr  = ("outcome", lambda x: round((x=="WIN").mean()*100, 1)),
        pnl = ("pnl", "sum"),
    ).round(2)

    return {
        "n":         len(df),
        "wr":        round(wr,    1),
        "tp1r":      round(tp1r,  1),
        "total":     round(total, 2),
        "avg":       round(avg,   2),
        "dd":        round(dd,    2),
        "sharpe":    round(sharpe,2),
        "kz_wr":     round(kz_wr, 1),
        "nkz_wr":    round(nkz_wr,1),
        "by_exit":   by_exit,
        "by_regime": by_regime,
        "by_year":   by_year,
    }


def verdict(m):
    if not m:                                                         return "❌  ข้อมูลไม่พอ"
    if m["wr"] >= 55 and m["sharpe"] >= 1.2 and abs(m["dd"]) <= 20: return "✅  STRONG PASS"
    if m["wr"] >= 48 and m["sharpe"] >= 0.8 and abs(m["dd"]) <= 30: return "✅  PASS"
    if m["wr"] >= 42 and m["sharpe"] >= 0.4:                        return "⚠️   MARGINAL"
    return "❌  FAIL"


def report(m, label):
    sep = "─" * 60
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
        df2["score"], bins=[0,10,15,20,25,30,45],
        labels=["≤10","11-15","16-20","21-25","26-30","≥31"]
    )
    sc = df2.groupby("score_band", observed=True).agg(
        n       = ("outcome", "count"),
        wr      = ("outcome", lambda x: round((x=="WIN").mean()*100, 1)),
        avg_pnl = ("pnl",     lambda x: round(x.mean(), 2)),
    )
    print("\n  SCORE BAND ANALYSIS:")
    print(sc.to_string())


# ── TF LEADERBOARD ────────────────────────────────────────────────────────────
def print_tf_leaderboard(tf_summary: list[dict]):
    """พิมพ์ตารางสรุป และหา TF ชนะ"""
    if not tf_summary:
        return

    df = pd.DataFrame(tf_summary).set_index("tf")

    # เรียงตาม wr ก่อน แล้ว sharpe
    df_sorted = df.sort_values(["wr", "sharpe"], ascending=False)

    sep = "═" * 70
    print(f"\n\n{sep}")
    print("  🏆  TF LEADERBOARD — ผลรวมทุก symbols")
    print(sep)
    header = f"  {'TF':<6} {'Trades':>7}  {'WR%':>6}  {'TP1%':>6}  {'Avg$':>7}  {'Total$':>9}  {'DD%':>7}  {'Sharpe':>7}  Verdict"
    print(header)
    print("  " + "─" * 66)

    medals = ["🥇", "🥈", "🥉"]
    for rank, (tf, row) in enumerate(df_sorted.iterrows()):
        medal   = medals[rank] if rank < 3 else "  "
        verd    = row["verdict"]
        verd_s  = "STRONG" if "STRONG" in verd else ("PASS" if "PASS" in verd else ("MARG" if "MARG" in verd else "FAIL"))
        print(f"  {medal} {tf:<5} {row['n']:>7,}  {row['wr']:>6.1f}%  {row['tp1r']:>6.1f}%  "
              f"${row['avg']:>6.2f}  ${row['total']:>8.2f}  {row['dd']:>6.2f}%  "
              f"{row['sharpe']:>7.2f}  {verd_s}")

    print(sep)

    # ผู้ชนะ
    winner = df_sorted.index[0]
    w      = df_sorted.iloc[0]
    print(f"\n  ✅  TF ที่ดีที่สุด: {winner}  "
          f"WR {w['wr']:.1f}%  Sharpe {w['sharpe']:.2f}  {w['verdict']}")
    print(sep)

    # Save leaderboard CSV
    df_sorted.reset_index().to_csv(TF_CSV, index=False)
    print(f"  บันทึก → {TF_CSV}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="WAKAM3 Multi-TF Walk-Forward Backtest")
    parser.add_argument("--years",    type=float, default=3,
                        help="จำนวนปีย้อนหลัง (default 3)")
    parser.add_argument("--symbols",  nargs="+",
                        help="เลือก symbols")
    parser.add_argument("--tf",       nargs="+", default=None,
                        help=f"TFs ที่จะทดสอบ (default: ทุก TF = {ALL_TFS})")
    parser.add_argument("--step",     type=int, default=None,
                        help="override scan step ทุก TF (default ตาม TF config)")
    parser.add_argument("--extended", action="store_true",
                        help="ใช้ 10 symbols แทน 5")
    args = parser.parse_args()

    symbols  = args.symbols or (EXTENDED_SYMBOLS if args.extended else DEFAULT_SYMBOLS)
    tfs      = args.tf or ALL_TFS

    # validate TF input
    invalid = [t for t in tfs if t not in TF_CONFIGS]
    if invalid:
        print(f"[ERROR] TF ไม่รองรับ: {invalid}  รองรับ: {ALL_TFS}")
        sys.exit(1)

    print("=" * 60)
    print("  AI TRADE — Multi-TF Walk-Forward Backtest (3-Year)")
    print(f"  Signal Scanner + 3 Agents | MIN_SCORE={SCANNER.MIN_SCORE} | Claude filter: score≥{CLAUDE_MIN_SCORE}")
    print(f"  Sizing: 1% balance × {MAX_LEVERAGE}x leverage | Start balance: ${INITIAL_BALANCE:,.0f}")
    print(f"  TP1=RR{TP1_R} | TP2=RR{TP2_R}")
    print(f"  Period  : {args.years} years")
    print(f"  Symbols : {', '.join(s.replace('/USDT','') for s in symbols)}")
    print(f"  TFs     : {', '.join(tfs)}")
    print("=" * 60)

    # check cache
    cache_ok = os.path.isdir(CACHE_DIR) and any(
        f.endswith(".parquet") for f in os.listdir(CACHE_DIR)
    ) if os.path.isdir(CACHE_DIR) else False
    if not cache_ok:
        print(f"\n  [INFO] ไม่พบ {CACHE_DIR}/ — ดึงจาก OKX API (ช้ากว่า)")
        print(f"  [INFO] รัน  py download_history.py  เพื่อสร้าง cache 3Y ก่อน")

    all_trades  = []     # trades ทุก TF รวมกัน
    tf_summary  = []     # สรุป metrics ต่อ TF
    total_scans = 0
    t_start     = time_mod.time()

    # ── Loop ทุก TF ─────────────────────────────────────────────────────────
    for tf_key in tfs:
        pri_tf, htf_tf, warmup, window, timeout_c, step = TF_CONFIGS[tf_key]

        # override step ถ้าระบุ --step
        if args.step is not None:
            step = args.step

        htf_label = htf_tf if htf_tf != pri_tf else f"{htf_tf}(self)"
        candle_est = int(args.years * 365 * 24 / _tf_hours(pri_tf))

        print(f"\n{'━'*60}")
        print(f"  TF: {pri_tf}  (HTF: {htf_label})  "
              f"warmup={warmup}  window={window}  timeout={timeout_c}c  step={step}")
        print(f"  ≈{candle_est:,} candles/symbol")
        print(f"{'━'*60}")

        tf_trades   = []   # trades รอบนี้
        tf_scans    = 0

        for sym in symbols:
            src_label = "cache" if cache_ok else "API"
            print(f"\n  [{sym}]  loading {args.years}y ({src_label})...", end=" ", flush=True)
            t_fetch = time_mod.time()

            df_pri = fetch_paginated(sym, pri_tf, args.years)
            df_htf = fetch_paginated(sym, htf_tf, args.years)
            df_1d  = fetch_paginated(sym, "1d",   args.years)

            src_p = "[cache]" if _load_cache(sym, pri_tf, args.years) is not None else "[API]"
            src_h = "[cache]" if _load_cache(sym, htf_tf, args.years) is not None else "[API]"
            src_d = "[cache]" if _load_cache(sym, "1d",   args.years) is not None else "[API]"
            print(f"{pri_tf}:{len(df_pri)}{src_p}  {htf_tf}:{len(df_htf)}{src_h}  "
                  f"1d:{len(df_1d)}{src_d}  ({time_mod.time()-t_fetch:.1f}s)")

            if df_pri.empty or len(df_pri) < warmup + 10:
                print(f"  primary data insufficient — skip")
                continue
            if df_htf.empty:
                print(f"  HTF ({htf_tf}) no data — skip")
                continue
            # df_1d ว่าง → ใช้ None (scanner จะข้าม daily Stoch filter)
            df_1d_arg = df_1d if not df_1d.empty else None

            t0 = time_mod.time()
            trades_df, scanned, balance_final = backtest_symbol(
                sym, df_pri, df_htf,
                warmup, window, timeout_c, step,
                pri_tf,
                df_1d_arg,
                balance=INITIAL_BALANCE,
            )
            elapsed = time_mod.time() - t0
            tf_scans    += scanned
            total_scans += scanned

            n_t = len(trades_df)
            print(f"  scanned {scanned:,} windows → {n_t} trades  balance: ${INITIAL_BALANCE:,.0f} → ${balance_final:,.0f}  ({elapsed:.1f}s)")

            if not trades_df.empty:
                m = metrics(trades_df)
                report(m, f"{sym} [{pri_tf}]  [{n_t} trades | {args.years}y]")
                tf_trades.append(trades_df)
                all_trades.append(trades_df)

        # ── TF summary ───────────────────────────────────────────────────────
        if tf_trades:
            tf_combined = pd.concat(tf_trades, ignore_index=True)
            m = metrics(tf_combined)
            report(m, f"TF {pri_tf} — ALL symbols  [{len(tf_combined)} trades]")
            score_analysis(tf_combined)

            if m:
                tf_summary.append({
                    "tf":      pri_tf,
                    "htf":     htf_tf,
                    "n":       m["n"],
                    "wr":      m["wr"],
                    "tp1r":    m["tp1r"],
                    "avg":     m["avg"],
                    "total":   m["total"],
                    "dd":      m["dd"],
                    "sharpe":  m["sharpe"],
                    "verdict": verdict(m),
                })
        else:
            print(f"\n  TF {pri_tf}: ไม่มี trades")

    # ── Final summary ────────────────────────────────────────────────────────
    total_elapsed = time_mod.time() - t_start
    print(f"\n\n{'='*60}")
    print(f"  เสร็จใน {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
    print(f"  scan calls รวม : {total_scans:,}")
    print(f"{'='*60}")

    # TF Leaderboard
    print_tf_leaderboard(tf_summary)

    # Save combined CSV
    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        combined["entry_ts_dt"] = pd.to_datetime(combined["entry_ts"], utc=True, errors="coerce")
        combined = combined.sort_values("entry_ts_dt").drop(columns="entry_ts_dt")
        combined.reset_index(drop=True, inplace=True)
        combined.to_csv(OUTPUT_CSV, index=False)
        print(f"\n  บันทึก → {OUTPUT_CSV}  ({len(combined)} trades รวมทุก TF)")

        # Score analysis รวม
        score_analysis(combined)
    else:
        print("\n  ไม่มี trades — ไม่สร้าง CSV")


def _tf_hours(tf: str) -> float:
    """แปลง TF string → จำนวนชั่วโมง (สำหรับ estimate candle count)"""
    m = {"15m": 0.25, "30m": 0.5, "1h": 1, "2h": 2, "4h": 4, "1d": 24, "1w": 168}
    return m.get(tf, 1)


if __name__ == "__main__":
    main()
