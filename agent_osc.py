"""
📈 Oscillator Agent — RSI(14) Divergence · Stochastic(14,1,3) · MACD(12,26,9) · OBV
Pine Script source: WAKAME_RSI_MACD_STOCH.pine

ส่ง report ให้ Signal Scanner ทุก scan
MAX_SCORE = 11  (+2 จาก OBV volume confirmation)
"""
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend    import MACD
from ta.volume   import OnBalanceVolumeIndicator

NAME      = "Oscillator Agent"
EMOJI     = "📈"
MAX_SCORE = 11   # 9 (original) + 2 (OBV SMA20 + SMA50 confirmation)


def _add_indicators(df):
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]

    # ── RSI(14) + Pivot-based Divergence ──────────────────────────────────────
    df["rsi"]    = RSIIndicator(c, 14).rsi()
    df["rsi_os"] = df["rsi"] < 40
    df["rsi_ob"] = df["rsi"] > 60

    pivot_lo = l.rolling(11, center=True).min() == l
    pivot_hi = h.rolling(11, center=True).max() == h

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

    # ── Stochastic(14, 1, 3) ──────────────────────────────────────────────────
    st = StochasticOscillator(h, l, c, 14, 3)
    df["stk"]   = st.stoch()
    df["std"]   = st.stoch_signal()
    df["st_up"] = (df["stk"] > df["std"]) & (df["stk"].shift(1) <= df["std"].shift(1))
    df["st_dn"] = (df["stk"] < df["std"]) & (df["stk"].shift(1) >= df["std"].shift(1))

    # ── MACD(12, 26, 9) ───────────────────────────────────────────────────────
    m = MACD(c, 26, 12, 9)
    df["hist"]    = m.macd_diff()
    df["macd_up"] = (df["hist"] > 0) & (df["hist"].shift(1) <= 0)
    df["macd_dn"] = (df["hist"] < 0) & (df["hist"].shift(1) >= 0)

    # ── Kill Zone (London 07-10 / NY 13-16 UTC) ───────────────────────────────
    df["hour"] = df.index.hour
    df["kz"]   = ((df["hour"] >= 7) & (df["hour"] < 10)) | \
                 ((df["hour"] >= 13) & (df["hour"] < 16))

    # ── NEW: OBV (On-Balance Volume) — Volume Trend Confirmation ──────────────
    # ต้องการ volume column — fallback False ถ้าไม่มี
    if "volume" in df.columns and df["volume"].sum() > 0:
        obv             = OnBalanceVolumeIndicator(c, df["volume"]).on_balance_volume()
        df["obv"]       = obv
        df["obv_sma20"] = obv.rolling(20, min_periods=10).mean()
        df["obv_sma50"] = obv.rolling(50, min_periods=20).mean()
        # OBV อยู่เหนือ SMA20 = short-term volume trend bull
        df["obv_above_20"] = df["obv"] > df["obv_sma20"]
        # OBV อยู่เหนือ SMA50 = longer-term volume confirms trend
        df["obv_above_50"] = df["obv"] > df["obv_sma50"]
    else:
        df["obv_above_20"] = False
        df["obv_above_50"] = False

    return df.dropna()


def _score_long(r):
    s = 0
    if r["rsi_bull_div"]:  s += 3
    if r["rsi_os"]:        s += 2
    if r["st_up"]:         s += 2
    if r["macd_up"]:       s += 2
    if r["obv_above_20"]:  s += 1   # NEW: volume above short-term average
    if r["obv_above_50"]:  s += 1   # NEW: volume above long-term average
    return s   # max 11


def _score_short(r):
    s = 0
    if r["rsi_bear_div"]:  s += 3
    if r["rsi_ob"]:        s += 2
    if r["st_dn"]:         s += 2
    if r["macd_dn"]:       s += 2
    if not r["obv_above_20"]: s += 1   # NEW: OBV below SMA20 = volume confirms downside
    if not r["obv_above_50"]: s += 1   # NEW: OBV below SMA50
    return s   # max 11


def run(df_1h, df_4h=None):
    """
    📈 รัน Oscillator Agent — ส่ง report ให้ Signal Scanner
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
        "rsi":         round(float(r["rsi"]), 1),
        "stk":         round(float(r["stk"]), 1),
        "hist":        round(float(r["hist"]), 6),
        "kz":          bool(r["kz"]),
        "details": {
            "rsi_bull_div":  bool(r["rsi_bull_div"]),
            "rsi_bear_div":  bool(r["rsi_bear_div"]),
            "rsi_os":        bool(r["rsi_os"]),
            "rsi_ob":        bool(r["rsi_ob"]),
            "st_up":         bool(r["st_up"]),
            "st_dn":         bool(r["st_dn"]),
            "macd_up":       bool(r["macd_up"]),
            "macd_dn":       bool(r["macd_dn"]),
            "obv_above_20":  bool(r.get("obv_above_20", False)),   # NEW
            "obv_above_50":  bool(r.get("obv_above_50", False)),   # NEW
        },
    }
