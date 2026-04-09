"""
🎯 Trend Agent — CDC ActionZone EMA12/26 · SMA50/100/200 · ATR Trailing Stop
Pine Script source: WaKam3.pine (CDC ActionZone V3 + ATR Trail + SMA)

ส่ง report ให้ Signal Scanner ทุก scan
"""
import numpy as np
from ta.trend      import EMAIndicator, SMAIndicator
from ta.volatility import AverageTrueRange

NAME      = "Trend Agent"
EMOJI     = "🎯"
MAX_SCORE = 11

EMA_FAST   = 12;  EMA_SLOW   = 26
SMA_50     = 50;  SMA_100    = 100;  SMA_200 = 200
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
    df["ema12"]  = EMAIndicator(c, EMA_FAST).ema_indicator()
    df["ema26"]  = EMAIndicator(c, EMA_SLOW).ema_indicator()
    df["sma50"]  = SMAIndicator(c, SMA_50).sma_indicator()
    df["sma100"] = SMAIndicator(c, SMA_100).sma_indicator()
    df["sma200"] = SMAIndicator(c, SMA_200).sma_indicator()

    df["bull"]         = df["ema12"] > df["ema26"]
    df["cross_up"]     = (~df["bull"].shift(1).fillna(False)) & df["bull"]
    df["cross_dn"]     = df["bull"].shift(1).fillna(False) & (~df["bull"])
    df["touch_bull"]   = (c >= df["ema26"] * (1 - RETRACE)) & (c <= df["ema26"] * (1 + RETRACE * 2)) & df["bull"]
    df["touch_bear"]   = (c <= df["ema26"] * (1 + RETRACE)) & (c >= df["ema26"] * (1 - RETRACE * 2)) & (~df["bull"])
    df["above_sma50"]  = c > df["sma50"]
    df["above_sma200"] = c > df["sma200"]
    df["sma50_gt_200"] = df["sma50"] > df["sma200"]

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
        df_1h = df_1h.copy()
        df_1h["htf_bull"] = True
        df_1h["htf_sma"]  = True
        return df_1h
    df_4h = df_4h.copy()
    df_4h["h12"]  = EMAIndicator(df_4h["close"], EMA_FAST).ema_indicator()
    df_4h["h26"]  = EMAIndicator(df_4h["close"], EMA_SLOW).ema_indicator()
    df_4h["h200"] = SMAIndicator(df_4h["close"], SMA_200).sma_indicator()
    df_4h["htf_bull"] = df_4h["h12"] > df_4h["h26"]
    df_4h["htf_sma"]  = df_4h["close"] > df_4h["h200"]
    htf   = df_4h[["htf_bull", "htf_sma"]].resample("1h").ffill()
    df_1h = df_1h.join(htf, how="left")
    df_1h["htf_bull"] = df_1h["htf_bull"].ffill().fillna(True)
    df_1h["htf_sma"]  = df_1h["htf_sma"].ffill().fillna(True)
    return df_1h


def _score_long(r):
    s = 0
    if r["bull"]:            s += 2
    if r["cross_up"]:        s += 2
    if r["touch_bull"]:      s += 1
    if r["above_sma50"]:     s += 1
    if r["above_sma200"]:    s += 2
    if r["sma50_gt_200"]:    s += 1
    if r["trail_slow_bull"]: s += 2
    return s   # max 11


def _score_short(r):
    s = 0
    if not r["bull"]:           s += 2
    if r["cross_dn"]:           s += 2
    if r["touch_bear"]:         s += 1
    if not r["above_sma50"]:    s += 1
    if not r["above_sma200"]:   s += 2
    if not r["sma50_gt_200"]:   s += 1
    if not r["trail_slow_bull"]:s += 2
    return s   # max 11


def run(df_1h, df_4h):
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

    sl = _score_long(r)  if (htf_bull and htf_sma)   else 0
    ss = _score_short(r) if not (htf_bull or htf_sma) else 0

    return {
        "agent":       NAME,
        "emoji":       EMOJI,
        "score_long":  sl,
        "score_short": ss,
        "max_score":   MAX_SCORE,
        "htf_bull":    htf_bull,
        "htf_sma":     htf_sma,
        "atr":         float(r.get("atr14", 0) or 0),
        "details": {
            "cdc_bull":        bool(r["bull"]),
            "cross_up":        bool(r["cross_up"]),
            "cross_dn":        bool(r["cross_dn"]),
            "touch_bull":      bool(r["touch_bull"]),
            "touch_bear":      bool(r["touch_bear"]),
            "above_sma50":     bool(r["above_sma50"]),
            "above_sma200":    bool(r["above_sma200"]),
            "sma50_gt_200":    bool(r["sma50_gt_200"]),
            "trail_slow_bull": bool(r["trail_slow_bull"]),
            "ema12":           round(float(r["ema12"]),  6),
            "ema26":           round(float(r["ema26"]),  6),
            "sma200":          round(float(r["sma200"]), 6),
        },
    }
