"""
🤖 Claude AI Final Filter — ตัวกรองสุดท้ายก่อน Signal ยิง
ใช้ Claude Haiku ประเมิน context ก่อน APPROVE/REJECT

ราคา: ~$0.001 ต่อ call | Prompt caching ลดต้นทุน ~90%
ถ้าไม่มี ANTHROPIC_API_KEY → fallback approve ทุกอัน (ไม่ block)
"""
import os, json

# Lazy-init — สร้าง client ครั้งแรกที่ถูกเรียก
_client = None

MODEL = "claude-haiku-4-5-20251001"

# ── System prompt (cached) ──────────────────────────────────────────────────────
# prompt นี้จะถูก cache ไว้ที่ Anthropic ประมาณ 5 นาที
# ทุก call ในรอบ scan เดียวกัน (~45 coins) ประหยัดค่า input token ~90%
SYSTEM_PROMPT = """\
You are a crypto trading signal evaluator. Your job: given a rule-based scanner \
signal that already scored ≥9/31, decide APPROVE or REJECT.

Scanner uses 3 specialist agents:
- Trend Agent (11pts max): CDC EMA7/30, SMA99 alignment (price+EMA7 above SMA99), ATR trail, HTF 4H bias
- SMC Agent (10pts max): BOS/CHoCH, QM patterns, discount/premium zones
- Osc Agent (9pts max): RSI divergence, Stoch crossover, MACD

Note: Kill Zone is context only — NOT a score bonus (removed per backtest 3Y analysis).

REJECT when ANY of these apply (hard rules, non-negotiable):
1. Score >= 15 AND regime != TRENDING  (overextension — backtest 3Y WR 36% at score 15+)
2. Regime == RANGING AND score < 12    (RANGING noise — WR 46.5% not worth the SL risk)
3. SL distance < 0.5%                  (too tight — noise stops, backtest WR 45.4%)
4. VOLATILE regime AND score < 14      (volatility chop)
5. LONG signal AND RSI > 75            (overbought entry)
6. SHORT signal AND RSI < 25           (oversold entry)

APPROVE otherwise — you are a precision gate, not a blocker. When in doubt, APPROVE.
TRENDING regime with score 9-14 and SL >= 0.5% should almost always be APPROVED.

Respond ONLY with this exact JSON (no markdown, no extra text):
{"decision":"APPROVE","reason":"brief reason under 80 chars"}"""


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

    sl_pct = signal.get("sl_pct", 0)
    user_content = (
        f"Symbol: {signal.get('symbol')} | Direction: {signal.get('side')}\n"
        f"Score: {signal.get('score')}/31 "
        f"(Trend:{signal.get('score_trend')} SMC:{signal.get('score_smc')} Osc:{signal.get('score_osc')})\n"
        f"Price: {signal.get('price')} | SL: {signal.get('sl')} ({sl_pct}% away)\n"
        f"TP1: {signal.get('tp1')} | TP2: {signal.get('tp2')}\n"
        f"RSI: {signal.get('rsi')} | HTF Bull: {scan_result.get('htf_bull')}\n"
        f"Kill Zone: {signal.get('in_kz')} | Discount Zone: {scan_result.get('in_discount')}\n"
        f"Regime: {signal.get('regime', 'UNKNOWN')}\n\n"
        f"APPROVE or REJECT? Reply with JSON only."
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=80,
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

        data     = json.loads(text)
        approved = data.get("decision", "REJECT").upper() == "APPROVE"
        reason   = str(data.get("reason", ""))[:120]
        return approved, reason

    except Exception as e:
        # fallback: อย่า block signal ถ้า Claude error
        return True, f"err:{str(e)[:60]}"
