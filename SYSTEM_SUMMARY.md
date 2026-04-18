# 🤖 WAKAM3 AI Trade System — สรุประบบทั้งหมด

> อัปเดตล่าสุด: 2026-04-18

---

## 🏗️ สถาปัตยกรรมระบบ

```
GitHub Actions (ทุก 15 นาที)
│
├── 📡 live_trader.py      — ดึง OHLCV จาก OKX
│       └── scan 36 Futures symbols × 3 TF (1H/4H/1D)
│
├── 🔍 signal_scanner.py   — รวมคะแนน 5 agents
│       ├── 🎯 agent_trend.py      — CDC EMA7/30, SMA99, ATR, ADX, BB  → MAX 13 pts
│       ├── 🏦 agent_smc.py        — BOS/CHoCH, QM, Premium/Discount   → MAX 10 pts
│       ├── 📈 agent_osc.py        — RSI Div, Stoch, MACD, OBV         → MAX 11 pts
│       ├── 💧 agent_liquidity.py  — Sweep, Equal H/L, Kill Zone        → BONUS +8 pts
│       └── 💰 agent_funding.py    — Funding Rate bias                  → BONUS +6 pts
│                                                    รวม MAX = 45 pts
│
├── 🧠 claude_filter.py    — Claude Haiku กรองสุดท้าย
│       ├── Hard reject (rule-based, ไม่เสีย token)
│       ├── Pyramid check (same symbol, max 2 positions)
│       └── APPROVE / REJECT + Shadow tracking
│
├── 🤖 paper_trade.py      — เปิด/ปิด paper positions
│       ├── Dynamic risk: 1% of balance × 20x leverage
│       ├── Daily Loss Cap: $50/วัน
│       ├── Correlation Guard: max 2 ทิศเดียวกันต่อกลุ่ม
│       ├── Pyramiding: max 2 positions/symbol
│       ├── Trade Timeout: 48h
│       └── HTF Reversal Exit: 4H EMA เปลี่ยนทิศ → cut
│
├── 📊 generate_dashboard.py — สร้าง dashboard_data.json
└── 📱 notify.py            — ส่ง Telegram (dedup 6h TTL)
```

---

## 🔧 Features ทั้งหมด

| Feature | รายละเอียด |
|---|---|
| **5 Specialist Agents** | Trend/SMC/Osc (core /31) + Liq/Fund (bonus) = MAX 45 |
| **Dynamic Weights** | ปรับ weight อัตโนมัติตาม win rate ของแต่ละ agent |
| **Claude Haiku Filter** | AI กรองสุดท้าย context-aware |
| **Prompt Caching** | System prompt cached ลดต้นทุน ~90% |
| **Method A Pre-filter** | positions_full / RANGING / pyramid_blocked → ไม่ถึง Claude |
| **Shadow Trades** | track outcome ของ signal ที่ถูก reject |
| **Signal Log** | บันทึกทุก signal ไม่ว่าเทรดหรือ skip |
| **Pyramiding** | Add to winners: trend≥10/13 + pnl≥0% → เปิด pos#2 ได้ |
| **Correlation Guard** | max 2 positions ทิศเดียวกันต่อกลุ่มเหรียญ |
| **HTF Reversal Exit** | 4H EMA เปลี่ยนทิศ → cut position ทันที |
| **Trade Timeout 48h** | ปิดอัตโนมัติถ้าค้างเกิน |
| **Daily Loss Cap $50** | หยุดเปิดใหม่ถ้าขาดทุนเกิน $50/วัน |
| **Watchlist GitHub Sync** | เพิ่มเหรียญใน UI → sync GitHub PAT → scan อัตโนมัติ |
| **Claude API Usage Card** | ติดตามค่าใช้จ่าย Haiku รายวัน |
| **Telegram Notifications** | TP1/TP2/SL/Timeout/Order hit (dedup 6h) |
| **Backtest 3Y** | Walk-forward backtest 9,961 trades Mar2023–Mar2026 |
| **Weekly Report** | Claude วิเคราะห์ผล backtest ส่ง Telegram ทุกอาทิตย์ |
| **Paper Lab** | วิเคราะห์ by TF / by Symbol / by Year / Trade Log |
| **TP2 Column** | แสดงใน Paper Positions + Backtest Trade Log |

---

## 📂 ไฟล์หลักในระบบ

| ไฟล์ | หน้าที่ |
|---|---|
| `live_trader.py` | Data Collector + Orchestrator (รัน scan ทุก 15 นาที) |
| `signal_scanner.py` | รวมคะแนน agents → SIGNAL / WATCH / IDLE |
| `agent_trend.py` | Trend Agent (EMA7/30, SMA99, ATR, ADX, BB) MAX 13 |
| `agent_smc.py` | SMC Agent (BOS/CHoCH, QM, Zones) MAX 10 |
| `agent_osc.py` | Oscillator Agent (RSI, Stoch, MACD, OBV) MAX 11 |
| `agent_liquidity.py` | Liquidity Agent (Sweep, Kill Zone) BONUS +8 |
| `agent_funding.py` | Funding Agent (Funding Rate bias) BONUS +6 |
| `claude_filter.py` | Claude Haiku AI Filter + Pyramid logic + Usage tracking |
| `paper_trade.py` | Paper Trading Engine (open/close/timeout/pyramid) |
| `notify.py` | Telegram Notifier (dedup 6h TTL) |
| `generate_dashboard.py` | สร้าง dashboard_data.json |
| `weekly_report.py` | Weekly Backtest Report → Telegram |
| `backtest_live.py` | Backtest 7d (signal_log data) |
| `backtest_3y.py` | Backtest 3Y (historical OHLCV) |
| `dashboard.html` | Web Dashboard (อ่าน dashboard_data.json) |
| `watchlist_custom.json` | Custom symbols ที่ user เพิ่มเอง → scan อัตโนมัติ |
| `api_usage.json` | Claude Haiku token/cost tracking รายวัน |
| `paper_trades.db` | SQLite: trades, shadow_trades, signal_log, portfolio |
| `weights.json` | Dynamic weights ของ 3 core agents |

---

## ⚙️ GitHub Actions Workflows

| Workflow | ตาราง | หน้าที่ |
|---|---|---|
| `scan.yml` | ทุก 15 นาที | live_trader → signal_scanner → claude_filter → paper_trade → notify |
| `backtest.yml` | อาทิตย์ 23:00 TH | backtest_live → generate_dashboard → weekly_report → Telegram |

---

## 💰 ต้นทุนและค่าใช้จ่าย

### Infrastructure (ฟรีทั้งหมด)
| บริการ | แผน | ราคา |
|---|---|---|
| **GitHub Actions** | Free tier (2,000 min/month) | $0 |
| **OKX API** | Public OHLCV + Funding Rate | $0 |
| **GitHub Repo** | Free | $0 |

### Claude API (จ่ายตามใช้ — Anthropic Console)
| Token Type | อัตรา |
|---|---|
| Input tokens | $0.80 / 1M tokens |
| Output tokens | $4.00 / 1M tokens |
| Cache write | $1.00 / 1M tokens |
| Cache read | $0.08 / 1M tokens (90% ถูกกว่า) |

### ค่าใช้จ่ายจริงต่อวัน
```
สถานการณ์ปกติ (positions เต็มบ่อย):
  ~5–10 Claude calls/วัน × $0.0016/call ≈ $0.008–0.016/วัน

สถานการณ์แย่ (scan เยอะ, cache miss ทุกครั้ง):
  ~20 calls/วัน × $0.0016 ≈ $0.032/วัน

$5 credit อยู่ได้ประมาณ: 150–600+ วัน
```

### รวมต่อเดือน
| รายการ | ต่อเดือน |
|---|---|
| Claude Haiku (filter) | ~$0.25–0.50 |
| Claude Sonnet (weekly report) | ~$0.10–0.20 |
| **รวมทั้งหมด** | **~$0.35–0.70/เดือน** |
| **$5 credit อยู่ได้** | **~7–14 เดือน** |

> **Note:** Cache Hit Rate ปัจจุบัน = 0% เพราะ scan ห่าง 15 นาที > TTL 5 นาที
> ถ้า cache hit ได้ 90% จะประหยัดได้อีก ~5–10x

---

## 📈 Score System

```
Core Score (normalize เทียบกัน):
  Trend Agent  → MAX 13 pts  ÷ 13 × weight_trend
  SMC Agent    → MAX 10 pts  ÷ 10 × weight_smc
  Osc Agent    → MAX 11 pts  ÷ 11 × weight_osc
  ───────────────────────────────
  Core normalized → /31 pts

Bonus Score (บวกตรงๆ ไม่ normalize):
  Liquidity Agent → BONUS +0 ถึง +8 pts
  Funding Agent   → BONUS +0 ถึง +6 pts
  ───────────────────────────────
  Total MAX = 45 pts

Threshold: score ≥ 9/45 → ส่งต่อ Claude Filter
```

---

## 🛡️ Risk Management Rules

### Hard Reject (ก่อนถึง Claude — ไม่เสีย token)
| Rule | เงื่อนไข |
|---|---|
| Overextension | score ≥ 15 AND regime ≠ TRENDING |
| RANGING noise | regime = RANGING AND score < 12 |
| RANGING ทุกกรณี | ข้าม (WR < 50% ใน backtest) |
| Tight SL | sl_pct < 0.5% |
| VOLATILE low score | regime = VOLATILE AND score < 14 |
| RSI overbought | LONG AND RSI > 75 |
| RSI oversold | SHORT AND RSI < 25 |
| Funding crowded | LONG AND funding > +0.15% |

### Portfolio Guards
| Guard | เงื่อนไข |
|---|---|
| Max positions | 10 positions พร้อมกัน |
| Daily Loss Cap | ขาดทุน > $50/วัน → หยุดเปิดใหม่ |
| Correlation | max 2 ทิศเดียวกันต่อกลุ่ม |
| Trade Timeout | ปิดอัตโนมัติที่ 48h |

### Pyramiding Rules
| เงื่อนไข | ผล |
|---|---|
| score_trend ≥ 10/13 AND existing_pnl ≥ 0% | ALLOW pyramid #2 |
| score_trend ≥ 9/13 AND existing_pnl > +1% | ALLOW pyramid #2 |
| มี 2 positions แล้ว | BLOCK (max 2/symbol) |
| ไม่ผ่านเงื่อนไข | BLOCK |

---

## 📊 ผล Backtest ล่าสุด (7d)

| Metric | ค่า |
|---|---|
| ช่วงเวลา | 2026-04-16 → 2026-04-17 |
| Signals | 22 |
| Win Rate | 63.6% |
| W/L | 14 / 8 |
| Total PnL | +$59.89 |
| Avg PnL | +$2.72 |
| Sharpe | 4.03 |
| Max Drawdown | -10.5% |
| Profit Factor | 1.85 |
| Verdict | ✅ STRONG PASS |

---

## 🔄 สถานะปัจจุบัน (2026-04-18)

```
✅ Paper Trading  — RESET แล้ว | Balance = $1,000.00 (เริ่มใหม่)
✅ Symbols        — 36 Futures (ลบ TAO/MNT/VET/DEXE/Spot ออก)
✅ Pyramiding     — พร้อม (max 2/symbol)
✅ Watchlist Sync — GitHub PAT ตั้งค่าแล้ว (auto sync)
✅ Claude Tracking— API usage card ใน Health Check
✅ Telegram       — Token ตั้งค่าแล้ว พร้อมรับ signal
🔄 Scan           — ทุก 15 นาที (GitHub Actions)
📅 Weekly Report  — อาทิตย์ 23:00 TH
```

---

## 🔮 สิ่งที่ควรติดตาม / TODO อนาคต

| รายการ | Priority | หมายเหตุ |
|---|---|---|
| Win Rate จริง | สูง | รอ 30+ trades เพื่อให้ Dynamic Weights ทำงาน |
| Pyramid trade ครั้งแรก | กลาง | ยังไม่เคยเกิด รอดู logic |
| Liq/Fund Win Rate | ต่ำ | รอ 50+ trades ค่อยเพิ่มใน dashboard |
| Cache Hit Rate | ต่ำ | ยัง 0% — acceptable ราคายังถูกอยู่ |
| Real Trading | อนาคต | เมื่อ paper WR > 55% ต่อเนื่อง 3 เดือน |
