"""High-level application boundary for the single Alpaca paper executor.

Only this application may own an exclusive paper client and an
``AlpacaPaperBroker``.  IPC callers exchange closed JSON DTOs; SDK objects,
credentials, risk limits and caller-supplied risk metrics never cross the
socket.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.execution.account_supervisor import account_scope_sha256
from trading_ai.execution.alpaca_paper import (
    TERMINAL_ORDER_STATUSES,
    AlpacaPaperBroker,
    PaperAccount,
    PaperFillActivity,
    PaperOrder,
    PaperOrderResult,
    PaperOrderSnapshot,
    PaperPosition,
)
from trading_ai.execution.order_journal import (
    OrderJournalError,
    OrderJournalReconciliationAttestation,
)
from trading_ai.execution.paper_executor_authz import (
    CAPABILITY_CANCEL,
    CAPABILITY_HEALTH,
    CAPABILITY_KILL,
    CAPABILITY_OBSERVE,
    CAPABILITY_OPEN,
    CAPABILITY_REDUCE,
    ExecutorAuthorizationDenied,
    ExecutorAuthorizationPolicy,
)
from trading_ai.execution.paper_executor_ipc import (
    PaperExecutorRequest,
    PaperExecutorRequestError,
)
from trading_ai.execution.paper_executor_journal import (
    DurableExecutorCommandJournal,
    ExecutorCommandJournalError,
    ExecutorCommandRecord,
    ExecutorCommandState,
    ExecutorRunState,
    ExecutorSafeFlattenLegKind,
    ExecutorSafeFlattenLegRecord,
    ExecutorSafeFlattenLegState,
    ExecutorSafeFlattenRecord,
    ExecutorSafeFlattenState,
)

_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,12}(?:/[A-Z0-9]{1,12})?\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_AMBIGUOUS_RESULT_STATUSES = frozenset({"submit_unresolved", "cancel_unresolved"})
_PREDISPATCH_RESULT_STATUSES = frozenset({"submit_deferred", "cancel_deferred"})
_MUTATING_OPERATIONS = frozenset(
    {"submit_order", "cancel_order", "latch_kill_switch", "start_safe_flatten"}
)
_SAFE_FLATTEN_STATUS_SCHEMA_VERSION = 1
_SAFE_FLATTEN_NAMESPACE = uuid.UUID("0f4bd025-e3a9-4e8f-a9ea-97fa1f570a23")
_ZERO_QUANTITY_TOLERANCE = 1e-9


@dataclass(frozen=True)
class ExecutorRiskContext:
    """Trusted, server-computed inputs consumed by the deterministic risk gate."""

    estimated_position_weight: float
    projected_gross_exposure: float
    daily_pnl_pct: float
    current_drawdown_pct: float


RiskContextProvider = Callable[
    [Mapping[str, object], PaperAccount, tuple[PaperPosition, ...], tuple[PaperOrderSnapshot, ...]],
    ExecutorRiskContext,
]


@dataclass(frozen=True)
class ExecutorHealth:
    status: str
    mutations_allowed: bool
    opening_orders_allowed: bool
    capability_mode: str
    account_scope_sha256: str
    fence_epoch: int
    policy_sha256: str
    authz_policy_sha256: str
    run_id: str
    pending_recovery: int
    kill_switch_active: bool


@dataclass(frozen=True)
class ExecutorRecoveryReport:
    resolved_recorded: int
    resolved_broker_order: int
    resolved_control: int
    pending: int


class PaperExecutorApplication:
    """Sequential high-level paper broker service with durable command replay."""

    def __init__(
        self,
        *,
        broker: AlpacaPaperBroker,
        authority: Any,
        journal: DurableExecutorCommandJournal,
        policy_sha256: str,
        authorization_policy: ExecutorAuthorizationPolicy,
        risk_context_provider: RiskContextProvider | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        if not bool(getattr(authority, "persistent_authority", False)):
            raise ValueError("paper executor requires process-lifetime account authority")
        fence_epoch = getattr(authority, "fence_epoch", None)
        account_id = getattr(authority, "account_id", None)
        executor_path = getattr(authority, "executor_journal_path", None)
        if type(fence_epoch) is not int or fence_epoch < 1:
            raise ValueError("paper executor authority fence is invalid")
        if type(account_id) is not str or not account_id:
            raise ValueError("paper executor account identity is invalid")
        expected_scope = account_scope_sha256(
            broker="alpaca",
            environment="paper",
            account_id=account_id,
        )
        if journal.account_scope_sha256 != expected_scope:
            raise ValueError("paper executor journal account scope is invalid")
        if executor_path is not None and Path(executor_path) != journal.path:
            raise ValueError("paper executor journal path is not canonical")
        if getattr(broker, "executor_authority", None) is not authority:
            raise ValueError("paper executor broker and account authority are not the same capability")
        authority_order_path = getattr(authority, "order_journal_path", None)
        broker_order_path = getattr(broker, "order_journal_path", None)
        if (
            not isinstance(authority_order_path, (str, Path))
            or not isinstance(broker_order_path, (str, Path))
            or Path(authority_order_path) != Path(broker_order_path)
        ):
            raise ValueError("paper executor broker order journal is not canonical")
        self._broker = broker
        self._authority = authority
        self._journal = journal
        self._policy_sha256 = _sha256(policy_sha256, label="policy_sha256")
        if not callable(getattr(authorization_policy, "require", None)) or not callable(
            getattr(authorization_policy, "admit", None)
        ):
            raise ValueError("paper executor authorization policy is invalid")
        self._authorization_policy = authorization_policy
        self._authz_policy_sha256 = _sha256(
            authorization_policy.policy_sha256,
            label="authz_policy_sha256",
        )
        self._risk_context_provider = risk_context_provider
        self._monotonic_clock = monotonic_clock
        self._run_id_factory = run_id_factory
        self._account_scope_sha256 = expected_scope
        self._lease_generation = fence_epoch
        self._fence_epoch: int | None = None
        self._run_id: str | None = None
        self._run_state: ExecutorRunState | None = None

    @property
    def run_id(self) -> str:
        if self._run_id is None:
            raise RuntimeError("paper executor application is not started")
        return self._run_id

    @property
    def run_state(self) -> ExecutorRunState | None:
        return self._run_state

    def start(self) -> ExecutorHealth:
        if self._run_id is not None:
            raise RuntimeError("paper executor application is already started")
        try:
            account = self._broker.read_account()
            if account.account_id != getattr(self._authority, "account_id", None):
                raise ValueError("paper executor broker account identity does not match authority")
            if str(account.status).strip().lower() != "active":
                raise ValueError("paper executor broker account is not active")
            run_id = self._run_id_factory()
            started = self._journal.start_run(
                run_id=run_id,
                policy_sha256=self._policy_sha256,
                pid=os.getpid(),
            )
            self._fence_epoch = started.fence_epoch
            self._run_id = started.run_id
            self._run_state = ExecutorRunState.STARTING
            self._transition_run(ExecutorRunState.STARTING, ExecutorRunState.RECOVERING)
            control = self._journal.read_control_state()
            if control.kill_switch_active:
                self._broker.activate_kill_switch(control.reason_code or "durable_kill_switch")
            recovery = self._recover_pending_commands()
            if recovery.pending:
                self._transition_run(ExecutorRunState.RECOVERING, ExecutorRunState.BLOCKED)
            else:
                self._transition_run(ExecutorRunState.RECOVERING, ExecutorRunState.READY)
            return self.health()
        except Exception:
            self._fail_start_best_effort()
            self._close_authority()
            raise

    def recover_once(self) -> ExecutorRecoveryReport:
        """Perform one read-only broker-first recovery pass.

        This method never submits or cancels an order.  A daemon may call it
        periodically while BLOCKED; absence of a broker order remains pending.
        """

        if self._run_state is ExecutorRunState.BLOCKED:
            self._transition_run(ExecutorRunState.BLOCKED, ExecutorRunState.RECOVERING)
        elif self._run_state is not ExecutorRunState.RECOVERING:
            raise RuntimeError("paper executor recovery requires a blocked run")
        try:
            report = self._recover_pending_commands()
            if report.pending:
                self._transition_run(
                    ExecutorRunState.RECOVERING,
                    ExecutorRunState.BLOCKED,
                )
            else:
                self._transition_run(
                    ExecutorRunState.RECOVERING,
                    ExecutorRunState.READY,
                )
            return report
        except Exception:
            if self._run_state is ExecutorRunState.RECOVERING:
                with suppress(ExecutorCommandJournalError):
                    self._transition_run(
                        ExecutorRunState.RECOVERING,
                        ExecutorRunState.BLOCKED,
                    )
            raise

    def close(self) -> None:
        run_id = self._run_id
        state = self._run_state
        try:
            if run_id is not None and state not in {
                None,
                ExecutorRunState.STOPPED,
                ExecutorRunState.CRASHED,
                ExecutorRunState.FAILED,
            }:
                if state is not ExecutorRunState.DRAINING:
                    self._transition_run(state, ExecutorRunState.DRAINING)
                self._transition_run(ExecutorRunState.DRAINING, ExecutorRunState.STOPPED)
        finally:
            self._close_authority()

    def health(self) -> ExecutorHealth:
        if self._run_id is None or self._run_state is None or self._fence_epoch is None:
            raise RuntimeError("paper executor application is not started")
        pending = len(self._journal.recovery_required())
        control = self._journal.read_control_state()
        active_flatten = self._journal.active_safe_flatten()
        flattening = (
            self._run_state is ExecutorRunState.READY
            and pending == 0
            and active_flatten is not None
        )
        mutations_allowed = (
            self._run_state is ExecutorRunState.READY
            and pending == 0
            and active_flatten is None
        )
        opening_orders_allowed = (
            mutations_allowed and self._risk_context_provider is not None and not control.kill_switch_active
        )
        capability_mode = "full" if opening_orders_allowed else "reduce_only" if mutations_allowed else "blocked"
        return ExecutorHealth(
            status="flattening" if flattening else self._run_state.value,
            mutations_allowed=mutations_allowed,
            opening_orders_allowed=opening_orders_allowed,
            capability_mode=capability_mode,
            account_scope_sha256=self._account_scope_sha256,
            fence_epoch=self._fence_epoch,
            policy_sha256=self._policy_sha256,
            authz_policy_sha256=self._authz_policy_sha256,
            run_id=self._run_id,
            pending_recovery=pending,
            kill_switch_active=control.kill_switch_active,
        )

    def metadata(self) -> dict[str, object]:
        health = self.health()
        return {
            "account_scope_sha256": health.account_scope_sha256,
            "fence_epoch": health.fence_epoch,
            "mutations_allowed": health.mutations_allowed,
            "opening_orders_allowed": health.opening_orders_allowed,
            "capability_mode": health.capability_mode,
            "policy_sha256": health.policy_sha256,
            "authz_policy_sha256": health.authz_policy_sha256,
            "run_id": health.run_id,
            "status": health.status,
        }

    def handle(self, request: PaperExecutorRequest) -> Mapping[str, Any]:
        self._deadline_guard(request)
        operation = request.operation
        payload = request.payload
        try:
            if operation == "health":
                _require_fields(payload, frozenset())
                self._authorize(request, CAPABILITY_HEALTH)
                return _health_to_dict(self.health())
            if operation == "read_account":
                _require_fields(payload, frozenset())
                self._authorize(request, CAPABILITY_OBSERVE)
                return {"account": _account_to_dict(self._broker.read_account())}
            if operation == "read_positions":
                _require_fields(payload, frozenset())
                self._authorize(request, CAPABILITY_OBSERVE)
                return {"positions": [_position_to_dict(position) for position in self._broker.read_positions()]}
            if operation == "list_orders":
                _require_fields(payload, frozenset({"status"}))
                status = _enum(payload["status"], {"open", "closed", "all"}, label="status")
                self._authorize(request, CAPABILITY_OBSERVE)
                return {"orders": [_order_snapshot_to_dict(order) for order in self._broker.list_orders(status=status)]}
            if operation == "get_order":
                self._authorize(request, CAPABILITY_OBSERVE)
                return {"order": _order_snapshot_to_dict(self._get_order(payload))}
            if operation == "list_fill_activities":
                self._authorize(request, CAPABILITY_OBSERVE)
                return {"fills": [_fill_to_dict(fill) for fill in self._list_fill_activities(payload)]}
            if operation == "latest_trade_price":
                _require_fields(payload, frozenset({"symbol"}))
                symbol = _symbol(payload["symbol"])
                self._authorize(request, CAPABILITY_OBSERVE)
                return {"symbol": symbol, "price": self._broker.latest_trade_price(symbol)}
            if operation == "get_safe_flatten_status":
                _require_fields(payload, frozenset({"operation_id"}))
                operation_id = _uuid_hex(payload["operation_id"], label="operation_id")
                self._authorize(request, CAPABILITY_OBSERVE)
                record = self._journal.get_safe_flatten(operation_id)
                if record is None:
                    raise PaperExecutorRequestError(
                        "operation_not_found",
                        "safe-flatten operation does not exist",
                    )
                return {"operation": self._safe_flatten_status(record)}
            if operation == "get_active_safe_flatten":
                _require_fields(payload, frozenset())
                self._authorize(request, CAPABILITY_OBSERVE)
                active = self._journal.active_safe_flatten()
                return {
                    "operation": (
                        None if active is None else self._safe_flatten_status(active)
                    )
                }
            if operation in _MUTATING_OPERATIONS:
                self._validate_mutation_payload(request)
                if operation == "start_safe_flatten":
                    for capability in (
                        CAPABILITY_OBSERVE,
                        CAPABILITY_KILL,
                        CAPABILITY_CANCEL,
                        CAPABILITY_REDUCE,
                    ):
                        self._authorize(request, capability)
                    return self._start_safe_flatten(request)
                self._authorize(request, self._mutation_capability(request))
                return self._execute_mutation(request)
        except PaperExecutorRequestError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise PaperExecutorRequestError(
                "invalid_request",
                "executor request payload is invalid",
            ) from exc
        raise PaperExecutorRequestError("operation_denied", "executor operation is not allowed")

    def _execute_mutation(self, request: PaperExecutorRequest) -> dict[str, object]:
        self._require_ready()
        self._require_mutation_target(request)

        if request.request_id != _expected_mutation_request_id(request):
            raise PaperExecutorRequestError(
                "invalid_request_id",
                "executor mutation request id is not bound to its business identity",
            )

        clean_payload = dict(request.payload)
        try:
            record, _created = self._journal.record(
                request_id=request.request_id,
                operation=request.operation,
                payload=clean_payload,
                **self._command_context(),
            )
        except ExecutorCommandJournalError as exc:
            raise PaperExecutorRequestError(
                "command_journal_rejected",
                "executor command identity or journal state was rejected",
            ) from exc

        replay = self._terminal_replay(record)
        if replay is not None:
            return replay
        if record.state is not ExecutorCommandState.RECORDED:
            raise PaperExecutorRequestError(
                "command_outcome_unknown",
                "executor command requires broker-first reconciliation",
            )
        try:
            self._journal.claim(request.request_id, **self._command_context())
            if request.operation == "latch_kill_switch":
                return self._latch_kill_switch(request)

            # A supervised broker may prove that it failed before entering the
            # raw SDK mutation method.  Persist that fact, reclaim the same
            # durable command and allow exactly one same-run retry.  No status
            # string can authorize this path: the one-shot, operation-bound
            # PaperOrderResult proof from the audited daemon must match.
            for dispatch_index in range(2):
                result = (
                    self._submit_order(request)
                    if request.operation == "submit_order"
                    else self._cancel_order(request)
                )
                if type(result) is not PaperOrderResult:
                    raise ExecutorCommandJournalError(
                        "broker mutation returned an invalid result capability"
                    )
                proven_not_dispatched = PaperOrderResult.proves_not_dispatched(
                    result,
                    request.operation,
                )
                if proven_not_dispatched:
                    if dispatch_index == 0:
                        self._journal.requeue_proven_not_dispatched(
                            request.request_id,
                            operation=request.operation,
                            reason=result.status,
                            **self._command_context(),
                        )
                        self._journal.claim(
                            request.request_id,
                            **self._command_context(),
                        )
                        continue
                    self._journal.reject_proven_not_dispatched_retry_exhausted(
                        request.request_id,
                        operation=request.operation,
                        reason=result.status,
                        **self._command_context(),
                    )
                    raise PaperExecutorRequestError(
                        "predispatch_retry_exhausted",
                        "executor command was not dispatched and its bounded retry was exhausted",
                    )

                if result.status in _AMBIGUOUS_RESULT_STATUSES | _PREDISPATCH_RESULT_STATUSES:
                    self._journal.mark_outcome_unknown(
                        request.request_id,
                        reason=result.status,
                        **self._command_context(),
                    )
                    self._block_run_best_effort()
                    raise PaperExecutorRequestError(
                        "command_outcome_unknown",
                        "executor command requires broker-first reconciliation",
                    )
                completed = self._journal.complete(
                    request.request_id,
                    _order_result_to_dict(result),
                    **self._command_context(),
                )
                assert completed.result is not None
                return completed.result
            raise AssertionError("bounded pre-dispatch retry loop did not terminate")
        except PaperExecutorRequestError:
            raise
        except ExecutorCommandJournalError as exc:
            self._mark_unknown_best_effort(request.request_id, "journal_or_dispatch_failure")
            raise PaperExecutorRequestError(
                "command_outcome_unknown",
                "executor command requires broker-first reconciliation",
            ) from exc
        except Exception as exc:
            self._mark_unknown_best_effort(request.request_id, "broker_or_application_failure")
            raise PaperExecutorRequestError(
                "command_outcome_unknown",
                "executor command requires broker-first reconciliation",
            ) from exc

    def _require_mutation_target(self, request: PaperExecutorRequest) -> None:
        target = request.target
        if target is None or (
            target.account_scope_sha256 != self._account_scope_sha256
            or target.policy_sha256 != self._policy_sha256
            or target.authz_policy_sha256 != self._authz_policy_sha256
            or target.run_id != self.run_id
            or target.fence_epoch != self._fence_epoch
        ):
            raise PaperExecutorRequestError(
                "target_mismatch",
                "executor mutation target no longer matches the active run",
            )

    def _start_safe_flatten(self, request: PaperExecutorRequest) -> dict[str, object]:
        self._require_mutation_target(request)
        operation_id = _uuid_hex(
            request.payload["operation_id"],
            label="operation_id",
        )
        if request.request_id != operation_id:
            raise PaperExecutorRequestError(
                "invalid_request_id",
                "safe-flatten request id must equal its operation id",
            )
        if (
            self._run_state is not ExecutorRunState.READY
            or self._journal.recovery_required()
        ):
            raise PaperExecutorRequestError(
                "executor_not_ready",
                "executor mutations are blocked pending recovery",
            )
        active = self._journal.active_safe_flatten()
        if active is not None and active.operation_id != operation_id:
            raise PaperExecutorRequestError(
                "safe_flatten_active",
                "another safe-flatten operation remains active",
            )
        try:
            record, _created = self._journal.start_safe_flatten_and_latch(
                operation_id=operation_id,
                initiated_by_uid=request.peer.uid,
                authz_policy_sha256=self._authz_policy_sha256,
                **self._command_context(),
            )
            self._broker.activate_kill_switch(record.reason_code)
            return {"operation": self._safe_flatten_status(record)}
        except ExecutorCommandJournalError as exc:
            raise PaperExecutorRequestError(
                "command_journal_rejected",
                "safe-flatten identity or journal state was rejected",
            ) from exc

    def _safe_flatten_status(
        self,
        record: ExecutorSafeFlattenRecord,
    ) -> dict[str, object]:
        control = self._journal.read_control_state()
        if (
            not control.kill_switch_active
            or control.generation < record.control_generation
        ):
            raise ExecutorCommandJournalError(
                "safe-flatten status lacks its durable kill switch"
            )
        outcome_unknown: dict[str, object] | None = None
        if record.state is ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN:
            unknown = tuple(
                leg
                for leg in self._journal.safe_flatten_legs(record.operation_id)
                if leg.state is ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN
            )
            if len(unknown) != 1 or record.resume_state is None:
                raise ExecutorCommandJournalError(
                    "safe-flatten ambiguous status is inconsistent"
                )
            leg = unknown[0]
            outcome_unknown = {
                "operation": leg.command_operation,
                "phase": record.resume_state.value,
                "request_id": leg.leg_id,
                "retry_allowed": False,
            }
        return {
            "schema_version": _SAFE_FLATTEN_STATUS_SCHEMA_VERSION,
            "operation_id": record.operation_id,
            "account_scope_sha256": record.account_scope_sha256,
            "state": record.state.value,
            "state_version": record.state_version,
            "terminal": record.terminal,
            "reconciled": record.reconciled,
            "kill_switch_active": True,
            "retry_allowed": False,
            "failure_code": (
                record.last_error_code
                if record.state is ExecutorSafeFlattenState.FAILED_LATCHED
                else None
            ),
            "outcome_unknown": outcome_unknown,
            "started_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def advance_safe_flatten_once(self) -> ExecutorSafeFlattenRecord | None:
        """Advance at most one durable phase and one broker mutation."""

        if self._run_state is not ExecutorRunState.READY:
            return None
        operation = self._journal.active_safe_flatten()
        if operation is None:
            return None
        self._broker.activate_kill_switch(operation.reason_code)
        if operation.state in {
            ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
            ExecutorSafeFlattenState.FAILED_LATCHED,
            ExecutorSafeFlattenState.FLAT_LATCHED,
        }:
            return operation
        if operation.state is ExecutorSafeFlattenState.LATCHED:
            return self._advance_safe_flatten_latched(operation)
        if operation.state is ExecutorSafeFlattenState.CANCELING:
            return self._advance_safe_flatten_canceling(operation)
        if operation.state is ExecutorSafeFlattenState.CANCEL_CONFIRMED:
            return self._advance_safe_flatten_cancel_confirmed(operation)
        if operation.state is ExecutorSafeFlattenState.CLOSING:
            return self._advance_safe_flatten_closing(operation)
        if operation.state is ExecutorSafeFlattenState.FILLS_CONFIRMED:
            return self._advance_safe_flatten_fills_confirmed(operation)
        if operation.state is ExecutorSafeFlattenState.RECONCILING:
            return self._advance_safe_flatten_reconciling(operation)
        raise ExecutorCommandJournalError("safe-flatten state is unsupported")

    def _advance_safe_flatten_latched(
        self,
        operation: ExecutorSafeFlattenRecord,
    ) -> ExecutorSafeFlattenRecord:
        try:
            open_orders = self._broker.list_orders(status="open")
        except Exception:
            return operation
        target = (
            ExecutorSafeFlattenState.CANCELING
            if open_orders
            else ExecutorSafeFlattenState.CANCEL_CONFIRMED
        )
        return self._transition_safe_flatten(
            operation,
            target,
            event_type=(
                "open_orders_observed"
                if open_orders
                else "no_open_orders_observed"
            ),
            metadata={"open_order_count": len(open_orders)},
        )

    def _advance_safe_flatten_canceling(
        self,
        operation: ExecutorSafeFlattenRecord,
    ) -> ExecutorSafeFlattenRecord:
        legs = self._journal.safe_flatten_legs(operation.operation_id)
        rejected = next(
            (
                leg
                for leg in legs
                if leg.kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER
                and leg.state is ExecutorSafeFlattenLegState.REJECTED
            ),
            None,
        )
        if rejected is not None:
            return self._fail_safe_flatten(operation, "cancel_rejected")
        pending = _pending_safe_flatten_leg(
            legs,
            kind=ExecutorSafeFlattenLegKind.CANCEL_ORDER,
        )
        if pending is not None:
            if pending.state is ExecutorSafeFlattenLegState.DISPATCHING:
                return self._block_ambiguous_safe_flatten_leg(
                    pending,
                    reason="dispatch_completion_missing",
                )
            if pending.state is ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN:
                return operation
            try:
                snapshot = self._broker.get_order(
                    order_id=str(pending.target["order_id"])
                )
            except OrderJournalError as exc:
                raise ExecutorCommandJournalError(
                    "canonical order journal is unavailable"
                ) from exc
            except Exception:
                return operation
            if not _snapshot_matches_cancel_leg(pending, snapshot):
                return operation
            status = snapshot.status.lower()
            if status not in TERMINAL_ORDER_STATUSES:
                return operation
            self._journal.verify_safe_flatten_leg(
                leg_id=pending.leg_id,
                verified=True,
                broker_order_id=snapshot.order_id,
                evidence={
                    "broker_order_id": snapshot.order_id,
                    "observed_at": _utc_timestamp(),
                    "observed_status": status,
                    "order_id": str(pending.target["order_id"]),
                },
                **self._command_context(),
            )
            return self._journal.get_safe_flatten(operation.operation_id) or operation

        try:
            open_orders = tuple(
                sorted(
                    self._broker.list_orders(status="open"),
                    key=lambda item: (item.order_id, item.client_order_id, item.symbol),
                )
            )
        except Exception:
            return operation
        if not open_orders:
            return self._transition_safe_flatten(
                operation,
                ExecutorSafeFlattenState.CANCEL_CONFIRMED,
                event_type="cancellations_confirmed",
                metadata={"open_order_count": 0},
            )
        targeted_ids = {
            str(leg.target["order_id"])
            for leg in legs
            if leg.kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER
        }
        snapshot = open_orders[0]
        if snapshot.order_id in targeted_ids:
            return operation
        ordinal = len(legs)
        target = {
            "client_order_id": snapshot.client_order_id or None,
            "order_id": snapshot.order_id,
            "symbol": snapshot.symbol,
        }
        leg_id = _safe_flatten_leg_id(
            operation.operation_id,
            ExecutorSafeFlattenLegKind.CANCEL_ORDER,
            ordinal,
            target,
        )
        leg, _created = self._journal.begin_safe_flatten_leg(
            operation_id=operation.operation_id,
            leg_id=leg_id,
            ordinal=ordinal,
            kind=ExecutorSafeFlattenLegKind.CANCEL_ORDER,
            target=target,
            command_operation="cancel_order",
            command_payload={"order_id": snapshot.order_id, "client_order_id": None},
            **self._command_context(),
        )
        try:
            result = self._broker.cancel_order(
                order_id=snapshot.order_id,
                execution_guard=self._internal_safe_flatten_guard,
            )
        except Exception:
            return self._block_ambiguous_safe_flatten_leg(
                leg,
                reason="broker_cancel_exception",
            )
        if result.status == "cancel_unresolved":
            return self._block_ambiguous_safe_flatten_leg(
                leg,
                reason="cancel_unresolved",
            )
        accepted = (
            result.accepted
            or result.status == "cancel_pending"
            or result.status.startswith("cancel_terminal_")
        )
        self._journal.complete_safe_flatten_leg(
            leg_id=leg.leg_id,
            result=_order_result_to_dict(result),
            accepted=accepted,
            broker_order_id=snapshot.order_id,
            **self._command_context(),
        )
        return self._journal.get_safe_flatten(operation.operation_id) or operation

    def _advance_safe_flatten_cancel_confirmed(
        self,
        operation: ExecutorSafeFlattenRecord,
    ) -> ExecutorSafeFlattenRecord:
        try:
            open_orders = self._broker.list_orders(status="open")
        except Exception:
            return operation
        target = (
            ExecutorSafeFlattenState.CANCELING
            if open_orders
            else ExecutorSafeFlattenState.CLOSING
        )
        return self._transition_safe_flatten(
            operation,
            target,
            event_type=(
                "open_orders_reappeared"
                if open_orders
                else "close_phase_started"
            ),
            metadata={"open_order_count": len(open_orders)},
        )

    def _advance_safe_flatten_closing(
        self,
        operation: ExecutorSafeFlattenRecord,
    ) -> ExecutorSafeFlattenRecord:
        legs = self._journal.safe_flatten_legs(operation.operation_id)
        rejected = next(
            (
                leg
                for leg in legs
                if leg.kind is ExecutorSafeFlattenLegKind.CLOSE_POSITION
                and leg.state is ExecutorSafeFlattenLegState.REJECTED
            ),
            None,
        )
        if rejected is not None:
            return self._fail_safe_flatten(operation, "close_rejected")
        pending = _pending_safe_flatten_leg(
            legs,
            kind=ExecutorSafeFlattenLegKind.CLOSE_POSITION,
        )
        if pending is not None:
            if pending.state is ExecutorSafeFlattenLegState.DISPATCHING:
                return self._block_ambiguous_safe_flatten_leg(
                    pending,
                    reason="dispatch_completion_missing",
                )
            if pending.state is ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN:
                return operation
            try:
                snapshot = self._broker.get_order_by_client_id(
                    str(pending.target["client_order_id"])
                )
            except OrderJournalError as exc:
                raise ExecutorCommandJournalError(
                    "canonical order journal is unavailable"
                ) from exc
            except Exception:
                return operation
            if not _snapshot_matches_close_leg(pending, snapshot):
                return operation
            status = snapshot.status.lower()
            if status not in TERMINAL_ORDER_STATUSES:
                return operation
            filled_quantity = snapshot.filled_quantity
            verified = (
                status == "filled"
                and _same_required_number(
                    filled_quantity,
                    pending.target["quantity"],
                )
            )
            self._journal.verify_safe_flatten_leg(
                leg_id=pending.leg_id,
                verified=verified,
                broker_order_id=snapshot.order_id,
                evidence={
                    "broker_order_id": snapshot.order_id,
                    "client_order_id": snapshot.client_order_id,
                    "filled_quantity": 0.0 if filled_quantity is None else filled_quantity,
                    "observed_at": _utc_timestamp(),
                    "observed_status": status,
                    "quantity": pending.target["quantity"],
                    "side": pending.target["side"],
                    "symbol": pending.target["symbol"],
                },
                **self._command_context(),
            )
            return self._journal.get_safe_flatten(operation.operation_id) or operation

        try:
            open_orders = self._broker.list_orders(status="open")
            if open_orders:
                return self._transition_safe_flatten(
                    operation,
                    ExecutorSafeFlattenState.CANCELING,
                    event_type="open_orders_reappeared",
                    metadata={"open_order_count": len(open_orders)},
                )
            positions = tuple(
                sorted(self._broker.read_positions(), key=lambda item: item.symbol)
            )
        except OrderJournalError as exc:
            raise ExecutorCommandJournalError(
                "canonical order journal is unavailable"
            ) from exc
        except Exception:
            return operation
        if not positions:
            return self._transition_safe_flatten(
                operation,
                ExecutorSafeFlattenState.FILLS_CONFIRMED,
                event_type="positions_flat_observed",
                metadata={"position_count": 0},
            )
        position = positions[0]
        recent = _latest_verified_close_leg(legs, symbol=position.symbol)
        if recent is not None:
            expected_positive = recent.target["side"] == "sell"
            if (position.quantity > 0) is not expected_positive:
                return self._fail_safe_flatten(
                    operation,
                    "exposure_direction_reversed",
                )
            if abs(position.quantity) >= float(recent.target["quantity"]) - _ZERO_QUANTITY_TOLERANCE:
                return operation
        ordinal = len(legs)
        side = "sell" if position.quantity > 0 else "buy"
        quantity = abs(position.quantity)
        client_order_id = _safe_flatten_client_order_id(operation.operation_id, ordinal)
        target = {
            "client_order_id": client_order_id,
            "quantity": quantity,
            "side": side,
            "symbol": position.symbol,
        }
        command_payload = {
            "order": {
                "symbol": position.symbol,
                "side": side,
                "client_order_id": client_order_id,
                "quantity": quantity,
                "notional": None,
                "reference_price": None,
                "order_type": "market",
                "limit_price": None,
                "position_intent": "close",
            }
        }
        leg_id = _safe_flatten_leg_id(
            operation.operation_id,
            ExecutorSafeFlattenLegKind.CLOSE_POSITION,
            ordinal,
            target,
        )
        leg, _created = self._journal.begin_safe_flatten_leg(
            operation_id=operation.operation_id,
            leg_id=leg_id,
            ordinal=ordinal,
            kind=ExecutorSafeFlattenLegKind.CLOSE_POSITION,
            target=target,
            command_operation="submit_order",
            command_payload=command_payload,
            **self._command_context(),
        )
        try:
            result = self._broker.submit_order(
                PaperOrder(
                    symbol=position.symbol,
                    side=side,
                    quantity=quantity,
                    client_order_id=client_order_id,
                    position_intent="close",
                ),
                execution_guard=self._internal_safe_flatten_guard,
            )
        except Exception:
            return self._block_ambiguous_safe_flatten_leg(
                leg,
                reason="broker_submit_exception",
            )
        if result.status == "submit_unresolved":
            return self._block_ambiguous_safe_flatten_leg(
                leg,
                reason="submit_unresolved",
            )
        self._journal.complete_safe_flatten_leg(
            leg_id=leg.leg_id,
            result=_order_result_to_dict(result),
            accepted=result.accepted,
            broker_order_id=None,
            **self._command_context(),
        )
        return self._journal.get_safe_flatten(operation.operation_id) or operation

    def _advance_safe_flatten_fills_confirmed(
        self,
        operation: ExecutorSafeFlattenRecord,
    ) -> ExecutorSafeFlattenRecord:
        try:
            open_orders = self._broker.list_orders(status="open")
            if open_orders:
                return self._transition_safe_flatten(
                    operation,
                    ExecutorSafeFlattenState.CANCELING,
                    event_type="open_orders_reappeared",
                    metadata={"open_order_count": len(open_orders)},
                )
            positions = self._broker.read_positions()
        except Exception:
            return operation
        if positions:
            return self._transition_safe_flatten(
                operation,
                ExecutorSafeFlattenState.CLOSING,
                event_type="positions_reappeared",
                metadata={"position_count": len(positions)},
            )
        return self._transition_safe_flatten(
            operation,
            ExecutorSafeFlattenState.RECONCILING,
            event_type="flat_snapshots_confirmed",
            metadata={"open_order_count": 0, "position_count": 0},
        )

    def _advance_safe_flatten_reconciling(
        self,
        operation: ExecutorSafeFlattenRecord,
    ) -> ExecutorSafeFlattenRecord:
        try:
            open_orders = self._broker.list_orders(status="open")
            if open_orders:
                return self._transition_safe_flatten(
                    operation,
                    ExecutorSafeFlattenState.CANCELING,
                    event_type="open_orders_reappeared",
                    metadata={"open_order_count": len(open_orders)},
                )
            positions = self._broker.read_positions()
            if positions:
                return self._transition_safe_flatten(
                    operation,
                    ExecutorSafeFlattenState.CLOSING,
                    event_type="positions_reappeared",
                    metadata={"position_count": len(positions)},
                )
            attestation = self._broker.reconcile_flat_account_order_journal()
            if not isinstance(attestation, OrderJournalReconciliationAttestation):
                return operation
            final_orders = self._broker.list_orders(status="open")
            final_positions = self._broker.read_positions()
        except OrderJournalError as exc:
            raise ExecutorCommandJournalError(
                "canonical order journal is unavailable"
            ) from exc
        except Exception:
            return operation
        if final_orders:
            return self._transition_safe_flatten(
                operation,
                ExecutorSafeFlattenState.CANCELING,
                event_type="open_orders_reappeared",
                metadata={"open_order_count": len(final_orders)},
            )
        if final_positions:
            return self._transition_safe_flatten(
                operation,
                ExecutorSafeFlattenState.CLOSING,
                event_type="positions_reappeared",
                metadata={"position_count": len(final_positions)},
            )
        return self._transition_safe_flatten(
            operation,
            ExecutorSafeFlattenState.FLAT_LATCHED,
            event_type="flat_account_reconciled",
            metadata={
                "broker_open_order_count": 0,
                "broker_position_count": 0,
                "journal_reconciled": True,
                "journal_record_count": attestation.record_count,
                "journal_max_event_sequence": attestation.max_event_sequence,
                "journal_projection_sha256": attestation.projection_sha256,
                "open_orders_sha256": _snapshot_sha256(
                    [_order_snapshot_to_dict(item) for item in final_orders]
                ),
                "positions_sha256": _snapshot_sha256(
                    [_position_to_dict(item) for item in final_positions]
                ),
            },
        )

    def _transition_safe_flatten(
        self,
        operation: ExecutorSafeFlattenRecord,
        target: ExecutorSafeFlattenState,
        *,
        event_type: str,
        metadata: Mapping[str, object],
    ) -> ExecutorSafeFlattenRecord:
        return self._journal.transition_safe_flatten(
            operation.operation_id,
            target,
            expected_state=operation.state,
            event_type=event_type,
            metadata=metadata,
            **self._command_context(),
        )

    def _fail_safe_flatten(
        self,
        operation: ExecutorSafeFlattenRecord,
        error_code: str,
    ) -> ExecutorSafeFlattenRecord:
        return self._journal.transition_safe_flatten(
            operation.operation_id,
            ExecutorSafeFlattenState.FAILED_LATCHED,
            expected_state=operation.state,
            event_type="workflow_failed_latched",
            metadata={"error_code": error_code},
            error_code=error_code,
            **self._command_context(),
        )

    def _block_ambiguous_safe_flatten_leg(
        self,
        leg: ExecutorSafeFlattenLegRecord,
        *,
        reason: str,
    ) -> ExecutorSafeFlattenRecord:
        try:
            operation = self._journal.mark_safe_flatten_leg_outcome_unknown(
                leg_id=leg.leg_id,
                reason=reason,
                **self._command_context(),
            )
        finally:
            self._block_run_best_effort()
        return operation

    def _internal_safe_flatten_guard(self) -> None:
        if self._run_state is not ExecutorRunState.READY:
            raise PaperExecutorRequestError(
                "executor_not_ready",
                "safe-flatten broker dispatch is not authorized",
            )
        if not bool(getattr(self._authority, "persistent_authority", False)):
            raise PaperExecutorRequestError(
                "authority_lost",
                "executor account authority is no longer active",
            )
        control = self._journal.read_control_state()
        if not control.kill_switch_active:
            raise PaperExecutorRequestError(
                "executor_not_ready",
                "safe-flatten durable kill switch is inactive",
            )

    def _authorize(self, request: PaperExecutorRequest, capability: str) -> None:
        try:
            self._authorization_policy.require(request.peer, capability)
        except ExecutorAuthorizationDenied as exc:
            raise PaperExecutorRequestError(
                "authorization_denied",
                "executor peer is not authorized for this operation",
            ) from exc

    @staticmethod
    def _mutation_capability(request: PaperExecutorRequest) -> str:
        if request.operation == "submit_order":
            order = _validated_submit_payload(request.payload)
            return (
                CAPABILITY_REDUCE
                if order["position_intent"] in {"reduce", "close"}
                else CAPABILITY_OPEN
            )
        if request.operation == "cancel_order":
            return CAPABILITY_CANCEL
        return CAPABILITY_KILL

    def _submit_order(self, request: PaperExecutorRequest) -> PaperOrderResult:
        order_payload = _validated_submit_payload(request.payload)
        position_intent = str(order_payload["position_intent"])
        if position_intent in {"reduce", "close"}:
            risk_context = ExecutorRiskContext(0.0, 0.0, 0.0, 0.0)
        else:
            provider = self._risk_context_provider
            if provider is None:
                return PaperOrderResult(
                    accepted=False,
                    status="risk_rejected",
                    reasons=("trusted_risk_context_unavailable",),
                    dry_run=False,
                )
            risk_context = provider(
                order_payload,
                self._broker.read_account(),
                self._broker.read_positions(),
                self._broker.list_orders(status="open"),
            )
            _validate_risk_context(risk_context)
        order = PaperOrder(
            symbol=str(order_payload["symbol"]),
            side=str(order_payload["side"]),
            quantity=_optional_float(order_payload["quantity"], label="quantity"),
            notional=_optional_float(order_payload["notional"], label="notional"),
            client_order_id=str(order_payload["client_order_id"]),
            estimated_position_weight=risk_context.estimated_position_weight,
            projected_gross_exposure=risk_context.projected_gross_exposure,
            daily_pnl_pct=risk_context.daily_pnl_pct,
            current_drawdown_pct=risk_context.current_drawdown_pct,
            reference_price=_optional_float(
                order_payload["reference_price"],
                label="reference_price",
            ),
            order_type=str(order_payload["order_type"]),
            limit_price=_optional_float(order_payload["limit_price"], label="limit_price"),
            position_intent=position_intent,
        )
        return self._broker.submit_order(
            order,
            execution_guard=lambda: self._deadline_and_authority_guard(request),
        )

    def _cancel_order(self, request: PaperExecutorRequest) -> PaperOrderResult:
        order_id, client_order_id = _validated_order_identifier(request.payload)
        return self._broker.cancel_order(
            client_order_id=client_order_id,
            order_id=order_id,
            execution_guard=lambda: self._deadline_and_authority_guard(request),
        )

    def _latch_kill_switch(self, request: PaperExecutorRequest) -> dict[str, object]:
        _require_fields(request.payload, frozenset({"reason_code"}))
        _reason_code(request.payload["reason_code"])
        self._deadline_and_authority_guard(request)
        completed = self._journal.latch_kill_switch_and_complete(
            request_id=request.request_id,
            **self._command_context(),
        )
        if completed.result is None:
            raise ExecutorCommandJournalError("atomic kill switch command has no terminal result")
        reason = completed.result.get("reason_code")
        self._broker.activate_kill_switch(str(reason) if reason is not None else "durable_kill_switch")
        return completed.result

    def _get_order(self, payload: Mapping[str, Any]) -> PaperOrderSnapshot:
        order_id, client_order_id = _validated_order_identifier(payload)
        if order_id is not None:
            return self._broker.get_order(order_id=order_id)
        assert client_order_id is not None
        return self._broker.get_order_by_client_id(client_order_id)

    def _list_fill_activities(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[PaperFillActivity, ...]:
        _require_fields(payload, frozenset({"after", "until"}))
        after = _datetime(payload["after"], label="after")
        until = _datetime(payload["until"], label="until")
        return self._broker.list_fill_activities(after=after, until=until)

    def _recover_pending_commands(self) -> ExecutorRecoveryReport:
        resolved_recorded = 0
        resolved_broker_order = 0
        resolved_control = 0
        active_flatten = self._journal.active_safe_flatten()
        safe_flatten_leg_ids = (
            frozenset()
            if active_flatten is None
            else frozenset(
                leg.leg_id
                for leg in self._journal.safe_flatten_legs(
                    active_flatten.operation_id
                )
            )
        )
        for record in self._journal.recovery_required():
            if record.state is ExecutorCommandState.RECORDED:
                self._journal.resolve_recorded_not_dispatched(
                    record.request_id,
                    **self._command_context(),
                )
                resolved_recorded += 1
                continue
            if record.state is not ExecutorCommandState.OUTCOME_UNKNOWN:
                continue
            if record.operation == "latch_kill_switch":
                self._journal.resolve_unknown_control_not_applied(
                    record.request_id,
                    **self._command_context(),
                )
                resolved_control += 1
                continue
            if record.operation not in {"submit_order", "cancel_order"}:
                continue
            snapshot = self._broker_order_for_recovery_or_none(record)
            if snapshot is None:
                continue
            if not _snapshot_matches_recovery_command(record, snapshot):
                continue
            if record.operation == "cancel_order" and snapshot.status.lower() not in TERMINAL_ORDER_STATUSES | {
                "pending_cancel"
            }:
                continue
            if record.request_id in safe_flatten_leg_ids:
                self._journal.resolve_safe_flatten_unknown_from_broker_observation(
                    leg_id=record.request_id,
                    client_order_id=snapshot.client_order_id,
                    broker_order_id=snapshot.order_id,
                    observed_status=snapshot.status.lower(),
                    **self._command_context(),
                )
            else:
                self._journal.resolve_unknown_from_broker_observation(
                    record.request_id,
                    client_order_id=snapshot.client_order_id,
                    broker_order_id=snapshot.order_id,
                    observed_status=snapshot.status.lower(),
                    **self._command_context(),
                )
            resolved_broker_order += 1
        return ExecutorRecoveryReport(
            resolved_recorded=resolved_recorded,
            resolved_broker_order=resolved_broker_order,
            resolved_control=resolved_control,
            pending=len(self._journal.recovery_required()),
        )

    def _broker_order_for_recovery(
        self,
        record: ExecutorCommandRecord,
    ) -> PaperOrderSnapshot:
        if record.operation == "submit_order":
            order = _validated_submit_payload(record.payload)
            return self._broker.get_order_by_client_id(str(order["client_order_id"]))
        order_id, client_order_id = _validated_order_identifier(record.payload)
        if order_id is not None:
            return self._broker.get_order(order_id=order_id)
        assert client_order_id is not None
        return self._broker.get_order_by_client_id(client_order_id)

    def _broker_order_for_recovery_or_none(
        self,
        record: ExecutorCommandRecord,
    ) -> PaperOrderSnapshot | None:
        try:
            return self._broker_order_for_recovery(record)
        except OrderJournalError as exc:
            raise ExecutorCommandJournalError(
                "canonical order journal is unavailable"
            ) from exc
        except Exception:
            # A 404, timeout and malformed snapshot are all absence of
            # positive evidence. None authorizes a second POST/DELETE.
            return None

    def _terminal_replay(self, record: ExecutorCommandRecord) -> dict[str, object] | None:
        if record.state is ExecutorCommandState.COMPLETED:
            if record.result is None:
                raise PaperExecutorRequestError(
                    "command_journal_rejected",
                    "executor terminal command result is unavailable",
                )
            return record.result
        if record.state is ExecutorCommandState.REJECTED:
            raise PaperExecutorRequestError(
                record.error_code or "command_journal_rejected",
                record.error_message or "executor command was rejected",
            )
        return None

    def _mark_unknown_best_effort(self, request_id: str, reason: str) -> None:
        try:
            current = self._journal.get(request_id)
            if current is not None and current.state is ExecutorCommandState.DISPATCHING:
                self._journal.mark_outcome_unknown(
                    request_id,
                    reason=reason,
                    **self._command_context(),
                )
                self._block_run_best_effort()
        except ExecutorCommandJournalError:
            return

    def _block_run_best_effort(self) -> None:
        if self._run_state is not ExecutorRunState.READY:
            return
        try:
            self._transition_run(ExecutorRunState.READY, ExecutorRunState.BLOCKED)
        except ExecutorCommandJournalError:
            return

    def _deadline_guard(self, request: PaperExecutorRequest) -> None:
        now = self._monotonic_clock()
        if not math.isfinite(now) or now >= request.deadline_monotonic:
            raise PaperExecutorRequestError("request_expired", "executor request deadline expired")

    def _deadline_and_authority_guard(self, request: PaperExecutorRequest) -> None:
        self._deadline_guard(request)
        if self._run_state is not ExecutorRunState.READY:
            raise PaperExecutorRequestError(
                "executor_not_ready",
                "executor mutations are blocked pending recovery",
            )
        if not bool(getattr(self._authority, "persistent_authority", False)):
            raise PaperExecutorRequestError(
                "authority_lost",
                "executor account authority is no longer active",
            )

    def _require_ready(self) -> None:
        health = self.health()
        if not health.mutations_allowed:
            raise PaperExecutorRequestError(
                "executor_not_ready",
                "executor mutations are blocked pending recovery",
            )

    def _command_context(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "fence_epoch": self._fence_epoch,
            "policy_sha256": self._policy_sha256,
        }

    @staticmethod
    def _validate_mutation_payload(request: PaperExecutorRequest) -> None:
        try:
            if request.operation == "submit_order":
                _validated_submit_payload(request.payload)
            elif request.operation == "cancel_order":
                _validated_order_identifier(request.payload)
            elif request.operation == "start_safe_flatten":
                _require_fields(request.payload, frozenset({"operation_id"}))
                _uuid_hex(request.payload["operation_id"], label="operation_id")
            else:
                _require_fields(request.payload, frozenset({"reason_code"}))
                _reason_code(request.payload["reason_code"])
        except (ValueError, TypeError, KeyError) as exc:
            raise PaperExecutorRequestError(
                "invalid_request",
                "executor request payload is invalid",
            ) from exc

    def _transition_run(
        self,
        expected: ExecutorRunState,
        target: ExecutorRunState,
    ) -> None:
        self._journal.transition_run(
            self.run_id,
            target,
            expected_state=expected,
        )
        self._run_state = target

    def _fail_start_best_effort(self) -> None:
        run_id = self._run_id
        state = self._run_state
        if (
            run_id is None
            or state is None
            or state
            in {
                ExecutorRunState.STOPPED,
                ExecutorRunState.CRASHED,
                ExecutorRunState.FAILED,
            }
        ):
            return
        try:
            self._journal.transition_run(
                run_id,
                ExecutorRunState.FAILED,
                expected_state=state,
                metadata={"reason": "startup_failed"},
            )
            self._run_state = ExecutorRunState.FAILED
        except ExecutorCommandJournalError:
            return

    def _close_authority(self) -> None:
        close = getattr(self._authority, "close", None)
        if callable(close):
            close()


def deterministic_command_id(operation: str, business_key: str) -> str:
    """Return a stable UUID command id without persisting caller-side secrets."""

    clean_operation = _reason_code(operation)
    clean_key = _identifier(business_key, label="business_key")
    namespace = uuid.UUID("f72bbb7a-23f9-4fb8-82d8-13207cb4aee3")
    return uuid.uuid5(namespace, f"{clean_operation}:{clean_key}").hex


def _uuid_hex(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.int == 0 or value != parsed.hex:
        raise ValueError(f"{label} is invalid")
    return parsed.hex


def _pending_safe_flatten_leg(
    legs: tuple[ExecutorSafeFlattenLegRecord, ...],
    *,
    kind: ExecutorSafeFlattenLegKind,
) -> ExecutorSafeFlattenLegRecord | None:
    pending = tuple(
        leg
        for leg in legs
        if leg.kind is kind
        and leg.state
        in {
            ExecutorSafeFlattenLegState.DISPATCHING,
            ExecutorSafeFlattenLegState.ACCEPTED,
            ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN,
        }
    )
    if len(pending) > 1:
        raise ExecutorCommandJournalError(
            "safe-flatten has concurrent broker legs"
        )
    return None if not pending else pending[0]


def _latest_verified_close_leg(
    legs: tuple[ExecutorSafeFlattenLegRecord, ...],
    *,
    symbol: str,
) -> ExecutorSafeFlattenLegRecord | None:
    matching = tuple(
        leg
        for leg in legs
        if leg.kind is ExecutorSafeFlattenLegKind.CLOSE_POSITION
        and leg.state is ExecutorSafeFlattenLegState.VERIFIED
        and leg.target["symbol"] == symbol
    )
    return None if not matching else max(matching, key=lambda item: item.ordinal)


def _snapshot_matches_cancel_leg(
    leg: ExecutorSafeFlattenLegRecord,
    snapshot: PaperOrderSnapshot,
) -> bool:
    expected_client_id = leg.target["client_order_id"]
    return (
        snapshot.order_id == leg.target["order_id"]
        and snapshot.symbol == leg.target["symbol"]
        and (
            expected_client_id is None
            or snapshot.client_order_id == expected_client_id
        )
    )


def _snapshot_matches_close_leg(
    leg: ExecutorSafeFlattenLegRecord,
    snapshot: PaperOrderSnapshot,
) -> bool:
    expected_tif = "gtc" if "/" in str(leg.target["symbol"]) else "day"
    return (
        bool(snapshot.order_id)
        and snapshot.client_order_id == leg.target["client_order_id"]
        and snapshot.symbol == leg.target["symbol"]
        and snapshot.side.lower() == leg.target["side"]
        and snapshot.order_type.lower() == "market"
        and snapshot.time_in_force.lower() == expected_tif
        and _same_required_number(snapshot.quantity, leg.target["quantity"])
        and snapshot.notional is None
        and snapshot.limit_price is None
    )


def _safe_flatten_leg_id(
    operation_id: str,
    kind: ExecutorSafeFlattenLegKind,
    ordinal: int,
    target: Mapping[str, object],
) -> str:
    target_json = json.dumps(
        dict(target),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    target_sha256 = hashlib.sha256(target_json.encode("utf-8")).hexdigest()
    return uuid.uuid5(
        _SAFE_FLATTEN_NAMESPACE,
        f"{operation_id}:{kind.value}:{ordinal}:{target_sha256}",
    ).hex


def _safe_flatten_client_order_id(operation_id: str, ordinal: int) -> str:
    return f"sf-{operation_id[:24]}-{ordinal:06d}"


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _snapshot_sha256(value: list[dict[str, object]]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(b"paper-safe-flatten-snapshot-v1\0" + payload).hexdigest()


def _expected_mutation_request_id(request: PaperExecutorRequest) -> str:
    if request.operation == "submit_order":
        order = _validated_submit_payload(request.payload)
        business_key = f"client_order_id:{order['client_order_id']}"
    elif request.operation == "cancel_order":
        order_id, client_order_id = _validated_order_identifier(request.payload)
        business_key = f"order_id:{order_id}" if order_id is not None else f"client_order_id:{client_order_id}"
    elif request.operation == "latch_kill_switch":
        _require_fields(request.payload, frozenset({"reason_code"}))
        business_key = f"reason_code:{_reason_code(request.payload['reason_code'])}"
    elif request.operation == "start_safe_flatten":
        _require_fields(request.payload, frozenset({"operation_id"}))
        return _uuid_hex(request.payload["operation_id"], label="operation_id")
    else:  # pragma: no cover - caller gates the closed operation set
        raise ValueError("operation has no deterministic mutation identity")
    return deterministic_command_id(request.operation, business_key)


def _validated_submit_payload(payload: Mapping[str, Any]) -> dict[str, object]:
    _require_fields(payload, frozenset({"order"}))
    raw = payload["order"]
    if not isinstance(raw, Mapping):
        raise ValueError("order must be an object")
    fields = frozenset(
        {
            "symbol",
            "side",
            "client_order_id",
            "quantity",
            "notional",
            "reference_price",
            "order_type",
            "limit_price",
            "position_intent",
        }
    )
    _require_fields(raw, fields)
    result: dict[str, object] = {
        "symbol": _symbol(raw["symbol"]),
        "side": _enum(raw["side"], {"buy", "sell"}, label="side"),
        "client_order_id": _identifier(raw["client_order_id"], label="client_order_id"),
        "quantity": _optional_positive(raw["quantity"], label="quantity"),
        "notional": _optional_positive(raw["notional"], label="notional"),
        "reference_price": _optional_positive(raw["reference_price"], label="reference_price"),
        "order_type": _enum(raw["order_type"], {"market", "limit"}, label="order_type"),
        "limit_price": _optional_positive(raw["limit_price"], label="limit_price"),
        "position_intent": _enum(
            raw["position_intent"],
            {"open", "increase", "reduce", "close"},
            label="position_intent",
        ),
    }
    if (result["quantity"] is None) == (result["notional"] is None):
        raise ValueError("exactly one of quantity/notional is required")
    if result["order_type"] == "limit" and result["limit_price"] is None:
        raise ValueError("limit price is required")
    if result["order_type"] == "market" and result["limit_price"] is not None:
        raise ValueError("market order cannot contain limit price")
    if result["position_intent"] in {"open", "increase"} and result["reference_price"] is None:
        raise ValueError("opening order requires reference price")
    return result


def _validated_order_identifier(
    payload: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    _require_fields(payload, frozenset({"order_id", "client_order_id"}))
    order_id = payload["order_id"]
    client_order_id = payload["client_order_id"]
    if (order_id is None) == (client_order_id is None):
        raise ValueError("exactly one order identifier is required")
    return (
        None if order_id is None else _identifier(order_id, label="order_id"),
        (None if client_order_id is None else _identifier(client_order_id, label="client_order_id")),
    )


def _snapshot_matches_recovery_command(
    record: ExecutorCommandRecord,
    snapshot: PaperOrderSnapshot,
) -> bool:
    if record.operation == "submit_order":
        try:
            order = _validated_submit_payload(record.payload)
        except (KeyError, TypeError, ValueError):
            return False
        symbol = str(order["symbol"])
        expected_tif = "gtc" if "/" in symbol else "day"
        size_matches = (
            _same_required_number(snapshot.quantity, order["quantity"])
            if order["quantity"] is not None
            else _same_required_number(snapshot.notional, order["notional"])
        )
        return (
            snapshot.client_order_id == order["client_order_id"]
            and snapshot.symbol.upper() == symbol
            and snapshot.side.lower() == order["side"]
            and snapshot.order_type.lower() == order["order_type"]
            and snapshot.time_in_force.lower() == expected_tif
            and size_matches
            and _same_optional_number(snapshot.limit_price, order["limit_price"])
        )
    if record.operation == "cancel_order":
        try:
            order_id, client_order_id = _validated_order_identifier(record.payload)
        except (KeyError, TypeError, ValueError):
            return False
        return (order_id is None or snapshot.order_id == order_id) and (
            client_order_id is None or snapshot.client_order_id == client_order_id
        )
    return False


def _same_optional_number(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    left_number = float(left)
    right_number = float(right)
    return math.isfinite(left_number) and math.isfinite(right_number) and abs(left_number - right_number) <= 1e-9


def _same_required_number(left: object, right: object) -> bool:
    return left is not None and right is not None and _same_optional_number(left, right)


def _health_to_dict(value: ExecutorHealth) -> dict[str, object]:
    return {
        "status": value.status,
        "mutations_allowed": value.mutations_allowed,
        "opening_orders_allowed": value.opening_orders_allowed,
        "capability_mode": value.capability_mode,
        "account_scope_sha256": value.account_scope_sha256,
        "fence_epoch": value.fence_epoch,
        "policy_sha256": value.policy_sha256,
        "authz_policy_sha256": value.authz_policy_sha256,
        "run_id": value.run_id,
        "pending_recovery": value.pending_recovery,
        "kill_switch_active": value.kill_switch_active,
    }


def _account_to_dict(value: PaperAccount) -> dict[str, object]:
    return {
        "account_id": value.account_id,
        "status": value.status,
        "cash": _finite(value.cash, label="cash"),
        "equity": _finite(value.equity, label="equity"),
        "buying_power": _finite(value.buying_power, label="buying_power"),
        "last_equity": _finite(value.last_equity, label="last_equity"),
    }


def _position_to_dict(value: PaperPosition) -> dict[str, object]:
    return {
        "symbol": _symbol(value.symbol),
        "quantity": _finite(value.quantity, label="quantity"),
        "market_value": _finite(value.market_value, label="market_value"),
        "avg_entry_price": _finite(value.avg_entry_price, label="avg_entry_price"),
        "current_price": _finite(value.current_price, label="current_price"),
        "unrealized_pl": _optional_float(value.unrealized_pl, label="unrealized_pl"),
        "unrealized_plpc": _optional_float(value.unrealized_plpc, label="unrealized_plpc"),
    }


def _order_snapshot_to_dict(value: PaperOrderSnapshot) -> dict[str, object]:
    return {
        "order_id": value.order_id,
        "client_order_id": value.client_order_id,
        "symbol": value.symbol,
        "side": value.side,
        "order_type": value.order_type,
        "time_in_force": value.time_in_force,
        "status": value.status,
        "notional": value.notional,
        "quantity": value.quantity,
        "filled_quantity": value.filled_quantity,
        "filled_avg_price": value.filled_avg_price,
        "submitted_at": value.submitted_at,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "expires_at": value.expires_at,
        "stop_price": value.stop_price,
        "limit_price": value.limit_price,
        "filled_at": value.filled_at,
        "realized_pnl": value.realized_pnl,
    }


def _fill_to_dict(value: PaperFillActivity) -> dict[str, object]:
    return {
        "activity_id": value.activity_id,
        "order_id": value.order_id,
        "symbol": value.symbol,
        "side": value.side,
        "quantity": value.quantity,
        "price": value.price,
        "transaction_time": value.transaction_time,
        "cumulative_quantity": value.cumulative_quantity,
        "leaves_quantity": value.leaves_quantity,
        "activity_type": value.activity_type,
        "order_status": value.order_status,
    }


def _order_result_to_dict(value: PaperOrderResult) -> dict[str, object]:
    return {
        "accepted": bool(value.accepted),
        "status": _reason_code(value.status),
        "reasons": [_reason_code(reason) for reason in value.reasons],
        "dry_run": bool(value.dry_run),
    }


def _validate_risk_context(value: ExecutorRiskContext) -> None:
    if not isinstance(value, ExecutorRiskContext):
        raise ValueError("risk context provider returned the wrong type")
    values = (
        value.estimated_position_weight,
        value.projected_gross_exposure,
        value.daily_pnl_pct,
        value.current_drawdown_pct,
    )
    if not all(type(item) in {int, float} and math.isfinite(float(item)) for item in values):
        raise ValueError("risk context is non-finite")


def _require_fields(payload: Mapping[str, Any], expected: frozenset[str]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("request fields are invalid")


def _symbol(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("symbol is invalid")
    clean = value.strip().upper()
    if _SYMBOL_RE.fullmatch(clean) is None:
        raise ValueError("symbol is invalid")
    return clean


def _identifier(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} is invalid")
    clean = value.strip()
    if _IDENTIFIER_RE.fullmatch(clean) is None:
        raise ValueError(f"{label} is invalid")
    return clean


def _reason_code(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value) is None:
        raise ValueError("reason code is invalid")
    return value


def _enum(value: Any, allowed: set[str], *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} is invalid")
    clean = value.strip().lower()
    if clean not in allowed:
        raise ValueError(f"{label} is invalid")
    return clean


def _optional_positive(value: Any, *, label: str) -> float | None:
    if value is None:
        return None
    number = _finite(value, label=label)
    if number <= 0:
        raise ValueError(f"{label} must be positive")
    return number


def _optional_float(value: Any, *, label: str) -> float | None:
    return None if value is None else _finite(value, label=label)


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _datetime(value: Any, *, label: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{label} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _sha256(value: Any, *, label: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise ValueError(f"{label} is invalid")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if value.lower() != value:
        raise ValueError(f"{label} is invalid")
    return value
