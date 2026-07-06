import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class CrossAssetSessionPlanTests(unittest.TestCase):
    def test_parser_accepts_cross_asset_session_plan_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "cross-asset-session-plan",
                "--as-of-date",
                "2026-06-19",
                "--positions",
                "reports/tmp/cross_asset_positions/latest.json",
                "--current-time",
                "16:45",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-19")
        self.assertEqual(args.positions, "reports/tmp/cross_asset_positions/latest.json")
        self.assertEqual(args.current_time, "16:45")
        self.assertEqual(args.futures_readiness, "reports/tmp/futures_readiness/latest.json")
        self.assertEqual(args.forex_readiness, "reports/tmp/forex_readiness/latest.json")
        self.assertEqual(args.futures_session_close_time, "17:00")
        self.assertEqual(args.forex_weekend_close_time, "21:00")
        self.assertEqual(args.flatten_window_minutes, 30)
        self.assertEqual(args.longer_term_symbol, [])
        self.assertEqual(args.output, "reports/tmp/cross_asset_session_plan/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/cross_asset_session_plan/latest.md")

    def test_plan_requires_futures_session_and_forex_weekend_close_without_broker_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            positions = write_json(
                root / "positions.json",
                {
                    "as_of_date": "2026-06-19",
                    "positions": [
                        {"symbol": "MES", "quantity": 1, "market_value": 5500.0},
                        {"symbol": "EURUSD", "quantity": 1000, "market_value": 1080.0},
                    ],
                    "safety": safe(),
                },
            )
            futures = write_json(root / "futures.json", futures_readiness())
            forex = write_json(root / "forex.json", forex_readiness())
            output = root / "session_plan.json"
            markdown = root / "session_plan.md"
            ledger = root / "session_plan.jsonl"

            exit_code = main(
                [
                    "cross-asset-session-plan",
                    "--as-of-date",
                    "2026-06-19",
                    "--positions",
                    str(positions),
                    "--futures-readiness",
                    str(futures),
                    "--forex-readiness",
                    str(forex),
                    "--current-time",
                    "16:45",
                    "--futures-session-close-time",
                    "17:00",
                    "--forex-weekend-close-time",
                    "17:00",
                    "--flatten-window-minutes",
                    "30",
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
        self.assertEqual(report["summary"]["close_required_count"], 2)
        self.assertEqual(
            [(action["symbol"], action["asset_class"], action["action"]) for action in report["actions"]],
            [
                ("MES", "futures", "CLOSE_BEFORE_SESSION_CLOSE"),
                ("EURUSD", "forex", "CLOSE_BEFORE_WEEKEND"),
            ],
        )
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["credentials_read"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertEqual(ledger_rows[0]["record_type"], "cross_asset_session_plan")

    def test_plan_blocks_unsafe_snapshot_even_for_longer_term_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            positions = write_json(
                root / "positions.json",
                {
                    "as_of_date": "2026-06-19",
                    "positions": [{"symbol": "EURUSD", "quantity": 1000}],
                    "safety": {**safe(), "live_trading_allowed": True},
                },
            )
            output = root / "session_plan.json"

            exit_code = main(
                [
                    "cross-asset-session-plan",
                    "--as-of-date",
                    "2026-06-19",
                    "--positions",
                    str(positions),
                    "--current-time",
                    "16:45",
                    "--longer-term-symbol",
                    "EURUSD",
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "ERROR")
        self.assertIn("position_snapshot_live_trading_flag", report["blockers"])
        self.assertEqual(report["actions"][0]["action"], "HOLD_LONGER_TERM")
        self.assertFalse(report["safety"]["orders_submitted"])


def futures_readiness() -> dict[str, Any]:
    return {
        "status": "OK",
        "contracts": [{"symbol": "MES", "ready": True, "calendar": {"timezone": "America/New_York"}}],
        "safety": safe_readiness(),
    }


def forex_readiness() -> dict[str, Any]:
    return {
        "status": "OK",
        "pairs": [{"symbol": "EURUSD", "ready": True, "sessions": {"timezone": "UTC", "active": "24x5"}}],
        "safety": safe_readiness(),
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


def safe_readiness() -> dict[str, object]:
    return {
        "read_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_enabled": False,
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
