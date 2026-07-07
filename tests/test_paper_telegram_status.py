import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main
from trading_ai.execution.autonomy_level import AutonomyState, save_autonomy_state


class PaperTelegramStatusTests(unittest.TestCase):
    def test_parser_accepts_paper_telegram_status_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-telegram-status",
                "--as-of-date",
                "2026-06-16",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.output, "reports/tmp/paper_telegram_status/latest.json")
        self.assertIsNone(args.performance)
        self.assertIsNone(args.position_watch)
        self.assertIsNone(args.forecast_report)
        self.assertIsNone(args.signal_plan)
        self.assertIsNone(args.autonomy_state_dir)
        self.assertEqual(args.autonomy_market, "equities")
        self.assertIsNone(args.n0_certification)

    def test_status_message_summarizes_performance_positions_forecast_and_eod_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            performance = write_json(root / "performance.json", performance_payload())
            position_watch = write_json(root / "watch.json", position_watch_payload())
            forecast = write_json(root / "forecast.json", forecast_payload())
            eod = write_json(root / "eod.json", eod_payload())
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--performance",
                    str(performance),
                    "--position-watch",
                    str(position_watch),
                    "--forecast-report",
                    str(forecast),
                    "--eod-position-plan",
                    str(eod),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        message = report["message"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "WARN")
        self.assertIn("Paper trading 2026-06-16", message)
        self.assertIn("Performance: OK", message)
        self.assertIn("Complete sessions: 12", message)
        self.assertIn("Open positions: SPY 0.25", message)
        self.assertIn("Forecast: OK local_return_forecaster_v1 rows=3", message)
        self.assertIn("EOD: CRITICAL close_required=1", message)
        self.assertFalse(report["telegram"]["sent"])
        self.assertFalse(report["safety"]["credentials_read"])
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_status_message_summarizes_signal_plan_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            signal_plan = write_json(root / "signal_plan.json", signal_plan_payload())
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--signal-plan",
                    str(signal_plan),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        signal_section = report["sections"]["signal"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(signal_section["decision"], "ELIGIBLE_FOR_PAPER")
        self.assertEqual(signal_section["selected_symbol"], "SPY")
        self.assertEqual(signal_section["selected_action"], "buy")
        self.assertEqual(signal_section["probability"], 0.72)
        self.assertIn("Signal: ELIGIBLE_FOR_PAPER SPY buy p=0.72", report["message"])
        self.assertFalse(report["telegram"]["sent"])
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_status_message_surfaces_protective_order_reviews_from_position_watch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            position_watch = write_json(root / "watch.json", position_watch_payload(protective_review_count=2))
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--position-watch",
                    str(position_watch),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["sections"]["positions"].get("protective_review_count"), 2)
        self.assertIn("protective_reviews=2", report["message"])
        self.assertFalse(report["telegram"]["sent"])

    def test_status_blocks_if_any_source_declares_live_or_orders_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            performance = write_json(root / "performance.json", performance_payload(live=True))
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
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

    def test_status_blocks_if_any_source_declares_stale_as_of_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            position_watch = write_json(root / "watch.json", position_watch_payload(as_of_date="2026-06-15"))
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--position-watch",
                    str(position_watch),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("position_watch_stale", report["blockers"])
        self.assertIn("Blockers: position_watch_stale", report["message"])
        self.assertFalse(report["telegram"]["sent"])

    def test_autonomy_section_absent_when_state_dir_not_provided(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertNotIn("autonomy", report["sections"])
        self.assertNotIn("Autonomy:", report["message"])

    def test_autonomy_section_absent_state_file_reads_ok_not_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "autonomy"
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--autonomy-state-dir",
                    str(state_dir),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        section = report["sections"]["autonomy"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(section["status"], "OK")
        self.assertEqual(section["level"], "N0_PAPER_AUTO")
        self.assertTrue(section["fail_closed"])
        self.assertFalse(section["open_incident"])
        self.assertIn("Autonomy: N0_PAPER_AUTO | incident=no", section["line"])
        self.assertIn("[fail-closed]", section["line"])
        self.assertIn(section["line"], report["message"])

    def test_autonomy_section_certified_with_accumulating_n0_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "autonomy"
            save_autonomy_state(
                AutonomyState(
                    market="equities",
                    level="N1_REAL_CANARY",
                    certified_at="2026-06-01T00:00:00+00:00",
                    certified_by="reviewer",
                    evidence_hash="deadbeef",
                    open_incident=False,
                    fail_closed=False,
                    updated_at="2026-06-01T00:00:00+00:00",
                ),
                state_dir=state_dir,
            )
            certification = root / "certification.json"
            write_json(
                certification,
                {
                    "status": "ACCUMULATING",
                    "clean_days": 5,
                    "min_clean_days": 20,
                    "remaining_clean_days": 15,
                },
            )
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--autonomy-state-dir",
                    str(state_dir),
                    "--n0-certification",
                    str(certification),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        section = report["sections"]["autonomy"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(section["status"], "OK")
        self.assertEqual(section["level"], "N1_REAL_CANARY")
        self.assertFalse(section["fail_closed"])
        self.assertNotIn("[fail-closed]", section["line"])
        self.assertEqual(section["certification"]["status"], "ACCUMULATING")
        self.assertIn("N0 evidence 5/20 (ACCUMULATING)", section["line"])

    def test_autonomy_section_open_incident_is_critical_and_bubbles_to_overall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "autonomy"
            save_autonomy_state(
                AutonomyState(
                    market="equities",
                    level="N0_PAPER_AUTO",
                    open_incident=True,
                    fail_closed=False,
                    updated_at="2026-06-01T00:00:00+00:00",
                ),
                state_dir=state_dir,
            )
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--autonomy-state-dir",
                    str(state_dir),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        section = report["sections"]["autonomy"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(section["status"], "CRITICAL")
        self.assertIn("incident=yes", section["line"])
        self.assertEqual(report["status"], "WARN")

    def test_autonomy_section_certification_unreadable_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "autonomy"
            save_autonomy_state(
                AutonomyState(
                    market="equities",
                    level="N0_PAPER_AUTO",
                    open_incident=False,
                    fail_closed=False,
                    updated_at="2026-06-01T00:00:00+00:00",
                ),
                state_dir=state_dir,
            )
            certification = root / "certification.json"
            certification.write_text("not json", encoding="utf-8")
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--autonomy-state-dir",
                    str(state_dir),
                    "--n0-certification",
                    str(certification),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        section = report["sections"]["autonomy"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(section["status"], "OK")
        self.assertEqual(section["certification"]["status"], "MISSING")
        self.assertEqual(section["certification"]["note"], "certification_unreadable")
        self.assertNotIn("certification_unreadable", report["blockers"])
        self.assertNotEqual(report["status"], "BLOCKED")

    def test_autonomy_line_rendered_after_operator_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "autonomy"
            output = root / "telegram_status.json"

            exit_code = main(
                [
                    "paper-telegram-status",
                    "--as-of-date",
                    "2026-06-16",
                    "--autonomy-state-dir",
                    str(state_dir),
                    "--output",
                    str(output),
                ]
            )
            report = read_json(output)

        lines = report["message"].splitlines()
        self.assertEqual(exit_code, 0)
        operator_index = next(i for i, line in enumerate(lines) if line.startswith("Operator:"))
        autonomy_index = next(i for i, line in enumerate(lines) if line.startswith("Autonomy:"))
        self.assertEqual(autonomy_index, operator_index + 1)


def performance_payload(*, live: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "as_of_date": "2026-06-16",
        "status": "OK",
        "paper_metrics": {
            "complete_sessions": 12,
            "fills": 8,
            "pending_closeouts": 0,
            "unmatched_closeouts": 0,
            "pnl": {"source": "proxy", "realized_pnl": 12.34},
            "performance_stable": False,
        },
        "safety": {
            "paper_only": not live,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": live,
            "live_trading_allowed": live,
        },
    }


def position_watch_payload(*, as_of_date: str = "2026-06-16", protective_review_count: int = 0) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "as_of_date": as_of_date,
        "status": "OK",
        "positions": [{"symbol": "SPY", "quantity": 0.25, "current_price": 210.0}],
        "position_plan": {"summary": {"position_count": 1, "close_count": 0, "hold_count": 1}},
        "protective_order_plan": {
            "status": "WARN" if protective_review_count else "OK",
            "summary": {"review_count": protective_review_count},
            "actions": [],
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def forecast_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "as_of_date": "2026-06-16",
        "status": "OK",
        "model_id": "local_return_forecaster_v1",
        "row_count": 3,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def signal_plan_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "as_of_date": "2026-06-16",
        "status": "OK",
        "decision": "ELIGIBLE_FOR_PAPER",
        "eligible_for_paper": True,
        "selected_symbol": "SPY",
        "selected_signal": {
            "symbol": "SPY",
            "action": "buy",
            "probability": 0.72,
            "threshold": 0.5,
            "atr": 4.2,
        },
        "selected_llm_proposal": {
            "symbol": "SPY",
            "action": "buy",
            "confidence": 0.68,
            "rationale": "matches baseline",
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def eod_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "as_of_date": "2026-06-16",
        "status": "CRITICAL",
        "summary": {"open_position_count": 1, "close_required_count": 1, "longer_term_hold_count": 0},
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
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
