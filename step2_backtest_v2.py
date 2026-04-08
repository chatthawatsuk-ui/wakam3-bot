"""
AI Trade System — Step 2 v2: Full Indicator Backtest Engine
============================================================
Backtest ด้วย indicator stack ครบชุดสำหรับ Intraday 1h-4h

Indicators:
  Layer 1 — Structure : EMA50, EMA200, BOS/CHoCH, HH/HL/LH/LL
  Layer 2 — Entry     : RSI14, MACD, Bollinger Band, OTE Fib 0.618-0.786
  Layer 3 — Volume    : Volume expansion, OI proxy (volume delta)
  Layer 4 — Macro     : ADX14, Kill Zone (London/NY), HTF bias (4h)

Strategies tested:
  A) TREND_FOLLOW   — BOS + OTE pullback + EMA bias
  B) MEAN_REVERSION — BB squeeze + RSI oversold + ADX low
  C) COMBINED       — ทั้งคู่ + 12-point scoring

Risk: 1-2% per trade (SL-based sizing)
Timeframe: 1h primary, 4h HTF bias

Usage:
    pip install pandas numpy ta tqdm
    python step2_backtest_v2.py
"""

import sqlite3
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

try:
    from ta.volatility  import BollingerBands, AverageTrueRange
    from ta.momentum    import RSIIndicator, StochasticOscillator
    from ta.trend       import MACD, EMAIndicator, ADXIndicator
except ImportError:
    print("[ERROR] กรุณา: pip install ta")
    exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH        = "trade_data.db"
SYMBOLS        = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TF_ENTRY       = "1h"
TF_HTF         = "4h"
TRADE_SIZE_USD = 1000.0    # portfolio size สมมติ
RISK_PCT       = 0.015     # 1.5% risk per trade
ATR_SL_MULT    = 1.2       # SL = ATR × 1.5
TP_R_RATIO     = 2.5       # TP = SL distance × 2R
MIN_SCORE      = 7         # ต้องผ่าน ≥ 6 ใน 10 ข้อ
MIN_ADX_TREND  = 22        # ADX > นี้ = trending
MAX_ADX_RANGE  = 20        # ADX < นี้ = ranging
OTE_LOW        = 0.618
OTE_HIGH       = 0.786
SWING_LOOKBACK = 20        # bars สำหรับ swing high/low

# Kill Zones (UTC hour)
LONDON_START, LONDON_END = 7, 10
NY_START,     NY_END     = 13, 16

# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame:
    try:
        conn = sqlite3.connect(DB_PATH)
        df   = pd.read_sql(f"""
            SELECT ts, open, high, low, close, volume
            FROM ohlcv
            WHERE symbol='{symbol}' AND timeframe='{timeframe}'
            ORDER BY ts
        """, conn)
        conn.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    df = df[["open","high","low","close","volume"]].astype(float)
    return df

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATOR ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # ── Layer 1: Structure ────────────────────────────────────────────────────
    df["ema50"]   = EMAIndicator(c, window=50).ema_indicator()
    df["ema200"]  = EMAIndicator(c, window=200).ema_indicator()
    df["ema_bull"] = df["ema50"] > df["ema200"]   # golden cross bias

    # Swing High / Low (rolling)
    df["swing_high"] = h.rolling(SWING_LOOKBACK).max()
    df["swing_low"]  = l.rolling(SWING_LOOKBACK).min()

    # HH / HL / LH / LL (compare current swing vs previous)
    df["prev_sh"] = df["swing_high"].shift(SWING_LOOKBACK)
    df["prev_sl"] = df["swing_low"].shift(SWING_LOOKBACK)
    df["hh"] = df["swing_high"] > df["prev_sh"]   # Higher High
    df["hl"] = df["swing_low"]  > df["prev_sl"]   # Higher Low  → uptrend
    df["lh"] = df["swing_high"] < df["prev_sh"]   # Lower High
    df["ll"] = df["swing_low"]  < df["prev_sl"]   # Lower Low   → downtrend

    # BOS (Break of Structure) — ราคาทะลุ swing high/low ก่อนหน้า
    df["bos_bull"] = c > df["prev_sh"]  # bullish BOS
    df["bos_bear"] = c < df["prev_sl"]  # bearish BOS

    # ── Layer 2: Entry ────────────────────────────────────────────────────────
    # RSI
    df["rsi"] = RSIIndicator(c, window=14).rsi()
    df["rsi_os"]  = df["rsi"] < 35   # oversold
    df["rsi_ob"]  = df["rsi"] > 65   # overbought

    # RSI Divergence (price lower low but RSI higher low = bullish div)
    df["rsi_bull_div"] = (c  < c.shift(5))  & (df["rsi"] > df["rsi"].shift(5)) & (df["rsi"] < 50)
    df["rsi_bear_div"] = (c  > c.shift(5))  & (df["rsi"] < df["rsi"].shift(5)) & (df["rsi"] > 50)

    # MACD
    macd_obj      = MACD(c, window_slow=26, window_fast=12, window_sign=9)
    df["macd"]    = macd_obj.macd()
    df["macd_sig"]= macd_obj.macd_signal()
    df["macd_hist"]= macd_obj.macd_diff()
    df["macd_bull"]= (df["macd"] > df["macd_sig"]) & (df["macd_hist"] > 0)
    df["macd_bear"]= (df["macd"] < df["macd_sig"]) & (df["macd_hist"] < 0)

    # Bollinger Band
    bb = BollingerBands(c, window=20, window_dev=2)
    df["bb_upper"]= bb.bollinger_hband()
    df["bb_lower"]= bb.bollinger_lband()
    df["bb_mid"]  = bb.bollinger_mavg()
    df["bb_bw"]   = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    df["bb_squeeze"] = df["bb_bw"] < df["bb_bw"].rolling(50).mean() * 0.7
    df["bb_touch_low"]  = c <= df["bb_lower"] * 1.003
    df["bb_touch_high"] = c >= df["bb_upper"] * 0.997

    # OTE Zone (Optimal Trade Entry — Fib 0.618–0.786 of last swing)
    swing_range = df["swing_high"] - df["swing_low"]
    df["ote_low_bull"]  = df["swing_high"] - swing_range * OTE_HIGH  # pullback ลงมา
    df["ote_high_bull"] = df["swing_high"] - swing_range * OTE_LOW
    df["in_ote_bull"] = (c >= df["ote_low_bull"]) & (c <= df["ote_high_bull"])

    # ATR
    df["atr"] = AverageTrueRange(h, l, c, window=14).average_true_range()

    # ── Layer 3: Volume ───────────────────────────────────────────────────────
    df["vol_ma"]    = v.rolling(20).mean()
    df["vol_expand"]= v > df["vol_ma"] * 1.4   # volume 40% เหนือ avg

    # Volume Delta proxy (up vs down candles)
    df["vol_bull_bar"] = (c > df["close"].shift(1)) & df["vol_expand"]
    df["vol_bear_bar"] = (c < df["close"].shift(1)) & df["vol_expand"]

    # ── Layer 4: Macro ────────────────────────────────────────────────────────
    adx_obj       = ADXIndicator(h, l, c, window=14)
    df["adx"]     = adx_obj.adx()
    df["adx_trending"] = df["adx"] > MIN_ADX_TREND
    df["adx_ranging"]  = df["adx"] < MAX_ADX_RANGE

    # Kill Zone
    df["hour"] = df.index.hour
    df["in_killzone"] = (
        ((df["hour"] >= LONDON_START) & (df["hour"] < LONDON_END)) |
        ((df["hour"] >= NY_START)     & (df["hour"] < NY_END))
    )

    # Session tag
    df["session"] = "LATE"
    df.loc[(df["hour"] >= 1)  & (df["hour"] < 8),  "session"] = "ASIA"
    df.loc[(df["hour"] >= 8)  & (df["hour"] < 13), "session"] = "EUROPE"
    df.loc[(df["hour"] >= 13) & (df["hour"] < 21), "session"] = "US"

    return df.dropna()


def add_htf_bias(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> pd.DataFrame:
    """Merge 4h EMA bias ลงใน 1h dataframe"""
    if df_4h.empty:
        df_1h["htf_bull"] = True   # default ถ้าไม่มี 4h data
        return df_1h

    df_4h["htf_ema50"]  = EMAIndicator(df_4h["close"], window=50).ema_indicator()
    df_4h["htf_ema200"] = EMAIndicator(df_4h["close"], window=200).ema_indicator()
    df_4h["htf_bull"]   = df_4h["htf_ema50"] > df_4h["htf_ema200"]

    # Resample 4h bias ลง 1h (forward fill)
    htf_bias = df_4h["htf_bull"].resample("1h").ffill()
    df_1h    = df_1h.join(htf_bias, how="left")
    df_1h["htf_bull"] = df_1h["htf_bull"].ffill().fillna(True)
    return df_1h

# ══════════════════════════════════════════════════════════════════════════════
#  SCORING ENGINE (12-point)
# ══════════════════════════════════════════════════════════════════════════════

def score_long(row):
    """คืน (score 0-10, points 0-100)"""
    checks = [
        bool(row["ema_bull"]),           # 1. EMA50 > EMA200
        bool(row["htf_bull"]),           # 2. 4h HTF bias bull
        bool(row["bos_bull"] or row["hh"] and row["hl"]),  # 3. Structure bull
        bool(row["in_ote_bull"]),        # 4. ราคาอยู่ใน OTE zone
        bool(row["rsi_os"] or row["rsi_bull_div"]),        # 5. RSI oversold/div
        bool(row["macd_bull"]),          # 6. MACD bull
        bool(row["bb_touch_low"] or row["bb_squeeze"]),    # 7. BB signal
        bool(row["adx_trending"]),       # 8. ADX trending
        bool(row["vol_expand"]),         # 9. Volume expansion
        bool(row["in_killzone"]),        # 10. Kill Zone timing
    ]
    passed = sum(checks)

    # Weighted score
    weights = [12, 12, 10, 10, 10, 8, 8, 10, 10, 10]
    score   = sum(w for c, w in zip(checks, weights) if c)
    return passed, score

def score_short(row):
    checks = [
        bool(not row["ema_bull"]),
        bool(not row["htf_bull"]),
        bool(row["bos_bear"] or row["lh"] and row["ll"]),
        bool(row["rsi_ob"] or row["rsi_bear_div"]),
        bool(row["macd_bear"]),
        bool(row["bb_touch_high"] or row["bb_squeeze"]),
        bool(row["adx_trending"]),
        bool(row["vol_expand"]),
        bool(row["in_killzone"]),
        True,   # placeholder
    ]
    passed = sum(checks)
    weights = [12, 12, 10, 10, 8, 8, 10, 10, 10, 10]
    score   = sum(w for c, w in zip(checks, weights) if c)
    return passed, score

# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY SIGNAL GENERATORS
# ══════════════════════════════════════════════════════════════════════════════

def signal_trend_follow(row):
    """TREND_FOLLOW: BOS + OTE pullback + EMA + HTF bias"""
    if (row["ema_bull"] and row["htf_bull"] and
        row["bos_bull"] and row["in_ote_bull"] and
        (row["rsi"] < 55) and row["vol_expand"]):
        return "LONG"
    if (not row["ema_bull"] and not row["htf_bull"] and
        row["bos_bear"] and
        (row["rsi"] > 45) and row["vol_expand"]):
        return "SHORT"
    return None

def signal_mean_reversion(row):
    """MEAN_REVERSION: BB squeeze + RSI oversold/ob + ADX low"""
    if (row["adx_ranging"] and row["bb_touch_low"] and
        row["rsi_os"] and row["vol_expand"]):
        return "LONG"
    if (row["adx_ranging"] and row["bb_touch_high"] and
        row["rsi_ob"] and row["vol_expand"]):
        return "SHORT"
    return None

def signal_combined(row):
    """COMBINED: 12-point score ≥ MIN_SCORE"""
    long_passed,  long_score  = score_long(row)
    short_passed, short_score = score_short(row)

    if long_passed  >= MIN_SCORE and long_score  > short_score:
        return "LONG"
    if short_passed >= MIN_SCORE and short_score > long_score:
        return "SHORT"
    return None

STRATEGIES = {
    "TREND_FOLLOW":    signal_trend_follow,
    "MEAN_REVERSION":  signal_mean_reversion,
    "COMBINED":        signal_combined,
}

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(df: pd.DataFrame, strategy_name: str) -> pd.DataFrame:
    signal_fn = STRATEGIES[strategy_name]
    trades    = []
    in_trade  = False
    entry_px  = sl_px = tp_px = 0.0
    entry_side= ""
    entry_meta= {}

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]

        if in_trade:
            hit_tp = hit_sl = False
            if entry_side == "LONG":
                hit_tp = row["high"] >= tp_px
                hit_sl = row["low"]  <= sl_px
            else:  # SHORT
                hit_tp = row["low"]  <= tp_px
                hit_sl = row["high"] >= sl_px

            if hit_tp or hit_sl:
                exit_px  = tp_px if hit_tp else sl_px
                outcome  = "WIN" if hit_tp else "LOSS"
                pnl_mult = 1 if entry_side == "LONG" else -1
                pnl_pct  = (exit_px - entry_px) / entry_px * pnl_mult * 100
                pnl_usd  = TRADE_SIZE_USD * RISK_PCT * (TP_R_RATIO if hit_tp else -1)

                passed, score = score_long(prev) if entry_side == "LONG" else score_short(prev)
                trades.append({
                    **entry_meta,
                    "exit_time":  row.name,
                    "exit_px":    round(exit_px, 4),
                    "outcome":    outcome,
                    "pnl_pct":    round(pnl_pct, 3),
                    "pnl_usd":    round(pnl_usd, 2),
                    "score":      score,
                    "passed":     passed,
                    "session":    prev["session"],
                    "in_killzone":bool(prev["in_killzone"]),
                    "adx":        round(prev["adx"], 1),
                    "rsi":        round(prev["rsi"], 1),
                    "vol_expand": bool(prev["vol_expand"]),
                })
                in_trade = False

        if not in_trade:
            side = signal_fn(prev)
            if side:
                atr     = prev["atr"]
                ep      = row["open"]
                sl_dist = atr * ATR_SL_MULT
                sl      = ep - sl_dist if side == "LONG" else ep + sl_dist
                tp      = ep + sl_dist * TP_R_RATIO if side == "LONG" else ep - sl_dist * TP_R_RATIO

                in_trade   = True
                entry_px   = ep
                sl_px      = sl
                tp_px      = tp
                entry_side = side
                entry_meta = {
                    "strategy":   strategy_name,
                    "symbol":     df.attrs.get("symbol", ""),
                    "side":       side,
                    "entry_time": row.name,
                    "entry_px":   round(ep, 4),
                    "sl_px":      round(sl, 4),
                    "tp_px":      round(tp, 4),
                    "atr":        round(atr, 4),
                }

    return pd.DataFrame(trades)

# ══════════════════════════════════════════════════════════════════════════════
#  METRICS + REPORT
# ══════════════════════════════════════════════════════════════════════════════

def calc_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty or len(trades) < 5:
        return {}

    wins      = trades[trades["outcome"] == "WIN"]
    total     = len(trades)
    win_rate  = len(wins) / total * 100
    avg_pnl   = trades["pnl_pct"].mean()
    total_pnl = trades["pnl_usd"].sum()

    equity    = trades["pnl_usd"].cumsum()
    peak      = equity.cummax()
    max_dd    = ((equity - peak) / peak.abs().replace(0, 1) * 100).min()

    std       = trades["pnl_pct"].std()
    sharpe    = (avg_pnl / std * (252 ** 0.5)) if std > 0 else 0

    # Expectancy
    avg_win  = wins["pnl_usd"].mean() if not wins.empty else 0
    avg_loss = trades[trades["outcome"]=="LOSS"]["pnl_usd"].mean() if (trades["outcome"]=="LOSS").any() else 0
    expect   = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

    # By session
    sess = trades.groupby("session").agg(
        n=("outcome","count"),
        wins=("outcome", lambda x: (x=="WIN").sum()),
        avg_pnl=("pnl_pct","mean"),
    )
    sess["wr"] = (sess["wins"]/sess["n"]*100).round(1)

    # Kill Zone effect
    kz    = trades.groupby("in_killzone")["outcome"].apply(lambda x: (x=="WIN").mean()*100).round(1)

    return {
        "total":    total,
        "win_rate": round(win_rate, 1),
        "avg_pnl":  round(avg_pnl, 3),
        "total_pnl":round(total_pnl, 2),
        "max_dd":   round(max_dd, 2),
        "sharpe":   round(sharpe, 2),
        "expect":   round(expect, 2),
        "session":  sess,
        "killzone": kz,
    }

def verdict(m: dict) -> str:
    if not m:
        return "❌  INSUFFICIENT DATA"
    wr, sh, dd = m["win_rate"], m["sharpe"], abs(m["max_dd"])
    if wr >= 52 and sh >= 1.2 and dd <= 18:
        return "✅  STRONG PASS — ไป Optimize"
    if wr >= 48 and sh >= 0.8 and dd <= 25:
        return "✅  PASS — ไป Phase 2"
    if wr >= 44 and sh >= 0.5:
        return "⚠️   MARGINAL — ปรับ parameter"
    return "❌  FAIL — เปลี่ยน strategy"

def print_report(m: dict, label: str):
    sep = "─" * 58
    print(f"\n{sep}")
    print(f"  {label}")
    print(sep)
    if not m:
        print("  ไม่มีข้อมูลเพียงพอ (< 5 trades)")
        print(sep)
        return
    print(f"  Trades       {m['total']:>8,}")
    print(f"  Win Rate     {m['win_rate']:>7.1f}%")
    print(f"  Avg PnL      {m['avg_pnl']:>7.3f}%")
    print(f"  Total PnL    ${m['total_pnl']:>8,.2f}")
    print(f"  Max DD       {m['max_dd']:>7.2f}%")
    print(f"  Sharpe       {m['sharpe']:>7.2f}")
    print(f"  Expectancy   ${m['expect']:>7.2f} / trade")
    print(f"\n  SESSION BREAKDOWN:")
    print(m["session"].to_string())
    print(f"\n  KILL ZONE EFFECT:")
    print(m["killzone"].to_string())
    print(f"\n  {verdict(m)}")
    print(sep)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 58)
    print("  AI TRADE SYSTEM — BACKTEST v2")
    print("  Intraday 1h-4h | Risk 1.5% | TP 2R")
    print("=" * 58)

    all_trades = []

    for symbol in SYMBOLS:
        print(f"\n[LOAD] {symbol}...")
        df_1h = load_ohlcv(symbol, TF_ENTRY)
        df_4h = load_ohlcv(symbol, TF_HTF)

        if df_1h.empty:
            print(f"  [SKIP] ไม่มีข้อมูล {symbol} {TF_ENTRY} — รัน step1 ก่อน")
            continue

        print(f"  1h: {len(df_1h):,} candles  4h: {len(df_4h):,} candles")

        df_1h = add_indicators(df_1h)
        df_1h = add_htf_bias(df_1h, df_4h)
        df_1h.attrs["symbol"] = symbol

        for strat_name in STRATEGIES:
            trades = run_backtest(df_1h, strat_name)
            if not trades.empty:
                all_trades.append(trades)
                m = calc_metrics(trades)
                print_report(m, f"{symbol}  ·  {strat_name}  [{len(trades)} trades]")

    if not all_trades:
        print("\n[WARN] ไม่มี trade เกิดขึ้นเลย")
        print("  → รัน step1_data_collector.py ก่อนเพื่อดึง historical data")
        return

    # ── รวมทุก symbol + strategy ──────────────────────────────────────────────
    combined = pd.concat(all_trades, ignore_index=True)
    combined.to_csv("backtest_results_v2.csv", index=False)
    print(f"\n[SAVE] บันทึกผลรวม {len(combined):,} trades → backtest_results_v2.csv")

    # ── Summary by strategy ───────────────────────────────────────────────────
    print("\n" + "=" * 58)
    print("  STRATEGY COMPARISON (รวมทุก symbol)")
    print("=" * 58)
    summary = combined.groupby("strategy").apply(lambda g: pd.Series({
        "trades":   len(g),
        "win_rate": round((g["outcome"]=="WIN").mean()*100, 1),
        "avg_pnl":  round(g["pnl_pct"].mean(), 3),
        "total_pnl":round(g["pnl_usd"].sum(), 2),
        "sharpe":   round(g["pnl_pct"].mean() / g["pnl_pct"].std() * (252**0.5), 2) if g["pnl_pct"].std() > 0 else 0,
    })).reset_index()
    print(summary.to_string(index=False))

    # ── Best strategy ─────────────────────────────────────────────────────────
    if not summary.empty:
        best = summary.loc[summary["sharpe"].idxmax()]
        print(f"\n  🏆 BEST: {best['strategy']}  (Sharpe {best['sharpe']:.2f}, WR {best['win_rate']}%)")

    print("\n[DONE] ขั้นตอนถัดไป:")
    print("  → ถ้า PASS: รัน step3_optimize.py เพื่อหา parameter ที่ดีที่สุด")
    print("  → ถ้า FAIL: ปรับ MIN_SCORE, ATR_SL_MULT, TP_R_RATIO ใน config")

if __name__ == "__main__":
    main()
