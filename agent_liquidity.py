"""
💧 Liquidity Agent — Equal H/L · Liquidity Sweep · Session Levels
หน้าที่: ตรวจ liquidity pools และ sweep confirmation
เสริม SMC Agent → ยืนยัน reversals ที่มี stop-hunt ก่อนหน้า

MAX_SCORE = 8
  +3 Liquidity Sweep (stop-hunt confirmed → reversal likely)
  +2 Equal Highs/Lows swept (liquidity pool cleared)
  +2 Session position (price holding above session low / below session high)
  +1 Liquidity magnet (equal H/L in opposite direction = target)

ทำได้จาก OHLCV — ไม่ต้องการ tick data หรือ external API
"""
import numpy as np
import pandas as pd

NAME      = "Liquidity Agent"
EMOJI     = "💧"
MAX_SCORE = 8

EQ_TOLERANCE   = 0.002   # 0.2% tolerance สำหรับ "equal" highs/lows
SWEEP_LOOKBACK = 10      # มองย้อนหลังกี่แท่งหา liquidity sweep
EQ_LOOKBACK    = 20      # หา equal H/L ใน N แท่งล่าสุด
MIN_EQ_COUNT   = 2       # ต้องมีอย่างน้อย 2 แท่งที่ level เดียวกัน


def _find_equal_levels(values: np.ndarray, tolerance: float = EQ_TOLERANCE) -> bool:
    """
    หา levels ที่ราคาเคย test ซ้ำ (equal highs / equal lows)
    คืน True ถ้ามีอย่างน้อย 2 แท่งที่ level เดียวกัน (±tolerance%)
    """
    if len(values) < 3:
        return False
    # เปรียบเทียบทุก pair
    for i in range(len(values) - 1):
        ref = values[i]
        if ref == 0:
            continue
        near = np.abs(values[i + 1:] - ref) / ref < tolerance
        if near.sum() >= 1:
            return True
    return False


def _detect_sweep(df: pd.DataFrame, n: int = SWEEP_LOOKBACK) -> tuple[bool, bool]:
    """
    Bullish Sweep: candle ที่ low ทะลุต่ำกว่า previous swing low แต่ close กลับมาสูงกว่า
    → หมายถึง stop-hunt ใต้ liquidity pool แล้ว reverse ขึ้น

    Bearish Sweep: candle ที่ high ทะลุสูงกว่า previous swing high แต่ close กลับต่ำกว่า
    → stop-hunt เหนือ liquidity pool แล้ว reverse ลง

    คืน (bull_sweep: bool, bear_sweep: bool)
    """
    if len(df) < n + 2:
        return False, False

    window = df.tail(n + 1)
    lows   = window["low"].values
    highs  = window["high"].values
    closes = window["close"].values

    bull_sweep = False
    bear_sweep = False

    for i in range(1, len(window)):
        prev_low  = lows[:i].min()
        prev_high = highs[:i].max()

        # Bullish sweep: wick ต่ำกว่า prev low (>0.1%) แต่ close กลับเกิน
        if lows[i] < prev_low * (1 - 0.001) and closes[i] > prev_low:
            bull_sweep = True

        # Bearish sweep: wick สูงกว่า prev high (>0.1%) แต่ close กลับต่ำกว่า
        if highs[i] > prev_high * (1 + 0.001) and closes[i] < prev_high:
            bear_sweep = True

    return bull_sweep, bear_sweep


def _session_position(df: pd.DataFrame) -> tuple[bool, bool]:
    """
    คำนวณตำแหน่ง current price ภายใน session range วันนี้
    คืน (above_session_low, below_session_high) — bool, bool

    - above_session_low = True → ราคาไม่ได้อยู่ที่ low สุดของวัน (มี support)
    - below_session_high = True → ราคาไม่ได้อยู่ที่ high สุดของวัน (มี resistance headroom)
    """
    try:
        if df.index.tz is None:
            today_start = pd.Timestamp.utcnow().floor("D").tz_localize(None)
        else:
            today_start = pd.Timestamp.utcnow().floor("D").tz_convert(df.index.tz)

        today = df[df.index >= today_start]
        if today.empty or len(today) < 2:
            return True, True   # default neutral

        current_px   = float(df["close"].iloc[-1])
        session_low  = float(today["low"].min())
        session_high = float(today["high"].max())
        session_range = session_high - session_low

        if session_range < current_px * 0.0001:
            return True, True

        pos_pct = (current_px - session_low) / session_range  # 0=low, 1=high
        # อยู่เหนือ 20% ของ range = ไม่ได้อยู่ที่ low (above_session_low)
        # อยู่ต่ำกว่า 80% ของ range = ไม่ได้อยู่ที่ high (below_session_high)
        return (pos_pct > 0.2), (pos_pct < 0.8)

    except Exception:
        return True, True


def _score_long(bull_sweep: bool, eq_lows: bool, eq_highs: bool,
                above_session_low: bool) -> int:
    """
    Liquidity LONG score (max 8):
      +3 Bullish sweep ยืนยัน — stop-hunt ผ่านแล้ว reversal likely
      +2 Equal lows swept — liquidity pool ด้านล่างถูก clear แล้ว
      +2 Price above session low — มี session support
      +1 Equal highs ด้านบน — liquidity target / price magnet
    """
    s = 0
    if bull_sweep:         s += 3
    if eq_lows:            s += 2
    if above_session_low:  s += 2
    if eq_highs:           s += 1
    return s


def _score_short(bear_sweep: bool, eq_highs: bool, eq_lows: bool,
                 below_session_high: bool) -> int:
    """
    Liquidity SHORT score (max 8):
      +3 Bearish sweep ยืนยัน — stop-hunt ผ่านแล้ว reversal likely
      +2 Equal highs swept — liquidity pool ด้านบนถูก clear แล้ว
      +2 Price below session high — มี session resistance
      +1 Equal lows ด้านล่าง — liquidity target / price magnet
    """
    s = 0
    if bear_sweep:          s += 3
    if eq_highs:            s += 2
    if below_session_high:  s += 2
    if eq_lows:             s += 1
    return s


def run(df_1h: pd.DataFrame, df_4h: pd.DataFrame = None) -> dict | None:
    """
    💧 รัน Liquidity Agent
    ต้องการ OHLCV ≥ 30 แท่ง
    Returns dict หรือ None ถ้าข้อมูลไม่พอ
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 30:
        return None

    try:
        # ── Equal Highs/Lows ───────────────────────────────────────────────────
        tail     = df_1h.tail(EQ_LOOKBACK)
        eq_highs = _find_equal_levels(tail["high"].values)
        eq_lows  = _find_equal_levels(tail["low"].values)

        # ── Liquidity Sweep ────────────────────────────────────────────────────
        bull_sweep, bear_sweep = _detect_sweep(df_1h, n=SWEEP_LOOKBACK)

        # ── Session Position ───────────────────────────────────────────────────
        above_session_low, below_session_high = _session_position(df_1h)

        # ── Scores ─────────────────────────────────────────────────────────────
        sl = _score_long(bull_sweep, eq_lows, eq_highs, above_session_low)
        ss = _score_short(bear_sweep, eq_highs, eq_lows, below_session_high)

        return {
            "agent":               NAME,
            "emoji":               EMOJI,
            "score_long":          sl,
            "score_short":         ss,
            "max_score":           MAX_SCORE,
            "bull_sweep":          bull_sweep,
            "bear_sweep":          bear_sweep,
            "eq_highs":            eq_highs,
            "eq_lows":             eq_lows,
            "above_session_low":   above_session_low,
            "below_session_high":  below_session_high,
            "details": {
                "bull_sweep":          bull_sweep,
                "bear_sweep":          bear_sweep,
                "eq_highs":            eq_highs,
                "eq_lows":             eq_lows,
                "above_session_low":   above_session_low,
                "below_session_high":  below_session_high,
            },
        }

    except Exception as e:
        print(f"[ERR] agent_liquidity: {e}")
        return None
