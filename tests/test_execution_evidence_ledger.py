from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading_ai.execution.execution_evidence_ledger import (
    DurableExecutionEvidenceLedger,
    EvidenceLedgerStorageError,
    ExecutionEvidenceConflictError,
    IncidentTransitionError,
    InvalidExecutionEvidenceError,
    canonical_evidence_json,
)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ExecutionEvidenceLedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.path = Path(self._temporary_directory.name) / "execution-evidence.sqlite3"
        self.clock = _Clock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
        self.ledger = DurableExecutionEvidenceLedger(self.path, clock=self.clock)

    def register_policy(
        self,
        *,
        policy_id: str = "gate1-v1",
        start: str = "2026-07-14T13:00:00Z",
        end: str = "2026-07-14T14:00:00Z",
    ):
        return self.ledger.register_policy(
            policy_id,
            {
                "max_median_shortfall_bps": Decimal("10.0"),
                "minimum_fills": 1,
                "scope": "paper-evidence-only",
            },
            effective_from=start,
            effective_until=end,
        )[0]

    @staticmethod
    def fill_payload(
        *,
        transaction_time: str = "2026-07-14T13:10:00Z",
        price: object = "100.25",
        quantity: object = "2.50",
        side: str = "buy",
    ) -> dict[str, object]:
        return {
            "order_id": "broker-order-1",
            "client_order_id": "client-order-1",
            "symbol": "SPY",
            "side": side,
            "quantity": quantity,
            "price": price,
            "transaction_time": transaction_time,
            "submitted_at": "2026-07-14T13:09:59Z",
            "fee_net_amount": "-0.01",
        }

    def advance_after_window(self) -> None:
        self.clock.value = datetime(2026, 7, 14, 14, 1, tzinfo=UTC)

    def add_complete_evidence(self):
        policy = self.register_policy()
        self.advance_after_window()
        fill = self.ledger.record_fill("activity-1", policy.policy_sha256, self.fill_payload())[0]
        manifest = self.ledger.record_manifest(
            "manifest-1",
            policy.policy_sha256,
            window_start=policy.effective_from,
            window_end=policy.effective_until,
            captured_at="2026-07-14T14:00:00Z",
            expected_activity_ids=[fill.activity_id],
            metadata={"source": "complete-paginated-snapshot", "page_count": 1},
        )[0]
        return policy, fill, manifest


class PolicyRegistrationTests(ExecutionEvidenceLedgerTestCase):
    def test_policy_is_pre_registered_by_canonical_envelope_hash(self) -> None:
        policy, created = self.ledger.register_policy(
            "gate1-v1",
            {"threshold": 10, "required": ["fills", "incidents"]},
            effective_from="2026-07-14T08:00:00-05:00",
            effective_until="2026-07-14T09:00:00-05:00",
        )
        self.assertTrue(created)
        self.assertEqual(policy.effective_from, "2026-07-14T13:00:00.000000Z")
        self.assertEqual(len(policy.policy_sha256), 64)
        self.assertLess(policy.registered_at, policy.effective_from)

    def test_exact_policy_replay_is_idempotent_even_after_window_starts(self) -> None:
        first, created = self.ledger.register_policy(
            "gate1-v1",
            {"threshold": 10},
            effective_from="2026-07-14T13:00:00Z",
            effective_until="2026-07-14T14:00:00Z",
        )
        self.assertTrue(created)
        self.advance_after_window()
        replay, replay_created = self.ledger.register_policy(
            "gate1-v1",
            {"threshold": 10},
            effective_from="2026-07-14T13:00:00Z",
            effective_until="2026-07-14T14:00:00Z",
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay, first)
        self.assertEqual(self.ledger.read_policy("gate1-v1"), first)
        self.assertIsNone(self.ledger.read_policy("not-registered"))

    def test_policy_id_reuse_with_different_content_is_a_conflict(self) -> None:
        self.ledger.register_policy(
            "gate1-v1",
            {"threshold": 10},
            effective_from="2026-07-14T13:00:00Z",
            effective_until="2026-07-14T14:00:00Z",
        )
        self.advance_after_window()
        with self.assertRaises(ExecutionEvidenceConflictError):
            self.ledger.register_policy(
                "gate1-v1",
                {"threshold": 11},
                effective_from="2026-07-14T13:00:00Z",
                effective_until="2026-07-14T14:00:00Z",
            )

    def test_policy_window_and_clock_must_be_timezone_aware_and_pre_registered(self) -> None:
        cases = (
            ("2026-07-14T13:00:00", "2026-07-14T14:00:00Z"),
            ("2026-07-14T13:00:00Z", "2026-07-14T13:00:00Z"),
            ("2026-07-14T11:00:00Z", "2026-07-14T13:00:00Z"),
        )
        for index, (start, end) in enumerate(cases):
            with self.subTest(start=start), self.assertRaises(InvalidExecutionEvidenceError):
                self.ledger.register_policy(
                    f"invalid-{index}",
                    {"threshold": 10},
                    effective_from=start,
                    effective_until=end,
                )
        bad_clock = DurableExecutionEvidenceLedger(
            Path(self._temporary_directory.name) / "bad-clock.sqlite3",
            clock=lambda: datetime(2026, 7, 14, 12, 0),
        )
        with self.assertRaises(InvalidExecutionEvidenceError):
            bad_clock.register_policy(
                "p",
                {"threshold": 1},
                effective_from="2026-07-14T13:00:00Z",
                effective_until="2026-07-14T14:00:00Z",
            )

    def test_non_finite_numbers_and_malformed_named_dates_are_rejected(self) -> None:
        for index, value in enumerate((float("nan"), float("inf"), Decimal("NaN"))):
            with self.subTest(value=value), self.assertRaises(InvalidExecutionEvidenceError):
                self.ledger.register_policy(
                    f"bad-number-{index}",
                    {"threshold": value},
                    effective_from="2026-07-14T13:00:00Z",
                    effective_until="2026-07-14T14:00:00Z",
                )
        with self.assertRaises(InvalidExecutionEvidenceError):
            self.ledger.register_policy(
                "bad-date",
                {"approved_at": "yesterday"},
                effective_from="2026-07-14T13:00:00Z",
                effective_until="2026-07-14T14:00:00Z",
            )


class FillEvidenceTests(ExecutionEvidenceLedgerTestCase):
    def test_individual_fill_replay_is_exact_and_decimal_canonical(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        first, created = self.ledger.record_fill(
            "activity-1",
            policy.policy_sha256,
            self.fill_payload(quantity=Decimal("2.500"), price=Decimal("100.2500")),
        )
        replay, replay_created = self.ledger.record_fill(
            "activity-1",
            policy.policy_sha256,
            self.fill_payload(quantity="2.5", price="100.25"),
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        self.assertEqual(first.payload["quantity"], "2.5")
        self.assertEqual(first.payload["price"], "100.25")
        self.assertEqual(first.previous_record_sha256, "0" * 64)

    def test_activity_id_conflict_and_payload_identity_mismatch_block(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        self.ledger.record_fill("activity-1", policy.policy_sha256, self.fill_payload())
        with self.assertRaises(ExecutionEvidenceConflictError):
            self.ledger.record_fill(
                "activity-1",
                policy.policy_sha256,
                self.fill_payload(price="100.26"),
            )
        mismatched = self.fill_payload()
        mismatched["activity_id"] = "different-activity"
        with self.assertRaises(InvalidExecutionEvidenceError):
            self.ledger.record_fill("activity-2", policy.policy_sha256, mismatched)

    def test_fill_requires_known_policy_and_strict_financial_fields(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        with self.assertRaises(InvalidExecutionEvidenceError):
            self.ledger.record_fill("activity-1", "f" * 64, self.fill_payload())
        bad_payloads = []
        for field, value in (
            ("quantity", 0),
            ("quantity", float("nan")),
            ("price", "inf"),
            ("side", "hold"),
            ("transaction_time", "2026-07-14T13:10:00"),
        ):
            payload = self.fill_payload()
            payload[field] = value
            bad_payloads.append(payload)
        for index, payload in enumerate(bad_payloads):
            with self.subTest(index=index), self.assertRaises(InvalidExecutionEvidenceError):
                self.ledger.record_fill(f"bad-{index}", policy.policy_sha256, payload)

    def test_fill_time_must_be_inside_policy_and_not_in_future(self) -> None:
        policy = self.register_policy()
        self.clock.value = datetime(2026, 7, 14, 13, 30, tzinfo=UTC)
        with self.assertRaises(InvalidExecutionEvidenceError):
            self.ledger.record_fill(
                "future",
                policy.policy_sha256,
                self.fill_payload(transaction_time="2026-07-14T13:31:00Z"),
            )
        with self.assertRaises(InvalidExecutionEvidenceError):
            self.ledger.record_fill(
                "outside",
                policy.policy_sha256,
                self.fill_payload(transaction_time="2026-07-14T14:00:00Z"),
            )


class ManifestAndVerificationTests(ExecutionEvidenceLedgerTestCase):
    def test_complete_manifest_makes_read_only_verification_valid(self) -> None:
        policy, fill, manifest = self.add_complete_evidence()
        result = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertTrue(result.valid)
        self.assertTrue(result.integrity_valid)
        self.assertTrue(result.completeness_valid)
        self.assertTrue(result.operationally_clear)
        self.assertEqual(result.fill_count, 1)
        self.assertEqual(result.manifest_count, 1)
        self.assertEqual(manifest.expected_activity_ids, (fill.activity_id,))
        self.assertNotEqual(result.chain_heads["fills"], "0" * 64)
        self.assertNotEqual(result.chain_heads["manifests"], "0" * 64)

    def test_verification_blocks_without_manifest_or_with_inexact_fill_set(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        self.ledger.record_fill("activity-1", policy.policy_sha256, self.fill_payload())
        no_manifest = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertTrue(no_manifest.integrity_valid)
        self.assertFalse(no_manifest.completeness_valid)
        self.ledger.record_manifest(
            "manifest-1",
            policy.policy_sha256,
            window_start=policy.effective_from,
            window_end=policy.effective_until,
            captured_at="2026-07-14T14:00:00Z",
            expected_activity_ids=["activity-2"],
        )
        mismatch = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertFalse(mismatch.valid)
        self.assertTrue(any("missing declared fills" in issue for issue in mismatch.issues))
        self.assertTrue(any("undeclared fills" in issue for issue in mismatch.issues))

    def test_manifests_must_cover_full_policy_window_without_gaps(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        self.ledger.record_manifest(
            "partial",
            policy.policy_sha256,
            window_start="2026-07-14T13:30:00Z",
            window_end=policy.effective_until,
            captured_at="2026-07-14T14:00:00Z",
            expected_activity_ids=[],
        )
        result = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertFalse(result.completeness_valid)
        self.assertTrue(any("coverage has a gap" in issue for issue in result.issues))

    def test_manifest_is_immutable_idempotent_and_temporally_strict(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        kwargs = {
            "window_start": policy.effective_from,
            "window_end": policy.effective_until,
            "captured_at": "2026-07-14T14:00:00Z",
            "expected_activity_ids": [],
            "metadata": {"pages": 0},
        }
        first, created = self.ledger.record_manifest("manifest-1", policy.policy_sha256, **kwargs)
        replay, replay_created = self.ledger.record_manifest(
            "manifest-1", policy.policy_sha256, **kwargs
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        with self.assertRaises(ExecutionEvidenceConflictError):
            self.ledger.record_manifest(
                "manifest-1",
                policy.policy_sha256,
                **{**kwargs, "metadata": {"pages": 1}},
            )
        with self.assertRaises(InvalidExecutionEvidenceError):
            self.ledger.record_manifest(
                "too-early",
                policy.policy_sha256,
                window_start=policy.effective_from,
                window_end=policy.effective_until,
                captured_at="2026-07-14T13:59:59Z",
                expected_activity_ids=[],
            )
        with self.assertRaises(InvalidExecutionEvidenceError):
            self.ledger.record_manifest(
                "duplicates",
                policy.policy_sha256,
                window_start=policy.effective_from,
                window_end=policy.effective_until,
                captured_at="2026-07-14T14:00:00Z",
                expected_activity_ids=["same", "same"],
            )


class IncidentLedgerTests(ExecutionEvidenceLedgerTestCase):
    def test_incident_status_is_derived_from_append_only_open_and_resolution_events(self) -> None:
        policy, _, _ = self.add_complete_evidence()
        opened, created = self.ledger.open_incident(
            "incident-1",
            "event-open-1",
            policy.policy_sha256,
            occurred_at="2026-07-14T13:20:00Z",
            details={"category": "fill-reconciliation", "severity": "P0"},
        )
        replay, replay_created = self.ledger.open_incident(
            "incident-1",
            "event-open-1",
            policy.policy_sha256,
            occurred_at="2026-07-14T13:20:00Z",
            details={"category": "fill-reconciliation", "severity": "P0"},
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(opened, replay)
        blocked = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertFalse(blocked.valid)
        self.assertEqual(blocked.open_incident_ids, ("incident-1",))
        self.assertTrue(blocked.integrity_valid)
        self.assertTrue(blocked.completeness_valid)
        self.assertFalse(blocked.operationally_clear)
        self.clock.value = datetime(2026, 7, 14, 14, 2, tzinfo=UTC)
        posterior_manifest = self.ledger.record_manifest(
            "manifest-after-incident",
            policy.policy_sha256,
            window_start=policy.effective_from,
            window_end=policy.effective_until,
            captured_at="2026-07-14T14:02:00Z",
            expected_activity_ids=["activity-1"],
            metadata={"source": "posterior-reconciliation"},
        )[0]
        resolved, resolution_created = self.ledger.resolve_incident(
            "incident-1",
            "event-resolved-1",
            resolved_at="2026-07-14T14:02:00Z",
            resolution={
                "action": "reconciled from immutable activity snapshot",
                "resolution_manifest_id": posterior_manifest.manifest_id,
            },
        )
        self.assertTrue(resolution_created)
        self.assertEqual(resolved.previous_record_sha256, opened.record_sha256)
        clear = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertTrue(clear.valid)
        self.assertEqual(clear.incident_event_count, 2)

    def test_invalid_incident_transitions_and_conflicting_events_block(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        with self.assertRaises(IncidentTransitionError):
            self.ledger.resolve_incident(
                "not-open",
                "event-1",
                resolved_at="2026-07-14T14:00:00Z",
                resolution={"action": "none"},
            )
        self.ledger.open_incident(
            "incident-1",
            "open-1",
            policy.policy_sha256,
            occurred_at="2026-07-14T13:30:00Z",
            details={"category": "reconciliation"},
        )
        with self.assertRaises(IncidentTransitionError):
            self.ledger.resolve_incident(
                "incident-1",
                "resolve-without-evidence",
                resolved_at="2026-07-14T14:00:00Z",
                resolution={"action": "manual acknowledgement only"},
            )
        with self.assertRaises(ExecutionEvidenceConflictError):
            self.ledger.open_incident(
                "incident-1",
                "open-1",
                policy.policy_sha256,
                occurred_at="2026-07-14T13:30:00Z",
                details={"category": "different"},
            )
        with self.assertRaises(IncidentTransitionError):
            self.ledger.open_incident(
                "incident-1",
                "open-2",
                policy.policy_sha256,
                occurred_at="2026-07-14T13:31:00Z",
                details={"category": "duplicate-open"},
            )
        with self.assertRaises(IncidentTransitionError):
            self.ledger.resolve_incident(
                "incident-1",
                "resolve-before-open",
                resolved_at="2026-07-14T13:29:00Z",
                resolution={"action": "impossible"},
            )


class DurabilityAndTamperTests(ExecutionEvidenceLedgerTestCase):
    def test_wal_foreign_keys_append_only_triggers_and_busy_timeout_fail_closed(self) -> None:
        policy, _, _ = self.add_complete_evidence()
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "fills_append_only"):
                connection.execute("UPDATE fills SET activity_id = 'changed'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "manifests_append_only"):
                connection.execute("DELETE FROM manifests")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "policies_append_only"):
                connection.execute("UPDATE policies SET policy_id = 'changed'")
        finally:
            connection.close()

        locked_ledger = DurableExecutionEvidenceLedger(
            self.path,
            clock=self.clock,
            busy_timeout_ms=20,
        )
        lock = sqlite3.connect(self.path, isolation_level=None)
        try:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaises(EvidenceLedgerStorageError):
                locked_ledger.open_incident(
                    "locked",
                    "locked-open",
                    policy.policy_sha256,
                    occurred_at="2026-07-14T13:40:00Z",
                    details={"category": "lock-test"},
                )
        finally:
            lock.rollback()
            lock.close()

    def test_payload_tampering_is_detected_and_blocks_future_appends(self) -> None:
        policy, _, _ = self.add_complete_evidence()
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("DROP TRIGGER fills_no_update")
            connection.execute(
                "UPDATE fills SET payload_json = replace(payload_json, '100.25', '999.25')"
            )
            connection.execute(
                """
                CREATE TRIGGER fills_no_update
                BEFORE UPDATE ON fills BEGIN
                    SELECT RAISE(ABORT, 'fills_append_only');
                END
                """
            )
        finally:
            connection.close()

        result = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertFalse(result.integrity_valid)
        self.assertTrue(any("fill activity-1" in issue for issue in result.issues))
        with self.assertRaises(EvidenceLedgerStorageError):
            self.ledger.open_incident(
                "after-tamper",
                "event-after-tamper",
                policy.policy_sha256,
                occurred_at="2026-07-14T13:30:00Z",
                details={"category": "must-not-append"},
            )

    def test_partial_chain_deletion_is_detected(self) -> None:
        policy = self.register_policy()
        self.advance_after_window()
        first = self.ledger.record_fill(
            "activity-1", policy.policy_sha256, self.fill_payload()
        )[0]
        second_payload = self.fill_payload(transaction_time="2026-07-14T13:20:00Z")
        second_payload["order_id"] = "broker-order-2"
        second_payload["client_order_id"] = "client-order-2"
        second = self.ledger.record_fill(
            "activity-2", policy.policy_sha256, second_payload
        )[0]
        self.assertEqual(second.previous_record_sha256, first.record_sha256)
        self.ledger.record_manifest(
            "manifest-1",
            policy.policy_sha256,
            window_start=policy.effective_from,
            window_end=policy.effective_until,
            captured_at="2026-07-14T14:00:00Z",
            expected_activity_ids=["activity-1", "activity-2"],
        )
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("DROP TRIGGER fills_no_delete")
            connection.execute("DELETE FROM fills WHERE activity_id = 'activity-1'")
            connection.execute(
                """
                CREATE TRIGGER fills_no_delete
                BEFORE DELETE ON fills BEGIN
                    SELECT RAISE(ABORT, 'fills_append_only');
                END
                """
            )
        finally:
            connection.close()
        result = self.ledger.verify(policy_sha256=policy.policy_sha256)
        self.assertFalse(result.integrity_valid)
        self.assertTrue(any("sequence gap" in issue for issue in result.issues))

    def test_existing_non_ledger_database_and_memory_paths_are_rejected(self) -> None:
        with self.assertRaises(InvalidExecutionEvidenceError):
            DurableExecutionEvidenceLedger(":memory:")
        other_path = Path(self._temporary_directory.name) / "other.sqlite3"
        connection = sqlite3.connect(other_path)
        try:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EvidenceLedgerStorageError):
            DurableExecutionEvidenceLedger(other_path)


class CanonicalEvidenceTests(unittest.TestCase):
    def test_canonical_json_normalizes_timezone_and_rejects_non_finite_values(self) -> None:
        canonical = canonical_evidence_json(
            {
                "captured_at": "2026-07-14T09:00:00-05:00",
                "value": -0.0,
                "decimal": Decimal("1.2300"),
            }
        )
        self.assertEqual(
            canonical,
            '{"captured_at":"2026-07-14T14:00:00.000000Z",'
            '"decimal":{"$decimal":"1.23"},"value":0.0}',
        )
        for value in (float("nan"), float("inf"), Decimal("Infinity")):
            with self.subTest(value=value), self.assertRaises(InvalidExecutionEvidenceError):
                canonical_evidence_json({"value": value})


if __name__ == "__main__":
    unittest.main()
