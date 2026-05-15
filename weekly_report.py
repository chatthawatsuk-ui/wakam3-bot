"""
╔══════════════════════════════════════════════════════════════════╗
║                    ⚠️  PROPOSAL ONLY                            ║
║  รายงานนี้เป็นแค่ข้อเสนอจากการวิเคราะห์ข้อมูล                  ║
║  ระบบไม่มีสิทธิ์ปรับค่าใดๆ โดยอัตโนมัติ                        ║
║  ต้องได้รับการคอนเฟิมจากเจ้าของก่อนทุกครั้ง                      ║
║                                                                  ║
║  ส่ง Report ทุกวันจันทร์ → บันทึกใน proposals/                  ║
║  หลังคอนเฟิม → สร้าง proposals/confirmed_proposal.json          ║
╚══════════════════════════════════════════════════════════════════╝

Level 4 — Condition-level tracking
  คำนวณ win rate ต่อ condition (choch_bull, rsi_bull_div, ฯลฯ)
  เสนอปรับ point ต่อ condition — ต้องคอนเฟิมก่อนใช้จริง

Level 5 — Market Regime Detection
  คำนวณ win rate ต่อ specialist แยกตาม regime (TRENDING/RANGING/VOLATILE)
  เสนอปรับ weights ต่อ regime — ต้องคอนเฟิมก่อนใช้จริง
"""
import os, json, sqlite3
from datetime import datetime, timezone, timedelta

PENDING_WEIGHTS    = "pending_weights.json"
PENDING_CONDITIONS = "pending_condition_points.json"
PENDING_REGIME     = "pending_regime_weights.json"

DB_PATH       = "paper_trades.db"
PROPOSALS_DIR = "proposals"
MIN_SIGNALS   = 10    # ขั้นต่ำก่อนเสนอปรับ
WEEKLY_REPORT_WEEKDAY_UTC = 6   # Sunday (Python: Monday=0)
WEEKLY_REPORT_HOUR_UTC    = 16  # 23:00 Asia/Bangkok


def _report_days() -> int:
    try:
        return max(1, int(os.environ.get("REPORT_DAYS", "7") or 7))
    except Exception:
        return 7


def _report_mode() -> str:
    mode = (os.environ.get("REPORT_MODE") or os.environ.get("GITHUB_EVENT_NAME") or "manual").lower()
    return "schedule" if mode == "schedule" else "manual"


def _latest_weekly_anchor(now: datetime) -> datetime:
    """คืนเวลา Sunday 16:00 UTC ล่าสุด ซึ่งตรงกับ Sunday 23:00 เวลาไทย."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    days_since = (now.weekday() - WEEKLY_REPORT_WEEKDAY_UTC) % 7
    anchor = (now - timedelta(days=days_since)).replace(
        hour=WEEKLY_REPORT_HOUR_UTC,
        minute=0,
        second=0,
        microsecond=0,
    )
    if anchor > now:
        anchor -= timedelta(days=7)
    return anchor


def _report_window(days: int = 7, now=None, mode=None):
    """
    Manual report ใช้รอบสัปดาห์ปัจจุบัน และไม่ขยับ baseline
    Scheduled report ใช้รอบก่อนหน้าเต็มสัปดาห์
    """
    now = now or datetime.now(timezone.utc)
    mode = mode or _report_mode()
    anchor = _latest_weekly_anchor(now)
    if mode == "schedule":
        start = anchor - timedelta(days=7)
        end = anchor
        basis = "scheduled weekly cycle: previous Sunday 23:00 TH → this Sunday 23:00 TH"
    else:
        start = anchor
        end = now
        basis = "manual preview: current weekly cycle since Sunday 23:00 TH; manual runs do not reset baseline"

    return start, end, basis, mode


def _build_signal_trade_reviews(cutoff: datetime, until: datetime, days: int):
    """สรุปทุก signal หลัง report ล่าสุด ภายในกรอบ days วัน."""
    try:
        import generate_dashboard as GD
        live_perf = GD.load_live_performance(
            days=days,
            since_iso=cutoff.isoformat(),
            until_iso=until.isoformat(),
        )
        if not live_perf or not live_perf.get("available"):
            return {}, {}

        summary_all = live_perf.get("summary") or {}
        summary_traded = live_perf.get("summary_traded") or {}

        signal_review = {
            "total": int(live_perf.get("total_rows", 0) or 0),
            "traded": int(live_perf.get("n_traded", 0) or 0),
            "skipped": int(live_perf.get("n_skipped", 0) or 0),
            "wr": float(summary_all.get("wr", 0) or 0),
            "total_pnl": float(summary_all.get("total_pnl", 0) or 0),
            "sharpe": float(summary_all.get("sharpe", 0) or 0),
            "dd": float(summary_all.get("dd", 0) or 0),
        }
        trade_review = {
            "n": int(summary_traded.get("n", 0) or 0),
            "wr": float(summary_traded.get("wr", 0) or 0),
            "total_pnl": float(summary_traded.get("total_pnl", 0) or 0),
            "sharpe": float(summary_traded.get("sharpe", 0) or 0),
            "dd": float(summary_traded.get("dd", 0) or 0),
        }
        return signal_review, trade_review
    except Exception as e:
        print(f"  [WARN] signal/trade reviews: {e}")
        return {}, {}

# ── Point ปัจจุบัน (hardcoded ใน agents) ──────────────────────────────────────
DEFAULT_POINTS = {
    # 🎯 Trend Agent — LONG
    "cdc_bull":        2,
    "cross_up":        2,
    "touch_bull":      1,
    "above_sma50":     1,
    "above_sma200":    2,
    "sma50_gt_200":    1,
    "trail_slow_bull": 2,
    # 🏦 SMC Agent — LONG
    "bos_bull":        2,
    "choch_bull":      3,
    "qm_bull":         2,
    "in_discount":     2,
    "in_eq":           1,
    # 📈 Osc Agent — LONG
    "rsi_bull_div":    3,
    "rsi_os":          2,
    "st_up":           2,
    "macd_up":         2,
    # 🎯 Trend Agent — SHORT
    "cross_dn":        2,
    "touch_bear":      1,
    "above_sma50_n":   1,   # not above_sma50
    "above_sma200_n":  2,
    "sma50_gt_200_n":  1,
    "trail_slow_n":    2,
    # 🏦 SMC Agent — SHORT
    "bos_bear":        2,
    "choch_bear":      3,
    "qm_bear":         2,
    "in_premium":      2,
    # 📈 Osc Agent — SHORT
    "rsi_bear_div":    3,
    "rsi_ob":          2,
    "st_dn":           2,
    "macd_dn":         2,
}

CONDITION_COLS = [
    "cdc_bull","cross_up","cross_dn","touch_bull","touch_bear",
    "above_sma50","above_sma200","sma50_gt_200","trail_slow_bull",
    "bos_bull","bos_bear","choch_bull","choch_bear",
    "qm_bull","qm_bear","in_discount","in_premium","in_eq",
    "rsi_bull_div","rsi_bear_div","rsi_os","rsi_ob",
    "st_up","st_dn","macd_up","macd_dn",
]


# ══════════════════════════════════════════════════════════════
# LEVEL 4 — CONDITION WIN RATES
# ══════════════════════════════════════════════════════════════
def calc_condition_winrates():
    """
    JOIN condition_snapshots × closed trades
    → win rate ต่อ condition

    ⚠️  ผลที่ได้เป็นแค่ข้อมูลสำหรับ Report เท่านั้น
    """
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()

        # ตรวจว่าตารางมีอยู่ไหม
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='condition_snapshots'")
        if not cur.fetchone():
            con.close()
            return {}

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        if not cur.fetchone():
            con.close()
            return {}

        # JOIN: จับคู่ signal_ts กับ entry_time ใน trades (±2 นาที)
        query = """
            SELECT cs.*, t.pnl_usd AS pnl
            FROM condition_snapshots cs
            JOIN trades t
              ON cs.symbol = t.symbol
             AND cs.side   = t.side
             AND ABS(
                 (julianday(cs.signal_ts) - julianday(t.opened_at)) * 86400
             ) < 120
            WHERE t.status = 'CLOSED'
              AND t.pnl_usd IS NOT NULL
        """
        rows = cur.execute(query).fetchall()
        cols = [d[0] for d in cur.description]
        con.close()

        if len(rows) < MIN_SIGNALS:
            return {"error": f"ข้อมูลไม่พอ — มี {len(rows)} signals ที่ปิดแล้ว (ต้องการ {MIN_SIGNALS}+)"}

        results = {}
        for cond in CONDITION_COLS:
            if cond not in cols:
                continue
            ci    = cols.index(cond)
            pnl_i = cols.index("pnl")
            active_rows = [r for r in rows if r[ci] == 1]
            if len(active_rows) < 5:
                continue
            wins = sum(1 for r in active_rows if r[pnl_i] > 0)
            results[cond] = {
                "win_rate":   round(wins / len(active_rows), 3),
                "count":      len(active_rows),
                "wins":       wins,
                "avg_pnl":    round(sum(r[pnl_i] for r in active_rows) / len(active_rows), 4),
                "current_pt": DEFAULT_POINTS.get(cond, 1),
            }
        return results

    except Exception as e:
        return {"error": str(e)}


def propose_condition_points(wr_data):
    """
    ⚠️  PROPOSAL ONLY — ไม่ได้แก้อะไรจริง
    กฎการเสนอ:
      win_rate >= 0.65 AND count >= 10 → เสนอ +1 point (max current+2)
      win_rate <= 0.35 AND count >= 10 → เสนอ -1 point (min 1)
      อื่นๆ → คง current
    """
    proposals = {}
    for cond, d in wr_data.items():
        if isinstance(d, dict) and "win_rate" in d:
            cur_pt = d["current_pt"]
            if d["count"] < MIN_SIGNALS:
                proposals[cond] = {"current": cur_pt, "proposed": cur_pt, "reason": "ข้อมูลไม่พอ"}
            elif d["win_rate"] >= 0.65:
                proposals[cond] = {"current": cur_pt, "proposed": min(cur_pt + 1, cur_pt + 2),
                                   "reason": f"win_rate สูง ({d['win_rate']:.1%})"}
            elif d["win_rate"] <= 0.35:
                proposals[cond] = {"current": cur_pt, "proposed": max(cur_pt - 1, 1),
                                   "reason": f"win_rate ต่ำ ({d['win_rate']:.1%})"}
            else:
                proposals[cond] = {"current": cur_pt, "proposed": cur_pt,
                                   "reason": f"win_rate ปกติ ({d['win_rate']:.1%})"}
    return proposals


# ══════════════════════════════════════════════════════════════
# LEVEL 5 — REGIME WIN RATES
# ══════════════════════════════════════════════════════════════
def calc_regime_performance():
    """
    WIN RATE ต่อ specialist แยกตาม regime
    → เพื่อเสนอ weight adjustment ต่อ regime

    ⚠️  ผลที่ได้เป็นแค่ข้อมูลสำหรับ Report เท่านั้น
    """
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='condition_snapshots'")
        if not cur.fetchone():
            con.close()
            return {}

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        if not cur.fetchone():
            con.close()
            return {}

        query = """
            SELECT cs.regime, t.pnl_usd AS pnl,
                   t.score_trend, t.score_smc, t.score_osc
            FROM condition_snapshots cs
            JOIN trades t
              ON cs.symbol = t.symbol
             AND cs.side   = t.side
             AND ABS(
                 (julianday(cs.signal_ts) - julianday(t.opened_at)) * 86400
             ) < 120
            WHERE t.status = 'CLOSED'
              AND t.pnl_usd IS NOT NULL
        """
        rows = cur.execute(query).fetchall()
        con.close()

        if len(rows) < MIN_SIGNALS:
            return {"error": f"ข้อมูลไม่พอ — มี {len(rows)} trades (ต้องการ {MIN_SIGNALS}+)"}

        regimes = {}
        for regime, pnl, s_trend, s_smc, s_osc in rows:
            if not regime:
                regime = "UNKNOWN"
            if regime not in regimes:
                regimes[regime] = []
            regimes[regime].append((pnl, s_trend or 0, s_smc or 0, s_osc or 0))

        results = {}
        for regime, trades in regimes.items():
            if len(trades) < 5:
                continue
            wins   = sum(1 for t in trades if t[0] > 0)
            wr     = wins / len(trades)
            avg_trend = sum(t[1] for t in trades) / len(trades)
            avg_smc   = sum(t[2] for t in trades) / len(trades)
            avg_osc   = sum(t[3] for t in trades) / len(trades)
            results[regime] = {
                "count":     len(trades),
                "win_rate":  round(wr, 3),
                "avg_score_trend": round(avg_trend, 2),
                "avg_score_smc":   round(avg_smc,   2),
                "avg_score_osc":   round(avg_osc,   2),
            }
        return results

    except Exception as e:
        return {"error": str(e)}


def propose_regime_weights(regime_data):
    """
    ⚠️  PROPOSAL ONLY — ไม่ได้แก้อะไรจริง
    เสนอ weight ต่อ regime โดย normalize avg_score → สัดส่วน
    """
    proposals = {}
    for regime, d in regime_data.items():
        if "error" in d or d.get("count", 0) < 5:
            continue
        t = d["avg_score_trend"] / 13   # MAX 13 (Phase 1: ADX+BB)
        s = d["avg_score_smc"]   / 10
        o = d["avg_score_osc"]   / 11   # MAX 11 (Phase 3: OBV)
        total = t + s + o
        if total == 0:
            proposals[regime] = {"W_TREND": 1/3, "W_SMC": 1/3, "W_OSC": 1/3,
                                 "reason": "ข้อมูลไม่พอ"}
        else:
            # blend 50% กับ equal weights เพื่อไม่ให้ extreme เกิน
            wt = round((t/total * 0.5) + (1/3 * 0.5), 3)
            ws = round((s/total * 0.5) + (1/3 * 0.5), 3)
            wo = round(1.0 - wt - ws, 3)
            proposals[regime] = {
                "W_TREND": wt, "W_SMC": ws, "W_OSC": wo,
                "win_rate": d["win_rate"],
                "count":    d["count"],
                "reason":   f"จาก avg scores: Trend={d['avg_score_trend']:.1f}/13 "
                            f"SMC={d['avg_score_smc']:.1f}/10 "
                            f"Osc={d['avg_score_osc']:.1f}/11",
            }
    return proposals


# ══════════════════════════════════════════════════════════════
# LEVEL 6 — CLAUDE HAIKU WEIGHT PROPOSAL
# ══════════════════════════════════════════════════════════════
def _claude_weight_proposal(specialist_wr, regime_data, cond_wr, backtest_summary=None, tf_data=None, save_pending=True):
    """
    ใช้ Claude Haiku วิเคราะห์ข้อมูลทั้งหมด → เสนอ weights + วิเคราะห์เชิงลึก
    บันทึก pending_weights.json (รอ /approve_weights จาก Telegram)
    คืน dict หรือ None ถ้าเกิดข้อผิดพลาด
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [SKIP] ANTHROPIC_API_KEY ไม่พบ — ข้าม Claude weight proposal")
        return None

    try:
        import anthropic

        # รวบรวมข้อมูลให้ Claude วิเคราะห์
        context_lines = [
            "# AI Trade System — Weekly Weight Analysis",
            f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            "",
            "## Specialist Win Rates (Paper Trades)",
        ]

        total_closed = specialist_wr.get("total_closed", 0)
        context_lines.append(f"Total closed trades: {total_closed}")
        for key in ["trend", "smc", "osc"]:
            d = specialist_wr.get(key, {})
            if isinstance(d, dict) and "winrate" in d:
                context_lines.append(
                    f"- {key.upper()}: WR={d['winrate']}% over {d.get('trades', 0)} trades"
                )

        if backtest_summary:
            context_lines += [
                "",
                "## Backtest Summary",
                f"- Trades: {backtest_summary.get('n', 0)}",
                f"- Win Rate: {backtest_summary.get('wr', 0)}%",
                f"- Total PnL: ${backtest_summary.get('total_pnl', 0)}",
                f"- Sharpe: {backtest_summary.get('sharpe', 0)}",
                f"- Max DD: {backtest_summary.get('dd', 0)}%",
            ]

        if regime_data and "error" not in regime_data:
            context_lines += ["", "## Market Regime Performance"]
            for regime, d in regime_data.items():
                context_lines.append(
                    f"- {regime}: {d.get('count', 0)} trades, "
                    f"WR={d.get('win_rate', 0):.1%}, "
                    f"avg Trend={d.get('avg_score_trend', 0):.1f}/13 "
                    f"SMC={d.get('avg_score_smc', 0):.1f}/10 "
                    f"Osc={d.get('avg_score_osc', 0):.1f}/11"
                )

        if cond_wr and "error" not in cond_wr:
            context_lines += ["", "## Condition Win Rates (Top 5 / Bottom 5)"]
            sorted_conds = sorted(
                [(k, v) for k, v in cond_wr.items() if isinstance(v, dict) and "win_rate" in v],
                key=lambda x: x[1]["win_rate"], reverse=True
            )
            for cond, d in sorted_conds[:5]:
                context_lines.append(f"- TOP {cond}: WR={d['win_rate']:.1%} ({d['count']} trades, avg_pnl=${d['avg_pnl']:.2f})")
            for cond, d in sorted_conds[-5:]:
                context_lines.append(f"- BOT {cond}: WR={d['win_rate']:.1%} ({d['count']} trades, avg_pnl=${d['avg_pnl']:.2f})")

        if tf_data:
            context_lines += ["", "## Performance by Timeframe"]
            for tf, d in sorted(tf_data.items(), key=lambda x: x[1].get("wr", 0), reverse=True):
                context_lines.append(
                    f"- {tf}: {d['n']} trades, WR={d['wr']:.1f}%, "
                    f"avg_pnl=${d['avg_pnl']:.2f}, total_pnl=${d['total_pnl']:.2f}"
                )

        context = "\n".join(context_lines)

        prompt = f"""You are an expert quant analyst for a crypto trading system.
The system uses 5 specialist agents to score trading signals:
- 🎯 Trend Agent (CDC EMA7/30 + SMA99 + ATR Trail + ADX + BB, max 13 pts) ← weighted
- 🏦 SMC Agent (Smart Money Concepts, max 10 pts) ← weighted
- 📈 Oscillator Agent (RSI + Stochastic + MACD + OBV, max 11 pts) ← weighted
- 💧 Liquidity Agent (Sweep + Equal Highs/Lows, max 8 pts bonus) ← direct add
- 💰 Funding Agent (OKX Funding Rate bias, max 6 pts bonus) ← direct add

Core 3 agents are normalized and weighted → /31, then Liq(+8) + Fund(+6) added directly.
Total MAX score = 45 pts. Weight proposal only covers core 3 agents (Trend/SMC/Osc).

{context}

Analyze the data above and respond ONLY with valid JSON in this exact format:
{{
  "trend": 0.xxx,
  "smc": 0.xxx,
  "osc": 0.xxx,
  "confidence": "LOW|MEDIUM|HIGH",
  "reasoning": "เหตุผลการเลือก weight เป็นภาษาไทย 2-3 ประโยค",
  "best_tf": "TF ที่ WR สูงสุด เช่น 1H",
  "best_tf_reason": "เหตุผลสั้นๆ เป็นภาษาไทย 1 ประโยค",
  "top_condition": "condition ที่ดีที่สุด",
  "weak_condition": "condition ที่แย่ที่สุด",
  "regime_insight": "วิเคราะห์ regime ที่ระบบทำได้ดีที่สุด เป็นภาษาไทย 1 ประโยค",
  "weekly_verdict": "สรุปสัปดาห์นี้เป็นภาษาไทย 1 ประโยค"
}}

Rules for weights:
- Each weight between 0.20 and 0.60
- Must sum to exactly 1.0
- If total trades < 10: use equal weights (0.333) and set confidence=LOW"""

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        # ดึง JSON จาก response (อาจมี markdown code block)
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            print(f"  [WARN] Claude response ไม่ใช่ JSON: {raw[:100]}")
            return None

        proposal = json.loads(json_match.group())

        # validate
        t = float(proposal.get("trend", 0))
        s = float(proposal.get("smc", 0))
        o = float(proposal.get("osc", 0))
        total = t + s + o

        if abs(total - 1.0) > 0.01:
            print(f"  [WARN] Claude weights ไม่รวมเป็น 1.0: {total:.3f} — normalize")
            t, s, o = t/total, s/total, o/total

        # clamp ระหว่าง 0.20–0.60
        t = max(0.20, min(0.60, t))
        s = max(0.20, min(0.60, s))
        o = max(0.20, min(0.60, o))
        total2 = t + s + o
        t, s, o = round(t/total2, 3), round(s/total2, 3), round(o/total2, 3)
        # ปรับให้ sum = 1.000 พอดี
        diff = round(1.0 - (t + s + o), 3)
        t = round(t + diff, 3)

        result = {
            "trend":           t,
            "smc":             s,
            "osc":             o,
            "confidence":      proposal.get("confidence", "MEDIUM"),
            "reasoning":       proposal.get("reasoning", ""),
            "best_tf":         proposal.get("best_tf", ""),
            "best_tf_reason":  proposal.get("best_tf_reason", ""),
            "top_condition":   proposal.get("top_condition", ""),
            "weak_condition":  proposal.get("weak_condition", ""),
            "regime_insight":  proposal.get("regime_insight", ""),
            "weekly_verdict":  proposal.get("weekly_verdict", ""),
            "generated":       datetime.now(timezone.utc).isoformat(),
            "reason":          f"Claude Haiku ({proposal.get('confidence','?')}): {proposal.get('reasoning', '')}",
        }

        if save_pending:
            with open(PENDING_WEIGHTS, "w") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"  🤖 Claude Haiku เสนอ Weights — "
              f"Trend:{t} SMC:{s} Osc:{o} [{result['confidence']}]")
        print(f"     เหตุผล: {result['reasoning']}")
        if save_pending:
            print(f"  💾 บันทึก → {PENDING_WEIGHTS} (รอ /approve_weights ทาง Telegram)")
        else:
            print("  👁 Weekly monitor only — ไม่สร้าง pending_weights.json")

        return result

    except Exception as e:
        import traceback
        print(f"  [ERROR] _claude_weight_proposal failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None


# ══════════════════════════════════════════════════════════════
# PENDING HELPERS — L4 + L5
# ══════════════════════════════════════════════════════════════
def _save_pending_conditions(cond_prop, enabled=True):
    """บันทึก pending_condition_points.json จาก L4 proposals"""
    changes = {k: v for k, v in cond_prop.items()
               if isinstance(v, dict) and v.get("proposed") != v.get("current")}
    if not changes:
        print("      (ไม่มี condition เปลี่ยนแปลง — ข้าม pending)")
        return
    if not enabled:
        print(f"  👁 Weekly WATCHLIST only — พบ {len(changes)} condition(s), ไม่สร้าง {PENDING_CONDITIONS}")
        return
    pending = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "status":    "PENDING_CONFIRMATION",
        "changes": {
            k: {
                "current":  v["current"],
                "proposed": v["proposed"],
                "reason":   v.get("reason", ""),
            }
            for k, v in changes.items()
        },
    }
    with open(PENDING_CONDITIONS, "w") as f:
        json.dump(pending, f, indent=2, ensure_ascii=False)
    print(f"  💾 บันทึก → {PENDING_CONDITIONS} (รอ /approve_conditions ทาง Telegram)")


def _save_pending_regime(regime_prop, enabled=True):
    """บันทึก pending_regime_weights.json จาก L5 proposals"""
    if not regime_prop:
        return
    if not enabled:
        print(f"  👁 Weekly WATCHLIST only — ไม่สร้าง {PENDING_REGIME}")
        return
    pending = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "status":    "PENDING_CONFIRMATION",
        "weights": {
            regime: {
                "trend": p["W_TREND"],
                "smc":   p["W_SMC"],
                "osc":   p["W_OSC"],
            }
            for regime, p in regime_prop.items()
        },
    }
    with open(PENDING_REGIME, "w") as f:
        json.dump(pending, f, indent=2, ensure_ascii=False)
    print(f"  💾 บันทึก → {PENDING_REGIME} (รอ /approve_regime ทาง Telegram)")


def _clear_weekly_pending_files():
    for path in [PENDING_WEIGHTS, PENDING_CONDITIONS, PENDING_REGIME]:
        if os.path.exists(path):
            os.remove(path)
            print(f"  🧹 ลบ weekly pending เดิม → {path}")


# ══════════════════════════════════════════════════════════════
# GENERATE WEEKLY REPORT
# ══════════════════════════════════════════════════════════════
def generate_weekly_report():
    """
    ╔══════════════════════════════════════════════════════════════╗
    ║  ⚠️  PROPOSAL ONLY — ห้ามแก้ไขค่าใดๆ โดยอัตโนมัติ         ║
    ║  รายงานนี้ต้องได้รับการคอนเฟิมจากเจ้าของก่อนทุกครั้ง        ║
    ╚══════════════════════════════════════════════════════════════╝
    """
    os.makedirs(PROPOSALS_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_days = _report_days()
    report_cutoff, report_until, report_basis, report_mode = _report_window(report_days)
    approval_enabled = os.environ.get("WEEKLY_APPROVAL_ENABLED", "").lower() in ("1", "true", "yes")
    if not approval_enabled:
        _clear_weekly_pending_files()

    print("=" * 60)
    print("📋 WEEKLY REPORT — PROPOSAL ONLY")
    print(f"   วันที่: {today}")
    print(f"   โหมด: {report_mode}")
    print(f"   ช่วงข้อมูล: {report_cutoff.strftime('%Y-%m-%d %H:%M UTC')} → {report_until.strftime('%Y-%m-%d %H:%M UTC')}")
    print("👁 Weekly report เป็น monitor/watchlist เท่านั้น — ไม่สร้าง pending approval")
    print("=" * 60)

    # ── Level 4: Condition win rates ──────────────────────────
    print("\n🔬 Level 4 — Condition Win Rates")
    cond_wr   = calc_condition_winrates()
    cond_prop = {}

    if "error" in cond_wr:
        print(f"   ⚠️  {cond_wr['error']}")
    else:
        cond_prop = propose_condition_points(cond_wr)
        changes = {k: v for k, v in cond_prop.items() if v["proposed"] != v["current"]}
        print(f"   ✅ วิเคราะห์ {len(cond_wr)} conditions")
        print(f"   👁 WATCHLIST {len(changes)} conditions:")
        for cond, v in changes.items():
            arrow = "↑" if v["proposed"] > v["current"] else "↓"
            print(f"      {arrow} {cond}: {v['current']} → {v['proposed']} ({v['reason']})")
        if not changes:
            print("      ✅ ไม่มีการเปลี่ยนแปลงที่แนะนำในสัปดาห์นี้")
    _save_pending_conditions(cond_prop, enabled=approval_enabled)

    # ── Level 5: Regime performance ───────────────────────────
    print("\n🌐 Level 5 — Market Regime Performance")
    regime_data = calc_regime_performance()
    regime_prop = {}

    if "error" in regime_data:
        print(f"   ⚠️  {regime_data['error']}")
    else:
        regime_prop = propose_regime_weights(regime_data)
        for regime, d in regime_data.items():
            print(f"   {regime}: {d['count']} trades, win_rate={d['win_rate']:.1%}")
        print("\n   👁 WATCHLIST Weights ต่อ Regime:")
        for regime, p in regime_prop.items():
            print(f"      {regime}: Trend={p['W_TREND']:.3f} SMC={p['W_SMC']:.3f} Osc={p['W_OSC']:.3f}")
            print(f"        → {p['reason']}")
    _save_pending_regime(regime_prop, enabled=approval_enabled)

    # ── Level 6: Claude Haiku Weight Proposal ────────────────
    print("\n🤖 Level 6 — Claude Haiku Weight Proposal")
    specialist_wr_raw = {}
    try:
        import sqlite3 as _sq
        con = _sq.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        if cur.fetchone():
            rows = cur.execute(
                "SELECT score_trend, score_smc, score_osc, outcome "
                "FROM trades WHERE status='CLOSED' AND outcome IS NOT NULL"
            ).fetchall()
            con.close()
            total_c = len(rows)
            for key_i, key in enumerate(["trend", "smc", "osc"]):
                trades_key = [(r[key_i], r[3]=="WIN") for r in rows]
                wins  = sum(1 for _, w in trades_key if w)
                w_pct = round(wins / total_c * 100, 1) if total_c > 0 else 50.0
                specialist_wr_raw[key] = {"winrate": w_pct, "trades": total_c}
            specialist_wr_raw["total_closed"] = total_c
        else:
            con.close()
    except Exception:
        pass

    # live trade summary — ดึงจาก paper_trades.db หลัง report ล่าสุด ภายในกรอบ report_days
    bt_summary = None
    try:
        import pandas as pd
        if os.path.exists(DB_PATH):
            con_bt = sqlite3.connect(DB_PATH)
            rows_bt = con_bt.execute(
                "SELECT pnl_usd, outcome FROM trades "
                "WHERE status='CLOSED' AND outcome IS NOT NULL "
                "AND closed_at >= ? AND closed_at < ?",
                (report_cutoff.isoformat(), report_until.isoformat())
            ).fetchall()
            con_bt.close()
            if rows_bt:
                import numpy as np
                pnls    = [r[0] or 0.0 for r in rows_bt]
                outcomes = [r[1] for r in rows_bt]
                n_bt    = len(pnls)
                wins_bt = sum(1 for o in outcomes if o == "WIN")
                pnl_arr = pd.Series(pnls, dtype=float)
                pnl_bt  = pnl_arr.sum()
                avg_bt  = pnl_arr.mean()
                std_bt  = pnl_arr.std()
                sharpe  = (avg_bt / std_bt) * (252 ** 0.5) if std_bt and std_bt > 0 else 0
                eq  = pnl_arr.cumsum()
                pk  = eq.cummax()
                cap = max(float(pk.max()), n_bt * 10.0)
                dd_val = round(float(((eq - pk) / cap * 100).min()), 1)
                bt_summary = {
                    "n":         n_bt,
                    "wr":        round(wins_bt / n_bt * 100, 1) if n_bt > 0 else 0,
                    "total_pnl": round(float(pnl_bt), 2),
                    "sharpe":    round(float(sharpe), 2),
                    "dd":        dd_val,
                }
    except Exception:
        pass

    signal_review, trade_review = _build_signal_trade_reviews(report_cutoff, report_until, report_days)
    if signal_review:
        print(f"\n📡 Signal Review (since {report_cutoff.strftime('%Y-%m-%d %H:%M UTC')})")
        print(f"   Signals={signal_review['total']} Traded={signal_review['traded']} Skipped={signal_review['skipped']} WR={signal_review['wr']}%")
    if trade_review:
        print(f"\n💼 Trade Review (since {report_cutoff.strftime('%Y-%m-%d %H:%M UTC')})")
        print(f"   Trades={trade_review['n']} WR={trade_review['wr']}% PnL=${trade_review['total_pnl']}")

    claude_prop = _claude_weight_proposal(
        specialist_wr_raw,
        regime_data if "error" not in regime_data else {},
        cond_wr if "error" not in cond_wr else {},
        bt_summary,
        None,
        save_pending=approval_enabled,
    )

    # ── บันทึก Proposal ───────────────────────────────────────
    proposal = {
        "generated":        today,
        "status":           "MONITOR_ONLY",
        "warning":          "👁 WEEKLY WATCHLIST ONLY — ไม่สร้าง pending approval; ใช้ Monthly framework สำหรับ tuning",
        "approval_enabled":  approval_enabled,
        "period":           {
            "days": report_days,
            "cutoff": report_cutoff.isoformat(),
            "until": report_until.isoformat(),
            "mode": report_mode,
            "basis": report_basis,
        },
        "signal_review":    signal_review,
        "trade_review":     trade_review,
        "level4": {
            "condition_winrates": cond_wr if "error" not in cond_wr else {},
            "proposals":          cond_prop,
        },
        "level5": {
            "regime_performance": regime_data if "error" not in regime_data else {},
            "proposals":          regime_prop,
        },
        "level6": {
            "claude_weight_proposal": claude_prop,
        },
    }

    path = os.path.join(PROPOSALS_DIR, f"{today}_proposal.json")
    with open(path, "w") as f:
        json.dump(proposal, f, indent=2, ensure_ascii=False)

    # อัพเดท latest_proposal.json เสมอ
    latest = os.path.join(PROPOSALS_DIR, "latest_proposal.json")
    with open(latest, "w") as f:
        json.dump(proposal, f, indent=2, ensure_ascii=False)

    print(f"\n💾 บันทึก → {path}")
    print(f"💾 บันทึก → {latest}")
    print("\n" + "=" * 60)
    print("👁 Weekly monitor only: ไม่ต้อง approve จากรายงาน 7 วัน")
    print("   L4 Condition Points → WATCHLIST เท่านั้น")
    print("   L5 Regime Weights   → WATCHLIST เท่านั้น")
    print("   L6 Weights          → ไม่สร้าง pending จาก weekly")
    print("=" * 60)

    # ── ส่ง Telegram ──────────────────────────────────────────────────────────
    _send_telegram(proposal, bt_summary)

    return proposal


def _send_telegram(proposal, backtest_summary=None):
    """ส่ง Weekly Report สรุปไป Telegram — ส่งได้แค่ครั้งเดียวต่อสัปดาห์"""
    # ── Dedup: ตรวจว่าส่งไปแล้วในรอบ 6 วันที่ผ่านมาหรือยัง ──────────────────
    mode = (proposal.get("period") or {}).get("mode") or _report_mode()
    SENT_FLAG = os.path.join(
        PROPOSALS_DIR,
        "last_weekly_scheduled_sent.json" if mode == "schedule" else "last_manual_report_sent.json",
    )
    force = os.environ.get("FORCE_REPORT", "").lower() in ("1", "true", "yes")
    try:
        if not force and os.path.exists(SENT_FLAG):
            with open(SENT_FLAG) as f:
                flag = json.load(f)
            last_sent = datetime.fromisoformat(flag.get("sent_at", "2000-01-01T00:00:00+00:00"))
            hours_ago = (datetime.now(timezone.utc) - last_sent).total_seconds() / 3600
            if hours_ago < 144:   # 144 ชั่วโมง = 6 วัน
                print(f"  [SKIP] Weekly Report ส่งไปแล้ว {hours_ago:.0f}h ที่แล้ว (< 144h) — ข้ามการส่ง Telegram")
                return
    except Exception:
        pass

    try:
        import notify as N
        # fallback: โหลด paper trades summary ถ้าไม่ได้ส่งมา
        if backtest_summary is None and os.path.exists(DB_PATH):
            try:
                import pandas as pd
                from datetime import timedelta
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                con_fb = sqlite3.connect(DB_PATH)
                rows_fb = con_fb.execute(
                    "SELECT pnl_usd, outcome FROM trades "
                    "WHERE status='CLOSED' AND outcome IS NOT NULL AND closed_at >= ?",
                    (cutoff,)
                ).fetchall()
                con_fb.close()
                if rows_fb:
                    pnls   = pd.Series([r[0] or 0.0 for r in rows_fb], dtype=float)
                    wins   = sum(1 for r in rows_fb if r[1] == "WIN")
                    total  = len(rows_fb)
                    avg    = pnls.mean(); std = pnls.std()
                    eq     = pnls.cumsum(); pk = eq.cummax()
                    cap    = max(float(pk.max()), total * 10.0)
                    backtest_summary = {
                        "n":         total,
                        "wr":        round(wins / total * 100, 1) if total > 0 else 0,
                        "total_pnl": round(float(pnls.sum()), 2),
                        "sharpe":    round((avg / std) * (252 ** 0.5), 2) if std and std > 0 else 0,
                        "dd":        round(float(((eq - pk) / cap * 100).min()), 1),
                    }
            except Exception as e:
                print(f"  [WARN] paper summary fallback: {e}")

        msg = N.weekly_report_msg(proposal, backtest_summary)
        ok  = N.send(msg)
        print(f"  Telegram Weekly Report: {'✅ ส่งแล้ว' if ok else '❌ ส่งไม่ได้'}")

        # ส่ง weight proposal แยกต่างหาก (ถ้ามี)
        wp_msg = N.weight_proposal_msg(proposal)
        if wp_msg:
            ok2 = N.send(wp_msg)
            print(f"  Telegram Weight Proposal: {'✅ ส่งแล้ว' if ok2 else '❌ ส่งไม่ได้'}")

        # Manual report เป็น preview/test: บันทึกแยก ไม่เอาไปเป็น baseline ของรอบ weekly จริง
        if ok:
            with open(SENT_FLAG, "w") as f:
                json.dump({
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "mode": mode,
                    "period": proposal.get("period", {}),
                }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [WARN] Telegram send: {e}")


if __name__ == "__main__":
    generate_weekly_report()
