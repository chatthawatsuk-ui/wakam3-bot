import sys, os, sqlite3, json, warnings, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from datetime import datetime, timezone
warnings.filterwarnings("ignore")

try:
    import ccxt
except ImportError:
    ccxt = None

DB_PATH   = "paper_trades.db"
OUT_PATH  = "dashboard_data.json"
PORT_SIZE = 1000.0

# OKX USDT Perpetual Futures — confirmed
FUTURES_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "LTC/USDT",
    "LINK/USDT", "AVAX/USDT", "SUI/USDT", "TON/USDT", "DOT/USDT",
    "SHIB/USDT", "HBAR/USDT", "XLM/USDT", "UNI/USDT", "NEAR/USDT",
    "TAO/USDT", "MNT/USDT", "PEPE/USDT", "AAVE/USDT", "ICP/USDT",
    "ETC/USDT", "RENDER/USDT", "ALGO/USDT", "POL/USDT", "ATOM/USDT",
    "WLD/USDT", "ENA/USDT", "FIL/USDT", "APT/USDT", "VET/USDT",
    "CRO/USDT", "TRUMP/USDT", "ONDO/USDT", "HYPE/USDT", "DEXE/USDT",
]

# ไม่มี OKX futures — ใช้ Spot
SPOT_SYMBOLS = [
    "MORPHO/USDT", "KAS/USDT", "QNT/USDT", "ZEC/USDT", "FLR/USDT",
]

SYMBOLS     = FUTURES_SYMBOLS + SPOT_SYMBOLS
FUTURES_SET = set(FUTURES_SYMBOLS)

def fetch_okx_prices():
    """ดึงราคา + 24h stats จาก OKX (futures+spot) — รันบน GitHub Actions"""
    if not ccxt:
        print("  [WARN] ccxt ไม่มี — ข้ามการดึงราคา OKX")
        return {}, [], {}

    live_prices = {}
    market_tickers = []
    tickers = {}

    try:
        exch_fut  = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        exch_spot = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "spot"}})

        tickers_fut  = exch_fut.fetch_tickers()
        tickers_spot = exch_spot.fetch_tickers()

        # normalize futures keys: "BTC/USDT:USDT" → "BTC/USDT"
        tickers_fut_norm = {k.split(':')[0]: v for k, v in tickers_fut.items()}

        # รวม: spot เป็น base, futures (normalized) ทับเพื่อให้ได้ราคา perp
        tickers = tickers_spot.copy()
        tickers.update(tickers_fut_norm)

        # ── live prices ทุกเหรียญใน SYMBOLS ──────────────────────────────
        for sym in SYMBOLS:
            t = tickers.get(sym)
            if not t:
                continue
            live_prices[sym] = {
                "price":  round(float(t.get("last") or 0), 8),
                "change": round(float(t.get("percentage") or 0), 2),
                "high":   round(float(t.get("high") or 0), 8),
                "low":    round(float(t.get("low") or 0), 8),
                "vol":    round(float(t.get("quoteVolume") or 0), 2),
            }

        # ── market tickers (futures USDT pairs, vol > $1M) ────────────────
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT") and not sym.endswith("/USDT"):
                continue
            vol = float(t.get("quoteVolume") or 0)
            price = float(t.get("last") or 0)
            if vol < 1_000_000 or price <= 0:
                continue
            base = sym.replace("/USDT:USDT", "").replace("/USDT", "")
            market_tickers.append({
                "sym":    base,
                "price":  round(price, 8),
                "change": round(float(t.get("percentage") or 0), 2),
                "vol":    round(vol, 2),
            })

        print(f"  OKX: {len(live_prices)} watchlist, {len(market_tickers)} market pairs")

    except Exception as e:
        print(f"  [WARN] OKX fetch_tickers: {e}")

    return live_prices, market_tickers, tickers  # tickers_norm ส่งต่อให้ calc_market_indices


# ── HELPERS ───────────────────────────────────────────────────────────────────
def _rsi(closes, period=14):
    """Simple RSI calculation (pure Python, no pandas needed)"""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_g  = sum(gains[-period:]) / period
    avg_l  = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def calc_market_indices(tickers_norm):
    """
    คำนวณ 2 ดัชนี:
    1. BTC Fear & Greed  — จาก BTC daily OHLCV (RSI + Momentum + Volatility)
    2. Altcoin Season    — เปรียบ % change 24h ของ 44 altcoins vs BTC
    """
    result = {
        "btc_fg":        {"value": 50, "label": "Neutral", "rsi": 50},
        "altcoin_season": {"value": 50, "label": "Neutral", "outperform": 0, "total": 0},
    }
    if not ccxt:
        return result

    # ── 1. Altcoin Season (24h proxy) ─────────────────────────────────────────
    try:
        btc_t    = tickers_norm.get("BTC/USDT", {})
        btc_chg  = float(btc_t.get("percentage") or 0)
        alts     = [s for s in SYMBOLS if s != "BTC/USDT"]
        outperform, valid = 0, 0
        for sym in alts:
            t = tickers_norm.get(sym, {})
            if not t:
                continue
            chg = float(t.get("percentage") or 0)
            valid += 1
            if chg > btc_chg:
                outperform += 1
        if valid > 0:
            score = round(outperform / valid * 100)
            if score >= 75:   lbl = "Altcoin Season 🚀"
            elif score >= 55: lbl = "Altcoins Leading"
            elif score >= 45: lbl = "Neutral"
            elif score >= 25: lbl = "Bitcoin Leading"
            else:             lbl = "Bitcoin Season ₿"
            result["altcoin_season"] = {
                "value":      score,
                "label":      lbl,
                "outperform": outperform,
                "total":      valid,
                "btc_24h":    round(btc_chg, 2),
                "timeframe":  "24h",
            }
        print(f"  Altcoin Season: {result['altcoin_season']['value']}/100 — {result['altcoin_season']['label']}")
    except Exception as e:
        print(f"  [WARN] altcoin_season: {e}")

    # ── 2. BTC Fear & Greed (daily OHLCV) ─────────────────────────────────────
    try:
        exch = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        bars = exch.fetch_ohlcv("BTC/USDT", "1d", limit=91)
        if bars and len(bars) >= 31:
            closes = [float(b[4]) for b in bars]   # close prices
            curr   = closes[-1]

            # RSI(14) daily
            rsi = _rsi(closes)

            # Momentum: % above/below 30d MA  (−20% → 0, +20% → 100)
            ma30      = sum(closes[-30:]) / 30
            momentum  = (curr - ma30) / ma30 * 100
            mom_score = max(0.0, min(100.0, (momentum + 20) / 40 * 100))

            # Volatility: std of last 14 daily returns (low vol = greed)
            rets   = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                      for i in range(len(closes)-14, len(closes))]
            mean_r = sum(rets) / len(rets)
            std_r  = (sum((r - mean_r)**2 for r in rets) / len(rets)) ** 0.5
            vol_score = max(0.0, min(100.0, 100 - std_r / 5 * 100))

            score = round(rsi * 0.4 + mom_score * 0.4 + vol_score * 0.2)
            if score >= 75:   lbl = "Extreme Greed"
            elif score >= 55: lbl = "Greed"
            elif score >= 45: lbl = "Neutral"
            elif score >= 25: lbl = "Fear"
            else:             lbl = "Extreme Fear"

            result["btc_fg"] = {
                "value": score,
                "label": lbl,
                "rsi":   rsi,
                "ma30_pct": round(momentum, 1),
            }
        print(f"  BTC F&G: {result['btc_fg']['value']}/100 — {result['btc_fg']['label']}")
    except Exception as e:
        print(f"  [WARN] btc_fg: {e}")

    return result


def calc_specialist_winrate(conn):
    """
    Level 2 — วัด Win Rate ต่อ Specialist จาก closed trades
    คืน dict พร้อมแสดงในหน้า Agent Team
    """
    empty = {"trades": 0, "wins": 0, "losses": 0, "winrate": None,
             "avg_score_win": None, "avg_score_loss": None,
             "buckets": []}

    try:
        rows = conn.execute("""
            SELECT score_trend, score_smc, score_osc, outcome
            FROM trades
            WHERE outcome IS NOT NULL
        """).fetchall()
    except Exception:
        return {"trend": empty, "smc": empty, "osc": empty, "total_closed": 0}

    if not rows:
        return {"trend": empty, "smc": empty, "osc": empty, "total_closed": 0}

    def _stat(scores_wins):
        """scores_wins = list of (score, is_win)"""
        if not scores_wins:
            return empty.copy()
        total  = len(scores_wins)
        wins   = sum(1 for _, w in scores_wins if w)
        losses = total - wins
        win_sc  = [s for s, w in scores_wins if w]
        loss_sc = [s for s, w in scores_wins if not w]
        # score buckets: Low(0-3) / Mid(4-6) / High(7+)
        buckets = []
        for lbl, lo, hi in [("Low 0-3",0,3),("Mid 4-6",4,6),("High 7+",7,99)]:
            b = [(s,w) for s,w in scores_wins if lo <= s <= hi]
            if b:
                bw = sum(1 for _,w in b if w)
                buckets.append({
                    "label":   lbl,
                    "trades":  len(b),
                    "winrate": round(bw/len(b)*100,1)
                })
        return {
            "trades":         total,
            "wins":           wins,
            "losses":         losses,
            "winrate":        round(wins/total*100, 1),
            "avg_score_win":  round(sum(win_sc)/len(win_sc), 1) if win_sc  else None,
            "avg_score_loss": round(sum(loss_sc)/len(loss_sc),1) if loss_sc else None,
            "buckets":        buckets,
        }

    trend_data = [(r[0], r[3]=="WIN") for r in rows]
    smc_data   = [(r[1], r[3]=="WIN") for r in rows]
    osc_data   = [(r[2], r[3]=="WIN") for r in rows]

    result = {
        "trend":        _stat(trend_data),
        "smc":          _stat(smc_data),
        "osc":          _stat(osc_data),
        "total_closed": len(rows),
    }
    print(f"  Specialist WinRate — {len(rows)} closed trades "
          f"| Trend:{result['trend']['winrate']}% "
          f"SMC:{result['smc']['winrate']}% "
          f"Osc:{result['osc']['winrate']}%")
    return result


MIN_TRADES = 10    # ต้องปิดกี่ trade ถึงจะ unlock weights
SMOOTHING  = 0.4   # blend กับ equal weight ป้องกัน overfit

def calc_dynamic_weights(specialist_wr):
    """
    Level 3 — คำนวณ dynamic weights จาก win rate แต่ละ specialist
    บันทึก weights.json ให้ live_trader.py อ่านใช้ run ถัดไป
    """
    total_closed = specialist_wr.get("total_closed", 0)
    eq = 1 / 3

    if total_closed < MIN_TRADES:
        weights = {
            "trend":   round(eq, 4),
            "smc":     round(eq, 4),
            "osc":     round(eq, 4),
            "locked":  True,
            "reason":  f"ต้องการ {MIN_TRADES} closed trades (มีอยู่ {total_closed})",
            "total_closed": total_closed,
            "generated": datetime.now(timezone.utc).isoformat(),
        }
    else:
        # win rate ต่อ specialist (fallback 0.5 ถ้าไม่มีข้อมูล)
        def _wr(key):
            d = specialist_wr.get(key, {})
            return d["winrate"] / 100 if d.get("winrate") is not None else 0.5

        wr_t = _wr("trend")
        wr_s = _wr("smc")
        wr_o = _wr("osc")

        # blend กับ equal weight (SMOOTHING)
        raw = {
            "trend": (1 - SMOOTHING) * wr_t + SMOOTHING * eq,
            "smc":   (1 - SMOOTHING) * wr_s + SMOOTHING * eq,
            "osc":   (1 - SMOOTHING) * wr_o + SMOOTHING * eq,
        }
        total = sum(raw.values())
        weights = {
            "trend":   round(raw["trend"] / total, 4),
            "smc":     round(raw["smc"]   / total, 4),
            "osc":     round(raw["osc"]   / total, 4),
            "locked":  False,
            "reason":  f"Adapted from {total_closed} closed trades (smoothing={SMOOTHING})",
            "total_closed": total_closed,
            "wr_trend": round(wr_t * 100, 1),
            "wr_smc":   round(wr_s * 100, 1),
            "wr_osc":   round(wr_o * 100, 1),
            "generated": datetime.now(timezone.utc).isoformat(),
        }

    try:
        with open("weights.json", "w") as f:
            json.dump(weights, f, indent=2, ensure_ascii=False)
        status = "🔒 locked" if weights["locked"] else "✅ adapted"
        print(f"  Dynamic Weights {status} — Trend:{weights['trend']} SMC:{weights['smc']} Osc:{weights['osc']}")
    except Exception as e:
        print(f"  [WARN] weights.json write: {e}")

    return weights


def load_backtest_data():
    """อ่าน backtest_live.csv (หรือ v5 fallback) → สร้าง summary สำหรับ Backtest page"""
    import pandas as pd

    candidates = [
        ("backtest_live.csv", "live"),
    ]
    df = None
    source = None
    loaded_file = None
    for fname, fmt in candidates:
        if os.path.exists(fname):
            try:
                tmp = pd.read_csv(fname)
                if len(tmp) > 0:
                    df = tmp
                    source = fmt
                    loaded_file = fname
                    break
            except Exception:
                continue

    if df is None or df.empty:
        return {"available": False}

    # Normalize v5 columns → live format
    if source == "v5":
        rename_map = {}
        if "kz"    in df.columns: rename_map["kz"]    = "in_kz"
        if "entry" in df.columns: rename_map["entry"] = "entry_ts"
        if "exit"  in df.columns: rename_map["exit"]  = "exit_ts"
        if rename_map:
            df = df.rename(columns=rename_map)
        if "exit_px" not in df.columns: df["exit_px"] = 0
        if "regime"  not in df.columns: df["regime"]  = "UNKNOWN"

    try:

        def _metrics(d):
            if len(d) < 2:
                return None
            wins  = (d["outcome"] == "WIN")
            wr    = round(wins.mean() * 100, 1)
            tp1r  = round(d["tp1_hit"].mean() * 100, 1) if "tp1_hit" in d else 0
            total = round(d["pnl"].sum(), 2)
            avg   = round(d["pnl"].mean(), 2)
            eq    = d["pnl"].cumsum()
            pk    = eq.cummax()
            dd    = round(((eq - pk) / pk.abs().replace(0, 1) * 100).min(), 1)
            std   = d["pnl"].std()
            sharpe = round((avg / std) * (252 ** 0.5), 2) if std > 0 else 0
            kz = d["in_kz"].astype(str).str.lower().isin(["true","1"])
            kz_wr = round(wins[kz].mean() * 100, 1) if kz.any() else None
            return {"n": len(d), "wr": wr, "tp1r": tp1r,
                    "total_pnl": total, "avg_pnl": avg,
                    "dd": dd, "sharpe": sharpe, "kz_wr": kz_wr}

        summary = _metrics(df)

        # by symbol
        by_sym = []
        for sym, g in df.groupby("sym"):
            m = _metrics(g)
            if m:
                m["sym"] = sym
                by_sym.append(m)
        by_sym.sort(key=lambda x: x["total_pnl"], reverse=True)

        # by exit type
        by_exit = []
        for et, g in df.groupby("exit_type"):
            wins_e = (g["outcome"] == "WIN").sum()
            by_exit.append({
                "exit_type": et,
                "n":         len(g),
                "wins":      int(wins_e),
                "wr":        round(wins_e / len(g) * 100, 1),
                "avg_pnl":   round(g["pnl"].mean(), 2),
            })

        # by score band
        df2 = df.copy()
        df2["band"] = pd.cut(df2["score"], bins=[0,10,15,20,25,31],
                             labels=["≤10","11-15","16-20","21-25","≥26"])
        by_score = []
        for band, g in df2.groupby("band", observed=True):
            wins_b = (g["outcome"] == "WIN").sum()
            by_score.append({
                "band":    str(band),
                "n":       len(g),
                "wr":      round(wins_b / len(g) * 100, 1),
                "avg_pnl": round(g["pnl"].mean(), 2),
            })

        # by regime
        by_regime = []
        for reg, g in df.groupby("regime"):
            wins_r = (g["outcome"] == "WIN").sum()
            by_regime.append({
                "regime":  reg,
                "n":       len(g),
                "wr":      round(wins_r / len(g) * 100, 1),
                "avg_pnl": round(g["pnl"].mean(), 2),
            })

        # recent trades (last 100)
        recent = df.tail(100).copy()
        recent = recent.fillna("")
        trades_list = recent[["sym","side","score","ep","sl","tp1","tp2",
                               "entry_ts","exit_ts","exit_px","exit_type",
                               "tp1_hit","outcome","pnl","in_kz","regime",
                               "rsi"]].to_dict("records")

        # equity curve (cumulative PnL)
        equity_bt = [0] + [round(v, 2) for v in df["pnl"].cumsum().tolist()]

        mtime = os.path.getmtime(loaded_file)
        generated = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        return {
            "available":  True,
            "generated":  generated,
            "total_rows": len(df),
            "summary":    summary,
            "by_symbol":  by_sym,
            "by_exit":    by_exit,
            "by_score":   by_score,
            "by_regime":  by_regime,
            "trades":     trades_list,
            "equity":     equity_bt,
        }
    except Exception as e:
        print(f"  [WARN] load_backtest_data: {e}")
        return {"available": False, "error": str(e)}


def main():
    last_scan = datetime.now(timezone.utc).isoformat()

    # ── ดึงราคา OKX ──────────────────────────────────────────────────────────
    live_prices, market_tickers, tickers_norm = fetch_okx_prices()

    # ── คำนวณ BTC F&G และ Altcoin Season (ใช้ tickers_norm ที่ดึงมาแล้ว) ──
    _indices = calc_market_indices(tickers_norm)

    # ── โหลด scan_results ─────────────────────────────────────────────────
    scan_results = []
    if os.path.exists("scan_results.json"):
        try:
            with open("scan_results.json") as f:
                raw = json.load(f)
            scan_results = sorted(raw, key=lambda x: x.get("best_score", 0), reverse=True)
            print(f"  scan_results.json — {len(scan_results)} coins")
        except Exception as e:
            print(f"  [WARN] scan_results.json: {e}")
    else:
        print("  ไม่พบ scan_results.json")

    # ── โหลด signals ──────────────────────────────────────────────────────
    signals = []
    if os.path.exists("latest_signals.json"):
        try:
            with open("latest_signals.json") as f:
                signals = json.load(f)
        except: pass

    # ── ถ้าไม่มี DB ──────────────────────────────────────────────────────
    if not os.path.exists(DB_PATH):
        data = {
            "balance": PORT_SIZE, "pnl": 0, "win_rate": 0, "total_trades": 0,
            "open_trades": [], "closed_trades": [], "sessions": [],
            "equity": [PORT_SIZE], "signals": signals,
            "scan_results": scan_results,
            "live_prices": live_prices,
            "market_tickers": market_tickers,
            "btc_fg":         _indices["btc_fg"],
            "altcoin_season": _indices["altcoin_season"],
            "last_scan": last_scan,
            "generated": last_scan,
        }
        with open(OUT_PATH, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✅ dashboard_data.json — no DB, {len(scan_results)} scan, {len(live_prices)} prices")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    bal = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()
    balance = float(bal["balance"]) if bal else PORT_SIZE

    closed_rows = conn.execute("""
        SELECT id, symbol, side, entry_px, exit_px, outcome, pnl_usd,
               sl_px, tp1_px, tp1_hit, opened_at, closed_at,
               notional_usd, leverage, risk_usd
        FROM trades WHERE status='CLOSED' ORDER BY id
    """).fetchall()

    closed = []
    for r in closed_rows:
        ep       = r["entry_px"]    or 0
        xp       = r["exit_px"]     or 0
        pnl      = r["pnl_usd"]     or 0
        notional = r["notional_usd"] or 0
        # %PNL = PnL / notional (% ของ position size จริง)
        # fallback: ถ้าไม่มี notional (trades เก่า) ใช้ price movement
        if notional > 0:
            pnl_pct = round(pnl / notional * 100, 2)
        elif ep > 0 and xp > 0:
            raw_pct = (xp - ep) / ep * 100
            pnl_pct = round(raw_pct if r["side"] == "LONG" else -raw_pct, 2)
        else:
            pnl_pct = 0.0
        closed.append({
            "id":          r["id"],
            "symbol":      r["symbol"],
            "side":        r["side"],
            "entry_price": ep,
            "exit_price":  xp,
            "sl_price":    r["sl_px"],
            "tp_price":    r["tp1_px"],
            "outcome":     r["outcome"],
            "pnl":         pnl,
            "pnl_pct":     pnl_pct,
            "leverage":    r["leverage"]    or 0,
            "notional":    notional,
            "open_time":   r["opened_at"],
            "close_time":  r["closed_at"],
        })

    total     = len(closed)
    wins      = sum(1 for t in closed if t["outcome"] == "WIN")
    wr        = wins / total * 100 if total > 0 else 0
    total_pnl = sum(t["pnl"] or 0 for t in closed)

    equity  = [PORT_SIZE]
    running = PORT_SIZE
    for t in closed:
        running += (t["pnl"] or 0)
        equity.append(round(running, 2))

    open_rows = conn.execute("""
        SELECT id, symbol, side, score, entry_px, sl_px, tp1_px, tp2_px,
               tp1_hit, rsi, opened_at, notional_usd, leverage, risk_usd
        FROM trades WHERE status='OPEN' ORDER BY score DESC, id DESC
    """).fetchall()

    opens = []
    for r in open_rows:
        sym      = r["symbol"]
        ep       = float(r["entry_px"]     or 0)
        sl       = float(r["sl_px"]        or 0)
        side     = r["side"]
        tp1_hit  = r["tp1_hit"]
        notional = float(r["notional_usd"] or 0)
        lev      = float(r["leverage"]     or 0)
        risk_usd = float(r["risk_usd"]     or 0)

        # ── Unrealized P&L ───────────────────────────────────────────────────
        lv      = live_prices.get(sym, {})
        curr_px = float(lv.get("price") or ep)

        pnl_pct = 0.0
        pnl_usd = 0.0
        if ep > 0 and curr_px > 0:
            raw_pct = (curr_px - ep) / ep * 100
            pnl_pct = raw_pct if side == "LONG" else -raw_pct

            if notional > 0:                          # ใช้ notional จาก DB (ถูกต้อง)
                pnl_usd = pnl_pct / 100 * notional
            else:                                     # fallback สำหรับ trades เก่า
                sl_dist = abs(ep - sl) / ep if (ep > 0 and sl > 0) else 0.005
                pos_usd = (PORT_SIZE * 0.01) / sl_dist if sl_dist > 0 else 0
                pnl_usd = pnl_pct / 100 * pos_usd

        opens.append({
            "id":            r["id"],
            "symbol":        sym,
            "side":          side,
            "score":         r["score"],
            "entry_price":   ep,
            "current_price": round(curr_px, 8),
            "sl_price":      sl,
            "tp_price":      float(r["tp1_px"] or 0),
            "tp2_price":     float(r["tp2_px"] or 0),
            "tp1_hit":       bool(tp1_hit),
            "rsi":           r["rsi"],
            "notional":      round(notional, 2),
            "leverage":      round(lev, 2),
            "risk_usd":      round(risk_usd, 2),
            "pnl_pct":       round(pnl_pct, 2),
            "pnl":          round(pnl_usd, 2),
            "open_time":    r["opened_at"],
        })

    # ── Level 2: Specialist Win Rate ─────────────────────────────────────────
    specialist_wr = calc_specialist_winrate(conn)

    # ── Level 3: Dynamic Weights ──────────────────────────────────────────────
    dyn_weights = calc_dynamic_weights(specialist_wr)

    sess_data = {}
    for t in closed:
        try:
            h = datetime.fromisoformat(t["open_time"]).hour
            if 1 <= h < 8:     s = "ASIA"
            elif 8 <= h < 13:  s = "EUROPE"
            elif 13 <= h < 21: s = "US"
            else:               s = "LATE"
        except:
            s = "—"
        if s not in sess_data:
            sess_data[s] = {"session": s, "total": 0, "wins": 0}
        sess_data[s]["total"] += 1
        if t["outcome"] == "WIN":
            sess_data[s]["wins"] += 1

    sessions = []
    for s in ["ASIA", "EUROPE", "US", "LATE"]:
        if s in sess_data:
            d = sess_data[s]
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1) if d["total"] > 0 else 0
            sessions.append(d)

    data = {
        "balance":       round(balance, 2),
        "pnl":           round(total_pnl, 2),
        "win_rate":      round(wr, 1),
        "total_trades":  total,
        "open_trades":   opens,
        "closed_trades": closed,
        "sessions":      sessions,
        "equity":        equity,
        "signals":       signals,
        "scan_results":  scan_results,
        "live_prices":    live_prices,
        "market_tickers": market_tickers,
        "btc_fg":         _indices["btc_fg"],
        "altcoin_season": _indices["altcoin_season"],
        "last_scan":        last_scan,
        "generated":        last_scan,
        "specialist_wr":    specialist_wr,
        "dyn_weights":      dyn_weights,
        "backtest":         load_backtest_data(),
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    conn.close()
    print(f"✅ dashboard_data.json — {total} trades, ${balance:.2f}, {len(scan_results)} scan, {len(live_prices)} prices")

if __name__ == "__main__":
    main()
