import sys, os, json
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("[ERROR] pip install requests")
    sys.exit(1)

# อ่านจาก environment variables (GitHub Actions Secrets)
# ถ้าไม่มีให้ fallback เป็น hardcoded (ใช้บน Mac)
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8696462277:AAFJQr2TkZBF0SkA3Cr2NuypcEshiJ2aUfA")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6512968157")

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

def signal_msg(sig):
    side_icon = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    kz_icon   = "⚡ Kill Zone" if sig.get("in_kz") else ""
    return (
        f"🤖 <b>AI TRADE SIGNAL</b> {kz_icon}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Symbol : <b>{sig['symbol']}</b>\n"
        f"Side   : {side_icon}\n"
        f"Score  : {sig['score']}/26\n"
        f"Price  : {sig['price']:,.4f}\n"
        f"SL     : {sig['sl']:,.4f}  (-{sig['sl_pct']}%)\n"
        f"TP1    : {sig['tp1']:,.4f}  (+RR1.2)\n"
        f"TP2    : {sig['tp2']:,.4f}  (+RR2.0)\n"
        f"RSI    : {sig['rsi']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ Paper Trade เท่านั้น"
    )

def close_msg(sym, side, outcome, pnl):
    icon = "✅ WIN" if outcome == "WIN" else "❌ LOSS"
    return (
        f"{icon}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Symbol : <b>{sym}</b>\n"
        f"Side   : {side}\n"
        f"PnL    : <b>${pnl:+.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━"
    )

if __name__ == "__main__":
    if not os.path.exists("latest_signals.json"):
        print("ไม่มี latest_signals.json — รัน live_trader.py ก่อน")
        sys.exit(0)

    with open("latest_signals.json") as f:
        signals = json.load(f)

    if not signals:
        print("ไม่มี signal ที่จะส่ง")
        sys.exit(0)

    for sig in signals:
        msg = signal_msg(sig)
        ok  = send(msg)
        status = "✅ ส่งแล้ว" if ok else "❌ ส่งไม่ได้"
        print(f"  {sig['symbol']} {sig['side']} — {status}")
