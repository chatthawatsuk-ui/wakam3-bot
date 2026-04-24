---
updated_at: "2026-04-23T00:00:00Z"
status: "active"
current_focus: "pyramid merge logic + notify format"
branch: "main"
project_type: "crypto-paper-trading-bot"
---

# Active Context

## Objective
ปรับปรุง paper trading bot ให้ pyramid position ทำงานถูกต้อง (MERGE ไม่ INSERT)
และ Telegram notification แสดงข้อมูลที่ชัดเจนขึ้น

## Current State
- `notify.py` — ปรับ format แล้ว: Signal H label, risk แสดงเป็น USD (balance×1%), Update messages เป็น 1-line
- `paper_trade.py` — pyramid แก้แล้ว: UPDATE existing row (weighted avg entry, combined qty/margin/notional/risk) แทน INSERT ใหม่
- `~/.claude/settings.json` — เพิ่ม ai-trade folder ใน additionalDirectories แล้ว
- `.agents/` — scaffold ใหม่ session นี้

## Blockers
- Pyramid SL/TP merge logic ยังใช้ค่าจาก signal ใหม่ — ต้องมาแก้ logic ให้ถูกต้องทีหลัง

## Next Action
แก้ pyramid SL/TP calculation เมื่อ merge position (เช่น weighted avg SL หรือ keep original SL)
