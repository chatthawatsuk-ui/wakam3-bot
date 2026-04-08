"""
AI Trade System — Step 1: Historical Data Collector
====================================================
ดึง OHLCV ย้อนหลัง 3 ปี จาก OKX (ฟรี ไม่ต้อง API key)
รองรับ timeframe: 5m, 15m, 1h, 4h, 1d
บันทึกลง SQLite (local) หรือ CSV

Usage:
    pip install ccxt pandas tqdm
    python step1_data_collector.py

Author: AI Trade System — Phase 1
"""

import ccxt
import pandas as pd
import sqlite3
import time
from datetime import datetime, timedelta
from tqdm import tqdm
import os

# ─── CONFIG ───────────────────────────────────────────────────────────────────

EXCHANGE_ID   = "okx"           # หรือ "binance"
SYMBOLS       = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT",
    "BNB/USDT", "XRP/USDT", "DOGE/USDT",
    "AVAX/USDT", "LINK/USDT", "ADA/USDT", "DOT/USDT",
]
TIMEFRAMES    = ["1h", "4h", "1d"]
YEARS_BACK    = 3
DB_PATH       = "trade_data.db"
CSV_DIR       = "data/csv"

# ─── INIT ─────────────────────────────────────────────────────────────────────

os.makedirs(CSV_DIR, exist_ok=True)

exchange = getattr(ccxt, EXCHANGE_ID)({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# ─── DATABASE SETUP ───────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol    TEXT    NOT NULL,
            timeframe TEXT    NOT NULL,
            ts        INTEGER NOT NULL,
            open      REAL,
            high      REAL,
            low       REAL,
            close     REAL,
            volume    REAL,
            UNIQUE(symbol, timeframe, ts)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_main ON ohlcv(symbol, timeframe, ts)")
    conn.commit()
    print(f"[DB] Initialized → {DB_PATH}")

# ─── FETCH ONE SYMBOL / TIMEFRAME ─────────────────────────────────────────────

def fetch_ohlcv(symbol: str, timeframe: str, since_dt: datetime) -> pd.DataFrame:
    """ดึงข้อมูล OHLCV ทีละ batch จนครบ"""
    since_ms  = int(since_dt.timestamp() * 1000)
    now_ms    = int(datetime.utcnow().timestamp() * 1000)
    all_bars  = []
    limit     = 300  # OKX max per request = 300

    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        except ccxt.RateLimitExceeded:
            time.sleep(10)
            continue
        except Exception as e:
            print(f"  [WARN] {symbol} {timeframe}: {e}")
            break

        if not bars:
            break

        all_bars.extend(bars)
        last_ts  = bars[-1][0]

        # หยุดเมื่อถึงปัจจุบัน
        if last_ts >= now_ms:
            break

        since_ms = last_ts + 1

        # ถ้าได้น้อยกว่า limit แปลว่าหมดข้อมูลแล้ว
        if len(bars) < limit:
            break

        time.sleep(0.5)  # เว้น 0.5s ต่อ request ป้องกัน rate limit

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["symbol"]    = symbol.replace("/", "")
    df["timeframe"] = timeframe
    return df

# ─── SAVE ─────────────────────────────────────────────────────────────────────

def save_to_db(conn: sqlite3.Connection, df: pd.DataFrame):
    if df.empty:
        return 0
    rows = df[["symbol", "timeframe", "ts", "open", "high", "low", "close", "volume"]].values.tolist()
    conn.executemany("""
        INSERT OR IGNORE INTO ohlcv (symbol, timeframe, ts, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    return len(rows)

def save_to_csv(df: pd.DataFrame, symbol: str, timeframe: str):
    if df.empty:
        return
    safe   = symbol.replace("/", "")
    path   = f"{CSV_DIR}/{safe}_{timeframe}.csv"
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms")
    df.to_csv(path, index=False)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    since_dt = datetime.utcnow() - timedelta(days=365 * YEARS_BACK)
    print(f"[START] Exchange: {EXCHANGE_ID.upper()}")
    print(f"[INFO]  ดึงข้อมูลตั้งแต่ {since_dt.strftime('%Y-%m-%d')} ถึงวันนี้")
    print(f"[INFO]  {len(SYMBOLS)} symbols × {len(TIMEFRAMES)} timeframes\n")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    total_rows = 0
    tasks      = [(s, tf) for s in SYMBOLS for tf in TIMEFRAMES]

    for symbol, timeframe in tqdm(tasks, desc="Fetching"):
        df         = fetch_ohlcv(symbol, timeframe, since_dt)
        saved      = save_to_db(conn, df)
        save_to_csv(df, symbol, timeframe)
        total_rows += saved
        tqdm.write(f"  ✓ {symbol:12s} {timeframe:4s} → {saved:,} rows")
        time.sleep(0.3)

    conn.close()

    print(f"\n[DONE] บันทึกรวม {total_rows:,} แถว → {DB_PATH}")
    print(f"[CSV]  ไฟล์ CSV อยู่ที่ {CSV_DIR}/")

    # ─── QUICK SUMMARY ────────────────────────────────────────────────────────
    conn2 = sqlite3.connect(DB_PATH)
    summary = pd.read_sql("""
        SELECT symbol, timeframe, COUNT(*) as rows,
               datetime(MIN(ts)/1000, 'unixepoch') as from_dt,
               datetime(MAX(ts)/1000, 'unixepoch') as to_dt
        FROM ohlcv
        GROUP BY symbol, timeframe
        ORDER BY symbol, timeframe
    """, conn2)
    conn2.close()

    print("\n─── SUMMARY ──────────────────────────────────────────────────")
    print(summary.to_string(index=False))

if __name__ == "__main__":
    main()
