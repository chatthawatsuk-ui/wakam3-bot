"""
backtest_ema_simple.py — BTC/USDT 30m | EMA7/30 + SMA99 only
Entry : EMA7 cross EMA30 + price side of SMA99
SL    : entry ± ATR14
TP1   : 1.2R  |  TP2 : 2.0R  (50/50 split)
"""
import warnings; warnings.filterwarnings("ignore")
import sys; sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
import ccxt
from ta.trend      import EMAIndicator, SMAIndicator
from ta.volatility import AverageTrueRange

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL   = "BTC/USDT"
DAYS     = 60
TF       = "30m"
RISK_USD = 10.0
TP1_R    = 1.2
TP2_R    = 2.0
TIMEOUT_CANDLES = 96   # 48h ใน 30m

# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch(symbol, tf, days):
    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    limit  = int(days * 24 * 60 / 30) + 300
    since  = int(ex.milliseconds() - days * 86400 * 1000)
    rows   = []
    while True:
        chunk = ex.fetch_ohlcv(symbol, tf, since=since, limit=300)
        if not chunk: break
        rows.extend(chunk)
        if len(chunk) < 300: break
        since = chunk[-1][0] + 1
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df = df[~df.index.duplicated()].sort_index()
    return df.tail(limit)

# ── Indicators ────────────────────────────────────────────────────────────────
def add_indicators(df):
    c, h, l = df["close"], df["high"], df["low"]
    df = df.copy()
    df["ema7"]  = EMAIndicator(c, 7).ema_indicator()
    df["ema30"] = EMAIndicator(c, 30).ema_indicator()
    df["sma99"] = SMAIndicator(c, 99).sma_indicator()
    df["atr14"] = AverageTrueRange(h, l, c, 14).average_true_range()
    df["bull"]     = df["ema7"] > df["ema30"]
    df["cross_up"] = (~df["bull"].shift(1).fillna(False)) & df["bull"]
    df["cross_dn"] = df["bull"].shift(1).fillna(False) & (~df["bull"])
    return df.dropna()

# ── Backtest ──────────────────────────────────────────────────────────────────
def run_backtest(df):
    trades = []
    in_trade = False
    entry_i = entry_px = sl_px = tp1_px = tp2_px = side = None
    tp1_hit = False

    for i in range(1, len(df)):
        r = df.iloc[i]

        # ── manage open trade ────────────────────────────────────────────────
        if in_trade:
            hi, lo, cl = r["high"], r["low"], r["close"]
            timeout = (i - entry_i) >= TIMEOUT_CANDLES

            if side == "LONG":
                if not tp1_hit and hi >= tp1_px:
                    tp1_hit = True
                    sl_px   = entry_px   # move SL to BE
                hit_sl = lo <= sl_px
                hit_tp2 = hi >= tp2_px
            else:
                if not tp1_hit and lo <= tp1_px:
                    tp1_hit = True
                    sl_px   = entry_px
                hit_sl  = hi >= sl_px
                hit_tp2 = lo <= tp2_px

            exit_px = exit_reason = None
            if hit_tp2:
                exit_px = tp2_px; exit_reason = "TP2"
            elif hit_sl:
                exit_px = sl_px;  exit_reason = "SL"
            elif timeout:
                exit_px = cl;     exit_reason = "TIMEOUT"

            if exit_px:
                dist = abs(entry_px - (sl_px if not tp1_hit else entry_px))
                dist_orig = abs(entry_px - trades[-1]["sl_orig"])
                if side == "LONG":
                    r1 = (exit_px - entry_px) / dist_orig if dist_orig > 0 else 0
                else:
                    r1 = (entry_px - exit_px) / dist_orig if dist_orig > 0 else 0

                if tp1_hit and exit_reason == "TP2":
                    pnl = RISK_USD * (0.5 * TP1_R + 0.5 * TP2_R)
                elif tp1_hit and exit_reason == "SL":
                    pnl = RISK_USD * 0.5 * TP1_R
                elif exit_reason == "TP2":
                    pnl = RISK_USD * (0.5 * TP1_R + 0.5 * TP2_R)
                elif exit_reason == "SL":
                    pnl = -RISK_USD
                else:
                    pnl = RISK_USD * r1

                trades[-1].update({
                    "exit_i": i, "exit_px": exit_px, "exit_ts": df.index[i],
                    "exit_reason": exit_reason, "pnl": pnl,
                    "win": pnl > 0
                })
                in_trade = False
            continue

        # ── look for new entry ───────────────────────────────────────────────
        atr = r["atr14"]
        if atr <= 0: continue

        if r["cross_up"] and r["close"] > r["sma99"]:
            side     = "LONG"
            entry_px = r["close"]
            sl_px    = entry_px - atr
            dist     = atr
        elif r["cross_dn"] and r["close"] < r["sma99"]:
            side     = "SHORT"
            entry_px = r["close"]
            sl_px    = entry_px + atr
            dist     = atr
        else:
            continue

        sl_pct = dist / entry_px * 100
        if sl_pct < 0.3: continue   # SL ใกล้เกิน

        tp1_px   = entry_px + dist * TP1_R if side == "LONG" else entry_px - dist * TP1_R
        tp2_px   = entry_px + dist * TP2_R if side == "LONG" else entry_px - dist * TP2_R
        in_trade = True
        tp1_hit  = False
        entry_i  = i
        trades.append({
            "entry_i": i, "entry_px": entry_px, "entry_ts": df.index[i],
            "side": side, "sl_px": sl_px, "sl_orig": sl_px,
            "tp1_px": tp1_px, "tp2_px": tp2_px,
            "exit_i": None, "exit_px": None, "exit_ts": None,
            "exit_reason": None, "pnl": None, "win": None
        })

    return [t for t in trades if t["exit_i"] is not None]

# ── Chart ─────────────────────────────────────────────────────────────────────
def plot_chart(df, trades):
    fig = plt.figure(figsize=(20, 12), facecolor="#0d1117")
    gs  = GridSpec(3, 1, figure=fig, height_ratios=[4, 1, 1], hspace=0.08)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)

    for ax in [ax1, ax2, ax3]:
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e", labelsize=8)
        ax.spines[:].set_color("#30363d")

    x  = np.arange(len(df))
    ts = df.index

    # ── Price + Candles (sampled for speed) ──────────────────────────────────
    ax1.plot(x, df["close"].values, color="#58a6ff", linewidth=0.8, alpha=0.6, label="BTC Close")
    ax1.plot(x, df["ema7"].values,  color="#f0883e", linewidth=1.2, label="EMA7")
    ax1.plot(x, df["ema30"].values, color="#3fb950", linewidth=1.2, label="EMA30")
    ax1.plot(x, df["sma99"].values, color="#bc8cff", linewidth=1.5, linestyle="--", label="SMA99")

    # ── Bull/Bear background zones ────────────────────────────────────────────
    bull_mask = df["bull"].values
    for i in range(len(df) - 1):
        if bull_mask[i]:
            ax1.axvspan(x[i], x[i+1], alpha=0.03, color="#3fb950", linewidth=0)
        else:
            ax1.axvspan(x[i], x[i+1], alpha=0.03, color="#f85149", linewidth=0)

    # ── Trade markers ─────────────────────────────────────────────────────────
    wins = losses = 0
    cum_pnl = [0]
    pnl_x   = [0]

    for t in trades:
        ei = t["entry_i"]
        xi = t["exit_i"]
        ep = t["entry_px"]
        xp = t["exit_px"]
        won = t["win"]
        if won: wins += 1
        else:   losses += 1

        col = "#3fb950" if won else "#f85149"
        mk_entry = "^" if t["side"] == "LONG" else "v"

        ax1.scatter(x[ei], ep, marker=mk_entry, color=col, s=80, zorder=5)
        ax1.scatter(x[xi], xp, marker="x",      color=col, s=60, zorder=5)
        ax1.plot([x[ei], x[xi]], [ep, xp], color=col, linewidth=0.5, alpha=0.4)

        cum_pnl.append(cum_pnl[-1] + t["pnl"])
        pnl_x.append(x[xi])

    total   = wins + losses
    wr      = wins / total * 100 if total else 0
    total_pnl = cum_pnl[-1]

    ax1.set_title(
        f"BTC/USDT 30m — EMA7/30 + SMA99 Only  |  {DAYS} Days\n"
        f"Trades: {total}  |  Win Rate: {wr:.1f}%  |  "
        f"Wins: {wins}  Losses: {losses}  |  Total PnL: ${total_pnl:+.2f}",
        color="white", fontsize=12, pad=10
    )
    ax1.legend(loc="upper left", fontsize=8, facecolor="#161b22",
               labelcolor="white", edgecolor="#30363d")
    ax1.yaxis.set_label_position("right"); ax1.yaxis.tick_right()
    ax1.set_ylabel("Price (USDT)", color="#8b949e", fontsize=8)

    # ── Cumulative PnL ────────────────────────────────────────────────────────
    ax2.plot(pnl_x, cum_pnl, color="#58a6ff", linewidth=1.5)
    ax2.fill_between(pnl_x, cum_pnl, 0,
                     where=[p >= 0 for p in cum_pnl],
                     color="#3fb950", alpha=0.3)
    ax2.fill_between(pnl_x, cum_pnl, 0,
                     where=[p < 0 for p in cum_pnl],
                     color="#f85149", alpha=0.3)
    ax2.axhline(0, color="#30363d", linewidth=0.8)
    ax2.set_ylabel("PnL $", color="#8b949e", fontsize=8)
    ax2.yaxis.set_label_position("right"); ax2.yaxis.tick_right()
    ax2.tick_params(labelbottom=False)

    # ── Win/Loss bar per trade ────────────────────────────────────────────────
    for t in trades:
        col = "#3fb950" if t["win"] else "#f85149"
        ax3.bar(x[t["exit_i"]], t["pnl"], color=col, width=2, alpha=0.8)
    ax3.axhline(0, color="#30363d", linewidth=0.8)
    ax3.set_ylabel("Trade PnL $", color="#8b949e", fontsize=8)
    ax3.yaxis.set_label_position("right"); ax3.yaxis.tick_right()

    # ── X-axis labels (monthly) ───────────────────────────────────────────────
    step = max(1, len(df) // 10)
    xticks = x[::step]
    xlabels = [ts[i].strftime("%b %d") for i in range(0, len(df), step)]
    ax3.set_xticks(xticks); ax3.set_xticklabels(xlabels, rotation=0, fontsize=7)
    ax1.set_xlim(0, len(df) - 1)

    out = "backtest_ema_chart.png"
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="#0d1117")
    plt.close()
    print(f"Chart saved → {out}")
    return out

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Fetching {SYMBOL} {TF} ({DAYS} days)...")
    df = fetch(SYMBOL, TF, DAYS)
    print(f"  {len(df):,} candles")

    df = add_indicators(df)
    trades = run_backtest(df)

    total  = len(trades)
    wins   = sum(1 for t in trades if t["win"])
    wr     = wins / total * 100 if total else 0
    pnl    = sum(t["pnl"] for t in trades)
    sharpe_vals = [t["pnl"] for t in trades]
    sharpe = (np.mean(sharpe_vals) / np.std(sharpe_vals) * np.sqrt(total)
              if len(sharpe_vals) > 1 and np.std(sharpe_vals) > 0 else 0)

    print(f"\n{'='*50}")
    print(f"  Trades   : {total}")
    print(f"  Win Rate : {wr:.1f}%  ({wins}W / {total-wins}L)")
    print(f"  Total PnL: ${pnl:+.2f}")
    print(f"  Sharpe   : {sharpe:.2f}")
    print(f"{'='*50}")

    plot_chart(df, trades)
