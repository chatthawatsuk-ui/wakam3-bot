import sys, os, sqlite3, warnings
import pandas as pd
import numpy as np
warnings.filterwarnings("ignore")

# macOS path fix
for _p in [
    os.path.expanduser("~/Library/Python/3.9/lib/python/site-packages"),
    os.path.expanduser("~/Library/Python/3.10/lib/python/site-packages"),
    os.path.expanduser("~/Library/Python/3.11/lib/python/site-packages"),
]:
    if os.path.exists(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from ta.momentum   import RSIIndicator, StochasticOscillator
from ta.trend      import EMAIndicator, SMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ── CONFIG ────────────────────────────────────────────────────────────────────
DB_PATH   = "trade_data.db"
SYMBOLS   = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TF_1H     = "1h"
TF_4H     = "4h"

EMA_FAST  = 7
EMA_SLOW  = 30
SMA_TREND = 99
SWING_LB  = 10
RETRACE   = 0.003

TP1_R     = 1.2    # Order 1 — ปิด 50%
TP2_R     = 2.0    # Order 2 — ลดจาก 3.1 → 2.0 (ทาง A)
MIN_SCORE = 8

# ── LOAD ──────────────────────────────────────────────────────────────────────
def load(symbol, tf):
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql(
            f"SELECT ts,open,high,low,close,volume FROM ohlcv "
            f"WHERE symbol='{symbol}' AND timeframe='{tf}' ORDER BY ts", conn)
        conn.close()
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    return df[["open","high","low","close","volume"]].astype(float)

# ── INDICATORS ────────────────────────────────────────────────────────────────
def indicators(df):
    c, h, l = df["close"], df["high"], df["low"]

    df["ema7"]  = EMAIndicator(c, EMA_FAST).ema_indicator()
    df["ema30"] = EMAIndicator(c, EMA_SLOW).ema_indicator()
    df["sma99"] = SMAIndicator(c, SMA_TREND).sma_indicator()

    df["bull"]       = df["ema7"] > df["ema30"]
    df["cross_up"]   = (~df["bull"].shift(1).fillna(False)) & df["bull"]
    df["cross_dn"]   = df["bull"].shift(1).fillna(False) & (~df["bull"])
    df["touch_bull"] = (c >= df["ema30"]*(1-RETRACE)) & (c <= df["ema30"]*(1+RETRACE*2)) & df["bull"]
    df["touch_bear"] = (c <= df["ema30"]*(1+RETRACE)) & (c >= df["ema30"]*(1-RETRACE*2)) & (~df["bull"])

    df["sh"] = h.rolling(SWING_LB*2+1, center=True).max()
    df["sl"] = l.rolling(SWING_LB*2+1, center=True).min()
    df["psh"] = df["sh"].shift(SWING_LB)
    df["psl"] = df["sl"].shift(SWING_LB)

    df["bos_bull"]   = c > df["psh"]
    df["bos_bear"]   = c < df["psl"]
    df["choch_bull"] = (~df["bos_bull"].shift(3).fillna(False)) & df["bos_bull"] & (~df["bull"].shift(5).fillna(True))
    df["choch_bear"] = (~df["bos_bear"].shift(3).fillna(False)) & df["bos_bear"] & df["bull"].shift(5).fillna(False)

    df["hh"] = df["sh"] > df["sh"].shift(SWING_LB)
    df["hl"] = df["sl"] > df["sl"].shift(SWING_LB)
    df["lh"] = df["sh"] < df["sh"].shift(SWING_LB)
    df["ll"] = df["sl"] < df["sl"].shift(SWING_LB)
    df["qm_bull"] = df["lh"].shift(2) & df["hl"]
    df["qm_bear"] = df["hl"].shift(2) & df["lh"]

    df["rsi"] = RSIIndicator(c, 14).rsi()
    df["rsi_os"] = df["rsi"] < 40
    df["rsi_ob"] = df["rsi"] > 60
    df["rsi_bull_div"] = (c < c.shift(5)) & (df["rsi"] > df["rsi"].shift(5)) & (df["rsi"] < 50)
    df["rsi_bear_div"] = (c > c.shift(5)) & (df["rsi"] < df["rsi"].shift(5)) & (df["rsi"] > 50)

    st = StochasticOscillator(h, l, c, 14, 3)
    df["stk"] = st.stoch()
    df["std"] = st.stoch_signal()
    df["st_up"] = (df["stk"] > df["std"]) & (df["stk"].shift(1) <= df["std"].shift(1))
    df["st_dn"] = (df["stk"] < df["std"]) & (df["stk"].shift(1) >= df["std"].shift(1))

    m = MACD(c, 26, 12, 9)
    df["hist"]    = m.macd_diff()
    df["macd_up"] = (df["hist"] > 0) & (df["hist"].shift(1) <= 0)
    df["macd_dn"] = (df["hist"] < 0) & (df["hist"].shift(1) >= 0)

    df["atr"]  = AverageTrueRange(h, l, c, 14).average_true_range()
    df["hour"] = df.index.hour
    df["sess"] = "LATE"
    df.loc[(df["hour"]>=1)  & (df["hour"]<8),  "sess"] = "ASIA"
    df.loc[(df["hour"]>=8)  & (df["hour"]<13), "sess"] = "EUROPE"
    df.loc[(df["hour"]>=13) & (df["hour"]<21), "sess"] = "US"
    df["kz"] = ((df["hour"]>=7)&(df["hour"]<10)) | ((df["hour"]>=13)&(df["hour"]<16))

    return df.dropna()


def htf_bias(df1h, df4h):
    if df4h.empty:
        df1h["htf_bull"] = True
        df1h["htf_sma"]  = True
        return df1h
    df4h["h7"]  = EMAIndicator(df4h["close"], EMA_FAST).ema_indicator()
    df4h["h30"] = EMAIndicator(df4h["close"], EMA_SLOW).ema_indicator()
    df4h["h99"] = SMAIndicator(df4h["close"], SMA_TREND).sma_indicator()
    df4h["htf_bull"] = df4h["h7"] > df4h["h30"]
    df4h["htf_sma"]  = df4h["close"] > df4h["h99"]
    htf = df4h[["htf_bull","htf_sma"]].resample("1h").ffill()
    df1h = df1h.join(htf, how="left")
    df1h["htf_bull"] = df1h["htf_bull"].ffill().fillna(True)
    df1h["htf_sma"]  = df1h["htf_sma"].ffill().fillna(True)
    return df1h

# ── SIGNAL ────────────────────────────────────────────────────────────────────
def sig_long(r):
    if not (r["htf_bull"] and r["htf_sma"]):
        return False, 0
    s = 0
    if r["bull"]:          s += 2
    if r["cross_up"]:      s += 2
    if r["touch_bull"]:    s += 2
    if r["bos_bull"]:      s += 2
    if r["choch_bull"]:    s += 3
    if r["qm_bull"]:       s += 2
    if r["rsi_bull_div"]:  s += 3
    if r["rsi_os"]:        s += 2
    if r["st_up"]:         s += 2
    if r["macd_up"]:       s += 2
    if r["kz"]:            s += 1
    return s >= MIN_SCORE, s

def sig_short(r):
    if r["htf_bull"] or r["htf_sma"]:
        return False, 0
    s = 0
    if not r["bull"]:      s += 2
    if r["cross_dn"]:      s += 2
    if r["touch_bear"]:    s += 2
    if r["bos_bear"]:      s += 2
    if r["choch_bear"]:    s += 3
    if r["qm_bear"]:       s += 2
    if r["rsi_bear_div"]:  s += 3
    if r["rsi_ob"]:        s += 2
    if r["st_dn"]:         s += 2
    if r["macd_dn"]:       s += 2
    if r["kz"]:            s += 1
    return s >= MIN_SCORE, s

# ── BACKTEST (A+B) ────────────────────────────────────────────────────────────
def backtest(df, sym, use_trailing=True):
    """
    TP1 = RR 1.2 → ปิด 50% (ทาง A)
    TP2 = RR 2.0 → ปิด 50% แบบ fixed (ทาง A)
         หรือ Trailing ตาม EMA7 (ทาง B) ถ้า use_trailing=True
    """
    trades   = []
    in_trade = False
    ep = sl = tp1 = tp2 = 0.0
    side = ""
    tp1_hit = False
    trail_sl = 0.0
    meta = {}

    for i in range(1, len(df)):
        r = df.iloc[i]
        p = df.iloc[i-1]

        if in_trade:
            h_i, l_i = r["high"], r["low"]
            ema7_now  = r["ema7"]

            # ── Trailing SL update หลัง TP1 hit (ทาง B) ──────────────────
            if use_trailing and tp1_hit:
                if side == "LONG":
                    # trail SL ตาม EMA7 (เลื่อนขึ้นเรื่อยๆ)
                    trail_sl = max(trail_sl, ema7_now * 0.999)
                else:
                    trail_sl = min(trail_sl, ema7_now * 1.001)

            # ── Check SL ──────────────────────────────────────────────────
            active_sl = trail_sl if (use_trailing and tp1_hit) else sl
            hit_sl = (side=="LONG" and l_i <= active_sl) or \
                     (side=="SHORT" and h_i >= active_sl)

            # ── Check TP1 ─────────────────────────────────────────────────
            hit_tp1 = not tp1_hit and (
                (side=="LONG"  and h_i >= tp1) or
                (side=="SHORT" and l_i <= tp1)
            )

            # ── Check TP2 (fixed) ─────────────────────────────────────────
            hit_tp2 = tp1_hit and not use_trailing and (
                (side=="LONG"  and h_i >= tp2) or
                (side=="SHORT" and l_i <= tp2)
            )

            # ── Process ───────────────────────────────────────────────────
            if hit_tp1:
                tp1_hit  = True
                # เซต trailing SL เริ่มต้นที่ breakeven
                trail_sl = ep if side=="LONG" else ep

            if hit_sl:
                if tp1_hit:
                    # TP1 ได้แล้ว Order2 โดน SL/Trail
                    pnl = 10*0.5*TP1_R + (-10*0.5)  # O1 win, O2 BE/loss
                    # ถ้า trail_sl > ep = ได้กำไรบน O2
                    if side=="LONG"  and active_sl > ep:
                        pnl = 10*0.5*TP1_R + 10*0.5*((active_sl-ep)/abs(ep-sl))
                    elif side=="SHORT" and active_sl < ep:
                        pnl = 10*0.5*TP1_R + 10*0.5*((ep-active_sl)/abs(ep-sl))
                    outcome = "WIN" if pnl > 0 else "LOSS"
                else:
                    pnl     = -10
                    outcome = "LOSS"
                trades.append({**meta,"exit":r.name,"outcome":outcome,
                               "pnl":round(pnl,2),"tp1_hit":tp1_hit,
                               "exit_type":"SL"})
                in_trade = tp1_hit = False

            elif hit_tp2:
                pnl = 10*0.5*TP1_R + 10*0.5*TP2_R
                trades.append({**meta,"exit":r.name,"outcome":"WIN",
                               "pnl":round(pnl,2),"tp1_hit":True,
                               "exit_type":"TP2"})
                in_trade = tp1_hit = False

        # ── Find new signal ───────────────────────────────────────────────
        else:
            lo, ls = sig_long(p)
            so, ss = sig_short(p)
            if not (lo or so):
                continue
            if lo and (not so or ls >= ss):
                chosen, score = "LONG", ls
            else:
                chosen, score = "SHORT", ss

            ep_p = r["open"]
            atr  = p["atr"]
            sl_p = min(p["sl"], ep_p - atr) if chosen=="LONG" \
                   else max(p["sh"], ep_p + atr)
            dist = abs(ep_p - sl_p)
            if dist < ep_p * 0.001:
                continue

            tp1_p = ep_p + dist*TP1_R if chosen=="LONG" else ep_p - dist*TP1_R
            tp2_p = ep_p + dist*TP2_R if chosen=="LONG" else ep_p - dist*TP2_R

            in_trade = True
            tp1_hit  = False
            trail_sl = sl_p
            ep, sl, tp1, tp2, side = ep_p, sl_p, tp1_p, tp2_p, chosen
            meta = {
                "sym":   sym,
                "side":  chosen,
                "entry": r.name,
                "ep":    round(ep_p,4),
                "sl":    round(sl_p,4),
                "tp1":   round(tp1_p,4),
                "tp2":   round(tp2_p,4),
                "score": score,
                "sess":  p["sess"],
                "kz":    bool(p["kz"]),
                "rsi":   round(p["rsi"],1),
            }

    return pd.DataFrame(trades)

# ── METRICS ───────────────────────────────────────────────────────────────────
def metrics(t):
    if len(t) < 5:
        return None
    wins   = (t["outcome"]=="WIN")
    wr     = wins.mean()*100
    tp1r   = t["tp1_hit"].mean()*100
    total  = t["pnl"].sum()
    avg    = t["pnl"].mean()
    eq     = t["pnl"].cumsum()
    pk     = eq.cummax()
    dd     = ((eq-pk)/pk.abs().replace(0,1)*100).min()
    std    = t["pnl"].std()
    sharpe = avg/std*(252**0.5) if std>0 else 0
    sess   = t.groupby("sess").agg(
        n=("outcome","count"),
        w=("outcome", lambda x:(x=="WIN").sum()),
        p=("pnl","mean")
    )
    sess["wr"] = (sess["w"]/sess["n"]*100).round(1)
    return {"total":len(t),"wr":round(wr,1),"tp1r":round(tp1r,1),
            "pnl":round(total,2),"avg":round(avg,2),
            "dd":round(dd,2),"sharpe":round(sharpe,2),"sess":sess}

def verdict(m):
    if not m: return "❌  ข้อมูลไม่พอ"
    if m["wr"]>=55 and m["sharpe"]>=1.2 and abs(m["dd"])<=20: return "✅  STRONG PASS"
    if m["wr"]>=48 and m["sharpe"]>=0.8 and abs(m["dd"])<=30: return "✅  PASS"
    if m["wr"]>=42 and m["sharpe"]>=0.4: return "⚠️   MARGINAL"
    return "❌  FAIL"

def report(m, label):
    sep = "─"*54
    print(f"\n{sep}\n  {label}\n{sep}")
    if not m:
        print("  trades น้อยเกินไป")
        return
    print(f"  Trades     {m['total']:>8,}")
    print(f"  Win Rate   {m['wr']:>7.1f}%")
    print(f"  TP1 Hit    {m['tp1r']:>7.1f}%")
    print(f"  Avg PnL    ${m['avg']:>7.2f}")
    print(f"  Total PnL  ${m['pnl']:>8.2f}")
    print(f"  Max DD     {m['dd']:>7.2f}%")
    print(f"  Sharpe     {m['sharpe']:>7.2f}")
    print(f"\n  SESSION:")
    print(m["sess"].to_string())
    print(f"\n  {verdict(m)}")
    print(sep)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("="*54)
    print("  AI TRADE SYSTEM — BACKTEST v5")
    print("  EMA7/30 · SMA99 · BOS/CHoCH/QM")
    print(f"  TP1=RR{TP1_R} (50%) + TP2=RR{TP2_R} (50%) + EMA7 Trail")
    print("="*54)

    results = {}

    for sym in SYMBOLS:
        print(f"\n[{sym}]")
        d1h = load(sym, TF_1H)
        d4h = load(sym, TF_4H)
        if d1h.empty:
            print("  ไม่มีข้อมูล")
            continue
        print(f"  1h:{len(d1h):,}  4h:{len(d4h):,}")
        d1h = indicators(d1h)
        d1h = htf_bias(d1h, d4h)

        # ── ทาง A: Fixed TP2=2.0R ─────────────────────────────────────────
        tA = backtest(d1h, sym, use_trailing=False)
        mA = metrics(tA)

        # ── ทาง B: Trailing EMA7 ──────────────────────────────────────────
        tB = backtest(d1h, sym, use_trailing=True)
        mB = metrics(tB)

        print(f"\n  [A] Fixed TP2={TP2_R}R")
        report(mA, f"{sym} — Fixed TP2={TP2_R}R  [{len(tA)} trades]")

        print(f"\n  [B] Trailing EMA7")
        report(mB, f"{sym} — Trailing EMA7  [{len(tB)} trades]")

        results[sym] = {"A": (tA, mA), "B": (tB, mB)}

    # ── Summary เปรียบเทียบ ────────────────────────────────────────────────
    print("\n" + "="*54)
    print("  COMPARISON — A vs B (รวมทุก Symbol)")
    print("="*54)

    for mode in ["A","B"]:
        label = f"Fixed TP2={TP2_R}R" if mode=="A" else "Trailing EMA7"
        all_t = [results[s][mode][0] for s in results if not results[s][mode][0].empty]
        if not all_t:
            continue
        combined = pd.concat(all_t, ignore_index=True)
        combined.to_csv(f"backtest_v5_{mode}.csv", index=False)
        m = metrics(combined)
        if m:
            print(f"\n  [{mode}] {label}")
            print(f"  Trades   : {m['total']:,}")
            print(f"  Win Rate : {m['wr']}%")
            print(f"  TP1 Hit  : {m['tp1r']}%")
            print(f"  Total PnL: ${m['pnl']:,.2f}")
            print(f"  Sharpe   : {m['sharpe']}")
            print(f"  Max DD   : {m['dd']}%")
            print(f"  {verdict(m)}")

    print("\n[DONE]")
    print("  backtest_v5_A.csv — Fixed TP2")
    print("  backtest_v5_B.csv — Trailing EMA7")

if __name__ == "__main__":
    main()
