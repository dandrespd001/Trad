import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperOpsCheckTests(unittest.TestCase):
    def test_parser_defaults_for_ops_check(self) -> None:
        args = build_parser().parse_args(["paper-ops-check", "--as-of-date", "2026-06-16"])

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.readiness_root, "reports/tmp/paper_daily_prepare")
        self.assertEqual(args.sessions_root, "reports/tmp/paper_session")
        self.assertEqual(args.monitor_root, "reports/tmp/paper_monitor")
        self.assertEqual(args.campaign_root, "reports/tmp/paper_campaign")
        self.assertEqual(args.decisions_root, "reports/tmp/paper_decisions")
        self.assertEqual(args.performance_root, "reports/tmp/paper_performance")
        self.assertEqual(args.ledger_input, [])
        self.assertEqual(args.output_dir, "reports/tmp/paper_ops_check")

    def test_complete_continue_day_produces_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")
            markdown = (root / "ops" / "2026-06-16" / "ops_check.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["artifacts"]["readiness"]["status"], "READY")
        self.assertEqual(payload["artifacts"]["decision"]["decision"], "CONTINUE")
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertIn("Status: **OK**", markdown)

    def test_missing_performance_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE", include_performance=False)

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("missing_performance", issue_codes(payload))

    def test_stop_decision_produces_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="STOP")

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertIn("decision_stop", issue_codes(payload))

    def test_invalid_required_json_produces_error_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            (root / "readiness" / "2026-06-16" / "readiness.json").write_text("{bad json", encoding="utf-8")

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_readiness_json", issue_codes(payload))

    def test_secret_like_values_are_redacted_in_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            write_json(
                root / "decisions" / "2026-06-16" / "decision.json",
                {
                    "status": "OK",
                    "decision": "CONTINUE",
                    "as_of_date": "2026-06-16",
                    "reason": "api_key=KEY secret_key=SECRET token=TOKEN",
                    "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
                },
            )

            exit_code = main(ops_args(root))
            output = (root / "ops" / "2026-06-16" / "ops_check.json").read_text(encoding="utf-8")
            markdown = (root / "ops" / "2026-06-16" / "ops_check.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("KEY", output)
        self.assertNotIn("SECRET", output)
        self.assertNotIn("TOKEN", output)
        self.assertNotIn("KEY", markdown)
        self.assertNotIn("SECRET", markdown)
        self.assertNotIn("TOKEN", markdown)
        self.assertIn("[redacted]", output)


def ops_args(root: Path) -> list[str]:
    return [
        "paper-ops-check",
        "--as-of-date",
        "2026-06-16",
        "--readiness-root",
        str(root / "readiness"),
        "--sessions-root",
        str(root / "sessions"),
        "--monitor-root",
        str(root / "monitor"),
        "--campaign-root",
        str(root / "campaign"),
        "--decisions-root",
        str(root / "decisions"),
        "--performance-root",
        str(root / "performance"),
        "--output-dir",
        str(root / "ops"),
    ]


def write_complete_day(root: Path, *, decision: str, include_performance: bool = True) -> None:
    write_json(
        root / "readiness" / "2026-06-16" / "readiness.json",
        {"status": "READY", "ready_for_paper_daily": True, "as_of_date": "2026-06-16", "reasons": []},
    )
    write_json(
        root / "monitor" / "2026-06-16" / "monitor.json",
        {
            "status": "OK",
            "monitor_summary": {
                "as_of_date": "2026-06-16",
                "critical_count": 0,
                "warning_count": 0,
                "pending_closeout_count": 0,
                "unmatched_closeout_count": 0,
            },
            "alerts": [],
        },
    )
    write_json(root / "campaign" / "2026-06-16" / "campaign.json", {"status": "OK", "as_of_date": "2026-06-16"})
    write_json(
        root / "decisions" / "2026-06-16" / "decision.json",
        {
            "status": "OK",
            "decision": decision,
            "as_of_date": "2026-06-16",
            "blockers": [] if decision == "CONTINUE" else [{"severity": "CRITICAL", "code": "manual_stop"}],
            "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
        },
    )
    if include_performance:
        write_json(
            root / "performance" / "2026-06-16" / "performance.json",
            {
                "status": "OK",
                "paper_metrics": {"pending_closeouts": 0, "unmatched_closeouts": 0},
                "statement_reconciliation": {"status": "MATCHED"},
            },
        )


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def issue_codes(payload: dict[str, object]) -> set[str]:
    return {str(issue["code"]) for issue in payload["issues"]}


if __name__ == "__main__":
    unittest.main()
