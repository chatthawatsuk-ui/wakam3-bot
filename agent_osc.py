"""
📈 Oscillator Agent — RSI(14) Divergence · Stochastic(14,1,3) · MACD(12,26,9)
Pine Script source: WAKAME_RSI_MACD_STOCH.pine

ส่ง report ให้ Signal Scanner ทุก scan
"""
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend    import MACD

NAME      = "Oscillator Agent"
EMOJI     = "📈"
MAX_SCORE = 9


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

    return df.dropna()


def _score_long(r):
    s = 0
    if r["rsi_bull_div"]: s += 3
    if r["rsi_os"]:       s += 2
    if r["st_up"]:        s += 2
    if r["macd_up"]:      s += 2
    return s   # max 9


def _score_short(r):
    s = 0
    if r["rsi_bear_div"]: s += 3
    if r["rsi_ob"]:       s += 2
    if r["st_dn"]:        s += 2
    if r["macd_dn"]:      s += 2
    return s   # max 9


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
            "rsi_bull_div": bool(r["rsi_bull_div"]),
            "rsi_bear_div": bool(r["rsi_bear_div"]),
            "rsi_os":       bool(r["rsi_os"]),
            "rsi_ob":       bool(r["rsi_ob"]),
            "st_up":        bool(r["st_up"]),
            "st_dn":        bool(r["st_dn"]),
            "macd_up":      bool(r["macd_up"]),
            "macd_dn":      bool(r["macd_dn"]),
        },
    }
