from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from trading_ai.execution import alpaca_paper as alpaca_paper_module
from trading_ai.execution.account_supervisor import account_scope_sha256
from trading_ai.execution.alpaca_paper import (
    PaperAccount,
    PaperOrderResult,
    PaperOrderSnapshot,
    PaperPosition,
)
from trading_ai.execution.order_journal import OrderJournalReconciliationAttestation
from trading_ai.execution.paper_account_executor import (
    PaperAccountDispatchNotStartedError,
)
from trading_ai.execution.paper_executor_authz import (
    EXECUTOR_CAPABILITIES,
    ExecutorAuthorizationDenied,
)
from trading_ai.execution.paper_executor_ipc import (
    ExecutorTarget,
    PaperExecutorRequest,
    PaperExecutorRequestError,
    PeerCredentials,
)
from trading_ai.execution.paper_executor_journal import (
    DurableExecutorCommandJournal,
    ExecutorCommandState,
    ExecutorRunState,
    ExecutorSafeFlattenLegState,
    ExecutorSafeFlattenState,
)
from trading_ai.execution.paper_executor_service import (
    ExecutorRiskContext,
    PaperExecutorApplication,
    deterministic_command_id,
)

ACCOUNT_ID = "f9ef2f82-c09b-4af0-a439-243fe31f77d9"
POLICY_SHA256 = "b" * 64
AUTHZ_POLICY_SHA256 = "c" * 64
ACCOUNT_SCOPE = account_scope_sha256(
    broker="alpaca",
    environment="paper",
    account_id=ACCOUNT_ID,
)


class FakeAuthority:
    def __init__(self, *, path: Path, fence_epoch: int = 1) -> None:
        self.account_id = ACCOUNT_ID
        self.executor_journal_path = path
        self.order_journal_path = path.with_name("orders.sqlite3")
        self.fence_epoch = fence_epoch
        self.persistent_authority = True
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.persistent_authority = False


class FakeBroker:
    def __init__(self) -> None:
        self.executor_authority = None
        self.order_journal_path: Path | None = None
        self.submit_calls = 0
        self.cancel_calls = 0
        self.submitted_orders: list[object] = []
        self.lookup_unavailable = False
        self.positions: tuple[PaperPosition, ...] = ()
        self.open_orders: tuple[PaperOrderSnapshot, ...] | None = None
        self.reconciliation_attestation: OrderJournalReconciliationAttestation | None = None
        self.kill_switch_reasons: list[str] = []
        self.submit_result = PaperOrderResult(True, "submitted", (), False)
        self.cancel_result = PaperOrderResult(True, "cancel_requested", (), False)
        self.order = PaperOrderSnapshot(
            order_id="broker-1",
            client_order_id="paper-spy-1",
            symbol="SPY",
            side="buy",
            order_type="market",
            time_in_force="day",
            status="accepted",
            notional=10.0,
            quantity=None,
            filled_quantity=0.0,
            filled_avg_price=None,
            submitted_at="2026-07-15T15:00:00Z",
            created_at="2026-07-15T15:00:00Z",
            updated_at="2026-07-15T15:00:00Z",
            expires_at="2026-07-16T20:00:00Z",
        )

    @staticmethod
    def read_account() -> PaperAccount:
        return PaperAccount(
            account_id=ACCOUNT_ID,
            status="ACTIVE",
            cash=1000.0,
            equity=1000.0,
            buying_power=1000.0,
            last_equity=1000.0,
        )

    def read_positions(self) -> tuple[PaperPosition, ...]:
        return self.positions

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        del status
        return (self.order,) if self.open_orders is None else self.open_orders

    def get_order(self, *, order_id: str) -> PaperOrderSnapshot:
        if self.lookup_unavailable:
            raise TimeoutError("broker unavailable")
        if order_id != self.order.order_id:
            raise ValueError("not found")
        return self.order

    def get_order_by_client_id(self, client_order_id: str) -> PaperOrderSnapshot:
        if self.lookup_unavailable:
            raise TimeoutError("broker unavailable")
        if client_order_id != self.order.client_order_id:
            raise ValueError("not found")
        return self.order

    @staticmethod
    def list_fill_activities(*, after, until) -> tuple[object, ...]:
        del after, until
        return ()

    @staticmethod
    def latest_trade_price(symbol: str) -> float | None:
        return 100.0 if symbol == "SPY" else None

    def submit_order(self, _order, *, execution_guard) -> PaperOrderResult:
        execution_guard()
        self.submit_calls += 1
        self.submitted_orders.append(_order)
        return self.submit_result

    def cancel_order(
        self,
        client_order_id: str | None = None,
        *,
        order_id: str | None = None,
        execution_guard,
    ) -> PaperOrderResult:
        del client_order_id, order_id
        execution_guard()
        self.cancel_calls += 1
        return self.cancel_result

    def activate_kill_switch(self, reason: str) -> None:
        self.kill_switch_reasons.append(reason)

    def reconcile_flat_account_order_journal(
        self,
    ) -> OrderJournalReconciliationAttestation | None:
        return self.reconciliation_attestation


class FakeAuthorizationPolicy:
    policy_sha256 = AUTHZ_POLICY_SHA256

    def __init__(self, capabilities: frozenset[str] = EXECUTOR_CAPABILITIES) -> None:
        self.capabilities = capabilities

    @staticmethod
    def admit(_peer: PeerCredentials) -> bool:
        return True

    def require(self, _peer: PeerCredentials, capability: str) -> object:
        if capability not in self.capabilities:
            raise ExecutorAuthorizationDenied("denied")
        return object()


class PaperExecutorApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.path = self.root / "executor.sqlite3"
        self.scope = account_scope_sha256(
            broker="alpaca",
            environment="paper",
            account_id=ACCOUNT_ID,
        )
        self.journal = DurableExecutorCommandJournal(
            self.path,
            account_scope_sha256=self.scope,
        )
        self.authority = FakeAuthority(path=self.path)
        self.broker = FakeBroker()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _application(
        self,
        *,
        fence_epoch: int = 1,
        risk_provider=None,
        authorization_policy=None,
        run_index: int = 1,
        monotonic_clock=time.monotonic,
    ) -> PaperExecutorApplication:
        self.authority = FakeAuthority(path=self.path, fence_epoch=fence_epoch)
        self.broker.executor_authority = self.authority
        self.broker.order_journal_path = self.authority.order_journal_path
        return PaperExecutorApplication(
            broker=self.broker,  # type: ignore[arg-type]
            authority=self.authority,
            journal=self.journal,
            policy_sha256=POLICY_SHA256,
            authorization_policy=authorization_policy or FakeAuthorizationPolicy(),
            risk_context_provider=risk_provider,
            monotonic_clock=monotonic_clock,
            run_id_factory=lambda: uuid.UUID(int=100 + run_index).hex,
        )

    @staticmethod
    def _request(
        operation: str,
        payload: dict[str, object],
        *,
        request_index: int = 1,
        deadline_offset: float = 5.0,
    ) -> PaperExecutorRequest:
        target = None
        request_id = uuid.UUID(int=request_index).hex
        if operation == "submit_order":
            order = payload["order"]
            assert isinstance(order, dict)
            request_id = deterministic_command_id(
                operation,
                f"client_order_id:{order['client_order_id']}",
            )
        elif operation == "cancel_order":
            if payload["order_id"] is not None:
                business_key = f"order_id:{payload['order_id']}"
            else:
                business_key = f"client_order_id:{payload['client_order_id']}"
            request_id = deterministic_command_id(operation, business_key)
        elif operation == "latch_kill_switch":
            request_id = deterministic_command_id(
                operation,
                f"reason_code:{payload['reason_code']}",
            )
        elif operation == "start_safe_flatten":
            request_id = str(payload["operation_id"])
        if operation in {
            "submit_order",
            "cancel_order",
            "latch_kill_switch",
            "start_safe_flatten",
        }:
            target = ExecutorTarget(
                account_scope_sha256=ACCOUNT_SCOPE,
                policy_sha256=POLICY_SHA256,
                authz_policy_sha256=AUTHZ_POLICY_SHA256,
                run_id=uuid.UUID(int=101).hex,
                fence_epoch=1,
            )
        return PaperExecutorRequest(
            request_id=request_id,
            operation=operation,
            deadline_unix_ms=int((time.time() + deadline_offset) * 1_000),
            deadline_monotonic=time.monotonic() + deadline_offset,
            payload=payload,
            peer=PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid()),
            target=target,
        )

    @staticmethod
    def _submit_payload(**extra: object) -> dict[str, object]:
        order: dict[str, object] = {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "paper-spy-1",
            "quantity": None,
            "notional": 10.0,
            "reference_price": 100.0,
            "order_type": "market",
            "limit_price": None,
            "position_intent": "open",
        }
        order.update(extra)
        return {"order": order}

    def _seed_previous_command(
        self,
        operation: str,
        payload: dict[str, object],
        *,
        dispatching: bool = True,
    ) -> str:
        run_one = uuid.UUID(int=101).hex
        self.journal.start_run(
            run_id=run_one,
            policy_sha256=POLICY_SHA256,
        )
        self.journal.transition_run(
            run_one,
            ExecutorRunState.RECOVERING,
            expected_state=ExecutorRunState.STARTING,
        )
        self.journal.transition_run(
            run_one,
            ExecutorRunState.READY,
            expected_state=ExecutorRunState.RECOVERING,
        )
        context = {
            "run_id": run_one,
            "fence_epoch": 1,
            "policy_sha256": POLICY_SHA256,
        }
        request_id = self._request(operation, payload).request_id
        self.journal.record(
            request_id=request_id,
            operation=operation,
            payload=payload,
            **context,
        )
        if dispatching:
            self.journal.claim(request_id, **context)
        return request_id

    def test_start_ready_health_reads_and_clean_stop(self) -> None:
        app = self._application(fence_epoch=999)
        health = app.start()

        self.assertEqual(health.status, "ready")
        self.assertEqual(health.fence_epoch, 1)
        self.assertTrue(health.mutations_allowed)
        self.assertEqual(health.authz_policy_sha256, AUTHZ_POLICY_SHA256)
        self.assertEqual(
            app.handle(self._request("read_account", {}, request_index=2))["account"]["equity"],  # type: ignore[index]
            1000.0,
        )
        self.assertEqual(
            len(app.handle(self._request("list_orders", {"status": "open"}, request_index=3))["orders"]),  # type: ignore[arg-type]
            1,
        )
        app.close()

        self.assertEqual(app.run_state, ExecutorRunState.STOPPED)
        self.assertTrue(self.authority.closed)

    def test_observer_cannot_mutate_and_denial_precedes_journal_or_broker(self) -> None:
        observer = FakeAuthorizationPolicy(frozenset({"health", "observe"}))
        app = self._application(authorization_policy=observer)
        app.start()
        requests = (
            self._request("submit_order", self._submit_payload()),
            self._request(
                "submit_order",
                self._submit_payload(
                    position_intent="close",
                    quantity=1.0,
                    notional=None,
                ),
                request_index=2,
            ),
            self._request(
                "cancel_order",
                {"order_id": "broker-1", "client_order_id": None},
                request_index=3,
            ),
            self._request(
                "latch_kill_switch",
                {"reason_code": "operator_stop"},
                request_index=4,
            ),
        )

        for request in requests:
            with self.subTest(operation=request.operation), self.assertRaises(
                PaperExecutorRequestError
            ) as raised:
                app.handle(request)
            self.assertEqual(raised.exception.code, "authorization_denied")
            self.assertIsNone(self.journal.get(request.request_id))

        self.assertEqual(self.broker.submit_calls, 0)
        self.assertEqual(self.broker.cancel_calls, 0)
        self.assertEqual(self.broker.kill_switch_reasons, [])
        app.close()

    def test_safe_flatten_start_requires_composite_authorization_before_journal(self) -> None:
        capabilities = frozenset(EXECUTOR_CAPABILITIES - {"reduce"})
        app = self._application(
            authorization_policy=FakeAuthorizationPolicy(capabilities)
        )
        app.start()
        operation_id = uuid.UUID(int=500).hex
        request = self._request(
            "start_safe_flatten",
            {"operation_id": operation_id},
            request_index=500,
        )

        with self.assertRaises(PaperExecutorRequestError) as raised:
            app.handle(request)

        self.assertEqual(raised.exception.code, "authorization_denied")
        self.assertIsNone(self.journal.get_safe_flatten(operation_id))
        self.assertFalse(self.journal.read_control_state().kill_switch_active)
        self.assertEqual(self.broker.kill_switch_reasons, [])
        app.close()

    def test_safe_flatten_daemon_workflow_is_serial_durable_and_stays_latched(self) -> None:
        app = self._application()
        app.start()
        operation_id = uuid.UUID(int=500).hex
        request = self._request(
            "start_safe_flatten",
            {"operation_id": operation_id},
            request_index=500,
        )

        started = app.handle(request)["operation"]

        self.assertEqual(started["state"], "latched")
        health = app.health()
        self.assertEqual(health.status, "flattening")
        self.assertFalse(health.mutations_allowed)
        self.assertTrue(health.kill_switch_active)
        active = app.handle(self._request("get_active_safe_flatten", {}))["operation"]
        self.assertEqual(active["operation_id"], operation_id)
        with self.assertRaises(PaperExecutorRequestError):
            app.handle(
                self._request(
                    "cancel_order",
                    {"order_id": "broker-1", "client_order_id": None},
                    request_index=501,
                )
            )

        app.advance_safe_flatten_once()
        self.assertEqual(
            self.journal.get_safe_flatten(operation_id).state,
            ExecutorSafeFlattenState.CANCELING,
        )
        app.advance_safe_flatten_once()
        self.assertEqual(self.broker.cancel_calls, 1)
        app.advance_safe_flatten_once()
        self.assertEqual(self.broker.cancel_calls, 1)

        self.broker.order = replace(self.broker.order, status="canceled")
        app.advance_safe_flatten_once()
        self.broker.open_orders = ()
        app.advance_safe_flatten_once()
        app.advance_safe_flatten_once()
        self.assertEqual(
            self.journal.get_safe_flatten(operation_id).state,
            ExecutorSafeFlattenState.CLOSING,
        )

        self.broker.positions = (
            PaperPosition(
                symbol="SPY",
                quantity=2.0,
                market_value=200.0,
                avg_entry_price=90.0,
                current_price=100.0,
            ),
        )
        app.advance_safe_flatten_once()
        self.assertEqual(self.broker.submit_calls, 1)
        submitted = self.broker.submitted_orders[0]
        self.assertEqual(submitted.side, "sell")
        self.assertEqual(submitted.quantity, 2.0)
        self.broker.order = replace(
            self.broker.order,
            order_id="broker-close-1",
            client_order_id=submitted.client_order_id,
            side="sell",
            status="filled",
            notional=None,
            quantity=2.0,
            filled_quantity=2.0,
        )
        app.advance_safe_flatten_once()
        app.advance_safe_flatten_once()
        self.assertEqual(self.broker.submit_calls, 1)

        self.broker.positions = ()
        app.advance_safe_flatten_once()
        app.advance_safe_flatten_once()
        self.broker.reconciliation_attestation = OrderJournalReconciliationAttestation(
            record_count=2,
            max_event_sequence=8,
            projection_sha256="d" * 64,
        )
        final = app.advance_safe_flatten_once()

        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.state, ExecutorSafeFlattenState.FLAT_LATCHED)
        status = app.handle(
            self._request(
                "get_safe_flatten_status",
                {"operation_id": operation_id},
                request_index=502,
            )
        )["operation"]
        self.assertTrue(status["terminal"])
        self.assertTrue(status["reconciled"])
        self.assertTrue(status["kill_switch_active"])
        self.assertTrue(self.journal.read_control_state().kill_switch_active)
        self.journal.full_audit()
        app.close()

    def test_safe_flatten_restart_recovers_ambiguous_cancel_without_second_delete(self) -> None:
        self.broker.cancel_result = PaperOrderResult(
            False,
            "cancel_unresolved",
            ("cancel_request_ambiguous",),
            False,
        )
        app = self._application(run_index=1)
        app.start()
        operation_id = uuid.UUID(int=500).hex
        app.handle(
            self._request(
                "start_safe_flatten",
                {"operation_id": operation_id},
                request_index=500,
            )
        )
        app.advance_safe_flatten_once()
        blocked = app.advance_safe_flatten_once()

        self.assertEqual(
            blocked.state,
            ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
        )
        self.assertEqual(app.run_state, ExecutorRunState.BLOCKED)
        self.assertEqual(self.broker.cancel_calls, 1)
        app.close()

        self.broker.order = replace(self.broker.order, status="pending_cancel")
        restarted = self._application(fence_epoch=2, run_index=2)
        health = restarted.start()

        self.assertEqual(health.status, "flattening")
        self.assertEqual(health.pending_recovery, 0)
        self.assertEqual(self.broker.cancel_calls, 1)
        recovered = self.journal.get_safe_flatten(operation_id)
        self.assertEqual(recovered.state, ExecutorSafeFlattenState.CANCELING)
        self.journal.full_audit()
        restarted.close()

    def test_safe_flatten_restart_recovers_ambiguous_close_without_second_submit(self) -> None:
        self.broker.open_orders = ()
        self.broker.positions = (
            PaperPosition(
                symbol="SPY",
                quantity=2.0,
                market_value=200.0,
                avg_entry_price=90.0,
                current_price=100.0,
            ),
        )
        self.broker.submit_result = PaperOrderResult(
            False,
            "submit_unresolved",
            ("submit_request_ambiguous",),
            False,
        )
        app = self._application(run_index=1)
        app.start()
        operation_id = uuid.UUID(int=501).hex
        app.handle(
            self._request(
                "start_safe_flatten",
                {"operation_id": operation_id},
                request_index=501,
            )
        )
        app.advance_safe_flatten_once()
        app.advance_safe_flatten_once()
        blocked = app.advance_safe_flatten_once()

        self.assertEqual(
            blocked.state,
            ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
        )
        self.assertEqual(app.run_state, ExecutorRunState.BLOCKED)
        self.assertEqual(self.broker.submit_calls, 1)
        submitted = self.broker.submitted_orders[0]
        app.close()

        self.broker.order = replace(
            self.broker.order,
            order_id="broker-close-1",
            client_order_id=submitted.client_order_id,
            side="sell",
            status="accepted",
            notional=None,
            quantity=2.0,
            filled_quantity=0.0,
        )
        restarted = self._application(fence_epoch=2, run_index=2)
        health = restarted.start()

        self.assertEqual(health.status, "flattening")
        self.assertEqual(health.pending_recovery, 0)
        self.assertEqual(self.broker.submit_calls, 1)
        recovered = self.journal.get_safe_flatten(operation_id)
        self.assertEqual(recovered.state, ExecutorSafeFlattenState.CLOSING)
        close_legs = self.journal.safe_flatten_legs(operation_id)
        self.assertEqual(len(close_legs), 1)
        self.assertEqual(close_legs[0].state, ExecutorSafeFlattenLegState.ACCEPTED)
        self.journal.full_audit()
        restarted.close()

    def test_crossed_broker_and_authority_capabilities_are_rejected(self) -> None:
        authority = FakeAuthority(path=self.path)
        crossed_authority = FakeAuthority(path=self.path)
        self.broker.executor_authority = crossed_authority
        self.broker.order_journal_path = authority.order_journal_path

        with self.assertRaisesRegex(ValueError, "same capability"):
            PaperExecutorApplication(
                broker=self.broker,  # type: ignore[arg-type]
                authority=authority,
                journal=self.journal,
                policy_sha256=POLICY_SHA256,
                authorization_policy=FakeAuthorizationPolicy(),
            )

        self.assertEqual(self.broker.submit_calls, 0)
        self.assertEqual(self.broker.cancel_calls, 0)

    def test_start_account_mismatch_closes_authority_lease(self) -> None:
        app = self._application()
        self.broker.read_account = lambda: PaperAccount(
            account_id="00000000-0000-0000-0000-000000000001",
            status="ACTIVE",
            cash=1000.0,
            equity=1000.0,
            buying_power=1000.0,
            last_equity=1000.0,
        )

        with self.assertRaisesRegex(ValueError, "does not match authority"):
            app.start()

        self.assertIsNone(app.run_state)
        self.assertTrue(self.authority.closed)
        self.assertFalse(self.authority.persistent_authority)

    def test_start_account_read_failure_closes_authority_lease(self) -> None:
        def fail_account_read() -> PaperAccount:
            raise TimeoutError("broker unavailable")

        app = self._application()
        self.broker.read_account = fail_account_read

        with self.assertRaisesRegex(TimeoutError, "broker unavailable"):
            app.start()

        self.assertIsNone(app.run_state)
        self.assertTrue(self.authority.closed)
        self.assertFalse(self.authority.persistent_authority)

    def test_opening_submit_without_trusted_risk_provider_fails_closed_and_replays(self) -> None:
        app = self._application()
        app.start()
        request = self._request("submit_order", self._submit_payload())

        first = app.handle(request)
        second = app.handle(request)

        self.assertFalse(first["accepted"])
        self.assertEqual(first["reasons"], ["trusted_risk_context_unavailable"])
        self.assertEqual(second, first)
        self.assertEqual(self.broker.submit_calls, 0)
        self.assertEqual(self.journal.get(request.request_id).state, ExecutorCommandState.COMPLETED)  # type: ignore[union-attr]
        app.close()

    def test_submit_with_trusted_context_calls_broker_once_and_terminal_replay_is_cached(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        first = app.handle(request)
        second = app.handle(request)

        self.assertTrue(first["accepted"])
        self.assertEqual(second, first)
        self.assertEqual(self.broker.submit_calls, 1)
        app.close()

    def test_ambiguous_broker_result_is_not_retried_and_blocks_new_mutations(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        self.broker.submit_result = PaperOrderResult(
            False,
            "submit_unresolved",
            ("ambiguous_submit_error",),
            False,
        )
        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        with self.assertRaisesRegex(PaperExecutorRequestError, "broker-first"):
            app.handle(request)
        with self.assertRaisesRegex(PaperExecutorRequestError, "pending recovery"):
            app.handle(
                self._request(
                    "cancel_order",
                    {"order_id": "broker-1", "client_order_id": None},
                    request_index=2,
                )
            )

        self.assertEqual(self.broker.submit_calls, 1)
        self.assertFalse(app.health().mutations_allowed)
        self.assertEqual(
            self.journal.get(request.request_id).state,  # type: ignore[union-attr]
            ExecutorCommandState.OUTCOME_UNKNOWN,
        )
        app.close()

    def test_proven_submit_predispatch_failure_retries_once_and_posts_once(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        broker_attempts = 0

        def submit_order(order, *, execution_guard) -> PaperOrderResult:
            nonlocal broker_attempts
            broker_attempts += 1
            if broker_attempts == 1:
                return alpaca_paper_module._not_dispatched_result(  # noqa: SLF001
                    operation="submit_order",
                    status="submit_deferred",
                    reason="paper_account_pre_dispatch_failed",
                )
            execution_guard()
            self.broker.submit_calls += 1
            self.broker.submitted_orders.append(order)
            return PaperOrderResult(True, "submitted", (), False)

        self.broker.submit_order = submit_order
        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        result = app.handle(request)

        self.assertTrue(result["accepted"])
        self.assertEqual(broker_attempts, 2)
        self.assertEqual(self.broker.submit_calls, 1)
        self.assertEqual(
            tuple(event.event_type for event in self.journal.events(request.request_id)),
            (
                "recorded",
                "dispatch_claimed",
                "dispatch_proven_not_started",
                "dispatch_claimed",
                "dispatch_completed",
            ),
        )
        self.journal.full_audit()
        app.close()

    def test_proven_cancel_predispatch_failure_retries_once_and_deletes_once(self) -> None:
        broker_attempts = 0

        def cancel_order(
            client_order_id: str | None = None,
            *,
            order_id: str | None = None,
            execution_guard,
        ) -> PaperOrderResult:
            nonlocal broker_attempts
            del client_order_id, order_id
            broker_attempts += 1
            if broker_attempts == 1:
                return alpaca_paper_module._not_dispatched_result(  # noqa: SLF001
                    operation="cancel_order",
                    status="cancel_deferred",
                    reason="paper_account_pre_dispatch_failed",
                )
            execution_guard()
            self.broker.cancel_calls += 1
            return PaperOrderResult(True, "cancel_requested", (), False)

        self.broker.cancel_order = cancel_order
        app = self._application()
        app.start()
        request = self._request(
            "cancel_order",
            {"order_id": "broker-1", "client_order_id": None},
        )

        result = app.handle(request)

        self.assertTrue(result["accepted"])
        self.assertEqual(broker_attempts, 2)
        self.assertEqual(self.broker.cancel_calls, 1)
        self.assertEqual(
            tuple(event.event_type for event in self.journal.events(request.request_id)),
            (
                "recorded",
                "dispatch_claimed",
                "dispatch_proven_not_started",
                "dispatch_claimed",
                "dispatch_completed",
            ),
        )
        self.journal.full_audit()
        app.close()

    def test_unproven_deferred_status_is_unknown_and_never_retried(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        self.broker.submit_result = PaperOrderResult(
            False,
            "submit_deferred",
            ("untrusted_status_only",),
            False,
        )
        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        with self.assertRaisesRegex(PaperExecutorRequestError, "broker-first"):
            app.handle(request)

        self.assertEqual(self.broker.submit_calls, 1)
        record = self.journal.get(request.request_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.state, ExecutorCommandState.OUTCOME_UNKNOWN)
        self.assertFalse(app.health().mutations_allowed)
        app.close()

    def test_result_subclass_cannot_override_not_dispatched_proof(self) -> None:
        class ForgedPaperOrderResult(PaperOrderResult):
            def proves_not_dispatched(self, operation: str) -> bool:
                del operation
                return True

        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        self.broker.submit_result = ForgedPaperOrderResult(
            False,
            "submit_deferred",
            ("forged_subclass",),
            False,
        )
        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        with self.assertRaisesRegex(PaperExecutorRequestError, "broker-first"):
            app.handle(request)

        self.assertEqual(self.broker.submit_calls, 1)
        self.assertEqual(
            self.journal.get(request.request_id).state,  # type: ignore[union-attr]
            ExecutorCommandState.OUTCOME_UNKNOWN,
        )
        self.assertFalse(app.health().mutations_allowed)
        app.close()

    def test_injected_broker_exception_cannot_forge_not_dispatched_proof(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        def submit_order(_order, *, execution_guard) -> PaperOrderResult:
            execution_guard()
            self.broker.submit_calls += 1
            raise PaperAccountDispatchNotStartedError(
                "forged after injected broker dispatch"
            )

        self.broker.submit_order = submit_order
        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        with self.assertRaisesRegex(PaperExecutorRequestError, "broker-first"):
            app.handle(request)
        with self.assertRaisesRegex(PaperExecutorRequestError, "pending recovery"):
            app.handle(request)

        self.assertEqual(self.broker.submit_calls, 1)
        self.assertEqual(
            self.journal.get(request.request_id).state,  # type: ignore[union-attr]
            ExecutorCommandState.OUTCOME_UNKNOWN,
        )
        self.assertFalse(app.health().mutations_allowed)
        app.close()

    def test_object_setattr_cannot_forge_not_dispatched_proof(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        forged = PaperOrderResult(
            False,
            "submit_deferred",
            ("forged_attribute",),
            False,
        )
        object.__setattr__(
            forged,
            "_not_dispatched_proof",
            (object(), "submit_order"),
        )
        self.broker.submit_result = forged
        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        with self.assertRaisesRegex(PaperExecutorRequestError, "broker-first"):
            app.handle(request)

        self.assertEqual(self.broker.submit_calls, 1)
        self.assertEqual(
            self.journal.get(request.request_id).state,  # type: ignore[union-attr]
            ExecutorCommandState.OUTCOME_UNKNOWN,
        )
        self.assertFalse(app.health().mutations_allowed)
        app.close()

    def test_second_proven_predispatch_failure_exhausts_retry_without_post(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        broker_attempts = 0

        def submit_order(_order, *, execution_guard) -> PaperOrderResult:
            nonlocal broker_attempts
            del execution_guard
            broker_attempts += 1
            return alpaca_paper_module._not_dispatched_result(  # noqa: SLF001
                operation="submit_order",
                status="submit_deferred",
                reason="paper_account_pre_dispatch_failed",
            )

        self.broker.submit_order = submit_order
        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())

        with self.assertRaisesRegex(
            PaperExecutorRequestError,
            "bounded retry was exhausted",
        ):
            app.handle(request)

        self.assertEqual(broker_attempts, 2)
        self.assertEqual(self.broker.submit_calls, 0)
        record = self.journal.get(request.request_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.state, ExecutorCommandState.REJECTED)
        self.assertEqual(record.error_code, "predispatch_retry_exhausted")
        self.journal.full_audit()
        app.close()

    def test_cancel_and_durable_kill_switch_use_closed_dtos(self) -> None:
        app = self._application()
        app.start()
        cancel = app.handle(
            self._request(
                "cancel_order",
                {"order_id": "broker-1", "client_order_id": None},
                request_index=1,
            )
        )
        latched = app.handle(
            self._request(
                "latch_kill_switch",
                {"reason_code": "daily_loss_limit"},
                request_index=2,
            )
        )

        self.assertTrue(cancel["accepted"])
        self.assertEqual(self.broker.cancel_calls, 1)
        self.assertTrue(latched["kill_switch_active"])
        self.assertEqual(self.journal.read_control_state().reason_code, "daily_loss_limit")
        self.assertEqual(self.broker.kill_switch_reasons, ["daily_loss_limit"])
        app.close()

    def test_latched_kill_switch_rejects_open_before_record_and_allows_reduce(
        self,
    ) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        app = self._application(risk_provider=risk_provider)
        app.start()
        app.handle(
            self._request(
                "latch_kill_switch",
                {"reason_code": "daily_loss_limit"},
            )
        )
        opening = self._request(
            "submit_order",
            self._submit_payload(client_order_id="paper-spy-open-blocked"),
        )

        with self.assertRaises(PaperExecutorRequestError) as raised:
            app.handle(opening)

        self.assertEqual(raised.exception.code, "command_journal_rejected")
        self.assertIsNone(self.journal.get(opening.request_id))
        self.assertEqual(self.journal.recovery_required(), ())
        self.assertEqual(self.broker.submit_calls, 0)
        health = app.health()
        self.assertTrue(health.mutations_allowed)
        self.assertFalse(health.opening_orders_allowed)
        self.assertEqual(health.capability_mode, "reduce_only")

        reducing = self._request(
            "submit_order",
            self._submit_payload(
                client_order_id="paper-spy-reduce-allowed",
                side="sell",
                position_intent="reduce",
            ),
        )
        result = app.handle(reducing)

        self.assertTrue(result["accepted"])
        self.assertEqual(self.broker.submit_calls, 1)
        app.close()

    def test_strict_payload_rejects_unknown_fields_before_broker(self) -> None:
        app = self._application()
        app.start()
        with self.assertRaisesRegex(PaperExecutorRequestError, "payload is invalid"):
            app.handle(
                self._request(
                    "submit_order",
                    self._submit_payload(daily_pnl_pct=0.0),
                )
            )
        with self.assertRaisesRegex(PaperExecutorRequestError, "deadline expired"):
            app.handle(self._request("health", {}, request_index=2, deadline_offset=-1.0))
        self.assertEqual(self.broker.submit_calls, 0)
        app.close()

    def test_dispatch_deadline_is_bound_to_server_monotonic_clock(self) -> None:
        app = self._application(monotonic_clock=lambda: 101.0)
        app.start()
        request = replace(
            self._request(
                "cancel_order",
                {"order_id": "broker-1", "client_order_id": None},
            ),
            deadline_unix_ms=9_999_999_999_999,
            deadline_monotonic=100.0,
        )

        with self.assertRaises(PaperExecutorRequestError) as raised:
            app.handle(request)

        self.assertEqual(raised.exception.code, "request_expired")
        self.assertEqual(self.broker.cancel_calls, 0)
        self.assertEqual(self.journal.recovery_required(), ())
        app.close()

    def test_incorrect_mutation_targets_and_request_id_never_reach_broker(self) -> None:
        def risk_provider(*_args) -> ExecutorRiskContext:
            return ExecutorRiskContext(0.01, 0.01, 0.0, 0.0)

        app = self._application(risk_provider=risk_provider)
        app.start()
        request = self._request("submit_order", self._submit_payload())
        assert request.target is not None
        mismatched_targets = (
            replace(request.target, account_scope_sha256="a" * 64),
            replace(request.target, policy_sha256="c" * 64),
            replace(request.target, run_id=uuid.UUID(int=999).hex),
            replace(request.target, fence_epoch=2),
        )

        for target in mismatched_targets:
            with self.subTest(target=target):
                with self.assertRaises(PaperExecutorRequestError) as raised:
                    app.handle(replace(request, target=target))
                self.assertEqual(raised.exception.code, "target_mismatch")

        with self.assertRaises(PaperExecutorRequestError) as raised:
            app.handle(replace(request, request_id=uuid.UUID(int=999).hex))
        self.assertEqual(raised.exception.code, "invalid_request_id")
        self.assertEqual(self.broker.submit_calls, 0)
        self.assertEqual(self.broker.cancel_calls, 0)
        self.assertEqual(self.journal.recovery_required(), ())
        app.close()

    def test_recorded_command_is_rejected_without_broker_write_on_restart(self) -> None:
        request_id = self._seed_previous_command(
            "submit_order",
            self._submit_payload(),
            dispatching=False,
        )

        app = self._application(fence_epoch=2, run_index=2)
        health = app.start()

        self.assertEqual(health.status, "ready")
        recovered = self.journal.get(request_id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, ExecutorCommandState.REJECTED)
        self.assertEqual(recovered.error_code, "command_not_dispatched")
        self.assertEqual(self.broker.submit_calls, 0)
        self.assertEqual(self.broker.cancel_calls, 0)
        app.close()

    def test_unknown_working_cancel_stays_blocked_without_second_delete(self) -> None:
        request_id = self._seed_previous_command(
            "cancel_order",
            {"order_id": "broker-1", "client_order_id": None},
        )

        app = self._application(fence_epoch=2, run_index=2)
        health = app.start()

        self.assertEqual(health.status, "blocked")
        self.assertEqual(health.pending_recovery, 1)
        self.assertEqual(self.broker.cancel_calls, 0)
        report = app.recover_once()
        self.assertEqual(report.pending, 1)
        self.assertEqual(self.broker.cancel_calls, 0)
        recovered = self.journal.get(request_id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, ExecutorCommandState.OUTCOME_UNKNOWN)
        app.close()

    def test_unknown_canceled_cancel_resolves_without_second_delete(self) -> None:
        request_id = self._seed_previous_command(
            "cancel_order",
            {"order_id": "broker-1", "client_order_id": None},
        )
        self.broker.order = replace(self.broker.order, status="canceled")

        app = self._application(fence_epoch=2, run_index=2)
        health = app.start()

        self.assertEqual(health.status, "ready")
        self.assertEqual(health.pending_recovery, 0)
        self.assertEqual(self.broker.cancel_calls, 0)
        recovered = self.journal.get(request_id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, ExecutorCommandState.COMPLETED)
        self.assertEqual(recovered.result["status"], "recovered_cancel_canceled")  # type: ignore[index]
        app.close()

    def test_unknown_pending_cancel_resolves_without_second_delete(self) -> None:
        request_id = self._seed_previous_command(
            "cancel_order",
            {"order_id": "broker-1", "client_order_id": None},
        )
        self.broker.order = replace(self.broker.order, status="pending_cancel")

        app = self._application(fence_epoch=2, run_index=2)
        health = app.start()

        self.assertEqual(health.status, "ready")
        self.assertEqual(health.pending_recovery, 0)
        self.assertEqual(self.broker.cancel_calls, 0)
        recovered = self.journal.get(request_id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, ExecutorCommandState.COMPLETED)
        self.assertEqual(recovered.result["status"], "recovered_cancel_pending_cancel")  # type: ignore[index]
        app.close()

    def test_submit_snapshot_with_different_intent_stays_blocked(self) -> None:
        request_id = self._seed_previous_command(
            "submit_order",
            self._submit_payload(),
        )
        self.broker.order = replace(self.broker.order, symbol="QQQ")

        app = self._application(fence_epoch=2, run_index=2)
        health = app.start()

        self.assertEqual(health.status, "blocked")
        self.assertEqual(health.pending_recovery, 1)
        self.assertEqual(self.broker.submit_calls, 0)
        report = app.recover_once()
        self.assertEqual(report.pending, 1)
        self.assertEqual(self.broker.submit_calls, 0)
        recovered = self.journal.get(request_id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, ExecutorCommandState.OUTCOME_UNKNOWN)
        app.close()

    def test_restart_recovers_dispatching_submit_from_matching_broker_order(self) -> None:
        run_one = uuid.UUID(int=101).hex
        self.journal.start_run(
            run_id=run_one,
            policy_sha256=POLICY_SHA256,
        )
        self.journal.transition_run(
            run_one,
            ExecutorRunState.RECOVERING,
            expected_state=ExecutorRunState.STARTING,
        )
        self.journal.transition_run(
            run_one,
            ExecutorRunState.READY,
            expected_state=ExecutorRunState.RECOVERING,
        )
        context = {
            "run_id": run_one,
            "fence_epoch": 1,
            "policy_sha256": POLICY_SHA256,
        }
        self.journal.record(
            request_id=_request_id(1),
            operation="submit_order",
            payload=self._submit_payload(),
            **context,
        )
        self.journal.claim(_request_id(1), **context)

        app = self._application(fence_epoch=2, run_index=2)
        health = app.start()

        self.assertEqual(health.status, "ready")
        self.assertTrue(health.mutations_allowed)
        self.assertEqual(health.pending_recovery, 0)
        recovered = self.journal.get(_request_id(1))
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, ExecutorCommandState.COMPLETED)
        self.assertEqual(recovered.result["status"], "recovered_submit_accepted")  # type: ignore[index]
        self.assertEqual(self.broker.submit_calls, 0)
        app.close()

    def test_restart_without_positive_broker_evidence_remains_blocked(self) -> None:
        run_one = uuid.UUID(int=101).hex
        self.journal.start_run(
            run_id=run_one,
            policy_sha256=POLICY_SHA256,
        )
        self.journal.transition_run(
            run_one,
            ExecutorRunState.RECOVERING,
            expected_state=ExecutorRunState.STARTING,
        )
        self.journal.transition_run(
            run_one,
            ExecutorRunState.READY,
            expected_state=ExecutorRunState.RECOVERING,
        )
        context = {
            "run_id": run_one,
            "fence_epoch": 1,
            "policy_sha256": POLICY_SHA256,
        }
        self.journal.record(
            request_id=_request_id(1),
            operation="submit_order",
            payload=self._submit_payload(),
            **context,
        )
        self.journal.claim(_request_id(1), **context)
        self.broker.lookup_unavailable = True

        app = self._application(fence_epoch=2, run_index=2)
        health = app.start()

        self.assertEqual(health.status, "blocked")
        self.assertFalse(health.mutations_allowed)
        self.assertEqual(health.pending_recovery, 1)
        report = app.recover_once()
        self.assertEqual(report.pending, 1)
        self.assertEqual(self.broker.submit_calls, 0)
        app.close()

    def test_canonical_journal_path_and_scope_are_mandatory(self) -> None:
        wrong_authority = FakeAuthority(path=self.root / "wrong.sqlite3")
        with self.assertRaisesRegex(ValueError, "canonical"):
            PaperExecutorApplication(
                broker=self.broker,  # type: ignore[arg-type]
                authority=wrong_authority,
                journal=self.journal,
                policy_sha256=POLICY_SHA256,
                authorization_policy=FakeAuthorizationPolicy(),
            )

    def test_deterministic_command_id_is_stable_and_domain_separated(self) -> None:
        first = deterministic_command_id("submit_order", "paper-spy-1")
        self.assertEqual(first, deterministic_command_id("submit_order", "paper-spy-1"))
        self.assertNotEqual(first, deterministic_command_id("cancel_order", "paper-spy-1"))


def _request_id(index: int) -> str:
    return uuid.UUID(int=index).hex


if __name__ == "__main__":
    unittest.main()
