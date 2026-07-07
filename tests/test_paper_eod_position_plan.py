import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_swing_declarations import record_swing_declaration

VALID_THESIS = "Breakout above 200d MA with strong volume confirmation and sector tailwind."
VALID_PLAN_HASH = "abc12345def67890"


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


class PaperEodPositionPlanSwingDeclarationTests(unittest.TestCase):
    def test_parser_accepts_swing_registry_flags_with_defaults(self) -> None:
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
        self.assertIsNone(args.swing_registry_dir)
        self.assertEqual(args.swing_lookback_days, 30)

    def test_paper_swing_declare_parser_has_no_submit_or_confirm_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-swing-declare",
                "--as-of-date",
                "2026-06-16",
                "--symbol",
                "SPY",
                "--plan-hash",
                VALID_PLAN_HASH,
                "--thesis",
                VALID_THESIS,
                "--max-overnight-loss-pct",
                "2.5",
                "--expires-on",
                "2026-06-20",
            ]
        )
        self.assertEqual(args.symbol, "SPY")
        self.assertEqual(args.max_overnight_loss_pct, 2.5)
        self.assertFalse(hasattr(args, "confirm"))
        self.assertFalse(hasattr(args, "submit"))
        self.assertFalse(hasattr(args, "confirm_paper"))

    def test_swing_declared_position_is_exempt_with_overnight_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            swing_dir = root / "swing"
            decision = record_swing_declaration(
                as_of_date="2026-06-15",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.5,
                expires_on="2026-06-17",
                registry_dir=swing_dir,
            )
            self.assertEqual(decision.status, "OK")

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
                    "--swing-registry-dir",
                    str(swing_dir),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["summary"]["close_required_count"], 0)
        self.assertEqual(report["summary"]["swing_declared_count"], 1)
        self.assertEqual(report["summary"]["swing_registry_fail_closed_dates"], [])
        action = report["actions"][0]
        self.assertEqual(action["action"], "HOLD_LONGER_TERM")
        self.assertEqual(action["reason"], "declared_swing_strategy")
        self.assertTrue(action["overnight_risk_review_required"])
        self.assertEqual(action["overnight_risk"]["max_overnight_loss_pct"], 2.5)
        self.assertEqual(action["overnight_risk"]["thesis"], VALID_THESIS)
        self.assertEqual(action["overnight_risk"]["expires_on"], "2026-06-17")
        self.assertEqual(action["overnight_risk"]["plan_hash"], VALID_PLAN_HASH)
        self.assertEqual(action["overnight_risk"]["declared_on"], "2026-06-15")

    def test_expired_declaration_is_flattened_not_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            swing_dir = root / "swing"
            decision = record_swing_declaration(
                as_of_date="2026-06-01",
                symbol="SPY",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.5,
                expires_on="2026-06-10",
                registry_dir=swing_dir,
            )
            self.assertEqual(decision.status, "OK")

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
                    "--swing-registry-dir",
                    str(swing_dir),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "CRITICAL")
        self.assertEqual(report["summary"]["swing_declared_count"], 0)
        self.assertEqual(report["summary"]["close_required_count"], 1)
        self.assertEqual(report["actions"][0]["action"], "CLOSE_BEFORE_MARKET_CLOSE")

    def test_corrupt_swing_registry_fails_closed_and_flattens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            swing_dir = root / "swing"
            corrupt_path = swing_dir / "2026-06-16" / "registry.json"
            corrupt_path.parent.mkdir(parents=True, exist_ok=True)
            corrupt_path.write_text("{not valid json", encoding="utf-8")

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
                    "--swing-registry-dir",
                    str(swing_dir),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "CRITICAL")
        self.assertEqual(report["summary"]["close_required_count"], 1)
        self.assertEqual(report["summary"]["swing_declared_count"], 0)
        self.assertIn("2026-06-16", report["summary"]["swing_registry_fail_closed_dates"])

    def test_without_swing_registry_dir_behavior_is_unchanged(self) -> None:
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
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "CRITICAL")
        self.assertEqual(report["summary"]["swing_declared_count"], 0)
        self.assertEqual(report["summary"]["swing_registry_fail_closed_dates"], [])
        self.assertEqual(report["actions"][0]["action"], "CLOSE_BEFORE_MARKET_CLOSE")

    def test_manual_longer_term_and_swing_declared_symbols_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            swing_dir = root / "swing"
            decision = record_swing_declaration(
                as_of_date="2026-06-15",
                symbol="QQQ",
                plan_hash=VALID_PLAN_HASH,
                thesis=VALID_THESIS,
                max_overnight_loss_pct=2.5,
                expires_on="2026-06-17",
                registry_dir=swing_dir,
            )
            self.assertEqual(decision.status, "OK")

            watch = write_json(
                root / "watch.json",
                position_watch_payload(
                    [position("SPY", quantity=0.25), position("QQQ", quantity=0.10)]
                ),
            )
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
                    "--swing-registry-dir",
                    str(swing_dir),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["summary"]["close_required_count"], 0)
        self.assertEqual(report["summary"]["longer_term_hold_count"], 1)
        self.assertEqual(report["summary"]["swing_declared_count"], 1)
        actions_by_symbol = {action["symbol"]: action for action in report["actions"]}
        self.assertEqual(actions_by_symbol["SPY"]["reason"], "explicit_longer_term_strategy")
        self.assertEqual(actions_by_symbol["QQQ"]["reason"], "declared_swing_strategy")
        self.assertIn("overnight_risk", actions_by_symbol["QQQ"])
        self.assertNotIn("overnight_risk", actions_by_symbol["SPY"])


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
