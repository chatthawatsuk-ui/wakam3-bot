"""
🏦 SMC Agent — BOS/CHoCH/QM · Premium/Discount Zones · Displacement · Mitigation Block
Pine Script source: WaKam3.pine (Smart Money Concepts © LuxAlgo)

MAX_SCORE = 12  (CHoCH+4, BOS+2, QM+2, Zone+2, Mitigation+2 — Equilibrium ลบออก)
"""
NAME      = "SMC Agent"
EMOJI     = "🏦"
MAX_SCORE = 12
SWING_LB  = 10


def _detect_displacement(df, lookback=3, atr_mult=1.2):
    """3+ large-body candles ติดกันทิศเดียว = displacement confirmation."""
    body  = (df["close"] - df["open"]).abs()
    atr   = body.rolling(14, min_periods=5).mean()
    large = body > atr * atr_mult
    bull  = (df["close"] > df["open"]) & large
    bear  = (df["close"] < df["open"]) & large
    bull_disp = bull.rolling(lookback).min().fillna(False).astype(bool)
    bear_disp = bear.rolling(lookback).min().fillna(False).astype(bool)
    return bull_disp, bear_disp


def _add_indicators(df):
    df = df.copy()
    c, h, l, o = df["close"], df["high"], df["low"], df["open"]

    # ── Swing High / Low ──────────────────────────────────────────────────────
    df["sh"]  = h.rolling(SWING_LB * 2 + 1, center=True).max()
    df["sl_"] = l.rolling(SWING_LB * 2 + 1, center=True).min()
    df["psh"] = df["sh"].shift(SWING_LB)
    df["psl"] = df["sl_"].shift(SWING_LB)

    # ── BOS ───────────────────────────────────────────────────────────────────
    df["bos_bull"] = c > df["psh"]
    df["bos_bear"] = c < df["psl"]

    # ── CHoCH — ผูกกับ swing structure จริง (ไม่ใช้ magic shift) ────────────
    # CHoCH bull: BOS bull เกิด + มี BOS bear ใน lookback ก่อนหน้า + ไม่ consecutive
    lookback_choch = SWING_LB * 2
    recent_bos_bear = df["bos_bear"].rolling(lookback_choch).max().shift(1).fillna(0).astype(bool)
    recent_bos_bull = df["bos_bull"].rolling(lookback_choch).max().shift(1).fillna(0).astype(bool)
    df["choch_bull"] = df["bos_bull"] & recent_bos_bear & ~df["bos_bull"].shift(1).fillna(False)
    df["choch_bear"] = df["bos_bear"] & recent_bos_bull & ~df["bos_bear"].shift(1).fillna(False)

    # ── HH / HL / LH / LL ────────────────────────────────────────────────────
    df["hh"] = df["sh"]  > df["sh"].shift(SWING_LB)
    df["hl"] = df["sl_"] > df["sl_"].shift(SWING_LB)
    df["lh"] = df["sh"]  < df["sh"].shift(SWING_LB)
    df["ll"] = df["sl_"] < df["sl_"].shift(SWING_LB)

    # ── QM — เพิ่ม BOS validation คั่น left/right shoulder ──────────────────
    recent_bos_bull_qm = df["bos_bull"].rolling(SWING_LB).max().shift(1).fillna(0).astype(bool)
    recent_bos_bear_qm = df["bos_bear"].rolling(SWING_LB).max().shift(1).fillna(0).astype(bool)
    df["qm_bull"] = df["lh"].shift(2) & df["hl"] & recent_bos_bull_qm
    df["qm_bear"] = df["hl"].shift(2) & df["lh"] & recent_bos_bear_qm

    # ── Displacement ──────────────────────────────────────────────────────────
    bull_disp, bear_disp = _detect_displacement(df)
    df["bull_displacement"] = bull_disp
    df["bear_displacement"] = bear_disp

    # ── Mitigation Block ──────────────────────────────────────────────────────
    bear_candle = c < o
    bull_candle = c > o

    # Bearish mitigation block: last bull candle close ที่เกิด bos_bear → retest
    mit_bear_zone = c.where(bull_candle).where(df["bos_bear"]).ffill()
    ever_bos_bear = df["bos_bear"].cumsum() > 0
    df["mitigation_bear"] = (
        ever_bos_bear &
        ~df["bos_bear"] &
        ((c - mit_bear_zone).abs() / (mit_bear_zone.abs() + 1e-9) < 0.003)
    )

    # Bullish mitigation block: last bear candle close ที่เกิด bos_bull → retest
    mit_bull_zone = c.where(bear_candle).where(df["bos_bull"]).ffill()
    ever_bos_bull = df["bos_bull"].cumsum() > 0
    df["mitigation_bull"] = (
        ever_bos_bull &
        ~df["bos_bull"] &
        ((c - mit_bull_zone).abs() / (mit_bull_zone.abs() + 1e-9) < 0.003)
    )

    # ── Premium / Discount Zones ──────────────────────────────────────────────
    eq_mid = (df["sh"] + df["sl_"]) / 2
    df["in_discount"] = c < eq_mid
    df["in_premium"]  = c > eq_mid
    df["in_eq"]       = (c - eq_mid).abs() / (df["sh"] - df["sl_"] + 1e-9) < 0.1

    return df.dropna()


def _score_long(r):
    s = 0
    # BOS: +2 ถ้ามี displacement ยืนยัน / +1 ถ้าไม่มี
    if r["bos_bull"]:
        s += 2 if r["bull_displacement"] else 1
    if r["choch_bull"]:      s += 4   # เพิ่มจาก +3 — primary reversal signal
    if r["qm_bull"]:         s += 2
    if r["in_discount"]:     s += 2
    if r["mitigation_bull"]: s += 2   # ใหม่
    # Equilibrium ลบออก — zone หลีกเลี่ยง ไม่ควร score บวก
    return s   # max 12


def _score_short(r):
    s = 0
    if r["bos_bear"]:
        s += 2 if r["bear_displacement"] else 1
    if r["choch_bear"]:      s += 4
    if r["qm_bear"]:         s += 2
    if r["in_premium"]:      s += 2
    if r["mitigation_bear"]: s += 2
    return s   # max 12


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
            "bos_bull":          bool(r["bos_bull"]),
            "bos_bear":          bool(r["bos_bear"]),
            "choch_bull":        bool(r["choch_bull"]),
            "choch_bear":        bool(r["choch_bear"]),
            "qm_bull":           bool(r["qm_bull"]),
            "qm_bear":           bool(r["qm_bear"]),
            "in_discount":       bool(r["in_discount"]),
            "in_premium":        bool(r["in_premium"]),
            "in_eq":             bool(r["in_eq"]),
            "bull_displacement": bool(r["bull_displacement"]),
            "bear_displacement": bool(r["bear_displacement"]),
            "mitigation_bull":   bool(r["mitigation_bull"]),
            "mitigation_bear":   bool(r["mitigation_bear"]),
        },
    }
