import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperTelegramHistoryTests(unittest.TestCase):
    def test_parser_accepts_paper_telegram_history_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-telegram-history",
                "--as-of-date",
                "2026-06-16",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.output, "reports/tmp/paper_telegram_history/latest.json")
        self.assertIsNone(args.performance)
        self.assertIsNone(args.weekly_summary)
        self.assertEqual(args.ledger_input, [])
        self.assertEqual(args.max_events, 5)

    def test_history_message_summarizes_performance_weekly_and_ledger_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            performance = write_json(root / "performance.json", performance_payload())
            weekly = write_json(root / "weekly.json", weekly_payload())
            ledger = write_jsonl(
                root / "ledger.jsonl",
                [
                    {"event_type": "paper_session", "status": "READY", "as_of_date": "2026-06-14"},
                    {
                        "event_type": "paper_execution",
                        "status": "SUBMITTED",
                        "symbol": "SPY",
                        "side": "buy",
                        "notional": 1.0,
                        "generated_at": "2026-06-15T15:00:00+00:00",
                    },
                    {
                        "event_type": "paper_closeout",
                        "status": "CLOSED",
                        "symbol": "SPY",
                        "side": "sell",
                        "notional": 1.2,
                        "generated_at": "2026-06-16T20:50:00+00:00",
                    },
                ],
            )
            output = root / "telegram_history.json"

            exit_code = main(
                [
                    "paper-telegram-history",
                    "--as-of-date",
                    "2026-06-16",
                    "--performance",
                    str(performance),
                    "--weekly-summary",
                    str(weekly),
                    "--ledger-input",
                    str(ledger),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        message = report["message"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "WARN")
        self.assertIn("Paper history 2026-06-16", message)
        self.assertIn("Performance: OK sessions=12 fills=8 PnL=12.34", message)
        self.assertIn("Weekly: WARN CONTINUE=3 REVIEW=1 STOP=0 fills=7", message)
        self.assertIn("Ledger: events=3 executions=1 closeouts=1 pending=0 unmatched=0", message)
        self.assertIn("Recent: paper_closeout CLOSED SPY", message)
        self.assertIn("paper_execution SUBMITTED SPY", message)
        self.assertFalse(report["telegram"]["sent"])
        self.assertFalse(report["safety"]["credentials_read"])
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_history_blocks_if_any_source_declares_live_or_orders_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            performance = write_json(root / "performance.json", performance_payload(live=True))
            output = root / "telegram_history.json"

            exit_code = main(
                [
                    "paper-telegram-history",
                    "--as-of-date",
                    "2026-06-16",
                    "--performance",
                    str(performance),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("performance_live_trading_flag", report["blockers"])
        self.assertFalse(report["telegram"]["sent"])


def performance_payload(*, live: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "OK",
        "paper_metrics": {
            "complete_sessions": 12,
            "fills": 8,
            "pending_closeouts": 0,
            "unmatched_closeouts": 0,
            "pnl": {"source": "broker_statement", "realized_pnl": 12.34},
        },
        "safety": safe(live=live),
    }


def weekly_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "WARN",
        "week": "2026-W25",
        "decisions": {"counts": {"CONTINUE": 3, "REVIEW": 1, "STOP": 0, "ERROR": 0}},
        "ledger": {"event_count": 9, "sessions_closed": 4, "fills": 7, "pending": 0, "unmatched": 0},
        "blockers": {"items": [{"severity": "WARNING", "code": "review_decision", "message": "review"}]},
        "safety": safe(),
    }


def safe(*, live: bool = False) -> dict[str, bool]:
    return {
        "paper_only": not live,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": live,
        "live_trading_allowed": live,
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
