"""
🤖 Claude AI Final Filter — ตัวกรองสุดท้ายก่อน Signal ยิง
ใช้ Claude Haiku ประเมิน context ก่อน APPROVE/REJECT

ราคา: ~$0.001 ต่อ call | Prompt caching ลดต้นทุน ~90%
ถ้าไม่มี ANTHROPIC_API_KEY → fallback approve ทุกอัน (ไม่ block)
"""
import os, json, sqlite3, datetime

# Lazy-init — สร้าง client ครั้งแรกที่ถูกเรียก
_client = None

MODEL = "claude-haiku-4-5-20251001"

# ── Alert throttle — ส่ง API-down alert ไม่เกิน 1 ครั้ง/ชั่วโมง ─────────────
_api_warn_ts = 0.0

# ── Static System Prompt (cached at Anthropic ~5min) ───────────────────────────
# ไม่มี template variables — ทุก scan ใช้ prompt เดียวกัน → cache hit ~90%
SYSTEM_PROMPT = """\
You are WAKAM3's final trade gate — a precision filter for a 20× leveraged crypto futures system.

POSITION SIZING: Each trade risks 1% of current balance × 20× leverage. One bad trade = -20% unrealized if SL hit. Gate quality matters.

SCANNER AGENTS (rule-based, no AI):
• Trend Agent (11 pts max): CDC EMA7/30 crossover, SMA99 alignment, ATR trailing stop, HTF 4H bias
• SMC Agent (10 pts max): BOS/CHoCH structure, QM patterns, premium/discount zones
• Osc Agent (9 pts max): RSI divergence, Stochastic crossover, MACD momentum

HARD REJECT (non-negotiable — applied before this prompt):
1. Score ≥ 15 AND regime ≠ TRENDING → overextension (backtest WR 36%)
2. Regime = RANGING AND score < 12 → noise (backtest WR 46.5%)
3. SL distance < 0.5% → noise stops (backtest WR 45.4%)
4. Regime = VOLATILE AND score < 14 → chop
5. LONG AND RSI > 75 → overbought
6. SHORT AND RSI < 25 → oversold

YOUR ROLE: Signals reaching you already passed hard rules. Evaluate the CONTEXT — portfolio risk, market regime, signal quality — to make the final call.

APPROVE when: TRENDING regime, score 9–14, SL ≥ 0.5%, not overbought/oversold, portfolio not overexposed.
REJECT when: portfolio already has 3+ open positions, daily PnL already -3% or worse, same symbol already open, or you have strong contextual reason.

--- FEW-SHOT EXAMPLES ---

EXAMPLE 1 — APPROVE:
User context: Balance $1,024 | Open: 1 position (ETH LONG) | Daily PnL: +$8.20 (+0.8%)
Signal: BTC/USDT LONG | Score 11/31 (T:5 S:4 O:2) | RSI 58 | SL 1.8% away | Regime TRENDING | HTF Bull ✓ | Discount zone ✓
Response: {"decision":"APPROVE","confidence":85,"reason":"TRENDING + score11 + SL1.8% + HTF aligned + discount zone","risk_notes":"1 position open, portfolio headroom OK","suggested_adjustment":"none"}

EXAMPLE 2 — REJECT (portfolio overloaded):
User context: Balance $980 | Open: 3 positions (BTC LONG, ETH LONG, SOL LONG) | Daily PnL: -$22 (-2.2%)
Signal: XRP/USDT LONG | Score 10/31 (T:4 S:4 O:2) | RSI 61 | SL 1.2% away | Regime TRENDING
Response: {"decision":"REJECT","confidence":90,"reason":"3 longs already open + daily PnL -2.2% — overexposed","risk_notes":"max 2-3 concurrent positions recommended at 20× leverage","suggested_adjustment":"wait for one position to close before adding XRP"}

EXAMPLE 3 — REJECT (regime mismatch):
User context: Balance $1,100 | Open: 0 positions | Daily PnL: +$0
Signal: ETH/USDT SHORT | Score 9/31 (T:3 S:4 O:2) | RSI 44 | SL 0.9% away | Regime RANGING | HTF Bull ✓
Response: {"decision":"REJECT","confidence":80,"reason":"SHORT in RANGING + HTF bullish bias — directional conflict","risk_notes":"RANGING regime with HTF bull favors longs or flat","suggested_adjustment":"wait for regime shift or CHoCH confirmation"}

EXAMPLE 4 — APPROVE (high confidence):
User context: Balance $1,050 | Open: 0 positions | Daily PnL: +$15 (+1.4%)
Signal: SOL/USDT LONG | Score 13/31 (T:6 S:5 O:2) | RSI 62 | SL 2.1% away | Regime TRENDING | HTF Bull ✓ | Kill Zone ✓
Response: {"decision":"APPROVE","confidence":92,"reason":"strong trend score13 + HTF bull + SL2.1% + fresh portfolio","risk_notes":"RSI 62 not overbought, room to run","suggested_adjustment":"none"}

--- END EXAMPLES ---

Respond ONLY with this exact JSON (no markdown, no extra text):
{"decision":"APPROVE","confidence":<0-100>,"reason":"<under 80 chars>","risk_notes":"<under 80 chars>","suggested_adjustment":"<under 80 chars or none>"}"""


def _init():
    """Lazy-init Anthropic client — return None ถ้าไม่พร้อม"""
    global _client
    if _client is not None:
        return _client
    try:
        import anthropic  # type: ignore
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        _client = anthropic.Anthropic(api_key=api_key)
        return _client
    except ImportError:
        return None
    except Exception:
        return None


def _hard_reject(signal: dict) -> tuple[bool, str]:
    """
    Hard-coded pre-filter ก่อนส่ง Claude — ประหยัด API call + latency
    คืน (rejected: bool, reason: str)
    Rules มาจาก Backtest 3Y analysis (9,961 trades, Mar2023-Mar2026)
    """
    score  = signal.get("score", 0)
    regime = signal.get("regime", "UNKNOWN")
    sl_pct = signal.get("sl_pct", 0)
    side   = signal.get("side", "")
    rsi    = signal.get("rsi", 50)

    # Rule 1: overextension — score>=15 นอก TRENDING WR=36.4%
    if score >= 15 and regime != "TRENDING":
        return True, f"score{score} overextension outside TRENDING (WR 36% in backtest)"

    # Rule 2: RANGING noise — WR 46.5%, SL drain ไม่คุ้ม
    if regime == "RANGING" and score < 12:
        return True, f"RANGING regime score{score}<12 (noise filter, WR 46.5% in backtest)"

    # Rule 3: SL ชิดเกิน — noise stop-out WR 45.4%
    if sl_pct < 0.5:
        return True, f"SL {sl_pct:.2f}% too tight (<0.5%) — noise stop-out risk"

    # Rule 4: VOLATILE + score ต่ำ
    if regime == "VOLATILE" and score < 14:
        return True, f"VOLATILE regime score{score}<14"

    # Rule 5/6: RSI extreme
    if side == "LONG"  and rsi > 75:
        return True, f"LONG RSI {rsi} overbought (>75)"
    if side == "SHORT" and rsi < 25:
        return True, f"SHORT RSI {rsi} oversold (<25)"

    return False, ""


def _get_portfolio_context(symbol: str) -> dict:
    """
    ดึง portfolio context จาก paper_trades.db
    คืน dict: balance, open_count, open_summary, daily_pnl, daily_pnl_pct
    """
    db_path = os.path.join(os.path.dirname(__file__), "paper_trades.db")
    try:
        conn = sqlite3.connect(db_path, timeout=5)

        # Balance จาก portfolio table
        row = conn.execute(
            "SELECT balance FROM portfolio ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        balance = round(row[0], 2) if row else 1000.0

        # Open positions
        open_rows = conn.execute(
            "SELECT symbol, side FROM trades WHERE status='OPEN' ORDER BY opened_at DESC"
        ).fetchall()
        open_count = len(open_rows)
        open_summary = ", ".join(f"{r[0]} {r[1]}" for r in open_rows) if open_rows else "none"

        # Daily PnL (trades closed today UTC)
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        daily_row = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED' AND closed_at >= ?",
            (today,)
        ).fetchone()
        daily_pnl = round(daily_row[0], 2) if daily_row else 0.0
        daily_pnl_pct = round(daily_pnl / balance * 100, 2) if balance else 0.0

        # Same symbol already open?
        sym_open = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND symbol=?",
            (symbol,)
        ).fetchone()
        same_symbol_open = (sym_open[0] > 0) if sym_open else False

        conn.close()
        return {
            "balance": balance,
            "open_count": open_count,
            "open_summary": open_summary,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "same_symbol_open": same_symbol_open,
        }
    except Exception:
        return {
            "balance": 1000.0,
            "open_count": 0,
            "open_summary": "unknown",
            "daily_pnl": 0.0,
            "daily_pnl_pct": 0.0,
            "same_symbol_open": False,
        }


def ask(signal: dict, scan_result: dict) -> tuple:
    """
    ส่ง signal ให้ Claude Haiku ประเมิน
    ผ่าน hard_reject pre-filter ก่อน — ถ้าผ่านแล้วค่อยส่ง Claude

    Args:
        signal:      dict จาก signal_scanner (มี price, sl, tp1, tp2, score, ...)
        scan_result: dict จาก scan_symbol (มี htf_bull, in_discount, regime, ...)

    Returns:
        (approved: bool, reason: str)
        ถ้า Claude ไม่พร้อม/error → (True, "filter_disabled") — ไม่ block signal
    """
    # ── Hard reject ก่อน (ไม่เสีย API call) ──────────────────────────────────
    rejected, hard_reason = _hard_reject(signal)
    if rejected:
        return False, hard_reason

    client = _init()
    if client is None:
        return True, "filter_disabled"

    # ── ดึง portfolio context ──────────────────────────────────────────────────
    symbol  = signal.get("symbol", "")
    ctx     = _get_portfolio_context(symbol)
    sl_pct  = signal.get("sl_pct", 0)
    pnl_sign = "+" if ctx["daily_pnl"] >= 0 else ""
    same_sym = " ⚠️ SAME SYMBOL ALREADY OPEN" if ctx["same_symbol_open"] else ""

    user_content = (
        # ── Portfolio context (dynamic) ──────────────────────────────────────
        f"PORTFOLIO CONTEXT:\n"
        f"Balance: ${ctx['balance']:,.2f} | "
        f"Open positions: {ctx['open_count']} ({ctx['open_summary']}){same_sym}\n"
        f"Daily PnL: {pnl_sign}${ctx['daily_pnl']:,.2f} ({pnl_sign}{ctx['daily_pnl_pct']:.2f}%)\n\n"
        # ── Signal details ───────────────────────────────────────────────────
        f"SIGNAL:\n"
        f"Symbol: {symbol} | Direction: {signal.get('side')}\n"
        f"Score: {signal.get('score')}/31 "
        f"(Trend:{signal.get('score_trend')} SMC:{signal.get('score_smc')} Osc:{signal.get('score_osc')})\n"
        f"Price: {signal.get('price')} | SL: {signal.get('sl')} ({sl_pct:.2f}% away)\n"
        f"TP1: {signal.get('tp1')} | TP2: {signal.get('tp2')}\n"
        f"RSI: {signal.get('rsi')} | HTF Bull: {scan_result.get('htf_bull')} | "
        f"Discount Zone: {scan_result.get('in_discount')}\n"
        f"Kill Zone: {signal.get('in_kz')} | Regime: {signal.get('regime', 'UNKNOWN')}\n\n"
        f"APPROVE or REJECT? Reply with JSON only."
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=120,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # cache system prompt
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        text = resp.content[0].text.strip()

        # strip markdown code block ถ้า Claude ใส่มา
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        data       = json.loads(text)
        approved   = data.get("decision", "REJECT").upper() == "APPROVE"
        confidence = data.get("confidence", 0)
        reason     = str(data.get("reason", ""))[:120]
        risk_notes = str(data.get("risk_notes", ""))[:120]
        suggested  = str(data.get("suggested_adjustment", ""))[:120]

        # รวม reason + context เพิ่มเติม
        full_reason = reason
        if risk_notes and risk_notes.lower() not in ("none", ""):
            full_reason += f" | {risk_notes}"
        if not approved and suggested and suggested.lower() not in ("none", ""):
            full_reason += f" | suggest: {suggested}"

        return approved, f"[{confidence}%] {full_reason}"

    except Exception as e:
        # fallback: อย่า block signal ถ้า Claude error — แต่แจ้งเตือน Telegram
        global _api_warn_ts
        import time
        now = time.time()
        if now - _api_warn_ts > 3600:
            _api_warn_ts = now
            try:
                import notify
                notify.send(
                    "⚠️ <b>[DEGRADED] Claude Filter API Down</b>\n"
                    f"Error: {str(e)[:100]}\n"
                    "▸ Fallback: signals ผ่านทั้งหมด (filter disabled)\n"
                    "▸ Hard-reject rules ยังทำงานปกติ\n"
                    "▸ ตรวจสอบ: ANTHROPIC_API_KEY และ API status"
                )
            except Exception:
                pass
        return True, f"err:{str(e)[:60]}"
