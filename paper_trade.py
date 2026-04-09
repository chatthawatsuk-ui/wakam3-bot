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
MAX_OPEN      = 5        # จำนวน position เปิดพร้อมกันสูงสุด
TP1_R         = 1.2
TP2_R         = 2.0

exchange = ccxt.okx({"enableRateLimit": True})


# ── POSITION SIZING ───────────────────────────────────────────────────────────
def calc_position(balance, entry_px, sl_px):
    """
    A: Dynamic risk — 1% ของ balance คงเหลือ (compound, ไม่ fixed $10)
    B: Risk-based sizing — คำนวณ position size จาก SL distance + leverage cap 5x

    สูตร:
      risk_usd = balance × 1%          ← A: dynamic
      notional = risk_usd / sl_dist%   ← B: position ที่จะ lose ≤ risk_usd ที่ SL
      leverage = notional / balance     ← leverage จริง
      leverage = min(leverage, 5x)      ← cap ไม่เกิน 5x
      qty      = notional / entry_px    ← จำนวน unit จริง

    Return: (qty, notional_usd, leverage, margin_usd, actual_risk_usd)
    """
    risk_usd    = balance * RISK_PCT
    sl_dist_pct = abs(entry_px - sl_px) / entry_px
    sl_dist_pct = max(sl_dist_pct, 0.001)          # ป้องกัน div/0

    notional = risk_usd / sl_dist_pct              # notional ก่อน cap
    leverage = notional / balance

    if leverage > MAX_LEVERAGE:                     # cap ที่ 5x
        leverage = MAX_LEVERAGE
        notional = balance * MAX_LEVERAGE

    qty         = notional / entry_px
    margin_usd  = notional / leverage
    actual_risk = notional * sl_dist_pct           # risk จริงหลัง cap

    return (
        qty,
        round(notional,    2),
        round(leverage,    2),
        round(margin_usd,  2),
        round(actual_risk, 2),
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
        ("score_trend",  "INTEGER DEFAULT 0"),
        ("score_smc",    "INTEGER DEFAULT 0"),
        ("score_osc",    "INTEGER DEFAULT 0"),
        ("qty",          "REAL DEFAULT 0"),
        ("notional_usd", "REAL DEFAULT 0"),
        ("leverage",     "REAL DEFAULT 1"),
        ("margin_usd",   "REAL DEFAULT 0"),
        ("risk_usd",     "REAL DEFAULT 0"),
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


# ── OPEN TRADE ────────────────────────────────────────────────────────────────
def open_trade(conn, sig):
    # ตรวจ symbol ซ้ำ
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
         score_trend, score_smc, score_osc, opened_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        sig["symbol"], sig["side"], sig["score"],
        sig["price"], sig["sl"], sig["tp1"], sig["tp2"],
        sig["sl_pct"], sig["rsi"],
        qty, notional, leverage, margin, risk,
        sig.get("score_trend", 0),
        sig.get("score_smc",   0),
        sig.get("score_osc",   0),
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(f"  ✅ เปิด #{trade_id} {sig['symbol']} {sig['side']} "
          f"@ {sig['price']} | qty={qty:.4f} notional=${notional:.0f} "
          f"lev={leverage:.1f}x risk=${risk:.2f}")
    return trade_id


# ── CHECK OPEN TRADES ─────────────────────────────────────────────────────────
def check_open_trades(conn):
    trades = conn.execute("""
        SELECT id, symbol, side, entry_px, sl_px, tp1_px, tp2_px,
               tp1_hit, qty, risk_usd
        FROM trades WHERE status='OPEN'
    """).fetchall()

    balance = get_balance(conn)
    closed  = []

    for t in trades:
        tid, sym, side, ep, sl, tp1, tp2, tp1_hit, qty, risk_usd = t
        px = get_price(sym)
        if not px:
            continue

        hit_sl  = (side == "LONG"  and px <= sl) or (side == "SHORT" and px >= sl)
        hit_tp1 = not tp1_hit and (
            (side == "LONG"  and px >= tp1) or (side == "SHORT" and px <= tp1))
        hit_tp2 = tp1_hit and (
            (side == "LONG"  and px >= tp2) or (side == "SHORT" and px <= tp2))

        # ── คำนวณ PnL จาก qty จริง (B) ────────────────────────
        # fallback → fixed risk_usd ถ้า qty=0 (trades เก่า)
        if qty and qty > 0 and ep and ep > 0:
            dist_tp1 = abs(tp1 - ep) if tp1 else 0
            dist_tp2 = abs(tp2 - ep) if tp2 else 0
            dist_sl  = abs(ep  - sl) if sl  else 0

            def pnl_from_qty(units, price_diff):
                return units * price_diff if side == "LONG" else -units * price_diff

            pnl_full_tp2 = (pnl_from_qty(qty * 0.5, dist_tp1) +
                            pnl_from_qty(qty * 0.5, dist_tp2))
            pnl_full_sl  = pnl_from_qty(qty, -dist_sl)
            pnl_sl_after_tp1 = (pnl_from_qty(qty * 0.5, dist_tp1) +
                                 pnl_from_qty(qty * 0.5, -dist_sl))
        else:
            # trades เก่า: ใช้ fixed risk_usd เหมือนเดิม
            r = risk_usd if risk_usd else balance * RISK_PCT
            pnl_full_tp2      =  r * 0.5 * TP1_R + r * 0.5 * TP2_R
            pnl_full_sl       = -r
            pnl_sl_after_tp1  =  r * 0.5 * TP1_R - r * 0.5

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
                outcome = "WIN" if pnl_sl_after_tp1 > 0 else "LOSS"
                _close(conn, tid, px, outcome, pnl_sl_after_tp1, reason="SL (หลัง TP1)")
                closed.append((tid, sym, outcome, pnl_sl_after_tp1))
            else:
                _close(conn, tid, px, "LOSS", pnl_full_sl, reason="SL ❌")
                closed.append((tid, sym, "LOSS", pnl_full_sl))

    return closed


def _close(conn, trade_id, exit_px, outcome, pnl, reason=""):
    conn.execute("""
        UPDATE trades SET
            status='CLOSED', exit_px=?, outcome=?, pnl_usd=?, closed_at=?
        WHERE id=?
    """, (exit_px, outcome, round(pnl, 2),
          datetime.now(timezone.utc).isoformat(), trade_id))
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


if __name__ == "__main__":
    main()
