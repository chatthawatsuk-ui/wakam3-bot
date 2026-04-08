import sys, os, sqlite3, json, warnings
from datetime import datetime, timezone
warnings.filterwarnings("ignore")

DB_PATH   = "paper_trades.db"
OUT_PATH  = "dashboard_data.json"
PORT_SIZE = 1000.0

def main():
    if not os.path.exists(DB_PATH):
        print(f"ไม่พบ {DB_PATH} — รัน bash run.sh ก่อน")
        data = {"balance":1000,"pnl":0,"win_rate":0,"total_trades":0,
                "open_trades":[],"closed_trades":[],"sessions":[],"equity":[1000],"signals":[]}
        with open(OUT_PATH,"w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Portfolio
    bal = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()
    balance = float(bal["balance"]) if bal else PORT_SIZE

    # Closed trades
    closed_rows = conn.execute("""
        SELECT id,symbol,side,entry_px,exit_px,outcome,pnl_usd,tp1_hit,opened_at,closed_at
        FROM trades WHERE status='CLOSED' ORDER BY id
    """).fetchall()
    closed = [dict(r) for r in closed_rows]
    total  = len(closed)
    wins   = sum(1 for t in closed if t["outcome"]=="WIN")
    wr     = wins/total*100 if total>0 else 0
    total_pnl = sum(t["pnl_usd"] or 0 for t in closed)

    # Equity curve
    equity = [PORT_SIZE]
    running = PORT_SIZE
    for t in closed:
        running += (t["pnl_usd"] or 0)
        equity.append(round(running, 2))

    # Open trades
    open_rows = conn.execute("""
        SELECT id,symbol,side,score,entry_px,sl_px,tp1_px,tp2_px,tp1_hit,rsi,opened_at
        FROM trades WHERE status='OPEN' ORDER BY id DESC
    """).fetchall()
    opens = [dict(r) for r in open_rows]

    # Sessions
    sess_data = {}
    for t in closed:
        # ดู session จาก opened_at hour
        try:
            h = datetime.fromisoformat(t["opened_at"]).hour
            if 1<=h<8:   s="ASIA"
            elif 8<=h<13: s="EUROPE"
            elif 13<=h<21: s="US"
            else: s="LATE"
        except: s="—"
        if s not in sess_data:
            sess_data[s] = {"name":s,"trades":0,"wins":0}
        sess_data[s]["trades"] += 1
        if t["outcome"]=="WIN": sess_data[s]["wins"] += 1
    sessions = []
    for s in ["ASIA","EUROPE","US","LATE"]:
        if s in sess_data:
            d = sess_data[s]
            d["wr"] = d["wins"]/d["trades"]*100 if d["trades"]>0 else 0
            sessions.append(d)

    # Signals from log
    signals = []
    if os.path.exists("latest_signals.json"):
        try:
            with open("latest_signals.json") as f:
                signals = json.load(f)
        except: pass

    # Scan results (all coins + scores)
    scan_results = []
    if os.path.exists("scan_results.json"):
        try:
            with open("scan_results.json") as f:
                raw = json.load(f)
            # เรียงจาก score สูงสุด
            scan_results = sorted(raw, key=lambda x: x.get("best_score", 0), reverse=True)
        except: pass

    last_scan = datetime.now(timezone.utc).isoformat()

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
        "generated":    datetime.now(timezone.utc).isoformat(),
    }

    with open(OUT_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    conn.close()
    print(f"✅ dashboard_data.json — {total} trades, balance ${balance:.2f}")

if __name__ == "__main__":
    main()
