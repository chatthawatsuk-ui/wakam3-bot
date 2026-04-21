"""
backtest_1y_chart.py — Realistic Portfolio Backtest
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Rules:
  - Start balance : $1,000
  - Risk per trade: 1% of current balance (dynamic)
  - Leverage      : 20x
  - Max positions : 10
  - Pyramid       : max 2 per symbol (same rules as live bot)
  - TF            : 30m gate = 30m (TF ใครTF)
  - Period        : 365 days | BTC/USDT
"""
import warnings; warnings.filterwarnings("ignore")
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import ccxt, time as time_mod
from ta.trend      import EMAIndicator, SMAIndicator

import signal_scanner as SCANNER
import agent_trend    as TREND

SCANNER.save_condition_snapshot = lambda *a, **kw: None
try: SCANNER.save_specialist_history = lambda *a, **kw: None
except: pass
SCANNER.DISABLE_FUNDING = True

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL          = "BTC/USDT"
DAYS         = 1095
WARMUP          = 250
START_BALANCE   = 1000.0
RISK_PCT        = 0.01        # 1% of balance
LEVERAGE        = 20
MAX_POSITIONS   = 10
MAX_PYRAMID     = 2
TP1_R           = 1.2
TP2_R           = 2.0
TIMEOUT_CANDLES = 96          # 48h × 2 (30m)
ROLLING_WIN     = 20
OUT_PNG         = "backtest_1y_chart.png"

# ── Patch HTF → 30m gate ──────────────────────────────────────────────────────
def _htf_30m(df_primary, df_htf):
    df = df_primary.copy()
    c  = df["close"]
    df["htf_bull"] = EMAIndicator(c, TREND.EMA_FAST).ema_indicator() > \
                     EMAIndicator(c, TREND.EMA_SLOW).ema_indicator()
    df["htf_sma"]  = c > SMAIndicator(c, TREND.SMA_99).sma_indicator()
    return df

TREND._htf_bias = _htf_30m

# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol, tf, days):
    ex    = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    since = int(ex.milliseconds() - (days + 10) * 86400_000)
    rows  = []
    while True:
        chunk = ex.fetch_ohlcv(symbol, tf, since=since, limit=300)
        if not chunk: break
        rows.extend(chunk)
        if len(chunk) < 300: break
        since = chunk[-1][0] + 1
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    return df[~df.index.duplicated()].sort_index()

# ── Position sizing ───────────────────────────────────────────────────────────
def calc_size(balance, entry_px, sl_px):
    """คืน (notional, margin, risk_usd) — None ถ้า margin ไม่พอ"""
    sl_pct = abs(entry_px - sl_px) / entry_px
    if sl_pct < 0.001: return None
    risk_usd = balance * RISK_PCT
    notional = risk_usd / sl_pct
    margin   = notional / LEVERAGE
    if margin > balance * 0.5: return None   # margin เกิน 50% balance
    return notional, margin, risk_usd

# ── PnL calculator ────────────────────────────────────────────────────────────
def calc_pnl(side, entry_px, sl_orig, tp1_hit, exit_px, exit_reason, notional):
    sl_pct  = abs(entry_px - sl_orig) / entry_px
    risk    = notional * sl_pct          # dollar at risk
    tp1_pnl = notional * sl_pct * TP1_R  # TP1 dollar gain
    tp2_pnl = notional * sl_pct * TP2_R

    if exit_reason == "TP2":
        return 0.5 * tp1_pnl + 0.5 * tp2_pnl
    if exit_reason == "SL":
        return 0.5 * tp1_pnl if tp1_hit else -risk
    # TIMEOUT
    r_val = (exit_px - entry_px) / entry_px if side == "LONG" \
            else (entry_px - exit_px) / entry_px
    return notional * r_val

# ── Backtest ──────────────────────────────────────────────────────────────────
def backtest(df_full):
    balance    = START_BALANCE
    open_trades = []   # list of dict
    closed      = []
    equity_ts   = [df_full.index[WARMUP]]
    equity_val  = [balance]

    def sym_trades(symbol):
        return [t for t in open_trades if t["symbol"] == symbol]

    for i in range(WARMUP, len(df_full)):
        r_now  = df_full.iloc[i]
        ts_now = df_full.index[i]
        hi, lo, cl = r_now["high"], r_now["low"], r_now["close"]

        # ── Update open trades ────────────────────────────────────────────────
        still_open = []
        for t in open_trades:
            side = t["side"]
            sl   = t["sl_px"]
            tp1  = t["tp1_px"]
            tp2  = t["tp2_px"]

            if side == "LONG":
                if not t["tp1_hit"] and hi >= tp1:
                    t["tp1_hit"] = True
                    t["sl_px"]   = t["entry_px"]
                hit_sl  = lo <= t["sl_px"]
                hit_tp2 = hi >= tp2
            else:
                if not t["tp1_hit"] and lo <= tp1:
                    t["tp1_hit"] = True
                    t["sl_px"]   = t["entry_px"]
                hit_sl  = hi >= t["sl_px"]
                hit_tp2 = lo <= tp2

            timeout = (i - t["entry_i"]) >= TIMEOUT_CANDLES
            reason = exit_px = None

            if hit_tp2:   exit_px, reason = tp2,       "TP2"
            elif hit_sl:  exit_px, reason = t["sl_px"], "SL"
            elif timeout: exit_px, reason = cl,         "TIMEOUT"

            if reason:
                pnl     = calc_pnl(side, t["entry_px"], t["sl_orig"],
                                   t["tp1_hit"], exit_px, reason, t["notional"])
                balance += pnl
                balance  = max(balance, 0.01)
                t.update({"exit_ts": ts_now, "exit_px": exit_px,
                           "exit_reason": reason, "pnl": pnl,
                           "win": pnl > 0, "balance_after": balance})
                closed.append(t)
            else:
                still_open.append(t)

        open_trades = still_open
        equity_ts.append(ts_now)
        equity_val.append(balance)

        # ── Scan for signal ───────────────────────────────────────────────────
        if len(open_trades) >= MAX_POSITIONS:
            continue

        slice_p = df_full.iloc[max(0, i - WARMUP): i + 1]
        try:
            sig, _ = SCANNER.scan_symbol(SYMBOL, slice_p, pd.DataFrame(), "FUTURES")
        except Exception:
            continue
        if sig is None: continue

        ep   = sig["price"]
        sl   = sig["sl"]
        side = sig["side"]

        # ── Pyramid check ─────────────────────────────────────────────────────
        same = sym_trades(SYMBOL)
        if same:
            if len(same) >= MAX_PYRAMID:
                continue
            # เช็คเงื่อนไข pyramid
            existing   = same[0]
            entry_ex   = existing["entry_px"]
            side_ex    = existing["side"]
            score_trend = sig.get("score_trend", 0)
            if side_ex == "LONG":
                pnl_pct = (cl - entry_ex) / entry_ex * 100
            else:
                pnl_pct = (entry_ex - cl) / entry_ex * 100

            pyr_ok = (score_trend >= 10 and pnl_pct >= 0.0) or \
                     (score_trend >= 9  and pnl_pct > 1.0)
            if not pyr_ok:
                continue

        # ── Size & open ───────────────────────────────────────────────────────
        sized = calc_size(balance, ep, sl)
        if sized is None: continue
        notional, margin, risk_usd = sized

        dist = abs(ep - sl)
        tp1  = ep + dist * TP1_R if side == "LONG" else ep - dist * TP1_R
        tp2  = ep + dist * TP2_R if side == "LONG" else ep - dist * TP2_R

        open_trades.append({
            "symbol":    SYMBOL,
            "entry_i":   i,
            "entry_ts":  ts_now,
            "entry_px":  ep,
            "sl_px":     sl,
            "sl_orig":   sl,
            "tp1_px":    tp1,
            "tp2_px":    tp2,
            "side":      side,
            "notional":  notional,
            "margin":    margin,
            "risk_usd":  risk_usd,
            "tp1_hit":   False,
            "pyramid":   len(same) + 1,
            "score_trend": sig.get("score_trend", 0),
        })

    return closed, equity_ts, equity_val

# ── Chart ─────────────────────────────────────────────────────────────────────
def plot(df_full, trades, equity_ts, equity_val):
    if not trades:
        print("No trades"); return

    tdf = pd.DataFrame(trades)
    tdf["win_int"] = tdf["win"].astype(int)
    tdf["roll_wr"] = tdf["win_int"].rolling(ROLLING_WIN, min_periods=5).mean() * 100
    tdf["cum_pnl"] = tdf["pnl"].cumsum()

    c     = df_full["close"]
    ema7  = EMAIndicator(c, 7).ema_indicator()
    ema30 = EMAIndicator(c, 30).ema_indicator()
    sma99 = SMAIndicator(c, 99).sma_indicator()

    wins   = tdf["win"].sum()
    losses = len(tdf) - wins
    wr     = wins / len(tdf) * 100
    final_bal = equity_val[-1]
    ret_pct   = (final_bal - START_BALANCE) / START_BALANCE * 100
    sv = tdf["pnl"].values
    sharpe = np.mean(sv) / np.std(sv) * np.sqrt(len(sv)) if np.std(sv) > 0 else 0
    max_eq = pd.Series(equity_val)
    max_dd = ((max_eq.cummax() - max_eq) / max_eq.cummax()).max() * 100

    fig = plt.figure(figsize=(22, 16), facecolor="#0d1117")
    gs  = gridspec.GridSpec(4, 1, figure=fig,
                            height_ratios=[4, 1.5, 1.5, 1], hspace=0.07)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    ax4 = fig.add_subplot(gs[3], sharex=ax1)

    for ax in [ax1, ax2, ax3, ax4]:
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e", labelsize=8)
        ax.spines[:].set_color("#30363d")

    x = df_full.index

    # Panel 1 — Price + indicators + trades
    ax1.plot(x, c,     color="#58a6ff", lw=0.7, alpha=0.6, label="BTC")
    ax1.plot(x, ema7,  color="#f0883e", lw=1.0, label="EMA7")
    ax1.plot(x, ema30, color="#3fb950", lw=1.0, label="EMA30")
    ax1.plot(x, sma99, color="#bc8cff", lw=1.4, ls="--", label="SMA99")

    for _, t in tdf.iterrows():
        col = "#3fb950" if t["win"] else "#f85149"
        mk  = "^" if t["side"] == "LONG" else "v"
        sz  = 90 if t.get("pyramid", 1) > 1 else 55
        ax1.scatter(t["entry_ts"], t["entry_px"], marker=mk, color=col, s=sz, zorder=5, alpha=0.85)
        ax1.scatter(t["exit_ts"],  t["exit_px"],  marker="x", color=col, s=40, zorder=5, alpha=0.7)

    ax1.set_title(
        f"BTC/USDT 30m — Full System (TF ใครTF) | 1 Year | Lev{LEVERAGE}x | Risk 1%/trade\n"
        f"Trades: {len(tdf)}  WR: {wr:.1f}% ({int(wins)}W/{int(losses)}L)  "
        f"Start: ${START_BALANCE:,.0f} → End: ${final_bal:,.2f}  "
        f"Return: {ret_pct:+.1f}%  MaxDD: {max_dd:.1f}%  Sharpe: {sharpe:.2f}",
        color="white", fontsize=11, pad=8
    )
    ax1.legend(loc="upper left", fontsize=8, facecolor="#161b22",
               labelcolor="white", edgecolor="#30363d")
    ax1.yaxis.tick_right()
    ax1.set_ylabel("BTC Price", color="#8b949e", fontsize=8)

    # Panel 2 — Rolling Win Rate
    ax2.plot(tdf["exit_ts"], tdf["roll_wr"], color="#e3b341", lw=1.5,
             label=f"Rolling {ROLLING_WIN}-trade WR%")
    ax2.axhline(50, color="#8b949e", lw=0.8, ls="--")
    ax2.fill_between(tdf["exit_ts"], tdf["roll_wr"], 50,
                     where=tdf["roll_wr"] >= 50, color="#3fb950", alpha=0.2)
    ax2.fill_between(tdf["exit_ts"], tdf["roll_wr"], 50,
                     where=tdf["roll_wr"] < 50,  color="#f85149", alpha=0.2)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Win Rate %", color="#8b949e", fontsize=8)
    ax2.yaxis.tick_right()
    ax2.legend(loc="upper left", fontsize=7, facecolor="#161b22",
               labelcolor="white", edgecolor="#30363d")
    ax2.tick_params(labelbottom=False)

    # Panel 3 — Equity Curve
    eq_s = pd.Series(equity_val, index=equity_ts)
    ax3.plot(equity_ts, equity_val, color="#58a6ff", lw=1.5, label="Equity")
    ax3.axhline(START_BALANCE, color="#30363d", lw=0.8, ls="--")
    ax3.fill_between(equity_ts, equity_val, START_BALANCE,
                     where=[v >= START_BALANCE for v in equity_val],
                     color="#3fb950", alpha=0.2)
    ax3.fill_between(equity_ts, equity_val, START_BALANCE,
                     where=[v < START_BALANCE for v in equity_val],
                     color="#f85149", alpha=0.2)
    ax3.set_ylabel("Balance $", color="#8b949e", fontsize=8)
    ax3.yaxis.tick_right()
    ax3.legend(loc="upper left", fontsize=7, facecolor="#161b22",
               labelcolor="white", edgecolor="#30363d")
    ax3.tick_params(labelbottom=False)

    # Panel 4 — Per-trade PnL bar
    for _, t in tdf.iterrows():
        col = "#3fb950" if t["win"] else "#f85149"
        ax4.bar(t["exit_ts"], t["pnl"], color=col,
                width=pd.Timedelta(hours=6), alpha=0.8)
    ax4.axhline(0, color="#30363d", lw=0.8)
    ax4.set_ylabel("Trade PnL $", color="#8b949e", fontsize=8)
    ax4.yaxis.tick_right()

    ax1.set_xlim(df_full.index[WARMUP], df_full.index[-1])
    plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor="#0d1117")
    plt.close()
    print(f"Chart saved → {OUT_PNG}")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Fetching {SYMBOL} 30m ({DAYS} days)...")
    df = fetch_ohlcv(SYMBOL, "30m", DAYS)
    print(f"  {len(df):,} candles  ({df.index[0].date()} → {df.index[-1].date()})")

    print("Running realistic backtest (1% risk, Lev20x, pyramid)...")
    t0 = time_mod.time()
    trades, eq_ts, eq_val = backtest(df)
    elapsed = time_mod.time() - t0

    tdf  = pd.DataFrame(trades)
    wins = tdf["win"].sum() if not tdf.empty else 0
    wr   = wins / len(tdf) * 100 if not tdf.empty else 0
    final_bal = eq_val[-1]
    ret   = (final_bal - START_BALANCE) / START_BALANCE * 100
    sv    = tdf["pnl"].values if not tdf.empty else [0]
    sharpe = np.mean(sv) / np.std(sv) * np.sqrt(len(sv)) if np.std(sv) > 0 else 0
    eq_s   = pd.Series(eq_val)
    max_dd = ((eq_s.cummax() - eq_s) / eq_s.cummax()).max() * 100

    print(f"\n{'='*60}")
    print(f"  Elapsed     : {elapsed:.0f}s")
    print(f"  Trades      : {len(tdf)}")
    print(f"  Win Rate    : {wr:.1f}%  ({int(wins)}W / {len(tdf)-int(wins)}L)")
    print(f"  Start       : ${START_BALANCE:,.2f}")
    print(f"  End Balance : ${final_bal:,.2f}")
    print(f"  Return      : {ret:+.1f}%")
    print(f"  Max DrawDown: {max_dd:.1f}%")
    print(f"  Sharpe      : {sharpe:.2f}")
    print(f"{'='*60}")

    plot(df, trades, eq_ts, eq_val)
