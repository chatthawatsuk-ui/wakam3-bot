"""
💰 Funding Agent — Funding Rate · Open Interest Change (OKX SWAP)
หน้าที่: ตรวจ market positioning ผ่าน funding rate
เหมาะกับ perpetual futures — funding บอกว่าใคร over-crowded

MAX_SCORE = 6
  Funding ต่ำ/ลบ  → shorts over-crowded → ดีสำหรับ LONG (squeeze risk สำหรับ shorts)
  Funding สูง/บวก → longs over-crowded  → ดีสำหรับ SHORT (liquidation risk สำหรับ longs)

Hard Reject:
  Funding > +0.15% AND LONG  → longs overloaded (ห้ามเปิด LONG)
  Funding < -0.05% AND SHORT → shorts overloaded (ห้ามเปิด SHORT)

⚠️  ใช้ live API จาก OKX — fallback score=0 ถ้าไม่มี data (spot/error)
    ไม่ block signal ถ้า API ล้มเหลว — แค่ bonus=0
"""
import requests

NAME      = "Funding Agent"
EMOJI     = "💰"
MAX_SCORE = 6

OKX_BASE = "https://www.okx.com"
TIMEOUT  = 5   # seconds

# ── Funding Rate Thresholds (in decimal, NOT %) ────────────────────────────────
HARD_REJECT_LONG  =  0.0015   # > +0.15%: long over-crowded → ห้าม LONG
HARD_REJECT_SHORT = -0.0005   # < -0.05%: short over-crowded → ห้าม SHORT

# LONG scoring thresholds
STRONG_LONG_FUD   = -0.0003   # < -0.03%: shorts paying heavily (+3)
NEUTRAL_UPPER_FUD =  0.0001   # < +0.01%: neutral zone (+2)
MILD_LONG_FUD     =  0.0005   # < +0.05%: mild positive (+1)

# SHORT scoring thresholds
STRONG_SHORT_FUD  =  0.0005   # > +0.05%: longs paying heavily (+3)
NEUTRAL_LOWER_FUD =  0.0001   # > +0.01%: neutral-to-positive (+2)
MILD_SHORT_FUD    = -0.0003   # > -0.03%: mild negative (+1)


def _symbol_to_instid(symbol: str) -> str:
    """แปลง symbol format: BTC/USDT → BTC-USDT-SWAP"""
    return symbol.replace("/", "-") + "-SWAP"


def _get_funding_rate(inst_id: str) -> float:
    """
    ดึง current funding rate จาก OKX public API (ไม่ต้อง auth)
    Returns float (decimal, เช่น 0.0001 = 0.01%) หรือ None ถ้า error
    """
    try:
        url  = f"{OKX_BASE}/api/v5/public/funding-rate"
        resp = requests.get(url, params={"instId": inst_id}, timeout=TIMEOUT)
        data = resp.json()
        if data.get("code") == "0" and data.get("data"):
            return float(data["data"][0]["fundingRate"])
    except Exception:
        pass
    return None


def _score_long(funding_rate: float) -> int:
    """
    LONG score ตาม funding rate (max 3 — ก่อน hard reject check):
      < -0.03%  (+3): shorts paying a lot → squeeze risk สูง สำหรับ shorts
      < +0.01%  (+2): neutral → ไม่มีแรงกดดัน long
      < +0.05%  (+1): slightly positive → acceptable
      >= +0.05% ( 0): longs paying too much → risky
    """
    if funding_rate < STRONG_LONG_FUD:   return 3
    if funding_rate < NEUTRAL_UPPER_FUD: return 2
    if funding_rate < MILD_LONG_FUD:     return 1
    return 0


def _score_short(funding_rate: float) -> int:
    """
    SHORT score ตาม funding rate (max 3 — ก่อน hard reject check):
      > +0.05%  (+3): longs paying a lot → liquidation risk สูง สำหรับ longs
      > +0.01%  (+2): slightly positive → longs paying
      > -0.03%  (+1): neutral
      <= -0.03% ( 0): shorts over-crowded → risky for SHORT
    """
    if funding_rate >= STRONG_SHORT_FUD:  return 3
    if funding_rate >= NEUTRAL_LOWER_FUD: return 2
    if funding_rate >= MILD_SHORT_FUD:    return 1
    return 0


def run(symbol: str) -> dict:
    """
    💰 รัน Funding Agent
    Args:
        symbol: เช่น "BTC/USDT"
    Returns:
        dict เสมอ — score=0 ถ้าไม่มี data (ไม่ block signal)
    """
    inst_id      = _symbol_to_instid(symbol)
    funding_rate = _get_funding_rate(inst_id)

    # ── API ล้มเหลว / Spot pair ────────────────────────────────────────────────
    if funding_rate is None:
        return {
            "agent":             NAME,
            "emoji":             EMOJI,
            "score_long":        0,
            "score_short":       0,
            "max_score":         MAX_SCORE,
            "funding_rate":      None,
            "funding_rate_pct":  None,
            "available":         False,
            "hard_reject_long":  False,
            "hard_reject_short": False,
            "details": {
                "funding_rate":      None,
                "available":         False,
                "hard_reject_long":  False,
                "hard_reject_short": False,
            },
        }

    # ── Hard Reject Flags ──────────────────────────────────────────────────────
    hard_reject_long  = funding_rate >= HARD_REJECT_LONG    # > +0.15%
    hard_reject_short = funding_rate <= HARD_REJECT_SHORT   # < -0.05%

    sl = 0 if hard_reject_long  else _score_long(funding_rate)
    ss = 0 if hard_reject_short else _score_short(funding_rate)

    fr_pct = round(funding_rate * 100, 4)   # เก็บเป็น % สำหรับ display

    return {
        "agent":             NAME,
        "emoji":             EMOJI,
        "score_long":        sl,
        "score_short":       ss,
        "max_score":         MAX_SCORE,
        "funding_rate":      fr_pct,         # % เช่น 0.01 = 0.01%
        "funding_rate_pct":  fr_pct,
        "available":         True,
        "hard_reject_long":  hard_reject_long,
        "hard_reject_short": hard_reject_short,
        "details": {
            "funding_rate":      fr_pct,
            "available":         True,
            "hard_reject_long":  hard_reject_long,
            "hard_reject_short": hard_reject_short,
        },
    }
