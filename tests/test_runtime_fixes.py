import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import claude_filter
import generate_dashboard
import paper_trade
import position_manager
import signal_scanner
import weekly_report


class RuntimeFixTests(unittest.TestCase):
    def _make_mock_claude_client(self, decision="APPROVE", confidence=85):
        """Helper: mock Anthropic client that returns a valid JSON response."""
        resp_json = json.dumps({
            "decision": decision, "confidence": confidence,
            "reason": "test", "risk_notes": "none", "suggested_adjustment": "none",
        })
        mock_client = mock.MagicMock()
        mock_resp = mock.MagicMock()
        mock_resp.content = [mock.MagicMock(text=resp_json)]
        mock_resp.usage = mock.MagicMock(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        mock_client.messages.create.return_value = mock_resp
        return mock_client

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

        mock_client = self._make_mock_claude_client()
        with mock.patch("claude_filter.sqlite3.connect", side_effect=sqlite3.DatabaseError("broken db")):
            with mock.patch("claude_filter._init", return_value=mock_client):
                with mock.patch("claude_filter._log_api_usage"):
                    approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved)
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

        mock_client = self._make_mock_claude_client()
        with mock.patch("claude_filter._get_portfolio_context", return_value=ctx):
            with mock.patch("claude_filter._init", return_value=mock_client):
                with mock.patch("claude_filter._log_api_usage"):
                    approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved)
        self.assertFalse(meta["execution_allowed"])
        self.assertEqual(meta["execution_block_reason"], "POSITIONS_FULL (10/10)")

    # ── P3 Claude fail-safe regression tests ───────────────────────────────

    def test_claude_api_exception_blocks_execution_allows_alert(self):
        """API exception → approved=True (alert), execution_allowed=False, reason=AI_FILTER_UNAVAILABLE"""
        signal = {
            "symbol": "BTC/USDT", "side": "LONG", "score": 14, "sl_pct": 1.2,
            "rsi": 55, "score_trend": 10, "price": 100.0, "sl": 98.8,
            "tp1": 101.4, "tp2": 102.4, "regime": "TRENDING",
        }
        ctx = {
            "balance": 1000.0, "open_count": 0, "open_summary": "none",
            "daily_pnl": 0.0, "daily_pnl_pct": 0.0,
            "same_symbol_open": False, "same_symbol_count": 0,
            "same_symbol_entry_px": 0.0, "same_symbol_side": "",
            "db_available": True, "db_error": "",
        }
        mock_client = mock.MagicMock()
        mock_client.messages.create.side_effect = Exception("API timeout")

        with mock.patch("claude_filter._get_portfolio_context", return_value=ctx):
            with mock.patch("claude_filter._init", return_value=mock_client):
                approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved, "alert path must stay open")
        self.assertIn("err:", reason)
        self.assertFalse(meta["execution_allowed"])
        self.assertEqual(meta["execution_block_reason"], "AI_FILTER_UNAVAILABLE")
        self.assertFalse(signal["execution_allowed"])
        self.assertEqual(signal["execution_block_reason"], "AI_FILTER_UNAVAILABLE")

    def test_no_api_key_blocks_execution_allows_alert(self):
        """No ANTHROPIC_API_KEY → approved=True (alert), execution blocked"""
        signal = {
            "symbol": "ETH/USDT", "side": "SHORT", "score": 12, "sl_pct": 1.5,
            "rsi": 45, "score_trend": 8, "price": 3000.0, "sl": 3045.0,
            "tp1": 2964.0, "tp2": 2940.0, "regime": "TRENDING",
        }

        with mock.patch("claude_filter._init", return_value=None):
            approved, reason, meta = claude_filter.ask(signal, {"htf_bull": False})

        self.assertTrue(approved, "alert path must stay open")
        self.assertEqual(reason, "filter_disabled")
        self.assertFalse(meta["execution_allowed"])
        self.assertEqual(meta["execution_block_reason"], "AI_FILTER_UNAVAILABLE")
        self.assertFalse(signal["execution_allowed"])

    def test_claude_invalid_json_blocks_execution_allows_alert(self):
        """Claude returns garbage → json.loads fails → execution blocked"""
        signal = {
            "symbol": "SOL/USDT", "side": "LONG", "score": 16, "sl_pct": 2.0,
            "rsi": 58, "score_trend": 11, "price": 150.0, "sl": 147.0,
            "tp1": 153.6, "tp2": 156.0, "regime": "TRENDING",
        }
        ctx = {
            "balance": 1000.0, "open_count": 0, "open_summary": "none",
            "daily_pnl": 0.0, "daily_pnl_pct": 0.0,
            "same_symbol_open": False, "same_symbol_count": 0,
            "same_symbol_entry_px": 0.0, "same_symbol_side": "",
            "db_available": True, "db_error": "",
        }
        mock_client = mock.MagicMock()
        mock_resp = mock.MagicMock()
        mock_resp.content = [mock.MagicMock(text="not valid json {{{")]
        mock_client.messages.create.return_value = mock_resp

        with mock.patch("claude_filter._get_portfolio_context", return_value=ctx):
            with mock.patch("claude_filter._init", return_value=mock_client):
                approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved, "alert path must stay open")
        self.assertIn("err:", reason)
        self.assertFalse(meta["execution_allowed"])
        self.assertEqual(meta["execution_block_reason"], "AI_FILTER_UNAVAILABLE")

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

    def test_manual_weekly_report_uses_current_sunday_anchor(self):
        now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)

        start, end, basis, mode = weekly_report._report_window(7, now=now, mode="manual")

        self.assertEqual(mode, "manual")
        self.assertEqual(start, datetime(2026, 5, 10, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(end, now)
        self.assertIn("manual preview", basis)

    def test_scheduled_weekly_report_uses_previous_full_cycle(self):
        now = datetime(2026, 5, 17, 16, 5, tzinfo=timezone.utc)

        start, end, basis, mode = weekly_report._report_window(7, now=now, mode="schedule")

        self.assertEqual(mode, "schedule")
        self.assertEqual(start, datetime(2026, 5, 10, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 5, 17, 16, 0, tzinfo=timezone.utc))
        self.assertIn("scheduled weekly cycle", basis)

    def test_tp1_timeout_uses_partial_close_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                conn = sqlite3.connect(":memory:")
                conn.execute(
                    "CREATE TABLE trades ("
                    "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_px REAL, "
                    "sl_px REAL, tp1_px REAL, tp2_px REAL, tp1_hit INTEGER, qty REAL, "
                    "risk_usd REAL, opened_at TEXT, status TEXT, exit_px REAL, outcome TEXT, "
                    "pnl_usd REAL, closed_at TEXT, exit_reason TEXT, leverage REAL, notional_usd REAL)"
                )
                conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL, updated TEXT)")
                conn.execute("INSERT INTO portfolio VALUES (1, 1000, '')")
                old_opened = "2026-05-01T00:00:00+00:00"
                conn.execute(
                    "INSERT INTO trades VALUES ("
                    "1, 'BTC/USDT', 'LONG', 100, 100, 112, 120, 1, 1, 10, ?, "
                    "'OPEN', NULL, NULL, NULL, NULL, NULL, 20, 200)"
                    ,
                    (old_opened,),
                )
                conn.commit()

                with mock.patch("paper_trade.get_price", return_value=90):
                    with mock.patch("paper_trade.datetime") as dt_mock:
                        dt_mock.now.return_value = datetime(2026, 5, 4, tzinfo=timezone.utc)
                        dt_mock.fromisoformat.side_effect = datetime.fromisoformat
                        dt_mock.timezone = timezone
                        paper_trade.check_open_trades(conn)

                row = conn.execute(
                    "SELECT outcome, pnl_usd, exit_reason FROM trades WHERE id=1"
                ).fetchone()
                balance = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()[0]
            finally:
                os.chdir(old_cwd)

        self.assertEqual(row, ("WIN", 1.0, "TIMEOUT"))
        self.assertEqual(balance, 1001.0)

    # ── TP1 partial-close accounting regression tests ──────────────────────

    def _make_tp1_trade_db(self):
        """Helper: in-memory DB with one OPEN LONG trade where tp1_hit=1.
        entry=100, sl=100(BE), tp1=112, tp2=120, qty=1, balance=1000."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_px REAL, "
            "sl_px REAL, tp1_px REAL, tp2_px REAL, tp1_hit INTEGER, qty REAL, "
            "risk_usd REAL, opened_at TEXT, status TEXT, exit_px REAL, outcome TEXT, "
            "pnl_usd REAL, closed_at TEXT, exit_reason TEXT, leverage REAL, notional_usd REAL)"
        )
        conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL, updated TEXT)")
        conn.execute("INSERT INTO portfolio VALUES (1, 1000, '')")
        opened = "2026-05-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO trades VALUES ("
            "1, 'BTC/USDT', 'LONG', 100, 100, 112, 120, 1, 1, 10, ?, "
            "'OPEN', NULL, NULL, NULL, NULL, NULL, 20, 200)",
            (opened,),
        )
        conn.commit()
        return conn

    def test_tp1_tp2_uses_partial_close_accounting(self):
        """TP1 hit → TP2 hit: PnL = 0.5×(112-100) + 0.5×(120-100) = 6+10 = 16"""
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                conn = self._make_tp1_trade_db()
                with mock.patch("paper_trade.get_price", return_value=120):
                    with mock.patch("paper_trade.datetime") as dt_mock:
                        dt_mock.now.return_value = datetime(2026, 5, 2, tzinfo=timezone.utc)
                        dt_mock.fromisoformat.side_effect = datetime.fromisoformat
                        dt_mock.timezone = timezone
                        paper_trade.check_open_trades(conn)

                row = conn.execute(
                    "SELECT outcome, pnl_usd, exit_reason FROM trades WHERE id=1"
                ).fetchone()
                balance = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()[0]
            finally:
                os.chdir(old_cwd)

        self.assertEqual(row[0], "WIN")
        self.assertAlmostEqual(row[1], 16.0)
        self.assertEqual(row[2], "TP2")
        self.assertAlmostEqual(balance, 1016.0)

    def test_tp1_sl_be_uses_partial_close_accounting(self):
        """TP1 hit → SL at BE (entry=100): PnL = 0.5×(112-100) + 0.5×(100-100) = 6+0 = 6"""
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                conn = self._make_tp1_trade_db()
                with mock.patch("paper_trade.get_price", return_value=100):
                    with mock.patch("paper_trade.datetime") as dt_mock:
                        dt_mock.now.return_value = datetime(2026, 5, 2, tzinfo=timezone.utc)
                        dt_mock.fromisoformat.side_effect = datetime.fromisoformat
                        dt_mock.timezone = timezone
                        paper_trade.check_open_trades(conn)

                row = conn.execute(
                    "SELECT outcome, pnl_usd, exit_reason FROM trades WHERE id=1"
                ).fetchone()
                balance = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()[0]
            finally:
                os.chdir(old_cwd)

        self.assertEqual(row[0], "WIN")
        self.assertAlmostEqual(row[1], 6.0)
        self.assertEqual(row[2], "SL_BE")
        self.assertAlmostEqual(balance, 1006.0)

    def test_tp1_forced_close_uses_partial_close_accounting(self):
        """TP1 hit → forced close at px=95: PnL = 0.5×(112-100) + 0.5×(95-100) = 6+(-2.5) = 3.5"""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_px REAL, "
            "sl_px REAL, tp1_px REAL, tp2_px REAL, tp1_hit INTEGER, qty REAL, "
            "score INTEGER, risk_usd REAL, opened_at TEXT, status TEXT, exit_px REAL, "
            "outcome TEXT, pnl_usd REAL, closed_at TEXT, exit_reason TEXT, "
            "leverage REAL, notional_usd REAL)"
        )
        conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL, updated TEXT)")
        conn.execute("INSERT INTO portfolio VALUES (1, 1000, '')")
        conn.execute(
            "INSERT INTO trades VALUES ("
            "1, 'BTC/USDT', 'LONG', 100, 100, 112, 120, 1, 1, "
            "5, 10, '2026-05-01T00:00:00+00:00', 'OPEN', NULL, NULL, NULL, NULL, NULL, 20, 200)"
        )
        conn.commit()

        pnl = paper_trade._position_close_pnl("LONG", 1, 100, 95, tp1_px=112, tp1_hit=1)

        self.assertAlmostEqual(pnl, 3.5)
        expected_outcome = "WIN"
        self.assertEqual("WIN" if pnl > 0 else "LOSS", expected_outcome)

    def test_tp1_max_position_close_uses_partial_close_accounting(self):
        """enforce_max_positions with tp1_hit=1: uses _position_close_pnl with tp1 partial accounting."""
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                conn = sqlite3.connect(":memory:")
                conn.execute(
                    "CREATE TABLE trades ("
                    "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_px REAL, "
                    "sl_px REAL, tp1_px REAL, tp2_px REAL, tp1_hit INTEGER, qty REAL, "
                    "score INTEGER, risk_usd REAL, opened_at TEXT, status TEXT, exit_px REAL, "
                    "outcome TEXT, pnl_usd REAL, closed_at TEXT, exit_reason TEXT, "
                    "leverage REAL, notional_usd REAL)"
                )
                conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL, updated TEXT)")
                conn.execute("INSERT INTO portfolio VALUES (1, 1000, '')")
                for i in range(1, 12):
                    conn.execute(
                        "INSERT INTO trades VALUES ("
                        "?, 'SYM' || ?||'/USDT', 'LONG', 100, 100, 112, 120, ?, 1, "
                        "?, 10, '2026-05-01T00:00:00+00:00', 'OPEN', NULL, NULL, NULL, NULL, NULL, 20, 200)",
                        (i, i, 1 if i == 1 else 0, i),
                    )
                conn.commit()

                with mock.patch("paper_trade.get_price", return_value=95):
                    closed_count = paper_trade.enforce_max_positions(conn)

                row = conn.execute(
                    "SELECT outcome, pnl_usd, exit_reason FROM trades WHERE id=1"
                ).fetchone()
                balance = conn.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()[0]
            finally:
                os.chdir(old_cwd)

        self.assertEqual(closed_count, 1)
        self.assertEqual(row[0], "WIN")
        self.assertAlmostEqual(row[1], 3.5)
        self.assertEqual(row[2], "MAX_POS")
        self.assertAlmostEqual(balance, 1003.5)

    def test_weekly_report_monitor_only_clears_pending_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                for path in [
                    weekly_report.PENDING_WEIGHTS,
                    weekly_report.PENDING_CONDITIONS,
                    weekly_report.PENDING_REGIME,
                ]:
                    with open(path, "w") as f:
                        f.write("{}")

                weekly_report._clear_weekly_pending_files()

                self.assertFalse(os.path.exists(weekly_report.PENDING_WEIGHTS))
                self.assertFalse(os.path.exists(weekly_report.PENDING_CONDITIONS))
                self.assertFalse(os.path.exists(weekly_report.PENDING_REGIME))
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
