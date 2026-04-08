sed -i '' '1s/^/import sys\nsys.path.insert(0, "\/Users\/-wakame-\/Library\/Python\/3.9\/lib\/python\/site-packages")\n/' step2_backtest_v3.py
python3 step2_backtest_v3.py
"""
AI Trade System — Step 2 v3: SMC Backtest Engine
=================================================
อ้างอิงจาก:
  - EMA 7 / EMA 30 / SMA 99  (WaKam3 + EMA 7/30 MTF)
  - BOS / CHoCH               (Break of Structure / Change of Character)
  - QM / QML Pattern          (Quasimodo reversal)
  - RSI Divergence            (WAKAME_RSI_MACD_STOCH)
  - Stochastic + MACD         (momentum confirm)
  - 2-Order System            (Order1 RR 1:1.2 + Order2 RR 1:3.1)

Logic:
  Major TF (4h): EMA7 vs EMA30 → Bullish/Bearish bias
  Minor TF (1h): EMA7 cross EMA30 → entry signal
  Filter:        SMA99, RSI div, Stoch, MACD histogram
  Entry:         Price retrace แตะ EMA30 หลัง cross
  SL:            ใต้/เหนือ swing low/high ล่าสุด
  TP1:           SL distance × 1.2R (ปิด 50%)
  TP2:           SL distance × 3.1R (ปิดที่เหลือ 50%)

Usage:
    python3 step2_backtest_v3.py
"""

import sqlite3
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

import sys, os

# เพิ่ม user site-packages สำหรับ macOS Xcode Python
_user_site = os.path.expanduser("~/Library/Python/3.9/lib/python/site-packages")
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)

try:
    from ta.momentum import RSIIndicator, StochasticOscillator, MACDIndicator
    from ta.trend    import EMAIndicator, SMAIndicator, MACD
    from ta.volatility import AverageTrueRange
except ImportError:
    print("[ERROR] ไม่พบ ta library")
    print("  รัน: /Library/Developer/CommandLineTools/usr/bin/python3 -m pip install ta")
    exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH        = "trade_data.db"
SYMBOLS        = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TF_MINOR       = "1h"     # entry timeframe
TF_MAJOR       = "4h"     # trend filter timeframe

# EMA / SMA
EMA_FAST       = 7
EMA_SLOW       = 30
SMA_TREND      = 99

# Entry
RETRACE_BUFFER = 0.003    # ราคา retrace ภายใน 0.3% ของ EMA30 = "แตะ"
SWING_LOOKBACK = 10       # bars สำหรับหา swing high/low

# 2-Order System (จาก Trade Journal)
TP1_R          = 1.2      # Order 1: ปิด 50%
TP2_R          = 3.1      # Order 2: ปิด 50%
ORDER_SPLIT    = 0.5      # 50% / 50%

# Risk
PORTFOLIO_USD  = 1000.0
RISK_PCT       = 0.01     # 1% per trade

# RSI / Stoch / MACD
RSI_OS         = 40
RSI_OB         = 60
STOCH_OS       = 25
STOCH_OB       = 75
SWING_DIV_BARS = 5        # lookback สำหรับ RSI divergence

# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_ohlcv(symbol, timeframe):
    try:
        conn = sqlite3.connect(DB_PATH)
        df   = pd.read_sql(f"""
            SELECT ts, open, high, low, close, volume
            FROM ohlcv WHERE symbol='{symbol}' AND timeframe='{timeframe}'
            ORDER BY ts
        """, conn)
        conn.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    return df[["open","high","low","close","volume"]].astype(float)

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def add_indicators(df):
    c, h, l = df["close"], df["high"], df["low"]

    # ── EMA / SMA ─────────────────────────────────────────────────────────────
    df["ema7"]  = EMAIndicator(c, window=EMA_FAST).ema_indicator()
    df["ema30"] = EMAIndicator(c, window=EMA_SLOW).ema_indicator()
    df["sma99"] = SMAIndicator(c, window=SMA_TREND).sma_indicator()

    # EMA cross
    df["ema_bull"]     = df["ema7"] > df["ema30"]
    df["cross_up"]     = (~df["ema_bull"].shift(1).fillna(False)) & df["ema_bull"]
    df["cross_down"]   = df["ema_bull"].shift(1).fillna(False) & (~df["ema_bull"])

    # Price retrace to EMA30
    df["touch_ema30_bull"] = (
        (c >= df["ema30"] * (1 - RETRACE_BUFFER)) &
        (c <= df["ema30"] * (1 + RETRACE_BUFFER * 2)) &
        df["ema_bull"]
    )
    df["touch_ema30_bear"] = (
        (c <= df["ema30"] * (1 + RETRACE_BUFFER)) &
        (c >= df["ema30"] * (1 - RETRACE_BUFFER * 2)) &
        (~df["ema_bull"])
    )

    # SMA99 filter
    df["above_sma99"] = c > df["sma99"]
    df["below_sma99"] = c < df["sma99"]

    # ── Swing High / Low (สำหรับ BOS/CHoCH/SL) ────────────────────────────────
    df["swing_high"] = h.rolling(SWING_LOOKBACK * 2 + 1, center=True).max()
    df["swing_low"]  = l.rolling(SWING_LOOKBACK * 2 + 1, center=True).min()
    df["prev_sh"]    = df["swing_high"].shift(SWING_LOOKBACK)
    df["prev_sl"]    = df["swing_low"].shift(SWING_LOOKBACK)

    # ── BOS — Break of Structure ──────────────────────────────────────────────
    # Bullish BOS: ราคา close เหนือ previous swing high → uptrend confirmed
    # Bearish BOS: ราคา close ใต้ previous swing low  → downtrend confirmed
    df["bos_bull"] = c > df["prev_sh"]
    df["bos_bear"] = c < df["prev_sl"]

    # ── CHoCH — Change of Character ───────────────────────────────────────────
    # Bullish CHoCH: เกิดใน downtrend แล้วทำ HH ครั้งแรก = trend พลิก
    # Bearish CHoCH: เกิดใน uptrend แล้วทำ LL ครั้งแรก = trend พลิก
    df["choch_bull"] = (~df["bos_bull"].shift(3).fillna(False)) & df["bos_bull"] & (~df["ema_bull"].shift(5).fillna(True))
    df["choch_bear"] = (~df["bos_bear"].shift(3).fillna(False)) & df["bos_bear"] & df["ema_bull"].shift(5).fillna(False)

    # ── QM / QML Pattern ──────────────────────────────────────────────────────
    # Quasimodo LONG: ราคาทำ Lower High แล้ว Higher Low → เตรียม breakout up
    # QM  = HH → LH → HL (failed bearish) → bullish reversal
    # QML = LL → HL → LH (failed bullish) → bearish reversal
    df["hh"] = df["swing_high"] > df["swing_high"].shift(SWING_LOOKBACK)
    df["hl"] = df["swing_low"]  > df["swing_low"].shift(SWING_LOOKBACK)
    df["lh"] = df["swing_high"] < df["swing_high"].shift(SWING_LOOKBACK)
    df["ll"] = df["swing_low"]  < df["swing_low"].shift(SWING_LOOKBACK)

    # QM Bull: LH + HL = failed bearish = พร้อม long
    df["qm_bull"] = df["lh"].shift(2) & df["hl"]
    # QML Bear: HL + LH = failed bullish = พร้อม short
    df["qml_bear"] = df["hl"].shift(2) & df["lh"]

    # ── RSI + Divergence ──────────────────────────────────────────────────────
    df["rsi"] = RSIIndicator(c, window=14).rsi()
    df["rsi_os"] = df["rsi"] < RSI_OS
    df["rsi_ob"] = df["rsi"] > RSI_OB

    # RSI Bullish Div: price LL แต่ RSI HL
    df["rsi_bull_div"] = (
        (c < c.shift(SWING_DIV_BARS)) &
        (df["rsi"] > df["rsi"].shift(SWING_DIV_BARS)) &
        (df["rsi"] < 50)
    )
    # RSI Bearish Div: price HH แต่ RSI LH
    df["rsi_bear_div"] = (
        (c > c.shift(SWING_DIV_BARS)) &
        (df["rsi"] < df["rsi"].shift(SWING_DIV_BARS)) &
        (df["rsi"] > 50)
    )

    # ── Stochastic ────────────────────────────────────────────────────────────
    stoch     = StochasticOscillator(h, l, c, window=14, smooth_window=3)
    df["stk"] = stoch.stoch()
    df["std"] = stoch.stoch_signal()
    df["stoch_cross_up"]   = (df["stk"] > df["std"]) & (df["stk"].shift(1) <= df["std"].shift(1)) & (df["stk"] < STOCH_OB)
    df["stoch_cross_down"] = (df["stk"] < df["std"]) & (df["stk"].shift(1) >= df["std"].shift(1)) & (df["stk"] > STOCH_OS)

    # ── MACD Histogram ────────────────────────────────────────────────────────
    macd_obj       = MACD(c, window_slow=26, window_fast=12, window_sign=9)
    df["macd_hist"]= macd_obj.macd_diff()
    df["macd_flip_up"]   = (df["macd_hist"] > 0) & (df["macd_hist"].shift(1) <= 0)
    df["macd_flip_down"] = (df["macd_hist"] < 0) & (df["macd_hist"].shift(1) >= 0)

    # ── ATR (สำหรับ SL ถ้า swing ไม่มี) ──────────────────────────────────────
    df["atr"] = AverageTrueRange(h, l, c, window=14).average_true_range()

    # ── Session ───────────────────────────────────────────────────────────────
    df["hour"]    = df.index.hour
    df["session"] = "LATE"
    df.loc[(df["hour"] >= 1)  & (df["hour"] < 8),  "session"] = "ASIA"
    df.loc[(df["hour"] >= 8)  & (df["hour"] < 13), "session"] = "EUROPE"
    df.loc[(df["hour"] >= 13) & (df["hour"] < 21), "session"] = "US"
    df["in_kz"] = (
        ((df["hour"] >= 7) & (df["hour"] < 10)) |
        ((df["hour"] >= 13) & (df["hour"] < 16))
    )

    return df.dropna()


def add_htf_bias(df_1h, df_4h):
    """Merge 4h EMA7/30 bias + SMA99 ลงใน 1h"""
    if df_4h.empty:
        df_1h["htf_bull"]    = True
        df_1h["htf_sma99"]   = True
        return df_1h

    df_4h["htf_ema7"]  = EMAIndicator(df_4h["close"], window=EMA_FAST).ema_indicator()
    df_4h["htf_ema30"] = EMAIndicator(df_4h["close"], window=EMA_SLOW).ema_indicator()
    df_4h["htf_sma99"] = SMAIndicator(df_4h["close"], window=SMA_TREND).sma_indicator()
    df_4h["htf_bull"]  = df_4h["htf_ema7"] > df_4h["htf_ema30"]
    df_4h["htf_above_sma99"] = df_4h["close"] > df_4h["htf_sma99"]

    htf = df_4h[["htf_bull","htf_above_sma99"]].resample("1h").ffill()
    df_1h = df_1h.join(htf, how="left")
    df_1h["htf_bull"]       = df_1h["htf_bull"].ffill().fillna(True)
    df_1h["htf_above_sma99"]= df_1h["htf_above_sma99"].ffill().fillna(True)
    return df_1h

# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def check_long(row):
    """
    LONG conditions:
    1. 4h EMA7 > EMA30 (major bull)
    2. 4h Price > SMA99
    3. 1h EMA7 cross EMA30 ขึ้น (minor confirm) หรือ retrace แตะ EMA30
    4. BOS bull หรือ CHoCH bull หรือ QM bull
    5. RSI divergence bull หรือ RSI oversold
    6. Stoch cross up หรือ MACD flip up
    """
    score = 0
    # Gate conditions — ถ้าไม่ผ่านข้อ 1-2 ไม่เข้าเลย
    if not (row["htf_bull"] and row["htf_above_sma99"]):
        return False, 0

    # 1h structure
    if row["ema_bull"]:          score += 2
    if row["cross_up"]:          score += 2   # bonus: cross เพิ่งเกิด
    if row["touch_ema30_bull"]:  score += 2   # optimal entry
    if row["above_sma99"]:       score += 1

    # SMC structure
    if row["bos_bull"]:          score += 2
    if row["choch_bull"]:        score += 3   # CHoCH = stronger signal
    if row["qm_bull"]:           score += 2

    # Momentum
    if row["rsi_bull_div"]:      score += 3
    if row["rsi_os"]:            score += 2
    if row["stoch_cross_up"]:    score += 2
    if row["macd_flip_up"]:      score += 2

    # Kill Zone bonus
    if row["in_kz"]:             score += 1

    return score >= 8, score


def check_short(row):
    """
    SHORT conditions (mirror of LONG):
    1. 4h EMA7 < EMA30 (major bear)
    2. 4h Price < SMA99
    3. 1h EMA7 cross EMA30 ลง
    4. BOS bear หรือ CHoCH bear หรือ QML bear
    5. RSI divergence bear หรือ RSI overbought
    6. Stoch cross down หรือ MACD flip down
    """
    score = 0
    if row["htf_bull"] or row["htf_above_sma99"]:
        return False, 0

    if not row["ema_bull"]:       score += 2
    if row["cross_down"]:         score += 2
    if row["touch_ema30_bear"]:   score += 2
    if row["below_sma99"]:        score += 1

    if row["bos_bear"]:           score += 2
    if row["choch_bear"]:         score += 3
    if row["qml_bear"]:           score += 2

    if row["rsi_bear_div"]:       score += 3
    if row["rsi_ob"]:             score += 2
    if row["stoch_cross_down"]:   score += 2
    if row["macd_flip_down"]:     score += 2

    if row["in_kz"]:              score += 1

    return score >= 8, score

# ══════════════════════════════════════════════════════════════════════════════
#  2-ORDER BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(df, symbol):
    trades   = []
    in_trade = False
    ep = sl = tp1 = tp2 = 0.0
    side = ""
    tp1_hit = False
    entry_meta = {}

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]

        # ── ถ้าอยู่ใน trade ──────────────────────────────────────────────────
        if in_trade:
            h_i = row["high"]
            l_i = row["low"]

            # SL hit → ทั้ง 2 orders แพ้
            if (side == "LONG"  and l_i <= sl) or \
               (side == "SHORT" and h_i >= sl):
                pnl_o1 = -PORTFOLIO_USD * RISK_PCT
                pnl_o2 = -PORTFOLIO_USD * RISK_PCT if not tp1_hit else 0
                _record(trades, entry_meta, row, "LOSS", sl,
                        pnl_o1, pnl_o2, tp1_hit, prev["score"] if "score" in prev else 0)
                in_trade = False
                tp1_hit  = False
                continue

            # TP1 hit → ปิด Order 1
            if not tp1_hit:
                if (side == "LONG"  and h_i >= tp1) or \
                   (side == "SHORT" and l_i <= tp1):
                    tp1_hit = True

            # TP2 hit → ปิด Order 2 → จบ trade
            if tp1_hit:
                if (side == "LONG"  and h_i >= tp2) or \
                   (side == "SHORT" and l_i <= tp2):
                    sl_dist = abs(ep - sl)
                    pnl_o1  = PORTFOLIO_USD * RISK_PCT * ORDER_SPLIT * TP1_R
                    pnl_o2  = PORTFOLIO_USD * RISK_PCT * ORDER_SPLIT * TP2_R
                    _record(trades, entry_meta, row, "WIN", tp2,
                            pnl_o1, pnl_o2, True, prev["score"] if "score" in prev else 0)
                    in_trade = False
                    tp1_hit  = False
                    continue

        # ── หา signal ────────────────────────────────────────────────────────
        else:
            long_ok,  long_score  = check_long(prev)
            short_ok, short_score = check_short(prev)

            if not (long_ok or short_ok):
                continue

            # เลือก side ที่ score สูงกว่า
            if long_ok and (not short_ok or long_score >= short_score):
                chosen_side  = "LONG"
                chosen_score = long_score
            else:
                chosen_side  = "SHORT"
                chosen_score = short_score

            ep_price = row["open"]
            atr      = prev["atr"]

            if chosen_side == "LONG":
                sl_price = min(prev["swing_low"], ep_price - atr * 1.0)
            else:
                sl_price = max(prev["swing_high"], ep_price + atr * 1.0)

            sl_dist = abs(ep_price - sl_price)
            if sl_dist < ep_price * 0.001:   # SL น้อยเกินไป skip
                continue

            tp1_price = ep_price + sl_dist * TP1_R if chosen_side == "LONG" else ep_price - sl_dist * TP1_R
            tp2_price = ep_price + sl_dist * TP2_R if chosen_side == "LONG" else ep_price - sl_dist * TP2_R

            in_trade   = True
            tp1_hit    = False
            ep         = ep_price
            sl         = sl_price
            tp1        = tp1_price
            tp2        = tp2_price
            side       = chosen_side
            entry_meta = {
                "symbol":     symbol,
                "side":       chosen_side,
                "entry_time": row.name,
                "entry_px":   round(ep_price, 4),
                "sl_px":      round(sl_price, 4),
                "tp1_px":     round(tp1_price, 4),
                "tp2_px":     round(tp2_price, 4),
                "sl_dist":    round(sl_dist, 4),
                "score":      chosen_score,
                "session":    prev["session"],
                "in_kz":      bool(prev["in_kz"]),
                "rsi":        round(prev["rsi"], 1),
                "atr":        round(atr, 4),
            }

    return pd.DataFrame(trades)


def _record(trades, meta, row, outcome, exit_px, pnl_o1, pnl_o2, tp1_hit, score):
    total_pnl = pnl_o1 + pnl_o2
    trades.append({
        **meta,
        "exit_time": row.name,
        "exit_px":   round(exit_px, 4),
        "outcome":   outcome,
        "tp1_hit":   tp1_hit,
        "pnl_o1":    round(pnl_o1, 2),
        "pnl_o2":    round(pnl_o2, 2),
        "pnl_total": round(total_pnl, 2),
    })

# ══════════════════════════════════════════════════════════════════════════════
#  METRICS + REPORT
# ══════════════════════════════════════════════════════════════════════════════

def calc_metrics(trades):
    if trades.empty or len(trades) < 5:
        return {}

    wins      = trades[trades["outcome"] == "WIN"]
    total     = len(trades)
    win_rate  = len(wins) / total * 100
    total_pnl = trades["pnl_total"].sum()
    avg_pnl   = trades["pnl_total"].mean()

    # TP1 hit rate (แม้ lose TP2 แต่ TP1 ได้)
    tp1_rate  = trades["tp1_hit"].mean() * 100

    equity    = trades["pnl_total"].cumsum()
    peak      = equity.cummax()
    max_dd    = ((equity - peak) / (peak.abs().replace(0, 1)) * 100).min()

    std       = trades["pnl_total"].std()
    sharpe    = avg_pnl / std * (252 ** 0.5) if std > 0 else 0

    # Expectancy
    avg_win   = wins["pnl_total"].mean() if not wins.empty else 0
    avg_loss  = trades[trades["outcome"] == "LOSS"]["pnl_total"].mean() if (trades["outcome"] == "LOSS").any() else 0
    expect    = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    # By session
    sess = trades.groupby("session").agg(
        n      = ("outcome", "count"),
        wins   = ("outcome", lambda x: (x == "WIN").sum()),
        avg_pnl= ("pnl_total", "mean"),
    )
    sess["wr"] = (sess["wins"] / sess["n"] * 100).round(1)

    # Kill Zone
    kz = trades.groupby("in_kz")["outcome"].apply(
        lambda x: (x == "WIN").mean() * 100).round(1)

    return {
        "total":    total,
        "win_rate": round(win_rate, 1),
        "tp1_rate": round(tp1_rate, 1),
        "avg_pnl":  round(avg_pnl, 2),
        "total_pnl":round(total_pnl, 2),
        "max_dd":   round(max_dd, 2),
        "sharpe":   round(sharpe, 2),
        "expect":   round(expect, 2),
        "session":  sess,
        "killzone": kz,
    }


def verdict(m):
    if not m:
        return "❌  INSUFFICIENT DATA (< 5 trades)"
    wr, sh, dd = m["win_rate"], m["sharpe"], abs(m["max_dd"])
    if wr >= 55 and sh >= 1.2 and dd <= 20:
        return "✅  STRONG PASS"
    if wr >= 48 and sh >= 0.8 and dd <= 30:
        return "✅  PASS — ไป Phase 2"
    if wr >= 42 and sh >= 0.4:
        return "⚠️   MARGINAL — ปรับ score threshold"
    return "❌  FAIL"


def print_report(m, label):
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  {label}")
    print(f"  System: EMA7/30 + SMA99 + BOS/CHoCH/QM + RSI/Stoch/MACD")
    print(f"  Orders: RR1:{TP1_R} (50%) + RR1:{TP2_R} (50%)")
    print(sep)
    if not m:
        print("  ไม่มีข้อมูลเพียงพอ")
        print(sep)
        return
    print(f"  Trades       {m['total']:>8,}")
    print(f"  Win Rate     {m['win_rate']:>7.1f}%")
    print(f"  TP1 Hit Rate {m['tp1_rate']:>7.1f}%  (Order 1 ปิดได้)")
    print(f"  Avg PnL      ${m['avg_pnl']:>8.2f}")
    print(f"  Total PnL    ${m['total_pnl']:>8.2f}")
    print(f"  Max DD       {m['max_dd']:>7.2f}%")
    print(f"  Sharpe       {m['sharpe']:>7.2f}")
    print(f"  Expectancy   ${m['expect']:>7.2f} / trade")
    print(f"\n  SESSION:")
    print(m["session"].to_string())
    print(f"\n  KILL ZONE:")
    print(m["killzone"].to_string())
    print(f"\n  {verdict(m)}")
    print(sep)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  AI TRADE SYSTEM — BACKTEST v3")
    print("  EMA 7/30 · SMA 99 · BOS/CHoCH/QM")
    print("  2-Order: RR1:1.2 + RR1:3.1")
    print("=" * 60)

    all_trades = []

    for symbol in SYMBOLS:
        print(f"\n[LOAD] {symbol}...")
        df_1h = load_ohlcv(symbol, TF_MINOR)
        df_4h = load_ohlcv(symbol, TF_MAJOR)

        if df_1h.empty:
            print(f"  [SKIP] ไม่มีข้อมูล — รัน step1 ก่อน")
            continue

        print(f"  1h: {len(df_1h):,} candles  |  4h: {len(df_4h):,} candles")

        df_1h = add_indicators(df_1h)
        df_1h = add_htf_bias(df_1h, df_4h)
        df_1h.attrs["symbol"] = symbol

        trades = run_backtest(df_1h, symbol)

        if trades.empty:
            print(f"  [WARN] ไม่มี signal เกิดขึ้น")
            continue

        all_trades.append(trades)
        m = calc_metrics(trades)
        print_report(m, f"{symbol}  [{len(trades)} trades]")

    if not all_trades:
        print("\n[WARN] ไม่มี trade เลย")
        return

    combined = pd.concat(all_trades, ignore_index=True)
    combined.to_csv("backtest_results_v3.csv", index=False)
    print(f"\n[SAVE] {len(combined):,} trades → backtest_results_v3.csv")

    # ── Summary รวม ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  SUMMARY — รวมทุก Symbol")
    print("=" * 60)
    m_all = calc_metrics(combined)
    if m_all:
        print(f"  Total Trades : {m_all['total']:,}")
        print(f"  Win Rate     : {m_all['win_rate']}%")
        print(f"  TP1 Hit Rate : {m_all['tp1_rate']}%")
        print(f"  Total PnL    : ${m_all['total_pnl']:,.2f}")
        print(f"  Sharpe       : {m_all['sharpe']}")
        print(f"  {verdict(m_all)}")

    print("\n[DONE]")
    print("  → ถ้า PASS: สร้าง live system ต่อ (Phase 2)")
    print("  → ถ้า MARGINAL: ปรับ score threshold ใน check_long/check_short")
    print("  → ถ้า FAIL: ปรับ RETRACE_BUFFER หรือ SWING_LOOKBACK")

if __name__ == "__main__":
    main()
