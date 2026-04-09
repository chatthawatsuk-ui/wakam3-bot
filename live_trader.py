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
# CMC Top 100 (ex-stablecoins/wrapped) × OKX USDT Perpetual Futures
SYMBOLS = [
    # ── Mega Cap ──────────────────────────────────────────────────────────────
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    # ── Large Cap ─────────────────────────────────────────────────────────────
    "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "LTC/USDT",
    "LINK/USDT", "AVAX/USDT", "SUI/USDT", "TON/USDT", "DOT/USDT",
    # ── Mid-Large Cap ─────────────────────────────────────────────────────────
    "SHIB/USDT", "HBAR/USDT", "XLM/USDT", "UNI/USDT", "NEAR/USDT",
    "TAO/USDT", "MNT/USDT", "PEPE/USDT", "AAVE/USDT", "ICP/USDT",
    # ── Mid Cap ───────────────────────────────────────────────────────────────
    "ETC/USDT", "ONDO/USDT", "RENDER/USDT", "ALGO/USDT", "POL/USDT",
    "ATOM/USDT", "WLD/USDT", "ENA/USDT", "FIL/USDT", "APT/USDT",
    # ── Active Futures (CMC top 100) ──────────────────────────────────────────
    "VET/USDT", "CRO/USDT", "TRUMP/USDT", "DEXE/USDT", "MORPHO/USDT",
    "KAS/USDT", "QNT/USDT", "HYPE/USDT", "ZEC/USDT", "FLR/USDT",
]

TF_1H      = "1h"
TF_4H      = "4h"
CANDLES    = 300        # จำนวน candle ที่ดึงมาคำนวณ

EMA_FAST   = 7
EMA_SLOW   = 30
SMA_TREND  = 99
SWING_LB   = 10
RETRACE    = 0.003
MIN_SCORE  = 8
TP1_R      = 1.2
TP2_R      = 2.0
RISK_PCT   = 0.01       # 1% per trade
SL_PCT     = 0.005      # 0.5% SL ของราคา
LEVERAGE   = 20         # x20 futures (ปลอดภัยกว่า x100)

DB_PATH    = "paper_trades.db"
LOG_PATH   = "signals.log"

# ── EXCHANGE (OKX Perpetual Futures / USDT Swap) ──────────────────────────────
exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},   # ใช้ perpetual futures OHLCV
})

# ── LOGGER ────────────────────────────────────────────────────────────────────
def log(msg):
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ── FETCH LIVE DATA ───────────────────────────────────────────────────────────
def fetch(symbol, timeframe, limit=CANDLES):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df   = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("dt", inplace=True)
        return df[["open","high","low","close","volume"]].astype(float)
    except Exception as e:
        log(f"[WARN] fetch {symbol} {timeframe}: {e}")
        return pd.DataFrame()

# ── INDICATORS ────────────────────────────────────────────────────────────────
def indicators(df):
    c, h, l = df["close"], df["high"], df["low"]

    df["ema7"]  = EMAIndicator(c, EMA_FAST).ema_indicator()
    df["ema30"] = EMAIndicator(c, EMA_SLOW).ema_indicator()
    df["sma99"] = SMAIndicator(c, SMA_TREND).sma_indicator()
    df["bull"]  = df["ema7"] > df["ema30"]

    df["cross_up"] = (~df["bull"].shift(1).fillna(False)) & df["bull"]
    df["cross_dn"] = df["bull"].shift(1).fillna(False) & (~df["bull"])
    df["touch_bull"] = (c >= df["ema30"]*(1-RETRACE)) & (c <= df["ema30"]*(1+RETRACE*2)) & df["bull"]
    df["touch_bear"] = (c <= df["ema30"]*(1+RETRACE)) & (c >= df["ema30"]*(1-RETRACE*2)) & (~df["bull"])

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

    df["rsi"] = RSIIndicator(c, 14).rsi()
    df["rsi_os"] = df["rsi"] < 40
    df["rsi_ob"] = df["rsi"] > 60
    df["rsi_bull_div"] = (c < c.shift(5)) & (df["rsi"] > df["rsi"].shift(5)) & (df["rsi"] < 50)
    df["rsi_bear_div"] = (c > c.shift(5)) & (df["rsi"] < df["rsi"].shift(5)) & (df["rsi"] > 50)

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
    df4h["h7"]  = EMAIndicator(df4h["close"], EMA_FAST).ema_indicator()
    df4h["h30"] = EMAIndicator(df4h["close"], EMA_SLOW).ema_indicator()
    df4h["h99"] = SMAIndicator(df4h["close"], SMA_TREND).sma_indicator()
    df4h["htf_bull"] = df4h["h7"] > df4h["h30"]
    df4h["htf_sma"]  = df4h["close"] > df4h["h99"]
    htf = df4h[["htf_bull","htf_sma"]].resample("1h").ffill()
    df1h = df1h.join(htf, how="left")
    df1h["htf_bull"] = df1h["htf_bull"].ffill().fillna(True)
    df1h["htf_sma"]  = df1h["htf_sma"].ffill().fillna(True)
    return df1h

# ── SIGNAL ────────────────────────────────────────────────────────────────────
def score_long(r):
    if not (r["htf_bull"] and r["htf_sma"]):
        return 0
    s = 0
    if r["bull"]:          s += 2
    if r["cross_up"]:      s += 2
    if r["touch_bull"]:    s += 2
    if r["bos_bull"]:      s += 2
    if r["choch_bull"]:    s += 3
    if r["qm_bull"]:       s += 2
    if r["rsi_bull_div"]:  s += 3
    if r["rsi_os"]:        s += 2
    if r["st_up"]:         s += 2
    if r["macd_up"]:       s += 2
    if r["kz"]:            s += 1
    return s

def score_short(r):
    if r["htf_bull"] or r["htf_sma"]:
        return 0
    s = 0
    if not r["bull"]:      s += 2
    if r["cross_dn"]:      s += 2
    if r["touch_bear"]:    s += 2
    if r["bos_bear"]:      s += 2
    if r["choch_bear"]:    s += 3
    if r["qm_bear"]:       s += 2
    if r["rsi_bear_div"]:  s += 3
    if r["rsi_ob"]:        s += 2
    if r["st_dn"]:         s += 2
    if r["macd_dn"]:       s += 2
    if r["kz"]:            s += 1
    return s

# ── SCAN ──────────────────────────────────────────────────────────────────────
def scan():
    signals     = []
    scan_results = []   # ← เก็บ score ทุกเหรียญ (สำหรับ dashboard scanner)
    log("─" * 50)
    log(f"SCAN เริ่ม — {len(SYMBOLS)} symbols")

    for sym in SYMBOLS:
        try:
            d1h = fetch(sym, TF_1H)
            d4h = fetch(sym, TF_4H)
            if d1h.empty or len(d1h) < 150:
                log(f"  {sym}: ข้อมูลไม่พอ")
                scan_results.append({"symbol": sym, "status": "NO_DATA",
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
            pct         = round(best / 26 * 100, 1)
            if best >= MIN_SCORE:
                status  = "SIGNAL"
            elif best >= 5:
                status  = "WATCH"      # ใกล้ signal (5-7 คะแนน)
            else:
                status  = "IDLE"

            scan_results.append({
                "symbol":      sym,
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
    log("EMA7/30 · SMA99 · BOS/CHoCH/QM")
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
