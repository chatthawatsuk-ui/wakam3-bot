"""
🎯 Trend Agent — CDC ActionZone EMA7/30 · SMA99 · ATR Trailing Stop
Pine Script source: WaKam3.pine (CDC ActionZone V3 + ATR Trail + SMA99)

ส่ง report ให้ Signal Scanner ทุก scan
"""
import numpy as np
from ta.trend      import EMAIndicator, SMAIndicator
from ta.volatility import AverageTrueRange
from ta.momentum   import StochasticOscillator

NAME      = "Trend Agent"
EMOJI     = "🎯"
MAX_SCORE = 11

# ── Alert throttle — ส่ง HTF fallback alert ไม่เกิน 1 ครั้ง/ชั่วโมง ────────────
_htf_warn_ts = 0.0

EMA_FAST   = 7;   EMA_SLOW  = 30
SMA_99     = 99
ATR_FAST_P = 5;   ATR_FAST_M = 0.5
ATR_SLOW_P = 10;  ATR_SLOW_M = 2.0
RETRACE    = 0.003


def _atr_trail(close_vals, atr_vals):
    """Running ATR Trailing Stop — state-dependent, ต้อง loop"""
    trail = np.full(len(close_vals), np.nan)
    trail[0] = close_vals[0] - atr_vals[0]
    for i in range(1, len(close_vals)):
        if np.isnan(atr_vals[i]):
            trail[i] = trail[i - 1]
            continue
        prev, c, a = trail[i - 1], close_vals[i], atr_vals[i]
        trail[i] = max(prev, c - a) if c > prev else min(prev, c + a)
    return trail


def _add_indicators(df):
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]

    # CDC ActionZone
    df["ema7"]  = EMAIndicator(c, EMA_FAST).ema_indicator()
    df["ema30"] = EMAIndicator(c, EMA_SLOW).ema_indicator()
    df["sma99"] = SMAIndicator(c, SMA_99).sma_indicator()

    df["bull"]          = df["ema7"] > df["ema30"]
    df["cross_up"]      = (~df["bull"].shift(1).fillna(False)) & df["bull"]
    df["cross_dn"]      = df["bull"].shift(1).fillna(False) & (~df["bull"])
    df["touch_bull"]    = (c >= df["ema30"] * (1 - RETRACE)) & (c <= df["ema30"] * (1 + RETRACE * 2)) & df["bull"]
    df["touch_bear"]    = (c <= df["ema30"] * (1 + RETRACE)) & (c >= df["ema30"] * (1 - RETRACE * 2)) & (~df["bull"])
    df["above_sma99"]   = c > df["sma99"]
    df["ema7_gt_sma99"] = df["ema7"] > df["sma99"]

    # ATR Trailing Stop
    atr_f = AverageTrueRange(h, l, c, ATR_FAST_P).average_true_range() * ATR_FAST_M
    atr_s = AverageTrueRange(h, l, c, ATR_SLOW_P).average_true_range() * ATR_SLOW_M
    df["atr14"]          = AverageTrueRange(h, l, c, 14).average_true_range()
    df["atr_trail_fast"] = _atr_trail(c.values, atr_f.values)
    df["atr_trail_slow"] = _atr_trail(c.values, atr_s.values)
    df["trail_slow_bull"] = c > df["atr_trail_slow"]

    return df.dropna()


def _htf_bias(df_1h, df_4h):
    """4H EMA/SMA bias — Trend Agent เป็นคนตัดสิน HTF"""
    if df_4h.empty:
        global _htf_warn_ts
        import time
        now = time.time()
        if now - _htf_warn_ts > 3600:
            _htf_warn_ts = now
            try:
                import notify
                notify.send(
                    "⚠️ <b>[DEGRADED] HTF 4H Data Missing</b>\n"
                    "Trend Agent fallback: htf_bull=True, htf_sma=True\n"
                    "▸ LONG gate เปิดโดยอัตโนมัติ (ไม่ผ่าน HTF filter)\n"
                    "▸ SHORT signals ถูก block\n"
                    "▸ ตรวจสอบ: OKX 4H data fetch หรือ rate limit"
                )
            except Exception:
                pass
        df_1h = df_1h.copy()
        df_1h["htf_bull"] = True
        df_1h["htf_sma"]  = True
        return df_1h
    df_4h = df_4h.copy()
    df_4h["h7"]  = EMAIndicator(df_4h["close"], EMA_FAST).ema_indicator()
    df_4h["h30"] = EMAIndicator(df_4h["close"], EMA_SLOW).ema_indicator()
    df_4h["h99"] = SMAIndicator(df_4h["close"], SMA_99).sma_indicator()
    df_4h["htf_bull"] = df_4h["h7"] > df_4h["h30"]
    df_4h["htf_sma"]  = df_4h["close"] > df_4h["h99"]
    htf   = df_4h[["htf_bull", "htf_sma"]].resample("1h").ffill()
    df_1h = df_1h.join(htf, how="left")
    df_1h["htf_bull"] = df_1h["htf_bull"].ffill().fillna(True)
    df_1h["htf_sma"]  = df_1h["htf_sma"].ffill().fillna(True)
    return df_1h


def _score_long(r):
    s = 0
    if r["bull"]:             s += 2
    if r["cross_up"]:         s += 2
    if r["touch_bull"]:       s += 1
    if r["above_sma99"]:      s += 2
    if r["ema7_gt_sma99"]:    s += 2
    if r["trail_slow_bull"]:  s += 2
    return s   # max 11


def _score_short(r):
    s = 0
    if not r["bull"]:            s += 2
    if r["cross_dn"]:            s += 2
    if r["touch_bear"]:          s += 1
    if not r["above_sma99"]:     s += 2
    if not r["ema7_gt_sma99"]:   s += 2
    if not r["trail_slow_bull"]: s += 2
    return s   # max 11


def _daily_stoch_bias(df_1d):
    """
    Daily Stochastic(14,1,3) — กำหนด bias วันนี้
    คืน True  = Stoch bullish (K > D) → Buy-only day
    คืน False = Stoch bearish (K < D) → Sell-only day
    คืน None  = ไม่มีข้อมูลหรือคำนวณไม่ได้ → ไม่ filter
    """
    if df_1d is None or df_1d.empty or len(df_1d) < 20:
        return None
    try:
        st  = StochasticOscillator(df_1d["high"], df_1d["low"], df_1d["close"], 14, 3)
        stk = st.stoch()
        std = st.stoch_signal()
        last_k = stk.iloc[-1]
        last_d = std.iloc[-1]
        if np.isnan(float(last_k)) or np.isnan(float(last_d)):
            return None
        return bool(float(last_k) > float(last_d))
    except Exception:
        return None


def run(df_1h, df_4h, df_1d=None):
    """
    🎯 รัน Trend Agent — ส่ง report ให้ Signal Scanner
    Returns dict หรือ None ถ้าข้อมูลไม่พอ
    """
    df = _add_indicators(df_1h)
    df = _htf_bias(df, df_4h)
    if df.empty or len(df) < 2:
        return None

    r        = df.iloc[-2]
    htf_bull = bool(r.get("htf_bull", True))
    htf_sma  = bool(r.get("htf_sma",  True))

    # ── Daily Stochastic Filter (SRISIAM 7/30 style) ─────────────────────────
    # True = Buy-only day, False = Sell-only day, None = filter disabled
    stoch_d_bull = _daily_stoch_bias(df_1d)

    long_ok  = htf_bull and htf_sma
    short_ok = not (htf_bull or htf_sma)

    # เพิ่ม Daily Stoch filter ถ้ามีข้อมูล Daily
    if stoch_d_bull is not None:
        long_ok  = long_ok  and stoch_d_bull
        short_ok = short_ok and not stoch_d_bull

    sl = _score_long(r)  if long_ok  else 0
    ss = _score_short(r) if short_ok else 0

    return {
        "agent":         NAME,
        "emoji":         EMOJI,
        "score_long":    sl,
        "score_short":   ss,
        "max_score":     MAX_SCORE,
        "htf_bull":      htf_bull,
        "htf_sma":       htf_sma,
        "stoch_d_bull":  stoch_d_bull,   # None = filter ไม่ active
        "atr":           float(r.get("atr14", 0) or 0),
        "details": {
            "cdc_bull":        bool(r["bull"]),
            "cross_up":        bool(r["cross_up"]),
            "cross_dn":        bool(r["cross_dn"]),
            "touch_bull":      bool(r["touch_bull"]),
            "touch_bear":      bool(r["touch_bear"]),
            "above_sma99":     bool(r["above_sma99"]),
            "ema7_gt_sma99":   bool(r["ema7_gt_sma99"]),
            "trail_slow_bull": bool(r["trail_slow_bull"]),
            "stoch_d_bull":    stoch_d_bull,
            "ema7":            round(float(r["ema7"]),  6),
            "ema30":           round(float(r["ema30"]), 6),
            "sma99":           round(float(r["sma99"]), 6),
        },
    }
