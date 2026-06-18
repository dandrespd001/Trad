import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperDayCloseTests(unittest.TestCase):
    def test_parser_defaults_for_day_close(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-day-close",
                "--readiness",
                "readiness.json",
                "--broker-run",
                "broker_run.json",
                "--monitor",
                "monitor.json",
                "--campaign-report",
                "campaign.json",
            ]
        )

        self.assertEqual(args.output_dir, "reports/tmp/paper_decisions")
        self.assertEqual(args.as_of_date, "auto")
        self.assertIsNone(args.operator)
        self.assertIsNone(args.reason)
        self.assertIsNone(args.ledger_output)

    def test_monitor_status_maps_to_auditable_decision(self) -> None:
        cases = {
            "OK": (0, "CONTINUE"),
            "WARN": (0, "REVIEW"),
            "CRITICAL": (1, "STOP"),
        }
        for monitor_status, (expected_exit, expected_decision) in cases.items():
            with self.subTest(monitor_status=monitor_status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                paths = write_day_close_inputs(root, monitor_status=monitor_status)
                ledger = root / "ledger.jsonl"

                exit_code = main(day_close_args(paths, output_dir=root / "decisions", ledger=ledger))
                payload = read_json(root / "decisions" / "2026-06-16" / "decision.json")
                markdown = (root / "decisions" / "2026-06-16" / "decision.md").read_text(encoding="utf-8")
                ledger_event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(payload["decision"], expected_decision)
            self.assertEqual(payload["state"], expected_decision)
            self.assertEqual(payload["as_of_date"], "2026-06-16")
            self.assertFalse(payload["safety"]["live_trading_authorized"])
            self.assertEqual(payload["artifacts"]["monitor"]["status"], monitor_status)
            self.assertEqual(len(payload["artifacts"]["monitor"]["sha256"]), 64)
            self.assertIn(f"Decision: **{expected_decision}**", markdown)
            self.assertEqual(ledger_event["event_type"], "paper_day_decision")
            self.assertEqual(ledger_event["status"], expected_decision)

    def test_invalid_artifact_writes_error_decision_and_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = write_day_close_inputs(root, monitor_status="OK")
            paths["monitor"].write_text("{bad json", encoding="utf-8")

            exit_code = main(
                day_close_args(paths, output_dir=root / "decisions", extra=["--as-of-date", "2026-06-16"])
            )
            payload = read_json(root / "decisions" / "2026-06-16" / "decision.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["decision"], "ERROR")
        self.assertIn("invalid_json", {blocker["code"] for blocker in payload["blockers"]})

    def test_day_close_redacts_secrets_in_json_markdown_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = write_day_close_inputs(root, monitor_status="WARN")
            ledger = root / "ledger.jsonl"
            secret_reason = "api_key=KEY secret_key=SECRET token=TOKEN"

            exit_code = main(
                day_close_args(
                    paths,
                    output_dir=root / "decisions",
                    ledger=ledger,
                    extra=["--operator", "ops", "--reason", secret_reason],
                )
            )
            json_text = (root / "decisions" / "2026-06-16" / "decision.json").read_text(encoding="utf-8")
            markdown_text = (root / "decisions" / "2026-06-16" / "decision.md").read_text(encoding="utf-8")
            ledger_text = ledger.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        for text in (json_text, markdown_text, ledger_text):
            self.assertNotIn("KEY", text)
            self.assertNotIn("SECRET", text)
            self.assertNotIn("TOKEN", text)
            self.assertIn("[redacted]", text)


def day_close_args(
    paths: dict[str, Path],
    *,
    output_dir: Path,
    ledger: Path | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    args = [
        "paper-day-close",
        "--readiness",
        str(paths["readiness"]),
        "--broker-run",
        str(paths["broker_run"]),
        "--monitor",
        str(paths["monitor"]),
        "--campaign-report",
        str(paths["campaign"]),
        "--output-dir",
        str(output_dir),
    ]
    if ledger is not None:
        args.extend(["--ledger-output", str(ledger)])
    if extra:
        args.extend(extra)
    return args


def write_day_close_inputs(root: Path, *, monitor_status: str) -> dict[str, Path]:
    readiness = root / "readiness.json"
    broker_run = root / "broker_run.json"
    monitor = root / "monitor.json"
    campaign = root / "campaign.json"
    write_json(
        readiness,
        {
            "status": "READY",
            "ready_for_paper_daily": True,
            "exit_code": 0,
            "as_of_date": "2026-06-16",
            "reasons": [],
        },
    )
    write_json(
        broker_run,
        {
            "status": "OK",
            "exit_code": 0,
            "as_of_date": "2026-06-16",
            "artifacts": {"monitor_json": str(monitor)},
            "reasons": [],
        },
    )
    write_json(
        monitor,
        {
            "status": monitor_status,
            "monitor_summary": {"as_of_date": "2026-06-16"},
            "alerts": [] if monitor_status == "OK" else [{"severity": "WARNING", "code": "review"}],
            "safety": {"live_trading_authorized": False},
        },
    )
    write_json(
        campaign,
        {
            "status": "OK" if monitor_status == "OK" else monitor_status,
            "as_of_date": "2026-06-16",
            "blockers": [],
            "progress": {"live_trading_authorized": False},
            "safety": {"live_trading_authorized": False},
        },
    )
    return {"readiness": readiness, "broker_run": broker_run, "monitor": monitor, "campaign": campaign}


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
