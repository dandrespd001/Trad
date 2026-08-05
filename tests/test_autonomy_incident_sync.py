import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_ai.cli import build_parser
from trading_ai.execution.autonomy_incident_sync import run_autonomy_incident_sync
from trading_ai.execution.autonomy_level import (
    AutonomyState,
    load_autonomy_state,
    save_autonomy_state,
)
from trading_ai.execution.live_circuit_breaker import (
    LiveCircuitBreakerState,
    save_live_circuit_breaker,
)
from trading_ai.execution.paper_common import write_json_artifact
from trading_ai.execution.paper_risk_state import (
    RiskState,
    save_risk_state,
    trip_kill_switch,
)


def _certify_to_n2(state_dir: Path, market: str = "equities") -> None:
    """Arrange the state under test without exercising promotion validation."""

    save_autonomy_state(
        AutonomyState(market=market, level="N2_REAL_SEMI_AUTO", fail_closed=False),
        state_dir=state_dir,
    )


class BreakerIncidentSyncTests(unittest.TestCase):
    def test_tripped_breaker_degrades_and_opens_incident(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            breaker_path = root / "breaker.json"
            _certify_to_n2(autonomy_dir)
            save_live_circuit_breaker(
                LiveCircuitBreakerState(tripped=True, reason="manual_trip"), breaker_path
            )

            result = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                breaker_state=breaker_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )

            self.assertEqual(result.status, "CRITICAL")
            self.assertEqual(len(result.payload["incidents_filed"]), 1)
            self.assertEqual(result.payload["incidents_filed"][0]["source"], "live_circuit_breaker")
            self.assertEqual(result.payload["autonomy_level_after"], "N1_REAL_CANARY")
            self.assertTrue(result.payload["open_incident_after"])

            state = load_autonomy_state("equities", state_dir=autonomy_dir)
            self.assertEqual(state.level, "N1_REAL_CANARY")
            self.assertTrue(state.open_incident)

    def test_second_identical_run_skips_and_does_not_re_degrade(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            breaker_path = root / "breaker.json"
            _certify_to_n2(autonomy_dir)
            save_live_circuit_breaker(
                LiveCircuitBreakerState(tripped=True, reason="manual_trip"), breaker_path
            )

            first = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                breaker_state=breaker_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(first.status, "CRITICAL")

            second = run_autonomy_incident_sync(
                as_of_date="2026-07-07",
                market="equities",
                breaker_state=breaker_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )

            self.assertEqual(second.status, "OK")
            self.assertEqual(second.payload["incidents_filed"], [])
            self.assertEqual(len(second.payload["skipped_already_synced"]), 1)

            state = load_autonomy_state("equities", state_dir=autonomy_dir)
            self.assertEqual(state.level, "N1_REAL_CANARY")

    def test_missing_breaker_file_is_fail_closed_incident_and_absence_is_no_op(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            missing_breaker_path = root / "does_not_exist.json"
            _certify_to_n2(autonomy_dir)

            result = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                breaker_state=missing_breaker_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(result.status, "CRITICAL")
            self.assertEqual(result.payload["incidents_filed"][0]["reason"], "breaker_missing_fail_closed")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            _certify_to_n2(autonomy_dir)

            result = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(result.status, "OK")
            self.assertNotIn("live_circuit_breaker", result.payload["sources_evaluated"])
            state = load_autonomy_state("equities", state_dir=autonomy_dir)
            self.assertEqual(state.level, "N2_REAL_SEMI_AUTO")


class ReconciliationIncidentSyncTests(unittest.TestCase):
    def test_blocked_reconciliation_incident_then_skip_then_ok_then_invalid(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            report_path = root / "reconciliation.json"
            _certify_to_n2(autonomy_dir)

            write_json_artifact(
                {
                    "status": "BLOCKED",
                    "divergences": [
                        {"code": "quantity_mismatch", "symbol": "AAPL", "message": "expected 10 but broker has 0"}
                    ],
                    "generated_at": "2026-07-06T00:00:00+00:00",
                },
                report_path,
            )

            first = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                reconciliation_report=report_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(first.status, "CRITICAL")
            self.assertEqual(first.payload["incidents_filed"][0]["reason"], "reconciliation_divergence")

            second = run_autonomy_incident_sync(
                as_of_date="2026-07-07",
                market="equities",
                reconciliation_report=report_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(second.status, "OK")
            self.assertEqual(len(second.payload["skipped_already_synced"]), 1)

            # OK report -> no incident, even though it's a "new" report path.
            ok_report_path = root / "reconciliation_ok.json"
            write_json_artifact({"status": "OK", "divergences": []}, ok_report_path)
            third = run_autonomy_incident_sync(
                as_of_date="2026-07-08",
                market="equities",
                reconciliation_report=ok_report_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(third.status, "OK")
            self.assertEqual(third.payload["incidents_filed"], [])

            # Unreadable artifact -> fail-closed incident.
            corrupt_report_path = root / "reconciliation_corrupt.json"
            corrupt_report_path.write_text("{not-json", encoding="utf-8")
            fourth = run_autonomy_incident_sync(
                as_of_date="2026-07-09",
                market="equities",
                reconciliation_report=corrupt_report_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(fourth.status, "CRITICAL")
            self.assertEqual(
                fourth.payload["incidents_filed"][0]["reason"], "reconciliation_artifact_invalid"
            )


class KillSwitchIncidentSyncTests(unittest.TestCase):
    def test_active_kill_switch_incident_and_inactive_is_no_op(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            risk_path = root / "risk_state.json"
            _certify_to_n2(autonomy_dir)

            tripped = trip_kill_switch(RiskState(), reason="account_drawdown_breached")
            save_risk_state(tripped, risk_path)

            result = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                risk_state=risk_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(result.status, "CRITICAL")
            self.assertEqual(result.payload["incidents_filed"][0]["source"], "paper_kill_switch")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            risk_path = root / "risk_state.json"
            _certify_to_n2(autonomy_dir)

            save_risk_state(RiskState(), risk_path)

            result = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                risk_state=risk_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(result.status, "OK")
            self.assertEqual(result.payload["incidents_filed"], [])
            state = load_autonomy_state("equities", state_dir=autonomy_dir)
            self.assertEqual(state.level, "N2_REAL_SEMI_AUTO")


class MultiSourceIncidentSyncTests(unittest.TestCase):
    def test_two_sources_trigger_two_incidents_in_one_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            breaker_path = root / "breaker.json"
            risk_path = root / "risk_state.json"
            _certify_to_n2(autonomy_dir)

            save_live_circuit_breaker(
                LiveCircuitBreakerState(tripped=True, reason="manual_trip"), breaker_path
            )
            tripped = trip_kill_switch(RiskState(), reason="account_drawdown_breached")
            save_risk_state(tripped, risk_path)

            result = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                breaker_state=breaker_path,
                risk_state=risk_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )

            self.assertEqual(result.status, "CRITICAL")
            self.assertEqual(len(result.payload["incidents_filed"]), 2)
            # N2 -> N1 (breaker) -> N0 (kill switch): two grave incidents in
            # the same run degrade two rungs.
            self.assertEqual(result.payload["autonomy_level_after"], "N0_PAPER_AUTO")

            state = load_autonomy_state("equities", state_dir=autonomy_dir)
            self.assertEqual(state.level, "N0_PAPER_AUTO")


class SyncStateFailClosedTests(unittest.TestCase):
    def test_corrupt_sync_state_is_fail_closed_and_reprocesses(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            autonomy_dir = root / "autonomy"
            output_dir = root / "sync"
            breaker_path = root / "breaker.json"
            _certify_to_n2(autonomy_dir)
            save_live_circuit_breaker(
                LiveCircuitBreakerState(tripped=True, reason="manual_trip"), breaker_path
            )

            first = run_autonomy_incident_sync(
                as_of_date="2026-07-06",
                market="equities",
                breaker_state=breaker_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )
            self.assertEqual(first.status, "CRITICAL")

            sync_state_path = output_dir / "equities" / "sync_state.json"
            self.assertTrue(sync_state_path.exists())
            payload = json.loads(sync_state_path.read_text(encoding="utf-8"))
            payload["integrity_sha256"] = "0" * 64
            sync_state_path.write_text(json.dumps(payload), encoding="utf-8")

            second = run_autonomy_incident_sync(
                as_of_date="2026-07-07",
                market="equities",
                breaker_state=breaker_path,
                autonomy_state_dir=autonomy_dir,
                output_dir=output_dir,
            )

            self.assertTrue(second.payload["sync_state_fail_closed"])
            self.assertEqual(second.status, "CRITICAL")
            self.assertEqual(len(second.payload["incidents_filed"]), 1)
            # Re-processed: this is a second grave incident, so the ladder
            # degrades one more rung (N1 -> N0).
            self.assertEqual(second.payload["autonomy_level_after"], "N0_PAPER_AUTO")


class InvalidInputTests(unittest.TestCase):
    def test_invalid_market_raises_value_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                run_autonomy_incident_sync(
                    as_of_date="2026-07-06",
                    market="crypto",
                    autonomy_state_dir=root / "autonomy",
                    output_dir=root / "sync",
                )

    def test_empty_as_of_date_is_blocked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = run_autonomy_incident_sync(
                as_of_date="   ",
                market="equities",
                autonomy_state_dir=root / "autonomy",
                output_dir=root / "sync",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("as_of_date_required", result.payload["blockers"])


class CliParserTests(unittest.TestCase):
    def test_parser_registers_autonomy_incident_sync_without_submit_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "autonomy-incident-sync",
                "--as-of-date",
                "2026-07-06",
                "--market",
                "equities",
                "--breaker-state",
                "breaker.json",
            ]
        )
        self.assertEqual(args.as_of_date, "2026-07-06")
        self.assertEqual(args.market, "equities")
        self.assertEqual(args.breaker_state, "breaker.json")
        self.assertIsNone(args.reconciliation_report)
        self.assertIsNone(args.risk_state)

        subparser = next(
            action.choices["autonomy-incident-sync"]
            for action in parser._subparsers._group_actions  # noqa: SLF001
            if hasattr(action, "choices") and "autonomy-incident-sync" in action.choices
        )
        option_strings = {
            option for action in subparser._actions for option in action.option_strings  # noqa: SLF001
        }
        for forbidden in ("--confirm-real", "--confirm-live", "--submit", "--execute-live"):
            self.assertNotIn(forbidden, option_strings)


if __name__ == "__main__":
    unittest.main()
