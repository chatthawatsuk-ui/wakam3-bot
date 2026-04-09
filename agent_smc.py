"""
🏦 SMC Agent — BOS/CHoCH/QM · Premium/Discount Zones
Pine Script source: WaKam3.pine (Smart Money Concepts © LuxAlgo)

ส่ง report ให้ Signal Scanner ทุก scan
"""
NAME      = "SMC Agent"
EMOJI     = "🏦"
MAX_SCORE = 10
SWING_LB  = 10


def _add_indicators(df):
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]

    # Swing High / Low
    df["sh"]  = h.rolling(SWING_LB * 2 + 1, center=True).max()
    df["sl_"] = l.rolling(SWING_LB * 2 + 1, center=True).min()
    df["psh"] = df["sh"].shift(SWING_LB)
    df["psl"] = df["sl_"].shift(SWING_LB)

    # BOS
    df["bos_bull"] = c > df["psh"]
    df["bos_bear"] = c < df["psl"]

    # CHoCH (Change of Character)
    bull_proxy = c > df["sh"].shift(1)
    df["choch_bull"] = (~df["bos_bull"].shift(3).fillna(False)) & df["bos_bull"] & (~bull_proxy.shift(5).fillna(True))
    df["choch_bear"] = (~df["bos_bear"].shift(3).fillna(False)) & df["bos_bear"] &   bull_proxy.shift(5).fillna(False)

    # QM (Quasimodo)
    df["hh"] = df["sh"]  > df["sh"].shift(SWING_LB)
    df["hl"] = df["sl_"] > df["sl_"].shift(SWING_LB)
    df["lh"] = df["sh"]  < df["sh"].shift(SWING_LB)
    df["qm_bull"] = df["lh"].shift(2) & df["hl"]
    df["qm_bear"] = df["hl"].shift(2) & df["lh"]

    # Premium / Discount Zones
    eq_mid = (df["sh"] + df["sl_"]) / 2
    df["in_discount"] = c < eq_mid
    df["in_premium"]  = c > eq_mid
    df["in_eq"]       = (c - eq_mid).abs() / (df["sh"] - df["sl_"] + 1e-9) < 0.1

    return df.dropna()


def _score_long(r):
    s = 0
    if r["bos_bull"]:    s += 2
    if r["choch_bull"]:  s += 3
    if r["qm_bull"]:     s += 2
    if r["in_discount"]: s += 2
    if r["in_eq"]:       s += 1
    return s   # max 10


def _score_short(r):
    s = 0
    if r["bos_bear"]:   s += 2
    if r["choch_bear"]: s += 3
    if r["qm_bear"]:    s += 2
    if r["in_premium"]: s += 2
    if r["in_eq"]:      s += 1
    return s   # max 10


def run(df_1h, df_4h=None):
    """
    🏦 รัน SMC Agent — ส่ง report ให้ Signal Scanner
    Returns dict หรือ None ถ้าข้อมูลไม่พอ
    """
    df = _add_indicators(df_1h)
    if df.empty or len(df) < 2:
        return None

    r = df.iloc[-2]

    return {
        "agent":       NAME,
        "emoji":       EMOJI,
        "score_long":  _score_long(r),
        "score_short": _score_short(r),
        "max_score":   MAX_SCORE,
        "swing_high":  float(r["sh"]  or 0),
        "swing_low":   float(r["sl_"] or 0),
        "details": {
            "bos_bull":    bool(r["bos_bull"]),
            "bos_bear":    bool(r["bos_bear"]),
            "choch_bull":  bool(r["choch_bull"]),
            "choch_bear":  bool(r["choch_bear"]),
            "qm_bull":     bool(r["qm_bull"]),
            "qm_bear":     bool(r["qm_bear"]),
            "in_discount": bool(r["in_discount"]),
            "in_premium":  bool(r["in_premium"]),
            "in_eq":       bool(r["in_eq"]),
        },
    }
