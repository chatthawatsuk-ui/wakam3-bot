"""
📐 Position Manager v2 — Pyramiding Up Only
สำหรับ WAKAM3 AI Trade System (OKX Futures 20x)

กลยุทธ์: Pyramid Up เท่านั้น — เพิ่ม position ขณะกำไร เพื่อ run trend
Averaging Down: ตัดออกทั้งหมด

โครงสร้าง 4 ไม้:
  ไม้ 1 (base)  — 1.000% — signal ปกติ         → paper_trade.open_trade() จัดการ
  ไม้ 2         — 0.500% — pnl ≥ +0.5%, trend ≥ 9
  ไม้ 3         — 0.250% — pnl ≥ +1.5%, trend ≥ 10
  ไม้ 4         — 0.125% — pnl ≥ +3.0%, trend ≥ 11
  ─────────────────────────────────────────────────
  Total max     — ~1.875% ต่อ symbol (leverage 20x safe zone)

SL Management:
  - trail ขึ้นตาม swing low ใหม่จาก SMC agent (signal.sl)
  - fallback: ATR × 1.5 จาก current price
  - SL ใหม่ต้องดีกว่า SL เดิมเสมอ (ไม่ถอยหลัง)

Blocked:
  - regime = RANGING
  - pyramid_level ≥ 4 (ครบแล้ว)
  - direction ไม่ตรงกับ open trade
  - pnl ≤ 0 (ยังขาดทุนอยู่)
"""

from __future__ import annotations
import sqlite3
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────
DB_PATH            = "paper_trades.db"
MAX_LEVERAGE       = 20
MAX_PYRAMID_LEVELS = 4

# ══════════════════════════════════════════════════════════════════════════════
# PYRAMID LEVELS CONFIG
# (min_pnl_pct, size_pct_of_balance, min_score_trend)
# ══════════════════════════════════════════════════════════════════════════════
PYRAMID_LEVELS = [
    (0.5,  0.0050,   9),   # ไม้ 2: pnl ≥ +0.5%,  size=0.50%, trend ≥ 9
    (1.5,  0.0025,  10),   # ไม้ 3: pnl ≥ +1.5%,  size=0.25%, trend ≥ 10
    (3.0,  0.00125, 11),   # ไม้ 4: pnl ≥ +3.0%,  size=0.125%,trend ≥ 11
]

STRONG_TREND_SCORE = 12   # score_trend ≥ นี้ → ลด pnl threshold 30%


# ══════════════════════════════════════════════════════════════════════════════
# CORE: should_pyramid()
# ══════════════════════════════════════════════════════════════════════════════
def should_pyramid(
    signal: dict,
    open_trade: dict,
    current_price: float,
) -> tuple[bool, str, dict]:
    """
    ประเมินว่าควร Pyramid Up หรือไม่

    Args:
        signal:        output จาก signal_scanner
                       ต้องมี: side, score_trend, regime, sl (optional), atr (optional)
        open_trade:    row จาก DB
                       ต้องมี: side, entry_px, sl_px, tp1_hit, pyramid_level
        current_price: ราคาตลาดปัจจุบัน

    Returns:
        (should_add: bool, reason: str, params: dict)
    """
    side        = open_trade.get("side", "")
    entry_px    = open_trade.get("entry_px", 0)
    sl_px       = open_trade.get("sl_px", 0)
    tp1_hit     = open_trade.get("tp1_hit", 0)
    pyr_level   = open_trade.get("pyramid_level", 1)
    score_trend = signal.get("score_trend", 0)
    regime      = signal.get("regime", "UNKNOWN")
    atr         = signal.get("atr", 0)

    # ── Block: RANGING ────────────────────────────────────────────────────────
    if regime == "RANGING":
        return False, "RANGING — pyramid blocked", {}

    # ── Block: ครบ max level ─────────────────────────────────────────────────
    if pyr_level >= MAX_PYRAMID_LEVELS:
        return False, f"Max pyramid reached ({pyr_level}/{MAX_PYRAMID_LEVELS})", {}

    # ── Block: invalid price ──────────────────────────────────────────────────
    if entry_px <= 0 or current_price <= 0:
        return False, "entry_px หรือ current_price invalid", {}

    # ── คำนวณ pnl_pct ────────────────────────────────────────────────────────
    if side == "LONG":
        pnl_pct = (current_price - entry_px) / entry_px * 100
    elif side == "SHORT":
        pnl_pct = (entry_px - current_price) / entry_px * 100
    else:
        return False, f"side invalid: {side}", {}

    # ── ดึง config ของ level ถัดไป ────────────────────────────────────────────
    next_level_idx = pyr_level - 1
    if next_level_idx >= len(PYRAMID_LEVELS):
        return False, "ไม่มี level config เพิ่มเติม", {}

    min_pnl, size_pct, min_trend = PYRAMID_LEVELS[next_level_idx]

    # ── Special: หลัง TP1 hit → SL อยู่ที่ BE แล้ว → ผ่อนปรน ────────────────
    if tp1_hit:
        min_pnl = 0.0
    # ── Special: Strong trend → ลด threshold 30% ─────────────────────────────
    elif score_trend >= STRONG_TREND_SCORE:
        min_pnl = min_pnl * 0.7

    # ── Check pnl ────────────────────────────────────────────────────────────
    if pnl_pct < min_pnl:
        return False, (
            f"pnl ยังไม่ถึง ไม้ {pyr_level + 1} "
            f"({pnl_pct:+.2f}% < {min_pnl:.2f}% required)"
        ), {}

    # ── Check score_trend ────────────────────────────────────────────────────
    if score_trend < min_trend:
        return False, (
            f"score_trend ต่ำ ไม้ {pyr_level + 1} "
            f"({score_trend}/13 < {min_trend} required)"
        ), {}

    # ── คำนวณ SL trail ───────────────────────────────────────────────────────
    new_sl = signal.get("sl", 0)
    if not new_sl or new_sl <= 0:
        if atr > 0:
            new_sl = (current_price - atr * 1.5) if side == "LONG" \
                else (current_price + atr * 1.5)
        else:
            new_sl = sl_px

    # trail เท่านั้น
    effective_sl = max(new_sl, sl_px) if side == "LONG" else min(new_sl, sl_px)

    # ── ผ่านทุก check ────────────────────────────────────────────────────────
    next_level = pyr_level + 1
    params = {
        "level":          next_level,
        "size_pct":       size_pct,
        "size_multiplier":size_pct / 0.01,
        "suggested_sl":   effective_sl,
        "trail_sl_moved": effective_sl != sl_px,
        "pnl_pct":        round(pnl_pct, 2),
        "tp1_hit":        bool(tp1_hit),
        "min_trend_used": min_trend,
        "min_pnl_used":   round(min_pnl, 2),
    }
    reason = (
        f"PYRAMID ไม้ {next_level}: "
        f"pnl={pnl_pct:+.2f}% | size={size_pct*100:.3f}% of balance | "
        f"trend={score_trend}/13 | "
        f"SL {sl_px:.6g}→{effective_sl:.6g} "
        f"({'trailed' if effective_sl != sl_px else 'unchanged'})"
    )
    return True, reason, params


# ══════════════════════════════════════════════════════════════════════════════
# EVALUATE — entry point หลัก
# ══════════════════════════════════════════════════════════════════════════════
def evaluate(
    signal: dict,
    open_trade: dict,
    current_price: float,
) -> tuple[str, str, dict]:
    """
    Returns: (action, reason, params)
    action = "PYRAMID" | "SKIP"
    """
    if signal.get("side") != open_trade.get("side"):
        return "SKIP", "Signal direction ไม่ตรงกับ open trade", {}

    entry_px = open_trade.get("entry_px", 0)
    side     = open_trade.get("side", "")

    if entry_px <= 0:
        return "SKIP", "entry_px invalid", {}

    if side == "LONG":
        pnl_pct = (current_price - entry_px) / entry_px * 100
    else:
        pnl_pct = (entry_px - current_price) / entry_px * 100

    if pnl_pct <= 0 and not open_trade.get("tp1_hit", 0):
        return "SKIP", f"ยังขาดทุน (pnl={pnl_pct:+.2f}%) — ไม่ pyramid", {}

    ok, reason, params = should_pyramid(signal, open_trade, current_price)
    if ok:
        return "PYRAMID", reason, params
    return "SKIP", reason, {}


# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def get_open_trade(conn: sqlite3.Connection, symbol: str, side: str) -> Optional[dict]:
    row = conn.execute("""
        SELECT id, symbol, side, entry_px, sl_px, tp1_px, tp2_px,
               tp1_hit, qty, notional_usd, margin_usd, risk_usd,
               pyramid_level, score_trend, score_smc, score_osc
        FROM trades
        WHERE symbol=? AND side=? AND status='OPEN'
        ORDER BY opened_at ASC LIMIT 1
    """, (symbol, side)).fetchone()

    if not row:
        return None

    cols = ["id","symbol","side","entry_px","sl_px","tp1_px","tp2_px",
            "tp1_hit","qty","notional_usd","margin_usd","risk_usd",
            "pyramid_level","score_trend","score_smc","score_osc"]
    return dict(zip(cols, row))


def apply_pyramid(
    conn: sqlite3.Connection,
    trade_id: int,
    params: dict,
    new_entry_px: float,
    balance: float,
) -> bool:
    try:
        trade = conn.execute(
            "SELECT entry_px, qty, notional_usd, margin_usd, pyramid_level, tp1_hit, sl_px "
            "FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if not trade:
            return False

        old_entry, old_qty, old_notional, old_margin, pyr_lvl, tp1_hit, cur_sl = trade
        old_qty      = old_qty      or 0.0
        old_notional = old_notional or 0.0
        old_margin   = old_margin   or 0.0
        pyr_lvl      = pyr_lvl      or 1

        size_pct     = params["size_pct"]
        add_margin   = balance * size_pct
        add_notional = add_margin * MAX_LEVERAGE
        add_qty      = add_notional / new_entry_px if new_entry_px > 0 else 0

        total_qty      = old_qty + add_qty
        avg_entry      = ((old_qty * old_entry) + (add_qty * new_entry_px)) / total_qty \
                         if total_qty > 0 else new_entry_px
        total_notional = old_notional + add_notional
        total_margin   = old_margin   + add_margin
        new_level      = pyr_lvl + 1
        new_sl         = params.get("suggested_sl")

        # ถ้า TP1 hit แล้ว (BE lock) และไม่มี suggested_sl → ใช้ avg entry ใหม่เป็น BE
        # เพื่อไม่ให้ sl_px ต่ำกว่า avg entry หลัง pyramid
        if not new_sl and tp1_hit:
            new_sl = round(avg_entry, 8)

        update = {
            "entry_px":      round(avg_entry,      8),
            "qty":           round(total_qty,       6),
            "notional_usd":  round(total_notional,  4),
            "margin_usd":    round(total_margin,    4),
            "pyramid_level": new_level,
        }
        if new_sl:
            update["sl_px"] = new_sl

        set_clause = ", ".join(f"{k}=?" for k in update)
        conn.execute(f"UPDATE trades SET {set_clause} WHERE id=?",
                     list(update.values()) + [trade_id])
        conn.commit()

        sl_info = f" | SL→{new_sl:.6g}" if new_sl else ""
        print(
            f"  🔺 PYRAMID ไม้ {new_level} #{trade_id} "
            f"avg {old_entry:.6g}→{avg_entry:.6g} "
            f"qty {old_qty:.4f}→{total_qty:.4f} "
            f"notional ${old_notional:.0f}→${total_notional:.0f}"
            f"{sl_info}"
        )
        return True

    except Exception as e:
        print(f"[ERR] apply_pyramid: {e}")
        return False


def migrate_db(conn: sqlite3.Connection):
    """ขยาย MAX_PYRAMID_PER_SYMBOL จาก 2 → 4 ใน paper_trade.py ด้วย"""
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN pyramid_level INTEGER DEFAULT 1")
        conn.commit()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# QUICK TEST
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    base_trade = {
        "id": 1, "symbol": "BTC/USDT", "side": "LONG",
        "entry_px": 90000.0, "sl_px": 88200.0,
        "tp1_hit": 0, "qty": 0.0233,
        "notional_usd": 2100.0, "margin_usd": 105.0,
        "pyramid_level": 1,
    }
    base_signal = {
        "symbol": "BTC/USDT", "side": "LONG",
        "score_trend": 10, "regime": "TRENDING",
        "atr": 450.0, "sl": 89200.0,
    }

    cases = [
        ("ไม้ 2 ✅ pnl+0.6% trend=10",  90540.0, 1, 10, "TRENDING"),
        ("ไม้ 2 ⛔ pnl+0.3% ยังไม่ถึง", 90270.0, 1, 10, "TRENDING"),
        ("ไม้ 3 ✅ pnl+1.6% trend=10",  91440.0, 2, 10, "TRENDING"),
        ("ไม้ 3 ⛔ trend=9 ไม่ผ่าน",    91440.0, 2,  9, "TRENDING"),
        ("ไม้ 4 ✅ pnl+3.1% trend=11",  92790.0, 3, 11, "TRENDING"),
        ("ไม้ 4 ⛔ trend=10 ไม่พอ",     92790.0, 3, 10, "TRENDING"),
        ("ครบ 4 ไม้แล้ว ⛔",            93000.0, 4, 12, "TRENDING"),
        ("RANGING block ⛔",             90540.0, 1, 10, "RANGING"),
        ("Strong trend ✅ ลด threshold", 90300.0, 1, 12, "TRENDING"),
        ("ขาดทุน ⛔",                    89100.0, 1, 10, "TRENDING"),
    ]

    print("=" * 65)
    print("PYRAMID UP v2 — 4 LEVELS TEST")
    print("=" * 65)
    for label, px, lvl, trend, regime in cases:
        trade  = {**base_trade, "pyramid_level": lvl}
        signal = {**base_signal, "score_trend": trend, "regime": regime}
        action, reason, params = evaluate(signal, trade, px)
        icon = "✅" if action == "PYRAMID" else "⛔"
        print(f"\n{icon} {label}")
        print(f"   {reason}")
        if params:
            print(f"   → size={params['size_pct']*100:.3f}% "
                  f"SL→{params['suggested_sl']:.6g}")
    print("\n" + "=" * 65)
