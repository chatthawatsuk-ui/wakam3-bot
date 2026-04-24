# Session — Pyramid Merge Fix + Notify Format

## Summary
แก้ bug pyramid position ที่ INSERT row ใหม่แทน MERGE และปรับ Telegram message format ให้ชัดเจนขึ้น

## Current State
- notify.py และ paper_trade.py แก้แล้วและ push ขึ้น GitHub
- .agents/ scaffold ใหม่

## Decisions Made
- Pyramid ใช้ UPDATE existing row (weighted avg entry) — 1 slot per symbol เพื่อไม่ให้กิน 10 slots
- Signal label "H" แทน "B" เพื่อบ่งบอกว่าผ่าน Haiku filter
- Risk แสดงเป็น USD (balance × 1%) แทน percentage
- Update messages เป็น 1-line สั้น กระชับ
- Backtest ใช้ symbol set เล็กกว่า intentionally — ไม่ต้องแก้

## Blockers
- Pyramid SL/TP เมื่อ merge ยังใช้ค่า signal ใหม่ — ต้องแก้ทีหลัง

## Files Touched
- `notify.py` — signal_msg(), close_msg(), order_limit_msg(), tp1_msg(), _get_balance()
- `paper_trade.py` — open_trade() pyramid MERGE logic
- `~/.claude/settings.json` — additionalDirectories เพิ่ม ai-trade path

## Next Todo
แก้ pyramid SL/TP logic: เมื่อ merge position ควร keep original SL หรือคำนวณ weighted avg SL

## Resume Prompt
"Continue wakam3-bot. Last: fixed pyramid merge (UPDATE not INSERT) and notify format. Next: fix pyramid SL/TP calculation when merging positions."
