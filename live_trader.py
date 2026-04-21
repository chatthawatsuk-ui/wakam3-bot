"""
📡 Data Collector + Orchestrator
  - ดึง OHLCV จาก OKX ทุก 15 นาที
  - ส่ง df ให้ Signal Scanner
  - Signal Scanner เรียก 3 Pine Specialists แล้วคืน signal
  - บันทึก scan_results.json + latest_signals.json

Flow:
  📡 live_trader (fetch data)
      └──▶ 🔍 signal_scanner.scan_symbol()
              ├── 🎯 agent_trend.run()
              ├── 🏦 agent_smc.run()
              └── 📈 agent_osc.run()
                      └──▶ 🤖 paper_trade.py
"""
import sys, os, warnings, time, json
from datetime import datetime, timezone
import pandas as pd
warnings.filterwarnings("ignore")

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt")
    sys.exit(1)

import signal_scanner as SCANNER

# ── CONFIG ────────────────────────────────────────────────────────────────────
FUTURES_SYMBOLS = [
    "BTC/USDT",  "ETH/USDT",  "BNB/USDT",  "XRP/USDT",  "SOL/USDT",
    "TRX/USDT",  "DOGE/USDT", "ADA/USDT",  "BCH/USDT",  "LTC/USDT",
    "LINK/USDT", "AVAX/USDT", "SUI/USDT",  "TON/USDT",  "DOT/USDT",
    "SHIB/USDT", "HBAR/USDT", "XLM/USDT",  "UNI/USDT",  "NEAR/USDT",
    "PEPE/USDT", "AAVE/USDT", "ICP/USDT",
    "ETC/USDT",  "RENDER/USDT","ALGO/USDT", "POL/USDT",  "ATOM/USDT",
    "WLD/USDT",  "ENA/USDT",  "FIL/USDT",  "APT/USDT",
    "CRO/USDT",  "TRUMP/USDT","ONDO/USDT", "HYPE/USDT",
]
SPOT_SYMBOLS = []   # ไม่มี Spot — ทุก symbol ใช้ Futures (swap)

# ── Watchlist Custom — โหลดจาก watchlist_custom.json ──────────────────────────
# เพิ่มเหรียญใน watchlist_custom.json แล้ว scanner จะ scan อัตโนมัติเป็น Futures
_WL_CUSTOM_PATH = "watchlist_custom.json"
try:
    import json as _json
    _wl_custom = _json.load(open(_WL_CUSTOM_PATH, encoding="utf-8"))
    _wl_futures = [s if "/" in s else s + "/USDT" for s in _wl_custom]
    # merge เฉพาะ symbol ที่ยังไม่มีใน list หลัก
    _new = [s for s in _wl_futures if s not in FUTURES_SYMBOLS and s not in SPOT_SYMBOLS]
    if _new:
        FUTURES_SYMBOLS = FUTURES_SYMBOLS + _new
        print(f"[Watchlist] เพิ่ม {len(_new)} custom Futures: {', '.join(_new)}")
except Exception:
    pass  # ไม่มีไฟล์ หรือ parse error → ข้ามไป

SYMBOLS     = FUTURES_SYMBOLS + SPOT_SYMBOLS
FUTURES_SET = set(FUTURES_SYMBOLS)

TF_1H   = "30m"   # Primary TF — 30m (TF ใครTF concept)
TF_4H   = "30m"   # Gate TF — ใช้ 30m เดียวกัน (ไม่ข้าม TF)
TF_1D   = "1d"
CANDLES = 500     # 30m ต้องการ candle มากขึ้นสำหรับ warmup
CANDLES_D = 50   # Daily: พอสำหรับ Stochastic(14,3)

DB_PATH  = "paper_trades.db"
LOG_PATH = "signals.log"

# ── EXCHANGES ─────────────────────────────────────────────────────────────────
exchange_futures = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})
exchange_spot = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})


# ── LOGGER ────────────────────────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ── FETCH OHLCV ───────────────────────────────────────────────────────────────
def fetch(symbol, timeframe, limit=CANDLES):
    exch = exchange_futures if symbol in FUTURES_SET else exchange_spot
    # ลอง format หลัก (e.g. RAVE/USDT) ก่อน
    # ถ้าไม่ได้ลอง format linear perpetual (e.g. RAVE/USDT:USDT) เป็น fallback
    candidates = [symbol]
    if symbol in FUTURES_SET and ":" not in symbol:
        # เพิ่ม :USDT suffix สำหรับ OKX linear perpetual format
        candidates.append(symbol.split("/")[0] + "/USDT:USDT")
    last_err = None
    for sym_try in candidates:
        try:
            bars = exch.fetch_ohlcv(sym_try, timeframe, limit=limit)
            if not bars:
                continue
            df = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("dt", inplace=True)
            return df[["open","high","low","close","volume"]].astype(float)
        except Exception as e:
            last_err = e
            continue
    log(f"[WARN] fetch {symbol} {timeframe}: {last_err}")
    return pd.DataFrame()


# ── MAIN SCAN — ส่งข้อมูลให้ Signal Scanner ──────────────────────────────────
def scan():
    signals      = []
    scan_results = []
    log("─" * 55)
    log(f"SCAN เริ่ม — {len(SYMBOLS)} symbols")
    log(f"⚖️  Weights — 🎯{SCANNER.W_TREND:.3f} 🏦{SCANNER.W_SMC:.3f} 📈{SCANNER.W_OSC:.3f} "
        f"({'dynamic' if abs(SCANNER.W_TREND - 1/3) > 0.01 else 'equal (default)'})")

    for sym in SYMBOLS:
        mtype = "FUTURES" if sym in FUTURES_SET else "SPOT"
        try:
            d1h = fetch(sym, TF_1H)
            d4h = fetch(sym, TF_4H)
            d1d = fetch(sym, TF_1D, limit=CANDLES_D)   # Daily — สำหรับ Stoch filter

            # MIN_CANDLES: 80 พอสำหรับ EMA30, RSI14, MACD(12,26,9), Stoch(14)
            # เดิม 150 ทำให้เหรียญ new listing ถูก reject แม้ indicator คำนวณได้
            MIN_CANDLES = 80
            if d1h.empty or len(d1h) < MIN_CANDLES:
                reason = "fetch_failed" if d1h.empty else f"only_{len(d1h)}_candles"
                log(f"  {sym}: ข้อมูลไม่พอ ({len(d1h)} candles < {MIN_CANDLES}) [{reason}]")
                scan_results.append({
                    "symbol": sym, "market_type": mtype, "status": "NO_DATA",
                    "no_data_reason": reason,
                    "best_score": 0, "score_long": 0, "score_short": 0,
                    "score_trend": 0, "score_smc": 0, "score_osc": 0,
                    "rsi": 0, "price": 0, "htf_bull": False,
                    "in_kz": False, "in_discount": False, "trail_bull": False,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                continue

            # ── ส่ง df ให้ Signal Scanner ─────────────────────
            sig, result = SCANNER.scan_symbol(sym, d1h, d4h, mtype, d1d)

            # merge TP/SL levels into scan result so dashboard can show them
            if sig:
                result["entry_price"] = sig["price"]
                result["sl_price"]    = sig["sl"]
                result["tp_price"]    = sig["tp1"]
                result["tp2_price"]   = sig["tp2"]

            scan_results.append(result)

            if sig:
                sig["tf"] = TF_1H.upper()   # primary scan TF — store in trade record
                signals.append(sig)
                log(f"  ✅ {sym} {sig['side']} score={sig['score']}/45 "
                    f"[🎯{sig['score_trend']} 🏦{sig['score_smc']} 📈{sig['score_osc']} "
                    f"💧{sig.get('score_liq',0)} 💰{sig.get('score_fund',0)}] "
                    f"px={sig['price']:.4f} sl={sig['sl']:.4f}")
            else:
                sc = result.get("best_score", 0)
                st = result.get("status", "?")
                log(f"  {sym}: {st} score={sc}/45 "
                    f"[🎯{result.get('score_trend',0)} "
                    f"🏦{result.get('score_smc',0)} "
                    f"📈{result.get('score_osc',0)} "
                    f"💧{result.get('score_liq',0)} "
                    f"💰{result.get('score_fund',0)}]")

            time.sleep(0.3)

        except Exception as e:
            log(f"  [ERR] {sym}: {e}")

    log(f"SCAN เสร็จ — พบ {len(signals)} signals จาก {len(scan_results)} coins")
    SCANNER.save_specialist_history(scan_results)
    return signals, scan_results


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 55)
    log("AI TRADE SYSTEM — LIVE SIGNAL ENGINE")
    log("🎯 Trend Agent | 🏦 SMC Agent | 📈 Osc Agent → 🔍 Signal Scanner")
    log("=" * 55)

    signals      = []
    scan_results = []

    try:
        signals, scan_results = scan()
    except Exception as e:
        log(f"[FATAL] scan() failed: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        try:
            with open("scan_results.json", "w") as f:
                json.dump(scan_results, f, indent=2, ensure_ascii=False)
            log(f"บันทึก → scan_results.json ({len(scan_results)} coins)")
        except Exception as e:
            log(f"[ERR] write scan_results.json: {e}")

    if signals:
        try:
            with open("latest_signals.json", "w") as f:
                json.dump(signals, f, indent=2, ensure_ascii=False)
            log(f"บันทึก → latest_signals.json ({len(signals)} signals)")
        except Exception as e:
            log(f"[ERR] write latest_signals.json: {e}")
    else:
        log("ไม่มี signal รอบนี้")
