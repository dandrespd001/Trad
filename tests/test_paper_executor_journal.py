from __future__ import annotations

import os
import signal
import sqlite3
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from trading_ai.execution.order_journal import intent_fingerprint
from trading_ai.execution.paper_executor_journal import (
    DurableExecutorCommandJournal,
    ExecutorCommandCollisionError,
    ExecutorCommandJournalError,
    ExecutorCommandRecord,
    ExecutorCommandSchemaError,
    ExecutorCommandState,
    ExecutorCommandStorageError,
    ExecutorCommandTransitionError,
    ExecutorRunNotReadyError,
    ExecutorRunState,
    ExecutorSafeFlattenLegKind,
    ExecutorSafeFlattenLegState,
    ExecutorSafeFlattenState,
    canonical_command_json,
    command_fingerprint,
)

ACCOUNT_SCOPE = "a" * 64
POLICY_A = "b" * 64
POLICY_B = "c" * 64


def _request_id(index: int = 1) -> str:
    return uuid.UUID(int=index).hex


def _payload(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "order": {
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1.0,
            "notional": None,
            "client_order_id": "paper-spy-1",
        }
    }
    value.update(updates)
    return value


def _evidence(status: str = "accepted") -> dict[str, object]:
    return {
        "source": "broker_order",
        "client_order_id": "paper-spy-1",
        "broker_order_id": "broker-1",
        "observed_status": status,
        "intent_fingerprint_sha256": "d" * 64,
    }


def _recoverable_submit_payload() -> dict[str, object]:
    return {
        "order": {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "paper-spy-1",
            "quantity": 1.0,
            "notional": None,
            "reference_price": 100.0,
            "order_type": "market",
            "limit_price": None,
            "position_intent": "open",
        }
    }


def _positive_reconciliation_evidence(
    record: ExecutorCommandRecord,
) -> dict[str, object]:
    return {
        "source": "broker_order",
        "client_order_id": "paper-spy-1",
        "broker_order_id": "broker-1",
        "observed_status": "accepted",
        "observed_at": record.updated_at,
        "command_fingerprint_sha256": record.fingerprint_sha256,
        "intent_fingerprint_sha256": intent_fingerprint(
            {
                "symbol": "SPY",
                "side": "buy",
                "quantity": 1.0,
                "notional": None,
                "order_type": "market",
                "time_in_force": "day",
                "limit_price": None,
                "position_intent": "open",
                "reference_price": 100.0,
            }
        ),
    }


class DurableExecutorCommandJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.path = self.root / "executor.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _journal(self, **kwargs: object) -> DurableExecutorCommandJournal:
        return DurableExecutorCommandJournal(
            self.path,
            account_scope_sha256=ACCOUNT_SCOPE,
            **kwargs,
        )

    def _start_run(
        self,
        journal: DurableExecutorCommandJournal,
        *,
        run_index: int,
        fence_epoch: int,
        policy: str = POLICY_A,
        ready: bool = True,
    ) -> str:
        run_id = _request_id(100 + run_index)
        started = journal.start_run(
            run_id=run_id,
            policy_sha256=policy,
        )
        self.assertEqual(started.fence_epoch, fence_epoch)
        journal.transition_run(
            run_id,
            ExecutorRunState.RECOVERING,
            expected_state=ExecutorRunState.STARTING,
        )
        if ready:
            journal.transition_run(
                run_id,
                ExecutorRunState.READY,
                expected_state=ExecutorRunState.RECOVERING,
            )
        return run_id

    @staticmethod
    def _context(run_id: str, *, fence: int = 1, policy: str = POLICY_A) -> dict[str, object]:
        return {
            "run_id": run_id,
            "fence_epoch": fence,
            "policy_sha256": policy,
        }

    def _start_safe_flatten(
        self,
        journal: DurableExecutorCommandJournal,
        run_id: str,
        *,
        operation_index: int = 500,
    ) -> str:
        operation_id = _request_id(operation_index)
        record, created = journal.start_safe_flatten_and_latch(
            operation_id=operation_id,
            initiated_by_uid=1001,
            authz_policy_sha256="e" * 64,
            **self._context(run_id),
        )
        self.assertTrue(created)
        self.assertEqual(record.state, ExecutorSafeFlattenState.LATCHED)
        return operation_id

    def test_record_is_private_durable_and_exact_replay_is_idempotent(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        record, created = journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **self._context(run_id),
        )
        replay, replay_created = self._journal().record(
            request_id=_request_id(),
            operation="submit_order",
            payload={
                "order": {
                    "client_order_id": "paper-spy-1",
                    "notional": None,
                    "quantity": 1.0,
                    "side": "buy",
                    "symbol": "SPY",
                }
            },
            **self._context(run_id),
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, record)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(journal.events(_request_id())), 1)
        self.assertEqual(journal.storage_identity.account_scope_sha256, ACCOUNT_SCOPE)

    def test_run_epoch_is_allocated_monotonically_and_cannot_be_selected(self) -> None:
        first = self._journal().start_run(
            run_id=_request_id(101),
            policy_sha256=POLICY_A,
        )
        second = self._journal().start_run(
            run_id=_request_id(102),
            policy_sha256=POLICY_B,
        )

        self.assertEqual((first.fence_epoch, second.fence_epoch), (1, 2))
        with self.assertRaises(TypeError):
            self._journal().start_run(  # type: ignore[call-arg]
                run_id=_request_id(103),
                fence_epoch=999,
                policy_sha256=POLICY_A,
            )

    def test_concurrent_run_allocators_produce_unique_contiguous_epochs(self) -> None:
        journal = self._journal(busy_timeout_ms=5_000)

        def start(index: int) -> tuple[str, int]:
            run = journal.start_run(
                run_id=_request_id(200 + index),
                policy_sha256=POLICY_A,
            )
            return run.run_id, run.fence_epoch

        with ThreadPoolExecutor(max_workers=8) as executor:
            runs = list(executor.map(start, range(16)))

        self.assertEqual(sorted(epoch for _run_id, epoch in runs), list(range(1, 17)))
        active = [
            journal.get_run(run_id)
            for run_id, _epoch in runs
            if journal.get_run(run_id).state
            in {
                ExecutorRunState.STARTING,
                ExecutorRunState.RECOVERING,
                ExecutorRunState.READY,
                ExecutorRunState.BLOCKED,
                ExecutorRunState.DRAINING,
            }
        ]
        self.assertEqual(len(active), 1)

    def test_failed_run_allocation_does_not_consume_epoch_and_overflow_blocks(self) -> None:
        journal = self._journal(busy_timeout_ms=25)
        first = journal.start_run(run_id=_request_id(301), policy_sha256=POLICY_A)
        lock = sqlite3.connect(self.path, isolation_level=None)
        try:
            lock.execute("PRAGMA journal_mode = WAL")
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ExecutorCommandStorageError):
                journal.start_run(run_id=_request_id(302), policy_sha256=POLICY_A)
        finally:
            lock.rollback()
            lock.close()

        second = journal.start_run(run_id=_request_id(303), policy_sha256=POLICY_A)
        self.assertEqual((first.fence_epoch, second.fence_epoch), (1, 2))
        with (
            patch(
                "trading_ai.execution.paper_executor_journal.MAX_SQLITE_INTEGER",
                second.fence_epoch,
            ),
            self.assertRaisesRegex(ExecutorCommandTransitionError, "exhausted"),
        ):
            journal.start_run(run_id=_request_id(304), policy_sha256=POLICY_A)

    def test_same_id_with_different_operation_or_payload_is_rejected(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **self._context(run_id),
        )

        with self.assertRaises(ExecutorCommandCollisionError):
            journal.record(
                request_id=_request_id(),
                operation="cancel_order",
                payload=_payload(),
                **self._context(run_id),
            )
        with self.assertRaises(ExecutorCommandCollisionError):
            journal.record(
                request_id=_request_id(),
                operation="submit_order",
                payload=_payload(order={"symbol": "QQQ"}),
                **self._context(run_id),
            )

    def test_canonicalization_normalizes_order_and_rejects_nonfinite(self) -> None:
        left = {"nested": {"values": (1.0, -0.0)}, "none": None}
        right = {"none": None, "nested": {"values": [1.0, 0.0]}}
        self.assertEqual(
            canonical_command_json("submit_order", left),
            canonical_command_json("submit_order", right),
        )
        self.assertEqual(
            command_fingerprint("submit_order", left),
            command_fingerprint("submit_order", right),
        )
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ExecutorCommandJournalError):
                canonical_command_json("submit_order", {"quantity": invalid})

    def test_claim_complete_terminal_replay_and_outcome_hash_are_durable(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        record, _created = journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **context,
        )
        claimed = journal.claim(record.request_id, **context)
        completed = journal.complete(
            record.request_id,
            {"accepted": True, "status": "submitted"},
            **context,
        )

        self.assertEqual(claimed.state, ExecutorCommandState.DISPATCHING)
        self.assertEqual(completed.state, ExecutorCommandState.COMPLETED)
        self.assertEqual(completed.result, {"accepted": True, "status": "submitted"})
        self.assertIsNotNone(completed.outcome_sha256)
        with self.assertRaises(ExecutorCommandTransitionError):
            journal.claim(record.request_id, **context)
        self.assertEqual(self._journal().get(record.request_id), completed)

    def test_structured_predispatch_requeue_is_audited_and_same_run_reclaimable(
        self,
    ) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        record, _created = journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **context,
        )
        journal.claim(record.request_id, **context)

        requeued = journal.requeue_proven_not_dispatched(
            record.request_id,
            operation="submit_order",
            reason="submit_deferred",
            **context,
        )
        reclaimed = journal.claim(record.request_id, **context)
        with self.assertRaisesRegex(
            ExecutorCommandTransitionError,
            "exhausted its same-run pre-dispatch retry",
        ):
            journal.requeue_proven_not_dispatched(
                record.request_id,
                operation="submit_order",
                reason="submit_deferred",
                **context,
            )
        completed = journal.complete(
            record.request_id,
            {"accepted": True, "status": "submitted"},
            **context,
        )

        self.assertEqual(requeued.state, ExecutorCommandState.RECORDED)
        self.assertEqual(reclaimed.state, ExecutorCommandState.DISPATCHING)
        self.assertEqual(completed.state, ExecutorCommandState.COMPLETED)
        events = journal.events(record.request_id)
        self.assertEqual(
            tuple(event.event_type for event in events),
            (
                "recorded",
                "dispatch_claimed",
                "dispatch_proven_not_started",
                "dispatch_claimed",
                "dispatch_completed",
            ),
        )
        proof_event = events[2]
        self.assertEqual(
            proof_event.metadata,
            {
                "classification": "structured_not_dispatched",
                "operation": "submit_order",
                "reason": "submit_deferred",
                "retry_scope": "same_run_once",
            },
        )
        journal.full_audit()

    def test_predispatch_requeue_cannot_cross_command_operation(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        record, _created = journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **context,
        )
        journal.claim(record.request_id, **context)

        with self.assertRaisesRegex(
            ExecutorCommandTransitionError,
            "does not match the audited transition",
        ):
            journal.requeue_proven_not_dispatched(
                record.request_id,
                operation="cancel_order",
                reason="cancel_deferred",
                **context,
            )

        current = journal.get(record.request_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.state, ExecutorCommandState.DISPATCHING)
        journal.full_audit()

    def test_sigkill_during_claim_rolls_back_atomically(self) -> None:
        if not hasattr(os, "fork") or not hasattr(signal, "SIGKILL"):
            self.skipTest("requires POSIX fork and SIGKILL")

        self._journal()
        run_id = _request_id(101)
        context = self._context(run_id)
        request_id = _request_id()

        child_pid = os.fork()
        if child_pid == 0:
            try:
                journal = self._journal()
                journal.start_run(
                    run_id=run_id,
                    policy_sha256=POLICY_A,
                )
                journal.transition_run(
                    run_id,
                    ExecutorRunState.RECOVERING,
                    expected_state=ExecutorRunState.STARTING,
                )
                journal.transition_run(
                    run_id,
                    ExecutorRunState.READY,
                    expected_state=ExecutorRunState.RECOVERING,
                )
                journal.record(
                    request_id=request_id,
                    operation="submit_order",
                    payload=_payload(),
                    **context,
                )
                original_append = journal._append_command_event

                def append_then_kill(*args: object, **kwargs: object) -> None:
                    original_append(*args, **kwargs)
                    if kwargs.get("event_type") == "dispatch_claimed":
                        os.kill(os.getpid(), signal.SIGKILL)

                with patch.object(
                    journal,
                    "_append_command_event",
                    side_effect=append_then_kill,
                ):
                    journal.claim(request_id, **context)
            except BaseException:
                os._exit(71)
            os._exit(70)

        child_status: int | None = None
        deadline = time.monotonic() + 5.0
        try:
            while time.monotonic() < deadline:
                waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
                if waited_pid == child_pid:
                    child_status = status
                    break
                time.sleep(0.01)
        finally:
            if child_status is None:
                with suppress(ProcessLookupError):
                    os.kill(child_pid, signal.SIGKILL)
                _waited_pid, child_status = os.waitpid(child_pid, 0)

        self.assertTrue(os.WIFSIGNALED(child_status))
        self.assertEqual(os.WTERMSIG(child_status), signal.SIGKILL)

        reopened = self._journal()
        rolled_back = reopened.get(request_id)
        self.assertIsNotNone(rolled_back)
        assert rolled_back is not None
        self.assertEqual(rolled_back.state, ExecutorCommandState.RECORDED)
        self.assertEqual(
            tuple(event.event_type for event in reopened.events(request_id)),
            ("recorded",),
        )
        reopened.full_audit()

        successor_run = self._start_run(
            reopened,
            run_index=2,
            fence_epoch=2,
            ready=False,
        )
        resolved = reopened.resolve_recorded_not_dispatched(
            request_id,
            **self._context(successor_run, fence=2),
        )
        self.assertEqual(resolved.state, ExecutorCommandState.REJECTED)
        self.assertEqual(resolved.error_code, "command_not_dispatched")
        reopened.full_audit()

    def test_terminal_outcome_and_trigger_definition_cannot_be_forged(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **context,
        )
        journal.claim(_request_id(), **context)
        journal.complete(_request_id(), {"accepted": True}, **context)

        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "terminal_immutable"):
                connection.execute(
                    "UPDATE executor_commands SET outcome_json = ? WHERE request_id = ?",
                    ('{"error":null,"ok":true,"payload":{"accepted":false}}', _request_id()),
                )
            connection.rollback()
            connection.execute("DROP TRIGGER executor_commands_identity_immutable")
            connection.execute(
                """
                CREATE TRIGGER executor_commands_identity_immutable
                BEFORE UPDATE ON executor_commands BEGIN SELECT 1; END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ExecutorCommandSchemaError):
            self._journal()

    def test_old_recorded_command_blocks_new_run_until_explicit_no_dispatch_resolution(self) -> None:
        journal = self._journal()
        run_one = self._start_run(journal, run_index=1, fence_epoch=1)
        journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **self._context(run_one),
        )
        journal.transition_run(
            run_one,
            ExecutorRunState.DRAINING,
            expected_state=ExecutorRunState.READY,
        )
        journal.transition_run(
            run_one,
            ExecutorRunState.STOPPED,
            expected_state=ExecutorRunState.DRAINING,
        )
        run_two = self._start_run(
            journal,
            run_index=2,
            fence_epoch=2,
            policy=POLICY_B,
            ready=False,
        )

        with self.assertRaises(ExecutorRunNotReadyError):
            journal.transition_run(
                run_two,
                ExecutorRunState.READY,
                expected_state=ExecutorRunState.RECOVERING,
            )
        with self.assertRaises(ExecutorRunNotReadyError):
            journal.record(
                request_id=_request_id(),
                operation="submit_order",
                payload=_payload(),
                **self._context(run_two, fence=2, policy=POLICY_B),
            )

        resolved = journal.resolve_recorded_not_dispatched(
            _request_id(),
            **self._context(run_two, fence=2, policy=POLICY_B),
        )
        journal.transition_run(
            run_two,
            ExecutorRunState.READY,
            expected_state=ExecutorRunState.RECOVERING,
        )
        self.assertEqual(resolved.state, ExecutorCommandState.REJECTED)
        self.assertEqual(resolved.error_code, "command_not_dispatched")

    def test_predecessor_dispatch_is_marked_unknown_and_requires_broker_evidence(self) -> None:
        journal = self._journal()
        run_one = self._start_run(journal, run_index=1, fence_epoch=1)
        context_one = self._context(run_one)
        journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_recoverable_submit_payload(),
            **context_one,
        )
        journal.claim(_request_id(), **context_one)

        run_two = self._start_run(
            journal,
            run_index=2,
            fence_epoch=2,
            ready=False,
        )
        unknown = journal.get(_request_id())
        self.assertIsNotNone(unknown)
        assert unknown is not None
        self.assertEqual(unknown.state, ExecutorCommandState.OUTCOME_UNKNOWN)
        self.assertEqual(journal.get_run(run_one).state, ExecutorRunState.CRASHED)  # type: ignore[union-attr]

        with self.assertRaises(ExecutorCommandJournalError):
            journal.resolve_unknown_completed(
                _request_id(),
                evidence={"source": "broker_order"},
                **self._context(run_two, fence=2),
            )
        unknown = journal.get(_request_id())
        self.assertIsNotNone(unknown)
        assert unknown is not None
        resolved = journal.resolve_unknown_completed(
            _request_id(),
            evidence=_positive_reconciliation_evidence(unknown),
            **self._context(run_two, fence=2),
        )
        journal.transition_run(
            run_two,
            ExecutorRunState.READY,
            expected_state=ExecutorRunState.RECOVERING,
        )
        self.assertTrue(resolved.result["accepted"])  # type: ignore[index]

    def test_unknown_in_same_run_cannot_be_cleared_by_absence_evidence(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        journal.record(
            request_id=_request_id(),
            operation="cancel_order",
            payload={"client_order_id": "paper-spy-1"},
            **context,
        )
        journal.claim(_request_id(), **context)
        journal.mark_outcome_unknown(
            _request_id(),
            reason="broker_response_ambiguous",
            **context,
        )
        with self.assertRaises(ExecutorCommandTransitionError):
            journal.resolve_unknown_rejected(
                _request_id(),
                evidence=_evidence("not_found"),
                **context,
            )
        journal.transition_run(
            run_id,
            ExecutorRunState.BLOCKED,
            expected_state=ExecutorRunState.READY,
        )
        with self.assertRaises(ExecutorCommandTransitionError):
            journal.resolve_unknown_rejected(
                _request_id(),
                evidence=_evidence("not_found"),
                **context,
            )
        self.assertEqual(
            tuple(record.request_id for record in journal.recovery_required()),
            (_request_id(),),
        )

    def test_definitive_rejection_uses_fixed_safe_message(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **context,
        )
        journal.claim(_request_id(), **context)
        rejected = journal.reject(_request_id(), code="policy_rejected", **context)

        self.assertEqual(rejected.error_code, "policy_rejected")
        self.assertEqual(rejected.error_message, "executor policy rejected the command")
        with self.assertRaises(ExecutorCommandJournalError):
            journal.reject(_request_id(2), code="arbitrary_secret_error", **context)

    def test_kill_switch_latch_is_durable_and_cannot_unlatch_online(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        journal.record(
            request_id=_request_id(),
            operation="latch_kill_switch",
            payload={"reason_code": "daily_loss_limit"},
            **context,
        )
        journal.claim(_request_id(), **context)
        journal.latch_kill_switch_and_complete(
            request_id=_request_id(),
            **context,
        )
        latched = journal.read_control_state()

        self.assertTrue(latched.kill_switch_active)
        self.assertEqual(self._journal().read_control_state(), latched)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot_unlatch"):
                connection.execute(
                    "UPDATE executor_control_state SET kill_switch_active = 0"
                )
        finally:
            connection.close()

    def test_concurrent_record_creates_once_and_claim_has_one_winner(self) -> None:
        journal = self._journal(busy_timeout_ms=2_000)
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)

        def record() -> bool:
            _record, created = journal.record(
                request_id=_request_id(),
                operation="submit_order",
                payload=_payload(),
                **context,
            )
            return created

        with ThreadPoolExecutor(max_workers=8) as executor:
            created_flags = list(executor.map(lambda _index: record(), range(16)))

        def claim() -> bool:
            try:
                journal.claim(_request_id(), **context)
            except ExecutorCommandTransitionError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            claim_flags = list(executor.map(lambda _index: claim(), range(16)))

        self.assertEqual(created_flags.count(True), 1)
        self.assertEqual(claim_flags.count(True), 1)

    def test_append_only_tamper_corruption_permissions_and_replacement_fail_closed(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **self._context(run_id),
        )
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                connection.execute(
                    "UPDATE executor_command_events SET metadata_json = '{}'"
                )
        finally:
            connection.close()

        hardlink = self.root / "executor-link.sqlite3"
        os.link(self.path, hardlink)
        try:
            with self.assertRaisesRegex(ExecutorCommandStorageError, "ownership"):
                journal.get(_request_id())
        finally:
            hardlink.unlink()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ExecutorCommandStorageError, "permissions"):
            journal.get(_request_id())

    def test_corrupt_database_unsafe_directory_scope_and_nil_id_fail_closed(self) -> None:
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        corrupt.chmod(0o600)
        with self.assertRaises(ExecutorCommandStorageError):
            DurableExecutorCommandJournal(corrupt, account_scope_sha256=ACCOUNT_SCOPE)

        self.root.chmod(0o755)
        with self.assertRaisesRegex(ExecutorCommandStorageError, "permissions"):
            DurableExecutorCommandJournal(
                self.root / "unsafe.sqlite3",
                account_scope_sha256=ACCOUNT_SCOPE,
            )
        self.root.chmod(0o700)
        journal = self._journal()
        with self.assertRaises(ExecutorCommandSchemaError):
            DurableExecutorCommandJournal(self.path, account_scope_sha256="e" * 64)
        with self.assertRaises(ExecutorCommandJournalError):
            journal.start_run(
                run_id="0" * 32,
                policy_sha256=POLICY_A,
            )

    def test_reconciliation_evidence_must_match_command_and_terminal_outcome(self) -> None:
        mismatches = (
            ("client_order_id", "another-order"),
            ("command_fingerprint_sha256", "e" * 64),
            ("observed_status", "not_found"),
        )

        for index, (field, wrong_value) in enumerate(mismatches, start=1):
            with self.subTest(field=field):
                path = self.root / f"evidence-{index}.sqlite3"
                journal = DurableExecutorCommandJournal(
                    path,
                    account_scope_sha256=ACCOUNT_SCOPE,
                )
                run_one = self._start_run(
                    journal,
                    run_index=index * 10,
                    fence_epoch=1,
                )
                context_one = self._context(run_one)
                journal.record(
                    request_id=_request_id(index),
                    operation="submit_order",
                    payload=_recoverable_submit_payload(),
                    **context_one,
                )
                journal.claim(_request_id(index), **context_one)
                run_two = self._start_run(
                    journal,
                    run_index=index * 10 + 1,
                    fence_epoch=2,
                    ready=False,
                )
                unknown = journal.get(_request_id(index))
                self.assertIsNotNone(unknown)
                assert unknown is not None
                evidence = _positive_reconciliation_evidence(unknown)
                evidence[field] = wrong_value

                with self.assertRaises(ExecutorCommandJournalError):
                    journal.resolve_unknown_completed(
                        _request_id(index),
                        evidence=evidence,
                        **self._context(run_two, fence=2),
                    )

    def test_kill_switch_reason_must_match_durable_command_payload(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        journal.record(
            request_id=_request_id(),
            operation="latch_kill_switch",
            payload={"reason_code": "daily_loss_limit"},
            **context,
        )
        journal.claim(_request_id(), **context)

        with self.assertRaises(TypeError):
            journal.latch_kill_switch_and_complete(
                request_id=_request_id(),
                reason_code="manual_override",
                **context,
            )
        self.assertFalse(journal.read_control_state().kill_switch_active)
        journal.latch_kill_switch_and_complete(
            request_id=_request_id(),
            **context,
        )
        self.assertEqual(
            journal.read_control_state().reason_code,
            "daily_loss_limit",
        )

    def test_tampered_control_history_fails_on_control_read(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        journal.record(
            request_id=_request_id(),
            operation="latch_kill_switch",
            payload={"reason_code": "daily_loss_limit"},
            **context,
        )
        journal.claim(_request_id(), **context)
        journal.latch_kill_switch_and_complete(
            request_id=_request_id(),
            **context,
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE executor_control_state SET reason_code = 'forged_reason' "
                "WHERE singleton = 1"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ExecutorCommandSchemaError):
            journal.read_control_state()

    def test_inactive_control_state_rejects_non_null_last_request_id(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        journal.record(
            request_id=_request_id(),
            operation="submit_order",
            payload=_payload(),
            **self._context(run_id),
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE executor_control_state SET last_request_id = ? "
                "WHERE singleton = 1",
                (_request_id(),),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ExecutorCommandSchemaError):
            journal.full_audit()

    def test_corrupt_schema_version_uses_journal_error_boundary(self) -> None:
        self._journal()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE executor_command_metadata SET schema_version = 'invalid'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ExecutorCommandJournalError):
            self._journal()

    def test_submit_claim_is_rejected_while_kill_switch_is_active(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        context = self._context(run_id)
        journal.record(
            request_id=_request_id(2),
            operation="submit_order",
            payload=_recoverable_submit_payload(),
            **context,
        )
        journal.record(
            request_id=_request_id(),
            operation="latch_kill_switch",
            payload={"reason_code": "daily_loss_limit"},
            **context,
        )
        journal.claim(_request_id(), **context)
        journal.latch_kill_switch_and_complete(
            request_id=_request_id(),
            **context,
        )

        with self.assertRaises(ExecutorRunNotReadyError):
            journal.claim(_request_id(2), **context)

    def test_active_run_with_ended_at_fails_lifecycle_audit(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        run = journal.get_run(run_id)
        self.assertIsNotNone(run)
        assert run is not None
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE executor_runs SET ended_at = ? WHERE run_id = ?",
                (run.updated_at, run_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ExecutorCommandSchemaError):
            journal.full_audit()

    def test_writer_lock_fails_closed_without_partial_command(self) -> None:
        journal = self._journal(busy_timeout_ms=25)
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        lock = sqlite3.connect(self.path, isolation_level=None)
        try:
            lock.execute("PRAGMA journal_mode = WAL")
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ExecutorCommandStorageError):
                journal.record(
                    request_id=_request_id(),
                    operation="submit_order",
                    payload=_payload(),
                    **self._context(run_id),
                )
        finally:
            lock.rollback()
            lock.close()
        self.assertIsNone(journal.get(_request_id()))

    def test_safe_flatten_start_is_atomic_replayable_and_exclusive(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        operation_id = self._start_safe_flatten(journal, run_id)

        replay, created = journal.start_safe_flatten_and_latch(
            operation_id=operation_id,
            initiated_by_uid=1001,
            authz_policy_sha256="e" * 64,
            **self._context(run_id),
        )

        self.assertFalse(created)
        self.assertEqual(replay.operation_id, operation_id)
        self.assertTrue(journal.read_control_state().kill_switch_active)
        with self.assertRaises(ExecutorRunNotReadyError):
            journal.record(
                request_id=_request_id(501),
                operation="cancel_order",
                payload={"order_id": "broker-1", "client_order_id": None},
                **self._context(run_id),
            )
        journal.full_audit()

    def test_safe_flatten_phase_cannot_advance_with_active_leg(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        operation_id = self._start_safe_flatten(journal, run_id)
        operation = journal.transition_safe_flatten(
            operation_id,
            ExecutorSafeFlattenState.CANCELING,
            expected_state=ExecutorSafeFlattenState.LATCHED,
            event_type="open_orders_observed",
            metadata={"open_order_count": 1},
            **self._context(run_id),
        )
        leg_id = _request_id(501)
        journal.begin_safe_flatten_leg(
            operation_id=operation_id,
            leg_id=leg_id,
            ordinal=0,
            kind=ExecutorSafeFlattenLegKind.CANCEL_ORDER,
            target={
                "order_id": "broker-1",
                "client_order_id": "client-1",
                "symbol": "SPY",
            },
            command_operation="cancel_order",
            command_payload={"order_id": "broker-1", "client_order_id": None},
            **self._context(run_id),
        )

        for target in (
            ExecutorSafeFlattenState.CANCEL_CONFIRMED,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        ):
            with self.assertRaises(ExecutorCommandTransitionError):
                journal.transition_safe_flatten(
                    operation_id,
                    target,
                    expected_state=operation.state,
                    event_type="forbidden_phase_change",
                    metadata={},
                    error_code=(
                        "cancel_rejected"
                        if target is ExecutorSafeFlattenState.FAILED_LATCHED
                        else None
                    ),
                    **self._context(run_id),
                )
        journal.full_audit()

    def test_safe_flatten_unknown_recovery_is_one_atomic_transition(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        operation_id = self._start_safe_flatten(journal, run_id)
        journal.transition_safe_flatten(
            operation_id,
            ExecutorSafeFlattenState.CANCELING,
            expected_state=ExecutorSafeFlattenState.LATCHED,
            event_type="open_orders_observed",
            metadata={"open_order_count": 1},
            **self._context(run_id),
        )
        leg_id = _request_id(501)
        journal.begin_safe_flatten_leg(
            operation_id=operation_id,
            leg_id=leg_id,
            ordinal=0,
            kind=ExecutorSafeFlattenLegKind.CANCEL_ORDER,
            target={
                "order_id": "broker-1",
                "client_order_id": "client-1",
                "symbol": "SPY",
            },
            command_operation="cancel_order",
            command_payload={"order_id": "broker-1", "client_order_id": None},
            **self._context(run_id),
        )
        blocked = journal.mark_safe_flatten_leg_outcome_unknown(
            leg_id=leg_id,
            reason="cancel_unresolved",
            **self._context(run_id),
        )
        self.assertEqual(blocked.state, ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN)
        journal.transition_run(
            run_id,
            ExecutorRunState.BLOCKED,
            expected_state=ExecutorRunState.READY,
        )
        journal.transition_run(
            run_id,
            ExecutorRunState.RECOVERING,
            expected_state=ExecutorRunState.BLOCKED,
        )

        with self.assertRaises(ExecutorCommandTransitionError):
            journal.resolve_unknown_from_broker_observation(
                leg_id,
                client_order_id="client-1",
                broker_order_id="broker-1",
                observed_status="pending_cancel",
                **self._context(run_id),
            )
        resumed = journal.resolve_safe_flatten_unknown_from_broker_observation(
            leg_id=leg_id,
            client_order_id="client-1",
            broker_order_id="broker-1",
            observed_status="pending_cancel",
            **self._context(run_id),
        )

        self.assertEqual(resumed.state, ExecutorSafeFlattenState.CANCELING)
        self.assertEqual(
            journal.safe_flatten_legs(operation_id)[0].state,
            ExecutorSafeFlattenLegState.ACCEPTED,
        )
        self.assertEqual(journal.get(leg_id).state, ExecutorCommandState.COMPLETED)
        journal.full_audit()

    def test_safe_flatten_verification_binds_broker_identity_and_full_fill(self) -> None:
        journal = self._journal()
        run_id = self._start_run(journal, run_index=1, fence_epoch=1)
        operation_id = self._start_safe_flatten(journal, run_id)
        journal.transition_safe_flatten(
            operation_id,
            ExecutorSafeFlattenState.CANCEL_CONFIRMED,
            expected_state=ExecutorSafeFlattenState.LATCHED,
            event_type="no_open_orders_observed",
            metadata={"open_order_count": 0},
            **self._context(run_id),
        )
        journal.transition_safe_flatten(
            operation_id,
            ExecutorSafeFlattenState.CLOSING,
            expected_state=ExecutorSafeFlattenState.CANCEL_CONFIRMED,
            event_type="close_phase_started",
            metadata={"open_order_count": 0},
            **self._context(run_id),
        )
        leg_id = _request_id(501)
        target = {
            "symbol": "SPY",
            "side": "sell",
            "quantity": 2.0,
            "client_order_id": "sf-close-1",
        }
        journal.begin_safe_flatten_leg(
            operation_id=operation_id,
            leg_id=leg_id,
            ordinal=0,
            kind=ExecutorSafeFlattenLegKind.CLOSE_POSITION,
            target=target,
            command_operation="submit_order",
            command_payload={
                "order": {
                    **target,
                    "notional": None,
                    "reference_price": None,
                    "order_type": "market",
                    "limit_price": None,
                    "position_intent": "close",
                }
            },
            **self._context(run_id),
        )
        journal.complete_safe_flatten_leg(
            leg_id=leg_id,
            result={
                "accepted": True,
                "status": "accepted",
                "reasons": [],
                "dry_run": False,
            },
            accepted=True,
            broker_order_id="broker-a",
            **self._context(run_id),
        )
        evidence = {
            "broker_order_id": "broker-b",
            "client_order_id": "sf-close-1",
            "filled_quantity": 2.0,
            "observed_at": journal.get(leg_id).updated_at,
            "observed_status": "filled",
            "quantity": 2.0,
            "side": "sell",
            "symbol": "SPY",
        }
        with self.assertRaises(ExecutorCommandCollisionError):
            journal.verify_safe_flatten_leg(
                leg_id=leg_id,
                verified=True,
                broker_order_id="broker-b",
                evidence=evidence,
                **self._context(run_id),
            )
        evidence["broker_order_id"] = "broker-a"
        verified = journal.verify_safe_flatten_leg(
            leg_id=leg_id,
            verified=True,
            broker_order_id="broker-a",
            evidence=evidence,
            **self._context(run_id),
        )
        self.assertEqual(verified.state, ExecutorSafeFlattenLegState.VERIFIED)
        journal.full_audit()


if __name__ == "__main__":
    unittest.main()
