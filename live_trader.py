import sys, os, sqlite3, warnings, time, json
from datetime import datetime, timezone
import pandas as pd
import numpy as np
warnings.filterwarnings("ignore")

from ta.momentum   import RSIIndicator, StochasticOscillator
from ta.trend      import EMAIndicator, SMAIndicator, MACD
from ta.volatility import AverageTrueRange

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
# OKX USDT Perpetual Futures — confirmed available (CMC Top 100)
FUTURES_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "LTC/USDT",
    "LINK/USDT", "AVAX/USDT", "SUI/USDT", "TON/USDT", "DOT/USDT",
    "SHIB/USDT", "HBAR/USDT", "XLM/USDT", "UNI/USDT", "NEAR/USDT",
    "TAO/USDT", "MNT/USDT", "PEPE/USDT", "AAVE/USDT", "ICP/USDT",
    "ETC/USDT", "RENDER/USDT", "ALGO/USDT", "POL/USDT", "ATOM/USDT",
    "WLD/USDT", "ENA/USDT", "FIL/USDT", "APT/USDT", "VET/USDT",
    "CRO/USDT", "TRUMP/USDT", "ONDO/USDT", "HYPE/USDT", "DEXE/USDT",
]

# CMC Top 100 แต่ไม่มี OKX perpetual futures — ใช้ Spot แทน
SPOT_SYMBOLS = [
    "MORPHO/USDT", "KAS/USDT", "QNT/USDT", "ZEC/USDT", "FLR/USDT",
]

SYMBOLS      = FUTURES_SYMBOLS + SPOT_SYMBOLS
FUTURES_SET  = set(FUTURES_SYMBOLS)

TF_1H      = "1h"
TF_4H      = "4h"
CANDLES    = 300

# ── CDC ActionZone (WaKam3.pine) ──────────────────────────────
EMA_FAST   = 12   # CDC Fast EMA  (was 7)
EMA_SLOW   = 26   # CDC Slow EMA  (was 30)
SMA_50     = 50   # short-term trend
SMA_100    = 100  # mid-term trend
SMA_200    = 200  # long-term trend  (was SMA_TREND=99)
# ── ATR Trailing Stop (WaKam3.pine) ──────────────────────────
ATR_FAST_P = 5    # Fast ATR period
ATR_FAST_M = 0.5  # Fast ATR multiplier
ATR_SLOW_P = 10   # Slow ATR period
ATR_SLOW_M = 2.0  # Slow ATR multiplier
SWING_LB   = 10
RETRACE    = 0.003
MIN_SCORE  = 8
TP1_R      = 1.2
TP2_R      = 2.0
RISK_PCT   = 0.01
SL_PCT     = 0.005
LEVERAGE   = 20         # x20 (ปลอดภัยกว่า x100)

DB_PATH    = "paper_trades.db"
LOG_PATH   = "signals.log"

# ── EXCHANGES ─────────────────────────────────────────────────────────────────
exchange_futures = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},   # perpetual futures OHLCV
})
exchange_spot = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})
exchange = exchange_futures  # default (ใช้ใน htf_bias ทั่วไป)

# ── LOGGER ────────────────────────────────────────────────────────────────────
def log(msg):
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ── FETCH LIVE DATA ───────────────────────────────────────────────────────────
def fetch(symbol, timeframe, limit=CANDLES, exch=None):
    if exch is None:
        exch = exchange_futures if symbol in FUTURES_SET else exchange_spot
    try:
        bars = exch.fetch_ohlcv(symbol, timeframe, limit=limit)
        df   = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("dt", inplace=True)
        return df[["open","high","low","close","volume"]].astype(float)
    except Exception as e:
        log(f"[WARN] fetch {symbol} {timeframe}: {e}")
        return pd.DataFrame()

# ── ATR TRAILING STOP (CDC / WaKam3) ─────────────────────────────────────────
def _atr_trail(close_vals, atr_vals):
    """Running ATR Trailing Stop — ต้อง loop เพราะ state ขึ้นกับแท่งก่อนหน้า"""
    trail = np.full(len(close_vals), np.nan)
    trail[0] = close_vals[0] - atr_vals[0]
    for i in range(1, len(close_vals)):
        if np.isnan(atr_vals[i]):
            trail[i] = trail[i-1]
            continue
        prev = trail[i-1]
        c    = close_vals[i]
        a    = atr_vals[i]
        trail[i] = max(prev, c - a) if c > prev else min(prev, c + a)
    return trail

# ── INDICATORS ────────────────────────────────────────────────────────────────
def indicators(df):
    c, h, l = df["close"], df["high"], df["low"]

    # ── 🎯 Trend Agent — CDC ActionZone + SMA + ATR Trailing Stop ──────────────
    df["ema12"]  = EMAIndicator(c, EMA_FAST).ema_indicator()   # CDC Fast
    df["ema26"]  = EMAIndicator(c, EMA_SLOW).ema_indicator()   # CDC Slow
    df["sma50"]  = SMAIndicator(c, SMA_50).sma_indicator()
    df["sma100"] = SMAIndicator(c, SMA_100).sma_indicator()
    df["sma200"] = SMAIndicator(c, SMA_200).sma_indicator()

    # CDC ActionZone signals
    df["bull"]      = df["ema12"] > df["ema26"]
    df["cdc_bull"]  = df["bull"]
    df["cross_up"]  = (~df["bull"].shift(1).fillna(False)) & df["bull"]
    df["cross_dn"]  = df["bull"].shift(1).fillna(False) & (~df["bull"])
    df["touch_bull"] = (c >= df["ema26"]*(1-RETRACE)) & (c <= df["ema26"]*(1+RETRACE*2)) & df["bull"]
    df["touch_bear"] = (c <= df["ema26"]*(1+RETRACE)) & (c >= df["ema26"]*(1-RETRACE*2)) & (~df["bull"])

    # SMA trend conditions
    df["above_sma50"]    = c > df["sma50"]
    df["above_sma200"]   = c > df["sma200"]
    df["sma50_gt_200"]   = df["sma50"] > df["sma200"]   # Golden Cross structure

    # ATR Trailing Stop Fast (5 × 0.5) and Slow (10 × 2.0)
    atr_f = AverageTrueRange(h, l, c, ATR_FAST_P).average_true_range() * ATR_FAST_M
    atr_s = AverageTrueRange(h, l, c, ATR_SLOW_P).average_true_range() * ATR_SLOW_M
    df["atr_trail_fast"] = _atr_trail(c.values, atr_f.values)
    df["atr_trail_slow"] = _atr_trail(c.values, atr_s.values)
    df["trail_fast_bull"] = c > df["atr_trail_fast"]
    df["trail_slow_bull"] = c > df["atr_trail_slow"]

    df["sh"]  = h.rolling(SWING_LB*2+1, center=True).max()
    df["sl_"] = l.rolling(SWING_LB*2+1, center=True).min()
    df["psh"] = df["sh"].shift(SWING_LB)
    df["psl"] = df["sl_"].shift(SWING_LB)

    df["bos_bull"]   = c > df["psh"]
    df["bos_bear"]   = c < df["psl"]
    df["choch_bull"] = (~df["bos_bull"].shift(3).fillna(False)) & df["bos_bull"] & (~df["bull"].shift(5).fillna(True))
    df["choch_bear"] = (~df["bos_bear"].shift(3).fillna(False)) & df["bos_bear"] & df["bull"].shift(5).fillna(False)

    df["hh"] = df["sh"] > df["sh"].shift(SWING_LB)
    df["hl"] = df["sl_"] > df["sl_"].shift(SWING_LB)
    df["lh"] = df["sh"] < df["sh"].shift(SWING_LB)
    df["qm_bull"] = df["lh"].shift(2) & df["hl"]
    df["qm_bear"] = df["hl"].shift(2) & df["lh"]

    # ── 🏦 SMC Agent — Premium / Discount Zones ────────────────────────────────
    swing_hi = h.rolling(SWING_LB*2+1, center=True).max()
    swing_lo = l.rolling(SWING_LB*2+1, center=True).min()
    eq_mid   = (swing_hi + swing_lo) / 2
    df["in_premium"]  = c > eq_mid   # price in premium zone → avoid long
    df["in_discount"] = c < eq_mid   # price in discount zone → good for long
    df["in_eq"]       = (c - eq_mid).abs() / (swing_hi - swing_lo + 1e-9) < 0.1

    # ── 📈 Oscillator Agent — RSI + Pivot Divergence ────────────────────────────
    df["rsi"] = RSIIndicator(c, 14).rsi()
    df["rsi_os"] = df["rsi"] < 40
    df["rsi_ob"] = df["rsi"] > 60

    # Pivot-based RSI Divergence (lookback 5 bars each side — เหมือน Pine Script)
    pivot_lo  = l.rolling(11, center=True).min() == l
    pivot_hi  = h.rolling(11, center=True).max() == h
    # Regular Bullish: price lower low, RSI higher low (at pivot lows)
    df["rsi_bull_div"] = (
        pivot_lo &
        (l < l.where(pivot_lo).shift(1).ffill()) &
        (df["rsi"] > df["rsi"].where(pivot_lo).shift(1).ffill()) &
        (df["rsi"] < 50)
    )
    # Regular Bearish: price higher high, RSI lower high (at pivot highs)
    df["rsi_bear_div"] = (
        pivot_hi &
        (h > h.where(pivot_hi).shift(1).ffill()) &
        (df["rsi"] < df["rsi"].where(pivot_hi).shift(1).ffill()) &
        (df["rsi"] > 50)
    )

    st = StochasticOscillator(h, l, c, 14, 3)
    df["stk"] = st.stoch()
    df["std"] = st.stoch_signal()
    df["st_up"] = (df["stk"] > df["std"]) & (df["stk"].shift(1) <= df["std"].shift(1))
    df["st_dn"] = (df["stk"] < df["std"]) & (df["stk"].shift(1) >= df["std"].shift(1))

    m = MACD(c, 26, 12, 9)
    df["hist"]    = m.macd_diff()
    df["macd_up"] = (df["hist"] > 0) & (df["hist"].shift(1) <= 0)
    df["macd_dn"] = (df["hist"] < 0) & (df["hist"].shift(1) >= 0)

    df["atr"]  = AverageTrueRange(h, l, c, 14).average_true_range()
    df["hour"] = df.index.hour
    df["kz"]   = ((df["hour"]>=7)&(df["hour"]<10)) | ((df["hour"]>=13)&(df["hour"]<16))

    return df.dropna()


def htf_bias(df1h, df4h):
    if df4h.empty:
        df1h["htf_bull"] = True
        df1h["htf_sma"]  = True
        return df1h
    df4h["h12"]  = EMAIndicator(df4h["close"], EMA_FAST).ema_indicator()
    df4h["h26"]  = EMAIndicator(df4h["close"], EMA_SLOW).ema_indicator()
    df4h["h200"] = SMAIndicator(df4h["close"], SMA_200).sma_indicator()
    df4h["htf_bull"] = df4h["h12"] > df4h["h26"]
    df4h["htf_sma"]  = df4h["close"] > df4h["h200"]
    htf = df4h[["htf_bull","htf_sma"]].resample("1h").ffill()
    df1h = df1h.join(htf, how="left")
    df1h["htf_bull"] = df1h["htf_bull"].ffill().fillna(True)
    df1h["htf_sma"]  = df1h["htf_sma"].ffill().fillna(True)
    return df1h

# ── SPECIALIST SCORERS ────────────────────────────────────────────────────────
def _trend_long(r):
    """🎯 Trend Agent — CDC EMA12/26 + SMA50/200 + ATR Trail (max 11)"""
    s = 0
    if r["cdc_bull"]:        s += 2   # EMA12 > EMA26
    if r["cross_up"]:        s += 2   # CDC buy cross
    if r["touch_bull"]:      s += 1   # pullback to EMA26
    if r["above_sma50"]:     s += 1   # above short-term trend
    if r["above_sma200"]:    s += 2   # above long-term trend
    if r["sma50_gt_200"]:    s += 1   # golden cross structure
    if r["trail_slow_bull"]: s += 2   # above ATR slow trail
    return s  # max 11

def _trend_short(r):
    """🎯 Trend Agent — SHORT (max 11)"""
    s = 0
    if not r["cdc_bull"]:        s += 2
    if r["cross_dn"]:            s += 2
    if r["touch_bear"]:          s += 1
    if not r["above_sma50"]:     s += 1
    if not r["above_sma200"]:    s += 2
    if not r["sma50_gt_200"]:    s += 1
    if not r["trail_slow_bull"]: s += 2
    return s  # max 11

def _smc_long(r):
    """🏦 SMC Agent — BOS/CHoCH/QM + Discount Zone (max 10)"""
    s = 0
    if r["bos_bull"]:    s += 2
    if r["choch_bull"]:  s += 3
    if r["qm_bull"]:     s += 2
    if r["in_discount"]: s += 2   # price in discount zone
    if r["in_eq"]:       s += 1   # price at equilibrium
    return s  # max 10

def _smc_short(r):
    """🏦 SMC Agent — SHORT (max 10)"""
    s = 0
    if r["bos_bear"]:    s += 2
    if r["choch_bear"]:  s += 3
    if r["qm_bear"]:     s += 2
    if r["in_premium"]:  s += 2   # price in premium zone
    if r["in_eq"]:       s += 1
    return s  # max 10

def _osc_long(r):
    """📈 Oscillator Agent — RSI Div + Stoch + MACD (max 9)"""
    s = 0
    if r["rsi_bull_div"]: s += 3
    if r["rsi_os"]:       s += 2
    if r["st_up"]:        s += 2
    if r["macd_up"]:      s += 2
    return s  # max 9

def _osc_short(r):
    """📈 Oscillator Agent — SHORT (max 9)"""
    s = 0
    if r["rsi_bear_div"]: s += 3
    if r["rsi_ob"]:       s += 2
    if r["st_dn"]:        s += 2
    if r["macd_dn"]:      s += 2
    return s  # max 9

# ── MAIN SIGNAL (รวม 3 specialist + Kill Zone bonus) ──────────────────────────
def score_long(r):
    """Total max = 11 + 10 + 9 + 1 = 31"""
    if not (r["htf_bull"] and r["htf_sma"]):
        return 0
    s = _trend_long(r) + _smc_long(r) + _osc_long(r)
    if r["kz"]: s += 1
    return s

def score_short(r):
    """Total max = 11 + 10 + 9 + 1 = 31"""
    if r["htf_bull"] or r["htf_sma"]:
        return 0
    s = _trend_short(r) + _smc_short(r) + _osc_short(r)
    if r["kz"]: s += 1
    return s

def specialist_breakdown(r, side):
    """ส่ง score แต่ละ specialist กลับมาเพื่อแสดงใน dashboard"""
    if side == "LONG":
        return {
            "trend": _trend_long(r),
            "smc":   _smc_long(r),
            "osc":   _osc_long(r),
        }
    return {
        "trend": _trend_short(r),
        "smc":   _smc_short(r),
        "osc":   _osc_short(r),
    }

# ── SCAN ──────────────────────────────────────────────────────────────────────
def scan():
    signals     = []
    scan_results = []   # ← เก็บ score ทุกเหรียญ (สำหรับ dashboard scanner)
    log("─" * 50)
    log(f"SCAN เริ่ม — {len(SYMBOLS)} symbols")

    for sym in SYMBOLS:
        mtype = "FUTURES" if sym in FUTURES_SET else "SPOT"
        try:
            d1h = fetch(sym, TF_1H)
            d4h = fetch(sym, TF_4H)
            if d1h.empty or len(d1h) < 150:
                log(f"  {sym}: ข้อมูลไม่พอ")
                scan_results.append({"symbol": sym, "status": "NO_DATA",
                                     "market_type": mtype,
                                     "score_long": 0, "score_short": 0,
                                     "best_score": 0, "price": 0, "rsi": 0,
                                     "htf_bull": False, "in_kz": False,
                                     "ts": datetime.now(timezone.utc).isoformat()})
                continue

            d1h = indicators(d1h)
            d1h = htf_bias(d1h, d4h)

            # ดูแท่งปิดล่าสุด (index -2 = แท่งที่ปิดแล้ว)
            r   = d1h.iloc[-2]
            px  = d1h.iloc[-1]["close"]   # ราคาปัจจุบัน

            sl  = score_long(r)
            ss  = score_short(r)

            # ── บันทึก scan result ทุกเหรียญ ──────────────────────────────
            best        = max(sl, ss)
            best_side   = "LONG" if sl >= ss else "SHORT"
            pct         = round(best / 31 * 100, 1)   # max score = 31
            if best >= MIN_SCORE:
                status  = "SIGNAL"
            elif best >= 5:
                status  = "WATCH"
            else:
                status  = "IDLE"

            bk = specialist_breakdown(r, best_side)

            scan_results.append({
                "symbol":      sym,
                "market_type": mtype,
                "status":      status,
                "side":        best_side,
                "score_long":  sl,
                "score_short": ss,
                "best_score":  best,
                "score_pct":   pct,
                "price":       round(float(px), 4),
                "rsi":         round(float(r["rsi"]), 1),
                "htf_bull":    bool(r["htf_bull"]),
                "in_kz":       bool(r["kz"]),
                # ── specialist breakdown ───────────────────────
                "score_trend": bk["trend"],   # 🎯 CDC Agent
                "score_smc":   bk["smc"],     # 🏦 SMC Agent
                "score_osc":   bk["osc"],     # 📈 Oscillator Agent
                "in_discount": bool(r["in_discount"]),
                "trail_bull":  bool(r["trail_slow_bull"]),
                "ts":          datetime.now(timezone.utc).isoformat(),
            })

            if sl >= MIN_SCORE and sl >= ss:
                side, score = "LONG", sl
            elif ss >= MIN_SCORE and ss > sl:
                side, score = "SHORT", ss
            else:
                log(f"  {sym}: watch (L={sl} S={ss}) px={px:.4f}")
                time.sleep(0.3)
                continue

            # คำนวณ levels
            atr  = r["atr"]
            ep   = px
            if side == "LONG":
                sl_p = min(float(r["sl_"]), ep - atr)
            else:
                sl_p = max(float(r["sh"]),  ep + atr)

            dist = abs(ep - sl_p)
            if dist < ep * 0.001:
                log(f"  {sym}: SL ใกล้เกินไป skip")
                continue

            tp1  = ep + dist*TP1_R if side=="LONG" else ep - dist*TP1_R
            tp2  = ep + dist*TP2_R if side=="LONG" else ep - dist*TP2_R

            sig = {
                "symbol":  sym,
                "side":    side,
                "score":   score,
                "price":   round(ep, 4),
                "sl":      round(sl_p, 4),
                "tp1":     round(tp1, 4),
                "tp2":     round(tp2, 4),
                "sl_pct":  round(dist/ep*100, 3),
                "rsi":     round(float(r["rsi"]), 1),
                "in_kz":   bool(r["kz"]),
                "ts":      datetime.now(timezone.utc).isoformat(),
            }
            signals.append(sig)
            log(f"  ✅ {sym} {side} score={score} px={ep:.4f} sl={sl_p:.4f} tp1={tp1:.4f} tp2={tp2:.4f}")

            time.sleep(0.3)

        except Exception as e:
            log(f"  [ERR] {sym}: {e}")

    log(f"SCAN เสร็จ — พบ {len(signals)} signals")
    return signals, scan_results


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 50)
    log("AI TRADE SYSTEM — LIVE SIGNAL ENGINE")
    log("🎯 Trend(CDC EMA12/26·SMA50/100/200·ATR Trail) | 🏦 SMC(BOS/CHoCH/QM·Zones) | 📈 Osc(RSI Div·Stoch·MACD)")
    log("=" * 50)

    signals      = []
    scan_results = []

    try:
        signals, scan_results = scan()
    except Exception as e:
        log(f"[FATAL] scan() failed: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        # บันทึก scan_results เสมอ — แม้ error จะต้องมีไฟล์ให้ dashboard
        try:
            with open("scan_results.json", "w") as f:
                json.dump(scan_results, f, indent=2, ensure_ascii=False)
            log(f"บันทึก → scan_results.json ({len(scan_results)} coins)")
        except Exception as e:
            log(f"[ERR] write scan_results.json: {e}")

    if signals:
        try:
            with open("latest_signals.json", "w") as f:
                json.dump(signals, f, indent=2, ensure_ascii=False)
            log(f"บันทึก → latest_signals.json ({len(signals)} signals)")
        except Exception as e:
            log(f"[ERR] write latest_signals.json: {e}")
    else:
        log("ไม่มี signal รอบนี้")
