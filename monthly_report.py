#!/usr/bin/env python3
"""
Monthly Report v0 — PHASE 1: Report-Only

สรุปผลการเทรดรอบ 30 วัน (หรือกำหนดเองผ่าน --days)
อ่านจาก paper_trades.db เท่านั้น — ไม่แก้ไขค่าใดๆ ในระบบ

Usage:
    python3 monthly_report.py                   # default 30 days
    python3 monthly_report.py --days 14         # custom period
    python3 monthly_report.py --telegram        # send to Telegram
    python3 monthly_report.py --days 30 --telegram
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = "paper_trades.db"
REPORTS_DIR = "reports/monthly"


# ═══════════════════════════════════════════════════════════════
# DATABASE QUERIES (read-only)
# ═══════════════════════════════════════════════════════════════

def _connect_db():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_closed_trades(conn, since_iso, until_iso):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT symbol, side, entry_px, exit_px, pnl_usd, outcome,
                  exit_reason, score, opened_at, closed_at,
                  tp1_hit, tp1_px, qty, notional_usd, regime
           FROM trades
           WHERE status = 'CLOSED'
             AND closed_at >= ? AND closed_at <= ?
           ORDER BY closed_at""",
        (since_iso, until_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_signal_log(conn, since_iso, until_iso):
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT symbol, side, score, was_traded, skip_reason,
                      logged_at, outcome
               FROM signal_log
               WHERE logged_at >= ? AND logged_at <= ?
               ORDER BY logged_at""",
            (since_iso, until_iso),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _fetch_balance(conn):
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT balance FROM portfolio WHERE id = 1"
        ).fetchone()
        return float(row["balance"]) if row else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# METRICS CALCULATION
# ═══════════════════════════════════════════════════════════════

def calc_trade_metrics(trades):
    if not trades:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl": 0, "avg_pnl": 0, "max_win": 0, "max_loss": 0,
            "profit_factor": 0, "max_drawdown": 0,
            "exit_reasons": {}, "regime_breakdown": {},
        }

    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    pnls = [t["pnl_usd"] for t in trades]
    total_pnl = sum(pnls)

    gross_win = sum(t["pnl_usd"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 0

    peak = 0
    dd = 0
    cumsum = 0
    for p in pnls:
        cumsum += p
        peak = max(peak, cumsum)
        dd = min(dd, cumsum - peak)

    exit_reasons = {}
    for t in trades:
        r = t.get("exit_reason") or "UNKNOWN"
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    regime_breakdown = {}
    for t in trades:
        reg = t.get("regime") or "UNKNOWN"
        if reg not in regime_breakdown:
            regime_breakdown[reg] = {"total": 0, "wins": 0, "pnl": 0}
        regime_breakdown[reg]["total"] += 1
        if t["outcome"] == "WIN":
            regime_breakdown[reg]["wins"] += 1
        regime_breakdown[reg]["pnl"] += t["pnl_usd"]

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
        "max_win": round(max(pnls), 2) if pnls else 0,
        "max_loss": round(min(pnls), 2) if pnls else 0,
        "profit_factor": profit_factor,
        "max_drawdown": round(dd, 2),
        "exit_reasons": exit_reasons,
        "regime_breakdown": {
            k: {
                "total": v["total"],
                "wins": v["wins"],
                "win_rate": round(v["wins"] / v["total"] * 100, 1) if v["total"] else 0,
                "pnl": round(v["pnl"], 2),
            }
            for k, v in regime_breakdown.items()
        },
    }


def calc_signal_metrics(signals):
    if not signals:
        return {
            "total": 0, "traded": 0, "skipped": 0,
            "skip_reasons": {},
        }

    traded = [s for s in signals if s.get("was_traded")]
    skipped = [s for s in signals if not s.get("was_traded")]

    skip_reasons = {}
    for s in skipped:
        reason = s.get("skip_reason") or "UNKNOWN"
        if reason.startswith("PYRAMID_BLOCKED "):
            reason = "PYRAMID_BLOCKED"
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    return {
        "total": len(signals),
        "traded": len(traded),
        "skipped": len(skipped),
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda x: -x[1])),
    }


def calc_safety_metrics(skip_reasons):
    safety_keys = [
        "AI_FILTER_UNAVAILABLE", "DB_UNAVAILABLE",
        "POSITIONS_FULL", "SL_REJECT",
    ]
    result = {}
    for key in safety_keys:
        count = 0
        for reason, cnt in skip_reasons.items():
            if reason.startswith(key):
                count += cnt
        if count > 0:
            result[key] = count
    return result


# ═══════════════════════════════════════════════════════════════
# THAI EXPLANATION
# ═══════════════════════════════════════════════════════════════

def generate_thai_explanation(trade_m, signal_m, safety_m, balance, days):
    lines = []
    lines.append(f"สรุปผลการเทรดช่วง {days} วันที่ผ่านมา:")
    lines.append("")

    if trade_m["total"] == 0:
        lines.append("ไม่มีเทรดที่ปิดในช่วงนี้ — อาจเป็นเพราะบอทยังไม่เปิดเทรดใหม่ หรือเทรดที่เปิดอยู่ยังไม่ถึงเงื่อนไขปิด")
        return "\n".join(lines)

    wr = trade_m["win_rate"]
    pnl = trade_m["total_pnl"]
    pf = trade_m["profit_factor"]
    dd = trade_m["max_drawdown"]

    if pnl > 0:
        lines.append(f"ระบบทำกำไรได้ ${pnl:+.2f} จาก {trade_m['total']} เทรด (ชนะ {wr}%)")
    else:
        lines.append(f"ระบบขาดทุน ${pnl:.2f} จาก {trade_m['total']} เทรด (ชนะ {wr}%)")

    if pf > 0:
        lines.append(f"Profit Factor = {pf} {'(ดี)' if pf >= 1.5 else '(ต้องปรับปรุง)' if pf < 1.0 else '(พอใช้)'}")

    if dd < 0:
        lines.append(f"Max Drawdown ช่วงนี้ = ${dd:.2f}")

    if balance is not None:
        lines.append(f"ยอดเงินปัจจุบัน = ${balance:.2f}")

    if trade_m.get("exit_reasons"):
        lines.append("")
        lines.append("สาเหตุปิดเทรด:")
        for reason, cnt in sorted(trade_m["exit_reasons"].items(), key=lambda x: -x[1]):
            lines.append(f"  {reason}: {cnt} ครั้ง")

    if signal_m["total"] > 0:
        lines.append("")
        lines.append(f"สัญญาณทั้งหมด {signal_m['total']} รายการ — เทรดจริง {signal_m['traded']}, ข้าม {signal_m['skipped']}")

    if safety_m:
        lines.append("")
        lines.append("Safety filter ที่ทำงาน:")
        for key, cnt in safety_m.items():
            lines.append(f"  {key}: {cnt} ครั้ง")

    lines.append("")
    if wr >= 45 and pnl > 0:
        lines.append("ระบบทำงานได้ดี — ไม่จำเป็นต้องปรับค่าใดๆ ตอนนี้")
    elif wr >= 35:
        lines.append("ระบบทำงานปกติ — ควรติดตามต่อ อาจรอดูอีก 1-2 สัปดาห์ก่อนตัดสินใจ")
    else:
        lines.append("ระบบมี win rate ต่ำ — ควร review สาเหตุหลักที่ขาดทุนก่อนเปิดรอบใหม่")

    lines.append("")
    lines.append("หมายเหตุ: รายงานนี้เป็น report-only — ไม่มีการปรับ config ใดๆ ทั้งสิ้น")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# TELEGRAM MESSAGE
# ═══════════════════════════════════════════════════════════════

def format_telegram_message(report):
    meta = report["metadata"]
    trade = report["trade_summary"]
    signal = report["signal_summary"]
    safety = report["safety_summary"]
    explanation = report["thai_explanation"]

    lines = []
    lines.append(f"📊 <b>Monthly Report ({meta['days']} วัน)</b>")
    lines.append(f"📅 {meta['period_start'][:10]} → {meta['period_end'][:10]}")
    lines.append(f"🏷 report-only (ไม่ปรับค่า)")
    lines.append("")

    if trade["total"] > 0:
        lines.append(f"<b>เทรด</b>")
        lines.append(f"  ปิดแล้ว: {trade['total']} ({trade['wins']}W / {trade['losses']}L)")
        lines.append(f"  Win Rate: {trade['win_rate']}%")
        lines.append(f"  PnL: ${trade['total_pnl']:+.2f} (avg ${trade['avg_pnl']:+.2f})")
        if trade["profit_factor"] > 0:
            lines.append(f"  Profit Factor: {trade['profit_factor']}")
        if trade["max_drawdown"] < 0:
            lines.append(f"  Max DD: ${trade['max_drawdown']:.2f}")
    else:
        lines.append("ไม่มีเทรดที่ปิดในช่วงนี้")

    if signal["total"] > 0:
        lines.append("")
        lines.append(f"<b>สัญญาณ</b>")
        lines.append(f"  ทั้งหมด: {signal['total']} | เทรด: {signal['traded']} | ข้าม: {signal['skipped']}")
        if signal.get("skip_reasons"):
            top_3 = list(signal["skip_reasons"].items())[:3]
            for reason, cnt in top_3:
                lines.append(f"  · {reason}: {cnt}")

    if safety:
        lines.append("")
        lines.append(f"<b>Safety</b>")
        for key, cnt in safety.items():
            lines.append(f"  {key}: {cnt}")

    if meta.get("balance") is not None:
        lines.append("")
        lines.append(f"💰 Balance: ${meta['balance']:.2f}")

    lines.append("")
    lines.append(explanation)

    return "\n".join(lines)


def send_telegram(message):
    try:
        import notify
        if not notify.BOT_TOKEN or not notify.CHAT_ID:
            print("[MONTHLY] Telegram token/chat_id missing — skip send")
            return False
        return notify.send(message)
    except ImportError:
        print("[MONTHLY] notify module not available — skip send")
        return False


# ═══════════════════════════════════════════════════════════════
# REPORT BUILDER
# ═══════════════════════════════════════════════════════════════

def build_report(days=30, now=None):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_iso = since.isoformat()
    until_iso = now.isoformat()

    conn = _connect_db()
    if not conn:
        return {
            "metadata": {
                "generated_at": now.isoformat(),
                "days": days,
                "period_start": since_iso,
                "period_end": until_iso,
                "balance": None,
                "phase": "v0-report-only",
                "db_available": False,
            },
            "trade_summary": calc_trade_metrics([]),
            "signal_summary": calc_signal_metrics([]),
            "safety_summary": {},
            "thai_explanation": "ไม่พบไฟล์ paper_trades.db — ไม่สามารถสร้างรายงานได้",
        }

    trades = _fetch_closed_trades(conn, since_iso, until_iso)
    signals = _fetch_signal_log(conn, since_iso, until_iso)
    balance = _fetch_balance(conn)
    conn.close()

    trade_m = calc_trade_metrics(trades)
    signal_m = calc_signal_metrics(signals)
    safety_m = calc_safety_metrics(signal_m.get("skip_reasons", {}))
    explanation = generate_thai_explanation(trade_m, signal_m, safety_m, balance, days)

    return {
        "metadata": {
            "generated_at": now.isoformat(),
            "days": days,
            "period_start": since_iso,
            "period_end": until_iso,
            "balance": balance,
            "phase": "v0-report-only",
            "db_available": True,
        },
        "trade_summary": trade_m,
        "signal_summary": signal_m,
        "safety_summary": safety_m,
        "thai_explanation": explanation,
    }


def save_report(report):
    Path(REPORTS_DIR).mkdir(parents=True, exist_ok=True)
    ts = report["metadata"]["generated_at"][:10]
    days = report["metadata"]["days"]

    json_path = os.path.join(REPORTS_DIR, f"{ts}_{days}d_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[MONTHLY] JSON → {json_path}")
    return json_path


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Monthly Report v0 (report-only)")
    parser.add_argument("--days", type=int, default=30, help="Report period in days (default: 30)")
    parser.add_argument("--telegram", action="store_true", help="Send summary to Telegram")
    args = parser.parse_args()

    print(f"[MONTHLY] Building {args.days}-day report...")
    report = build_report(days=args.days)

    json_path = save_report(report)

    trade = report["trade_summary"]
    signal = report["signal_summary"]
    print(f"[MONTHLY] Trades: {trade['total']} (WR {trade['win_rate']}%, PnL ${trade['total_pnl']:+.2f})")
    print(f"[MONTHLY] Signals: {signal['total']} (traded {signal['traded']}, skipped {signal['skipped']})")

    if args.telegram:
        msg = format_telegram_message(report)
        ok = send_telegram(msg)
        print(f"[MONTHLY] Telegram: {'sent' if ok else 'failed or skipped'}")

    print(f"\n{report['thai_explanation']}")
    print(f"\n[MONTHLY] Phase 1 report-only — ไม่มีการปรับค่า config ใดๆ")


if __name__ == "__main__":
    main()
