import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from trading_ai.execution.order_journal import (
    BrokerOrderIdCollisionError,
    DurableOrderJournal,
    InvalidOrderIntentError,
    InvalidOrderTransitionError,
    JournalState,
    OrderIntentCollisionError,
    OrderJournalSchemaError,
    OrderJournalStorageError,
    canonical_intent_json,
    intent_fingerprint,
)


def _intent(**updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "symbol": "SPY",
        "side": "buy",
        "quantity": 1.0,
        "notional": None,
        "order_type": "market",
        "limit_price": None,
        "tags": {"purpose": "paper_entry", "attempt": 1},
    }
    body.update(updates)
    return body


class DurableOrderJournalTests(unittest.TestCase):
    def test_record_persists_across_instances_and_exact_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            first = DurableOrderJournal(path)

            created_record, created = first.record_intent("entry-spy-1", _intent())
            second = DurableOrderJournal(path)
            replay_record, replay_created = second.record_intent(
                "entry-spy-1",
                {
                    "tags": {"attempt": 1, "purpose": "paper_entry"},
                    "limit_price": None,
                    "order_type": "market",
                    "notional": None,
                    "quantity": 1.0,
                    "side": "buy",
                    "symbol": "SPY",
                },
            )

            self.assertTrue(created)
            self.assertFalse(replay_created)
            self.assertEqual(replay_record, created_record)
            self.assertEqual(second.get("entry-spy-1"), created_record)
            self.assertEqual(len(second.events("entry-spy-1")), 1)

    def test_client_order_id_collision_with_different_intent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())

            with self.assertRaises(OrderIntentCollisionError):
                journal.record_intent("entry-spy-1", _intent(quantity=2.0))

            self.assertEqual(journal.get("entry-spy-1").intent["quantity"], 1.0)  # type: ignore[union-attr]
            self.assertEqual(len(journal.events("entry-spy-1")), 1)

    def test_canonicalization_is_stable_for_nested_dict_none_tuple_and_negative_zero(self) -> None:
        left = {
            "b": None,
            "a": {"values": (1.25, -0.0), "enabled": True},
        }
        right = {
            "a": {"enabled": True, "values": [1.25, 0.0]},
            "b": None,
        }

        self.assertEqual(canonical_intent_json(left), canonical_intent_json(right))
        self.assertEqual(intent_fingerprint(left), intent_fingerprint(right))
        with self.assertRaises(InvalidOrderIntentError):
            canonical_intent_json({"quantity": float("nan")})
        with self.assertRaises(InvalidOrderIntentError):
            canonical_intent_json({1: "non-string key"})  # type: ignore[dict-item]

    def test_transition_updates_record_and_appends_ordered_metadata_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())

            attempted, attempted_changed = journal.transition(
                "entry-spy-1",
                JournalState.SUBMIT_ATTEMPTED,
                metadata={"attempt": 1, "transport": None},
                expected_state=JournalState.INTENT_RECORDED,
            )
            acknowledged, acknowledged_changed = journal.transition(
                "entry-spy-1",
                JournalState.ACKNOWLEDGED,
                broker_order_id="broker-123",
                metadata={"status": "accepted"},
                expected_state=JournalState.SUBMIT_ATTEMPTED,
            )
            replay, replay_changed = journal.transition(
                "entry-spy-1",
                JournalState.ACKNOWLEDGED,
                broker_order_id="broker-123",
            )

            self.assertTrue(attempted_changed)
            self.assertEqual(attempted.state, JournalState.SUBMIT_ATTEMPTED)
            self.assertTrue(acknowledged_changed)
            self.assertEqual(acknowledged.broker_order_id, "broker-123")
            self.assertFalse(replay_changed)
            self.assertEqual(replay, acknowledged)
            events = journal.events("entry-spy-1")
            self.assertEqual([event.sequence for event in events], sorted(event.sequence for event in events))
            self.assertEqual(
                [event.to_state for event in events],
                [
                    JournalState.INTENT_RECORDED,
                    JournalState.SUBMIT_ATTEMPTED,
                    JournalState.ACKNOWLEDGED,
                ],
            )
            self.assertEqual(events[1].metadata, {"attempt": 1, "transport": None})
            self.assertEqual(events[2].broker_order_id, "broker-123")

    def test_cancel_side_effect_markers_distinguish_attempt_from_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())
            journal.transition("entry-spy-1", JournalState.SUBMIT_ATTEMPTED)
            journal.transition(
                "entry-spy-1",
                JournalState.ACKNOWLEDGED,
                broker_order_id="broker-123",
            )
            journal.transition("entry-spy-1", JournalState.CANCEL_REQUESTED)

            journal.record_marker(
                "entry-spy-1",
                "cancel_dispatch_attempted",
                expected_state=JournalState.CANCEL_REQUESTED,
                metadata={"inside_account_lease": True},
            )
            attempted_events = journal.events("entry-spy-1")
            journal.record_marker(
                "entry-spy-1",
                "cancel_request_accepted",
                expected_state=JournalState.CANCEL_REQUESTED,
                metadata={"broker_status_before_delete": "new"},
            )

            self.assertEqual(attempted_events[-1].event_type, "cancel_dispatch_attempted")
            self.assertEqual(
                journal.events("entry-spy-1")[-1].event_type,
                "cancel_request_accepted",
            )
            self.assertEqual(
                DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
                .get("entry-spy-1")
                .state,  # type: ignore[union-attr]
                JournalState.CANCEL_REQUESTED,
            )

    def test_order_marker_requires_allowlisted_type_and_fresh_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())

            with self.assertRaises(InvalidOrderTransitionError):
                journal.record_marker(
                    "entry-spy-1",
                    "arbitrary_marker",
                    expected_state=JournalState.INTENT_RECORDED,
                )
            with self.assertRaises(InvalidOrderTransitionError):
                journal.record_marker(
                    "entry-spy-1",
                    "cancel_dispatch_attempted",
                    expected_state=JournalState.CANCEL_REQUESTED,
                )

            self.assertEqual(len(journal.events("entry-spy-1")), 1)

    def test_invalid_or_stale_transition_leaves_state_and_events_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())

            with self.assertRaises(InvalidOrderTransitionError):
                journal.transition("entry-spy-1", JournalState.CANCEL_REQUESTED)
            with self.assertRaises(InvalidOrderTransitionError):
                journal.transition(
                    "entry-spy-1",
                    JournalState.SUBMIT_ATTEMPTED,
                    expected_state=JournalState.ACKNOWLEDGED,
                )

            self.assertEqual(journal.get("entry-spy-1").state, JournalState.INTENT_RECORDED)  # type: ignore[union-attr]
            self.assertEqual(len(journal.events("entry-spy-1")), 1)

    def test_broker_order_identity_cannot_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())
            journal.transition("entry-spy-1", JournalState.SUBMIT_ATTEMPTED)
            journal.transition(
                "entry-spy-1",
                JournalState.ACKNOWLEDGED,
                broker_order_id="broker-123",
            )

            with self.assertRaises(BrokerOrderIdCollisionError):
                journal.transition(
                    "entry-spy-1",
                    JournalState.CANCEL_REQUESTED,
                    broker_order_id="broker-OTHER",
                )

            self.assertEqual(journal.get("entry-spy-1").state, JournalState.ACKNOWLEDGED)  # type: ignore[union-attr]

    def test_broker_order_identity_cannot_bind_to_two_intents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())
            journal.record_intent("entry-spy-2", _intent(quantity=2.0))
            journal.transition("entry-spy-1", JournalState.SUBMIT_ATTEMPTED)
            journal.transition("entry-spy-2", JournalState.SUBMIT_ATTEMPTED)
            journal.transition(
                "entry-spy-1",
                JournalState.ACKNOWLEDGED,
                broker_order_id="broker-123",
            )

            with self.assertRaises(BrokerOrderIdCollisionError):
                journal.transition(
                    "entry-spy-2",
                    JournalState.ACKNOWLEDGED,
                    broker_order_id="broker-123",
                )

    def test_submit_response_may_resolve_directly_to_terminal_filled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("entry-spy-1", _intent())
            journal.transition("entry-spy-1", JournalState.SUBMIT_ATTEMPTED)

            filled, changed = journal.transition(
                "entry-spy-1",
                JournalState.FILLED,
                broker_order_id="broker-123",
                metadata={"broker_status": "filled"},
            )

            self.assertTrue(changed)
            self.assertEqual(filled.state, JournalState.FILLED)
            self.assertEqual(filled.broker_order_id, "broker-123")
            self.assertEqual(journal.events("entry-spy-1")[-1].to_state, JournalState.FILLED)

    def test_event_table_is_append_only_at_database_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            journal = DurableOrderJournal(path)
            journal.record_intent("entry-spy-1", _intent())

            connection = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "order_events_append_only"):
                    connection.execute("UPDATE order_events SET event_type = 'tampered'")
                connection.rollback()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "order_events_append_only"):
                    connection.execute("DELETE FROM order_events")
            finally:
                connection.close()

            self.assertEqual(journal.events("entry-spy-1")[0].event_type, "intent_recorded")

    def test_corrupt_database_fails_closed_with_specific_storage_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            path.write_bytes(b"this is not a sqlite database")

            with self.assertRaises(OrderJournalStorageError):
                DurableOrderJournal(path)

    def test_unsupported_schema_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            DurableOrderJournal(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE journal_metadata SET schema_version = 999")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(OrderJournalSchemaError):
                DurableOrderJournal(path)

    def test_removed_append_only_trigger_blocks_existing_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            journal = DurableOrderJournal(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TRIGGER order_events_no_delete")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(OrderJournalSchemaError):
                journal.record_intent("entry-spy-1", _intent())

    def test_record_state_inconsistent_with_event_history_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            journal = DurableOrderJournal(path)
            journal.record_intent("entry-spy-1", _intent())
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE order_intents SET state = 'reconciled' WHERE client_order_id = ?",
                    ("entry-spy-1",),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(OrderJournalSchemaError):
                journal.get("entry-spy-1")

    def test_writer_lock_fails_closed_instead_of_proceeding_without_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            journal = DurableOrderJournal(path, busy_timeout_ms=25)
            lock = sqlite3.connect(path, isolation_level=None)
            try:
                lock.execute("PRAGMA journal_mode = WAL")
                lock.execute("BEGIN IMMEDIATE")
                with self.assertRaises(OrderJournalStorageError):
                    journal.record_intent("entry-spy-1", _intent())
            finally:
                lock.rollback()
                lock.close()

            self.assertIsNone(journal.get("entry-spy-1"))

    def test_batch_reconciliation_is_atomic_and_attested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            for index, state in enumerate(
                (JournalState.FILLED, JournalState.CANCELED),
                start=1,
            ):
                client_id = f"entry-spy-{index}"
                journal.record_intent(client_id, _intent(quantity=float(index)))
                journal.transition(
                    client_id,
                    state,
                    broker_order_id=(f"broker-{index}" if state is JournalState.FILLED else None),
                )

            attestation = journal.reconcile_terminal_records(
                metadata={"operation": "test_batch"}
            )

            self.assertEqual(attestation.record_count, 2)
            self.assertEqual(len(attestation.projection_sha256), 64)
            self.assertEqual(journal.reconciliation_attestation(), attestation)
            self.assertTrue(
                all(
                    record.state is JournalState.RECONCILED
                    for record in journal.records()
                )
            )

    def test_batch_reconciliation_rejects_nonterminal_without_partial_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = DurableOrderJournal(Path(temp_dir) / "orders.sqlite3")
            journal.record_intent("terminal", _intent())
            journal.transition("terminal", JournalState.REJECTED)
            journal.record_intent("ambiguous", _intent(quantity=2.0))

            with self.assertRaises(InvalidOrderTransitionError):
                journal.reconcile_terminal_records()

            self.assertEqual(journal.get("terminal").state, JournalState.REJECTED)
            self.assertEqual(
                journal.get("ambiguous").state,
                JournalState.INTENT_RECORDED,
            )

    def test_concurrent_same_intent_creates_once_and_replays_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.sqlite3"
            journal = DurableOrderJournal(path, busy_timeout_ms=2_000)

            def record() -> bool:
                _record, created = journal.record_intent("entry-spy-1", _intent())
                return created

            with ThreadPoolExecutor(max_workers=8) as executor:
                created_flags = list(executor.map(lambda _index: record(), range(16)))

            self.assertEqual(created_flags.count(True), 1)
            self.assertEqual(created_flags.count(False), 15)
            self.assertEqual(len(journal.events("entry-spy-1")), 1)


if __name__ == "__main__":
    unittest.main()
