import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperTelegramSendTests(unittest.TestCase):
    def test_parser_accepts_paper_telegram_send_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-telegram-send",
                "--as-of-date",
                "2026-06-16",
                "--artifact",
                "reports/tmp/paper_telegram_status/latest.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.artifact, "reports/tmp/paper_telegram_status/latest.json")
        self.assertEqual(args.output, "reports/tmp/paper_telegram_send/latest.json")
        self.assertFalse(args.send_telegram)
        self.assertFalse(args.telegram_dry_run)

    def test_dry_run_records_safe_local_message_without_reading_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = write_json(root / "status.json", telegram_artifact(message="Paper trading 2026-06-16\nStatus: OK"))
            output = root / "send.json"

            exit_code = main(
                [
                    "paper-telegram-send",
                    "--as-of-date",
                    "2026-06-16",
                    "--artifact",
                    str(artifact),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["source"]["artifact_status"], "OK")
        self.assertEqual(report["telegram"]["status"], "DRY_RUN")
        self.assertFalse(report["telegram"]["send_enabled"])
        self.assertTrue(report["telegram"]["dry_run"])
        self.assertFalse(report["telegram"]["sent"])
        self.assertEqual(report["telegram"]["message_length"], len("Paper trading 2026-06-16\nStatus: OK"))
        self.assertFalse(report["safety"]["credentials_read"])
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_blocks_unsafe_artifact_even_when_send_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = write_json(
                root / "unsafe.json",
                telegram_artifact(
                    message="Do not send",
                    safety={
                        "paper_only": True,
                        "broker_client_built": False,
                        "credentials_read": True,
                        "orders_submitted": False,
                        "live_trading_authorized": False,
                        "live_trading_allowed": False,
                    },
                ),
            )
            output = root / "send.json"

            exit_code = main(
                [
                    "paper-telegram-send",
                    "--as-of-date",
                    "2026-06-16",
                    "--artifact",
                    str(artifact),
                    "--send-telegram",
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("source_credentials_read", report["blockers"])
        self.assertFalse(report["telegram"]["send_enabled"])
        self.assertFalse(report["telegram"]["sent"])
        self.assertFalse(report["safety"]["orders_submitted"])


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
