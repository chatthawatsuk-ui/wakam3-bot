"""
backtest_portfolio.py — Multi-Symbol Portfolio Backtest (3Y)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Rules:
  - อ่านจาก historical_data/*.parquet (ไม่ fetch ใหม่)
  - 9 symbols: BTC ETH SOL BNB XRP ADA AVAX DOGE LINK
  - Balance เดียว $1,000 ร่วมกันทุก symbol
  - Risk per trade: 1% ของ balance ปัจจุบัน
  - Leverage: 20x
  - Max positions: 10 (รวมทุก symbol)
  - Pyramid: max 2 per symbol
  - TF: 30m gate = 30m (TF ใครTF)
  - Period: 3Y (2023-04 → 2026-04)
"""
import warnings; warnings.filterwarnings("ignore")
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from ta.trend import EMAIndicator, SMAIndicator

import signal_scanner as SCANNER
import agent_trend    as TREND

SCANNER.save_condition_snapshot = lambda *a, **kw: None
try: SCANNER.save_specialist_history = lambda *a, **kw: None
except: pass
SCANNER.DISABLE_FUNDING = True

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOGE", "LINK"]
DATA_DIR        = "historical_data"
WARMUP          = 250
START_BALANCE   = 1000.0
RISK_PCT        = 0.01
LEVERAGE        = 20
MAX_POSITIONS   = 10
MAX_PYRAMID     = 2
TP1_R           = 1.2
TP2_R           = 2.0
TIMEOUT_CANDLES = 96
ROLLING_WIN     = 20
OUT_PNG         = "backtest_portfolio.png"

# ── Patch HTF → 30m gate (TF ใครTF) ─────────────────────────────────────────
def _htf_30m(df_primary, df_htf):
    df = df_primary.copy()
    c  = df["close"]
    df["htf_bull"] = EMAIndicator(c, TREND.EMA_FAST).ema_indicator() > \
                     EMAIndicator(c, TREND.EMA_SLOW).ema_indicator()
    df["htf_sma"]  = c > SMAIndicator(c, TREND.SMA_99).sma_indicator()
    return df

TREND._htf_bias = _htf_30m

# ── Load parquet ──────────────────────────────────────────────────────────────
def load_data():
    data = {}
    for sym in SYMBOLS:
        path = os.path.join(DATA_DIR, f"{sym}_USDT_30m.parquet")
        if not os.path.exists(path):
            print(f"[WARN] ไม่พบ {path} — ข้าม")
            continue
        df = pd.read_parquet(path)
        df = df[~df.index.duplicated()].sort_index()
        data[sym] = df
        print(f"  {sym:6} {len(df):>6} candles  {df.index[0].date()} → {df.index[-1].date()}")
    return data

# ── Align timestamps ──────────────────────────────────────────────────────────
def align_timestamps(data):
    """หา index ที่ทุก symbol มีข้อมูลพร้อมกัน"""
    common = None
    for df in data.values():
        common = df.index if common is None else common.intersection(df.index)
    return sorted(common)

# ── Position sizing ───────────────────────────────────────────────────────────
def calc_size(balance, entry_px, sl_px):
    sl_pct = abs(entry_px - sl_px) / entry_px
    if sl_pct < 0.001: return None
    risk_usd = balance * RISK_PCT
    notional = risk_usd / sl_pct
    margin   = notional / LEVERAGE
    if margin > balance * 0.5: return None
    return notional, margin, risk_usd

# ── PnL calculator ────────────────────────────────────────────────────────────
def calc_pnl(side, entry_px, sl_orig, tp1_hit, exit_px, exit_reason, notional):
    sl_pct  = abs(entry_px - sl_orig) / entry_px
    risk    = notional * sl_pct
    tp1_pnl = notional * sl_pct * TP1_R
    tp2_pnl = notional * sl_pct * TP2_R

    if exit_reason == "TP2":
        return 0.5 * tp1_pnl + 0.5 * tp2_pnl
    if exit_reason == "SL":
        return 0.5 * tp1_pnl if tp1_hit else -risk
    r_val = (exit_px - entry_px) / entry_px if side == "LONG" \
            else (entry_px - exit_px) / entry_px
    return notional * r_val

# ── Portfolio Backtest ────────────────────────────────────────────────────────
def backtest(data, timestamps):
    balance     = START_BALANCE
    open_trades = []
    closed      = []
    equity_ts   = [timestamps[WARMUP]]
    equity_val  = [balance]

    def open_by_sym(sym):
        return [t for t in open_trades if t["symbol"] == sym]

    total = len(timestamps)
    for i in range(WARMUP, total):
        ts_now = timestamps[i]

        # ── Update open trades ────────────────────────────────────────────────
        still_open = []
        for t in open_trades:
            sym  = t["symbol"]
            side = t["side"]
            df   = data[sym]
            if ts_now not in df.index:
                still_open.append(t); continue

            row = df.loc[ts_now]
            hi, lo, cl = row["high"], row["low"], row["close"]

            if side == "LONG":
                if not t["tp1_hit"] and hi >= t["tp1_px"]:
                    t["tp1_hit"] = True
                    t["sl_px"]   = t["entry_px"]
                hit_sl  = lo <= t["sl_px"]
                hit_tp2 = hi >= t["tp2_px"]
            else:
                if not t["tp1_hit"] and lo <= t["tp1_px"]:
                    t["tp1_hit"] = True
                    t["sl_px"]   = t["entry_px"]
                hit_sl  = hi >= t["sl_px"]
                hit_tp2 = lo <= t["tp2_px"]

            timeout = (i - t["entry_i"]) >= TIMEOUT_CANDLES
            reason = exit_px = None

            if hit_tp2:   exit_px, reason = t["tp2_px"],  "TP2"
            elif hit_sl:  exit_px, reason = t["sl_px"],   "SL"
            elif timeout: exit_px, reason = cl,            "TIMEOUT"

            if reason:
                pnl = calc_pnl(side, t["entry_px"], t["sl_orig"],
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

        # ── Scan all symbols for signals ──────────────────────────────────────
        if len(open_trades) >= MAX_POSITIONS:
            continue

        candidates = []
        for sym in SYMBOLS:
            df = data[sym]
            # หา index position ของ ts_now ใน df นี้
            try:
                idx = df.index.get_loc(ts_now)
            except KeyError:
                continue
            if idx < WARMUP:
                continue

            slice_p = df.iloc[max(0, idx - WARMUP): idx + 1]
            try:
                sig, _ = SCANNER.scan_symbol(
                    f"{sym}/USDT", slice_p, pd.DataFrame(), "FUTURES"
                )
            except Exception:
                continue
            if sig is None:
                continue

            # Pyramid check
            same = open_by_sym(sym)
            if same:
                if len(same) >= MAX_PYRAMID:
                    continue
                existing    = same[0]
                entry_ex    = existing["entry_px"]
                side_ex     = existing["side"]
                score_trend = sig.get("score_trend", 0)
                cl_now      = df.loc[ts_now, "close"]
                if side_ex == "LONG":
                    pnl_pct = (cl_now - entry_ex) / entry_ex * 100
                else:
                    pnl_pct = (entry_ex - cl_now) / entry_ex * 100
                pyr_ok = (score_trend >= 10 and pnl_pct >= 0.0) or \
                         (score_trend >= 9  and pnl_pct > 1.0)
                if not pyr_ok:
                    continue

            candidates.append((sig.get("score", 0), sym, sig))

        # sort by score — เปิด signal ที่ดีที่สุดก่อน
        candidates.sort(key=lambda x: x[0], reverse=True)

        for _, sym, sig in candidates:
            if len(open_trades) >= MAX_POSITIONS:
                break

            ep   = sig["price"]
            sl   = sig["sl"]
            side = sig["side"]
            sized = calc_size(balance, ep, sl)
            if sized is None:
                continue
            notional, margin, risk_usd = sized

            dist = abs(ep - sl)
            tp1  = ep + dist * TP1_R if side == "LONG" else ep - dist * TP1_R
            tp2  = ep + dist * TP2_R if side == "LONG" else ep - dist * TP2_R

            open_trades.append({
                "symbol":      sym,
                "entry_i":     i,
                "entry_ts":    ts_now,
                "entry_px":    ep,
                "sl_px":       sl,
                "sl_orig":     sl,
                "tp1_px":      tp1,
                "tp2_px":      tp2,
                "side":        side,
                "notional":    notional,
                "margin":      margin,
                "risk_usd":    risk_usd,
                "tp1_hit":     False,
                "pyramid":     len(open_by_sym(sym)) + 1,
                "score_trend": sig.get("score_trend", 0),
                "score":       sig.get("score", 0),
            })

        if i % 5000 == 0:
            pct = (i - WARMUP) / (total - WARMUP) * 100
            print(f"  [{pct:5.1f}%] candle {i}/{total}  balance=${balance:,.2f}"
                  f"  open={len(open_trades)}  closed={len(closed)}")

    return closed, equity_ts, equity_val

# ── Chart ─────────────────────────────────────────────────────────────────────
def plot(data, trades, equity_ts, equity_val):
    if not trades:
        print("No trades"); return

    tdf = pd.DataFrame(trades)
    tdf["win_int"] = tdf["win"].astype(int)
    tdf["roll_wr"] = tdf["win_int"].rolling(ROLLING_WIN, min_periods=5).mean() * 100

    wins   = tdf["win"].sum()
    losses = len(tdf) - wins
    wr     = wins / len(tdf) * 100
    final_bal = equity_val[-1]
    ret_pct   = (final_bal - START_BALANCE) / START_BALANCE * 100
    sv = tdf["pnl"].values
    sharpe = np.mean(sv) / np.std(sv) * np.sqrt(len(sv)) if np.std(sv) > 0 else 0
    eq_s   = pd.Series(equity_val)
    max_dd = ((eq_s.cummax() - eq_s) / eq_s.cummax()).max() * 100

    # symbol breakdown
    sym_stats = tdf.groupby("symbol").agg(
        trades=("win", "count"),
        wins=("win", "sum"),
        pnl=("pnl", "sum")
    ).sort_values("pnl", ascending=False)

    print("\n── Symbol Breakdown ──────────────────────────")
    for sym, row in sym_stats.iterrows():
        wr_s = row["wins"] / row["trades"] * 100
        print(f"  {sym:6}  {int(row['trades']):>4} trades  WR {wr_s:.0f}%  PnL ${row['pnl']:+.2f}")
    print(f"  {'TOTAL':6}  {len(tdf):>4} trades  WR {wr:.1f}%  PnL ${final_bal-START_BALANCE:+.2f}")

    # ── BTC price for reference ───────────────────────────────────────────────
    btc = data["BTC"]
    c   = btc["close"]
    ema7  = EMAIndicator(c, 7).ema_indicator()
    ema30 = EMAIndicator(c, 30).ema_indicator()
    sma99 = SMAIndicator(c, 99).sma_indicator()

    COLORS = {
        "BTC":"#f7931a","ETH":"#627eea","SOL":"#9945ff","BNB":"#f3ba2f",
        "XRP":"#00aae4","ADA":"#0033ad","AVAX":"#e84142","DOGE":"#c2a633","LINK":"#2a5ada"
    }

    fig = plt.figure(figsize=(24, 18), facecolor="#0d1117")
    gs  = gridspec.GridSpec(4, 1, figure=fig,
                            height_ratios=[4, 1.5, 1.5, 1.2], hspace=0.08)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2])
    ax4 = fig.add_subplot(gs[3])

    for ax in [ax1, ax2, ax3, ax4]:
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e", labelsize=8)
        ax.spines[:].set_color("#30363d")

    # Panel 1 — BTC price + trade markers per symbol
    ax1.plot(btc.index, c,     color="#58a6ff", lw=0.7, alpha=0.5, label="BTC")
    ax1.plot(btc.index, ema7,  color="#f0883e", lw=1.0, label="EMA7")
    ax1.plot(btc.index, ema30, color="#3fb950", lw=1.0, label="EMA30")
    ax1.plot(btc.index, sma99, color="#bc8cff", lw=1.4, ls="--", label="SMA99")

    for _, t in tdf.iterrows():
        sym = t["symbol"]
        col = COLORS.get(sym, "#ffffff")
        mk  = "^" if t["side"] == "LONG" else "v"
        # map entry time to BTC price for visual reference
        if t["entry_ts"] in btc.index:
            ref_px = btc.loc[t["entry_ts"], "close"]
            ax1.scatter(t["entry_ts"], ref_px,
                       marker=mk, color=col, s=50, zorder=5, alpha=0.7)

    ax1.set_title(
        f"Portfolio 9-Symbol 30m — TF ใครTF | 3Y | Lev{LEVERAGE}x | Risk 1%/trade\n"
        f"Trades: {len(tdf)}  WR: {wr:.1f}% ({int(wins)}W/{int(losses)}L)  "
        f"$1,000 → ${final_bal:,.2f}  Return: {ret_pct:+.1f}%  "
        f"MaxDD: {max_dd:.1f}%  Sharpe: {sharpe:.2f}",
        color="white", fontsize=11, pad=8
    )
    ax1.legend(loc="upper left", fontsize=8, facecolor="#161b22",
               labelcolor="white", edgecolor="#30363d")
    ax1.yaxis.tick_right()
    ax1.set_ylabel("BTC Price (ref)", color="#8b949e", fontsize=8)

    # Panel 2 — Rolling WR
    ax2.plot(tdf["exit_ts"], tdf["roll_wr"], color="#e3b341", lw=1.5,
             label=f"Rolling {ROLLING_WIN}-trade WR%")
    ax2.axhline(50, color="#8b949e", lw=0.8, ls="--")
    ax2.fill_between(tdf["exit_ts"], tdf["roll_wr"], 50,
                     where=tdf["roll_wr"] >= 50, color="#3fb950", alpha=0.2)
    ax2.fill_between(tdf["exit_ts"], tdf["roll_wr"], 50,
                     where=tdf["roll_wr"] < 50,  color="#f85149", alpha=0.2)
    ax2.set_ylabel("Win Rate %", color="#8b949e", fontsize=8)
    ax2.yaxis.tick_right()
    ax2.legend(loc="upper left", fontsize=8, facecolor="#161b22",
               labelcolor="white", edgecolor="#30363d")

    # Panel 3 — Equity curve
    eq_ts  = pd.Series(equity_ts)
    eq_val = pd.Series(equity_val)
    ax3.plot(eq_ts, eq_val, color="#58a6ff", lw=1.5, label="Portfolio Balance")
    ax3.axhline(START_BALANCE, color="#8b949e", lw=0.8, ls="--")
    ax3.fill_between(eq_ts, eq_val, START_BALANCE,
                     where=eq_val >= START_BALANCE, color="#3fb950", alpha=0.15)
    ax3.fill_between(eq_ts, eq_val, START_BALANCE,
                     where=eq_val < START_BALANCE,  color="#f85149", alpha=0.15)
    ax3.set_ylabel("Balance ($)", color="#8b949e", fontsize=8)
    ax3.yaxis.tick_right()
    ax3.legend(loc="upper left", fontsize=8, facecolor="#161b22",
               labelcolor="white", edgecolor="#30363d")

    # Panel 4 — PnL per symbol bar
    sym_order = sym_stats.index.tolist()
    bar_cols  = ["#3fb950" if sym_stats.loc[s,"pnl"] >= 0 else "#f85149" for s in sym_order]
    ax4.bar(sym_order, sym_stats["pnl"], color=bar_cols, alpha=0.85)
    ax4.axhline(0, color="#8b949e", lw=0.8)
    ax4.set_ylabel("PnL ($)", color="#8b949e", fontsize=8)
    ax4.yaxis.tick_right()
    for sym, val in zip(sym_order, sym_stats["pnl"]):
        ax4.text(sym, val + (2 if val >= 0 else -6), f"${val:+.0f}",
                 color="white", fontsize=7, ha="center")

    plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor="#0d1117")
    print(f"\nบันทึก → {OUT_PNG}")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("PORTFOLIO BACKTEST — 9 Symbols 30m 3Y")
    print("=" * 55)

    print("\nโหลด parquet...")
    data = load_data()
    if not data:
        print("[ERR] ไม่มีข้อมูล"); sys.exit(1)

    print("\nAlign timestamps...")
    timestamps = align_timestamps(data)
    print(f"  {len(timestamps)} common candles  "
          f"{timestamps[0].date()} → {timestamps[-1].date()}")

    print(f"\nรัน backtest ({len(timestamps)-WARMUP:,} candles × {len(data)} symbols)...")
    trades, equity_ts, equity_val = backtest(data, timestamps)

    if not trades:
        print("ไม่มี trade"); sys.exit(0)

    tdf = pd.DataFrame(trades)
    wins = tdf["win"].sum()
    wr   = wins / len(tdf) * 100
    ret  = (equity_val[-1] - START_BALANCE) / START_BALANCE * 100
    print(f"\n── ผลสรุป ──────────────────────────────────")
    print(f"  Trades : {len(tdf)}")
    print(f"  WR     : {wr:.1f}%  ({int(wins)}W / {int(len(tdf)-wins)}L)")
    print(f"  Balance: ${START_BALANCE:,.0f} → ${equity_val[-1]:,.2f}  ({ret:+.1f}%)")
    sv = tdf["pnl"].values
    sharpe = np.mean(sv) / np.std(sv) * np.sqrt(len(sv)) if np.std(sv) > 0 else 0
    eq_s = pd.Series(equity_val)
    max_dd = ((eq_s.cummax() - eq_s) / eq_s.cummax()).max() * 100
    print(f"  Sharpe : {sharpe:.2f}")
    print(f"  MaxDD  : {max_dd:.1f}%")

    print("\nสร้าง chart...")
    plot(data, trades, equity_ts, equity_val)
