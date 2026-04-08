import sys, os, sqlite3, json, warnings
from datetime import datetime, timezone
warnings.filterwarnings("ignore")

DB_PATH   = "paper_trades.db"
OUT_PATH  = "dashboard_data.json"
PORT_SIZE = 1000.0

def main():
    last_scan = datetime.now(timezone.utc).isoformat()

    # ── โหลด scan_results ก่อนเลย (ไม่ต้องรอ DB) ──────────────────────────────
    scan_results = []
    if os.path.exists("scan_results.json"):
        try:
            with open("scan_results.json") as f:
                raw = json.load(f)
            scan_results = sorted(raw, key=lambda x: x.get("best_score", 0), reverse=True)
            print(f"  โหลด scan_results.json — {len(scan_results)} coins")
        except Exception as e:
            print(f"  [WARN] scan_results.json: {e}")
    else:
        print("  ไม่พบ scan_results.json — รอ live_trader.py รันก่อน")

    # ── โหลด signals ─────────────────────────────────────────────────────────
    signals = []
    if os.path.exists("latest_signals.json"):
        try:
            with open("latest_signals.json") as f:
                signals = json.load(f)
        except: pass

    # ── ถ้าไม่มี DB ให้ save แค่ scan_results ก่อนแล้วออก ────────────────────
    if not os.path.exists(DB_PATH):
        print(f"  ไม่พบ {DB_PATH} — บันทึก scan_results เท่านั้น")
        data = {
            "balance": PORT_SIZE, "pnl": 0, "win_rate": 0, "total_trades": 0,
            "open_trades": [], "closed_trades": [], "sessions": [],
            "equity": [PORT_SIZE], "signals": signals,
            "scan_results": scan_results,
            "last_scan": last_scan,
            "generated": last_scan,
        }
        with open(OUT_PATH, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✅ dashboard_data.json — no DB, {len(scan_results)} scan results")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ── Portfolio balance ─────────────────────────────────────────────────────
    bal = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()
    balance = float(bal["balance"]) if bal else PORT_SIZE

    # ── Closed trades (แปลง field names ให้ตรงกับ dashboard) ─────────────────
    closed_rows = conn.execute("""
        SELECT id, symbol, side, entry_px, exit_px, outcome, pnl_usd,
               sl_px, tp1_px, tp1_hit, opened_at, closed_at
        FROM trades WHERE status='CLOSED' ORDER BY id
    """).fetchall()

    closed = []
    for r in closed_rows:
        ep = r["entry_px"] or 0
        xp = r["exit_px"] or 0
        pnl_pct = 0.0
        if ep > 0 and xp > 0:
            raw_pct = (xp - ep) / ep * 100
            pnl_pct = raw_pct if r["side"] == "LONG" else -raw_pct
        closed.append({
            "id":           r["id"],
            "symbol":       r["symbol"],
            "side":         r["side"],
            "entry_price":  r["entry_px"],
            "exit_price":   r["exit_px"],
            "sl_price":     r["sl_px"],
            "tp_price":     r["tp1_px"],
            "outcome":      r["outcome"],
            "pnl":          r["pnl_usd"],
            "pnl_pct":      round(pnl_pct, 2),
            "open_time":    r["opened_at"],
            "close_time":   r["closed_at"],
        })

    total     = len(closed)
    wins      = sum(1 for t in closed if t["outcome"] == "WIN")
    wr        = wins / total * 100 if total > 0 else 0
    total_pnl = sum(t["pnl"] or 0 for t in closed)

    # ── Equity curve ──────────────────────────────────────────────────────────
    equity  = [PORT_SIZE]
    running = PORT_SIZE
    for t in closed:
        running += (t["pnl"] or 0)
        equity.append(round(running, 2))

    # ── Open trades (แปลง field names) ────────────────────────────────────────
    open_rows = conn.execute("""
        SELECT id, symbol, side, score, entry_px, sl_px, tp1_px, tp2_px,
               tp1_hit, rsi, opened_at
        FROM trades WHERE status='OPEN' ORDER BY id DESC
    """).fetchall()

    opens = []
    for r in open_rows:
        opens.append({
            "id":           r["id"],
            "symbol":       r["symbol"],
            "side":         r["side"],
            "score":        r["score"],
            "entry_price":  r["entry_px"],
            "sl_price":     r["sl_px"],
            "tp_price":     r["tp1_px"],
            "rsi":          r["rsi"],
            "pnl_pct":      0,
            "pnl":          0,
            "open_time":    r["opened_at"],
        })

    # ── Sessions (แก้ key names) ───────────────────────────────────────────────
    sess_data = {}
    for t in closed:
        try:
            h = datetime.fromisoformat(t["open_time"]).hour
            if 1 <= h < 8:    s = "ASIA"
            elif 8 <= h < 13: s = "EUROPE"
            elif 13 <= h < 21: s = "US"
            else:              s = "LATE"
        except:
            s = "—"
        if s not in sess_data:
            sess_data[s] = {"session": s, "total": 0, "wins": 0}
        sess_data[s]["total"] += 1
        if t["outcome"] == "WIN":
            sess_data[s]["wins"] += 1

    sessions = []
    for s in ["ASIA", "EUROPE", "US", "LATE"]:
        if s in sess_data:
            d = sess_data[s]
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1) if d["total"] > 0 else 0
            sessions.append(d)

    # ── บันทึก ────────────────────────────────────────────────────────────────
    data = {
        "balance":      round(balance, 2),
        "pnl":          round(total_pnl, 2),
        "win_rate":     round(wr, 1),
        "total_trades": total,
        "open_trades":  opens,
        "closed_trades": closed,
        "sessions":     sessions,
        "equity":       equity,
        "signals":      signals,
        "scan_results": scan_results,
        "last_scan":    last_scan,
        "generated":    last_scan,
    }

    with open(OUT_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    conn.close()
    print(f"✅ dashboard_data.json — {total} trades, balance ${balance:.2f}, {len(scan_results)} scan results")

if __name__ == "__main__":
    main()
