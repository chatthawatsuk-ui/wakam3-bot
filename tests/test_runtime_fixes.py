import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import claude_filter
import generate_dashboard
import paper_trade
import position_manager
import signal_scanner


class RuntimeFixTests(unittest.TestCase):
    def test_db_context_failure_keeps_alert_but_blocks_execution(self):
        signal = {
            "symbol": "BTC/USDT",
            "side": "LONG",
            "score": 14,
            "sl_pct": 1.2,
            "rsi": 55,
            "score_trend": 10,
            "price": 100.0,
            "sl": 98.8,
            "tp1": 101.4,
            "tp2": 102.4,
            "regime": "TRENDING",
        }

        with mock.patch("claude_filter.sqlite3.connect", side_effect=sqlite3.DatabaseError("broken db")):
            with mock.patch("claude_filter._init", return_value=None):
                approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved)
        self.assertEqual(reason, "filter_disabled")
        self.assertFalse(meta["execution_allowed"])
        self.assertEqual(meta["execution_block_reason"], "DB_UNAVAILABLE")
        self.assertFalse(signal["execution_allowed"])

    def test_positions_full_is_alert_only_not_filter_reject(self):
        signal = {
            "symbol": "SOL/USDT",
            "side": "SHORT",
            "score": 15,
            "sl_pct": 1.0,
            "rsi": 50,
            "score_trend": 10,
            "price": 100.0,
            "sl": 101.0,
            "tp1": 98.8,
            "tp2": 98.0,
            "regime": "TRENDING",
        }
        ctx = {
            "balance": 1000.0,
            "open_count": 10,
            "open_summary": "BTC/USDT LONG",
            "daily_pnl": 0.0,
            "daily_pnl_pct": 0.0,
            "same_symbol_open": False,
            "same_symbol_count": 0,
            "same_symbol_entry_px": 0.0,
            "same_symbol_side": "",
            "db_available": True,
        }

        with mock.patch("claude_filter._get_portfolio_context", return_value=ctx):
            with mock.patch("claude_filter._init", return_value=None):
                approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved)
        self.assertEqual(reason, "filter_disabled")
        self.assertFalse(meta["execution_allowed"])
        self.assertEqual(meta["execution_block_reason"], "POSITIONS_FULL (10/10)")

    def test_reject_scan_marks_status_and_reason(self):
        result = {"status": "SIGNAL"}
        sig, updated = signal_scanner._reject_scan(result, "SL_REJECT", "bad stop")

        self.assertIsNone(sig)
        self.assertEqual(updated["status"], "SL_REJECT")
        self.assertEqual(updated["reject_reason"], "bad stop")
        self.assertEqual(updated["claude_reason"], "bad stop")

    def test_close_reason_maps_forced_close_to_max_pos(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                conn = sqlite3.connect(":memory:")
                conn.execute(
                    "CREATE TABLE trades ("
                    "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_px REAL, "
                    "leverage REAL, notional_usd REAL, status TEXT, exit_px REAL, "
                    "outcome TEXT, pnl_usd REAL, closed_at TEXT, exit_reason TEXT)"
                )
                conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL, updated TEXT)")
                conn.execute(
                    "INSERT INTO trades (id, symbol, side, entry_px, leverage, notional_usd, status) "
                    "VALUES (1, 'BTC/USDT', 'LONG', 100, 20, 200, 'OPEN')"
                )
                conn.execute("INSERT INTO portfolio VALUES (1, 1000, '')")
                conn.commit()

                paper_trade._close(conn, 1, 99, "LOSS", -1, reason="MaxPos forced close (score=1)")
                row = conn.execute("SELECT exit_reason FROM trades WHERE id=1").fetchone()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(row[0], "MAX_POS")

    def test_execution_block_skips_open_trade(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE signal_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, score INTEGER, "
            "entry_px REAL, sl_px REAL, tp1_px REAL, tp2_px REAL, regime TEXT, tf TEXT, "
            "was_traded INTEGER, skip_reason TEXT, tp1_hit INTEGER DEFAULT 0, "
            "outcome TEXT DEFAULT 'PENDING', exit_reason TEXT, exit_px REAL, logged_at TEXT, "
            "resolved_at TEXT, balance_at_signal REAL DEFAULT 1000.0, score_liq INTEGER DEFAULT 0, "
            "score_fund INTEGER DEFAULT 0, funding_rate REAL, bull_sweep INTEGER DEFAULT 0, "
            "bear_sweep INTEGER DEFAULT 0)"
        )
        conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL, updated TEXT)")
        conn.execute("INSERT INTO portfolio VALUES (1, 1000, '')")
        conn.commit()

        sig = {
            "symbol": "BTC/USDT",
            "side": "LONG",
            "score": 12,
            "price": 100.0,
            "sl": 99.0,
            "tp1": 101.2,
            "tp2": 102.0,
            "regime": "TRENDING",
            "execution_allowed": False,
            "execution_block_reason": "DB_UNAVAILABLE",
        }

        self.assertIsNone(paper_trade.open_trade(conn, sig))
        row = conn.execute("SELECT was_traded, skip_reason FROM signal_log").fetchone()
        self.assertEqual(row, (0, "DB_UNAVAILABLE"))

    def test_pyramid_evaluate_still_allows_valid_pyramid(self):
        trade = {
            "side": "LONG",
            "entry_px": 100.0,
            "sl_px": 98.0,
            "tp1_hit": 0,
            "pyramid_level": 1,
        }
        signal = {
            "side": "LONG",
            "score_trend": 10,
            "regime": "TRENDING",
            "sl": 100.5,
            "atr": 1.0,
        }

        action, _, params = position_manager.evaluate(signal, trade, 101.0)

        self.assertEqual(action, "PYRAMID")
        self.assertEqual(params["level"], 2)

    def test_approval_commands_can_apply_multiple_pending_types(self):
        commands = {"/approve_conditions", "/approve_regime"}

        self.assertTrue(generate_dashboard._command_seen(commands, "/approve_conditions"))
        self.assertTrue(generate_dashboard._command_seen(commands, "/approve_regime"))
        self.assertFalse(generate_dashboard._command_seen(commands, "/approve_weights"))


if __name__ == "__main__":
    unittest.main()
