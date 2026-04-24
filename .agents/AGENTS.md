# AGENTS.md — Repository Policy

## Project
**wakam3-bot** — AI-powered crypto paper trading system on OKX Futures (Perpetual Swap)

## Stack
- Python 3.x, ccxt, pandas, SQLite, python-telegram-bot
- GitHub Actions for CI/CD (automated scan, trade, backtest)
- Claude Haiku as signal filter (`claude_filter.py`)

## Key Rules
- **Never commit secrets** — API keys live in GitHub Secrets / local env only
- **watchlist_custom.json** is single source of truth for live trading symbols (35 symbols)
- **paper_trades.db** is local only — not committed to git
- Backtest files use smaller symbol sets intentionally (performance)
- Always `git pull origin main --no-rebase` before push (GitHub Actions pushes frequently)

## Architecture
```
signal_scanner.py  →  claude_filter.py  →  paper_trade.py  →  notify.py
      ↓                                          ↓
live_trader.py                           paper_trades.db (SQLite)
      ↓
download_history.py → historical_data/*.parquet → backtest_*.py
```

## GitHub Actions Workflows
- `trade.yml` — runs every 15min: scan → filter → paper trade → notify
- `backtest_realistic.yml` — weekly backtest + performance report
- `download_history.yml` — downloads OKX historical OHLCV data

## Do Not Modify Without Care
- `paper_trade.py` — pyramid merge logic (UPDATE existing row, not INSERT)
- `notify.py` — Telegram message format (Signal H label, risk in USD)
- `live_trader.py` — production trading logic
