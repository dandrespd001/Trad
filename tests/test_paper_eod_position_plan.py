import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperEodPositionPlanTests(unittest.TestCase):
    def test_parser_accepts_eod_position_plan_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-eod-position-plan",
                "--as-of-date",
                "2026-06-16",
                "--position-watch",
                "/tmp/watch.json",  # noqa: S108
                "--current-time",
                "15:50",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.position_watch, "/tmp/watch.json")  # noqa: S108
        self.assertEqual(args.current_time, "15:50")
        self.assertEqual(args.market_close_time, "16:00")
        self.assertEqual(args.flatten_window_minutes, 15)
        self.assertEqual(args.longer_term_symbol, [])

    def test_intraday_position_near_market_close_requires_close_without_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watch = write_json(root / "watch.json", position_watch_payload([position("SPY", quantity=0.25)]))
            output = root / "eod.json"
            markdown = root / "eod.md"
            ledger = root / "ledger.jsonl"

            exit_code = main(
                [
                    "paper-eod-position-plan",
                    "--as-of-date",
                    "2026-06-16",
                    "--position-watch",
                    str(watch),
                    "--current-time",
                    "15:50",
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                    "--ledger-output",
                    str(ledger),
                ]
            )
            report = read_json(output)
            ledger_rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "CRITICAL")
        self.assertTrue(report["market_clock"]["within_flatten_window"])
        self.assertEqual(report["summary"]["close_required_count"], 1)
        self.assertEqual(report["actions"][0]["action"], "CLOSE_BEFORE_MARKET_CLOSE")
        self.assertEqual(report["actions"][0]["symbol"], "SPY")
        self.assertIn("paper-safe-flatten", report["actions"][0]["suggested_next_command"])
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertEqual(ledger_rows[0]["record_type"], "paper_eod_position_plan")

    def test_longer_term_symbol_is_not_flattened_but_stays_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watch = write_json(root / "watch.json", position_watch_payload([position("SPY", quantity=0.25)]))
            output = root / "eod.json"

            exit_code = main(
                [
                    "paper-eod-position-plan",
                    "--as-of-date",
                    "2026-06-16",
                    "--position-watch",
                    str(watch),
                    "--current-time",
                    "15:50",
                    "--longer-term-symbol",
                    "SPY",
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["summary"]["close_required_count"], 0)
        self.assertEqual(report["summary"]["longer_term_hold_count"], 1)
        self.assertEqual(report["actions"][0]["action"], "HOLD_LONGER_TERM")
        self.assertEqual(report["actions"][0]["reason"], "explicit_longer_term_strategy")
        self.assertTrue(report["actions"][0]["overnight_risk_review_required"])


def position(symbol: str, *, quantity: float) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "quantity": quantity,
        "market_value": 50.0,
        "avg_entry_price": 200.0,
        "current_price": 210.0,
    }


def position_watch_payload(positions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-06-16T19:50:00+00:00",
        "status": "OK",
        "session": {"as_of_date": "2026-06-16"},
        "positions": positions,
        "position_plan": {"actions": [], "summary": {"position_count": len(positions)}},
        "safety": {
            "paper_only": True,
            "read_only": True,
            "broker_client_built": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
