# Repo Tree — ai-trade (wakam3-bot)
> Generated: 2026-04-23

## Core Trading Scripts
- `signal_scanner.py` — scans OKX for signals using multi-agent scoring
- `claude_filter.py` — Claude Haiku as secondary signal filter
- `paper_trade.py` — paper trading engine (SQLite DB, pyramid, TP/SL)
- `live_trader.py` — live trading (reads watchlist_custom.json, 35 symbols)
- `notify.py` — Telegram notifications (signal, update, close messages)

## Agent Modules (signal scoring)
- `agent_trend.py` — EMA trend, HTF alignment
- `agent_osc.py` — RSI, momentum oscillators
- `agent_smc.py` — Smart Money Concepts (FVG, BOS, CHoCH)
- `agent_funding.py` — funding rate analysis
- `agent_liquidity.py` — liquidity sweep detection

## Backtest
- `backtest_3y.py` — 3-year backtest (smaller symbol set, by design)
- `backtest_live.py` — backtest matching live strategy
- `backtest_portfolio.py` — portfolio-level backtest
- `download_history.py` — pre-download OHLCV → historical_data/*.parquet

## Config / Data
- `watchlist_custom.json` — 35 symbols (single source of truth for live)
- `weights.json` — agent scoring weights
- `paper_trades.db` — SQLite (trades, portfolio, signal_log tables)
- `scan_results.json` / `latest_signals.json` — live signal cache
- `regime_state.json` — current market regime

## CI/CD (.github/workflows/)
- `trade.yml` — every 30min: scan → filter → trade → notify
- `backtest_realistic.yml` — weekly performance report
- `download_history.yml` — historical data download

## Dashboard
- `dashboard.html` + `generate_dashboard.py` — web dashboard
- `dashboard_data.json` — data feed for dashboard

## Proposals
- `proposals/YYYY-MM-DD_proposal.json` — daily weight adjustment proposals
