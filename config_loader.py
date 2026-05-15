"""
Shared config loaders — condition points (L4) + regime weights (L5)
อ่านจาก condition_points.json และ regime_weights.json
"""
import json, os

CONDITION_POINTS_PATH = "condition_points.json"
REGIME_WEIGHTS_PATH   = "regime_weights.json"

DEFAULT_CONDITION_POINTS = {
    # Trend Agent — LONG
    "cdc_bull":         2,
    "cross_up":         2,
    "touch_bull":       1,
    "above_sma99":      2,
    "ema7_gt_sma99":    2,
    "trail_slow_bull":  2,
    "adx_strong":       1,
    "bb_squeeze":       1,
    # Trend Agent — SHORT
    "cdc_bear":         2,
    "cross_dn":         2,
    "touch_bear":       1,
    "above_sma99_n":    2,
    "ema7_gt_sma99_n":  2,
    "trail_slow_n":     2,
    # SMC Agent — LONG
    "bos_bull_base":    1,
    "bos_bull_disp":    1,
    "choch_bull":       4,
    "qm_bull":          2,
    "in_discount":      2,
    "mitigation_bull":  2,
    # SMC Agent — SHORT
    "bos_bear_base":    1,
    "bos_bear_disp":    1,
    "choch_bear":       4,
    "qm_bear":          2,
    "in_premium":       2,
    "mitigation_bear":  2,
    # Osc Agent — LONG
    "rsi_bull_div":     3,
    "rsi_hidden_bull":  2,
    "rsi_exag_bull":    1,
    "rsi_os":           2,
    "st_up":            2,
    "macd_up":          2,
    "obv_above_20":     1,
    "obv_above_50":     1,
    "obv_bull_div":     2,
    # Osc Agent — SHORT
    "rsi_bear_div":     3,
    "rsi_hidden_bear":  2,
    "rsi_exag_bear":    1,
    "rsi_ob":           2,
    "st_dn":            2,
    "macd_dn":          2,
    "obv_above_20_n":   1,
    "obv_above_50_n":   1,
    "obv_bear_div":     2,
}

DEFAULT_REGIME_WEIGHTS = {
    "TRENDING": {"trend": 0.400, "smc": 0.300, "osc": 0.300},
    "RANGING":  {"trend": 0.267, "smc": 0.300, "osc": 0.433},
    "VOLATILE": {"trend": 0.333, "smc": 0.400, "osc": 0.267},
    "UNKNOWN":  {"trend": 0.333, "smc": 0.333, "osc": 0.334},
}


def load_condition_points():
    """
    อ่าน condition_points.json — fallback DEFAULT_CONDITION_POINTS ถ้าไม่มี
    คืน dict {condition_key: int_points}
    """
    try:
        if os.path.exists(CONDITION_POINTS_PATH):
            with open(CONDITION_POINTS_PATH) as f:
                saved = json.load(f)
            merged = dict(DEFAULT_CONDITION_POINTS)
            merged.update({k: max(0, int(v)) for k, v in saved.items()
                           if k in DEFAULT_CONDITION_POINTS})
            return merged
    except Exception:
        pass
    return dict(DEFAULT_CONDITION_POINTS)


def load_regime_weights():
    """
    อ่าน regime_weights.json — fallback DEFAULT_REGIME_WEIGHTS ถ้าไม่มี
    คืน dict {regime: {trend, smc, osc}}
    """
    try:
        if os.path.exists(REGIME_WEIGHTS_PATH):
            with open(REGIME_WEIGHTS_PATH) as f:
                saved = json.load(f)
            merged = {}
            for regime, defaults in DEFAULT_REGIME_WEIGHTS.items():
                if regime in saved:
                    w = saved[regime]
                    t = float(w.get("trend", defaults["trend"]))
                    s = float(w.get("smc",   defaults["smc"]))
                    o = float(w.get("osc",   defaults["osc"]))
                    total = t + s + o
                    if abs(total - 1.0) > 0.01:
                        t, s, o = t / total, s / total, o / total
                    merged[regime] = {
                        "trend": round(t, 3),
                        "smc":   round(s, 3),
                        "osc":   round(o, 3),
                    }
                else:
                    merged[regime] = defaults
            return merged
    except Exception:
        pass
    return {k: dict(v) for k, v in DEFAULT_REGIME_WEIGHTS.items()}
