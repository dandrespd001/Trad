import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from trading_ai.cli import build_parser, main
from trading_ai.execution.autonomy_level import (
    DEFAULT_STATE_DIR as AUTONOMY_DEFAULT_STATE_DIR,
    certify_autonomy_promotion,
)
from trading_ai.execution.live_canary import expected_live_canary_confirmation, run_live_canary
from trading_ai.execution.live_connection import AlpacaLivePriceResult
from trading_ai.execution.live_circuit_breaker import LiveCircuitBreakerState, save_live_circuit_breaker
from trading_ai.execution.live_safe_flatten import run_live_safe_flatten
from trading_ai.execution.live_reconciliation import LivePosition
from trading_ai.execution.paper_signal_approval import (
    DEFAULT_REGISTRY_DIR as SIGNAL_APPROVAL_DEFAULT_REGISTRY_DIR,
    compute_plan_hash,
    record_signal_plan_review,
)
from trading_ai.risk.policy import RiskLimits

GOOD_EQUITIES_EVIDENCE = {
    "clean_days": 20,
    "evidence_kind": "paper_certification",
    "artifact_hash": "hash-equities-1",
}


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


class FakeObjectResponseBroker:
    def __init__(self) -> None:
        self.submitted = []

    def submit_order(self, order):
        self.submitted.append(order)
        broker_response = type("BrokerResponse", (), {"id": "object-order-1", "status": "accepted"})()
        return type(
            "Result",
            (),
            {
                "accepted": True,
                "status": "submitted",
                "reasons": (),
                "dry_run": False,
                "broker_response": broker_response,
            },
        )()


class FakeFlattenBroker:
    def read_positions(self):
        return [LivePosition(symbol="SPY", quantity=1.0)]


class RunLiveCanaryTests(unittest.TestCase):
    def test_real_submit_requires_runtime_risk_limits_before_runtime_or_submit(self) -> None:
        calls = []
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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                live_price=100.01,
                market_clock=lambda: True,
                runtime_factory=lambda: calls.append("called"),
                output_dir=root / "out",
                enable_real_submit=True,
                broker=broker,
                allowlist=("SPY",),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("live_risk_limits_required", result.payload["blockers"])
        self.assertEqual(calls, [])
        self.assertEqual(broker.submitted, [])

    def test_real_submit_requires_allowlist_before_runtime_or_submit(self) -> None:
        calls = []
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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                live_price=100.01,
                market_clock=lambda: True,
                runtime_factory=lambda: calls.append("called"),
                output_dir=root / "out",
                enable_real_submit=True,
                broker=broker,
                risk_limits=RiskLimits(live_trading_allowed=True),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("live_allowlist_required", result.payload["blockers"])
        self.assertEqual(calls, [])
        self.assertEqual(broker.submitted, [])

    def test_real_submit_requires_second_exact_confirmation(self) -> None:
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
                confirm_real_submit="wrong",
                reference_price=100.0,
                live_price=100.01,
                output_dir=root / "out",
                enable_real_submit=True,
                broker=broker,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("real_submit_confirmation_mismatch", result.payload["blockers"])
        self.assertEqual(broker.submitted, [])
        self.assertFalse(result.payload["safety"]["orders_submitted"])

    def test_real_submit_runtime_factory_is_not_called_when_offline_prechecks_block(self) -> None:
        calls = []

        def runtime_factory():
            calls.append("called")
            raise AssertionError("runtime must not be built while offline blockers exist")

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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                runtime_factory=runtime_factory,
                output_dir=root / "out",
                enable_real_submit=True,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("s0_s11_evidence_missing", result.payload["blockers"])
        self.assertEqual(calls, [])
        self.assertFalse(result.payload["safety"]["broker_client_built"])

    def test_real_submit_path_builds_runtime_after_prechecks_and_submits_once_with_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)
            broker = FakeBroker()
            calls = []

            def runtime_factory():
                calls.append("called")
                return {
                    "broker": broker,
                    "market_clock": type("Clock", (), {"is_open": True})(),
                    "live_price": 100.01,
                    "credentials_read": True,
                }

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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                max_price_deviation_pct=0.05,
                runtime_factory=runtime_factory,
                output_dir=root / "out",
                enable_real_submit=True,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
            )

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(calls, ["called"])
        self.assertEqual(len(broker.submitted), 1)
        self.assertEqual(broker.submitted[0].reference_price, 100.0)
        self.assertEqual(broker.submitted[0].live_price, 100.01)
        self.assertEqual(broker.submitted[0].max_price_deviation_pct, 0.05)
        self.assertTrue(result.payload["safety"]["broker_client_built"])
        self.assertTrue(result.payload["safety"]["credentials_read"])
        self.assertEqual(result.payload["reference_price"], 100.0)
        self.assertEqual(result.payload["live_price"], 100.01)
        self.assertLess(result.payload["price_deviation_pct"], 0.05)

    def test_real_submit_blocks_closed_runtime_clock_before_submit(self) -> None:
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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                runtime_factory=lambda: {
                    "broker": broker,
                    "market_clock": type("Clock", (), {"is_open": False})(),
                    "live_price": 100.01,
                    "credentials_read": True,
                },
                output_dir=root / "out",
                enable_real_submit=True,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("market_clock_closed", result.payload["blockers"])
        self.assertEqual(broker.submitted, [])

    def test_real_submit_clock_error_writes_blocked_evidence_without_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)
            broker = FakeBroker()

            def failing_clock():
                raise RuntimeError("clock token=SHOULD_NOT_APPEAR")

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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                runtime_factory=lambda: {
                    "broker": broker,
                    "market_clock": failing_clock,
                    "live_price": 100.01,
                    "credentials_read": True,
                },
                output_dir=root / "out",
                enable_real_submit=True,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
            )
            self.assertTrue(result.output_path.exists())

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("market_clock_unavailable", result.payload["blockers"])
        self.assertEqual(result.payload["runtime_error_code"], "market_clock_error")
        self.assertEqual(broker.submitted, [])
        serialized = json.dumps(result.payload, sort_keys=True)
        self.assertNotIn("SHOULD_NOT_APPEAR", serialized)
        self.assertNotIn("token=", serialized)

    def test_real_submit_blocks_live_price_deviation_before_submit(self) -> None:
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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                max_price_deviation_pct=0.05,
                runtime_factory=lambda: {
                    "broker": broker,
                    "market_clock": type("Clock", (), {"is_open": True})(),
                    "live_price": 106.0,
                    "credentials_read": True,
                },
                output_dir=root / "out",
                enable_real_submit=True,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("price_sanity_failed", result.payload["blockers"])
        self.assertEqual(result.payload["price_deviation_pct"], 0.06)
        self.assertEqual(broker.submitted, [])

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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                live_price=100.01,
                market_clock=lambda: True,
                output_dir=root / "out",
                enable_real_submit=True,
                broker=broker,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
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

    def test_submit_path_preserves_object_broker_response_order_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)
            broker = FakeObjectResponseBroker()

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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                live_price=100.01,
                market_clock=lambda: True,
                output_dir=root / "out",
                enable_real_submit=True,
                broker=broker,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
            )

        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(result.payload["post_check"]["order_id"], "object-order-1")
        self.assertEqual(result.payload["post_check"]["fill_status"], "accepted")
        self.assertEqual(result.payload["post_check"]["raw_status"], "submitted")

    def test_real_submit_runtime_error_records_code_without_sensitive_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)

            def runtime_factory():
                raise RuntimeError("secret=SHOULD_NOT_APPEAR missing Alpaca key")

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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                runtime_factory=runtime_factory,
                output_dir=root / "out",
                enable_real_submit=True,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
            )

        serialized = json.dumps(result.payload, sort_keys=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("live_runtime_build_failed", result.payload["blockers"])
        self.assertEqual(result.payload["runtime_error_code"], "live_runtime_unexpected_error")
        self.assertNotIn("SHOULD_NOT_APPEAR", serialized)
        self.assertNotIn("secret=", serialized)

    def test_real_submit_live_price_result_error_records_market_data_code(self) -> None:
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
                confirm_real_submit=expected_real_submit_confirmation(
                    as_of_date="2026-06-16",
                    symbol="SPY",
                    expected_readiness_hash=sha256(readiness),
                    reviewer="ops",
                    reason="approved canary",
                ),
                reference_price=100.0,
                runtime_factory=lambda: {
                    "broker": broker,
                    "market_clock": type("Clock", (), {"is_open": True})(),
                    "live_price_result": lambda symbol: AlpacaLivePriceResult(
                        price=None, error_code="market_data_unavailable"
                    ),
                    "credentials_read": True,
                },
                output_dir=root / "out",
                enable_real_submit=True,
                risk_limits=RiskLimits(live_trading_allowed=True),
                allowlist=("SPY",),
                **real_submit_autonomy_kwargs(root),
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("missing_live_price", result.payload["blockers"])
        self.assertEqual(result.payload["market_data_error_code"], "market_data_unavailable")
        self.assertEqual(broker.submitted, [])

    def test_script_exists_and_requires_exact_confirmation(self) -> None:
        script = Path("scripts/run-live-canary.sh").read_text(encoding="utf-8")

        self.assertIn("CONFIRM_LIVE_CANARY", script)
        self.assertIn("EXPECTED_CONFIRMATION", script)
        self.assertIn("ENABLE_REAL_SUBMIT", script)
        self.assertIn("YES_I_UNDERSTAND_LIVE_ORDER", script)
        self.assertIn("live-canary", script)
        self.assertIn('source "$ROOT/scripts/lib/python-bin.sh"', script)
        self.assertIn('"$PYTHON_BIN" -m trading_ai.cli "${ARGS[@]}"', script)
        self.assertNotIn("python3 -m trading_ai.cli", script)

    def test_dry_run_command_evidence_does_not_claim_real_submit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)

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
            )

        self.assertEqual(result.status, "READY_FOR_SUBMIT")
        evidence = result.payload["command_evidence"]
        self.assertIn("trading-ai live-canary", evidence)
        self.assertFalse(any("--enable-real-submit" in item for item in evidence))

    def test_script_only_passes_enable_real_submit_inside_exact_env_gate(self) -> None:
        script = Path("scripts/run-live-canary.sh").read_text(encoding="utf-8")
        enable_index = script.index("--enable-real-submit")
        gate_index = script.index('ENABLE_REAL_SUBMIT:-}" == "YES_I_UNDERSTAND_LIVE_ORDER"')

        self.assertLess(gate_index, enable_index)
        self.assertIn("RISK_LIVE", script)
        self.assertIn("REFERENCE_PRICE", script)
        self.assertIn("CONFIRM_LIVE_SUBMIT", script)

    def test_cli_enable_real_submit_requires_live_risk_path_before_artifact_reads(self) -> None:
        exit_code, stderr = run_cli(
            "live-canary",
            "--as-of-date",
            "2026-06-16",
            "--symbol",
            "SPY",
            "--notional-usd",
            "1",
            "--readiness",
            "missing-readiness.json",
            "--expected-readiness-hash",
            "0" * 64,
            "--breaker-state",
            "missing-breaker.json",
            "--rehearsal-summary",
            "missing-summary.json",
            "--rollback-evidence",
            "missing-rollback.json",
            "--reviewer",
            "ops",
            "--reason",
            "approved canary",
            "--confirmation",
            "placeholder",
            "--enable-real-submit",
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("--risk-live is required", stderr)

    def test_cli_enable_real_submit_requires_reference_price(self) -> None:
        exit_code, stderr = run_cli(
            *base_real_submit_cli_args(),
            "--risk-live",
            "/tmp/runtime-risk.yml",
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("--reference-price is required", stderr)

    def test_cli_enable_real_submit_requires_second_confirmation(self) -> None:
        exit_code, stderr = run_cli(
            *base_real_submit_cli_args(),
            "--risk-live",
            "/tmp/runtime-risk.yml",
            "--reference-price",
            "100",
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm-real-submit is required", stderr)


def make_signal_plan(
    *,
    as_of_date: str = "2026-06-16",
    generated_at: str = "2026-06-16T10:00:00+00:00",
    decision: str = "ELIGIBLE_FOR_PAPER",
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "decision": decision,
        "selected_symbol": "SPY",
        "eligible_for_paper": decision == "ELIGIBLE_FOR_PAPER",
    }


def real_submit_autonomy_kwargs(root: Path) -> dict[str, object]:
    """Fixture helper for pre-existing real-submit tests: certifies
    ``equities`` to N1 and records an approved matching signal plan under
    ``root``, so tests exercising runtime/price/clock logic unrelated to the
    Sprint A6 autonomy + signal-approval gate can clear that (now mandatory)
    precondition without asserting on it directly.
    """
    autonomy_state_dir = root / "autonomy"
    approval_registry_dir = root / "approval"
    certify_autonomy_promotion(
        market="equities",
        target_level="N1_REAL_CANARY",
        evidence=GOOD_EQUITIES_EVIDENCE,
        reviewer="ops",
        reason="20 clean paper days",
        state_dir=autonomy_state_dir,
    )
    plan = make_signal_plan()
    plan_path = write_json(root / "signal_plan.json", plan)
    plan_hash = compute_plan_hash(plan)
    record_signal_plan_review(
        as_of_date="2026-06-16",
        plan_hash_prefix=plan_hash[:8],
        verdict="approved",
        actor_user_id="reviewer-1",
        actor_chat_id="chat-1",
        source_update_id=1,
        reason="",
        plan=plan,
        registry_dir=approval_registry_dir,
    )
    return {
        "autonomy_state_dir": autonomy_state_dir,
        "autonomy_market": "equities",
        "signal_plan": plan_path,
        "approval_registry_dir": approval_registry_dir,
    }


def base_real_submit_run_kwargs(root: Path) -> dict[str, object]:
    """Shared happy-path kwargs for a real-submit ``run_live_canary`` call,
    matching the fixtures used by ``test_submit_path_uses_fake_broker_once_after_all_prechecks``.
    """
    readiness = write_json(root / "readiness.json", readiness_payload())
    breaker = write_clean_breaker(root / "breaker.json")
    rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
    rollback = write_rollback(root)
    return {
        "as_of_date": "2026-06-16",
        "symbol": "SPY",
        "notional_usd": 1.0,
        "readiness": readiness,
        "expected_readiness_hash": sha256(readiness),
        "breaker_state_path": breaker,
        "rehearsal_summary": rehearsal,
        "rollback_evidence": rollback.output_path,
        "reviewer": "ops",
        "reason": "approved canary",
        "confirmation": expected_live_canary_confirmation(
            as_of_date="2026-06-16", symbol="SPY", reviewer="ops", reason="approved canary"
        ),
        "confirm_real_submit": expected_real_submit_confirmation(
            as_of_date="2026-06-16",
            symbol="SPY",
            expected_readiness_hash=sha256(readiness),
            reviewer="ops",
            reason="approved canary",
        ),
        "reference_price": 100.0,
        "live_price": 100.01,
        "market_clock": lambda: True,
        "output_dir": root / "out",
        "enable_real_submit": True,
        "broker": FakeBroker(),
        "risk_limits": RiskLimits(live_trading_allowed=True),
        "allowlist": ("SPY",),
    }


class LiveCanaryAutonomyGateTests(unittest.TestCase):
    def test_real_submit_without_autonomy_state_blocks_and_reports_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            autonomy_state_dir = root / "autonomy"
            kwargs = base_real_submit_run_kwargs(root)

            result = run_live_canary(
                **kwargs,
                autonomy_state_dir=autonomy_state_dir,
                autonomy_market="equities",
            )

        self.assertIn("autonomy_level_insufficient", result.payload["blockers"])
        self.assertEqual(result.payload["autonomy"]["market"], "equities")
        self.assertEqual(result.payload["autonomy"]["level"], "N0_PAPER_AUTO")
        self.assertTrue(result.payload["autonomy"]["fail_closed"])
        self.assertIn("autonomy_level_insufficient", result.payload["autonomy"]["gate_blockers"])

    def test_real_submit_with_n1_equities_and_approved_plan_has_no_new_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            autonomy_state_dir = root / "autonomy"
            approval_registry_dir = root / "approval"
            certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence=GOOD_EQUITIES_EVIDENCE,
                reviewer="ops",
                reason="20 clean paper days",
                state_dir=autonomy_state_dir,
            )
            plan = make_signal_plan()
            plan_path = write_json(root / "signal_plan.json", plan)
            plan_hash = compute_plan_hash(plan)
            decision = record_signal_plan_review(
                as_of_date="2026-06-16",
                plan_hash_prefix=plan_hash[:8],
                verdict="approved",
                actor_user_id="reviewer-1",
                actor_chat_id="chat-1",
                source_update_id=1,
                reason="",
                plan=plan,
                registry_dir=approval_registry_dir,
            )
            self.assertEqual(decision.status, "OK", decision.payload)

            kwargs = base_real_submit_run_kwargs(root)
            result = run_live_canary(
                **kwargs,
                autonomy_state_dir=autonomy_state_dir,
                autonomy_market="equities",
                signal_plan=plan_path,
                approval_registry_dir=approval_registry_dir,
            )

        new_blockers = {
            "autonomy_level_insufficient",
            "autonomy_open_incident",
            "autonomy_state_fail_closed",
            "signal_plan_artifact_missing",
            "signal_plan_artifact_invalid",
            "plan_generated_at_invalid",
            "signal_approval_missing",
            "signal_plan_vetoed",
            "approval_registry_fail_closed",
        }
        self.assertFalse(new_blockers.intersection(result.payload["blockers"]))
        self.assertEqual(result.payload["autonomy"]["gate_blockers"], [])
        self.assertEqual(result.payload["signal_approval"]["gate_blockers"], [])
        self.assertEqual(result.payload["signal_approval"]["plan_hash"], plan_hash)

    def test_real_submit_with_vetoed_plan_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            autonomy_state_dir = root / "autonomy"
            approval_registry_dir = root / "approval"
            certify_autonomy_promotion(
                market="equities",
                target_level="N1_REAL_CANARY",
                evidence=GOOD_EQUITIES_EVIDENCE,
                reviewer="ops",
                reason="20 clean paper days",
                state_dir=autonomy_state_dir,
            )
            plan = make_signal_plan()
            plan_path = write_json(root / "signal_plan.json", plan)
            plan_hash = compute_plan_hash(plan)
            record_signal_plan_review(
                as_of_date="2026-06-16",
                plan_hash_prefix=plan_hash[:8],
                verdict="vetoed",
                actor_user_id="reviewer-1",
                actor_chat_id="chat-1",
                source_update_id=1,
                reason="news risk",
                plan=plan,
                registry_dir=approval_registry_dir,
            )

            kwargs = base_real_submit_run_kwargs(root)
            result = run_live_canary(
                **kwargs,
                autonomy_state_dir=autonomy_state_dir,
                autonomy_market="equities",
                signal_plan=plan_path,
                approval_registry_dir=approval_registry_dir,
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("signal_plan_vetoed", result.payload["blockers"])
        self.assertIn("signal_plan_vetoed", result.payload["signal_approval"]["gate_blockers"])

    def test_real_submit_without_signal_plan_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kwargs = base_real_submit_run_kwargs(root)

            result = run_live_canary(**kwargs, autonomy_state_dir=root / "autonomy")

        self.assertIn("signal_plan_artifact_missing", result.payload["blockers"])
        self.assertIsNone(result.payload["signal_approval"]["plan_path"])
        self.assertIsNone(result.payload["signal_approval"]["plan_hash"])

    def test_real_submit_with_unreadable_signal_plan_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kwargs = base_real_submit_run_kwargs(root)
            bad_plan_path = root / "bad_plan.json"
            bad_plan_path.write_text("not valid json", encoding="utf-8")

            result = run_live_canary(
                **kwargs,
                autonomy_state_dir=root / "autonomy",
                signal_plan=bad_plan_path,
            )

        self.assertIn("signal_plan_artifact_invalid", result.payload["blockers"])
        self.assertIsNone(result.payload["signal_approval"]["plan_hash"])

    def test_real_submit_with_missing_generated_at_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kwargs = base_real_submit_run_kwargs(root)
            plan = make_signal_plan()
            del plan["generated_at"]
            plan_path = write_json(root / "signal_plan.json", plan)

            result = run_live_canary(
                **kwargs,
                autonomy_state_dir=root / "autonomy",
                signal_plan=plan_path,
            )

        self.assertIn("plan_generated_at_invalid", result.payload["blockers"])

    def test_real_submit_with_garbage_generated_at_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kwargs = base_real_submit_run_kwargs(root)
            plan = make_signal_plan(generated_at="not-a-timestamp")
            plan_path = write_json(root / "signal_plan.json", plan)

            result = run_live_canary(
                **kwargs,
                autonomy_state_dir=root / "autonomy",
                signal_plan=plan_path,
            )

        self.assertIn("plan_generated_at_invalid", result.payload["blockers"])

    def test_dry_run_without_autonomy_state_has_no_new_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = write_json(root / "readiness.json", readiness_payload())
            breaker = write_clean_breaker(root / "breaker.json")
            rehearsal = write_json(root / "summary.json", {"status": "PASSED"})
            rollback = write_rollback(root)

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
                autonomy_state_dir=root / "autonomy",
            )

        self.assertEqual(result.status, "READY_FOR_SUBMIT")
        new_blockers = {
            "autonomy_level_insufficient",
            "autonomy_open_incident",
            "autonomy_state_fail_closed",
            "signal_plan_artifact_missing",
            "signal_plan_artifact_invalid",
            "plan_generated_at_invalid",
            "signal_approval_missing",
            "signal_plan_vetoed",
            "approval_registry_fail_closed",
        }
        self.assertFalse(new_blockers.intersection(result.payload["blockers"]))
        self.assertEqual(result.payload["autonomy"]["level"], "N0_PAPER_AUTO")
        self.assertTrue(result.payload["autonomy"]["fail_closed"])
        self.assertIn("autonomy_level_insufficient", result.payload["autonomy"]["gate_blockers"])
        self.assertIsNone(result.payload["signal_approval"]["plan_path"])
        self.assertIsNone(result.payload["signal_approval"]["plan_hash"])
        self.assertEqual(result.payload["signal_approval"]["gate_blockers"], [])


class LiveCanaryCliAutonomyFlagsTests(unittest.TestCase):
    def test_new_flags_present_with_correct_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "live-canary",
                "--as-of-date",
                "2026-06-16",
                "--symbol",
                "SPY",
                "--notional-usd",
                "1",
                "--readiness",
                "readiness.json",
                "--expected-readiness-hash",
                "0" * 64,
                "--breaker-state",
                "breaker.json",
                "--rehearsal-summary",
                "summary.json",
                "--rollback-evidence",
                "rollback.json",
                "--reviewer",
                "ops",
                "--reason",
                "approved canary",
                "--confirmation",
                "placeholder",
            ]
        )

        self.assertEqual(args.autonomy_state_dir, AUTONOMY_DEFAULT_STATE_DIR)
        self.assertEqual(args.autonomy_market, "equities")
        self.assertIsNone(args.signal_plan)
        self.assertEqual(args.approval_registry_dir, SIGNAL_APPROVAL_DEFAULT_REGISTRY_DIR)

        # Existing flags/defaults remain unchanged.
        self.assertEqual(args.output_dir, "reports/tmp/live_canary")
        self.assertEqual(args.universe, "configs/universe.yml")
        self.assertFalse(args.market_open_confirmed)
        self.assertFalse(args.enable_real_submit)
        self.assertIsNone(args.risk_live)
        self.assertIsNone(args.reference_price)
        self.assertIsNone(args.confirm_real_submit)

    def test_autonomy_market_flag_is_restricted_to_known_markets(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "live-canary",
                    "--as-of-date",
                    "2026-06-16",
                    "--symbol",
                    "SPY",
                    "--notional-usd",
                    "1",
                    "--readiness",
                    "readiness.json",
                    "--expected-readiness-hash",
                    "0" * 64,
                    "--breaker-state",
                    "breaker.json",
                    "--rehearsal-summary",
                    "summary.json",
                    "--rollback-evidence",
                    "rollback.json",
                    "--reviewer",
                    "ops",
                    "--reason",
                    "approved canary",
                    "--confirmation",
                    "placeholder",
                    "--autonomy-market",
                    "not-a-real-market",
                ]
            )


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


def expected_real_submit_confirmation(
    *,
    as_of_date: str,
    symbol: str,
    expected_readiness_hash: str,
    reviewer: str,
    reason: str,
) -> str:
    return (
        f"I confirm REAL LIVE SUBMIT {as_of_date} {symbol.upper()} USD 1 "
        f"readiness_hash={expected_readiness_hash} reviewer={reviewer} reason={reason}"
    )


def run_cli(*args: str) -> tuple[int, str]:
    stderr = StringIO()
    with redirect_stderr(stderr):
        exit_code = main(list(args))
    return exit_code, stderr.getvalue()


def base_real_submit_cli_args() -> tuple[str, ...]:
    return (
        "live-canary",
        "--as-of-date",
        "2026-06-16",
        "--symbol",
        "SPY",
        "--notional-usd",
        "1",
        "--readiness",
        "missing-readiness.json",
        "--expected-readiness-hash",
        "0" * 64,
        "--breaker-state",
        "missing-breaker.json",
        "--rehearsal-summary",
        "missing-summary.json",
        "--rollback-evidence",
        "missing-rollback.json",
        "--reviewer",
        "ops",
        "--reason",
        "approved canary",
        "--confirmation",
        "placeholder",
        "--enable-real-submit",
    )


if __name__ == "__main__":
    unittest.main()
