import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.execution.live_canary import expected_live_canary_confirmation, run_live_canary
from trading_ai.execution.live_circuit_breaker import LiveCircuitBreakerState, save_live_circuit_breaker
from trading_ai.execution.live_safe_flatten import run_live_safe_flatten
from trading_ai.execution.live_reconciliation import LivePosition


class FakeBroker:
    def __init__(self) -> None:
        self.submitted = []

    def submit_order(self, order):
        self.submitted.append(order)
        return type(
            "Result",
            (),
            {
                "accepted": True,
                "status": "accepted",
                "reasons": (),
                "dry_run": False,
                "broker_response": {"id": "live-order-1", "status": "accepted"},
            },
        )()


class FakeFlattenBroker:
    def read_positions(self):
        return [LivePosition(symbol="SPY", quantity=1.0)]


class RunLiveCanaryTests(unittest.TestCase):
    def test_blocks_missing_evidence_without_building_live_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rollback = write_rollback(root)

            result = run_live_canary(
                as_of_date="2026-06-16",
                symbol="SPY",
                notional_usd=1.0,
                readiness=readiness,
                expected_readiness_hash=sha256(readiness),
                breaker_state_path=breaker,
                rehearsal_summary=root / "missing_summary.json",
                rollback_evidence=rollback.output_path,
                reviewer="ops",
                reason="approved canary",
                confirmation=expected_live_canary_confirmation(
                    as_of_date="2026-06-16", symbol="SPY", reviewer="ops", reason="approved canary"
                ),
                output_dir=root / "out",
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("s0_s11_evidence_missing", result.payload["blockers"])
        self.assertFalse(result.payload["safety"]["broker_client_built"])
        self.assertFalse(result.payload["safety"]["orders_submitted"])

    def test_blocks_bad_confirmation_hash_breaker_market_notional_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = root / "breaker.json"
            save_live_circuit_breaker(LiveCircuitBreakerState(tripped=True, reason="manual_trip"), breaker)
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})

            result = run_live_canary(
                as_of_date="2026-06-16",
                symbol="SPY",
                notional_usd=2.0,
                readiness=readiness,
                expected_readiness_hash="0" * 64,
                breaker_state_path=breaker,
                rehearsal_summary=rehearsal,
                rollback_evidence=root / "missing_rollback.json",
                reviewer="ops",
                reason="approved canary",
                confirmation="wrong",
                output_dir=root / "out",
                market_open=False,
            )

        self.assertEqual(result.status, "BLOCKED")
        for blocker in (
            "confirmation_mismatch",
            "readiness_hash_mismatch",
            "breaker_tripped:manual_trip",
            "market_closed",
            "notional_must_be_usd_1",
            "rollback_not_prevalidated",
        ):
            self.assertIn(blocker, result.payload["blockers"])
        self.assertFalse(result.payload["safety"]["orders_submitted"])

    def test_blocks_non_trading_day_even_when_human_confirms_market_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)

            result = run_live_canary(
                as_of_date="2026-01-03",
                symbol="SPY",
                notional_usd=1.0,
                readiness=readiness,
                expected_readiness_hash=sha256(readiness),
                breaker_state_path=breaker,
                rehearsal_summary=rehearsal,
                rollback_evidence=rollback.output_path,
                reviewer="ops",
                reason="approved canary",
                confirmation=expected_live_canary_confirmation(
                    as_of_date="2026-01-03", symbol="SPY", reviewer="ops", reason="approved canary"
                ),
                output_dir=root / "out",
                market_open=True,
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("market_calendar_closed", result.payload["blockers"])
        self.assertFalse(result.payload["safety"]["orders_submitted"])

    def test_blocks_closed_machine_market_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)

            result = run_live_canary(
                as_of_date="2026-01-05",
                symbol="SPY",
                notional_usd=1.0,
                readiness=readiness,
                expected_readiness_hash=sha256(readiness),
                breaker_state_path=breaker,
                rehearsal_summary=rehearsal,
                rollback_evidence=rollback.output_path,
                reviewer="ops",
                reason="approved canary",
                confirmation=expected_live_canary_confirmation(
                    as_of_date="2026-01-05", symbol="SPY", reviewer="ops", reason="approved canary"
                ),
                output_dir=root / "out",
                market_open=True,
                market_clock=lambda: False,
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("market_clock_closed", result.payload["blockers"])
        self.assertFalse(result.payload["safety"]["orders_submitted"])

    def test_submit_path_uses_fake_broker_once_after_all_prechecks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)
            broker = FakeBroker()

            result = run_live_canary(
                as_of_date="2026-06-16",
                symbol="SPY",
                notional_usd=1.0,
                readiness=readiness,
                expected_readiness_hash=sha256(readiness),
                breaker_state_path=breaker,
                rehearsal_summary=rehearsal,
                rollback_evidence=rollback.output_path,
                reviewer="ops",
                reason="approved canary",
                confirmation=expected_live_canary_confirmation(
                    as_of_date="2026-06-16", symbol="SPY", reviewer="ops", reason="approved canary"
                ),
                output_dir=root / "out",
                enable_real_submit=True,
                broker=broker,
            )
            payload = json.loads(result.output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(len(broker.submitted), 1)
        self.assertEqual(broker.submitted[0].notional, 1.0)
        self.assertEqual(payload["post_check"]["order_id"], "live-order-1")
        self.assertIn("python -m trading_ai.cli live-safe-flatten", payload["rollback_command"])
        self.assertIn("--positions-fixture <positions.json>", payload["rollback_command"])
        self.assertIn("--allowlist SPY", payload["rollback_command"])
        self.assertTrue(payload["safety"]["orders_submitted"])

    def test_script_exists_and_requires_exact_confirmation(self) -> None:
        script = Path("scripts/run-live-canary.sh").read_text(encoding="utf-8")

        self.assertIn("CONFIRM_LIVE_CANARY", script)
        self.assertIn("EXPECTED_CONFIRMATION", script)
        self.assertNotIn("--enable-real-submit", script)
        self.assertIn("live-canary", script)


def readiness_payload() -> dict[str, object]:
    return {
        "live_readiness_state": "READY_FOR_LIVE_CANARY",
        "safety": {"orders_submitted": False, "live_trading_authorized": False},
    }


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_clean_breaker(path: Path) -> Path:
    save_live_circuit_breaker(LiveCircuitBreakerState(tripped=False, reason=None), path)
    return path


def write_rollback(root: Path):
    return run_live_safe_flatten(
        as_of_date="2026-06-16",
        broker=FakeFlattenBroker(),
        allowlist=("SPY",),
        reviewer="ops",
        reason="prevalidated rollback",
        output_dir=root / "rollback",
    )


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
