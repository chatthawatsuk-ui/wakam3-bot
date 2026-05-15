import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

import claude_filter
import generate_dashboard
import monthly_report
import notify
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

    def test_claude_api_exception_bypasses_continues_execution(self):
        """P3 V2 bypass: API exception → approved=True, execution_allowed=True, ai_filter_bypassed=True"""
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
                with mock.patch("claude_filter._log_bypass_event") as mock_log:
                    approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved, "bypass path must keep signal approved")
        self.assertTrue(reason.startswith("bypass:"))
        self.assertTrue(meta.get("ai_filter_bypassed"))
        self.assertEqual(meta.get("bypass_reason"), "timeout")
        self.assertTrue(meta.get("execution_allowed", True),
                        "P3 bypass must NOT block execution")
        self.assertTrue(signal.get("ai_filter_bypassed"))
        self.assertEqual(signal.get("bypass_reason"), "timeout")
        mock_log.assert_called_once_with("timeout", signal_id="", symbol="BTC/USDT")

    def test_no_api_key_bypasses_continues_execution(self):
        """P3 V2 bypass: no ANTHROPIC_API_KEY → bypass, signal continues"""
        signal = {
            "symbol": "ETH/USDT", "side": "SHORT", "score": 12, "sl_pct": 1.5,
            "rsi": 45, "score_trend": 8, "price": 3000.0, "sl": 3045.0,
            "tp1": 2964.0, "tp2": 2940.0, "regime": "TRENDING",
        }
        ctx = {
            "balance": 1000.0, "open_count": 0, "open_summary": "none",
            "daily_pnl": 0.0, "daily_pnl_pct": 0.0,
            "same_symbol_open": False, "same_symbol_count": 0,
            "same_symbol_entry_px": 0.0, "same_symbol_side": "",
            "db_available": True, "db_error": "",
        }

        with mock.patch("claude_filter._get_portfolio_context", return_value=ctx):
            with mock.patch("claude_filter._init", return_value=None):
                with mock.patch("claude_filter._log_bypass_event") as mock_log:
                    approved, reason, meta = claude_filter.ask(signal, {"htf_bull": False})

        self.assertTrue(approved, "bypass path must keep signal approved")
        self.assertEqual(reason, "bypass:api_key_missing")
        self.assertTrue(meta.get("ai_filter_bypassed"))
        self.assertEqual(meta.get("bypass_reason"), "api_key_missing")
        self.assertTrue(meta.get("execution_allowed", True),
                        "P3 bypass must NOT block execution")
        self.assertTrue(signal.get("ai_filter_bypassed"))
        mock_log.assert_called_once_with("api_key_missing", signal_id="", symbol="ETH/USDT")

    def test_claude_invalid_json_bypasses_continues_execution(self):
        """P3 V2 bypass: invalid JSON → bypass with reason=invalid_response"""
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
                with mock.patch("claude_filter._log_bypass_event"):
                    approved, reason, meta = claude_filter.ask(signal, {"htf_bull": True})

        self.assertTrue(approved, "bypass path must keep signal approved")
        self.assertTrue(reason.startswith("bypass:"))
        self.assertTrue(meta.get("ai_filter_bypassed"))
        self.assertTrue(meta.get("execution_allowed", True))

    def test_bypass_event_logged_to_db(self):
        """P3 V2: _log_bypass_event creates table + inserts row in paper_trades.db"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "paper_trades.db")
            with mock.patch.object(
                claude_filter.os.path, "dirname", return_value=tmp,
            ):
                claude_filter._log_bypass_event(
                    "api_key_missing", signal_id="sig1", symbol="BTC/USDT"
                )
                claude_filter._log_bypass_event(
                    "timeout", signal_id="sig2", symbol="ETH/USDT"
                )

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT reason, signal_id, symbol FROM bypass_events ORDER BY id"
            ).fetchall()
            conn.close()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("api_key_missing", "sig1", "BTC/USDT"))
        self.assertEqual(rows[1], ("timeout", "sig2", "ETH/USDT"))

    def test_classify_bypass_reason(self):
        """P3 V2: exception → reason code mapping"""
        self.assertEqual(
            claude_filter._classify_bypass_reason(Exception("Request timed out")),
            "timeout",
        )
        self.assertEqual(
            claude_filter._classify_bypass_reason(Exception("credit exhausted")),
            "credit_exhausted",
        )
        self.assertEqual(
            claude_filter._classify_bypass_reason(Exception("Invalid JSON")),
            "invalid_response",
        )
        self.assertEqual(
            claude_filter._classify_bypass_reason(Exception("503 Service Unavailable")),
            "http_error",
        )

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


    # ── P1: SL Distance Cap tests ──────────────────────────────────

    def _make_scan_env(self, entry_price, atr, swing_low, swing_high,
                       score_long=15, score_short=5):
        """Helper: mock agents + DataFrame so scan_symbol reaches SL calc."""
        import pandas as pd
        df_1h = pd.DataFrame({"close": [entry_price] * 30})
        df_4h = pd.DataFrame({"close": [entry_price] * 30})

        t_rep = {
            "score_long": score_long, "score_short": score_short,
            "atr": atr, "htf_bull": True, "htf_sma": True,
            "details": {"trail_slow_bull": False},
        }
        s_rep = {
            "score_long": score_long, "score_short": score_short,
            "swing_low": swing_low, "swing_high": swing_high,
            "details": {"in_discount": False},
        }
        o_rep = {
            "score_long": score_long, "score_short": score_short,
            "rsi": 50, "kz": False,
        }
        l_rep = {
            "score_long": 0, "score_short": 0,
            "bull_sweep": False, "bear_sweep": False,
        }
        return df_1h, df_4h, t_rep, s_rep, o_rep, l_rep

    def _run_scan_with_sl(self, entry, atr, swing_low, swing_high,
                          score_long=15, score_short=5):
        """Run scan_symbol with controlled SL parameters, return (signal, result)."""
        df_1h, df_4h, t_rep, s_rep, o_rep, l_rep = self._make_scan_env(
            entry, atr, swing_low, swing_high, score_long, score_short,
        )
        with mock.patch("signal_scanner.DISABLE_FUNDING", True), \
             mock.patch("signal_scanner._detect_regime", return_value="NORMAL"), \
             mock.patch("signal_scanner.save_shadow_signal"), \
             mock.patch("signal_scanner.save_condition_snapshot"), \
             mock.patch("signal_scanner.TREND") as m_t, \
             mock.patch("signal_scanner.SMC") as m_s, \
             mock.patch("signal_scanner.OSC") as m_o, \
             mock.patch("signal_scanner.LIQUIDITY") as m_l:
            m_t.run.return_value = t_rep
            m_t.MAX_SCORE = 13
            m_s.run.return_value = s_rep
            m_s.MAX_SCORE = 12
            m_o.run.return_value = o_rep
            m_o.MAX_SCORE = 16
            m_l.run.return_value = l_rep
            return signal_scanner.scan_symbol("TEST/USDT", df_1h, df_4h)

    def test_p1_sl_reject_pct_long(self):
        """LONG with SL > 4% from entry → SL_REJECT."""
        sig, res = self._run_scan_with_sl(
            entry=100.0, atr=2.0, swing_low=94.0, swing_high=106.0,
        )
        self.assertIsNone(sig)
        self.assertEqual(res["status"], "SL_REJECT")
        self.assertIn("6.0%", res["reject_reason"])

    def test_p1_sl_reject_pct_short(self):
        """SHORT with SL > 4% from entry → SL_REJECT."""
        sig, res = self._run_scan_with_sl(
            entry=100.0, atr=2.0, swing_low=94.0, swing_high=106.0,
            score_long=5, score_short=15,
        )
        self.assertIsNone(sig)
        self.assertEqual(res["status"], "SL_REJECT")
        self.assertIn("6.0%", res["reject_reason"])

    def test_p1_sl_reject_atr_mult(self):
        """SL < 4% but > ATR×3 → SL_REJECT on ATR rule."""
        sig, res = self._run_scan_with_sl(
            entry=100.0, atr=1.0, swing_low=96.5, swing_high=103.5,
        )
        self.assertIsNone(sig)
        self.assertEqual(res["status"], "SL_REJECT")
        self.assertIn("ATR", res["reject_reason"])

    def test_p1_sl_valid_passes(self):
        """SL < 4% AND < ATR×3 → passes SL checks (not SL_REJECT)."""
        sig, res = self._run_scan_with_sl(
            entry=100.0, atr=2.0, swing_low=98.0, swing_high=102.0,
        )
        self.assertNotEqual(res["status"], "SL_REJECT")


    # ── Monthly Report v0 tests ────────────────────────────────────

    def _make_monthly_db(self, trades=None, signals=None, balance=1000.0):
        """Helper: in-memory DB with trades + signal_log for monthly report."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_px REAL, "
            "exit_px REAL, pnl_usd REAL, outcome TEXT, exit_reason TEXT, "
            "score INTEGER, opened_at TEXT, closed_at TEXT, status TEXT, "
            "tp1_hit INTEGER, tp1_px REAL, qty REAL, notional_usd REAL, "
            "regime TEXT, sl_px REAL, tp2_px REAL)"
        )
        conn.execute(
            "CREATE TABLE signal_log ("
            "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, score INTEGER, "
            "was_traded INTEGER, skip_reason TEXT, logged_at TEXT, outcome TEXT)"
        )
        conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL, updated TEXT)")
        conn.execute("INSERT INTO portfolio VALUES (1, ?, '')", (balance,))
        if trades:
            for t in trades:
                conn.execute(
                    "INSERT INTO trades (symbol, side, entry_px, exit_px, pnl_usd, "
                    "outcome, exit_reason, score, opened_at, closed_at, status, "
                    "tp1_hit, tp1_px, qty, notional_usd, regime) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["symbol"], t["side"], t["entry_px"], t["exit_px"],
                     t["pnl_usd"], t["outcome"], t["exit_reason"], t["score"],
                     t["opened_at"], t["closed_at"], "CLOSED",
                     t.get("tp1_hit", 0), t.get("tp1_px"), t.get("qty", 1),
                     t.get("notional_usd", 200), t.get("regime", "TRENDING")),
                )
        if signals:
            for s in signals:
                conn.execute(
                    "INSERT INTO signal_log (symbol, side, score, was_traded, "
                    "skip_reason, logged_at, outcome) VALUES (?,?,?,?,?,?,?)",
                    (s["symbol"], s["side"], s.get("score", 10),
                     s.get("was_traded", 0), s.get("skip_reason"),
                     s["logged_at"], s.get("outcome")),
                )
        conn.commit()
        return conn

    def test_monthly_report_empty_db(self):
        """Monthly report with no trades or signals produces valid output."""
        conn = self._make_monthly_db()
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        since = now - timedelta(days=30)
        trades = monthly_report._fetch_closed_trades(conn, since.isoformat(), now.isoformat())
        signals = monthly_report._fetch_signal_log(conn, since.isoformat(), now.isoformat())
        conn.close()
        trade_m = monthly_report.calc_trade_metrics(trades)
        signal_m = monthly_report.calc_signal_metrics(signals)
        self.assertEqual(trade_m["total"], 0)
        self.assertEqual(signal_m["total"], 0)
        self.assertEqual(trade_m["win_rate"], 0)

    def test_monthly_report_aggregates_trades(self):
        """Monthly report correctly aggregates closed trades."""
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        trades = [
            {"symbol": "BTC/USDT", "side": "LONG", "entry_px": 100, "exit_px": 110,
             "pnl_usd": 10.0, "outcome": "WIN", "exit_reason": "TP2", "score": 15,
             "opened_at": "2026-05-01T00:00:00+00:00",
             "closed_at": "2026-05-02T00:00:00+00:00"},
            {"symbol": "ETH/USDT", "side": "SHORT", "entry_px": 200, "exit_px": 210,
             "pnl_usd": -5.0, "outcome": "LOSS", "exit_reason": "SL", "score": 12,
             "opened_at": "2026-05-03T00:00:00+00:00",
             "closed_at": "2026-05-04T00:00:00+00:00"},
            {"symbol": "SOL/USDT", "side": "LONG", "entry_px": 50, "exit_px": 55,
             "pnl_usd": 8.0, "outcome": "WIN", "exit_reason": "TP2", "score": 18,
             "opened_at": "2026-05-05T00:00:00+00:00",
             "closed_at": "2026-05-06T00:00:00+00:00"},
        ]
        conn = self._make_monthly_db(trades=trades, balance=1013.0)
        fetched = monthly_report._fetch_closed_trades(
            conn, (now - timedelta(days=30)).isoformat(), now.isoformat()
        )
        conn.close()
        m = monthly_report.calc_trade_metrics(fetched)
        self.assertEqual(m["total"], 3)
        self.assertEqual(m["wins"], 2)
        self.assertEqual(m["losses"], 1)
        self.assertAlmostEqual(m["total_pnl"], 13.0)
        self.assertAlmostEqual(m["win_rate"], 66.7)
        self.assertEqual(m["exit_reasons"]["TP2"], 2)
        self.assertEqual(m["exit_reasons"]["SL"], 1)

    def test_monthly_report_signal_skip_reasons(self):
        """Monthly report counts signal skip reasons correctly."""
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        signals = [
            {"symbol": "BTC/USDT", "side": "LONG", "was_traded": 1,
             "logged_at": "2026-05-01T00:00:00+00:00"},
            {"symbol": "ETH/USDT", "side": "SHORT", "was_traded": 0,
             "skip_reason": "SL_REJECT",
             "logged_at": "2026-05-02T00:00:00+00:00"},
            {"symbol": "SOL/USDT", "side": "LONG", "was_traded": 0,
             "skip_reason": "AI_FILTER_UNAVAILABLE",
             "logged_at": "2026-05-03T00:00:00+00:00"},
            {"symbol": "ADA/USDT", "side": "LONG", "was_traded": 0,
             "skip_reason": "MAX_OPEN",
             "logged_at": "2026-05-04T00:00:00+00:00"},
            {"symbol": "DOT/USDT", "side": "SHORT", "was_traded": 0,
             "skip_reason": "AI_FILTER_UNAVAILABLE",
             "logged_at": "2026-05-05T00:00:00+00:00"},
        ]
        conn = self._make_monthly_db(signals=signals)
        fetched = monthly_report._fetch_signal_log(
            conn, (now - timedelta(days=30)).isoformat(), now.isoformat()
        )
        conn.close()
        sm = monthly_report.calc_signal_metrics(fetched)
        self.assertEqual(sm["total"], 5)
        self.assertEqual(sm["traded"], 1)
        self.assertEqual(sm["skipped"], 4)
        safety = monthly_report.calc_safety_metrics(sm["skip_reasons"])
        self.assertEqual(safety["AI_FILTER_UNAVAILABLE"], 2)
        self.assertEqual(safety["SL_REJECT"], 1)

    def test_monthly_report_missing_db(self):
        """Monthly report handles missing DB gracefully."""
        with mock.patch("monthly_report.DB_PATH", "/nonexistent/path.db"):
            report = monthly_report.build_report(days=30)
        self.assertFalse(report["metadata"]["db_available"])
        self.assertEqual(report["trade_summary"]["total"], 0)

    def test_monthly_report_no_pending_files(self):
        """Monthly report does NOT create any pending approval files."""
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                conn = self._make_monthly_db(balance=1000.0)
                with mock.patch("monthly_report._connect_db", return_value=conn):
                    report = monthly_report.build_report(days=30)
                    monthly_report.save_report(report)
                self.assertFalse(os.path.exists("pending_weights.json"))
                self.assertFalse(os.path.exists("pending_condition_points.json"))
                self.assertFalse(os.path.exists("pending_regime_weights.json"))
                self.assertTrue(os.path.exists("reports/monthly"))
            finally:
                os.chdir(old_cwd)

    def test_monthly_telegram_format(self):
        """Telegram format function produces readable output without crashing."""
        report = {
            "metadata": {
                "generated_at": "2026-05-15T00:00:00+00:00",
                "days": 30, "period_start": "2026-04-15T00:00:00+00:00",
                "period_end": "2026-05-15T00:00:00+00:00",
                "balance": 950.0, "phase": "v0-report-only", "db_available": True,
            },
            "trade_summary": {
                "total": 10, "wins": 4, "losses": 6, "win_rate": 40.0,
                "total_pnl": -15.50, "avg_pnl": -1.55, "max_win": 12.0,
                "max_loss": -8.0, "profit_factor": 0.85, "max_drawdown": -20.0,
                "exit_reasons": {"TP2": 3, "SL": 5, "HTF_REVERSAL": 2},
                "regime_breakdown": {},
            },
            "signal_summary": {
                "total": 50, "traded": 10, "skipped": 40,
                "skip_reasons": {"MAX_OPEN": 20, "SL_REJECT": 5, "CORR_LIMIT": 15},
            },
            "safety_summary": {"SL_REJECT": 5},
            "thai_explanation": "ระบบมี win rate ต่ำ — ควร review",
        }
        msg = monthly_report.format_telegram_message(report)
        self.assertIn("Monthly Report", msg)
        self.assertIn("40.0%", msg)
        self.assertIn("-15.50", msg)
        self.assertIn("SL_REJECT", msg)
        self.assertIn("report-only", msg)


    # ── Telegram signal dedupe tests ────────────────────────────────

    def _setup_dedupe(self, tmp, notified_data=None):
        """Helper: set up temp notified_signals.json for dedupe tests."""
        path = os.path.join(tmp, "notified_signals.json")
        if notified_data:
            with open(path, "w") as f:
                json.dump(notified_data, f)
        return path

    def test_dedupe_same_ts_suppressed(self):
        """Same symbol+side with same timestamp → suppressed."""
        with tempfile.TemporaryDirectory() as tmp:
            ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            path = self._setup_dedupe(tmp, {"ADA/USDT_SHORT": ts})
            with mock.patch("notify.NOTIFIED_PATH", path):
                sig = {"symbol": "ADA/USDT", "side": "SHORT", "ts": ts}
                self.assertTrue(notify.is_already_notified(sig))

    def test_dedupe_older_ts_suppressed(self):
        """Same symbol+side with older signal timestamp → suppressed."""
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            stored_ts = (now - timedelta(minutes=1)).isoformat()
            older_ts  = (now - timedelta(hours=1)).isoformat()
            path = self._setup_dedupe(tmp, {"ADA/USDT_SHORT": stored_ts})
            with mock.patch("notify.NOTIFIED_PATH", path):
                sig = {"symbol": "ADA/USDT", "side": "SHORT", "ts": older_ts}
                self.assertTrue(notify.is_already_notified(sig))

    def test_dedupe_newer_ts_allowed(self):
        """Same symbol+side with newer signal timestamp → allowed."""
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            stored_ts = (now - timedelta(hours=2)).isoformat()
            newer_ts  = (now - timedelta(minutes=1)).isoformat()
            path = self._setup_dedupe(tmp, {"ADA/USDT_SHORT": stored_ts})
            with mock.patch("notify.NOTIFIED_PATH", path):
                sig = {"symbol": "ADA/USDT", "side": "SHORT", "ts": newer_ts}
                self.assertFalse(notify.is_already_notified(sig))

    def test_dedupe_mark_stores_sig_ts(self):
        """mark_notified stores sig['ts'], not current UTC time."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup_dedupe(tmp)
            sig_ts = "2026-05-15T11:01:00+00:00"
            with mock.patch("notify.NOTIFIED_PATH", path):
                notify.mark_notified({"symbol": "BTC/USDT", "side": "LONG", "ts": sig_ts})
                with open(path) as f:
                    data = json.load(f)
            self.assertEqual(data["BTC/USDT_LONG"], sig_ts)

    def test_dedupe_missing_ts_suppressed(self):
        """Key exists but signal has no ts → suppressed to avoid spam."""
        with tempfile.TemporaryDirectory() as tmp:
            stored_ts = datetime.now(timezone.utc).isoformat()
            path = self._setup_dedupe(tmp, {"ETH/USDT_LONG": stored_ts})
            with mock.patch("notify.NOTIFIED_PATH", path):
                sig = {"symbol": "ETH/USDT", "side": "LONG"}
                self.assertTrue(notify.is_already_notified(sig))

    def test_dedupe_ai_filter_newer_ts_allowed(self):
        """AI_FILTER_UNAVAILABLE alert-only signal with newer ts → allowed."""
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            stored_ts = (now - timedelta(hours=3)).isoformat()
            newer_ts  = (now - timedelta(minutes=1)).isoformat()
            path = self._setup_dedupe(tmp, {"SOL/USDT_SHORT": stored_ts})
            with mock.patch("notify.NOTIFIED_PATH", path):
                sig = {
                    "symbol": "SOL/USDT", "side": "SHORT",
                    "ts": newer_ts,
                    "execution_allowed": False,
                    "execution_block_reason": "AI_FILTER_UNAVAILABLE",
                }
                self.assertFalse(notify.is_already_notified(sig))

    # ── Monthly Report: Calendar Month ──────────────────────────

    def test_previous_month_range_normal(self):
        """_previous_month_range on 2026-05-15 → April 2026."""
        now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
        since, until, label = monthly_report._previous_month_range(now)
        self.assertEqual(label, "2026-04")
        self.assertEqual(since, datetime(2026, 4, 1, tzinfo=timezone.utc))
        self.assertEqual(until, datetime(2026, 5, 1, tzinfo=timezone.utc))

    def test_previous_month_range_january(self):
        """_previous_month_range on 2026-01-01 → December 2025."""
        now = datetime(2026, 1, 1, 0, 10, 0, tzinfo=timezone.utc)
        since, until, label = monthly_report._previous_month_range(now)
        self.assertEqual(label, "2025-12")
        self.assertEqual(since, datetime(2025, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(until, datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_previous_month_range_february(self):
        """_previous_month_range on 2026-03-01 → February (28 days)."""
        now = datetime(2026, 3, 1, 0, 10, 0, tzinfo=timezone.utc)
        since, until, label = monthly_report._previous_month_range(now)
        self.assertEqual(label, "2026-02")
        self.assertEqual(since, datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(until, datetime(2026, 3, 1, tzinfo=timezone.utc))
        self.assertEqual((until - since).days, 28)

    def test_previous_month_range_leap_year(self):
        """_previous_month_range on 2028-03-01 → February (29 days, leap year)."""
        now = datetime(2028, 3, 1, 0, 10, 0, tzinfo=timezone.utc)
        since, until, label = monthly_report._previous_month_range(now)
        self.assertEqual(label, "2028-02")
        self.assertEqual((until - since).days, 29)

    def test_month_range_specific(self):
        """_month_range('2026-04') → April 1-30."""
        since, until, label = monthly_report._month_range("2026-04")
        self.assertEqual(label, "2026-04")
        self.assertEqual(since, datetime(2026, 4, 1, tzinfo=timezone.utc))
        self.assertEqual(until, datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.assertEqual((until - since).days, 30)

    def test_month_range_invalid_format(self):
        """_month_range with bad format → ValueError."""
        with self.assertRaises(ValueError):
            monthly_report._month_range("2026")
        with self.assertRaises(ValueError):
            monthly_report._month_range("2026-13")

    def test_build_report_month_metadata(self):
        """build_report_month sets mode=calendar-month and month label."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE trades (
                id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
                entry_px REAL, exit_px REAL, pnl_usd REAL,
                outcome TEXT, exit_reason TEXT, score REAL,
                opened_at TEXT, closed_at TEXT, tp1_hit INTEGER,
                tp1_px REAL, qty REAL, notional_usd REAL,
                regime TEXT, status TEXT)""")
            conn.execute("""CREATE TABLE signal_log (
                symbol TEXT, side TEXT, score REAL, was_traded INTEGER,
                skip_reason TEXT, logged_at TEXT, outcome TEXT)""")
            conn.execute("CREATE TABLE portfolio (id INTEGER PRIMARY KEY, balance REAL)")
            conn.execute("INSERT INTO portfolio VALUES (1, 1000.0)")
            conn.commit()
            conn.close()

            with mock.patch.object(monthly_report, "DB_PATH", db_path):
                since = datetime(2026, 4, 1, tzinfo=timezone.utc)
                until = datetime(2026, 5, 1, tzinfo=timezone.utc)
                report = monthly_report.build_report_month(since, until, "2026-04")

            meta = report["metadata"]
            self.assertEqual(meta["mode"], "calendar-month")
            self.assertEqual(meta["month"], "2026-04")
            self.assertEqual(meta["days"], 30)
            self.assertTrue(meta["db_available"])

    def test_save_report_calendar_filename(self):
        """save_report uses YYYY-MM_monthly_report.json for calendar month."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(monthly_report, "REPORTS_DIR", tmp):
                report = {
                    "metadata": {
                        "generated_at": "2026-05-01T00:10:00+00:00",
                        "days": 30, "month": "2026-04",
                        "mode": "calendar-month",
                        "period_start": "2026-04-01T00:00:00+00:00",
                        "period_end": "2026-05-01T00:00:00+00:00",
                        "balance": 1000.0,
                        "phase": "v0-report-only", "db_available": True,
                    },
                    "trade_summary": monthly_report.calc_trade_metrics([]),
                    "signal_summary": monthly_report.calc_signal_metrics([]),
                    "safety_summary": {},
                    "thai_explanation": "test",
                }
                path = monthly_report.save_report(report)
                self.assertTrue(path.endswith("2026-04_monthly_report.json"))
                self.assertTrue(os.path.exists(path))

    def test_save_report_days_filename_unchanged(self):
        """save_report still uses old format for --days mode."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(monthly_report, "REPORTS_DIR", tmp):
                report = {
                    "metadata": {
                        "generated_at": "2026-05-15T10:00:00+00:00",
                        "days": 30,
                        "period_start": "2026-04-15T10:00:00+00:00",
                        "period_end": "2026-05-15T10:00:00+00:00",
                        "balance": 1000.0,
                        "phase": "v0-report-only", "db_available": True,
                    },
                    "trade_summary": monthly_report.calc_trade_metrics([]),
                    "signal_summary": monthly_report.calc_signal_metrics([]),
                    "safety_summary": {},
                    "thai_explanation": "test",
                }
                path = monthly_report.save_report(report)
                self.assertTrue(path.endswith("2026-05-15_30d_report.json"))

    def test_telegram_message_calendar_month_title(self):
        """format_telegram_message shows month label for calendar-month mode."""
        report = {
            "metadata": {
                "days": 30, "month": "2026-04",
                "mode": "calendar-month",
                "period_start": "2026-04-01T00:00:00+00:00",
                "period_end": "2026-05-01T00:00:00+00:00",
                "balance": 1000.0,
            },
            "trade_summary": monthly_report.calc_trade_metrics([]),
            "signal_summary": monthly_report.calc_signal_metrics([]),
            "safety_summary": {},
            "thai_explanation": "test",
        }
        msg = monthly_report.format_telegram_message(report)
        self.assertIn("2026-04", msg)
        self.assertNotIn("30 วัน", msg)


if __name__ == "__main__":
    unittest.main()
