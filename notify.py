import sys, os, json
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("[ERROR] pip install requests")
    sys.exit(1)

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8696462277:AAFJQr2TkZBF0SkA3Cr2NuypcEshiJ2aUfA")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6512968157")

NOTIFIED_PATH = "notified_signals.json"   # track signals ที่ส่งไปแล้ว
CLOSED_PATH   = "closed_results.json"     # paper_trade.py เขียน, notify.py อ่าน


# ── SEND ──────────────────────────────────────────────────────────────────────
def send(message):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")
        return False


# ── MESSAGE TEMPLATES ─────────────────────────────────────────────────────────
def signal_msg(sig):
    side_icon = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    kz_icon   = " ⚡ Kill Zone" if sig.get("in_kz") else ""
    regime    = sig.get("regime", "")
    regime_str = f"\nRegime : {regime}" if regime else ""
    return (
        f"🤖 <b>AI TRADE SIGNAL</b>{kz_icon}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Symbol : <b>{sig['symbol']}</b>\n"
        f"Side   : {side_icon}\n"
        f"Score  : {sig['score']}/31  "
        f"[🎯{sig.get('score_trend',0)} 🏦{sig.get('score_smc',0)} 📈{sig.get('score_osc',0)}]\n"
        f"Price  : {sig['price']:,.4f}\n"
        f"SL     : {sig['sl']:,.4f}  (-{sig['sl_pct']}%)\n"
        f"TP1    : {sig['tp1']:,.4f}  (+RR1.2)\n"
        f"TP2    : {sig['tp2']:,.4f}  (+RR2.0)\n"
        f"RSI    : {sig['rsi']}{regime_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Paper Trade เท่านั้น"
    )


def close_msg(r):
    icon    = "✅ WIN" if r["outcome"] == "WIN" else "❌ LOSS"
    side    = r.get("side", "")
    ep      = r.get("entry_px", 0)
    ex      = r.get("exit_px",  0)
    pnl     = r.get("pnl",      0)
    lev     = r.get("leverage",  0)
    notional = r.get("notional", 0)
    reason  = r.get("reason", "")   # TP1/TP2/SL

    lev_str = f"  {lev:.1f}x / ${notional:.0f}" if lev else ""
    return (
        f"{icon} <b>{r['symbol']}</b> {side} — {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : {ep:,.4f}\n"
        f"Exit   : {ex:,.4f}\n"
        f"PnL    : <b>${pnl:+.2f}</b>{lev_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


# ── DEDUP — ตรวจว่าส่งไปแล้วหรือยัง ─────────────────────────────────────────
def _load_notified():
    try:
        if os.path.exists(NOTIFIED_PATH):
            with open(NOTIFIED_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_notified(data):
    # เก็บแค่ 24 ชั่วโมงย้อนหลัง
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    data   = {k: v for k, v in data.items() if v >= cutoff}
    with open(NOTIFIED_PATH, "w") as f:
        json.dump(data, f)


def _signal_key(sig):
    # key = symbol_side_ts — unique ต่อ signal
    return f"{sig['symbol']}_{sig['side']}_{sig.get('ts','')}"


def is_already_notified(sig):
    data = _load_notified()
    return _signal_key(sig) in data


def mark_notified(sig):
    data = _load_notified()
    data[_signal_key(sig)] = datetime.now(timezone.utc).isoformat()
    _save_notified(data)


# ── CLOSE RESULTS — paper_trade.py เขียน, notify.py อ่านแล้วล้าง ───────────
def notify_closed_trades():
    """อ่าน closed_results.json → ส่ง Telegram → ลบไฟล์"""
    if not os.path.exists(CLOSED_PATH):
        return
    try:
        with open(CLOSED_PATH) as f:
            results = json.load(f)
        for r in results:
            msg = close_msg(r)
            ok  = send(msg)
            status = "✅" if ok else "❌"
            print(f"  {status} ปิด {r['symbol']} {r.get('outcome','')} "
                  f"PnL=${r.get('pnl',0):+.2f}")
        os.remove(CLOSED_PATH)   # ล้างหลังส่งแล้ว
    except Exception as e:
        print(f"[NOTIFY] closed_results error: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. ส่งผล closed trades ก่อน (TP/SL hit)
    print("[1] เช็ค Closed Trade Results...")
    notify_closed_trades()

    # 2. ส่ง new signals (dedup)
    print("[2] เช็ค New Signals...")
    if not os.path.exists("latest_signals.json"):
        print("  ไม่มี latest_signals.json")
        sys.exit(0)

    with open("latest_signals.json") as f:
        signals = json.load(f)

    if not signals:
        print("  ไม่มี signal")
        sys.exit(0)

    new_count = 0
    for sig in signals:
        if is_already_notified(sig):
            print(f"  [SKIP] {sig['symbol']} {sig['side']} — ส่งไปแล้ว")
            continue
        msg = signal_msg(sig)
        ok  = send(msg)
        if ok:
            mark_notified(sig)
            new_count += 1
        status = "✅ ส่งแล้ว" if ok else "❌ ส่งไม่ได้"
        print(f"  {sig['symbol']} {sig['side']} — {status}")

    print(f"  ส่ง {new_count} signals ใหม่ (skip {len(signals)-new_count} ที่ส่งไปแล้ว)")
