import sys, os, json, sqlite3
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("[ERROR] pip install requests")
    sys.exit(1)

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

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
def _fmt(px):
    """smart format ราคา — ป้องกัน PEPE/SHIB แสดง 0.0"""
    if px <= 0:
        return "0"
    if px < 0.0001:
        return f"{px:.10f}".rstrip('0')
    if px < 0.001:
        return f"{px:.8f}".rstrip('0')
    if px < 0.1:
        return f"{px:.6f}".rstrip('0')
    if px < 10:
        return f"{px:.4f}"
    return f"{px:,.2f}"


def signal_msg(sig):
    is_long   = sig["side"] == "LONG"
    dir_icon  = "🟢" if is_long else "🔴"
    kz_tag    = " ⚡KZ" if sig.get("in_kz") else ""
    regime    = sig.get("regime", "")
    regime_str = f"\n📊 Regime : {regime}" if regime else ""
    sym_clean = sig['symbol'].replace("/USDT", "/USDT.P")
    return (
        f"================================\n"
        f"🚀 <b>Signal : {sym_clean} - RR1.2{kz_tag}</b>\n"
        f"↕️ Direction : {sig['side']} {dir_icon}\n"
        f"--------------------------------\n"
        f"🔵 Entry : {_fmt(sig['price'])}\n"
        f"🟢 TP1   : {_fmt(sig['tp1'])}  (RR1.2)\n"
        f"🟢 TP2   : {_fmt(sig['tp2'])}  (RR2.0)\n"
        f"🔴 SL    : {_fmt(sig['sl'])}  (-{sig['sl_pct']}%)\n"
        f"🎫 Risk per trade : 1-5%\n"
        f"📊 Score : {sig['score']}/45"
        f"  [🎯{sig.get('score_trend',0)} 🏦{sig.get('score_smc',0)} 📈{sig.get('score_osc',0)}"
        f" 💧{sig.get('score_liq',0)} 💰{sig.get('score_fund',0)}]"
        f"  RSI:{sig['rsi']}{regime_str}\n"
        f"================================\n"
        f"⚠️ Paper Trade เท่านั้น"
    )


def weekly_report_msg(proposal, backtest_summary=None):
    """สรุป Weekly Report + Backtest ส่ง Telegram"""
    today = proposal.get("generated", "")
    lines = [
        "================================",
        f"📋 <b>WEEKLY REPORT — {today}</b>",
        "⚠️ Proposal Only (ต้องคอนเฟิมก่อนปรับ)",
        "================================",
    ]

    # Backtest summary
    if backtest_summary:
        s = backtest_summary
        pnl_sign = "+" if (s.get("total_pnl") or 0) >= 0 else ""
        wr_icon  = "🟢" if (s.get("wr") or 0) >= 50 else "🔴"
        pnl_icon = "🟢" if (s.get("total_pnl") or 0) >= 0 else "🔴"
        lines += [
            "",
            "📊 <b>Backtest (Walk-Forward)</b>",
            f"🔵 Trades   : {s.get('n', 0)}",
            f"{wr_icon} Win Rate  : {s.get('wr', 0)}%",
            f"{pnl_icon} Total PnL : {pnl_sign}${s.get('total_pnl', 0)}",
            f"📈 Sharpe   : {s.get('sharpe', 0)}",
            f"📉 Max DD   : {s.get('dd', 0)}%",
        ]

    # Level 4
    l4      = proposal.get("level4", {})
    props   = l4.get("proposals", {})
    changes = {k: v for k, v in props.items()
               if isinstance(v, dict) and v.get("proposed") != v.get("current")}
    lines += ["", "🔬 <b>Level 4 — Condition Analysis</b>"]
    if changes:
        lines.append(f"📌 เสนอปรับ {len(changes)} conditions:")
        for cond, v in list(changes.items())[:5]:
            arrow = "↑" if v["proposed"] > v["current"] else "↓"
            lines.append(f"  {arrow} {cond}: {v['current']} → {v['proposed']}")
        if len(changes) > 5:
            lines.append(f"  ... และอีก {len(changes)-5} conditions")
    else:
        lines.append("  ✅ ไม่มีการเปลี่ยนแปลงแนะนำ")

    # Level 5
    l5          = proposal.get("level5", {})
    regime_data = l5.get("regime_performance", {})
    lines += ["", "🌐 <b>Level 5 — Market Regime</b>"]
    if regime_data and "error" not in regime_data:
        for regime, d in regime_data.items():
            wr = d.get("win_rate", 0)
            lines.append(f"  {regime}: {d.get('count',0)} trades, WR {wr:.0%}")
    else:
        lines.append("  ⚠️ ข้อมูลไม่พอ (รอ 10+ trades)")

    # Level 6 — Claude weight proposal (brief summary only, full in separate msg)
    l6 = proposal.get("level6", {})
    cp = l6.get("claude_weight_proposal")
    if cp:
        conf_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(cp.get("confidence",""), "⚪")
        lines += [
            "",
            "🤖 <b>Level 6 — Claude Weight Proposal</b>",
            f"  🎯{cp['trend']:.3f} 🏦{cp['smc']:.3f} 📈{cp['osc']:.3f} {conf_icon}",
            "  → ดูรายละเอียดในข้อความถัดไป",
        ]

    lines += ["", "================================", "💾 ดูรายละเอียด → proposals/"]
    return "\n".join(lines)


def weight_proposal_msg(proposal):
    """สร้าง Telegram message สำหรับ Claude Haiku weight proposal + deep analysis"""
    l6 = proposal.get("level6", {})
    cp = l6.get("claude_weight_proposal")
    if not cp:
        return None

    conf_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(cp.get("confidence", ""), "⚪")

    lines = [
        "================================",
        f"🤖 <b>AI Analysis — {proposal.get('generated','')} </b>",
        "================================",
        "",
        "⚖️ <b>Weight Proposal</b>",
        f"  🎯 Trend : {cp['trend']:.3f}",
        f"  🏦 SMC   : {cp['smc']:.3f}",
        f"  📈 Osc   : {cp['osc']:.3f}",
        f"  {conf_icon} Confidence: {cp.get('confidence','?')}",
        f"  📝 {cp.get('reasoning','')}",
    ]

    if cp.get("best_tf"):
        lines += [
            "",
            "⏱ <b>Best Timeframe</b>",
            f"  🏆 {cp['best_tf']} — {cp.get('best_tf_reason', '')}",
        ]

    if cp.get("top_condition") or cp.get("weak_condition"):
        lines += ["", "🔬 <b>Condition Insight</b>"]
        if cp.get("top_condition"):
            lines.append(f"  ✅ ดีที่สุด : {cp['top_condition']}")
        if cp.get("weak_condition"):
            lines.append(f"  ⚠️ แย่ที่สุด: {cp['weak_condition']}")

    if cp.get("regime_insight"):
        lines += ["", f"🌐 <b>Regime</b>: {cp['regime_insight']}"]

    if cp.get("weekly_verdict"):
        lines += ["", f"📋 <b>สรุปสัปดาห์</b>: {cp['weekly_verdict']}"]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "✅ พิมพ์ <b>/approve_weights</b> เพื่ออนุมัติ",
        "❌ ไม่ตอบ = ไม่มีการเปลี่ยนแปลง",
        "================================",
    ]
    return "\n".join(lines)


def get_recent_messages(limit=20):
    """
    ดึง Telegram messages ล่าสุด (getUpdates)
    คืน list of {"update_id", "text", "date"} หรือ []
    """
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": limit, "allowed_updates": ["message"]},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        results = r.json().get("result", [])
        return [
            {
                "update_id": upd.get("update_id"),
                "text":      upd.get("message", {}).get("text", ""),
                "date":      upd.get("message", {}).get("date", 0),
            }
            for upd in results
            if upd.get("message", {}).get("text")
        ]
    except Exception as e:
        print(f"[NOTIFY] getUpdates error: {e}")
        return []


def order_limit_msg(r):
    is_long  = r.get("side", "") == "LONG"
    dir_icon = "🟢" if is_long else "🔴"
    sym_clean = r['symbol'].replace("/USDT", "/USDT.P")
    ep  = r.get("entry_px", 0)
    tp1 = r.get("tp1_px",   0)
    tp2 = r.get("tp2_px",   0)
    sl  = r.get("sl_px",    0)
    return (
        f"================================\n"
        f"🔵 <b>Update : {sym_clean} - Order Limit Hit ✅</b>\n"
        f"↕️ Direction : {r.get('side','')} {dir_icon}\n"
        f"--------------------------------\n"
        f"🔵 Entry : {_fmt(ep)}\n"
        f"🟢 TP1   : {_fmt(tp1)}  (RR1.2)\n"
        f"🟢 TP2   : {_fmt(tp2)}  (RR2.0)\n"
        f"🔴 SL    : {_fmt(sl)}\n"
        f"================================"
    )


def close_msg(r):
    icon    = "✅ WIN" if r.get("outcome") == "WIN" else "❌ LOSS"
    side    = r.get("side", "")
    ep      = r.get("entry_px", 0)
    ex      = r.get("exit_px",  0)
    pnl     = r.get("pnl",      0)
    lev     = r.get("leverage",  0)
    notional = r.get("notional", 0)
    reason  = r.get("reason", "")

    lev_str = f"  {lev:.1f}x / ${notional:.0f}" if lev else ""
    return (
        f"{icon} <b>{r['symbol']}</b> {side} — {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : {ep:.8g}\n"
        f"Exit   : {ex:.8g}\n"
        f"PnL    : <b>${pnl:+.2f}</b>{lev_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def tp1_msg(r):
    side = r.get("side", "")
    ep   = r.get("entry_px",   0)
    tp1  = r.get("tp1_px",     0)
    tgt  = r.get("tp1_target", 0)
    side_icon = "🟢" if side == "LONG" else "🔴"
    return (
        f"🎯 <b>TP1 HIT</b> — {side_icon} {r['symbol']} {side}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : {ep:.8g}\n"
        f"TP1    : {tp1:.8g}  ✅ (target {tgt:.8g})\n"
        f"SL ขยับ → Breakeven {ep:.8g}\n"
        f"รอ TP2 ต่อ 🚀\n"
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
    # เก็บแค่ 6 ชั่วโมงย้อนหลัง — ถ้า signal เดิม fire อีกรอบหลัง 6h จะส่งใหม่ได้
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    data   = {k: v for k, v in data.items() if v >= cutoff}
    with open(NOTIFIED_PATH, "w") as f:
        json.dump(data, f)


def _signal_key(sig):
    # key = symbol_side เท่านั้น — ป้องกันส่งซ้ำทุก 15 นาที
    # ts ถูกตัดออก เพราะมันเปลี่ยนทุก scan ทำให้ dedup ไม่ทำงาน
    return f"{sig['symbol']}_{sig['side']}"


def is_already_notified(sig):
    data = _load_notified()
    return _signal_key(sig) in data


def mark_notified(sig):
    data = _load_notified()
    data[_signal_key(sig)] = datetime.now(timezone.utc).isoformat()
    _save_notified(data)


# ── CLOSE RESULTS — paper_trade.py เขียน, notify.py อ่านแล้วล้าง ───────────
def notify_closed_trades():
    """อ่าน closed_results.json → ส่ง Telegram (CLOSE + TP1) → ลบไฟล์"""
    if not os.path.exists(CLOSED_PATH):
        return
    try:
        with open(CLOSED_PATH) as f:
            results = json.load(f)
        for r in results:
            if r.get("type") == "TP1":
                msg = tp1_msg(r)
                ok  = send(msg)
                status = "✅" if ok else "❌"
                print(f"  {status} TP1 {r['symbol']} {r.get('side','')}")
            elif r.get("type") == "ORDER_LIMIT_HIT":
                msg = order_limit_msg(r)
                ok  = send(msg)
                status = "✅" if ok else "❌"
                print(f"  {status} Order Limit Hit {r['symbol']} {r.get('side','')}")
            else:
                msg = close_msg(r)
                ok  = send(msg)
                status = "✅" if ok else "❌"
                print(f"  {status} ปิด {r['symbol']} {r.get('outcome','')} "
                      f"PnL=${r.get('pnl',0):+.2f}")
        os.remove(CLOSED_PATH)
    except Exception as e:
        print(f"[NOTIFY] closed_results error: {e}")


# ── ตรวจว่า signal นี้ถูกเปิดเทรดจริงหรือเปล่า ────────────────────────────────
DB_PATH = "paper_trades.db"

def get_traded_symbols(window_minutes: int = 60) -> set:
    """
    คืน set ของ (symbol, side) ที่ถูกเปิดเทรดจริงใน N นาทีล่าสุด
    ใช้ signal_log.was_traded=1 เป็น source หลัก (แม่นยำกว่า trades table)
    Fallback ไป trades table ถ้า signal_log ยังไม่มี
    """
    if not os.path.exists(DB_PATH):
        return set()
    try:
        conn = sqlite3.connect(DB_PATH)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()

        # ── วิธีที่ 1: ใช้ signal_log (แม่นที่สุด) ─────────────────────────────
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        if "signal_log" in tables:
            rows = conn.execute("""
                SELECT symbol, side FROM signal_log
                WHERE was_traded = 1 AND logged_at >= ?
            """, (cutoff,)).fetchall()
            if rows:
                conn.close()
                result = {(r[0], r[1]) for r in rows}
                print(f"  [DB] signal_log traded: {result}")
                return result
            print("  [DB] signal_log ไม่มี was_traded=1 ใน window นี้ — fallback trades table")

        # ── วิธีที่ 2: fallback trades table ─────────────────────────────────────
        rows = conn.execute("""
            SELECT symbol, side FROM trades
            WHERE opened_at >= ?
        """, (cutoff,)).fetchall()
        conn.close()
        result = {(r[0], r[1]) for r in rows}
        print(f"  [DB] trades fallback: {result}")
        return result

    except Exception as e:
        print(f"[NOTIFY] DB check error: {e}")
        return set()


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. ส่งผล closed trades ก่อน (TP/SL hit)
    print("[1] เช็ค Closed Trade Results...")
    notify_closed_trades()

    # 2. ส่ง new signals — เฉพาะที่ถูกเทรดจริงเท่านั้น
    print("[2] เช็ค New Signals (traded only)...")
    if not os.path.exists("latest_signals.json"):
        print("  ไม่มี latest_signals.json")
        sys.exit(0)

    with open("latest_signals.json") as f:
        signals = json.load(f)

    if not signals:
        print("  ไม่มี signal")
        sys.exit(0)

    # ดึง set ของ (symbol, side) ที่เพิ่งเปิด trade จริงใน 60 นาทีล่าสุด
    # → ใช้ signal_log.was_traded=1 เป็นหลัก, fallback trades table
    traded = get_traded_symbols(window_minutes=60)
    print(f"  Signals ใน JSON: {len(signals)} | เทรดจริง (DB): {len(traded)}")

    # debug: แสดง symbols ที่จะส่ง vs ที่ skip
    sig_list = [(s["symbol"], s["side"]) for s in signals]
    print(f"  Signals: {sig_list}")
    print(f"  Traded : {sorted(traded)}")

    new_count  = 0
    skip_count = 0
    for sig in signals:
        sym_side = (sig["symbol"], sig["side"])

        # ── กรอง: ส่งเฉพาะที่มี trade จริงใน DB ───────────────────────────
        if sym_side not in traded:
            print(f"  [NOT TRADED] {sig['symbol']} {sig['side']} — skip")
            skip_count += 1
            continue

        if is_already_notified(sig):
            print(f"  [DEDUP] {sig['symbol']} {sig['side']} — ส่งไปแล้วใน 6h")
            skip_count += 1
            continue

        msg = signal_msg(sig)
        ok  = send(msg)
        if ok:
            mark_notified(sig)
            new_count += 1
        status = "✅ ส่งแล้ว" if ok else "❌ ส่งไม่ได้"
        print(f"  {sig['symbol']} {sig['side']} — {status}")

    print(f"  ✅ ส่ง {new_count} signals ใหม่ | ⏭ skip {skip_count} (ไม่ได้เทรด/ส่งแล้ว)")
