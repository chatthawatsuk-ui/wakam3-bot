# WAKAM3 Improvement Plan v1

**System:** OKX Perpetual Futures paper-trading bot  
**Runtime:** GitHub Actions every 15 minutes  
**Stack:** Python · ccxt · ta · SQLite · Claude Haiku  
**Date:** 2026-05-15

---

## 1. Philosophy

| Layer | Role | Cadence |
|-------|------|---------|
| **Weekly** | Radar — surface patterns, flag anomalies | Every Sunday 16:00 UTC |
| **Monthly** | Analyst — evaluate strategy drift, propose parameter updates | Every 1st of month |
| **Quarterly** | Strategy committee — regime review, agent weight overhaul | Every quarter |
| **Emergency guard** | Seatbelt — hard guardrails, always active | Every run |

Weekly = observation only.  
Monthly = structured analysis → proposal → human approval.  
No system auto-applies changes without explicit `/approve_*` command from Telegram.

---

## 2. System Overview (as of 2026-05-15)

**Scoring pipeline:**

```
live_trader.py (30m OHLCV)
  └─▶ signal_scanner.scan_symbol()
        ├─ agent_trend   → max 13 pts  (CDC EMA7/30, SMA99, ATR trail, ADX, BB squeeze)
        ├─ agent_smc     → max 12 pts  (BOS/CHoCH, QM, premium/discount zones)
        ├─ agent_osc     → max 16 pts  (RSI div, Stoch, MACD, OBV)
        ├─ agent_liquidity → +8 bonus  (sweep, equal H/L, session)
        └─ agent_funding   → +6 bonus  (funding rate positioning)
              └─▶ Total MAX = 45
```

**Execution pipeline:**

```
signal_scanner → claude_filter (Haiku gate) → paper_trade.py → SQLite
```

**Key config constants (current values):**

| Constant | File | Value |
|----------|------|-------|
| `MIN_SCORE` | `signal_scanner.py:35` | 9 |
| `MAX_SL_PCT` | `signal_scanner.py:36` | 0.15 (15%) |
| `RISK_PCT` | `paper_trade.py:19` | 0.01 (1%) |
| `MAX_LEVERAGE` | `paper_trade.py:20` | 20 |
| `TP1_R` | `paper_trade.py:24` | 1.2 |
| `TP2_R` | `paper_trade.py:25` | 2.0 |
| `DAILY_LOSS_CAP` | `paper_trade.py:29` | $50 |
| `MAX_OPEN` | `paper_trade.py:21` | 10 |
| `choch_bull/bear` | `condition_points.json` / `config_loader.py:31` | +4 |
| `MODEL` | `claude_filter.py:22` | claude-haiku-4-5-20251001 |
| Haiku fail behavior | `claude_filter.py:453` | **fail-open** (approve all) |

**TF note:** `live_trader.py` fetches 30m OHLCV and passes the same DataFrame as both `df_1h` and `df_4h` to downstream agents. True HTF 4H data is not currently fetched.

---

## 3. Historical Data Protection

```
historical_data/          ← 3-year OHLCV parquet files (2023–2026)
download_history.py       ← data download script
backtest_3y.py            ← backtest engine
backtest_*.py             ← all backtest variants
```

**These files are INPUT DATA — never in scope for any bot reset or cleanup.**

Day-0 reset scope = bot state only:
- `paper_trades.db` — trade history, portfolio balance
- `*.json` output files (`latest_signals.json`, `scan_results.json`, `api_usage.json`, etc.)
- Pending approval files (`pending_*.json`)

Historical OHLCV parquet files must never be deleted, moved, or reset as part of bot maintenance.

---

## 4. Already Completed

### Phase A — Weekly Approve Disabled ✅ DONE

**What changed:**
- `weekly_report.py` — report is now monitoring/watchlist only
- Weekly no longer creates `pending_condition_points.json`, `pending_regime_weights.json`, `pending_weights.json` from 7-day data
- Weekly Telegram summary no longer shows `/approve_conditions`, `/approve_regime`, `/approve_weights` commands
- `_clear_weekly_pending_files()` function added — cleans up any stale pending files on weekly run

**Test coverage:** `test_weekly_report_monitor_only_clears_pending_files` in `tests/test_runtime_fixes.py`

**Why:** Weekly 7-day window is too short and too noisy to drive parameter changes. Weekly = radar, not decision-maker.

---

### P0 — TP1 Partial Close Accounting Fix ✅ DONE

**What changed:**
- Normal TP1→TP2 and TP1→SL_BE paths were already correct (50% at TP1, 50% at TP2/SL)
- Bug was isolated to **TP1→TIMEOUT** and **TP1→forced/max-close** exit paths
- Fixed: PnL for these paths now = `(0.5 × qty × (tp1_px - entry_px)) + (0.5 × qty × (close_px - entry_px))`
- Previously these paths computed `qty × (close_px - entry_px)` ignoring the TP1 partial realization

**Test coverage:** `test_tp1_timeout_uses_partial_close_accounting` in `tests/test_runtime_fixes.py`  
Verifies: trade with `tp1_hit=1`, timeout at price=90, entry=100, tp1=112, qty=1  
Expected: `outcome=WIN`, `pnl=1.0` (0.5×12 + 0.5×(−10) = 6 − 5 = 1)

**Caveat:** Forced-close / MAX_POS exit path fixed but does not yet have a dedicated regression test. Recommended to add before any further PnL-path changes.

**Baseline note:** Trade history recorded before this fix contains PnL noise on timeout/forced paths. Treat pre-fix baseline as directional reference only, not precise accounting.

---

## 5. Improvement Priority List

| # | Item | Effort | Impact | Status |
|---|------|--------|--------|--------|
| P1 | SL cap ≤ 4% (ATR×3 fallback) | S | Critical | Pending |
| P2 | MIN_SCORE 9 → 14 + core floor | S | High | Pending |
| P3 | Haiku fail-safe → reject (not approve) | XS | Easy win | Pending |
| P4 | CHoCH +4 → +2 | XS | Quick | Pending |
| P5 | Event filter FOMC/CPI block ±1h | L | High if leverage=20x | Pending |
| P6 | HTF 4H bias filter (true multi-TF) | XL | Highest long-term | Pending |
| M1 | Monthly report framework | L | Medium | Pending |

---

## 6. P1 — SL Cap ≤ 4% (or ATR×3, whichever is tighter)

**Problem:** `MAX_SL_PCT = 0.15` (15%) allows very wide stops. At 20× leverage, a 5% adverse move = 100% margin loss. SL must be capped to protect capital.

**Rule:** `effective_sl_dist = min(signal_sl_dist, 0.04, atr * 3 / entry_px)`  
If signal SL would exceed cap → reject signal (not widen SL, which distorts R-ratios).

**Design options:**
- Option A: Hard-reject in `signal_scanner.py` before Claude gate (cheapest, consistent)
- Option B: Clamp SL distance in `signal_scanner.py` and recalculate TP1/TP2 (changes RR ratio)
- Recommendation: **Option A** — reject and log reason. Clamping SL silently changes trade character.

---

## 7. P2 — MIN_SCORE 14 + Core Floor

**Problem:** `MIN_SCORE = 9` is too permissive. A signal can pass with only 1-2 agents contributing.

**Proposed rules:**
1. `MIN_SCORE` total: 9 → **14**
2. Core floor: at least 2 of 3 core agents (Trend, SMC, Osc) must have score > 0
3. Minimum per-agent threshold: none proposed yet (needs backtest validation)

**Open question:** Distribution of current signals at score 9-13 is unknown. Need to query `signal_log` to verify how many signals would be filtered before implementing. If >50% of live signals are in 9-13 range, threshold of 14 may kill too many opportunities.

---

## 8. P3 — Haiku Fail-Safe: Reject on API Failure

**Problem:** `claude_filter.py:453` — when Anthropic API raises any exception, current behavior:
```python
return True, f"err:{str(e)[:60]}", execution_meta
```
`True` = approved. API down → all signals pass unfiltered.

**Fix:** Change to `return False` (reject) with a clearly labeled reason string so paper_trade skips the trade. Hard-reject rules (`_hard_reject()`) still run before API call, so genuinely bad signals are still blocked regardless.

**Consideration:** If API is down for hours, bot stops trading entirely. This is the correct conservative behavior for a leveraged system. Telegram alert already fires on API errors.

---

## 9. P4 — CHoCH Weight: +4 → +2

**Problem:** CHoCH (Change of Character) is weighted at +4 — same as a full Trend agent signal. CHoCH alone can push a weak signal to MIN_SCORE. This over-weights a single SMC concept.

**Fix:** Edit `condition_points.json` key `choch_bull` and `choch_bear` from 4 → 2.

**Note:** `config_loader.py` merges `condition_points.json` over `DEFAULT_CONDITION_POINTS`. Only the JSON file needs updating — no code change required.

---

## 10. P5 — Event Filter: FOMC / CPI Block ±1h

**Problem:** High-impact macro events cause wick spikes >5% in a single 30m candle. At 20× leverage, this can trigger SL even on correct direction trades.

**Design:**
- Maintain a static or API-sourced event calendar
- Block new trade entry 1h before and 1h after scheduled events
- Existing open positions: do NOT force-close, just block new entries
- Source options: static hardcoded dates (simple), or fetch from economic calendar API (complex)

**Effort note:** Static schedule requires manual maintenance. API source adds external dependency. Recommend starting with static list covering major recurring events.

---

## 11. P6 — True HTF 4H Bias Filter

**Problem:** `live_trader.py` currently passes 30m DataFrame as both `df_1h` and `df_4h`. Agents receive identical data for both timeframes. `agent_trend._htf_bias()` has the correct 4H logic but receives 30m data.

**Design:**
- `live_trader.py`: fetch separate `4h` OHLCV alongside `30m` for each symbol
- Pass true 4H df as `df_4h` to `TREND.run()` and `SCANNER.scan_symbol()`
- `agent_trend._htf_bias()` already handles true 4H correctly — no agent code change needed
- `scan_symbol()` signature already accepts `df_4h` — no scanner interface change needed
- Rate limit impact: +1 API call per symbol per run (38 symbols × 2 calls = 76 calls/run instead of 38)

**Effort:** Large — requires `live_trader.py` data fetch refactor + integration tests + monitoring for rate limit errors.

---

## 12. M1 — Monthly Report Framework

**Context:** Weekly = radar (monitoring only, no approvals). Monthly = analyst (structured proposals, human-approved parameter changes).

**Components to build:**
- `monthly_report.py` — aggregate 30-day trade data, generate L4/L5 proposals
- `.github/workflows/monthly_report.yml` — cron trigger (1st of each month)
- `notify.py` — add monthly Telegram message format
- `proposals/monthly/` — directory for pending monthly proposal files

**Monthly report should include:**
1. 30-day PnL, win rate, avg RR breakdown by regime
2. Agent score distribution (identify which agents fire most/least)
3. L4 proposal: condition_points adjustments with supporting stats
4. L5 proposal: regime_weights adjustments
5. Human approves via `/approve_conditions` and `/approve_regime` on Telegram

---

## 13. Implementation Mapping

### P1 — SL Cap

| Item | Detail |
|------|--------|
| **Primary file** | `signal_scanner.py` |
| **Entry point** | `scan_symbol()` — after SL is calculated (~line 270), before Claude gate |
| **Secondary file** | `paper_trade.py` — optional defensive check in `open_trade()` |
| **Config** | No new config — hardcode `SL_CAP_PCT = 0.04` as constant in `signal_scanner.py` |
| **ATR source** | `t_rep["atr"]` is already available at the SL calculation point |
| **Test needed** | `test_sl_cap_rejects_wide_stop` — signal with SL 8% from entry → expect `SL_REJECT` |
| **Migration risk** | None — reject path only, no DB changes |
| **Verification** | Check `scan_results.json` for `SL_REJECT` entries post-deploy; query `signal_log` for `skip_reason='SL_CAP'` |

### P2 — MIN_SCORE + Core Floor

| Item | Detail |
|------|--------|
| **Primary file** | `signal_scanner.py` |
| **Entry point** | `MIN_SCORE = 9` (line 35) → change to `14` |
| **Core floor logic** | Add check after `_weighted_score()` call: count how many of `b_trend`, `b_smc`, `b_osc` are > 0 |
| **Config** | `MIN_SCORE` is a module constant — change in source, not config file |
| **Pre-req** | Query `SELECT score, COUNT(*) FROM signal_log WHERE outcome='PENDING' OR was_traded=1 GROUP BY score ORDER BY score` to understand current signal distribution before committing to threshold |
| **Test needed** | `test_min_score_14_rejects_score_13` + `test_core_floor_requires_two_agents` |
| **Migration risk** | None — filter only, no DB schema change |
| **Verification** | Monitor signal rate for 48h post-deploy; expect fewer SIGNAL statuses |

### P3 — Haiku Fail-Safe

| Item | Detail |
|------|--------|
| **Primary file** | `claude_filter.py` |
| **Entry point** | `ask()` exception handler, line ~436 |
| **Change** | `return True, ...` → `return False, f"claude_api_error:{str(e)[:60]}", execution_meta` |
| **Secondary** | `notify.py` — existing Telegram alert on API error already in place, no change needed |
| **Test needed** | `test_haiku_api_error_rejects_signal` — mock `client.messages.create` to raise exception → assert `approved=False` |
| **Migration risk** | None |
| **Verification** | Temporarily set invalid API key in test env; confirm signals are blocked |

### P4 — CHoCH Weight

| Item | Detail |
|------|--------|
| **Primary file** | `condition_points.json` |
| **Keys** | `"choch_bull": 4` → `2`, `"choch_bear": 4` → `2` |
| **Secondary** | `config_loader.py` `DEFAULT_CONDITION_POINTS` lines 31–32 — update defaults to match |
| **No code change** | `agent_smc.py` reads from `_PT["choch_bull"]` dynamically via `config_loader` |
| **MAX_SCORE impact** | SMC MAX_SCORE recalculates dynamically: drops from 12 → 10 |
| **Test needed** | `test_choch_weight_reduced` — verify `agent_smc.MAX_SCORE` equals new expected sum |
| **Migration risk** | None |
| **Verification** | `python3 -c "import agent_smc; print(agent_smc.MAX_SCORE)"` — should be 10 after change |

### P5 — Event Filter

| Item | Detail |
|------|--------|
| **New file** | `event_calendar.py` — static list of high-impact event datetimes (UTC) + `is_blocked()` function |
| **Primary integration** | `live_trader.py` — check `is_blocked()` before calling `scan_symbol()`, skip all signals if blocked |
| **Alternative integration** | `signal_scanner.scan_symbol()` — add `event_blocked` flag to scan_result |
| **Config** | Static event list maintained in `event_calendar.py`; update manually each quarter |
| **Test needed** | `test_event_filter_blocks_during_fomc` — mock datetime to FOMC window → expect no signals |
| **Migration risk** | None |
| **Verification** | Manual: check bot logs on next FOMC date |

### P6 — HTF 4H Bias Filter

| Item | Detail |
|------|--------|
| **Primary file** | `live_trader.py` — add separate `4h` OHLCV fetch per symbol |
| **Fetch pattern** | `exchange.fetch_ohlcv(sym, '4h', limit=150)` — 150 candles = 25 days (warm EMA30) |
| **Pass-through** | `scan_symbol(sym, df_30m, df_4h_true, ...)` |
| **Agent impact** | `agent_trend._htf_bias()` already handles true 4H — no change needed |
| **Rate limit** | OKX public endpoint: no key needed, ~10 req/sec. 38 symbols × 2 TF = 76 calls/run at 15min cadence = safe |
| **Fallback** | `agent_trend._htf_bias()` already has fail-open fallback if `df_4h.empty` |
| **Test needed** | Integration test: mock separate 30m and 4H data where they disagree → verify HTF filter blocks misaligned signal |
| **Migration risk** | None — additive change |
| **Verification** | Add `htf_source_tf` field to scan_result; log in `scan_results.json` to confirm 4H data is flowing |

### M1 — Monthly Report

| Item | Detail |
|------|--------|
| **New files** | `monthly_report.py`, `.github/workflows/monthly_report.yml`, `proposals/monthly/` (dir) |
| **Modified files** | `notify.py` (monthly message format), `generate_dashboard.py` (monthly approval polling) |
| **Trigger** | Cron: `0 16 1 * *` (1st of each month at 16:00 UTC) |
| **Data source** | `paper_trades.db` — 30-day window of closed trades |
| **Output** | `proposals/monthly/YYYY-MM-proposals.json` + Telegram message |
| **Approval path** | Same `/approve_conditions` + `/approve_regime` Telegram commands, polled by `generate_dashboard.py` |
| **Test needed** | `test_monthly_report_generates_proposals` — mock DB with 30 days trades → verify proposal keys |
| **Migration risk** | None — new files only |

---

## 14. Open Questions

1. **Signal distribution at score 9-13:** Before implementing P2, query `signal_log` to understand what % of current signals fall in the 9-13 range. Raising MIN_SCORE to 14 without this data risks silencing too many valid signals or too few bad ones.

2. **TP2 hit rate data:** TA review suggested TP2 could be raised from 2.0R to 2.5R. This requires actual backtest or live data showing TP2 hit rate at current 2.0R threshold before any change. Do not change without data.

3. **P3 fail-safe trading gap:** If Haiku API is down for extended period (>2h) and fail-safe rejects all signals, bot will miss trading opportunities. Acceptable trade-off for a paper system, but worth documenting as known behavior.

4. **CHoCH reduction cascades:** SMC MAX_SCORE drops from 12 to 10 after P4. This changes the normalization denominator in `_weighted_score()`. Verify that no test or hardcoded reference assumes SMC MAX=12 before deploying.

5. **live_trader.py TF aliasing:** Comment in `live_trader.py` states `df_1h/df_4h` parameter names are "intentional aliases" for 30m data. If P6 true-HTF is implemented, this comment and the downstream agent parameter names should be cleaned up to avoid confusion.

6. **Forced/max-close TP1 regression test:** P0 fix covered TP1→TIMEOUT with a unit test. TP1→forced-close (MAX_POS enforcement) path was also fixed but has no dedicated regression test yet. Add before any further PnL path changes.

---

## 15. Validation Checklist (run before any commit)

```bash
# Compile check — no syntax errors
python3 -m compileall -q .

# Run all tests
python3 -m unittest discover -s tests -v

# Whitespace/conflict marker check
git diff --check
```

Expected baseline: all 9 tests in `test_runtime_fixes.py` pass.

---

*Document reflects codebase state as of 2026-05-15. Update Section 4 and Section 13 as items are completed.*
