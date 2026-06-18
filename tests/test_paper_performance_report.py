import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperPerformanceReportTests(unittest.TestCase):
    def test_parser_defaults_for_performance_report(self) -> None:
        args = build_parser().parse_args(["paper-performance-report"])

        self.assertEqual(args.sessions_root, "reports/tmp/paper_session")
        self.assertEqual(args.session_dir, [])
        self.assertEqual(args.ledger_input, [])
        self.assertIsNone(args.backtest_report)
        self.assertIsNone(args.broker_statement)
        self.assertEqual(args.output, "reports/tmp/paper_performance/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/paper_performance/latest.md")

    def test_closed_session_with_fill_produces_paper_only_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            output = root / "performance.json"
            markdown = root / "performance.md"

            exit_code = main(performance_args(root / "sessions", output=output, markdown=markdown))
            payload = read_json(output)
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["paper_metrics"]["complete_sessions"], 1)
        self.assertEqual(payload["paper_metrics"]["submits"], 1)
        self.assertEqual(payload["paper_metrics"]["fills"], 1)
        self.assertEqual(payload["paper_metrics"]["symbols"], ["SPY"])
        self.assertEqual(payload["paper_metrics"]["pnl"]["source"], "proxy")
        self.assertFalse(payload["paper_metrics"]["performance_stable"])
        self.assertIn("missing_backtest_report", payload["warnings"])
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertIn("PnL source: `proxy`", markdown_text)

    def test_pending_or_unmatched_closeout_blocks_stable_performance(self) -> None:
        for status in ("PENDING", "UNMATCHED"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_performance_session(root / "sessions" / status.lower(), closeout_status=status)
                output = root / "performance.json"

                exit_code = main(
                    performance_args(root / "sessions", output=output, markdown=root / "performance.md")
                )
                payload = read_json(output)

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "WARN")
            self.assertFalse(payload["paper_metrics"]["performance_stable"])
            self.assertIn(f"closeout_{status.lower()}", payload["blockers"])

    def test_missing_fill_price_warns_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(
                root / "sessions" / "daily" / "2026-06-16",
                closeout_status="CLOSED",
                filled_avg_price=None,
            )
            output = root / "performance.json"

            exit_code = main(performance_args(root / "sessions", output=output, markdown=root / "performance.md"))
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertIn("missing_fill_price", payload["warnings"])
        self.assertEqual(payload["paper_metrics"]["fills"], 1)

    def test_backtest_report_adds_gap_section_without_authorizing_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            backtest = root / "backtest.json"
            write_json(backtest, {"metrics": {"trade_count": 10, "turnover": 0.2, "estimated_costs": 0.01}})
            output = root / "performance.json"

            exit_code = main(
                performance_args(
                    root / "sessions",
                    output=output,
                    markdown=root / "performance.md",
                    extra=["--backtest-report", str(backtest)],
                )
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["paper_vs_backtest"]["backtest_available"], True)
        self.assertEqual(payload["paper_vs_backtest"]["backtest_metrics"]["trade_count"], 10)
        self.assertFalse(payload["safety"]["live_trading_authorized"])

    def test_valid_broker_statement_switches_pnl_source_to_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            statement = root / "statement.json"
            write_statement(statement, client_order_id="signal-spy-20260616")
            output = root / "performance.json"

            exit_code = main(
                performance_args(
                    root / "sessions",
                    output=output,
                    markdown=root / "performance.md",
                    extra=["--broker-statement", str(statement)],
                )
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["paper_metrics"]["pnl"]["source"], "broker_statement")
        self.assertTrue(payload["paper_metrics"]["pnl"]["broker_statement"])
        self.assertEqual(payload["paper_metrics"]["pnl"]["realized_pnl"], 0.03)
        self.assertEqual(payload["statement_reconciliation"]["matched_fills"], 1)
        self.assertFalse(payload["safety"]["live_trading_authorized"])

    def test_missing_statement_keeps_proxy_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            output = root / "performance.json"

            exit_code = main(
                performance_args(
                    root / "sessions",
                    output=output,
                    markdown=root / "performance.md",
                    extra=["--broker-statement", str(root / "missing_statement.json")],
                )
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["paper_metrics"]["pnl"]["source"], "proxy")
        self.assertIn("missing_broker_statement", payload["warnings"])

    def test_local_fill_without_statement_match_produces_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            statement = root / "statement.json"
            write_statement(statement, client_order_id="other-order")
            output = root / "performance.json"

            exit_code = main(
                performance_args(
                    root / "sessions",
                    output=output,
                    markdown=root / "performance.md",
                    extra=["--broker-statement", str(statement)],
                )
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("statement_missing_fill", payload["blockers"])
        self.assertEqual(payload["statement_reconciliation"]["missing_fills"], 1)

    def test_invalid_statement_writes_error_report_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_performance_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            statement = root / "statement.json"
            statement.write_text("{bad json", encoding="utf-8")
            output = root / "performance.json"

            exit_code = main(
                performance_args(
                    root / "sessions",
                    output=output,
                    markdown=root / "performance.md",
                    extra=["--broker-statement", str(statement)],
                )
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_broker_statement", payload["blockers"])


def performance_args(
    sessions_root: Path,
    *,
    output: Path,
    markdown: Path,
    extra: list[str] | None = None,
) -> list[str]:
    args = [
        "paper-performance-report",
        "--sessions-root",
        str(sessions_root),
        "--output",
        str(output),
        "--markdown-output",
        str(markdown),
    ]
    if extra:
        args.extend(extra)
    return args


def write_performance_session(
    session_dir: Path,
    *,
    closeout_status: str,
    filled_avg_price: float | None = 500.0,
) -> None:
    (session_dir / "audit").mkdir(parents=True)
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    (session_dir / "execution").mkdir()
    (session_dir / "closeout").mkdir()
    expected_order = {
        "symbol": "SPY",
        "side": "buy",
        "client_order_id": "signal-spy-20260616",
        "type": "market",
        "time_in_force": "day",
        "notional": 1.0,
    }
    write_json(
        session_dir / "session.json",
        {
            "ready_for_paper_review": True,
            "exit_code": 0,
            "as_of_date": "2026-06-16",
            "paths": {
                "audit_report": str(session_dir / "audit" / "paper_audit.json"),
                "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
                "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
            },
        },
    )
    write_json(
        session_dir / "audit" / "paper_audit.json",
        {"ready_for_paper_review": True, "summary": {"fail_count": 0}, "findings": []},
    )
    write_json(
        session_dir / "paper" / "paper_signal_order.json",
        {
            "submitted": True,
            "freshness_allowed": True,
            "preflight": {"allowed": True, "reasons": []},
            "order_intent": expected_order,
        },
    )
    write_json(session_dir / "fresh_data" / "freshness.json", {"allowed": True, "reasons": []})
    write_json(
        session_dir / "execution" / "paper_execution.json",
        {
            "generated_at": "2026-06-16T00:02:00+00:00",
            "status": "SUBMITTED",
            "session": {"session_dir": str(session_dir), "ready_for_paper_review": True},
            "order_sent": expected_order,
            "preflight": {"allowed": True, "reasons": []},
            "broker_result": {"accepted": True, "reasons": []},
        },
    )
    broker_order = {
        "client_order_id": "signal-spy-20260616",
        "symbol": "SPY",
        "side": "buy",
        "status": "filled",
        "notional": 1.0,
        "filled_quantity": 0.002,
        "filled_avg_price": filled_avg_price,
    }
    write_json(
        session_dir / "closeout" / "paper_closeout.json",
        {
            "generated_at": "2026-06-16T00:03:00+00:00",
            "status": closeout_status,
            "session": {"session_dir": str(session_dir), "as_of_date": "2026-06-16"},
            "expected_order": expected_order,
            "broker_order": broker_order,
            "positions": [{"symbol": "SPY", "market_value": "1.01"}],
            "reasons": [] if closeout_status == "CLOSED" else ["not_filled_yet"],
        },
    )


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_statement(path: Path, *, client_order_id: str) -> None:
    write_json(
        path,
        {
            "fills": [
                {
                    "client_order_id": client_order_id,
                    "symbol": "SPY",
                    "side": "buy",
                    "quantity": 0.002,
                    "filled_avg_price": 500.0,
                    "filled_at": "2026-06-16T00:03:00+00:00",
                    "realized_pnl": 0.03,
                }
            ]
        },
    )


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
