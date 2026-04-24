# Paper Trade Architecture

## Database (paper_trades.db)
Tables: `trades`, `portfolio`, `signal_log`

### trades columns (key)
- `id`, `symbol`, `side`, `status` (OPEN/CLOSED)
- `entry_px`, `qty`, `notional_usd`, `margin_usd`, `risk_usd`
- `sl_px`, `tp1_px`, `tp2_px`
- `pyramid_level` (1=first entry, 2=pyramid add)
- `score`, `opened_at`, `closed_at`, `pnl_usd`, `close_reason`

## Position Sizing
- `margin = balance × 1%`
- `notional = margin × 20x`
- `qty = notional / entry_px`
- Max open positions: 10 slots

## Pyramid Logic (paper_trade.py → open_trade())
- Condition: `score_trend >= 10 AND pnl_pct > 0.5%`
- **MERGE** into existing row (UPDATE, not INSERT) — 1 slot per symbol
- Weighted average entry: `(existing_qty × existing_entry + add_qty × new_entry) / total_qty`
- Combined: qty, notional, margin, risk (sum)
- SL/TP: currently uses new signal values — TODO: needs proper logic

## Signal Flow
```
signal dict → claude_filter → paper_trade.open_trade()
                                    ↓
                           check existing OPEN position
                                    ↓
                      pyramid eligible? → UPDATE existing row
                                    ↓
                      new position? → INSERT with pyramid_level=1
```

## Notify Format (notify.py)
- `signal_msg()` → "🚀 Signal H : BTC - RR1.2⚡KZ" (H = Haiku approved)
- Risk line: `$XX.XX (Lev 20x)` based on current balance
- Update messages: 1-line format
  - `"Update : 🔵 BTC - Order Limit Hit"`
  - `"Update : 🎯 BTC - TP1 Hit → SL ขยับ Breakeven"`
  - `"Update : 🔒 BTC - SL → Breakeven (WIN)  +$X.XX"`
  - `"Update : ✅ BTC - TP2 Hit (WIN)  +$X.XX"`
  - `"Update : ❌ BTC - SL Hit (LOSS)  -$X.XX"`
