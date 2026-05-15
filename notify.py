import sys, os, json, sqlite3
from datetime import datetime, timezone, timedelta
from html import escape

try:
    import requests
except ImportError:
    print("[ERROR] pip install requests")
    sys.exit(1)

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

NOTIFIED_PATH = "notified_signals.json"   # track signals ที่ส่งไปแล้ว
CLOSED_PATH   = "closed_results.json"     # paper_trade.py เขียน, notify.py อ่าน
DB_PATH       = "paper_trades.db"


# ── SEND ──────────────────────────────────────────────────────────────────────
def send(message):
    def _send_chunk(chunk):
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200

    try:
        if len(message) <= 3900:
            return _send_chunk(message)

        ok = True
        chunk = ""
        for line in message.splitlines():
            candidate = f"{chunk}\n{line}" if chunk else line
            if len(candidate) > 3900:
                ok = _send_chunk(chunk) and ok
                chunk = line
            else:
                chunk = candidate
        if chunk:
            ok = _send_chunk(chunk) and ok
        return ok
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")
        return False


# ── MESSAGE TEMPLATES ─────────────────────────────────────────────────────────
def _proposal_link(proposal):
    repo = os.environ.get("GITHUB_REPOSITORY", "chatthawatsuk-ui/wakam3-bot")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    generated = proposal.get("generated") or "latest"
    path = f"proposals/{generated}_proposal.json" if generated != "latest" else "proposals/latest_proposal.json"
    return f"https://github.com/{repo}/blob/{ref}/{path}"


def _period_label(proposal):
    period = proposal.get("period") or {}
    cutoff = period.get("cutoff")
    until = period.get("until")
    mode = period.get("mode")
    days = period.get("days", 7)
    if not cutoff:
        return f"{days}d"
    try:
        dt = datetime.fromisoformat(cutoff)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if until:
            end = datetime.fromisoformat(until)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            label = f"{dt.strftime('%Y-%m-%d %H:%M UTC')} → {end.strftime('%Y-%m-%d %H:%M UTC')}"
        else:
            label = f"since {dt.strftime('%Y-%m-%d %H:%M UTC')}"
        if mode == "manual":
            return f"{label} · manual preview"
        if mode == "schedule":
            return f"{label} · scheduled weekly"
        return label
    except Exception:
        return f"weekly window ({days}d)"


def _performance_verdict(signal_review, trade_review, backtest_summary):
    wr = float((trade_review or {}).get("wr") or (signal_review or {}).get("wr") or 0)
    pnl = float((trade_review or {}).get("total_pnl") or 0)
    skipped = int((signal_review or {}).get("skipped") or 0)
    total = int((signal_review or {}).get("total") or 0)
    skip_pct = (skipped / total * 100) if total else 0
    live_wr = float((backtest_summary or {}).get("wr") or 0)
    live_pnl = float((backtest_summary or {}).get("total_pnl") or 0)

    if pnl < 0 or wr < 45 or live_pnl < 0 or live_wr < 45:
        verdict = "ระบบยังอ่อนในรอบนี้ ควรลดคะแนนเงื่อนไขที่พาเข้าแล้วแพ้บ่อยก่อน"
    elif skip_pct > 60:
        verdict = "สัญญาณออกเยอะ แต่โดนบล็อกเยอะ ควรดูคุณภาพ signal และ guard เพิ่ม"
    else:
        verdict = "ระบบพอใช้ได้ ยังไม่ต้องปรับแรง ให้ปรับเฉพาะเงื่อนไขที่สถิติชัด"
    return verdict


def _detailed_report_analysis(proposal, signal_review, trade_review, backtest_summary):
    period = proposal.get("period") or {}
    basis = period.get("basis") or ""
    mode = period.get("mode") or "manual"
    total = int((signal_review or {}).get("total") or 0)
    traded = int((signal_review or {}).get("traded") or 0)
    skipped = int((signal_review or {}).get("skipped") or 0)
    traded_pct = (traded / total * 100) if total else 0
    skipped_pct = (skipped / total * 100) if total else 0
    trade_wr = float((trade_review or {}).get("wr") or 0)
    trade_pnl = float((trade_review or {}).get("total_pnl") or 0)
    live_wr = float((backtest_summary or {}).get("wr") or 0)
    live_pnl = float((backtest_summary or {}).get("total_pnl") or 0)

    if mode == "manual":
        mode_text = "รอบนี้เป็น manual preview ใช้ช่วงข้อมูลของ weekly cycle ปัจจุบัน การกดเทสจะไม่รีเซ็ตฐานรายงานจริง"
    else:
        mode_text = "รอบนี้เป็น scheduled weekly report ใช้ข้อมูลของรอบสัปดาห์อัตโนมัติเต็มรอบ"

    lines = [
        "",
        "🧭 <b>สรุปแบบเต็ม</b>",
        f"  ช่วงข้อมูล: {escape(basis)}",
        f"  โหมดรายงาน: {escape(mode_text)}",
    ]
    if total:
        lines += [
            f"  Signal ทั้งหมด {total} ตัว แบ่งเป็นเปิด paper trade {traded} ตัว ({traded_pct:.1f}%) และ skip {skipped} ตัว ({skipped_pct:.1f}%)",
            "  จุดที่ต้องดู: ถ้า skipped สูงมาก แปลว่าระบบมี signal ออกเยอะ แต่ execution guard / position limit / filter กันไว้เยอะ",
        ]
    if trade_review:
        pnl_word = "บวก" if trade_pnl >= 0 else "ลบ"
        lines += [
            f"  เฉพาะไม้ที่เปิดจริง: {trade_review.get('n', 0)} trades, WR {trade_wr:.1f}%, PnL {pnl_word} ${trade_pnl:.2f}",
            "  ถ้า WR ต่ำกว่า 45% หรือ PnL ติดลบ ควรปรับแบบลดความเสี่ยงก่อน ไม่ควรเพิ่มความถี่การเข้าไม้",
        ]
    if backtest_summary:
        pnl_word = "บวก" if live_pnl >= 0 else "ลบ"
        lines += [
            f"  Live Paper Results: WR {live_wr:.1f}%, PnL {pnl_word} ${live_pnl:.2f}, Sharpe {backtest_summary.get('sharpe', 0)}",
            "  ส่วนนี้คือผลจริงจาก paper trading ไม่ใช่ 3Y offline backtest จึงใช้ตัดสินใจการปรับรายสัปดาห์",
        ]
    lines += [
        f"  ข้อสรุป: {escape(_performance_verdict(signal_review, trade_review, backtest_summary))}",
    ]
    return lines


def _condition_plain_text(cond, v):
    current = v.get("current", 0)
    proposed = v.get("proposed", 0)
    arrow = "↑" if proposed > current else "↓"
    label = _CONDITION_LABELS.get(cond, cond)
    reason = v.get("reason", "")
    meaning = "ให้ผ่านง่ายขึ้น" if proposed > current else "ให้ผ่านยากขึ้น"
    return (
        f"  {arrow} {escape(str(cond))}: {current} → {proposed} "
        f"({escape(label)}) — {escape(str(reason))}; ผลคือ {meaning}"
    )


def _regime_plain_text(regime, d):
    wr = d.get("win_rate", 0)
    count = d.get("count", 0)
    trend = d.get("avg_score_trend", 0)
    smc = d.get("avg_score_smc", 0)
    osc = d.get("avg_score_osc", 0)
    if wr < 0.45:
        state = "ยังทำผลงานต่ำ ควรลดความกล้าในการให้ผ่านหรือให้น้ำหนักเฉพาะ agent ที่พิสูจน์ตัวเองกว่า"
    elif wr < 0.52:
        state = "ยังกลาง ๆ ควรปรับเบา ๆ และรอดูข้อมูลเพิ่ม"
    else:
        state = "ทำผลงานพอใช้ได้ ควรคงหรือปรับแบบระวัง"
    return (
        f"  {regime}: {count} trades, WR {wr:.0%} — "
        f"avg score Trend {trend:.1f}/13, SMC {smc:.1f}/10, Osc {osc:.1f}/11; {state}"
    )


_CONDITION_LABELS = {
    "cdc_bull": "trend ฝั่ง Long",
    "cross_up": "EMA cross ฝั่ง Long",
    "cross_dn": "EMA cross ฝั่ง Short",
    "trail_slow_bull": "ATR trailing trend",
    "in_discount": "เข้า Long ใน discount zone",
    "in_premium": "เข้า Short ใน premium zone",
    "rsi_ob": "RSI overbought",
    "st_up": "Stochastic ขาขึ้น",
    "macd_dn": "MACD ขาลง",
}


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


def _get_balance():
    """ดึง balance ล่าสุดจาก portfolio table"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            "SELECT balance FROM portfolio ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else 1000.0
    except Exception:
        return 1000.0


def signal_msg(sig):
    is_long    = sig["side"] == "LONG"
    dir_icon   = "🟢" if is_long else "🔴"
    kz_tag     = " ⚡KZ" if sig.get("in_kz") else ""
    regime     = sig.get("regime", "")
    regime_str = f"\n📊 Regime : {regime}" if regime else ""
    sym_clean  = sig['symbol'].replace("/USDT", "/USDT.P")
    sig_label  = "Signal H" if sig.get("haiku_filtered") else "Signal"
    balance    = _get_balance()
    risk_usd   = balance * 0.01
    return (
        f"================================\n"
        f"🚀 <b>{sig_label} : {sym_clean} - RR1.2{kz_tag}</b>\n"
        f"↕️ Direction : {sig['side']} {dir_icon}\n"
        f"--------------------------------\n"
        f"🔵 Entry : {_fmt(sig['price'])}\n"
        f"🟢 TP1   : {_fmt(sig['tp1'])}  (RR1.2)\n"
        f"🟢 TP2   : {_fmt(sig['tp2'])}  (RR2.0)\n"
        f"🔴 SL    : {_fmt(sig['sl'])}  (-{sig['sl_pct']}%)\n"
        f"💰 Risk  : ${risk_usd:.2f}  (Lev 20x)\n"
        f"📊 Score : {sig['score']}/45"
        f"  [🎯{sig.get('score_trend',0)} 🏦{sig.get('score_smc',0)} 📈{sig.get('score_osc',0)}"
        f" 💧{sig.get('score_liq',0)} 💰{sig.get('score_fund',0)}]"
        f"  RSI:{sig['rsi']}{regime_str}\n"
        f"================================\n"
        f"⚠️ Paper Trade เท่านั้น"
    )


def signal_status_msg(sig, status_row):
    base = signal_msg(sig)
    if not sig.get("execution_allowed", True):
        reason = sig.get("execution_block_reason") or "EXECUTION_BLOCKED"
        status = f"⏭ Alert only ({reason})"
    elif not status_row:
        status = "⏳ ยังไม่พบสถานะใน DB"
    elif status_row.get("was_traded"):
        status = "✅ เปิดเป็น Paper Position"
    else:
        reason = status_row.get("skip_reason") or "SKIPPED"
        status = f"⏭ ไม่ได้เปิดเทรด ({reason})"
    return base + f"\n📌 Status : {status}"


def weekly_report_msg(proposal, backtest_summary=None):
    """สรุป Weekly Report + Backtest ส่ง Telegram"""
    today = proposal.get("generated", "")
    period_label = _period_label(proposal)
    proposal_url = _proposal_link(proposal)
    lines = [
        "================================",
        f"📋 <b>WEEKLY REPORT — {today}</b>",
        "👁 Weekly Watchlist Only (ยังไม่เปิดให้ approve)",
        "================================",
    ]

    signal_review = proposal.get("signal_review", {})
    if signal_review:
        lines += [
            "",
            f"📡 <b>Signal Review ({period_label})</b>",
            f"🔵 Signals : {signal_review.get('total', 0)}",
            f"✅ Traded  : {signal_review.get('traded', 0)}",
            f"⏭ Skipped : {signal_review.get('skipped', 0)}",
            f"📈 WR      : {signal_review.get('wr', 0)}%",
        ]

    trade_review = proposal.get("trade_review", {})
    if trade_review:
        pnl_sign = "+" if (trade_review.get("total_pnl") or 0) >= 0 else ""
        lines += [
            "",
            f"💼 <b>Trade Review ({period_label})</b>",
            f"🔵 Trades   : {trade_review.get('n', 0)}",
            f"📈 WR       : {trade_review.get('wr', 0)}%",
            f"💰 Total PnL: {pnl_sign}${trade_review.get('total_pnl', 0)}",
        ]

    # Live paper-trade summary
    if backtest_summary:
        s = backtest_summary
        pnl_sign = "+" if (s.get("total_pnl") or 0) >= 0 else ""
        wr_icon  = "🟢" if (s.get("wr") or 0) >= 50 else "🔴"
        pnl_icon = "🟢" if (s.get("total_pnl") or 0) >= 0 else "🔴"
        lines += [
            "",
            "📊 <b>Live Paper Results</b>",
            f"🔵 Trades   : {s.get('n', 0)}",
            f"{wr_icon} Win Rate  : {s.get('wr', 0)}%",
            f"{pnl_icon} Total PnL : {pnl_sign}${s.get('total_pnl', 0)}",
            f"📈 Sharpe   : {s.get('sharpe', 0)}",
            f"📉 Max DD   : {s.get('dd', 0)}%",
        ]

    if signal_review or trade_review or backtest_summary:
        lines += _detailed_report_analysis(proposal, signal_review, trade_review, backtest_summary)

    # Level 4
    l4      = proposal.get("level4", {})
    props   = l4.get("proposals", {})
    changes = {k: v for k, v in props.items()
               if isinstance(v, dict) and v.get("proposed") != v.get("current")}
    approval_enabled = bool(proposal.get("approval_enabled"))

    lines += ["", "🔬 <b>Level 4 — Condition Watchlist</b>"]
    if changes:
        lines.append(f"👁 พบ {len(changes)} conditions ที่ควรจับตา:")
        for cond, v in changes.items():
            lines.append(_condition_plain_text(cond, v))
        lines += [
            "  แปลแบบง่าย: เงื่อนไขกลุ่มนี้เคยช่วยดันคะแนนให้เข้า trade แต่ผล 7 วันล่าสุดแพ้บ่อย",
            "  Weekly จะใช้เป็น watchlist เท่านั้น ยังไม่สร้าง pending approval จากข้อมูล 7 วัน",
            "  ต้องรอ Monthly sample gate/cooldown ก่อนค่อยเสนอปรับ condition points จริง",
        ]
    else:
        lines.append("  ✅ ไม่มีการเปลี่ยนแปลงแนะนำ")

    # Level 5
    l5          = proposal.get("level5", {})
    regime_data = l5.get("regime_performance", {})
    lines += ["", "🌐 <b>Level 5 — Market Regime</b>"]
    if regime_data and "error" not in regime_data:
        lines.append("  หน้าที่: แยกว่าบอททำงานในตลาดแบบ TRENDING / VOLATILE ดีแค่ไหน")
        lines.append("  ระบบดูจำนวน trade, win rate, และคะแนนเฉลี่ยของ Trend/SMC/Osc ในแต่ละสภาพตลาด")
        for regime, d in regime_data.items():
            lines.append(_regime_plain_text(regime, d))
        l5_props = l5.get("proposals", {})
        if l5_props:
            lines.append("  ข้อเสนอ: ใช้น้ำหนัก agent แยกตาม regime แทนน้ำหนักเดียวทั้งตลาด")
            for regime, p in l5_props.items():
                lines.append(
                    f"    {regime}: Trend {p.get('W_TREND',0):.3f}, "
                    f"SMC {p.get('W_SMC',0):.3f}, Osc {p.get('W_OSC',0):.3f}"
                )
            lines.append("  แปลแบบง่าย: ตอนตลาดมี trend ระบบจะเชื่อ Trend มากสุด และลด SMC เพราะสถิติ SMC เฉลี่ยยังอ่อน")
            lines.append("  Weekly จะใช้เป็น watchlist เท่านั้น ยังไม่ปรับ regime weights จาก sample 7 วัน")
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
    else:
        lines += [
            "",
            "🤖 <b>Level 6 — Claude Analysis</b>",
            "  หน้าที่: ให้ Claude Haiku อ่านภาพรวมทั้งหมดแล้วเสนอ core weights ของ Trend/SMC/Osc แบบมีเหตุผล",
            "  core weights คือค่าน้ำหนักกลางของ agent ทั้ง 3 ตัว ไม่ใช่ condition points และไม่ใช่ regime weights",
            "  สถานะตอนนี้: ยังไม่ได้วิเคราะห์ เพราะไม่มี/ยังไม่ได้เติม Anthropic API credit หรือ key",
            "  ดังนั้นคำสั่งอนุมัติ weights ตอนนี้ยังไม่มีอะไรให้ใช้ รอให้ Level 6 สร้าง pending_weights.json ก่อน",
            "  ตอนนี้ใช้สถิติ rule-based จาก Level 4/5 ไปก่อน",
        ]

    condition_count = len(changes)
    lines += [
        "",
        "✅ <b>สถานะการอนุมัติ</b>",
        "  Weekly report รอบนี้เป็น monitoring/watchlist เท่านั้น",
        "  ไม่ต้องใช้คำสั่งอนุมัติ condition/regime จากข้อมูล 7 วัน",
        "  การปรับ condition points จะย้ายไป Monthly framework ที่มี sample gate + cooldown",
        "  การอนุมัติ weights ใช้เฉพาะตอนมี AI proposal ที่สร้าง pending_weights.json แล้วเท่านั้น",
        "",
        "================================",
        f"💾 รายละเอียดเต็ม → <a href=\"{proposal_url}\">proposal JSON</a>",
    ]
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
    base = r['symbol'].replace("/USDT", "")
    return f"Update : 🔵 {base} - Order Limit Hit"


def close_msg(r):
    base    = r['symbol'].replace("/USDT", "")
    outcome = r.get("outcome", "")
    reason  = r.get("reason", "").lower()
    pnl     = r.get("pnl", 0)
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

    if "sl_be" in reason:
        return f"Update : 🔒 {base} - SL → Breakeven (WIN)  {pnl_str}"
    elif "tp2" in reason:
        return f"Update : ✅ {base} - TP2 Hit (WIN)  {pnl_str}"
    elif "sl" in reason:
        return f"Update : ❌ {base} - SL Hit (LOSS)  {pnl_str}"
    elif "timeout" in reason:
        icon = "✅" if outcome == "WIN" else "❌"
        return f"Update : {icon} {base} - Timeout ({outcome})  {pnl_str}"
    elif "htf" in reason:
        icon = "✅" if outcome == "WIN" else "❌"
        return f"Update : {icon} {base} - HTF Exit ({outcome})  {pnl_str}"
    else:
        icon = "✅" if outcome == "WIN" else "❌"
        return f"Update : {icon} {base} - {outcome}  {pnl_str}"


def tp1_msg(r):
    base = r['symbol'].replace("/USDT", "")
    return f"Update : 🎯 {base} - TP1 Hit → SL ขยับ Breakeven"


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
    key  = _signal_key(sig)
    if key not in data:
        return False
    stored_ts = data[key]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    if stored_ts < cutoff:
        return False
    sig_ts = sig.get("ts") or ""
    if not sig_ts:
        return True
    return sig_ts <= stored_ts


def mark_notified(sig):
    data = _load_notified()
    data[_signal_key(sig)] = sig.get("ts") or datetime.now(timezone.utc).isoformat()
    _save_notified(data)


# ── CLOSE RESULTS — paper_trade.py เขียน, notify.py อ่านแล้วล้าง ───────────
def notify_closed_trades():
    """อ่าน closed_results.json → ส่ง Telegram (CLOSE + TP1) → ลบเฉพาะที่ส่งสำเร็จ"""
    if not os.path.exists(CLOSED_PATH):
        return
    try:
        with open(CLOSED_PATH) as f:
            results = json.load(f)

        failed = []
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
            if not ok:
                failed.append(r)

        if failed:
            # เขียนเฉพาะ event ที่ส่งไม่สำเร็จกลับไว้ รอบถัดไปจะ retry
            with open(CLOSED_PATH, "w") as f:
                json.dump(failed, f, ensure_ascii=False)
            print(f"  [NOTIFY] retry queue: {len(failed)} event(s) เก็บไว้สำหรับรอบถัดไป")
        else:
            os.remove(CLOSED_PATH)
    except Exception as e:
        print(f"[NOTIFY] closed_results error: {e}")


# ── ตรวจว่า signal นี้ถูกเปิดเทรดจริงหรือเปล่า ────────────────────────────────
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


def get_recent_signal_status(window_minutes: int = 120) -> dict:
    """คืนสถานะล่าสุดของ signals ใน window: {(symbol, side): {was_traded, skip_reason}}"""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
        rows = conn.execute("""
            SELECT symbol, side, was_traded, COALESCE(skip_reason, '') AS skip_reason, logged_at
            FROM signal_log
            WHERE logged_at >= ?
            ORDER BY logged_at DESC
        """, (cutoff,)).fetchall()
        conn.close()
        out = {}
        for sym, side, was_traded, skip_reason, _ in rows:
            key = (sym, side)
            if key not in out:
                out[key] = {"was_traded": int(was_traded or 0), "skip_reason": skip_reason or ""}
        return out
    except Exception as e:
        print(f"[NOTIFY] recent signal status error: {e}")
        return {}


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. ส่งผล closed trades ก่อน (TP/SL hit)
    print("[1] เช็ค Closed Trade Results...")
    notify_closed_trades()

    # 2. ส่ง new signals — ส่งทุก signal พร้อมสถานะ traded/skip
    print("[2] เช็ค New Signals (all signals)...")
    if not os.path.exists("latest_signals.json"):
        print("  ไม่มี latest_signals.json")
        sys.exit(0)

    with open("latest_signals.json") as f:
        signals = json.load(f)

    if not signals:
        print("  ไม่มี signal")
        sys.exit(0)

    status_map = get_recent_signal_status(window_minutes=120)
    traded = get_traded_symbols(window_minutes=60)
    print(f"  Signals ใน JSON: {len(signals)} | เทรดจริง (DB): {len(traded)} | มีสถานะล่าสุด: {len(status_map)}")

    # debug: แสดง symbols ที่จะส่ง vs ที่ skip
    sig_list = [(s["symbol"], s["side"]) for s in signals]
    print(f"  Signals: {sig_list}")
    print(f"  Traded : {sorted(traded)}")

    new_count  = 0
    skip_count = 0
    for sig in signals:
        sym_side = (sig["symbol"], sig["side"])

        if is_already_notified(sig):
            print(f"  [DEDUP] {sig['symbol']} {sig['side']} — ส่งไปแล้วใน 6h")
            skip_count += 1
            continue

        status_row = status_map.get(sym_side)
        msg = signal_status_msg(sig, status_row)
        ok  = send(msg)
        if ok:
            mark_notified(sig)
            new_count += 1
        status = "✅ ส่งแล้ว" if ok else "❌ ส่งไม่ได้"
        print(f"  {sig['symbol']} {sig['side']} — {status}")

    print(f"  ✅ ส่ง {new_count} signals ใหม่ | ⏭ skip {skip_count} (dedup/ส่งไม่สำเร็จ)")
