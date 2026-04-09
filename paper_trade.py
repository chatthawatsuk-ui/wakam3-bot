import sys, os, sqlite3, json, warnings
from datetime import datetime, timezone
import pandas as pd
warnings.filterwarnings("ignore")

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
DB_PATH   = "paper_trades.db"
PORT_SIZE = 1000.0   # USD
RISK_PCT  = 0.01     # 1% per trade
TP1_R     = 1.2
TP2_R     = 2.0

exchange  = ccxt.okx({"enableRateLimit": True})

# ── DB SETUP ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT,
            side        TEXT,
            score       INTEGER,
            entry_px    REAL,
            sl_px       REAL,
            tp1_px      REAL,
            tp2_px      REAL,
            sl_pct      REAL,
            rsi         REAL,
            status      TEXT DEFAULT 'OPEN',
            tp1_hit     INTEGER DEFAULT 0,
            exit_px     REAL,
            outcome     TEXT,
            pnl_usd     REAL,
            opened_at   TEXT,
            closed_at   TEXT,
            score_trend INTEGER DEFAULT 0,
            score_smc   INTEGER DEFAULT 0,
            score_osc   INTEGER DEFAULT 0
        )
    """)
    # migrate: เพิ่ม columns ถ้า DB เก่ายังไม่มี
    for col in ("score_trend", "score_smc", "score_osc"):
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} INTEGER DEFAULT 0")
        except Exception:
            pass  # column มีอยู่แล้ว
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id        INTEGER PRIMARY KEY,
            balance   REAL,
            updated   TEXT
        )
    """)
    # init portfolio ถ้ายังไม่มี
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

# ── OPEN TRADE ────────────────────────────────────────────────────────────────
def open_trade(conn, sig):
    # ตรวจว่ามี open trade ของ symbol นี้แล้วหรือยัง
    existing = conn.execute(
        "SELECT id FROM trades WHERE symbol=? AND status='OPEN'",
        (sig["symbol"],)).fetchone()
    if existing:
        print(f"  [SKIP] {sig['symbol']} มี trade เปิดอยู่แล้ว")
        return None

    conn.execute("""
        INSERT INTO trades
        (symbol,side,score,entry_px,sl_px,tp1_px,tp2_px,sl_pct,rsi,
         score_trend,score_smc,score_osc,opened_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        sig["symbol"], sig["side"], sig["score"],
        sig["price"], sig["sl"], sig["tp1"], sig["tp2"],
        sig["sl_pct"], sig["rsi"],
        sig.get("score_trend", 0),
        sig.get("score_smc",   0),
        sig.get("score_osc",   0),
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(f"  ✅ เปิด Paper Trade #{trade_id} — {sig['symbol']} {sig['side']} @ {sig['price']}")
    return trade_id

# ── CHECK OPEN TRADES ─────────────────────────────────────────────────────────
def check_open_trades(conn):
    trades = conn.execute(
        "SELECT id,symbol,side,entry_px,sl_px,tp1_px,tp2_px,tp1_hit FROM trades WHERE status='OPEN'"
    ).fetchall()

    closed = []
    for t in trades:
        tid, sym, side, ep, sl, tp1, tp2, tp1_hit = t
        px = get_price(sym)
        if not px:
            continue

        dist     = abs(ep - sl)
        hit_sl   = (side=="LONG" and px <= sl) or (side=="SHORT" and px >= sl)
        hit_tp1  = not tp1_hit and ((side=="LONG" and px >= tp1) or (side=="SHORT" and px <= tp1))
        hit_tp2  = tp1_hit and ((side=="LONG" and px >= tp2) or (side=="SHORT" and px <= tp2))

        risk_usd = PORT_SIZE * RISK_PCT

        if hit_tp1:
            conn.execute("UPDATE trades SET tp1_hit=1 WHERE id=?", (tid,))
            conn.commit()
            print(f"  🎯 #{tid} {sym} TP1 Hit @ {px:.4f}")

        elif hit_tp2:
            pnl = risk_usd*0.5*TP1_R + risk_usd*0.5*TP2_R
            _close(conn, tid, px, "WIN", pnl)
            closed.append((tid, sym, "WIN", pnl))

        elif hit_sl:
            if tp1_hit:
                pnl = risk_usd*0.5*TP1_R - risk_usd*0.5
                outcome = "WIN" if pnl > 0 else "LOSS"
            else:
                pnl     = -risk_usd
                outcome = "LOSS"
            _close(conn, tid, px, outcome, pnl)
            closed.append((tid, sym, outcome, pnl))

    return closed

def _close(conn, trade_id, exit_px, outcome, pnl):
    conn.execute("""
        UPDATE trades SET
            status='CLOSED', exit_px=?, outcome=?, pnl_usd=?,
            closed_at=?
        WHERE id=?
    """, (exit_px, outcome, round(pnl,2),
          datetime.now(timezone.utc).isoformat(), trade_id))

    # อัพเดท portfolio balance
    conn.execute("""
        UPDATE portfolio SET
            balance = balance + ?,
            updated = ?
        WHERE id = 1
    """, (round(pnl,2), datetime.now(timezone.utc).isoformat()))
    conn.commit()
    icon = "✅" if outcome=="WIN" else "❌"
    print(f"  {icon} ปิด #{trade_id} {outcome} PnL=${pnl:+.2f}")

# ── PORTFOLIO SUMMARY ─────────────────────────────────────────────────────────
def summary(conn):
    bal  = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()[0]
    tots = conn.execute("SELECT COUNT(*),SUM(pnl_usd) FROM trades WHERE status='CLOSED'").fetchone()
    wins = conn.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND outcome='WIN'").fetchone()[0]
    open_= conn.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]

    total_t = tots[0] or 0
    total_p = tots[1] or 0
    wr      = wins/total_t*100 if total_t > 0 else 0

    print("\n" + "="*46)
    print("  PAPER TRADE PORTFOLIO")
    print("="*46)
    print(f"  Balance    : ${bal:,.2f}  ({(bal/PORT_SIZE-1)*100:+.1f}%)")
    print(f"  Open trades: {open_}")
    print(f"  Closed     : {total_t}  (W:{wins} L:{total_t-wins})")
    print(f"  Win Rate   : {wr:.1f}%")
    print(f"  Total PnL  : ${total_p:+,.2f}")

    # แสดง open trades
    opens = conn.execute("""
        SELECT symbol,side,entry_px,sl_px,tp1_px,tp2_px,score,opened_at,tp1_hit
        FROM trades WHERE status='OPEN'
    """).fetchall()
    if opens:
        print(f"\n  OPEN TRADES:")
        print(f"  {'Symbol':<10} {'Side':<6} {'Entry':>10} {'SL':>10} {'TP1':>10} {'TP2':>10} {'Score':>5} {'TP1?'}")
        for o in opens:
            sym,side,ep,sl,tp1,tp2,sc,ts,t1h = o
            print(f"  {sym:<10} {side:<6} {ep:>10.4f} {sl:>10.4f} {tp1:>10.4f} {tp2:>10.4f} {sc:>5} {'✅' if t1h else '-'}")
    print("="*46)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("="*46)
    print("  PAPER TRADE ENGINE")
    print("="*46)

    conn = init_db()

    # 1. เช็ค open trades ก่อน
    print("\n[1] เช็ค Open Trades...")
    closed = check_open_trades(conn)
    if not closed:
        print("  ไม่มี trade ที่ถึง SL/TP")

    # 2. รับ signals จาก live_trader
    print("\n[2] อ่าน Signals...")
    if not os.path.exists("latest_signals.json"):
        print("  ไม่มีไฟล์ latest_signals.json")
        print("  รัน python3 live_trader.py ก่อน")
    else:
        with open("latest_signals.json") as f:
            signals = json.load(f)
        print(f"  พบ {len(signals)} signals")
        for sig in signals:
            open_trade(conn, sig)

    # 3. แสดง summary
    summary(conn)
    conn.close()

if __name__ == "__main__":
    main()
