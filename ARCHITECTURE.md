# WAKAM3 Bot — System Architecture

> OKX Futures Paper Trading System  
> อัพเดทล่าสุด: 2026-05-01

---

## ภาพรวม

ระบบ Paper Trading อัตโนมัติสำหรับ **OKX Perpetual Futures** ทำงานบน **GitHub Actions** ทุก 15 นาที  
ไม่มี server ของตัวเอง — state ทั้งหมดเก็บใน Git repository

```
GitHub Actions (Cron ทุก 15 นาที)
    ↓
live_trader.py → signal_scanner.py → paper_trade.py → generate_dashboard.py → notify.py
    ↓
Git commit → GitHub Pages (dashboard.html)
```

---

## Pipeline ทีละขั้นตอน

### Step 1 — `live_trader.py` (Data Collector + Orchestrator)

- ดึง OHLCV จาก OKX exchange (30m candles, 500 bars)
- วนทุก symbol ใน `watchlist_custom.json` (38 symbols)
- เรียก `signal_scanner.scan_symbol()` สำหรับแต่ละ symbol
- บันทึกผลลัพธ์ → `scan_results.json`, `latest_signals.json`

**Timeframe:** 30m (Primary Signal TF)

---

### Step 2 — `signal_scanner.py` (Signal Router)

รับ report จาก 5 Specialist Agents แล้วรวมคะแนน → ตัดสิน SIGNAL / WATCH / IDLE

```
agent_trend.py      🎯  CDC EMA7/30, SMA99, ATR Trailing, ADX, BB Squeeze   max 13 pts
agent_smc.py        🏦  SMC: FVG, BOS, Order Block, Liquidity Zone            max 10 pts
agent_osc.py        📈  RSI, Stochastic, Momentum                             max  8 pts
agent_liquidity.py  💧  Volume Spike, Sweep, Liquidation Level                max  8 pts
agent_funding.py    💰  Funding Rate, Open Interest                           max  6 pts
                                                                        Total max 45 pts
```

**Regime Detection:** TRENDING / RANGING (ใช้ ADX + BB Width)  
**Signal Threshold:** Score ≥ ที่กำหนด + ผ่าน Regime filter

---

### Step 2.5 — `claude_filter.py` (AI Final Filter)

Claude Haiku ตรวจสอบ signal ขั้นสุดท้ายก่อนส่งให้ paper_trade  
- ใช้ **Prompt Caching** ลดต้นทุน ~90%
- ราคา: ~$0.001 ต่อ call
- ถ้าไม่มี `ANTHROPIC_API_KEY` → fallback approve ทุกอัน (ไม่ block)

---

### Step 3 — `paper_trade.py` (Trading Engine หลัก)

#### Config

| Parameter | ค่า | หมายความ |
|-----------|-----|-----------|
| `RISK_PCT` | 1% | Margin ต่อ trade = 1% ของ balance |
| `MAX_LEVERAGE` | 20x | Leverage ทุก position |
| `MAX_OPEN` | 10 | Position เปิดพร้อมกันสูงสุด |
| `MAX_PYRAMID_PER_SYMBOL` | 4 | ไม้ pyramid สูงสุดต่อ symbol |
| `TRADE_TIMEOUT_HRS` | 48h | Auto-close ถ้าค้างเกิน |
| `TP1_R` | 1.2R | TP1 = Risk × 1.2 |
| `TP2_R` | 2.0R | TP2 = Risk × 2.0 |
| `DAILY_LOSS_CAP` | $50 | ขาดทุนสูงสุดต่อวัน (UTC) |

#### check_open_trades() — ตรวจทุก position ที่เปิด

```
สำหรับทุก OPEN position:
  1. BE Guard: ถ้า tp1_hit=1 และ sl ยังต่ำกว่า entry → แก้ sl=entry อัตโนมัติ
  2. คำนวณ hit_sl, hit_tp1, hit_tp2 จาก get_price()
  3. Timeout: เกิน 48h → ปิดที่ราคาตลาด
  4. TP1 Hit → ย้าย SL มาที่ entry (BE Lock), set tp1_hit=1
  5. TP2 Hit → WIN, ปิด position
  6. SL Hit (tp1_hit=0) → LOSS
  7. SL Hit (tp1_hit=1) → WIN/SL_BE (กำไรจาก TP1 half)
  8. HTF Reversal: EMA7/EMA30 บน 1H กลับทิศ → ออกก่อน SL โดน
```

**get_price():** ลอง `SYMBOL/USDT` ก่อน → fallback `SYMBOL/USDT:USDT` (swap format)

#### open_trade() — พิจารณาเปิด position ใหม่

```
Guard ตามลำดับ:
  1. Daily Loss Cap → block ถ้าขาดทุนเกิน $50 วันนี้
  2. Pyramid Check → ถ้ามี position เดิมอยู่ → ประเมิน pyramid (ไม่ผ่าน CORR)
  3. OPP_DIRECTION Guard → block ถ้ามี position ตรงข้ามบน symbol เดียวกัน
  4. Correlation Limit → max 2 positions ทิศทางเดียวต่อกลุ่ม
  5. MAX_OPEN Check → block ถ้า position ≥ 10
  6. เปิด position ใหม่
```

---

### `position_manager.py` (Pyramid Logic)

#### Pyramid Levels

| ไม้ | เงื่อนไข PnL | Size | Min Score Trend |
|-----|-------------|------|-----------------|
| ไม้ 2 | ≥ +0.50% | 0.50% of balance | ≥ 9 |
| ไม้ 3 | ≥ +1.50% | 0.25% of balance | ≥ 10 |
| ไม้ 4 | ≥ +3.00% | 0.125% of balance | ≥ 11 |

**Special rules:**
- ถ้า `tp1_hit=1` (BE Lock) → ยกเว้น PnL threshold (pyramid ได้เลย)
- ถ้า Strong Trend (score ≥ 12) → ลด PnL threshold 30%
- Pyramid update `avg_entry` → ถ้า `tp1_hit=1` แล้ว → sl_px ขยับตาม avg_entry ใหม่

#### BE Guard (apply_pyramid)

เมื่อ pyramid เพิ่มไม้ใหม่หลัง TP1 hit แล้ว avg_entry จะขยับขึ้น  
ระบบจะ auto-set `sl_px = avg_entry` เพื่อรักษา BE ไว้เสมอ

---

### Correlation Groups

```python
Large Caps:  BTC, ETH, BNB, SOL, AVAX, DOT, LINK, NEAR, APT, SUI, TON
XRP Group:   XRP, ADA, XLM, HBAR
Meme Coins:  DOGE, SHIB, PEPE, TRUMP
DeFi:        AAVE, UNI, DEXE, ENA
AI/Data:     RENDER, WLD, TAO, ICP, ONDO
```

> max 2 positions ทิศทางเดียวกัน (LONG หรือ SHORT) ต่อกลุ่ม

---

### Step 4 — `generate_dashboard.py`

สร้าง `dashboard_data.json` ส่งให้ dashboard:
- ดึงราคา real-time จาก OKX ผ่าน `fetch_tickers()` (bulk, normalize `X/USDT:USDT` → `X/USDT`)
- รวบรวมสถิติ: balance, PnL, WR, session breakdown
- คำนวณ live PnL ทุก open position
- Generate backtest summary

---

### Step 5 — `notify.py` (Telegram Notifications)

| Event | Message |
|-------|---------|
| Signal เปิด position | `✅ SYMBOL - Entry @ price` |
| TP1 Hit | `🎯 SYMBOL - TP1 Hit → SL ขยับ Breakeven` |
| TP2 Hit | `✅ SYMBOL - TP2 Hit (WIN) +$X` |
| SL Hit | `❌ SYMBOL - SL Hit (LOSS) -$X` |
| SL_BE Hit | `🔒 SYMBOL - SL → Breakeven (WIN) +$X` |
| Timeout | `⏰ SYMBOL - Timeout (WIN/LOSS)` |

**Dedup:** ป้องกันส่ง signal ซ้ำภายใน 6 ชั่วโมง  
**Retry Queue:** `closed_results.json` — commit ไว้ใน repo เพื่อ retry รอบถัดไปถ้าส่งไม่ได้  
**PYRAMID_BLOCKED** → ไม่ส่ง notification (เงียบ)

---

## Data Files

| ไฟล์ | ประเภท | หน้าที่ |
|------|--------|---------|
| `paper_trades.db` | SQLite | trades, portfolio, signal_log |
| `dashboard_data.json` | JSON | Data สำหรับ dashboard (commit ทุก scan) |
| `scan_results.json` | JSON | ผล scan ล่าสุดทุก symbol |
| `latest_signals.json` | JSON | Signals ที่ผ่าน threshold รอบปัจจุบัน |
| `closed_results.json` | JSON | Telegram retry queue |
| `weights.json` | JSON | น้ำหนัก scoring ต่อ agent (adaptive) |
| `notified_signals.json` | JSON | Dedup log (6h TTL) |
| `watchlist_custom.json` | JSON | 38 symbols ที่ scan |
| `regime_state.json` | JSON | Regime ตลาดล่าสุด |
| `winrate_snapshot.json` | JSON | WR snapshot สำหรับ weight adjustment |

---

## Frontend (`dashboard.html`)

Single HTML file, serve ผ่าน **GitHub Pages** (main branch)  
ไม่มี backend — อ่าน `dashboard_data.json` โดยตรง

### Position Bar

```
[origSL ─── 0%] ──[red=SL]──[yellow=Entry]──[white=TP1]──[green=Now]── [100% ─── TP2]
```

- **Left boundary (0%):** Original SL ณ วันที่เปิด (คำนวณจาก `sl_pct_orig`)  
- **Red marker:** Current SL (ขยับได้ตาม BE/Pyramid)  
- **Yellow marker:** Avg Entry Price  
- **White marker:** TP1 level  
- **Green/Red marker:** ราคาปัจจุบัน

---

## Backtesting vs Live Reporting

ระบบมีงานวิเคราะห์ผล 2 แบบที่แยกกันชัดเจน:

| ระบบ | ใช้ทำอะไร | Trigger | กระทบ pipeline live หรือไม่ |
|------|-----------|---------|------------------------------|
| 3Y Backtest | Offline validation สำหรับ tune agent/filter ก่อนเปลี่ยน strategy | manual workflow หรือรัน script backtest โดยตรง | ไม่กระทบ live scan |
| 7D Summary | รายงานผลจริงจาก production/paper trades ล่าสุด | weekly report workflow | อ่าน state จริงจาก `paper_trades.db` |

3Y Backtest ไม่ใช่ production performance report และไม่ควรถูกนำไปปนกับ 7D Summary
เวลาวิเคราะห์ผล live trading หรือ alert behavior.

---

## GitHub Actions Workflow

```yaml
Trigger: cron '*/15 * * * *'  (ทุก 15 นาที)
Runner:  ubuntu-latest, Python 3.11

Steps:
  1. Checkout repo
  2. pip install -r requirements.txt
  3. python live_trader.py        ← scan signals
  4. python paper_trade.py        ← check/open trades
  5. python generate_dashboard.py ← build data
  6. python notify.py             ← send Telegram
  7. git commit & push            ← save state
```

**Concurrency:** `group: wakam3-main-state` → ไม่ run ซ้อนกัน  
**State persistence:** paper_trades.db + JSON files commit กลับ repo ทุกรอบ

---

## Exchange Integration

```python
exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}   # Perpetual Futures เท่านั้น
})
```

- ราคา: `fetch_ticker(symbol)` → fallback `fetch_ticker(symbol:USDT)` ถ้าล้มเหลว
- Bulk prices: `fetch_tickers()` → normalize key (`X/USDT:USDT` → `X/USDT`)

---

## Secrets (GitHub Repository Secrets)

| Secret | ใช้ใน |
|--------|-------|
| `ANTHROPIC_API_KEY` | claude_filter.py (Claude Haiku) |
| `TELEGRAM_TOKEN` | notify.py |
| `TELEGRAM_CHAT_ID` | notify.py |

---

## Known Behaviors & Design Decisions

| เรื่อง | การตัดสินใจ |
|--------|-------------|
| Position sizing | Fixed 1% margin (ไม่ใช่ fixed risk) → Pyramid > 1% by design |
| HTF Reversal | ใช้ 1H candles (ไม่ใช่ 30m) เพื่อลด noise |
| Pyramid vs CORR | Pyramid check ก่อน CORR → pyramid ไม่นับ correlation limit ใหม่ |
| Hedge prevention | OPP_DIRECTION guard: ไม่เปิด LONG+SHORT บน symbol เดียวกัน |
| BE after Pyramid | apply_pyramid() auto-set sl=avg_entry ถ้า tp1_hit=1 |
| DB persistence | GitHub Actions ทุก run checkout fresh → state อยู่ใน committed files |
| Price fetch failure | get_price คืน None → check_open_trades skip trade (ไม่ปิด SL/TP) |
