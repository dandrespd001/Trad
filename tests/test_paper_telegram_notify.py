import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperTelegramNotifyTests(unittest.TestCase):
    def test_parser_accepts_paper_telegram_notify_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-telegram-notify",
                "--as-of-date",
                "2026-06-16",
                "--artifact",
                "reports/tmp/paper_telegram_status/latest.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.artifact, ["reports/tmp/paper_telegram_status/latest.json"])
        self.assertEqual(args.output, "reports/tmp/paper_telegram_notify/latest.json")
        self.assertEqual(args.send_output_dir, "reports/tmp/paper_telegram_send")
        self.assertIsNone(args.ledger_output)
        self.assertFalse(args.send_telegram)
        self.assertFalse(args.telegram_dry_run)

    def test_notify_dry_run_preflights_multiple_artifacts_and_writes_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status = write_json(root / "status.json", telegram_artifact(message="Status OK"))
            history = write_json(root / "history.json", telegram_artifact(message="History OK", status="WARN"))
            output = root / "notify.json"
            ledger = root / "notify.jsonl"
            send_dir = root / "send_reports"

            exit_code = main(
                [
                    "paper-telegram-notify",
                    "--as-of-date",
                    "2026-06-16",
                    "--artifact",
                    str(status),
                    "--artifact",
                    str(history),
                    "--send-output-dir",
                    str(send_dir),
                    "--output",
                    str(output),
                    "--ledger-output",
                    str(ledger),
                ]
            )
            report = read_json(output)
            ledger_rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            send_reports_exist = all(Path(str(item["send_report_path"])).exists() for item in report["notifications"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["summary"]["artifact_count"], 2)
        self.assertEqual(report["summary"]["dry_run_count"], 2)
        self.assertEqual(report["summary"]["sent_count"], 0)
        self.assertEqual(report["summary"]["blocked_count"], 0)
        self.assertEqual([item["telegram_status"] for item in report["notifications"]], ["DRY_RUN", "DRY_RUN"])
        self.assertTrue(send_reports_exist)
        self.assertFalse(report["safety"]["credentials_read"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertEqual([row["record_type"] for row in ledger_rows], ["paper_telegram_notify"])

    def test_notify_blocks_batch_when_any_artifact_is_not_sendable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status = write_json(root / "status.json", telegram_artifact(message="Status OK"))
            blocked = write_json(root / "blocked.json", telegram_artifact(message="Blocked", status="BLOCKED"))
            output = root / "notify.json"

            exit_code = main(
                [
                    "paper-telegram-notify",
                    "--as-of-date",
                    "2026-06-16",
                    "--artifact",
                    str(status),
                    "--artifact",
                    str(blocked),
                    "--send-telegram",
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["summary"]["blocked_count"], 1)
        self.assertEqual(report["summary"]["sent_count"], 0)
        self.assertIn("artifact_blocked", report["blockers"])
        self.assertFalse(report["safety"]["credentials_read"])
        self.assertTrue(all(item["telegram_status"] != "SENT" for item in report["notifications"]))


def telegram_artifact(
    *,
    message: str,
    status: str = "OK",
    safety: dict[str, object] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "as_of_date": "2026-06-16",
        "status": status,
        "message": message,
        "telegram": {"send_enabled": False, "sent": False, "parse_mode": "plain_text"},
        "blockers": [],
        "safety": safety or safe(),
    }


def safe() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
