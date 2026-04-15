import sys, os, sqlite3, json, warnings
from datetime import datetime, timezone
import pandas as pd
warnings.filterwarnings("ignore")

CLOSED_PATH = "closed_results.json"   # notify.py จะอ่านไฟล์นี้เพื่อส่ง Telegram

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
DB_PATH       = "paper_trades.db"
PORT_SIZE     = 1000.0   # ยอดเริ่มต้น USD
RISK_PCT      = 0.01     # A: 1% ของ balance คงเหลือ (dynamic)
MAX_LEVERAGE  = 5        # B: leverage สูงสุด (cap)
MAX_OPEN           = 10   # จำนวน position เปิดพร้อมกันสูงสุด
TRADE_TIMEOUT_HRS  = 48   # ปิด trade อัตโนมัติถ้าค้างเกิน N ชั่วโมง
TP1_R         = 1.2
TP2_R         = 2.0

# ── GUARDRAIL 1: Daily Loss Cap ────────────────────────────────────────────────
# ถ้าขาดทุนรวมวันนี้ (UTC) เกิน $50 → หยุดเปิด trade ใหม่ทั้งวัน
DAILY_LOSS_CAP = 50.0

# ── GUARDRAIL 2: Correlation Groups ──────────────────────────────────────────
# สูงสุด N positions ทิศเดียวกันในกลุ่มเดียวกัน
MAX_CORR_SAME_DIR = 2
CORR_GROUPS = [
    # Large caps — มักเคลื่อนตาม BTC
    {"BTC/USDT","ETH/USDT","BNB/USDT","SOL/USDT","AVAX/USDT",
     "DOT/USDT","LINK/USDT","NEAR/USDT","APT/USDT","SUI/USDT","TON/USDT"},
    # XRP ecosystem
    {"XRP/USDT","ADA/USDT","XLM/USDT","HBAR/USDT"},
    # Meme coins
    {"DOGE/USDT","SHIB/USDT","PEPE/USDT","TRUMP/USDT"},
    # DeFi
    {"AAVE/USDT","UNI/USDT","DEXE/USDT","ENA/USDT"},
    # AI / Data
    {"RENDER/USDT","WLD/USDT","TAO/USDT","ICP/USDT","ONDO/USDT"},
]

exchange = ccxt.okx({"enableRateLimit": True})


# ── POSITION SIZING ───────────────────────────────────────────────────────────
def calc_position(balance, entry_px, sl_px):
    """
    Position Sizing — margin 1% ของ Available × 5x leverage
      margin_usd  = balance × RISK_PCT  (e.g. $1054 × 1% = $10.54)
      leverage    = MAX_LEVERAGE = 5x
      notional    = margin × leverage   (e.g. $10.54 × 5 = $52.73)
      qty         = notional / entry_px
      actual_risk = notional × sl_dist%  (ขาดทุนถ้าโดน SL — จุด SL กำหนดโดย agent)

    Return: (qty, notional_usd, leverage, margin_usd, actual_risk_usd)
    """
    sl_dist = abs(entry_px - sl_px) / entry_px
    sl_dist = max(sl_dist, 0.001)

    margin_usd  = balance * RISK_PCT          # 1% ของยอดเงิน
    leverage    = float(MAX_LEVERAGE)         # 5x
    notional    = margin_usd * leverage       # margin × 5
    qty         = notional / entry_px
    actual_risk = notional * sl_dist          # risk จริงถ้าโดน SL

    return (
        qty,
        round(notional,     2),
        round(leverage,     2),
        round(margin_usd,   2),
        round(actual_risk,  2),
    )


# ── DB SETUP ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol       TEXT,
            side         TEXT,
            score        INTEGER,
            entry_px     REAL,
            sl_px        REAL,
            tp1_px       REAL,
            tp2_px       REAL,
            sl_pct       REAL,
            rsi          REAL,
            qty          REAL    DEFAULT 0,
            notional_usd REAL    DEFAULT 0,
            leverage     REAL    DEFAULT 1,
            margin_usd   REAL    DEFAULT 0,
            risk_usd     REAL    DEFAULT 0,
            status       TEXT    DEFAULT 'OPEN',
            tp1_hit      INTEGER DEFAULT 0,
            exit_px      REAL,
            outcome      TEXT,
            pnl_usd      REAL,
            opened_at    TEXT,
            closed_at    TEXT,
            score_trend  INTEGER DEFAULT 0,
            score_smc    INTEGER DEFAULT 0,
            score_osc    INTEGER DEFAULT 0
        )
    """)
    # migrate: เพิ่ม columns ที่อาจไม่มีใน DB เก่า
    new_cols = [
        ("score_trend",      "INTEGER DEFAULT 0"),
        ("score_smc",        "INTEGER DEFAULT 0"),
        ("score_osc",        "INTEGER DEFAULT 0"),
        ("qty",              "REAL DEFAULT 0"),
        ("notional_usd",     "REAL DEFAULT 0"),
        ("leverage",         "REAL DEFAULT 1"),
        ("margin_usd",       "REAL DEFAULT 0"),
        ("risk_usd",         "REAL DEFAULT 0"),
        ("exit_reason",      "TEXT"),
        ("regime",           "TEXT DEFAULT 'UNKNOWN'"),
        ("claude_approved",  "INTEGER DEFAULT 1"),   # 1=approved, 0=rejected (shouldn't reach here if 0)
        ("claude_reason",    "TEXT DEFAULT ''"),
    ]
    for col, definition in new_cols:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
        except Exception:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id      INTEGER PRIMARY KEY,
            balance REAL,
            updated TEXT
        )
    """)

    # ── Shadow Trades — บันทึก signals ที่ Claude Reject เพื่อ track outcome ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT,
            side          TEXT,
            score         INTEGER,
            entry_px      REAL,
            sl_px         REAL,
            tp1_px        REAL,
            tp2_px        REAL,
            sl_pct        REAL,
            regime        TEXT,
            claude_reason TEXT,
            created_at    TEXT,
            outcome       TEXT  DEFAULT 'PENDING',
            exit_px       REAL,
            resolved_at   TEXT
        )
    """)
    cur = conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()
    if cur[0] == 0:
        conn.execute("INSERT INTO portfolio VALUES (1, ?, ?)",
                     (PORT_SIZE, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return conn


# ── GET CURRENT PRICE ─────────────────────────────────────────────────────────
def get_price(symbol):
    try:
        t = exchange.fetch_ticker(symbol)
        return float(t["last"])
    except Exception:
        return None


def get_balance(conn):
    return conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()[0]


# ── GUARDRAIL 1: Daily Loss Cap ────────────────────────────────────────────────
def get_daily_pnl(conn) -> float:
    """ขาดทุนรวมวันนี้ (UTC) จาก trades ที่ปิดแล้ว — คืนค่าลบถ้าขาดทุน"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM trades WHERE status='CLOSED' AND closed_at LIKE ?",
        (f"{today}%",)
    ).fetchone()
    return float(row[0]) if row else 0.0


# ── GUARDRAIL 2: Correlation Check ────────────────────────────────────────────
def check_correlation(conn, sym: str, side: str) -> tuple:
    """
    ตรวจว่าเปิด position นี้จะทำให้ concentrated ใน correlation group เกินไปหรือไม่
    คืน (ok: bool, reason: str)
    """
    for group in CORR_GROUPS:
        if sym not in group:
            continue
        placeholders = ",".join("?" * len(group))
        count = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE status='OPEN' AND symbol IN ({placeholders}) AND side=?",
            (*sorted(group), side)
        ).fetchone()[0]
        if count >= MAX_CORR_SAME_DIR:
            peers = [s for s in group if s != sym][:3]
            return False, f"Corr limit: {count}/{MAX_CORR_SAME_DIR} {side} in group ({', '.join(peers)}...)"
        return True, ""
    return True, ""   # ไม่อยู่ในกลุ่มใด


# ── SHADOW TRADE OUTCOME CHECKER ──────────────────────────────────────────────
def check_shadow_trades(conn):
    """
    ตรวจ shadow trades ที่ยังค้าง PENDING — ดูว่าถ้าเปิดจริงจะ WIN/LOSS
    resolve เมื่อ TP1 hit, SL hit, หรือ timeout 48h
    """
    rows = conn.execute("""
        SELECT id, symbol, side, entry_px, sl_px, tp1_px, created_at
        FROM shadow_trades WHERE outcome='PENDING'
    """).fetchall()

    if not rows:
        return

    now = datetime.now(timezone.utc)
    resolved = 0
    for row in rows:
        sid, sym, side, ep, sl, tp1, created_at = row
        px = get_price(sym)
        if not px:
            continue

        try:
            created_dt = datetime.fromisoformat(created_at)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            hours = (now - created_dt).total_seconds() / 3600
        except Exception:
            hours = 0

        hit_tp1 = tp1 and ((side == "LONG" and px >= tp1) or (side == "SHORT" and px <= tp1))
        hit_sl  = sl  and ((side == "LONG" and px <= sl)  or (side == "SHORT" and px >= sl))
        timed_out = hours >= TRADE_TIMEOUT_HRS

        if hit_tp1:
            outcome = "WIN"
        elif hit_sl:
            outcome = "LOSS"
        elif timed_out:
            if ep and ep > 0:
                outcome = "WIN" if ((side == "LONG" and px > ep) or (side == "SHORT" and px < ep)) else "LOSS"
            else:
                outcome = "TIMEOUT"
        else:
            continue

        conn.execute("""
            UPDATE shadow_trades SET outcome=?, exit_px=?, resolved_at=? WHERE id=?
        """, (outcome, round(px, 8), now.isoformat(), sid))
        resolved += 1

    if resolved:
        conn.commit()
        print(f"  [SHADOW] resolved {resolved} shadow trade(s)")


# ── OPEN TRADE ────────────────────────────────────────────────────────────────
def open_trade(conn, sig):
    # ── Guardrail 1: Daily Loss Cap ─────────────────────────────────────────
    daily_pnl = get_daily_pnl(conn)
    if daily_pnl < -DAILY_LOSS_CAP:
        print(f"  [GUARD] Daily loss cap: ${daily_pnl:.2f} < -${DAILY_LOSS_CAP} — หยุดเปิดทั้งวัน")
        return None

    # ── Guardrail 2: Correlation Check ──────────────────────────────────────
    corr_ok, corr_reason = check_correlation(conn, sig["symbol"], sig["side"])
    if not corr_ok:
        print(f"  [GUARD] {sig['symbol']} — {corr_reason}")
        return None

    # ── ตรวจ symbol ซ้ำ ─────────────────────────────────────────────────────
    existing = conn.execute(
        "SELECT id FROM trades WHERE symbol=? AND status='OPEN'",
        (sig["symbol"],)).fetchone()
    if existing:
        print(f"  [SKIP] {sig['symbol']} มี trade เปิดอยู่แล้ว")
        return None

    # ตรวจ max open positions
    open_count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
    if open_count >= MAX_OPEN:
        print(f"  [SKIP] {sig['symbol']} — เปิดครบ {MAX_OPEN} positions แล้ว")
        return None

    entry_px = sig["price"]
    sl_px    = sig["sl"]
    balance  = get_balance(conn)

    if entry_px > 0 and sl_px > 0 and abs(entry_px - sl_px) / entry_px >= 0.001:
        qty, notional, leverage, margin, risk = calc_position(balance, entry_px, sl_px)
    else:
        # fallback: fixed $10 risk ถ้าราคาไม่มีหรือผิดปกติ
        qty = notional = leverage = margin = risk = 0

    conn.execute("""
        INSERT INTO trades
        (symbol, side, score, entry_px, sl_px, tp1_px, tp2_px, sl_pct, rsi,
         qty, notional_usd, leverage, margin_usd, risk_usd,
         score_trend, score_smc, score_osc, regime,
         claude_approved, claude_reason, tf, opened_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        sig["symbol"], sig["side"], sig["score"],
        sig["price"], sig["sl"], sig["tp1"], sig["tp2"],
        sig["sl_pct"], sig["rsi"],
        qty, notional, leverage, margin, risk,
        sig.get("score_trend", 0),
        sig.get("score_smc",   0),
        sig.get("score_osc",   0),
        sig.get("regime", "UNKNOWN"),
        int(sig.get("claude_approved", True)),
        sig.get("claude_reason", ""),
        sig.get("tf", "1H"),
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(f"  ✅ เปิด #{trade_id} {sig['symbol']} {sig['side']} "
          f"@ {sig['price']} | qty={qty:.4f} notional=${notional:.0f} "
          f"lev={leverage:.1f}x risk=${risk:.2f}")

    # แจ้ง Telegram: Order Limit Hit
    _append_order_limit_hit(sig)
    return trade_id


# ── ENFORCE MAX POSITIONS — ปิด positions ส่วนเกิน ────────────────────────────
def enforce_max_positions(conn):
    """
    ถ้า open positions > MAX_OPEN → ปิดตัวที่ score ต่ำสุดที่ราคาตลาดปัจจุบัน
    คิด PnL จริง — outcome = WIN/LOSS/VOID ตามผลจริง
    """
    opens = conn.execute("""
        SELECT id, symbol, side, score, entry_px, qty
        FROM trades WHERE status='OPEN'
        ORDER BY score DESC, id ASC
    """).fetchall()

    excess = len(opens) - MAX_OPEN
    if excess <= 0:
        return 0

    # ปิดตัวที่ score ต่ำที่สุด (ท้ายสุดของ list)
    to_close = opens[-excess:]
    for t in to_close:
        tid, sym, side, score, ep, qty = t
        px = get_price(sym)
        if px and px > 0 and qty and qty > 0 and ep and ep > 0:
            diff = (px - ep) if side == "LONG" else (ep - px)
            pnl  = qty * diff
            outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "VOID")
        else:
            px      = ep   # ไม่ได้ราคา ใช้ entry แทน
            pnl     = 0
            outcome = "VOID"
        _close(conn, tid, px, outcome, pnl,
               reason=f"MaxPos forced close (score={score})")
        print(f"  [CLOSE] #{tid} {sym} {side} score={score} → {outcome} ${pnl:.2f} (เกิน MAX_OPEN={MAX_OPEN})")
    return excess


# ── CHECK OPEN TRADES ─────────────────────────────────────────────────────────
def check_open_trades(conn):
    trades = conn.execute("""
        SELECT id, symbol, side, entry_px, sl_px, tp1_px, tp2_px,
               tp1_hit, qty, risk_usd, opened_at
        FROM trades WHERE status='OPEN'
    """).fetchall()

    balance = get_balance(conn)
    closed  = []
    now     = datetime.now(timezone.utc)

    for t in trades:
        tid, sym, side, ep, sl, tp1, tp2, tp1_hit, qty, risk_usd, opened_at = t
        px = get_price(sym)
        if not px:
            continue

        # ── Trade Timeout — ปิดอัตโนมัติถ้าค้างเกิน TRADE_TIMEOUT_HRS ──
        try:
            opened_dt = datetime.fromisoformat(opened_at)
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=timezone.utc)
            hours_open = (now - opened_dt).total_seconds() / 3600
        except Exception:
            hours_open = 0

        if hours_open > TRADE_TIMEOUT_HRS:
            # คำนวณ PnL จริง ณ ราคาตลาด
            if qty and qty > 0 and ep and ep > 0:
                diff = (px - ep) if side == "LONG" else (ep - px)
                pnl  = qty * diff
            else:
                pnl = 0
            outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "VOID")
            _close(conn, tid, px, outcome, pnl,
                   reason=f"Timeout {TRADE_TIMEOUT_HRS}h ⏰")
            closed.append((tid, sym, outcome, pnl))
            continue

        hit_sl  = (side == "LONG"  and px <= sl) or (side == "SHORT" and px >= sl)
        hit_tp1 = not tp1_hit and (
            (side == "LONG"  and px >= tp1) or (side == "SHORT" and px <= tp1))
        hit_tp2 = tp1_hit and (
            (side == "LONG"  and px >= tp2) or (side == "SHORT" and px <= tp2))

        # ── คำนวณ PnL จาก qty จริง ────────────────────────────
        # ใช้ signed difference (tp1-ep, tp2-ep, sl-ep) ไม่ใช่ abs()
        # pnl_from_qty จัดการ LONG/SHORT direction อัตโนมัติ
        #
        # LONG:  tp1>ep → (tp1-ep)>0 → profit ✓   sl<ep → (sl-ep)<0 → loss ✓
        # SHORT: tp1<ep → (tp1-ep)<0 → pnl flipped → profit ✓
        #        sl>ep  → (sl-ep)>0  → pnl flipped → loss ✓
        if qty and qty > 0 and ep and ep > 0:
            sgn_tp1 = (tp1 - ep) if tp1 else 0   # negative for SHORT (tp1 < ep)
            sgn_tp2 = (tp2 - ep) if tp2 else 0   # negative for SHORT
            sgn_sl  = (sl  - ep) if sl  else 0   # negative for LONG, positive for SHORT

            def pnl_from_qty(units, price_diff):
                return units * price_diff if side == "LONG" else -units * price_diff

            pnl_full_tp2     = (pnl_from_qty(qty * 0.5, sgn_tp1) +
                                 pnl_from_qty(qty * 0.5, sgn_tp2))
            pnl_full_sl      = pnl_from_qty(qty, sgn_sl)
            # SL_BE: SL ย้ายมา entry → sl=ep → sgn_sl=0 → half at TP1 + half at 0
            pnl_sl_after_tp1 = (pnl_from_qty(qty * 0.5, sgn_tp1) +
                                 pnl_from_qty(qty * 0.5, sgn_sl))
        else:
            # trades เก่า: ใช้ fixed risk_usd (ไม่ขึ้นกับ LONG/SHORT direction)
            r = risk_usd if risk_usd else balance * RISK_PCT
            pnl_full_tp2      =  r * 0.5 * TP1_R + r * 0.5 * TP2_R
            pnl_full_sl       = -r
            pnl_sl_after_tp1  =  r * 0.5 * TP1_R   # half at TP1 profit, half at BE (0)

        if hit_tp1:
            # ── Trailing Stop: ขยับ SL → Breakeven (entry_px) ──
            conn.execute(
                "UPDATE trades SET tp1_hit=1, sl_px=? WHERE id=?",
                (ep, tid))
            conn.commit()
            print(f"  🎯 #{tid} {sym} TP1 Hit @ {px:.8g} → SL ขยับเป็น Breakeven {ep:.8g}")
            # แจ้ง Telegram ว่า TP1 hit
            _append_tp1_alert(sym, side, ep, px, tp1)

        elif hit_tp2:
            _close(conn, tid, px, "WIN", pnl_full_tp2, reason="TP2 ✅")
            closed.append((tid, sym, "WIN", pnl_full_tp2))

        elif hit_sl:
            if tp1_hit:
                # SL_BE = TP1 hit แล้ว SL ย้ายมา entry → เสมอตัว + กำไรจาก TP1 half
                # pnl_sl_after_tp1 guaranteed > 0 เพราะ sgn_tp1 ทำกำไรทั้ง LONG/SHORT
                _close(conn, tid, px, "WIN", pnl_sl_after_tp1, reason="SL_BE")
                closed.append((tid, sym, "WIN", pnl_sl_after_tp1))
            else:
                _close(conn, tid, px, "LOSS", pnl_full_sl, reason="SL ❌")
                closed.append((tid, sym, "LOSS", pnl_full_sl))

    return closed


def _close(conn, trade_id, exit_px, outcome, pnl, reason=""):
    # กำหนด exit_reason จาก reason string
    r_low = reason.lower()
    if "tp2" in r_low or "tp 2" in r_low:
        exit_reason = "TP2"
    elif "sl_be" in r_low:          # ต้องเช็ค SL_BE ก่อน tp1 เพราะ "sl_be" ไม่มี "tp"
        exit_reason = "SL_BE"
    elif "tp1" in r_low or "tp 1" in r_low:
        exit_reason = "TP1"
    elif "timeout" in r_low:
        exit_reason = "TIMEOUT"
    elif "maxpos" in r_low or "max_pos" in r_low or "forced" in r_low:
        exit_reason = "TIMEOUT"
    else:
        exit_reason = "SL"

    conn.execute("""
        UPDATE trades SET
            status='CLOSED', exit_px=?, outcome=?, pnl_usd=?, closed_at=?, exit_reason=?
        WHERE id=?
    """, (exit_px, outcome, round(pnl, 2),
          datetime.now(timezone.utc).isoformat(), exit_reason, trade_id))
    conn.execute("""
        UPDATE portfolio SET balance = balance + ?, updated = ? WHERE id=1
    """, (round(pnl, 2), datetime.now(timezone.utc).isoformat()))
    conn.commit()

    # อ่านข้อมูล trade เพื่อส่ง Telegram
    row = conn.execute(
        "SELECT symbol, side, entry_px, leverage, notional_usd FROM trades WHERE id=?",
        (trade_id,)
    ).fetchone()
    if row:
        sym, side, ep, lev, notional = row
        _append_closed_result(sym, side, ep, exit_px, outcome, pnl, lev, notional, reason)

    icon = "✅" if outcome == "WIN" else "❌"
    print(f"  {icon} ปิด #{trade_id} {outcome} PnL=${pnl:+.2f} [{reason}]")


def _append_closed_result(sym, side, entry_px, exit_px, outcome, pnl,
                           leverage, notional, reason):
    """เพิ่มผลลัพธ์ใน closed_results.json — notify.py จะอ่านแล้วส่ง Telegram"""
    results = []
    if os.path.exists(CLOSED_PATH):
        try:
            with open(CLOSED_PATH) as f:
                results = json.load(f)
        except Exception:
            results = []
    results.append({
        "symbol":   sym,
        "side":     side,
        "entry_px": round(entry_px or 0, 6),
        "exit_px":  round(exit_px  or 0, 6),
        "outcome":  outcome,
        "pnl":      round(pnl, 2),
        "leverage": round(leverage  or 0, 2),
        "notional": round(notional  or 0, 2),
        "reason":   reason,
        "ts":       datetime.now(timezone.utc).isoformat(),
    })
    with open(CLOSED_PATH, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


def _append_order_limit_hit(sig):
    """แจ้ง Telegram: Order Limit Hit — เมื่อ trade เปิดสำเร็จ"""
    results = []
    if os.path.exists(CLOSED_PATH):
        try:
            with open(CLOSED_PATH) as f:
                results = json.load(f)
        except Exception:
            results = []
    results.append({
        "type":     "ORDER_LIMIT_HIT",
        "symbol":   sig["symbol"],
        "side":     sig["side"],
        "entry_px": sig["price"],
        "tp1_px":   sig["tp1"],
        "tp2_px":   sig["tp2"],
        "sl_px":    sig["sl"],
        "ts":       datetime.now(timezone.utc).isoformat(),
    })
    with open(CLOSED_PATH, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


def _append_tp1_alert(sym, side, entry_px, tp1_px, tp1_target):
    """แจ้ง TP1 Hit ผ่าน closed_results.json — type='TP1'"""
    results = []
    if os.path.exists(CLOSED_PATH):
        try:
            with open(CLOSED_PATH) as f:
                results = json.load(f)
        except Exception:
            results = []
    results.append({
        "type":      "TP1",
        "symbol":    sym,
        "side":      side,
        "entry_px":  entry_px,
        "tp1_px":    tp1_px,
        "tp1_target": tp1_target,
        "reason":    f"TP1 ✅ SL → Breakeven @ {entry_px:.8g}",
        "ts":        datetime.now(timezone.utc).isoformat(),
    })
    with open(CLOSED_PATH, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


# ── PORTFOLIO SUMMARY ─────────────────────────────────────────────────────────
def summary(conn):
    bal   = get_balance(conn)
    tots  = conn.execute(
        "SELECT COUNT(*), SUM(pnl_usd) FROM trades WHERE status='CLOSED'").fetchone()
    wins  = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND outcome='WIN'").fetchone()[0]
    open_ = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]

    total_t = tots[0] or 0
    total_p = tots[1] or 0
    wr      = wins / total_t * 100 if total_t > 0 else 0

    print("\n" + "=" * 56)
    print("  PAPER TRADE PORTFOLIO")
    print("=" * 56)
    print(f"  Balance    : ${bal:,.2f}  ({(bal/PORT_SIZE-1)*100:+.1f}%)")
    print(f"  Open trades: {open_}")
    print(f"  Closed     : {total_t}  (W:{wins} L:{total_t-wins})")
    print(f"  Win Rate   : {wr:.1f}%")
    print(f"  Total PnL  : ${total_p:+,.2f}")

    opens = conn.execute("""
        SELECT symbol, side, entry_px, sl_px, tp1_px, score,
               leverage, notional_usd, risk_usd, tp1_hit
        FROM trades WHERE status='OPEN'
    """).fetchall()
    if opens:
        print(f"\n  OPEN TRADES:")
        print(f"  {'Symbol':<12} {'Side':<6} {'Entry':>9} {'SL':>9} "
              f"{'Lev':>5} {'Notional':>9} {'Risk$':>7} {'Score':>5} {'TP1?'}")
        for o in opens:
            sym, side, ep, sl, tp1, sc, lev, notional, risk, t1h = o
            lev_str = f"{lev:.1f}x" if lev else "-"
            not_str = f"${notional:.0f}" if notional else "-"
            risk_str = f"${risk:.2f}" if risk else "-"
            print(f"  {sym:<12} {side:<6} {ep:>9.4f} {sl:>9.4f} "
                  f"{lev_str:>5} {not_str:>9} {risk_str:>7} {sc:>5} "
                  f"{'✅' if t1h else '-'}")
    print("=" * 56)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 56)
    print("  PAPER TRADE ENGINE")
    print(f"  A: Dynamic risk 1% of balance  |  B: Leverage cap {MAX_LEVERAGE}x")
    print("=" * 56)

    conn = init_db()

    print(f"\n[0] Enforce MAX_OPEN={MAX_OPEN}...")
    voided = enforce_max_positions(conn)
    if voided:
        print(f"  Voided {voided} positions ส่วนเกิน")
    cnt = conn.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
    print(f"  Open positions: {cnt}/{MAX_OPEN}")

    print(f"\n[0b] Daily Loss Cap check (cap=${DAILY_LOSS_CAP})...")
    daily_pnl = get_daily_pnl(conn)
    if daily_pnl < -DAILY_LOSS_CAP:
        print(f"  ⛔ Daily loss cap ACTIVE: ${daily_pnl:.2f} — ไม่เปิด trade ใหม่วันนี้")
    else:
        print(f"  Daily PnL: ${daily_pnl:+.2f} (cap: -${DAILY_LOSS_CAP})")

    print("\n[0c] Shadow Trade outcomes...")
    check_shadow_trades(conn)

    print("\n[1] เช็ค Open Trades...")
    closed = check_open_trades(conn)
    if not closed:
        print("  ไม่มี trade ที่ถึง SL/TP")

    print("\n[2] อ่าน Signals...")
    if not os.path.exists("latest_signals.json"):
        print("  ไม่มีไฟล์ latest_signals.json")
    else:
        with open("latest_signals.json") as f:
            signals = json.load(f)
        print(f"  พบ {len(signals)} signals")

        # cooldown: symbols ที่เพิ่งปิดในรอบนี้ → ห้ามเปิดใหม่ทันที
        just_closed = {sym for _, sym, _, _ in closed}

        for sig in signals:
            if sig["symbol"] in just_closed:
                print(f"  [COOLDOWN] {sig['symbol']} เพิ่งปิดในรอบนี้ — รอรอบหน้า")
                continue
            open_trade(conn, sig)

    summary(conn)
    conn.close()

    # ── ส่ง Telegram ทุก event: TP1/TP2/SL/Timeout/OrderLimitHit ──
    print("\n[3] ส่ง Telegram Notifications...")
    try:
        import notify as NOTIFY
        NOTIFY.notify_closed_trades()
    except Exception as e:
        print(f"  [WARN] notify failed: {e}")


if __name__ == "__main__":
    main()
