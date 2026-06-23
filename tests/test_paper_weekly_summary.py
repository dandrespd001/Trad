import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperWeeklySummaryTests(unittest.TestCase):
    def test_parser_defaults_for_weekly_summary(self) -> None:
        args = build_parser().parse_args(["paper-weekly-summary"])

        self.assertEqual(args.decisions_root, "reports/tmp/paper_decisions")
        self.assertEqual(args.performance_root, "reports/tmp/paper_performance")
        self.assertEqual(args.campaign_root, "reports/tmp/paper_campaign")
        self.assertEqual(args.ledger_input, [])
        self.assertEqual(args.output_dir, "reports/tmp/paper_weekly_summary")
        self.assertEqual(args.week, "auto")
        self.assertEqual(args.as_of_date, "today")
        self.assertEqual(args.history_weeks, 1)

        args = build_parser().parse_args(["paper-weekly-summary", "--history-weeks", "4"])
        self.assertEqual(args.history_weeks, 4)

    def test_five_continue_decisions_produce_ok_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for day in range(15, 20):
                write_decision(root / "decisions" / f"2026-06-{day}" / "decision.json", "CONTINUE")
            write_performance(root / "performance" / "latest.json", status="OK")
            write_ledger(
                root / "ledger.jsonl",
                [
                    {"event_type": "paper_session", "status": "READY", "as_of_date": "2026-06-15"},
                    {"event_type": "paper_closeout", "status": "CLOSED", "symbol": "SPY"},
                ],
            )

            exit_code = main(weekly_args(root, ledger=root / "ledger.jsonl"))
            payload = read_json(root / "weekly" / "2026-W25" / "weekly_summary.json")
            markdown = (root / "weekly" / "2026-W25" / "weekly_summary.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["decisions"]["counts"]["CONTINUE"], 5)
        self.assertEqual(payload["ledger"]["sessions_closed"], 1)
        self.assertEqual(payload["ledger"]["fills"], 1)
        self.assertEqual(payload["performance"]["warnings"], [])
        self.assertIn("Status: **OK**", markdown)

    def test_stop_decision_produces_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_decision(root / "decisions" / "2026-06-15" / "decision.json", "CONTINUE")
            write_decision(root / "decisions" / "2026-06-16" / "decision.json", "STOP", blockers=["broker_run_error"])

            exit_code = main(weekly_args(root))
            payload = read_json(root / "weekly" / "2026-W25" / "weekly_summary.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertEqual(payload["decisions"]["counts"]["STOP"], 1)
        self.assertIn("stop_decision", blocker_codes(payload))

    def test_recurrent_review_decisions_produce_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_decision(
                root / "decisions" / "2026-06-15" / "decision.json", "REVIEW", blockers=["missing_backtest_report"]
            )
            write_decision(
                root / "decisions" / "2026-06-16" / "decision.json", "REVIEW", blockers=["missing_backtest_report"]
            )

            exit_code = main(weekly_args(root))
            payload = read_json(root / "weekly" / "2026-W25" / "weekly_summary.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["decisions"]["counts"]["REVIEW"], 2)
        self.assertIn("recurrent_review", blocker_codes(payload))
        self.assertIn("missing_backtest_report", payload["blockers"]["recurrent"])

    def test_invalid_decision_json_writes_error_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "decisions" / "2026-06-15" / "decision.json"
            path.parent.mkdir(parents=True)
            path.write_text("{bad json", encoding="utf-8")

            exit_code = main(weekly_args(root))
            payload = read_json(root / "weekly" / "2026-W25" / "weekly_summary.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("decision_invalid_json", blocker_codes(payload))

    def test_secret_like_values_are_redacted_in_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_decision(
                root / "decisions" / "2026-06-15" / "decision.json",
                "REVIEW",
                blockers=["api_key=KEY secret_key=SECRET token=TOKEN"],
            )

            exit_code = main(weekly_args(root))
            output = (root / "weekly" / "2026-W25" / "weekly_summary.json").read_text(encoding="utf-8")
            markdown = (root / "weekly" / "2026-W25" / "weekly_summary.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("KEY", output)
        self.assertNotIn("SECRET", output)
        self.assertNotIn("TOKEN", output)
        self.assertNotIn("KEY", markdown)
        self.assertNotIn("SECRET", markdown)
        self.assertNotIn("TOKEN", markdown)
        self.assertIn("[redacted]", output)

    def test_same_blocker_in_historical_weeks_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_decision(root / "decisions" / "2026-06-19" / "decision.json", "CONTINUE")
            write_decision(
                root / "decisions" / "2026-06-10" / "decision.json",
                "REVIEW",
                blockers=["missing_backtest_report"],
            )
            write_decision(
                root / "decisions" / "2026-06-11" / "decision.json",
                "REVIEW",
                blockers=["missing_backtest_report"],
            )
            write_performance(root / "performance" / "2026-06-19" / "latest.json", status="OK")

            exit_code = main([*weekly_args(root), "--history-weeks", "4"])
            payload = read_json(root / "weekly" / "2026-W25" / "weekly_summary.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("blocker_aging", payload)
        self.assertIn("missing_backtest_report", payload["blocker_aging"]["recurrent_blockers"])
        self.assertIn("missing_backtest_report", payload["blocker_aging"]["historical_recurrent_blockers"])

    def test_historical_invalid_json_warns_but_current_invalid_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_decision(root / "decisions" / "2026-06-19" / "decision.json", "CONTINUE")
            historical = root / "decisions" / "2026-06-10" / "decision.json"
            historical.parent.mkdir(parents=True)
            historical.write_text("{bad json", encoding="utf-8")

            exit_code = main([*weekly_args(root), "--history-weeks", "4"])
            payload = read_json(root / "weekly" / "2026-W25" / "weekly_summary.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("history_invalid_json", payload["blocker_aging"]["warnings"])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "decisions" / "2026-06-19" / "decision.json"
            current.parent.mkdir(parents=True)
            current.write_text("{bad json", encoding="utf-8")

            exit_code = main([*weekly_args(root), "--history-weeks", "4"])
            payload = read_json(root / "weekly" / "2026-W25" / "weekly_summary.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")


def weekly_args(root: Path, *, ledger: Path | None = None) -> list[str]:
    args = [
        "paper-weekly-summary",
        "--decisions-root",
        str(root / "decisions"),
        "--performance-root",
        str(root / "performance"),
        "--campaign-root",
        str(root / "campaign"),
        "--output-dir",
        str(root / "weekly"),
        "--week",
        "2026-W25",
        "--as-of-date",
        "2026-06-19",
    ]
    if ledger is not None:
        args.extend(["--ledger-input", str(ledger)])
    return args


def write_decision(path: Path, decision: str, *, blockers: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-06-19T22:00:00+00:00",
        "as_of_date": path.parent.name,
        "decision": decision,
        "state": decision,
        "blockers": [{"severity": "WARNING", "code": code, "message": code} for code in blockers or []],
        "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_performance(path: Path, *, status: str) -> None:
    path.parent.mkdir(parents=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-06-19T22:05:00+00:00",
        "status": status,
        "paper_metrics": {
            "complete_sessions": 5,
            "fills": 5,
            "pending_closeouts": 0,
            "unmatched_closeouts": 0,
        },
        "warnings": [],
        "blockers": [],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_ledger(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def blocker_codes(payload: dict[str, object]) -> set[str]:
    return {str(blocker["code"]) for blocker in payload["blockers"]["items"]}


if __name__ == "__main__":
    unittest.main()
