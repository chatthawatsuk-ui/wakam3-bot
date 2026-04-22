"""
download_history.py — Pre-download 3Y historical OHLCV จาก OKX → Parquet cache
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

บันทึกไว้ใน  historical_data/{SYMBOL}_{TF}.parquet
backtest_live.py จะดึงจาก cache แทน OKX API ทุกครั้ง → เร็วกว่ามาก

TFs ที่ดาวน์โหลด (primary + htf ทุก TF config):
  15m  30m  1h  2h  4h  1d  1w

Usage:
  py download_history.py                        # 10 symbols, 3Y, ทุก TF
  py download_history.py --symbols BTC/USDT     # symbol เดียว
  py download_history.py --tf 1h 4h             # TF เฉพาะ
  py download_history.py --days 365             # ย้อนหลัง 1 ปี
  py download_history.py --refresh              # force re-download ทับไฟล์เดิม
"""
import sys, os, time, argparse, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timezone, timedelta
import pandas as pd

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt"); sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
CACHE_DIR = "historical_data"

FALLBACK_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT", "SUI/USDT",
]

# TFs ที่ backtest_3y.py ใช้ (primary + htf ทุกคู่)
# 15m→1h, 30m→2h, 1h→4h, 2h→4h, 4h→1d  ครอบคลุมทุก pair
ALL_TFS = ["15m", "30m", "1h", "2h", "4h", "1d"]

DEFAULT_DAYS = 1100   # ~3 ปี (เผื่อ weekend/holiday gaps)
API_LIMIT    = 300    # OKX max candles per call
SLEEP_SEC    = 0.25   # หน่วงหลังแต่ละ call

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════
exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {
        "defaultType":        "swap",      # Perpetual Futures เท่านั้น
        "fetchMarkets":       ["swap"],     # โหลดเฉพาะ swap — ไม่โหลด Spot
    },
})

print("Loading OKX Futures (Swap) markets...", end=" ", flush=True)
try:
    exchange.load_markets()
    swap_count = sum(1 for m in exchange.markets.values() if m.get("swap"))
    print(f"{swap_count} swap pairs")
except Exception as e:
    print(f"WARN: {e}")
    print("  ข้ามไป — จะ load อัตโนมัติตอน fetch แรก")

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def to_swap_symbol(symbol: str) -> str:
    """BTC/USDT → BTC/USDT:USDT  (OKX linear perpetual swap format)"""
    if ":" in symbol:
        return symbol
    base, quote = symbol.split("/")
    return f"{base}/{quote}:{quote}"

def sym_to_fname(symbol: str, tf: str) -> str:
    """BTC/USDT + 1h → BTC_USDT_1h  (ชื่อไฟล์ใช้ format สั้น)"""
    base_sym = symbol.split(":")[0]   # ตัด :USDT ออก
    return base_sym.replace("/", "_") + f"_{tf}"

def cache_path(symbol: str, tf: str) -> str:
    fname = sym_to_fname(symbol, tf)
    return os.path.join(CACHE_DIR, f"{fname}.parquet")


def load_watchlist_symbols(path="watchlist_custom.json"):
    try:
        with open(path, encoding="utf-8") as f:
            syms = json.load(f)
        out = []
        for s in syms:
            if not s:
                continue
            sym = str(s).strip().upper()
            if "/" not in sym:
                sym += "/USDT"
            out.append(sym)
        return out or FALLBACK_SYMBOLS
    except Exception:
        return FALLBACK_SYMBOLS

def fetch_all(symbol: str, tf: str, days: float) -> pd.DataFrame:
    """ดึง OHLCV ย้อนหลัง N วัน ด้วย pagination"""
    swap_sym = to_swap_symbol(symbol)   # BTC/USDT → BTC/USDT:USDT
    since_dt = datetime.now(timezone.utc) - timedelta(days=days + 0.5)
    since_ms = int(since_dt.timestamp() * 1000)
    all_bars: list = []
    call_count = 0

    while True:
        try:
            bars = exchange.fetch_ohlcv(swap_sym, tf, since=since_ms, limit=API_LIMIT)
            call_count += 1
        except Exception as e:
            print(f"\n  [WARN] {swap_sym} {tf} call#{call_count}: {e}")
            break

        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < API_LIMIT:
            break
        since_ms = bars[-1][0] + 1
        time.sleep(SLEEP_SEC)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("dt").set_index("dt").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="WAKAM3 Historical Data Downloader")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols (default: watchlist_custom.json)")
    parser.add_argument("--tf",      nargs="+", default=None,
                        help=f"TFs (default: {ALL_TFS})")
    parser.add_argument("--days",    type=float, default=DEFAULT_DAYS,
                        help=f"ย้อนหลังกี่วัน (default: {DEFAULT_DAYS} ≈ 3Y)")
    parser.add_argument("--refresh", action="store_true",
                        help="force re-download ทับไฟล์ที่มีอยู่แล้ว")
    args = parser.parse_args()

    symbols = args.symbols or load_watchlist_symbols()
    tfs     = args.tf      or ALL_TFS
    days    = args.days
    refresh = args.refresh

    os.makedirs(CACHE_DIR, exist_ok=True)

    total  = len(symbols) * len(tfs)
    done   = 0
    skip   = 0
    errors = 0
    t_start = time.time()

    print("=" * 65)
    print("  WAKAM3 — Historical Data Downloader")
    print(f"  Symbols : {len(symbols)}  |  TFs : {', '.join(tfs)}")
    print(f"  Period  : {days:.0f} days (~{days/365:.1f} years)")
    print(f"  Target  : {CACHE_DIR}/")
    print(f"  Refresh : {'YES (overwrite)' if refresh else 'NO (skip existing)'}")
    print("=" * 65)

    for sym in symbols:
        print(f"\n[{sym}]")
        for tf in tfs:
            fpath = cache_path(sym, tf)

            # Skip ถ้ามีไฟล์อยู่แล้วและไม่ได้ --refresh
            if not refresh and os.path.exists(fpath):
                try:
                    existing = pd.read_parquet(fpath)
                    latest   = existing.index.max()
                    age_days = (datetime.now(timezone.utc) - latest.to_pydatetime()).days
                    rows     = len(existing)
                    print(f"  {tf:<4} SKIP  {rows:>7,} rows  latest={str(latest)[:10]}  ({age_days}d ago)")
                    skip += 1
                    done += 1
                    continue
                except Exception:
                    pass  # file corrupt → re-download

            print(f"  {tf:<4} fetching...", end=" ", flush=True)
            t0  = time.time()
            df  = fetch_all(sym, tf, days)
            elapsed = time.time() - t0

            if df.empty:
                print(f"EMPTY — ข้ามไป (อาจ symbol ไม่รองรับ TF นี้)")
                errors += 1
                done   += 1
                continue

            try:
                df.to_parquet(fpath, engine="pyarrow", compression="snappy")
                size_kb = os.path.getsize(fpath) / 1024
                print(f"OK  {len(df):>7,} rows  "
                      f"{str(df.index.min())[:10]} → {str(df.index.max())[:10]}  "
                      f"{size_kb:.0f}KB  ({elapsed:.1f}s)")
            except Exception as e:
                print(f"SAVE ERROR: {e}")
                errors += 1

            done += 1
            # Progress
            pct = done / total * 100
            elapsed_total = time.time() - t_start
            eta = (elapsed_total / done) * (total - done) if done else 0
            print(f"  {'─'*55} [{done}/{total}] {pct:.0f}%  ETA {eta:.0f}s")

    # Summary
    elapsed_total = time.time() - t_start
    print("\n" + "=" * 65)
    print(f"  เสร็จสิ้น — {done}/{total} ไฟล์")
    print(f"  Skip (มีอยู่แล้ว) : {skip}")
    print(f"  Error / Empty    : {errors}")
    print(f"  เวลาทั้งหมด       : {elapsed_total/60:.1f} นาที")
    # Show folder size
    total_mb = sum(
        os.path.getsize(os.path.join(CACHE_DIR, f))
        for f in os.listdir(CACHE_DIR)
        if f.endswith(".parquet")
    ) / (1024 * 1024)
    print(f"  ขนาด cache       : {total_mb:.1f} MB  ({CACHE_DIR}/)")
    print("=" * 65)

if __name__ == "__main__":
    main()
