"""
📈 Oscillator Agent — RSI(14) Divergence · Stochastic(14,1,3) · MACD(12,26,9) · OBV
Pine Script source: WAKAME_RSI_MACD_STOCH.pine

ส่ง report ให้ Signal Scanner ทุก scan
MAX_SCORE = 13  (Regular+3, Hidden+2, Exag+1, OS/OB+2, Stoch+2, MACD+2, OBV SMA+2, OBV div+2)
"""
import logging
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend    import MACD
from ta.volume   import OnBalanceVolumeIndicator
from config_loader import load_condition_points as _load_pts
_PT = _load_pts()

log = logging.getLogger(__name__)

NAME      = "Oscillator Agent"
EMOJI     = "📈"
MAX_SCORE = (
    _PT["rsi_bull_div"] + _PT["rsi_hidden_bull"] + _PT["rsi_exag_bull"] +
    _PT["rsi_os"] + _PT["st_up"] + _PT["macd_up"] +
    _PT["obv_above_20"] + _PT["obv_above_50"] + _PT["obv_bull_div"]
)


def _add_indicators(df):
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]

    # ── RSI(14) ───────────────────────────────────────────────────────────────
    df["rsi"]    = RSIIndicator(c, 14).rsi()
    df["rsi_os"] = df["rsi"] < 30    # true oversold
    df["rsi_ob"] = df["rsi"] > 70    # true overbought

    pivot_lo = l.rolling(11, center=True).min() == l
    pivot_hi = h.rolling(11, center=True).max() == h

    prev_lo_l   = l.where(pivot_lo).shift(1).ffill()
    prev_lo_rsi = df["rsi"].where(pivot_lo).shift(1).ffill()
    prev_hi_h   = h.where(pivot_hi).shift(1).ffill()
    prev_hi_rsi = df["rsi"].where(pivot_hi).shift(1).ffill()

    # Regular Bullish: price LL + RSI HL — กรอง midzone (RSI ต้อง < 40)
    df["rsi_bull_div"] = (
        pivot_lo &
        (l < prev_lo_l) &
        (df["rsi"] > prev_lo_rsi) &
        (df["rsi"] < 40)
    )
    # Regular Bearish: price HH + RSI LH — กรอง midzone (RSI ต้อง > 60)
    df["rsi_bear_div"] = (
        pivot_hi &
        (h > prev_hi_h) &
        (df["rsi"] < prev_hi_rsi) &
        (df["rsi"] > 60)
    )

    # Hidden Bullish: price HL + RSI LL — trend continuation, midzone ยอมรับได้
    df["rsi_hidden_bull"] = (
        pivot_lo &
        (l > prev_lo_l) &
        (df["rsi"] < prev_lo_rsi) &
        (df["rsi"] < 55)
    )
    # Hidden Bearish: price LH + RSI HH — trend continuation
    df["rsi_hidden_bear"] = (
        pivot_hi &
        (h < prev_hi_h) &
        (df["rsi"] > prev_hi_rsi) &
        (df["rsi"] > 45)
    )

    # Exaggerated Bullish: price EQL (≈ equal low) + RSI HL — Class B
    _eq_tol = 0.002
    df["rsi_exag_bull"] = (
        pivot_lo &
        ((l - prev_lo_l).abs() / (prev_lo_l.abs() + 1e-9) < _eq_tol) &
        (df["rsi"] > prev_lo_rsi) &
        (df["rsi"] < 40)
    )
    # Exaggerated Bearish: price EQH + RSI LH
    df["rsi_exag_bear"] = (
        pivot_hi &
        ((h - prev_hi_h).abs() / (prev_hi_h.abs() + 1e-9) < _eq_tol) &
        (df["rsi"] < prev_hi_rsi) &
        (df["rsi"] > 60)
    )

    # ── Stochastic(14, 1, 3) ──────────────────────────────────────────────────
    st = StochasticOscillator(h, l, c, 14, 3)
    df["stk"]   = st.stoch()
    df["std"]   = st.stoch_signal()
    df["st_up"] = (df["stk"] > df["std"]) & (df["stk"].shift(1) <= df["std"].shift(1))
    df["st_dn"] = (df["stk"] < df["std"]) & (df["stk"].shift(1) >= df["std"].shift(1))

    # ── MACD(12, 26, 9) — ใช้ Histogram เป็นหลัก ──────────────────────────────
    m = MACD(c, 26, 12, 9)
    df["hist"]    = m.macd_diff()
    df["macd_up"] = (df["hist"] > 0) & (df["hist"].shift(1) <= 0)
    df["macd_dn"] = (df["hist"] < 0) & (df["hist"].shift(1) >= 0)

    # ── Kill Zone (London 07-10 / NY 13-16 UTC) ───────────────────────────────
    df["hour"] = df.index.hour
    df["kz"]   = ((df["hour"] >= 7) & (df["hour"] < 10)) | \
                 ((df["hour"] >= 13) & (df["hour"] < 16))

    # ── OBV ───────────────────────────────────────────────────────────────────
    if "volume" in df.columns and df["volume"].sum() > 0:
        obv             = OnBalanceVolumeIndicator(c, df["volume"]).on_balance_volume()
        df["obv"]       = obv
        df["obv_sma20"] = obv.rolling(20, min_periods=10).mean()
        df["obv_sma50"] = obv.rolling(50, min_periods=20).mean()
        df["obv_above_20"] = df["obv"] > df["obv_sma20"]
        df["obv_above_50"] = df["obv"] > df["obv_sma50"]

        # OBV Divergence — ยืนยัน price divergence ด้วย volume
        prev_lo_obv = obv.where(pivot_lo).shift(1).ffill()
        prev_hi_obv = obv.where(pivot_hi).shift(1).ffill()
        df["obv_bull_div"] = pivot_lo & (l < prev_lo_l) & (obv > prev_lo_obv)
        df["obv_bear_div"] = pivot_hi & (h > prev_hi_h) & (obv < prev_hi_obv)

        df["obv_active"] = True
    else:
        log.warning("OBV disabled: no volume data")
        for col in ["obv_above_20", "obv_above_50", "obv_bull_div", "obv_bear_div"]:
            df[col] = False
        df["obv_active"] = False

    return df.dropna()


def _score_long(r):
    s = 0
    if r["rsi_bull_div"]:    s += _PT["rsi_bull_div"]
    if r["rsi_hidden_bull"]: s += _PT["rsi_hidden_bull"]
    if r["rsi_exag_bull"]:   s += _PT["rsi_exag_bull"]
    if r["rsi_os"]:          s += _PT["rsi_os"]
    if r["st_up"]:           s += _PT["st_up"]
    if r["macd_up"]:         s += _PT["macd_up"]
    if r["obv_above_20"]:    s += _PT["obv_above_20"]
    if r["obv_above_50"]:    s += _PT["obv_above_50"]
    if r["obv_bull_div"]:    s += _PT["obv_bull_div"]
    if r["kz"] and s > 0:
        s = min(round(s * 1.5), MAX_SCORE)
    return s


def _score_short(r):
    s = 0
    if r["rsi_bear_div"]:     s += _PT["rsi_bear_div"]
    if r["rsi_hidden_bear"]:  s += _PT["rsi_hidden_bear"]
    if r["rsi_exag_bear"]:    s += _PT["rsi_exag_bear"]
    if r["rsi_ob"]:           s += _PT["rsi_ob"]
    if r["st_dn"]:            s += _PT["st_dn"]
    if r["macd_dn"]:          s += _PT["macd_dn"]
    if not r["obv_above_20"]: s += _PT["obv_above_20_n"]
    if not r["obv_above_50"]: s += _PT["obv_above_50_n"]
    if r["obv_bear_div"]:     s += _PT["obv_bear_div"]
    if r["kz"] and s > 0:
        s = min(round(s * 1.5), MAX_SCORE)
    return s


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
        "obv_active":  bool(r.get("obv_active", False)),
        "details": {
            "rsi_bull_div":    bool(r["rsi_bull_div"]),
            "rsi_hidden_bull": bool(r["rsi_hidden_bull"]),
            "rsi_exag_bull":   bool(r["rsi_exag_bull"]),
            "rsi_bear_div":    bool(r["rsi_bear_div"]),
            "rsi_hidden_bear": bool(r["rsi_hidden_bear"]),
            "rsi_exag_bear":   bool(r["rsi_exag_bear"]),
            "rsi_os":          bool(r["rsi_os"]),
            "rsi_ob":          bool(r["rsi_ob"]),
            "st_up":           bool(r["st_up"]),
            "st_dn":           bool(r["st_dn"]),
            "macd_up":         bool(r["macd_up"]),
            "macd_dn":         bool(r["macd_dn"]),
            "obv_above_20":    bool(r.get("obv_above_20", False)),
            "obv_above_50":    bool(r.get("obv_above_50", False)),
            "obv_bull_div":    bool(r.get("obv_bull_div", False)),
            "obv_bear_div":    bool(r.get("obv_bear_div", False)),
        },
    }
