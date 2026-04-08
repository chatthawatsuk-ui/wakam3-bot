#!/bin/bash
echo "========================================"
echo "  AI TRADE SYSTEM"
echo "  $(date '+%Y-%m-%d %H:%M')"
echo "========================================"
cd "$(dirname "$0")"
echo ""
echo "[1/4] สแกนหา Signals..."
python3 live_trader.py
echo ""
echo "[2/4] อัพเดท Paper Trades..."
python3 paper_trade.py
echo ""
echo "[3/4] Generate Dashboard..."
python3 generate_dashboard.py
echo ""
echo "[4/4] ส่ง Telegram..."
python3 notify.py
echo ""
echo "========================================"
echo "  เสร็จแล้ว — รันอีกครั้งใน 1 ชั่วโมง"
echo "========================================"
