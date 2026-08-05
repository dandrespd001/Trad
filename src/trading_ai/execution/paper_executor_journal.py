"""Durable authority ledger for the single Alpaca paper executor.

This ledger complements the order-intent journal.  It binds every mutating IPC
command to an account scope, policy, executor run, fencing epoch and canonical
payload before dispatch.  Terminal outcomes are hashed and immutable.  A new
run cannot become READY while a prior command is recorded, dispatching, or
outcome-unknown; recovery is explicit and evidence-bearing.

The ledger never retries a broker action and never treats a local fencing epoch
as broker-enforced.  It is a same-host audit/control boundary only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from trading_ai.execution.order_journal import intent_fingerprint

COMMAND_SCHEMA_VERSION = 4
COMMAND_ENVELOPE_SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
MAX_COMMAND_PAYLOAD_BYTES = 256 * 1024
# Leave deterministic room for the IPC response envelope and server metadata.
# Persisting an outcome which cannot subsequently be returned would turn a
# known broker result into a client-side ambiguity.
MAX_COMMAND_OUTCOME_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_ITEMS = 20_000
MAX_RECOVERY_EVIDENCE_AGE_SECONDS = 120.0
MAX_RECOVERY_EVIDENCE_FUTURE_SKEW_SECONDS = 5.0
MAX_SQLITE_INTEGER = (1 << 63) - 1

_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_REASON_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_ACTIVE_RUN_STATES = frozenset({"starting", "recovering", "ready", "blocked", "draining"})
_TERMINAL_COMMAND_STATES = frozenset({"completed", "rejected"})
_KNOWN_BROKER_ORDER_STATUSES = frozenset(
    {
        "accepted",
        "accepted_for_bidding",
        "calculated",
        "canceled",
        "done_for_day",
        "expired",
        "filled",
        "new",
        "partially_filled",
        "pending_cancel",
        "pending_new",
        "pending_replace",
        "rejected",
        "stopped",
        "suspended",
    }
)
_CANCEL_RECOVERY_TERMINAL_STATUSES = frozenset(
    {"canceled", "expired", "filled", "pending_cancel", "rejected"}
)
_SAFE_ERROR_MESSAGES = {
    "command_not_dispatched": "executor command was not dispatched by the previous run",
    "predispatch_retry_exhausted": "executor command was not dispatched and its bounded retry was exhausted",
    "policy_rejected": "executor policy rejected the command",
    "recovery_rejected": "executor reconciliation proved that no action was accepted",
}


class ExecutorCommandJournalError(RuntimeError):
    """Base error; account mutations must stop when raised."""


class ExecutorCommandStorageError(ExecutorCommandJournalError):
    """The authority ledger cannot be trusted or updated."""


class ExecutorCommandSchemaError(ExecutorCommandStorageError):
    """The durable schema, history, or append-only controls are invalid."""


class ExecutorCommandCollisionError(ExecutorCommandJournalError):
    """A durable identity was reused for different content."""


class ExecutorCommandTransitionError(ExecutorCommandJournalError):
    """A command or run transition is stale or forbidden."""


class ExecutorRunNotReadyError(ExecutorCommandTransitionError):
    """The executor run is not authorized to accept mutations."""


class ExecutorCommandState(StrEnum):
    RECORDED = "recorded"
    DISPATCHING = "dispatching"
    COMPLETED = "completed"
    REJECTED = "rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ExecutorRunState(StrEnum):
    STARTING = "starting"
    RECOVERING = "recovering"
    READY = "ready"
    BLOCKED = "blocked"
    DRAINING = "draining"
    STOPPED = "stopped"
    CRASHED = "crashed"
    FAILED = "failed"


class ExecutorSafeFlattenState(StrEnum):
    """Durable, executor-owned emergency-flatten workflow states."""

    LATCHED = "latched"
    CANCELING = "canceling"
    CANCEL_CONFIRMED = "cancel_confirmed"
    CLOSING = "closing"
    FILLS_CONFIRMED = "fills_confirmed"
    RECONCILING = "reconciling"
    BLOCKED_OUTCOME_UNKNOWN = "blocked_outcome_unknown"
    FAILED_LATCHED = "failed_latched"
    FLAT_LATCHED = "flat_latched"


class ExecutorSafeFlattenLegKind(StrEnum):
    CANCEL_ORDER = "cancel_order"
    CLOSE_POSITION = "close_position"


class ExecutorSafeFlattenLegState(StrEnum):
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    VERIFIED = "verified"
    REJECTED = "rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"


_COMMAND_TRANSITIONS: dict[ExecutorCommandState, frozenset[ExecutorCommandState]] = {
    ExecutorCommandState.RECORDED: frozenset(
        {ExecutorCommandState.DISPATCHING, ExecutorCommandState.REJECTED}
    ),
    ExecutorCommandState.DISPATCHING: frozenset(
        {
            ExecutorCommandState.RECORDED,
            ExecutorCommandState.COMPLETED,
            ExecutorCommandState.REJECTED,
            ExecutorCommandState.OUTCOME_UNKNOWN,
        }
    ),
    ExecutorCommandState.OUTCOME_UNKNOWN: frozenset(
        {ExecutorCommandState.COMPLETED, ExecutorCommandState.REJECTED}
    ),
    ExecutorCommandState.COMPLETED: frozenset(),
    ExecutorCommandState.REJECTED: frozenset(),
}

_RUN_TRANSITIONS: dict[ExecutorRunState, frozenset[ExecutorRunState]] = {
    ExecutorRunState.STARTING: frozenset(
        {ExecutorRunState.RECOVERING, ExecutorRunState.FAILED, ExecutorRunState.CRASHED}
    ),
    ExecutorRunState.RECOVERING: frozenset(
        {
            ExecutorRunState.READY,
            ExecutorRunState.BLOCKED,
            ExecutorRunState.DRAINING,
            ExecutorRunState.FAILED,
            ExecutorRunState.CRASHED,
        }
    ),
    ExecutorRunState.READY: frozenset(
        {
            ExecutorRunState.BLOCKED,
            ExecutorRunState.DRAINING,
            ExecutorRunState.FAILED,
            ExecutorRunState.CRASHED,
        }
    ),
    ExecutorRunState.BLOCKED: frozenset(
        {
            ExecutorRunState.RECOVERING,
            ExecutorRunState.DRAINING,
            ExecutorRunState.FAILED,
            ExecutorRunState.CRASHED,
        }
    ),
    ExecutorRunState.DRAINING: frozenset(
        {ExecutorRunState.STOPPED, ExecutorRunState.FAILED, ExecutorRunState.CRASHED}
    ),
    ExecutorRunState.STOPPED: frozenset(),
    ExecutorRunState.CRASHED: frozenset(),
    ExecutorRunState.FAILED: frozenset(),
}

_SAFE_FLATTEN_TRANSITIONS: dict[
    ExecutorSafeFlattenState,
    frozenset[ExecutorSafeFlattenState],
] = {
    ExecutorSafeFlattenState.LATCHED: frozenset(
        {
            ExecutorSafeFlattenState.LATCHED,
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.CANCEL_CONFIRMED,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        }
    ),
    ExecutorSafeFlattenState.CANCELING: frozenset(
        {
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.CANCEL_CONFIRMED,
            ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        }
    ),
    ExecutorSafeFlattenState.CANCEL_CONFIRMED: frozenset(
        {
            ExecutorSafeFlattenState.CANCEL_CONFIRMED,
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.CLOSING,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        }
    ),
    ExecutorSafeFlattenState.CLOSING: frozenset(
        {
            ExecutorSafeFlattenState.CLOSING,
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.FILLS_CONFIRMED,
            ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        }
    ),
    ExecutorSafeFlattenState.FILLS_CONFIRMED: frozenset(
        {
            ExecutorSafeFlattenState.FILLS_CONFIRMED,
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.CLOSING,
            ExecutorSafeFlattenState.RECONCILING,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        }
    ),
    ExecutorSafeFlattenState.RECONCILING: frozenset(
        {
            ExecutorSafeFlattenState.RECONCILING,
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.CLOSING,
            ExecutorSafeFlattenState.FLAT_LATCHED,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        }
    ),
    ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN: frozenset(
        {
            ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.CLOSING,
            ExecutorSafeFlattenState.FAILED_LATCHED,
        }
    ),
    ExecutorSafeFlattenState.FAILED_LATCHED: frozenset(),
    ExecutorSafeFlattenState.FLAT_LATCHED: frozenset(),
}

_SAFE_FLATTEN_LEG_TRANSITIONS: dict[
    ExecutorSafeFlattenLegState,
    frozenset[ExecutorSafeFlattenLegState],
] = {
    ExecutorSafeFlattenLegState.DISPATCHING: frozenset(
        {
            ExecutorSafeFlattenLegState.ACCEPTED,
            ExecutorSafeFlattenLegState.REJECTED,
            ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN,
        }
    ),
    ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN: frozenset(
        {
            ExecutorSafeFlattenLegState.ACCEPTED,
            ExecutorSafeFlattenLegState.REJECTED,
        }
    ),
    ExecutorSafeFlattenLegState.ACCEPTED: frozenset(
        {
            ExecutorSafeFlattenLegState.VERIFIED,
            ExecutorSafeFlattenLegState.REJECTED,
        }
    ),
    ExecutorSafeFlattenLegState.VERIFIED: frozenset(),
    ExecutorSafeFlattenLegState.REJECTED: frozenset(),
}


@dataclass(frozen=True)
class ExecutorLedgerIdentity:
    device: int
    inode: int
    ledger_id: str
    account_scope_sha256: str
    created_at: str


@dataclass(frozen=True)
class ExecutorRunRecord:
    run_id: str
    account_scope_sha256: str
    fence_epoch: int
    policy_sha256: str
    pid: int
    state: ExecutorRunState
    started_at: str
    updated_at: str
    ended_at: str | None


@dataclass(frozen=True)
class ExecutorRunEvent:
    sequence: int
    run_id: str
    from_state: ExecutorRunState | None
    to_state: ExecutorRunState
    metadata: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class ExecutorCommandRecord:
    request_id: str
    operation: str
    fingerprint_sha256: str
    payload: dict[str, object]
    state: ExecutorCommandState
    outcome: dict[str, object] | None
    outcome_sha256: str | None
    fence_epoch: int
    policy_sha256: str
    created_run_id: str
    last_run_id: str
    created_at: str
    updated_at: str

    @property
    def result(self) -> dict[str, object] | None:
        if self.outcome is None or not self.outcome.get("ok"):
            return None
        payload = self.outcome.get("payload")
        return payload if isinstance(payload, dict) else None

    @property
    def error_code(self) -> str | None:
        if self.outcome is None or self.outcome.get("ok") is not False:
            return None
        error = self.outcome.get("error")
        return str(error.get("code")) if isinstance(error, dict) else None

    @property
    def error_message(self) -> str | None:
        if self.outcome is None or self.outcome.get("ok") is not False:
            return None
        error = self.outcome.get("error")
        return str(error.get("message")) if isinstance(error, dict) else None


@dataclass(frozen=True)
class ExecutorCommandEvent:
    sequence: int
    request_id: str
    run_id: str
    event_type: str
    from_state: ExecutorCommandState | None
    to_state: ExecutorCommandState
    metadata: dict[str, object]
    outcome_sha256: str | None
    created_at: str


@dataclass(frozen=True)
class ExecutorControlState:
    kill_switch_active: bool
    reason_code: str | None
    generation: int
    last_request_id: str | None
    updated_at: str


@dataclass(frozen=True)
class ExecutorSafeFlattenRecord:
    operation_id: str
    initiating_request_id: str
    account_scope_sha256: str
    reason_code: str
    control_generation: int
    state: ExecutorSafeFlattenState
    state_version: int
    resume_state: ExecutorSafeFlattenState | None
    last_error_code: str | None
    initiated_by_uid: int
    created_run_id: str
    last_run_id: str
    created_fence_epoch: int
    last_fence_epoch: int
    policy_sha256: str
    last_policy_sha256: str
    authz_policy_sha256: str
    created_at: str
    updated_at: str
    terminal_at: str | None

    @property
    def terminal(self) -> bool:
        return self.state in {
            ExecutorSafeFlattenState.FAILED_LATCHED,
            ExecutorSafeFlattenState.FLAT_LATCHED,
        }

    @property
    def reconciled(self) -> bool:
        return self.state is ExecutorSafeFlattenState.FLAT_LATCHED


@dataclass(frozen=True)
class ExecutorSafeFlattenEvent:
    sequence: int
    operation_id: str
    run_id: str
    from_state: ExecutorSafeFlattenState | None
    to_state: ExecutorSafeFlattenState
    state_version: int
    event_type: str
    metadata: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class ExecutorSafeFlattenLegRecord:
    leg_id: str
    operation_id: str
    ordinal: int
    kind: ExecutorSafeFlattenLegKind
    target: dict[str, object]
    target_sha256: str
    command_operation: str
    state: ExecutorSafeFlattenLegState
    broker_order_id: str | None
    created_run_id: str
    last_run_id: str
    created_at: str
    updated_at: str
    verified_at: str | None


@dataclass(frozen=True)
class ExecutorSafeFlattenLegEvent:
    sequence: int
    leg_id: str
    run_id: str
    from_state: ExecutorSafeFlattenLegState | None
    to_state: ExecutorSafeFlattenLegState
    event_type: str
    metadata: dict[str, object]
    created_at: str


_UNSET = object()


def canonical_command_json(operation: str, payload: Mapping[str, object]) -> str:
    clean_operation = _validated_operation(operation)
    normalized = _normalize_json_object(payload, location="payload")
    return _canonical_json(
        {
            "command_schema_version": COMMAND_ENVELOPE_SCHEMA_VERSION,
            "operation": clean_operation,
            "payload": normalized,
        },
        max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
        label="executor command",
    )


def command_fingerprint(operation: str, payload: Mapping[str, object]) -> str:
    return _sha256_text(canonical_command_json(operation, payload))


class DurableExecutorCommandJournal:
    """Account-scoped SQLite ledger with run and command state machines."""

    def __init__(
        self,
        path: str | Path,
        *,
        account_scope_sha256: str,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        raw_path = str(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise ExecutorCommandStorageError("executor ledger must use a filesystem path")
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.account_scope_sha256 = _validated_sha256(
            account_scope_sha256,
            label="account_scope_sha256",
        )
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._initialize()
        self._identity = self._read_identity_without_pin()

    @property
    def storage_identity(self) -> ExecutorLedgerIdentity:
        identity = self._read_identity_without_pin()
        if identity != self._identity:
            raise ExecutorCommandStorageError("executor ledger identity changed")
        return identity

    def start_run(
        self,
        *,
        run_id: str,
        policy_sha256: str,
        pid: int | None = None,
    ) -> ExecutorRunRecord:
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        clean_pid = os.getpid() if pid is None else _validated_pid(pid)
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM executor_runs WHERE run_id = ?",
                (clean_run_id,),
            ).fetchone() is not None:
                raise ExecutorCommandCollisionError("executor run id already exists")
            maximum = connection.execute(
                "SELECT MAX(fence_epoch) FROM executor_runs"
            ).fetchone()[0]
            if maximum is None:
                clean_epoch = 1
            elif type(maximum) is not int or maximum < 1:
                raise ExecutorCommandSchemaError("executor fence history is invalid")
            elif maximum >= MAX_SQLITE_INTEGER:
                raise ExecutorCommandTransitionError("executor fence epoch is exhausted")
            else:
                clean_epoch = maximum + 1
            timestamp = self._timestamp()
            connection.execute(
                """
                INSERT INTO executor_runs (
                    run_id, account_scope_sha256, fence_epoch, policy_sha256,
                    pid, state, started_at, updated_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    clean_run_id,
                    self.account_scope_sha256,
                    clean_epoch,
                    clean_policy,
                    clean_pid,
                    ExecutorRunState.STARTING.value,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_run_event(
                connection,
                run_id=clean_run_id,
                from_state=None,
                to_state=ExecutorRunState.STARTING,
                metadata_json=_canonical_json({}, max_bytes=MAX_COMMAND_PAYLOAD_BYTES, label="run metadata"),
                timestamp=timestamp,
            )

            predecessor_rows = connection.execute(
                """
                SELECT * FROM executor_runs
                WHERE run_id <> ? AND state IN ('starting','recovering','ready','blocked','draining')
                ORDER BY fence_epoch
                """,
                (clean_run_id,),
            ).fetchall()
            for row in predecessor_rows:
                predecessor = self._run_from_row(row)
                self._validate_run_history(connection, predecessor)
                connection.execute(
                    """
                    UPDATE executor_runs
                    SET state = ?, updated_at = ?, ended_at = ?
                    WHERE run_id = ? AND state = ?
                    """,
                    (
                        ExecutorRunState.CRASHED.value,
                        timestamp,
                        timestamp,
                        predecessor.run_id,
                        predecessor.state.value,
                    ),
                )
                self._append_run_event(
                    connection,
                    run_id=predecessor.run_id,
                    from_state=predecessor.state,
                    to_state=ExecutorRunState.CRASHED,
                    metadata_json=_canonical_json(
                        {"successor_run_id": clean_run_id},
                        max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                        label="run metadata",
                    ),
                    timestamp=timestamp,
                )
                self._validate_run_history(
                    connection,
                    self._run_by_id(connection, predecessor.run_id),
                )

            dispatching_rows = connection.execute(
                "SELECT * FROM executor_commands WHERE state = 'dispatching'"
            ).fetchall()
            for row in dispatching_rows:
                command = self._command_from_row(row)
                self._validate_command_history(connection, command)
                leg_row = connection.execute(
                    "SELECT * FROM executor_safe_flatten_legs WHERE leg_id = ?",
                    (command.request_id,),
                ).fetchone()
                leg = None
                operation = None
                if leg_row is not None:
                    leg = self._safe_flatten_leg_from_row(leg_row)
                    operation = self._safe_flatten_by_id(
                        connection,
                        leg.operation_id,
                    )
                    self._validate_safe_flatten_leg_history(connection, leg)
                    self._validate_safe_flatten_operation_history(
                        connection,
                        operation,
                    )
                cursor = connection.execute(
                    """
                    UPDATE executor_commands
                    SET state = ?, last_run_id = ?, updated_at = ?
                    WHERE request_id = ? AND state = ?
                    """,
                    (
                        ExecutorCommandState.OUTCOME_UNKNOWN.value,
                        clean_run_id,
                        timestamp,
                        command.request_id,
                        ExecutorCommandState.DISPATCHING.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ExecutorCommandStorageError(
                        "crashed executor command changed concurrently"
                    )
                self._append_command_event(
                    connection,
                    request_id=command.request_id,
                    run_id=clean_run_id,
                    event_type="predecessor_crashed",
                    from_state=ExecutorCommandState.DISPATCHING,
                    to_state=ExecutorCommandState.OUTCOME_UNKNOWN,
                    metadata_json=_canonical_json(
                        {"previous_run_id": command.last_run_id},
                        max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                        label="command metadata",
                    ),
                    outcome_sha256=None,
                    timestamp=timestamp,
                )
                if leg is not None:
                    if leg.state is not ExecutorSafeFlattenLegState.DISPATCHING:
                        raise ExecutorCommandSchemaError(
                            "crashed safe-flatten command and leg states disagree"
                        )
                    assert operation is not None
                    if operation.state not in {
                        ExecutorSafeFlattenState.CANCELING,
                        ExecutorSafeFlattenState.CLOSING,
                    }:
                        raise ExecutorCommandSchemaError(
                            "crashed safe-flatten leg has no resumable phase"
                        )
                    cursor = connection.execute(
                        """
                        UPDATE executor_safe_flatten_legs
                        SET state = 'outcome_unknown', last_run_id = ?, updated_at = ?
                        WHERE leg_id = ? AND state = 'dispatching'
                        """,
                        (clean_run_id, timestamp, leg.leg_id),
                    )
                    if cursor.rowcount != 1:
                        raise ExecutorCommandStorageError(
                            "crashed safe-flatten leg changed concurrently"
                        )
                    self._append_safe_flatten_leg_event(
                        connection,
                        leg_id=leg.leg_id,
                        run_id=clean_run_id,
                        event_type="predecessor_crashed",
                        from_state=ExecutorSafeFlattenLegState.DISPATCHING,
                        to_state=ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN,
                        metadata={"previous_run_id": leg.last_run_id},
                        timestamp=timestamp,
                    )
                    version = operation.state_version + 1
                    cursor = connection.execute(
                        """
                        UPDATE executor_safe_flatten_operations
                        SET state = 'blocked_outcome_unknown', state_version = ?,
                            resume_state = ?,
                            last_error_code = 'command_outcome_unknown',
                            last_run_id = ?, last_fence_epoch = ?,
                            last_policy_sha256 = ?, updated_at = ?
                        WHERE operation_id = ? AND state = ? AND state_version = ?
                        """,
                        (
                            version,
                            operation.state.value,
                            clean_run_id,
                            clean_epoch,
                            clean_policy,
                            timestamp,
                            operation.operation_id,
                            operation.state.value,
                            operation.state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ExecutorCommandStorageError(
                            "crashed safe-flatten operation changed concurrently"
                        )
                    self._append_safe_flatten_operation_event(
                        connection,
                        operation_id=operation.operation_id,
                        run_id=clean_run_id,
                        event_type="predecessor_crashed",
                        from_state=operation.state,
                        to_state=ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
                        state_version=version,
                        metadata={
                            "leg_id": leg.leg_id,
                            "operation": command.operation,
                            "phase": operation.state.value,
                            "request_id": command.request_id,
                            "retry_allowed": False,
                        },
                        timestamp=timestamp,
                    )
                    self._validate_safe_flatten_leg_history(
                        connection,
                        self._safe_flatten_leg_by_id(connection, leg.leg_id),
                    )
                    self._validate_safe_flatten_operation_history(
                        connection,
                        self._safe_flatten_by_id(connection, operation.operation_id),
                    )
                self._validate_command_history(
                    connection,
                    self._command_by_id(connection, command.request_id),
                )

            created = self._run_by_id(connection, clean_run_id)
            self._validate_active_run_uniqueness(connection)
            self._validate_run_history(connection, created)
            connection.commit()
            return created
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError("failed to start executor run") from exc
        finally:
            connection.close()

    def transition_run(
        self,
        run_id: str,
        state: ExecutorRunState | str,
        *,
        expected_state: ExecutorRunState | str,
        metadata: Mapping[str, object] | None = None,
    ) -> ExecutorRunRecord:
        clean_run_id = _validated_request_id(run_id, label="run_id")
        target = _coerce_run_state(state)
        expected = _coerce_run_state(expected_state)
        metadata_json = _canonical_json(
            _normalize_json_object(metadata or {}, location="run metadata"),
            max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            label="run metadata",
        )
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._run_by_id(connection, clean_run_id)
            self._validate_run_history(connection, current)
            if current.state is not expected:
                raise ExecutorCommandTransitionError(
                    f"executor run is {current.state.value}, expected {expected.value}"
                )
            if target not in _RUN_TRANSITIONS[current.state]:
                raise ExecutorCommandTransitionError(
                    f"executor run transition {current.state.value}->{target.value} is forbidden"
                )
            if target is ExecutorRunState.READY:
                pending = connection.execute(
                    """
                    SELECT request_id FROM executor_commands
                    WHERE state IN ('recorded','dispatching','outcome_unknown') LIMIT 1
                    """
                ).fetchone()
                if pending is not None:
                    raise ExecutorRunNotReadyError(
                        "executor run has commands requiring recovery"
                    )
            ended_at = timestamp if target in {
                ExecutorRunState.STOPPED,
                ExecutorRunState.CRASHED,
                ExecutorRunState.FAILED,
            } else None
            connection.execute(
                """
                UPDATE executor_runs
                SET state = ?, updated_at = ?, ended_at = ?
                WHERE run_id = ? AND state = ?
                """,
                (target.value, timestamp, ended_at, clean_run_id, current.state.value),
            )
            self._append_run_event(
                connection,
                run_id=clean_run_id,
                from_state=current.state,
                to_state=target,
                metadata_json=metadata_json,
                timestamp=timestamp,
            )
            updated = self._run_by_id(connection, clean_run_id)
            self._validate_active_run_uniqueness(connection)
            self._validate_run_history(connection, updated)
            connection.commit()
            return updated
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError("failed to transition executor run") from exc
        finally:
            connection.close()

    def get_run(self, run_id: str) -> ExecutorRunRecord | None:
        clean_run_id = _validated_request_id(run_id, label="run_id")
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM executor_runs WHERE run_id = ?",
                (clean_run_id,),
            ).fetchone()
            if row is None:
                return None
            record = self._run_from_row(row)
            self._validate_run_history(connection, record)
            return record
        finally:
            connection.close()

    def record(
        self,
        *,
        request_id: str,
        operation: str,
        payload: Mapping[str, object],
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> tuple[ExecutorCommandRecord, bool]:
        clean_id = _validated_request_id(request_id)
        clean_operation = _validated_operation(operation)
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        canonical = canonical_command_json(clean_operation, payload)
        fingerprint = _sha256_text(canonical)
        payload_object = _strict_json_object(canonical, label="executor command")["payload"]
        assert isinstance(payload_object, dict)
        payload_json = _canonical_json(
            payload_object,
            max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            label="command payload",
        )
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            row = connection.execute(
                "SELECT * FROM executor_commands WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
            if row is not None:
                record = self._command_from_row(row)
                self._validate_command_history(connection, record)
                if (
                    record.operation != clean_operation
                    or record.fingerprint_sha256 != fingerprint
                    or canonical_command_json(record.operation, record.payload) != canonical
                ):
                    raise ExecutorCommandCollisionError(
                        "executor request id is already bound to a different command"
                    )
                if record.state.value not in _TERMINAL_COMMAND_STATES and (
                    record.fence_epoch != clean_epoch
                    or record.policy_sha256 != clean_policy
                    or record.last_run_id != clean_run_id
                ):
                    raise ExecutorCommandTransitionError(
                        "nonterminal executor command requires startup recovery"
                    )
                connection.commit()
                return record, False

            active_flatten = connection.execute(
                """
                SELECT operation_id FROM executor_safe_flatten_operations
                WHERE state <> 'flat_latched' LIMIT 1
                """
            ).fetchone()
            if active_flatten is not None:
                raise ExecutorRunNotReadyError(
                    "safe-flatten blocks new external executor commands"
                )
            self._validate_control_history(connection)
            control = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            if control.kill_switch_active and _is_nonreducing_submit(
                clean_operation,
                payload_object,
            ):
                raise ExecutorRunNotReadyError(
                    "durable kill switch blocks a new non-reducing submit"
                )

            connection.execute(
                """
                INSERT INTO executor_commands (
                    request_id, operation, fingerprint_sha256, payload_json,
                    state, outcome_json, outcome_sha256, fence_epoch,
                    policy_sha256, created_run_id, last_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_id,
                    clean_operation,
                    fingerprint,
                    payload_json,
                    ExecutorCommandState.RECORDED.value,
                    clean_epoch,
                    clean_policy,
                    clean_run_id,
                    clean_run_id,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_command_event(
                connection,
                request_id=clean_id,
                run_id=clean_run_id,
                event_type="recorded",
                from_state=None,
                to_state=ExecutorCommandState.RECORDED,
                metadata_json=_canonical_json(
                    {"fence_epoch": clean_epoch, "policy_sha256": clean_policy},
                    max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                    label="command metadata",
                ),
                outcome_sha256=None,
                timestamp=timestamp,
            )
            created = self._command_by_id(connection, clean_id)
            connection.commit()
            return created, True
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to record executor command; mutations must remain blocked"
            ) from exc
        finally:
            connection.close()

    def get(self, request_id: str) -> ExecutorCommandRecord | None:
        clean_id = _validated_request_id(request_id)
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM executor_commands WHERE request_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                return None
            record = self._command_from_row(row)
            self._validate_command_history(connection, record)
            return record
        finally:
            connection.close()

    def claim(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        return self._transition_command_normal(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            expected=(ExecutorCommandState.RECORDED,),
            target=ExecutorCommandState.DISPATCHING,
            event_type="dispatch_claimed",
            outcome=None,
            metadata={},
        )

    def complete(
        self,
        request_id: str,
        result: Mapping[str, object],
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        outcome = {
            "ok": True,
            "payload": _normalize_json_object(result, location="result"),
            "error": None,
        }
        return self._transition_command_normal(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            expected=(ExecutorCommandState.DISPATCHING,),
            target=ExecutorCommandState.COMPLETED,
            event_type="dispatch_completed",
            outcome=outcome,
            metadata={},
        )

    def reject(
        self,
        request_id: str,
        *,
        code: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        outcome = _safe_error_outcome(code)
        return self._transition_command_normal(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            expected=(ExecutorCommandState.DISPATCHING,),
            target=ExecutorCommandState.REJECTED,
            event_type="dispatch_rejected",
            outcome=outcome,
            metadata={},
        )

    def mark_outcome_unknown(
        self,
        request_id: str,
        *,
        reason: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        return self._transition_command_normal(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            expected=(ExecutorCommandState.DISPATCHING,),
            target=ExecutorCommandState.OUTCOME_UNKNOWN,
            event_type="outcome_unknown",
            outcome=None,
            metadata={"reason": _validated_reason_code(reason)},
        )

    def requeue_proven_not_dispatched(
        self,
        request_id: str,
        *,
        operation: str,
        reason: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        """Return one claimed command to RECORDED on structured no-dispatch proof.

        The application may use this transition only while retaining the
        in-process :class:`PaperOrderResult` capability which proves that the
        supervised SDK mutation method was not entered.  The proof itself is
        deliberately not serializable; this ledger records its classification,
        operation and bounded reason for later audit.
        """

        clean_operation = _validated_operation(operation)
        if clean_operation not in {"submit_order", "cancel_order"}:
            raise ExecutorCommandTransitionError(
                "only broker order commands may be requeued before dispatch"
            )
        return self._transition_command_normal(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            expected=(ExecutorCommandState.DISPATCHING,),
            target=ExecutorCommandState.RECORDED,
            event_type="dispatch_proven_not_started",
            outcome=None,
            metadata={
                "classification": "structured_not_dispatched",
                "operation": clean_operation,
                "reason": _validated_reason_code(reason),
                "retry_scope": "same_run_once",
            },
            required_operation=clean_operation,
            forbid_prior_same_run_event_type="dispatch_proven_not_started",
        )

    def reject_proven_not_dispatched_retry_exhausted(
        self,
        request_id: str,
        *,
        operation: str,
        reason: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        """Terminate a second proven pre-dispatch failure without ambiguity."""

        clean_operation = _validated_operation(operation)
        if clean_operation not in {"submit_order", "cancel_order"}:
            raise ExecutorCommandTransitionError(
                "only broker order commands have a pre-dispatch retry budget"
            )
        return self._transition_command_normal(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            expected=(ExecutorCommandState.DISPATCHING,),
            target=ExecutorCommandState.REJECTED,
            event_type="dispatch_not_started_retry_exhausted",
            outcome=_safe_error_outcome("predispatch_retry_exhausted"),
            metadata={
                "classification": "structured_not_dispatched",
                "operation": clean_operation,
                "reason": _validated_reason_code(reason),
                "retry_scope": "same_run_once",
            },
            required_operation=clean_operation,
        )

    def resolve_recorded_not_dispatched(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        clean_id = _validated_request_id(request_id)
        return self._transition_command(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            allowed_run_states=(ExecutorRunState.RECOVERING, ExecutorRunState.BLOCKED),
            expected=(ExecutorCommandState.RECORDED,),
            target=ExecutorCommandState.REJECTED,
            event_type="reconciled_not_dispatched",
            outcome=_safe_error_outcome("command_not_dispatched"),
            metadata={
                "evidence": {
                    "source": "executor_command_journal",
                    "request_id": clean_id,
                    "observed_state": ExecutorCommandState.RECORDED.value,
                }
            },
            require_original_context=False,
            recovery_evidence=None,
        )

    def resolve_unknown_completed(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        evidence: Mapping[str, object],
    ) -> ExecutorCommandRecord:
        current = self.get(request_id)
        if current is None:
            raise ExecutorCommandTransitionError("unknown executor request id")
        connection = self._open_connection()
        try:
            if connection.execute(
                "SELECT 1 FROM executor_safe_flatten_legs WHERE leg_id = ?",
                (current.request_id,),
            ).fetchone() is not None:
                raise ExecutorCommandTransitionError(
                    "safe-flatten recovery requires its atomic workflow API"
                )
        finally:
            connection.close()
        clean_evidence = _validated_reconciliation_evidence(evidence)
        result = _recovered_order_result(current, clean_evidence)
        outcome = {
            "ok": True,
            "payload": result,
            "error": None,
        }
        return self._transition_command_recovery(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            expected=(ExecutorCommandState.OUTCOME_UNKNOWN,),
            target=ExecutorCommandState.COMPLETED,
            event_type="reconciled_completed",
            outcome=outcome,
            evidence=evidence,
        )

    def resolve_unknown_from_broker_observation(
        self,
        request_id: str,
        *,
        client_order_id: str,
        broker_order_id: str,
        observed_status: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        """Resolve one ambiguous order from a fresh, positive broker read.

        Identity hashes and the observation timestamp are derived internally;
        callers cannot substitute evidence belonging to another command.
        """

        current = self.get(request_id)
        if current is None:
            raise ExecutorCommandTransitionError("unknown executor request id")
        evidence = {
            "source": "broker_order",
            "client_order_id": _validated_text(
                client_order_id,
                label="client_order_id",
            ),
            "broker_order_id": _validated_text(
                broker_order_id,
                label="broker_order_id",
            ),
            "observed_status": _validated_reason_code(observed_status),
            "observed_at": self._timestamp(),
            "command_fingerprint_sha256": current.fingerprint_sha256,
            "intent_fingerprint_sha256": _business_intent_fingerprint(current),
        }
        return self.resolve_unknown_completed(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            evidence=evidence,
        )

    def resolve_unknown_control_not_applied(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        clean_id = _validated_request_id(request_id)
        return self._transition_command(
            clean_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            allowed_run_states=(ExecutorRunState.RECOVERING, ExecutorRunState.BLOCKED),
            expected=(ExecutorCommandState.OUTCOME_UNKNOWN,),
            target=ExecutorCommandState.REJECTED,
            event_type="reconciled_control_not_applied",
            outcome=_safe_error_outcome("command_not_dispatched"),
            metadata={
                "evidence": {
                    "source": "atomic_control_transaction",
                    "request_id": clean_id,
                    "observed_state": "not_applied",
                }
            },
            require_original_context=False,
            require_control_not_applied=True,
        )

    def resolve_unknown_rejected(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        evidence: Mapping[str, object],
    ) -> ExecutorCommandRecord:
        del request_id, run_id, fence_epoch, policy_sha256, evidence
        raise ExecutorCommandTransitionError(
            "an ambiguous broker mutation cannot be rejected from absence evidence"
        )

    def recovery_required(self) -> tuple[ExecutorCommandRecord, ...]:
        connection = self._open_connection()
        try:
            rows = connection.execute(
                """
                SELECT * FROM executor_commands
                WHERE state IN ('recorded','dispatching','outcome_unknown')
                ORDER BY created_at, request_id
                """
            ).fetchall()
            records = tuple(self._command_from_row(row) for row in rows)
            for record in records:
                self._validate_command_history(connection, record)
            return records
        finally:
            connection.close()

    def events(self, request_id: str | None = None) -> tuple[ExecutorCommandEvent, ...]:
        clean_id = None if request_id is None else _validated_request_id(request_id)
        connection = self._open_connection()
        try:
            if clean_id is None:
                rows = connection.execute(
                    "SELECT * FROM executor_command_events ORDER BY sequence"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM executor_command_events
                    WHERE request_id = ? ORDER BY sequence
                    """,
                    (clean_id,),
                ).fetchall()
            return tuple(self._command_event_from_row(row) for row in rows)
        finally:
            connection.close()

    def run_events(self, run_id: str | None = None) -> tuple[ExecutorRunEvent, ...]:
        clean_id = None if run_id is None else _validated_request_id(run_id, label="run_id")
        connection = self._open_connection()
        try:
            if clean_id is None:
                rows = connection.execute(
                    "SELECT * FROM executor_run_events ORDER BY sequence"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM executor_run_events WHERE run_id = ? ORDER BY sequence",
                    (clean_id,),
                ).fetchall()
            return tuple(self._run_event_from_row(row) for row in rows)
        finally:
            connection.close()

    def read_control_state(self) -> ExecutorControlState:
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM executor_control_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ExecutorCommandSchemaError("executor control state is missing")
            state = self._control_from_row(row)
            self._validate_control_history(connection)
            return state
        finally:
            connection.close()

    def latch_kill_switch_and_complete(
        self,
        *,
        request_id: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorCommandRecord:
        clean_request_id = _validated_request_id(request_id)
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            command = self._command_by_id(connection, clean_request_id)
            if (
                command.operation != "latch_kill_switch"
                or command.state is not ExecutorCommandState.DISPATCHING
                or command.last_run_id != clean_run_id
            ):
                raise ExecutorCommandTransitionError(
                    "kill switch requires its claimed durable command"
                )
            self._validate_command_history(connection, command)
            if set(command.payload) != {"reason_code"}:
                raise ExecutorCommandSchemaError(
                    "durable kill switch command payload is invalid"
                )
            clean_reason = _validated_reason_code(command.payload["reason_code"])
            self._validate_control_history(connection)
            current = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            if not current.kill_switch_active:
                generation = current.generation + 1
                cursor = connection.execute(
                    """
                    UPDATE executor_control_state
                    SET kill_switch_active = 1, reason_code = ?, generation = ?,
                        last_request_id = ?, updated_at = ?
                    WHERE singleton = 1 AND kill_switch_active = 0
                    """,
                    (clean_reason, generation, clean_request_id, timestamp),
                )
                if cursor.rowcount != 1:
                    raise ExecutorCommandStorageError(
                        "executor kill switch changed concurrently"
                    )
                connection.execute(
                    """
                    INSERT INTO executor_control_events (
                        generation, event_type, reason_code, request_id, run_id, created_at
                    ) VALUES (?, 'kill_switch_latched', ?, ?, ?, ?)
                    """,
                    (generation, clean_reason, clean_request_id, clean_run_id, timestamp),
                )
            updated = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            result = {
                "kill_switch_active": updated.kill_switch_active,
                "generation": updated.generation,
                "reason_code": updated.reason_code,
            }
            outcome = {"ok": True, "payload": result, "error": None}
            outcome_json = _canonical_json(
                outcome,
                max_bytes=MAX_COMMAND_OUTCOME_BYTES,
                label="command outcome",
            )
            outcome_sha256 = _sha256_text(outcome_json)
            cursor = connection.execute(
                """
                UPDATE executor_commands
                SET state = ?, outcome_json = ?, outcome_sha256 = ?,
                    last_run_id = ?, updated_at = ?
                WHERE request_id = ? AND state = ?
                """,
                (
                    ExecutorCommandState.COMPLETED.value,
                    outcome_json,
                    outcome_sha256,
                    clean_run_id,
                    timestamp,
                    clean_request_id,
                    ExecutorCommandState.DISPATCHING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "executor kill switch command changed concurrently"
                )
            self._append_command_event(
                connection,
                request_id=clean_request_id,
                run_id=clean_run_id,
                event_type="dispatch_completed",
                from_state=ExecutorCommandState.DISPATCHING,
                to_state=ExecutorCommandState.COMPLETED,
                metadata_json=_canonical_json(
                    {"atomic_with": "kill_switch_control"},
                    max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                    label="command metadata",
                ),
                outcome_sha256=outcome_sha256,
                timestamp=timestamp,
            )
            completed = self._command_by_id(connection, clean_request_id)
            self._validate_command_history(connection, completed)
            self._validate_control_history(connection)
            connection.commit()
            return completed
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to latch executor kill switch atomically"
            ) from exc
        finally:
            connection.close()

    def start_safe_flatten_and_latch(
        self,
        *,
        operation_id: str,
        initiated_by_uid: int,
        authz_policy_sha256: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> tuple[ExecutorSafeFlattenRecord, bool]:
        """Create the workflow, latch control, and complete its start atomically."""

        clean_operation_id = _validated_request_id(
            operation_id,
            label="operation_id",
        )
        clean_uid = _validated_positive_integer(
            initiated_by_uid,
            label="initiated_by_uid",
        )
        clean_authz = _validated_sha256(
            authz_policy_sha256,
            label="authz_policy_sha256",
        )
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        reason_code = "paper_safe_flatten_confirmed"
        payload = {"operation_id": clean_operation_id}
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            existing_row = connection.execute(
                "SELECT * FROM executor_safe_flatten_operations WHERE operation_id = ?",
                (clean_operation_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._safe_flatten_from_row(existing_row)
                command = self._command_by_id(connection, clean_operation_id)
                self._validate_command_history(connection, command)
                if (
                    command.operation != "start_safe_flatten"
                    or command.payload != payload
                    or command.state is not ExecutorCommandState.COMPLETED
                    or existing.initiated_by_uid != clean_uid
                    or existing.authz_policy_sha256 != clean_authz
                ):
                    raise ExecutorCommandCollisionError(
                        "safe-flatten operation id is already bound to different content"
                    )
                self._validate_safe_flatten_operation_history(connection, existing)
                connection.commit()
                return existing, False

            active = connection.execute(
                """
                SELECT operation_id FROM executor_safe_flatten_operations
                WHERE state <> 'flat_latched' LIMIT 1
                """
            ).fetchone()
            if active is not None:
                raise ExecutorCommandTransitionError(
                    "another safe-flatten operation remains active"
                )
            pending_command = connection.execute(
                """
                SELECT request_id FROM executor_commands
                WHERE state IN ('recorded','dispatching','outcome_unknown')
                LIMIT 1
                """
            ).fetchone()
            if pending_command is not None:
                raise ExecutorCommandTransitionError(
                    "safe-flatten cannot start while a command requires recovery"
                )
            if connection.execute(
                "SELECT 1 FROM executor_commands WHERE request_id = ?",
                (clean_operation_id,),
            ).fetchone() is not None:
                raise ExecutorCommandCollisionError(
                    "safe-flatten operation id collides with an executor command"
                )

            self._insert_recorded_command(
                connection,
                request_id=clean_operation_id,
                operation="start_safe_flatten",
                payload=payload,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                timestamp=timestamp,
            )
            self._claim_command_in_transaction(
                connection,
                request_id=clean_operation_id,
                run_id=clean_run_id,
                timestamp=timestamp,
                event_metadata={"atomic_with": "safe_flatten_control"},
            )

            self._validate_control_history(connection)
            control = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            if not control.kill_switch_active:
                generation = control.generation + 1
                cursor = connection.execute(
                    """
                    UPDATE executor_control_state
                    SET kill_switch_active = 1, reason_code = ?, generation = ?,
                        last_request_id = ?, updated_at = ?
                    WHERE singleton = 1 AND kill_switch_active = 0
                    """,
                    (
                        reason_code,
                        generation,
                        clean_operation_id,
                        timestamp,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ExecutorCommandStorageError(
                        "executor kill switch changed concurrently"
                    )
                connection.execute(
                    """
                    INSERT INTO executor_control_events (
                        generation, event_type, reason_code, request_id,
                        run_id, created_at
                    ) VALUES (?, 'kill_switch_latched', ?, ?, ?, ?)
                    """,
                    (
                        generation,
                        reason_code,
                        clean_operation_id,
                        clean_run_id,
                        timestamp,
                    ),
                )
            updated_control = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            if not updated_control.kill_switch_active or updated_control.generation < 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten requires an active durable kill switch"
                )

            connection.execute(
                """
                INSERT INTO executor_safe_flatten_operations (
                    operation_id, initiating_request_id, account_scope_sha256,
                    reason_code, control_generation, state, state_version,
                    resume_state, last_error_code, initiated_by_uid,
                    created_run_id, last_run_id, created_fence_epoch,
                    last_fence_epoch, policy_sha256, last_policy_sha256,
                    authz_policy_sha256, created_at, updated_at, terminal_at
                ) VALUES (?, ?, ?, ?, ?, 'latched', 1, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    clean_operation_id,
                    clean_operation_id,
                    self.account_scope_sha256,
                    reason_code,
                    updated_control.generation,
                    clean_uid,
                    clean_run_id,
                    clean_run_id,
                    clean_epoch,
                    clean_epoch,
                    clean_policy,
                    clean_policy,
                    clean_authz,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_safe_flatten_operation_event(
                connection,
                operation_id=clean_operation_id,
                run_id=clean_run_id,
                event_type="workflow_started",
                from_state=None,
                to_state=ExecutorSafeFlattenState.LATCHED,
                state_version=1,
                metadata={
                    "control_generation": updated_control.generation,
                    "kill_switch_active": True,
                },
                timestamp=timestamp,
            )
            self._complete_command_in_transaction(
                connection,
                request_id=clean_operation_id,
                result={
                    "operation_id": clean_operation_id,
                    "state": ExecutorSafeFlattenState.LATCHED.value,
                    "state_version": 1,
                    "kill_switch_active": True,
                },
                run_id=clean_run_id,
                timestamp=timestamp,
                event_metadata={"atomic_with": "safe_flatten_control"},
            )
            created = self._safe_flatten_by_id(connection, clean_operation_id)
            self._validate_command_history(
                connection,
                self._command_by_id(connection, clean_operation_id),
            )
            self._validate_control_history(connection)
            self._validate_safe_flatten_operation_history(connection, created)
            connection.commit()
            return created, True
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to start safe-flatten atomically"
            ) from exc
        finally:
            connection.close()

    def get_safe_flatten(
        self,
        operation_id: str,
    ) -> ExecutorSafeFlattenRecord | None:
        clean_id = _validated_request_id(operation_id, label="operation_id")
        connection = self._open_connection()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM executor_safe_flatten_operations WHERE operation_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            record = self._safe_flatten_from_row(row)
            self._validate_control_history(connection)
            self._validate_safe_flatten_operation_history(connection, record)
            connection.commit()
            return record
        finally:
            connection.close()

    def active_safe_flatten(self) -> ExecutorSafeFlattenRecord | None:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT * FROM executor_safe_flatten_operations
                WHERE state <> 'flat_latched'
                ORDER BY created_at, operation_id
                """
            ).fetchall()
            if len(rows) > 1:
                raise ExecutorCommandSchemaError(
                    "multiple safe-flatten operations claim active authority"
                )
            if not rows:
                connection.commit()
                return None
            record = self._safe_flatten_from_row(rows[0])
            self._validate_control_history(connection)
            self._validate_safe_flatten_operation_history(connection, record)
            connection.commit()
            return record
        finally:
            connection.close()

    def transition_safe_flatten(
        self,
        operation_id: str,
        state: ExecutorSafeFlattenState | str,
        *,
        expected_state: ExecutorSafeFlattenState | str,
        event_type: str,
        metadata: Mapping[str, object],
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        resume_state: ExecutorSafeFlattenState | str | None = None,
        error_code: str | None = None,
    ) -> ExecutorSafeFlattenRecord:
        clean_operation_id = _validated_request_id(
            operation_id,
            label="operation_id",
        )
        target = _coerce_safe_flatten_state(state)
        expected = _coerce_safe_flatten_state(expected_state)
        clean_event_type = _validated_reason_code(event_type)
        clean_metadata = _normalize_json_object(
            metadata,
            location="safe-flatten metadata",
        )
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        clean_resume = (
            None
            if resume_state is None
            else _coerce_safe_flatten_state(resume_state)
        )
        if clean_resume not in {
            None,
            ExecutorSafeFlattenState.CANCELING,
            ExecutorSafeFlattenState.CLOSING,
        }:
            raise ExecutorCommandTransitionError(
                "safe-flatten resume state is invalid"
            )
        clean_error = (
            None if error_code is None else _validated_reason_code(error_code)
        )
        if target is ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN:
            raise ExecutorCommandTransitionError(
                "only atomic broker-outcome handling may block safe-flatten"
            )
        if clean_resume is not None:
            raise ExecutorCommandTransitionError(
                "safe-flatten resume state is only valid for an ambiguous outcome"
            )
        if target is ExecutorSafeFlattenState.FAILED_LATCHED and clean_error is None:
            raise ExecutorCommandTransitionError(
                "failed safe-flatten state requires a safe error code"
            )

        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            current = self._safe_flatten_by_id(connection, clean_operation_id)
            self._validate_safe_flatten_operation_history(connection, current)
            if current.state is ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN:
                raise ExecutorCommandTransitionError(
                    "only atomic broker recovery may resume safe-flatten"
                )
            if current.state is not expected:
                raise ExecutorCommandTransitionError(
                    f"safe-flatten operation is {current.state.value}, expected {expected.value}"
                )
            if target not in _SAFE_FLATTEN_TRANSITIONS[current.state]:
                raise ExecutorCommandTransitionError(
                    f"safe-flatten transition {current.state.value}->{target.value} is forbidden"
                )
            if target is not current.state:
                active_leg = connection.execute(
                    """
                    SELECT leg_id FROM executor_safe_flatten_legs
                    WHERE operation_id = ?
                      AND state IN ('dispatching','accepted','outcome_unknown')
                    LIMIT 1
                    """,
                    (clean_operation_id,),
                ).fetchone()
                if active_leg is not None:
                    raise ExecutorCommandTransitionError(
                        "safe-flatten cannot change phase with an active broker leg"
                    )
            if target is ExecutorSafeFlattenState.CANCEL_CONFIRMED:
                unverified_cancel = connection.execute(
                    """
                    SELECT leg_id FROM executor_safe_flatten_legs
                    WHERE operation_id = ? AND kind = 'cancel_order'
                      AND state <> 'verified' LIMIT 1
                    """,
                    (clean_operation_id,),
                ).fetchone()
                if unverified_cancel is not None:
                    raise ExecutorCommandTransitionError(
                        "safe-flatten cancellation phase has an unverified leg"
                    )
            if target in {
                ExecutorSafeFlattenState.FILLS_CONFIRMED,
                ExecutorSafeFlattenState.RECONCILING,
                ExecutorSafeFlattenState.FLAT_LATCHED,
            }:
                unverified_close = connection.execute(
                    """
                    SELECT leg_id FROM executor_safe_flatten_legs
                    WHERE operation_id = ? AND kind = 'close_position'
                      AND state <> 'verified' LIMIT 1
                    """,
                    (clean_operation_id,),
                ).fetchone()
                if unverified_close is not None:
                    raise ExecutorCommandTransitionError(
                        "safe-flatten close phase has an unverified leg"
                    )
            control = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            self._validate_control_history(connection)
            if (
                not control.kill_switch_active
                or control.generation < current.control_generation
            ):
                raise ExecutorCommandTransitionError(
                    "safe-flatten durable kill switch is not active"
                )
            if target is ExecutorSafeFlattenState.FLAT_LATCHED:
                _validated_safe_flatten_final_evidence(clean_metadata)
                nonverified = connection.execute(
                    """
                    SELECT leg_id FROM executor_safe_flatten_legs
                    WHERE operation_id = ? AND state <> 'verified' LIMIT 1
                    """,
                    (clean_operation_id,),
                ).fetchone()
                if nonverified is not None:
                    raise ExecutorCommandTransitionError(
                        "safe-flatten cannot finish with an unverified leg"
                    )
            version = current.state_version + 1
            terminal_at = (
                timestamp
                if target
                in {
                    ExecutorSafeFlattenState.FAILED_LATCHED,
                    ExecutorSafeFlattenState.FLAT_LATCHED,
                }
                else None
            )
            cursor = connection.execute(
                """
                UPDATE executor_safe_flatten_operations
                SET state = ?, state_version = ?, resume_state = ?,
                    last_error_code = ?, last_run_id = ?, last_fence_epoch = ?,
                    last_policy_sha256 = ?, updated_at = ?, terminal_at = ?
                WHERE operation_id = ? AND state = ? AND state_version = ?
                """,
                (
                    target.value,
                    version,
                    None if clean_resume is None else clean_resume.value,
                    clean_error,
                    clean_run_id,
                    clean_epoch,
                    clean_policy,
                    timestamp,
                    terminal_at,
                    clean_operation_id,
                    current.state.value,
                    current.state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten operation changed concurrently"
                )
            self._append_safe_flatten_operation_event(
                connection,
                operation_id=clean_operation_id,
                run_id=clean_run_id,
                event_type=clean_event_type,
                from_state=current.state,
                to_state=target,
                state_version=version,
                metadata=clean_metadata,
                timestamp=timestamp,
            )
            updated = self._safe_flatten_by_id(connection, clean_operation_id)
            self._validate_safe_flatten_operation_history(connection, updated)
            connection.commit()
            return updated
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to transition safe-flatten operation"
            ) from exc
        finally:
            connection.close()

    def safe_flatten_legs(
        self,
        operation_id: str,
    ) -> tuple[ExecutorSafeFlattenLegRecord, ...]:
        clean_id = _validated_request_id(operation_id, label="operation_id")
        connection = self._open_connection()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT * FROM executor_safe_flatten_legs
                WHERE operation_id = ? ORDER BY ordinal, leg_id
                """,
                (clean_id,),
            ).fetchall()
            records = tuple(self._safe_flatten_leg_from_row(row) for row in rows)
            for record in records:
                self._validate_safe_flatten_leg_history(connection, record)
            connection.commit()
            return records
        finally:
            connection.close()

    def begin_safe_flatten_leg(
        self,
        *,
        operation_id: str,
        leg_id: str,
        ordinal: int,
        kind: ExecutorSafeFlattenLegKind | str,
        target: Mapping[str, object],
        command_operation: str,
        command_payload: Mapping[str, object],
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> tuple[ExecutorSafeFlattenLegRecord, bool]:
        clean_operation_id = _validated_request_id(operation_id, label="operation_id")
        clean_leg_id = _validated_request_id(leg_id, label="leg_id")
        clean_ordinal = _validated_nonnegative_integer(ordinal, label="ordinal")
        clean_kind = _coerce_safe_flatten_leg_kind(kind)
        clean_command_operation = _validated_operation(command_operation)
        clean_target = _validated_safe_flatten_leg_target(
            clean_kind,
            target,
            command_operation=clean_command_operation,
            command_payload=command_payload,
        )
        target_json = _canonical_json(
            clean_target,
            max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            label="safe-flatten leg target",
        )
        target_sha256 = _sha256_text(target_json)
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        expected_operation_state = (
            ExecutorSafeFlattenState.CANCELING
            if clean_kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER
            else ExecutorSafeFlattenState.CLOSING
        )
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            operation = self._safe_flatten_by_id(connection, clean_operation_id)
            self._validate_safe_flatten_operation_history(connection, operation)
            if operation.state is not expected_operation_state:
                raise ExecutorCommandTransitionError(
                    "safe-flatten leg does not match the active workflow phase"
                )
            control = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            self._validate_control_history(connection)
            if not control.kill_switch_active:
                raise ExecutorCommandTransitionError(
                    "safe-flatten durable kill switch is not active"
                )

            existing_row = connection.execute(
                "SELECT * FROM executor_safe_flatten_legs WHERE leg_id = ?",
                (clean_leg_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._safe_flatten_leg_from_row(existing_row)
                command = self._command_by_id(connection, clean_leg_id)
                if (
                    existing.operation_id != clean_operation_id
                    or existing.ordinal != clean_ordinal
                    or existing.kind is not clean_kind
                    or existing.target_sha256 != target_sha256
                    or existing.target != clean_target
                    or existing.command_operation != clean_command_operation
                    or command.operation != clean_command_operation
                    or command_fingerprint(command.operation, command.payload)
                    != command_fingerprint(clean_command_operation, command_payload)
                ):
                    raise ExecutorCommandCollisionError(
                        "safe-flatten leg id is already bound to different content"
                    )
                self._validate_command_history(connection, command)
                self._validate_safe_flatten_leg_history(connection, existing)
                connection.commit()
                return existing, False

            pending = connection.execute(
                """
                SELECT leg_id FROM executor_safe_flatten_legs
                WHERE operation_id = ?
                  AND state IN ('dispatching','accepted','outcome_unknown')
                LIMIT 1
                """,
                (clean_operation_id,),
            ).fetchone()
            if pending is not None:
                raise ExecutorCommandTransitionError(
                    "safe-flatten requires serial broker legs"
                )
            if connection.execute(
                "SELECT 1 FROM executor_commands WHERE request_id = ?",
                (clean_leg_id,),
            ).fetchone() is not None:
                raise ExecutorCommandCollisionError(
                    "safe-flatten leg id collides with an executor command"
                )

            self._insert_recorded_command(
                connection,
                request_id=clean_leg_id,
                operation=clean_command_operation,
                payload=command_payload,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                timestamp=timestamp,
            )
            self._claim_command_in_transaction(
                connection,
                request_id=clean_leg_id,
                run_id=clean_run_id,
                timestamp=timestamp,
                event_metadata={
                    "safe_flatten_operation_id": clean_operation_id,
                    "safe_flatten_ordinal": clean_ordinal,
                },
            )
            connection.execute(
                """
                INSERT INTO executor_safe_flatten_legs (
                    leg_id, operation_id, ordinal, kind, target_json,
                    target_sha256, command_operation, state, broker_order_id,
                    created_run_id, last_run_id, created_at, updated_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatching', NULL, ?, ?, ?, ?, NULL)
                """,
                (
                    clean_leg_id,
                    clean_operation_id,
                    clean_ordinal,
                    clean_kind.value,
                    target_json,
                    target_sha256,
                    clean_command_operation,
                    clean_run_id,
                    clean_run_id,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_safe_flatten_leg_event(
                connection,
                leg_id=clean_leg_id,
                run_id=clean_run_id,
                event_type="dispatch_claimed",
                from_state=None,
                to_state=ExecutorSafeFlattenLegState.DISPATCHING,
                metadata={"ordinal": clean_ordinal},
                timestamp=timestamp,
            )
            self._touch_safe_flatten_operation(
                connection,
                operation=operation,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                event_type="leg_started",
                metadata={
                    "kind": clean_kind.value,
                    "leg_id": clean_leg_id,
                    "ordinal": clean_ordinal,
                },
                timestamp=timestamp,
            )
            created = self._safe_flatten_leg_by_id(connection, clean_leg_id)
            self._validate_command_history(
                connection,
                self._command_by_id(connection, clean_leg_id),
            )
            self._validate_safe_flatten_leg_history(connection, created)
            self._validate_safe_flatten_operation_history(
                connection,
                self._safe_flatten_by_id(connection, clean_operation_id),
            )
            connection.commit()
            return created, True
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to begin safe-flatten leg"
            ) from exc
        finally:
            connection.close()

    def complete_safe_flatten_leg(
        self,
        *,
        leg_id: str,
        result: Mapping[str, object],
        accepted: bool,
        broker_order_id: str | None,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorSafeFlattenLegRecord:
        clean_leg_id = _validated_request_id(leg_id, label="leg_id")
        if type(accepted) is not bool:
            raise ExecutorCommandJournalError("safe-flatten accepted flag is invalid")
        clean_broker_id = (
            None
            if broker_order_id is None
            else _validated_text(broker_order_id, label="broker_order_id")
        )
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        target_state = (
            ExecutorSafeFlattenLegState.ACCEPTED
            if accepted
            else ExecutorSafeFlattenLegState.REJECTED
        )
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            leg = self._safe_flatten_leg_by_id(connection, clean_leg_id)
            if leg.state is not ExecutorSafeFlattenLegState.DISPATCHING:
                raise ExecutorCommandTransitionError(
                    "safe-flatten leg is not dispatching"
                )
            clean_result = _validated_safe_flatten_dispatch_result(
                leg,
                result=result,
                accepted=accepted,
                broker_order_id=clean_broker_id,
            )
            command = self._command_by_id(connection, clean_leg_id)
            if command.state is not ExecutorCommandState.DISPATCHING:
                raise ExecutorCommandTransitionError(
                    "safe-flatten command is not dispatching"
                )
            completed = self._complete_command_in_transaction(
                connection,
                request_id=clean_leg_id,
                result=clean_result,
                run_id=clean_run_id,
                timestamp=timestamp,
                event_metadata={
                    "safe_flatten_operation_id": leg.operation_id,
                    "safe_flatten_leg_state": target_state.value,
                },
            )
            cursor = connection.execute(
                """
                UPDATE executor_safe_flatten_legs
                SET state = ?, broker_order_id = ?, last_run_id = ?, updated_at = ?
                WHERE leg_id = ? AND state = 'dispatching'
                """,
                (
                    target_state.value,
                    clean_broker_id,
                    clean_run_id,
                    timestamp,
                    clean_leg_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten leg changed concurrently"
                )
            self._append_safe_flatten_leg_event(
                connection,
                leg_id=clean_leg_id,
                run_id=clean_run_id,
                event_type="dispatch_completed",
                from_state=ExecutorSafeFlattenLegState.DISPATCHING,
                to_state=target_state,
                metadata={"broker_order_id_present": clean_broker_id is not None},
                timestamp=timestamp,
            )
            operation = self._safe_flatten_by_id(connection, leg.operation_id)
            self._touch_safe_flatten_operation(
                connection,
                operation=operation,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                event_type="leg_dispatch_completed",
                metadata={
                    "kind": leg.kind.value,
                    "leg_id": clean_leg_id,
                    "leg_state": target_state.value,
                },
                timestamp=timestamp,
            )
            updated = self._safe_flatten_leg_by_id(connection, clean_leg_id)
            self._validate_command_history(connection, completed)
            self._validate_safe_flatten_leg_history(connection, updated)
            connection.commit()
            return updated
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to complete safe-flatten leg"
            ) from exc
        finally:
            connection.close()

    def mark_safe_flatten_leg_outcome_unknown(
        self,
        *,
        leg_id: str,
        reason: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorSafeFlattenRecord:
        clean_leg_id = _validated_request_id(leg_id, label="leg_id")
        clean_reason = _validated_reason_code(reason)
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            leg = self._safe_flatten_leg_by_id(connection, clean_leg_id)
            if leg.state is not ExecutorSafeFlattenLegState.DISPATCHING:
                raise ExecutorCommandTransitionError(
                    "safe-flatten leg is not dispatching"
                )
            command = self._command_by_id(connection, clean_leg_id)
            if command.state is not ExecutorCommandState.DISPATCHING:
                raise ExecutorCommandTransitionError(
                    "safe-flatten command is not dispatching"
                )
            operation = self._safe_flatten_by_id(connection, leg.operation_id)
            resume_state = operation.state
            if resume_state not in {
                ExecutorSafeFlattenState.CANCELING,
                ExecutorSafeFlattenState.CLOSING,
            }:
                raise ExecutorCommandTransitionError(
                    "safe-flatten ambiguous leg has no resumable phase"
                )

            cursor = connection.execute(
                """
                UPDATE executor_commands
                SET state = 'outcome_unknown', last_run_id = ?, updated_at = ?
                WHERE request_id = ? AND state = 'dispatching'
                """,
                (clean_run_id, timestamp, clean_leg_id),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten command changed concurrently"
                )
            self._append_command_event(
                connection,
                request_id=clean_leg_id,
                run_id=clean_run_id,
                event_type="outcome_unknown",
                from_state=ExecutorCommandState.DISPATCHING,
                to_state=ExecutorCommandState.OUTCOME_UNKNOWN,
                metadata_json=_canonical_json(
                    {
                        "reason": clean_reason,
                        "safe_flatten_operation_id": leg.operation_id,
                    },
                    max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                    label="command metadata",
                ),
                outcome_sha256=None,
                timestamp=timestamp,
            )
            cursor = connection.execute(
                """
                UPDATE executor_safe_flatten_legs
                SET state = 'outcome_unknown', last_run_id = ?, updated_at = ?
                WHERE leg_id = ? AND state = 'dispatching'
                """,
                (clean_run_id, timestamp, clean_leg_id),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten leg changed concurrently"
                )
            self._append_safe_flatten_leg_event(
                connection,
                leg_id=clean_leg_id,
                run_id=clean_run_id,
                event_type="outcome_unknown",
                from_state=ExecutorSafeFlattenLegState.DISPATCHING,
                to_state=ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN,
                metadata={"reason": clean_reason},
                timestamp=timestamp,
            )
            version = operation.state_version + 1
            cursor = connection.execute(
                """
                UPDATE executor_safe_flatten_operations
                SET state = 'blocked_outcome_unknown', state_version = ?,
                    resume_state = ?, last_error_code = 'command_outcome_unknown',
                    last_run_id = ?, last_fence_epoch = ?,
                    last_policy_sha256 = ?, updated_at = ?
                WHERE operation_id = ? AND state = ? AND state_version = ?
                """,
                (
                    version,
                    resume_state.value,
                    clean_run_id,
                    clean_epoch,
                    clean_policy,
                    timestamp,
                    operation.operation_id,
                    operation.state.value,
                    operation.state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten operation changed concurrently"
                )
            self._append_safe_flatten_operation_event(
                connection,
                operation_id=operation.operation_id,
                run_id=clean_run_id,
                event_type="leg_outcome_unknown",
                from_state=operation.state,
                to_state=ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
                state_version=version,
                metadata={
                    "leg_id": clean_leg_id,
                    "operation": command.operation,
                    "phase": resume_state.value,
                    "request_id": clean_leg_id,
                    "retry_allowed": False,
                },
                timestamp=timestamp,
            )
            updated = self._safe_flatten_by_id(connection, operation.operation_id)
            self._validate_command_history(
                connection,
                self._command_by_id(connection, clean_leg_id),
            )
            self._validate_safe_flatten_leg_history(
                connection,
                self._safe_flatten_leg_by_id(connection, clean_leg_id),
            )
            self._validate_safe_flatten_operation_history(connection, updated)
            connection.commit()
            return updated
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to block ambiguous safe-flatten leg"
            ) from exc
        finally:
            connection.close()

    def verify_safe_flatten_leg(
        self,
        *,
        leg_id: str,
        verified: bool,
        broker_order_id: str | None,
        evidence: Mapping[str, object],
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorSafeFlattenLegRecord:
        clean_leg_id = _validated_request_id(leg_id, label="leg_id")
        if type(verified) is not bool:
            raise ExecutorCommandJournalError("safe-flatten verified flag is invalid")
        clean_broker_id = (
            None
            if broker_order_id is None
            else _validated_text(broker_order_id, label="broker_order_id")
        )
        clean_evidence = _normalize_json_object(
            evidence,
            location="safe-flatten leg evidence",
        )
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        target_state = (
            ExecutorSafeFlattenLegState.VERIFIED
            if verified
            else ExecutorSafeFlattenLegState.REJECTED
        )
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.READY,),
            )
            leg = self._safe_flatten_leg_by_id(connection, clean_leg_id)
            if leg.state is not ExecutorSafeFlattenLegState.ACCEPTED:
                raise ExecutorCommandTransitionError(
                    "safe-flatten leg is not awaiting verification"
                )
            clean_evidence = _validated_safe_flatten_leg_evidence(
                leg,
                verified=verified,
                broker_order_id=clean_broker_id,
                evidence=clean_evidence,
            )
            if (
                leg.broker_order_id is not None
                and clean_broker_id is not None
                and leg.broker_order_id != clean_broker_id
            ):
                raise ExecutorCommandCollisionError(
                    "safe-flatten leg resolved to a different broker order id"
                )
            cursor = connection.execute(
                """
                UPDATE executor_safe_flatten_legs
                SET state = ?, broker_order_id = COALESCE(broker_order_id, ?),
                    last_run_id = ?, updated_at = ?, verified_at = ?
                WHERE leg_id = ? AND state = 'accepted'
                """,
                (
                    target_state.value,
                    clean_broker_id,
                    clean_run_id,
                    timestamp,
                    timestamp if verified else None,
                    clean_leg_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten leg changed concurrently"
                )
            self._append_safe_flatten_leg_event(
                connection,
                leg_id=clean_leg_id,
                run_id=clean_run_id,
                event_type="broker_verified" if verified else "broker_rejected",
                from_state=ExecutorSafeFlattenLegState.ACCEPTED,
                to_state=target_state,
                metadata=clean_evidence,
                timestamp=timestamp,
            )
            operation = self._safe_flatten_by_id(connection, leg.operation_id)
            self._touch_safe_flatten_operation(
                connection,
                operation=operation,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                event_type="leg_verified" if verified else "leg_rejected",
                metadata={
                    "kind": leg.kind.value,
                    "leg_id": clean_leg_id,
                    "verified": verified,
                },
                timestamp=timestamp,
            )
            updated = self._safe_flatten_leg_by_id(connection, clean_leg_id)
            self._validate_safe_flatten_leg_history(connection, updated)
            connection.commit()
            return updated
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to verify safe-flatten leg"
            ) from exc
        finally:
            connection.close()

    def resolve_safe_flatten_unknown_from_broker_observation(
        self,
        *,
        leg_id: str,
        client_order_id: str,
        broker_order_id: str,
        observed_status: str,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
    ) -> ExecutorSafeFlattenRecord:
        """Atomically recover command, leg and workflow from broker evidence."""

        clean_leg_id = _validated_request_id(leg_id, label="leg_id")
        clean_client_order_id = _validated_text(
            client_order_id,
            label="client_order_id",
        )
        clean_broker_order_id = _validated_text(
            broker_order_id,
            label="broker_order_id",
        )
        clean_status = _validated_reason_code(observed_status)
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=(ExecutorRunState.RECOVERING, ExecutorRunState.BLOCKED),
            )
            leg = self._safe_flatten_leg_by_id(connection, clean_leg_id)
            operation = self._safe_flatten_by_id(connection, leg.operation_id)
            command = self._command_by_id(connection, clean_leg_id)
            self._validate_command_history(connection, command)
            self._validate_safe_flatten_leg_history(connection, leg)
            self._validate_safe_flatten_operation_history(connection, operation)
            self._validate_control_history(connection)
            control = self._control_from_row(
                connection.execute(
                    "SELECT * FROM executor_control_state WHERE singleton = 1"
                ).fetchone()
            )
            expected_resume_state = (
                ExecutorSafeFlattenState.CANCELING
                if leg.kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER
                else ExecutorSafeFlattenState.CLOSING
            )
            if (
                leg.state is not ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN
                or operation.state is not ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN
                or operation.resume_state is not expected_resume_state
                or command.state is not ExecutorCommandState.OUTCOME_UNKNOWN
                or not control.kill_switch_active
                or control.generation < operation.control_generation
            ):
                raise ExecutorCommandTransitionError(
                    "safe-flatten recovery state is not ambiguous"
                )
            evidence = _validated_reconciliation_evidence(
                {
                    "source": "broker_order",
                    "client_order_id": clean_client_order_id,
                    "broker_order_id": clean_broker_order_id,
                    "observed_status": clean_status,
                    "observed_at": timestamp,
                    "command_fingerprint_sha256": command.fingerprint_sha256,
                    "intent_fingerprint_sha256": _business_intent_fingerprint(command),
                }
            )
            self._validate_reconciliation_evidence_for_command(
                command,
                evidence,
                target=ExecutorCommandState.COMPLETED,
            )
            result = _recovered_order_result(command, evidence)
            outcome = {"ok": True, "payload": result, "error": None}
            outcome_json = _canonical_json(
                outcome,
                max_bytes=MAX_COMMAND_OUTCOME_BYTES,
                label="command outcome",
            )
            outcome_sha256 = _sha256_text(outcome_json)
            cursor = connection.execute(
                """
                UPDATE executor_commands
                SET state = 'completed', outcome_json = ?, outcome_sha256 = ?,
                    last_run_id = ?, updated_at = ?
                WHERE request_id = ? AND state = 'outcome_unknown'
                """,
                (
                    outcome_json,
                    outcome_sha256,
                    clean_run_id,
                    timestamp,
                    clean_leg_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten recovered command changed concurrently"
                )
            self._append_command_event(
                connection,
                request_id=clean_leg_id,
                run_id=clean_run_id,
                event_type="reconciled_completed",
                from_state=ExecutorCommandState.OUTCOME_UNKNOWN,
                to_state=ExecutorCommandState.COMPLETED,
                metadata_json=_canonical_json(
                    {"evidence": evidence},
                    max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                    label="command metadata",
                ),
                outcome_sha256=outcome_sha256,
                timestamp=timestamp,
            )
            resume_state = operation.resume_state
            cursor = connection.execute(
                """
                UPDATE executor_safe_flatten_legs
                SET state = 'accepted', broker_order_id = ?,
                    last_run_id = ?, updated_at = ?
                WHERE leg_id = ? AND state = 'outcome_unknown'
                """,
                (
                    clean_broker_order_id,
                    clean_run_id,
                    timestamp,
                    clean_leg_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten recovered leg changed concurrently"
                )
            self._append_safe_flatten_leg_event(
                connection,
                leg_id=clean_leg_id,
                run_id=clean_run_id,
                event_type="broker_first_recovered",
                from_state=ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN,
                to_state=ExecutorSafeFlattenLegState.ACCEPTED,
                metadata={
                    "broker_order_id_present": True,
                    "request_id": clean_leg_id,
                },
                timestamp=timestamp,
            )
            version = operation.state_version + 1
            cursor = connection.execute(
                """
                UPDATE executor_safe_flatten_operations
                SET state = ?, state_version = ?, resume_state = NULL,
                    last_error_code = NULL, last_run_id = ?,
                    last_fence_epoch = ?, last_policy_sha256 = ?, updated_at = ?
                WHERE operation_id = ? AND state = 'blocked_outcome_unknown'
                  AND state_version = ?
                """,
                (
                    resume_state.value,
                    version,
                    clean_run_id,
                    clean_epoch,
                    clean_policy,
                    timestamp,
                    operation.operation_id,
                    operation.state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError(
                    "safe-flatten recovered operation changed concurrently"
                )
            self._append_safe_flatten_operation_event(
                connection,
                operation_id=operation.operation_id,
                run_id=clean_run_id,
                event_type="broker_first_recovered",
                from_state=ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
                to_state=resume_state,
                state_version=version,
                metadata={
                    "leg_id": clean_leg_id,
                    "request_id": clean_leg_id,
                    "retry_performed": False,
                },
                timestamp=timestamp,
            )
            updated = self._safe_flatten_by_id(connection, operation.operation_id)
            self._validate_command_history(
                connection,
                self._command_by_id(connection, clean_leg_id),
            )
            self._validate_safe_flatten_leg_history(
                connection,
                self._safe_flatten_leg_by_id(connection, clean_leg_id),
            )
            self._validate_safe_flatten_operation_history(connection, updated)
            connection.commit()
            return updated
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to recover ambiguous safe-flatten operation"
            ) from exc
        finally:
            connection.close()

    def full_audit(self) -> None:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN")
            self._validate_schema(connection, full=True)
            connection.commit()
        finally:
            connection.close()

    def _transition_command_normal(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        expected: Sequence[ExecutorCommandState],
        target: ExecutorCommandState,
        event_type: str,
        outcome: Mapping[str, object] | None,
        metadata: Mapping[str, object],
        required_operation: str | None = None,
        forbid_prior_same_run_event_type: str | None = None,
    ) -> ExecutorCommandRecord:
        return self._transition_command(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            allowed_run_states=(ExecutorRunState.READY,),
            expected=expected,
            target=target,
            event_type=event_type,
            outcome=outcome,
            metadata=metadata,
            require_original_context=True,
            required_operation=required_operation,
            forbid_prior_same_run_event_type=forbid_prior_same_run_event_type,
        )

    def _transition_command_recovery(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        expected: Sequence[ExecutorCommandState],
        target: ExecutorCommandState,
        event_type: str,
        outcome: Mapping[str, object],
        evidence: Mapping[str, object],
    ) -> ExecutorCommandRecord:
        clean_evidence = _validated_reconciliation_evidence(evidence)
        return self._transition_command(
            request_id,
            run_id=run_id,
            fence_epoch=fence_epoch,
            policy_sha256=policy_sha256,
            allowed_run_states=(ExecutorRunState.RECOVERING, ExecutorRunState.BLOCKED),
            expected=expected,
            target=target,
            event_type=event_type,
            outcome=outcome,
            metadata={"evidence": clean_evidence},
            require_original_context=False,
            recovery_evidence=clean_evidence,
        )

    def _transition_command(
        self,
        request_id: str,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        allowed_run_states: Sequence[ExecutorRunState],
        expected: Sequence[ExecutorCommandState],
        target: ExecutorCommandState,
        event_type: str,
        outcome: Mapping[str, object] | None,
        metadata: Mapping[str, object],
        require_original_context: bool,
        recovery_evidence: Mapping[str, object] | None = None,
        require_control_not_applied: bool = False,
        required_operation: str | None = None,
        forbid_prior_same_run_event_type: str | None = None,
    ) -> ExecutorCommandRecord:
        clean_id = _validated_request_id(request_id)
        clean_run_id = _validated_request_id(run_id, label="run_id")
        clean_epoch = _validated_fence_epoch(fence_epoch)
        clean_policy = _validated_sha256(policy_sha256, label="policy_sha256")
        metadata_json = _canonical_json(
            _normalize_json_object(metadata, location="command metadata"),
            max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            label="command metadata",
        )
        outcome_json = None
        outcome_sha256 = None
        if outcome is not None:
            normalized_outcome = _normalize_json_object(outcome, location="outcome")
            _validated_outcome(normalized_outcome, target=target)
            outcome_json = _canonical_json(
                normalized_outcome,
                max_bytes=MAX_COMMAND_OUTCOME_BYTES,
                label="command outcome",
            )
            outcome_sha256 = _sha256_text(outcome_json)
        elif target in {ExecutorCommandState.COMPLETED, ExecutorCommandState.REJECTED}:
            raise ExecutorCommandTransitionError("terminal command requires an outcome")
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validated_run_context(
                connection,
                run_id=clean_run_id,
                fence_epoch=clean_epoch,
                policy_sha256=clean_policy,
                allowed_states=allowed_run_states,
            )
            current = self._command_by_id(connection, clean_id)
            self._validate_command_history(connection, current)
            if required_operation is not None and current.operation != required_operation:
                raise ExecutorCommandTransitionError(
                    "executor command operation does not match the audited transition"
                )
            if forbid_prior_same_run_event_type is not None:
                clean_event_type = _validated_reason_code(
                    forbid_prior_same_run_event_type
                )
                prior = connection.execute(
                    """
                    SELECT 1 FROM executor_command_events
                    WHERE request_id = ? AND run_id = ? AND event_type = ?
                    LIMIT 1
                    """,
                    (clean_id, clean_run_id, clean_event_type),
                ).fetchone()
                if prior is not None:
                    raise ExecutorCommandTransitionError(
                        "executor command exhausted its same-run pre-dispatch retry"
                    )
            if recovery_evidence is not None:
                self._validate_reconciliation_evidence_for_command(
                    current,
                    recovery_evidence,
                    target=target,
                )
            if require_control_not_applied:
                if current.operation != "latch_kill_switch":
                    raise ExecutorCommandTransitionError(
                        "atomic control recovery requires a kill switch command"
                    )
                self._validate_control_history(connection)
                control = self._control_from_row(
                    connection.execute(
                        "SELECT * FROM executor_control_state WHERE singleton = 1"
                    ).fetchone()
                )
                if control.last_request_id == current.request_id:
                    raise ExecutorCommandTransitionError(
                        "durable control state shows that the command was applied"
                    )
            if current.state not in expected:
                raise ExecutorCommandTransitionError(
                    f"executor command is {current.state.value}, expected {[item.value for item in expected]}"
                )
            if target is ExecutorCommandState.DISPATCHING:
                active_flatten = connection.execute(
                    """
                    SELECT operation_id FROM executor_safe_flatten_operations
                    WHERE state <> 'flat_latched' LIMIT 1
                    """
                ).fetchone()
                leg = connection.execute(
                    "SELECT leg_id FROM executor_safe_flatten_legs WHERE leg_id = ?",
                    (current.request_id,),
                ).fetchone()
                if active_flatten is not None and leg is None:
                    raise ExecutorRunNotReadyError(
                        "safe-flatten blocks external command dispatch"
                    )
                self._validate_control_history(connection)
                control = self._control_from_row(
                    connection.execute(
                        "SELECT * FROM executor_control_state WHERE singleton = 1"
                    ).fetchone()
                )
                if control.kill_switch_active and _is_nonreducing_submit(
                    current.operation,
                    current.payload,
                ):
                    raise ExecutorRunNotReadyError(
                        "durable kill switch blocks non-reducing dispatch"
                    )
            if target not in _COMMAND_TRANSITIONS[current.state]:
                raise ExecutorCommandTransitionError(
                    f"executor command transition {current.state.value}->{target.value} is forbidden"
                )
            if require_original_context and (
                current.fence_epoch != clean_epoch
                or current.policy_sha256 != clean_policy
                or current.last_run_id != clean_run_id
            ):
                raise ExecutorCommandTransitionError(
                    "executor command context changed before dispatch completion"
                )
            cursor = connection.execute(
                """
                UPDATE executor_commands
                SET state = ?, outcome_json = ?, outcome_sha256 = ?,
                    last_run_id = ?, updated_at = ?
                WHERE request_id = ? AND state = ?
                """,
                (
                    target.value,
                    outcome_json,
                    outcome_sha256,
                    clean_run_id,
                    timestamp,
                    clean_id,
                    current.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutorCommandStorageError("executor command changed concurrently")
            self._append_command_event(
                connection,
                request_id=clean_id,
                run_id=clean_run_id,
                event_type=event_type,
                from_state=current.state,
                to_state=target,
                metadata_json=metadata_json,
                outcome_sha256=outcome_sha256,
                timestamp=timestamp,
            )
            updated = self._command_by_id(connection, clean_id)
            self._validate_command_history(connection, updated)
            connection.commit()
            return updated
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "failed to update executor command; mutations must remain blocked"
            ) from exc
        finally:
            connection.close()

    def _validated_run_context(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        allowed_states: Sequence[ExecutorRunState],
    ) -> ExecutorRunRecord:
        run = self._run_by_id(connection, run_id)
        self._validate_active_run_uniqueness(connection)
        self._validate_run_history(connection, run)
        if (
            run.account_scope_sha256 != self.account_scope_sha256
            or run.fence_epoch != fence_epoch
            or run.policy_sha256 != policy_sha256
            or run.pid != os.getpid()
        ):
            raise ExecutorRunNotReadyError("executor run authority context does not match")
        if run.state not in allowed_states:
            raise ExecutorRunNotReadyError(
                f"executor run state {run.state.value} does not authorize this operation"
            )
        return run

    def _validate_reconciliation_evidence_for_command(
        self,
        record: ExecutorCommandRecord,
        evidence: Mapping[str, object],
        *,
        target: ExecutorCommandState,
    ) -> None:
        if target is not ExecutorCommandState.COMPLETED:
            raise ExecutorCommandTransitionError(
                "ambiguous broker mutations require a positive broker observation"
            )
        if record.operation not in {"submit_order", "cancel_order"}:
            raise ExecutorCommandTransitionError(
                "broker order evidence cannot resolve this executor operation"
            )
        if evidence["source"] != "broker_order":
            raise ExecutorCommandTransitionError(
                "ambiguous mutation recovery requires a fresh broker order"
            )
        if evidence["command_fingerprint_sha256"] != record.fingerprint_sha256:
            raise ExecutorCommandCollisionError(
                "reconciliation evidence belongs to a different executor command"
            )
        if evidence["intent_fingerprint_sha256"] != _business_intent_fingerprint(record):
            raise ExecutorCommandCollisionError(
                "reconciliation evidence belongs to a different order intent"
            )

        observed = _parse_timestamp(str(evidence["observed_at"]))
        command_updated = _parse_timestamp(record.updated_at)
        now = _parse_timestamp(self._timestamp())
        if observed < command_updated:
            raise ExecutorCommandTransitionError(
                "reconciliation evidence predates the ambiguous command state"
            )
        age_seconds = (now - observed).total_seconds()
        if age_seconds > MAX_RECOVERY_EVIDENCE_AGE_SECONDS:
            raise ExecutorCommandTransitionError("reconciliation evidence is stale")
        if age_seconds < -MAX_RECOVERY_EVIDENCE_FUTURE_SKEW_SECONDS:
            raise ExecutorCommandTransitionError("reconciliation evidence is from the future")

        status = str(evidence["observed_status"])
        broker_order_id = evidence["broker_order_id"]
        client_order_id = evidence["client_order_id"]
        if status not in _KNOWN_BROKER_ORDER_STATUSES or broker_order_id is None:
            raise ExecutorCommandTransitionError(
                "reconciliation did not observe a concrete broker order"
            )
        if record.operation == "submit_order":
            order = _command_order_payload(record)
            if client_order_id != order["client_order_id"]:
                raise ExecutorCommandCollisionError(
                    "reconciliation broker order has a different client_order_id"
                )
            return

        order_id, expected_client_order_id = _command_cancel_target(record)
        if status not in _CANCEL_RECOVERY_TERMINAL_STATUSES:
            raise ExecutorCommandTransitionError(
                "cancel outcome remains ambiguous at the observed broker status"
            )
        if order_id is not None and broker_order_id != order_id:
            raise ExecutorCommandCollisionError(
                "reconciliation broker order has a different order_id"
            )
        if expected_client_order_id is not None and client_order_id != expected_client_order_id:
            raise ExecutorCommandCollisionError(
                "reconciliation broker order has a different client_order_id"
            )

    def _initialize(self) -> None:
        _validate_private_directory(self.path.parent)
        created = False
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        except OSError as exc:
            raise ExecutorCommandStorageError("cannot create executor authority ledger") from exc
        else:
            os.close(descriptor)
            created = True
        _validate_private_file(self.path)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            if created:
                connection.execute("BEGIN IMMEDIATE")
                self._create_schema(connection)
                connection.commit()
            else:
                self._migrate_v3_if_required(connection)
            self._validate_schema(connection, full=True)
        except ExecutorCommandJournalError:
            if connection is not None:
                _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                _rollback_quietly(connection)
            raise ExecutorCommandStorageError("executor authority ledger initialization failed") from exc
        finally:
            if connection is not None:
                connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        _validate_private_directory(self.path.parent)
        stat_identity = _private_file_identity(self.path)
        expected = getattr(self, "_identity", None)
        if expected is not None and (stat_identity[0], stat_identity[1]) != (
            expected.device,
            expected.inode,
        ):
            raise ExecutorCommandStorageError("executor ledger file was replaced")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            self._validate_schema(connection, full=False)
            if expected is not None:
                identity = self._identity_from_connection(connection, stat_identity=stat_identity)
                if identity != expected:
                    raise ExecutorCommandStorageError("executor ledger identity changed")
            return connection
        except ExecutorCommandJournalError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise ExecutorCommandStorageError("executor authority ledger is unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms:d}")
            connection.execute("PRAGMA foreign_keys = ON")
            journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            connection.execute("PRAGMA synchronous = FULL")
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            if journal_mode != "wal" or foreign_keys != 1 or synchronous != 2:
                raise ExecutorCommandStorageError(
                    "executor ledger durability settings are unavailable"
                )
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        for _kind, _name, statement in _schema_objects():
            connection.execute(statement)
        timestamp = self._timestamp()
        connection.execute(
            """
            INSERT INTO executor_command_metadata (
                singleton, schema_version, ledger_id, account_scope_sha256, created_at
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (
                COMMAND_SCHEMA_VERSION,
                uuid.uuid4().hex,
                self.account_scope_sha256,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO executor_control_state (
                singleton, kill_switch_active, reason_code, generation,
                last_request_id, updated_at
            ) VALUES (1, 0, NULL, 0, NULL, ?)
            """,
            (timestamp,),
        )

    def _migrate_v3_if_required(self, connection: sqlite3.Connection) -> None:
        """Apply the sole supported additive migration after exact v3 audit."""

        try:
            rows = connection.execute(
                "SELECT schema_version FROM executor_command_metadata WHERE singleton = 1"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ExecutorCommandSchemaError(
                "executor ledger schema version is unavailable"
            ) from exc
        if len(rows) != 1:
            raise ExecutorCommandSchemaError("executor ledger metadata row is invalid")
        try:
            version = _strict_integer(rows[0][0])
        except (TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError(
                "executor ledger schema version is invalid"
            ) from exc
        if version == COMMAND_SCHEMA_VERSION:
            return
        if version != 3:
            raise ExecutorCommandSchemaError(
                "executor ledger schema version is unsupported"
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            expected = {
                (kind, name): _normalize_sql(statement)
                for kind, name, statement in _schema_objects_v3()
            }
            actual = _schema_definitions(connection)
            if actual != expected:
                raise ExecutorCommandSchemaError(
                    "executor v3 ledger schema definitions are missing or unexpected"
                )
            metadata = connection.execute(
                """
                SELECT singleton, schema_version, ledger_id,
                       account_scope_sha256, created_at
                FROM executor_command_metadata
                """
            ).fetchall()
            if (
                len(metadata) != 1
                or _strict_integer(metadata[0][0]) != 1
                or _strict_integer(metadata[0][1]) != 3
            ):
                raise ExecutorCommandSchemaError(
                    "executor v3 ledger metadata row is invalid"
                )
            _validated_request_id(metadata[0][2], label="ledger_id")
            if (
                _validated_sha256(
                    metadata[0][3],
                    label="account_scope_sha256",
                )
                != self.account_scope_sha256
            ):
                raise ExecutorCommandSchemaError(
                    "executor ledger account scope does not match"
                )
            _validated_timestamp(metadata[0][4])
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ExecutorCommandSchemaError(
                    "executor v3 ledger foreign keys are invalid"
                )
            integrity = connection.execute("PRAGMA quick_check").fetchall()
            if len(integrity) != 1 or str(integrity[0][0]).lower() != "ok":
                raise ExecutorCommandSchemaError(
                    "executor v3 ledger integrity check failed"
                )
            self._validate_active_run_uniqueness(connection)
            for row in connection.execute(
                "SELECT * FROM executor_runs ORDER BY fence_epoch"
            ):
                self._validate_run_history(connection, self._run_from_row(row))
            for row in connection.execute(
                "SELECT * FROM executor_commands ORDER BY created_at"
            ):
                self._validate_command_history(connection, self._command_from_row(row))
            self._validate_control_history(connection)

            for _kind, name, statement in _schema_objects():
                if name.startswith("executor_safe_flatten"):
                    connection.execute(statement)
            connection.execute(
                "UPDATE executor_command_metadata SET schema_version = ? WHERE singleton = 1",
                (COMMAND_SCHEMA_VERSION,),
            )
            self._validate_schema(connection, full=True)
            connection.commit()
        except ExecutorCommandJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise ExecutorCommandStorageError(
                "executor authority ledger v3 migration failed"
            ) from exc

    def _validate_schema(self, connection: sqlite3.Connection, *, full: bool) -> None:
        expected = {
            (kind, name): _normalize_sql(statement)
            for kind, name, statement in _schema_objects()
        }
        actual = _schema_definitions(connection)
        if actual != expected:
            raise ExecutorCommandSchemaError(
                "executor ledger schema definitions are missing or unexpected"
            )
        metadata = connection.execute(
            """
            SELECT singleton, schema_version, ledger_id, account_scope_sha256, created_at
            FROM executor_command_metadata
            """
        ).fetchall()
        try:
            if len(metadata) != 1 or _strict_integer(metadata[0][0]) != 1:
                raise ExecutorCommandSchemaError("executor ledger metadata row is invalid")
            if _strict_integer(metadata[0][1]) != COMMAND_SCHEMA_VERSION:
                raise ExecutorCommandSchemaError(
                    "executor ledger schema version is unsupported"
                )
            _validated_request_id(metadata[0][2], label="ledger_id")
            stored_scope = _validated_sha256(
                metadata[0][3],
                label="account_scope_sha256",
            )
            _validated_timestamp(metadata[0][4])
        except ExecutorCommandJournalError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError(
                "executor ledger metadata row is invalid"
            ) from exc
        if stored_scope != self.account_scope_sha256:
            raise ExecutorCommandSchemaError("executor ledger account scope does not match")
        foreign_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_violations:
            raise ExecutorCommandSchemaError("executor ledger foreign keys are invalid")
        if not full:
            return
        integrity = connection.execute("PRAGMA quick_check").fetchall()
        if len(integrity) != 1 or str(integrity[0][0]).lower() != "ok":
            raise ExecutorCommandSchemaError("executor ledger integrity check failed")
        self._validate_active_run_uniqueness(connection)
        for row in connection.execute("SELECT * FROM executor_runs ORDER BY fence_epoch"):
            self._validate_run_history(connection, self._run_from_row(row))
        for row in connection.execute("SELECT * FROM executor_commands ORDER BY created_at"):
            self._validate_command_history(connection, self._command_from_row(row))
        self._validate_control_history(connection)
        self._validate_safe_flatten_history(connection)

    @staticmethod
    def _validate_active_run_uniqueness(connection: sqlite3.Connection) -> None:
        active = connection.execute(
            """
            SELECT run_id FROM executor_runs
            WHERE state IN ('starting','recovering','ready','blocked','draining')
            LIMIT 2
            """
        ).fetchall()
        if len(active) > 1:
            raise ExecutorCommandSchemaError("multiple executor runs claim active authority")

    def _validate_run_history(
        self,
        connection: sqlite3.Connection,
        record: ExecutorRunRecord,
    ) -> None:
        events = tuple(
            self._run_event_from_row(row)
            for row in connection.execute(
                "SELECT * FROM executor_run_events WHERE run_id = ? ORDER BY sequence",
                (record.run_id,),
            ).fetchall()
        )
        if not events:
            raise ExecutorCommandSchemaError("executor run has no event history")
        first = events[0]
        if (
            first.from_state is not None
            or first.to_state is not ExecutorRunState.STARTING
            or first.run_id != record.run_id
            or first.created_at != record.started_at
        ):
            raise ExecutorCommandSchemaError("executor run initial event is invalid")
        state = ExecutorRunState.STARTING
        previous_timestamp = _parse_timestamp(first.created_at)
        for event in events[1:]:
            event_timestamp = _parse_timestamp(event.created_at)
            if (
                event.run_id != record.run_id
                or event.from_state is not state
                or event.to_state not in _RUN_TRANSITIONS[state]
                or event_timestamp < previous_timestamp
            ):
                raise ExecutorCommandSchemaError("executor run event chain is invalid")
            state = event.to_state
            previous_timestamp = event_timestamp
        terminal = record.state in {
            ExecutorRunState.STOPPED,
            ExecutorRunState.CRASHED,
            ExecutorRunState.FAILED,
        }
        if (
            state is not record.state
            or events[-1].created_at != record.updated_at
            or (terminal and record.ended_at != record.updated_at)
            or (not terminal and record.ended_at is not None)
        ):
            raise ExecutorCommandSchemaError("executor run state disagrees with history")

    def _validate_command_history(
        self,
        connection: sqlite3.Connection,
        record: ExecutorCommandRecord,
    ) -> None:
        events = tuple(
            self._command_event_from_row(row)
            for row in connection.execute(
                "SELECT * FROM executor_command_events WHERE request_id = ? ORDER BY sequence",
                (record.request_id,),
            ).fetchall()
        )
        if not events:
            raise ExecutorCommandSchemaError("executor command has no event history")
        first = events[0]
        if (
            first.event_type != "recorded"
            or first.from_state is not None
            or first.to_state is not ExecutorCommandState.RECORDED
            or first.outcome_sha256 is not None
            or first.run_id != record.created_run_id
        ):
            raise ExecutorCommandSchemaError("executor command initial event is invalid")
        state = ExecutorCommandState.RECORDED
        outcome_sha256: str | None = None
        last_run_id = first.run_id
        predispatch_requeue_runs: set[str] = set()
        previous_timestamp = _parse_timestamp(first.created_at)
        for event in events[1:]:
            event_timestamp = _parse_timestamp(event.created_at)
            if (
                event.from_state is not state
                or event.to_state not in _COMMAND_TRANSITIONS[state]
                or event_timestamp < previous_timestamp
            ):
                raise ExecutorCommandSchemaError("executor command event chain is invalid")
            if event.to_state is ExecutorCommandState.RECORDED:
                expected_reason = {
                    "submit_order": "submit_deferred",
                    "cancel_order": "cancel_deferred",
                }.get(record.operation)
                if (
                    state is not ExecutorCommandState.DISPATCHING
                    or event.event_type != "dispatch_proven_not_started"
                    or event.run_id in predispatch_requeue_runs
                    or expected_reason is None
                    or event.metadata
                    != {
                        "classification": "structured_not_dispatched",
                        "operation": record.operation,
                        "reason": expected_reason,
                        "retry_scope": "same_run_once",
                    }
                ):
                    raise ExecutorCommandSchemaError(
                        "executor pre-dispatch requeue history is invalid"
                    )
                predispatch_requeue_runs.add(event.run_id)
            if event.to_state.value in _TERMINAL_COMMAND_STATES:
                if event.outcome_sha256 is None:
                    raise ExecutorCommandSchemaError("terminal command event lacks outcome hash")
                outcome_sha256 = event.outcome_sha256
            elif event.outcome_sha256 is not None:
                raise ExecutorCommandSchemaError("nonterminal command event has outcome hash")
            state = event.to_state
            last_run_id = event.run_id
            previous_timestamp = event_timestamp
        if (
            state is not record.state
            or outcome_sha256 != record.outcome_sha256
            or last_run_id != record.last_run_id
            or events[-1].created_at != record.updated_at
        ):
            raise ExecutorCommandSchemaError("executor command state disagrees with history")

    def _validate_control_history(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT * FROM executor_control_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ExecutorCommandSchemaError("executor control state is missing")
        state = self._control_from_row(row)
        events = connection.execute(
            "SELECT * FROM executor_control_events ORDER BY generation"
        ).fetchall()
        if not state.kill_switch_active:
            metadata = connection.execute(
                "SELECT created_at FROM executor_command_metadata WHERE singleton = 1"
            ).fetchone()
            if (
                state.generation != 0
                or events
                or state.reason_code is not None
                or state.last_request_id is not None
                or metadata is None
                or state.updated_at != _validated_timestamp(metadata["created_at"])
            ):
                raise ExecutorCommandSchemaError("inactive kill switch history is invalid")
            return
        if len(events) != state.generation or not events:
            raise ExecutorCommandSchemaError("kill switch generation history is invalid")
        previous_timestamp: datetime | None = None
        for expected_generation, event in enumerate(events, start=1):
            try:
                generation = int(event["generation"])
                event_type = str(event["event_type"])
                reason = _validated_reason_code(event["reason_code"])
                request_id = _validated_request_id(event["request_id"])
                run_id = _validated_request_id(event["run_id"], label="run_id")
                created_at = _validated_timestamp(event["created_at"])
            except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
                raise ExecutorCommandSchemaError(
                    "executor kill switch event is invalid"
                ) from exc
            if generation != expected_generation or event_type != "kill_switch_latched":
                raise ExecutorCommandSchemaError("kill switch event chain is invalid")
            parsed_timestamp = _parse_timestamp(created_at)
            if previous_timestamp is not None and parsed_timestamp < previous_timestamp:
                raise ExecutorCommandSchemaError("kill switch event timestamps are invalid")
            previous_timestamp = parsed_timestamp
            command = self._command_by_id(connection, request_id)
            self._validate_command_history(connection, command)
            if command.operation == "latch_kill_switch":
                command_matches = (
                    command.payload == {"reason_code": reason}
                    and command.result
                    == {
                        "generation": generation,
                        "kill_switch_active": True,
                        "reason_code": reason,
                    }
                )
            elif command.operation == "start_safe_flatten":
                result = command.result
                command_matches = (
                    reason == "paper_safe_flatten_confirmed"
                    and command.payload == {"operation_id": request_id}
                    and isinstance(result, dict)
                    and result
                    == {
                        "kill_switch_active": True,
                        "operation_id": request_id,
                        "state": ExecutorSafeFlattenState.LATCHED.value,
                        "state_version": 1,
                    }
                )
            else:
                command_matches = False
            if (
                not command_matches
                or command.state is not ExecutorCommandState.COMPLETED
                or command.last_run_id != run_id
            ):
                raise ExecutorCommandSchemaError(
                    "kill switch event is not bound to its completed command"
                )
        last = events[-1]
        if (
            int(last["generation"]) != state.generation
            or str(last["event_type"]) != "kill_switch_latched"
            or str(last["reason_code"]) != state.reason_code
            or str(last["request_id"]) != state.last_request_id
            or str(last["created_at"]) != state.updated_at
        ):
            raise ExecutorCommandSchemaError("kill switch state disagrees with history")

    def _validate_safe_flatten_history(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        operation_rows = connection.execute(
            "SELECT * FROM executor_safe_flatten_operations ORDER BY created_at, operation_id"
        ).fetchall()
        active = 0
        for row in operation_rows:
            operation = self._safe_flatten_from_row(row)
            self._validate_safe_flatten_operation_history(connection, operation)
            if operation.state is not ExecutorSafeFlattenState.FLAT_LATCHED:
                active += 1
                external_pending = connection.execute(
                    """
                    SELECT command.request_id
                    FROM executor_commands AS command
                    LEFT JOIN executor_safe_flatten_legs AS leg
                      ON leg.leg_id = command.request_id
                    WHERE command.state IN ('recorded','dispatching','outcome_unknown')
                      AND (leg.operation_id IS NULL OR leg.operation_id <> ?)
                    LIMIT 1
                    """,
                    (operation.operation_id,),
                ).fetchone()
                if external_pending is not None:
                    raise ExecutorCommandSchemaError(
                        "safe-flatten overlaps an external pending command"
                    )
        if active > 1:
            raise ExecutorCommandSchemaError(
                "multiple safe-flatten operations claim active authority"
            )
        for row in connection.execute(
            "SELECT * FROM executor_safe_flatten_legs ORDER BY operation_id, ordinal, leg_id"
        ):
            self._validate_safe_flatten_leg_history(
                connection,
                self._safe_flatten_leg_from_row(row),
            )
        serial_violations = connection.execute(
            """
            SELECT operation_id, COUNT(*) AS pending
            FROM executor_safe_flatten_legs
            WHERE state IN ('dispatching','accepted','outcome_unknown')
            GROUP BY operation_id HAVING COUNT(*) > 1
            """
        ).fetchall()
        if serial_violations:
            raise ExecutorCommandSchemaError(
                "safe-flatten has concurrent broker legs"
            )

    def _validate_safe_flatten_operation_history(
        self,
        connection: sqlite3.Connection,
        record: ExecutorSafeFlattenRecord,
    ) -> None:
        events = tuple(
            self._safe_flatten_event_from_row(row)
            for row in connection.execute(
                """
                SELECT * FROM executor_safe_flatten_operation_events
                WHERE operation_id = ? ORDER BY sequence
                """,
                (record.operation_id,),
            ).fetchall()
        )
        if not events:
            raise ExecutorCommandSchemaError(
                "safe-flatten operation has no event history"
            )
        first = events[0]
        if (
            first.operation_id != record.operation_id
            or first.from_state is not None
            or first.to_state is not ExecutorSafeFlattenState.LATCHED
            or first.state_version != 1
            or first.run_id != record.created_run_id
            or first.created_at != record.created_at
            or first.event_type != "workflow_started"
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten initial event is invalid"
            )
        state = ExecutorSafeFlattenState.LATCHED
        version = 1
        last_run_id = first.run_id
        previous_timestamp = _parse_timestamp(first.created_at)
        for event in events[1:]:
            event_timestamp = _parse_timestamp(event.created_at)
            if (
                event.operation_id != record.operation_id
                or event.from_state is not state
                or event.to_state not in _SAFE_FLATTEN_TRANSITIONS[state]
                or event.state_version != version + 1
                or event_timestamp < previous_timestamp
            ):
                raise ExecutorCommandSchemaError(
                    "safe-flatten operation event chain is invalid"
                )
            state = event.to_state
            version = event.state_version
            last_run_id = event.run_id
            previous_timestamp = event_timestamp
        if (
            state is not record.state
            or version != record.state_version
            or last_run_id != record.last_run_id
            or events[-1].created_at != record.updated_at
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten operation state disagrees with history"
            )

        command = self._command_by_id(connection, record.initiating_request_id)
        self._validate_command_history(connection, command)
        if (
            command.operation != "start_safe_flatten"
            or command.payload != {"operation_id": record.operation_id}
            or command.state is not ExecutorCommandState.COMPLETED
            or command.result
            != {
                "kill_switch_active": True,
                "operation_id": record.operation_id,
                "state": ExecutorSafeFlattenState.LATCHED.value,
                "state_version": 1,
            }
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten operation is not bound to its start command"
            )
        control = self._control_from_row(
            connection.execute(
                "SELECT * FROM executor_control_state WHERE singleton = 1"
            ).fetchone()
        )
        if (
            not control.kill_switch_active
            or control.generation < record.control_generation
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten operation lacks its durable kill switch"
            )
        active_legs = tuple(
            self._safe_flatten_leg_from_row(row)
            for row in connection.execute(
                """
                SELECT * FROM executor_safe_flatten_legs
                WHERE operation_id = ?
                  AND state IN ('dispatching','accepted','outcome_unknown')
                ORDER BY ordinal, leg_id
                """,
                (record.operation_id,),
            ).fetchall()
        )
        if len(active_legs) > 1:
            raise ExecutorCommandSchemaError(
                "safe-flatten has concurrent active legs"
            )
        if record.state is ExecutorSafeFlattenState.CANCELING and active_legs:
            if active_legs[0].kind is not ExecutorSafeFlattenLegKind.CANCEL_ORDER:
                raise ExecutorCommandSchemaError(
                    "safe-flatten cancel phase owns the wrong leg kind"
                )
        elif record.state is ExecutorSafeFlattenState.CLOSING and active_legs:
            if active_legs[0].kind is not ExecutorSafeFlattenLegKind.CLOSE_POSITION:
                raise ExecutorCommandSchemaError(
                    "safe-flatten close phase owns the wrong leg kind"
                )
        elif (
            record.state is not ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN
            and active_legs
            and record.state
            not in {
                ExecutorSafeFlattenState.CANCELING,
                ExecutorSafeFlattenState.CLOSING,
            }
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten active leg is outside a mutable phase"
            )
        if record.state is ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN:
            unknown = connection.execute(
                """
                SELECT leg_id FROM executor_safe_flatten_legs
                WHERE operation_id = ? AND state = 'outcome_unknown'
                """,
                (record.operation_id,),
            ).fetchall()
            if len(unknown) != 1:
                raise ExecutorCommandSchemaError(
                    "ambiguous safe-flatten operation lacks exactly one ambiguous leg"
                )
            unknown_leg = active_legs[0] if active_legs else None
            expected_resume = (
                ExecutorSafeFlattenState.CANCELING
                if unknown_leg is not None
                and unknown_leg.kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER
                else ExecutorSafeFlattenState.CLOSING
            )
            if unknown_leg is None or record.resume_state is not expected_resume:
                raise ExecutorCommandSchemaError(
                    "ambiguous safe-flatten resume phase disagrees with its leg"
                )
        if record.state in {
            ExecutorSafeFlattenState.CANCEL_CONFIRMED,
            ExecutorSafeFlattenState.CLOSING,
            ExecutorSafeFlattenState.FILLS_CONFIRMED,
            ExecutorSafeFlattenState.RECONCILING,
            ExecutorSafeFlattenState.FLAT_LATCHED,
        }:
            unverified_cancel = connection.execute(
                """
                SELECT leg_id FROM executor_safe_flatten_legs
                WHERE operation_id = ? AND kind = 'cancel_order'
                  AND state <> 'verified' LIMIT 1
                """,
                (record.operation_id,),
            ).fetchone()
            if unverified_cancel is not None:
                raise ExecutorCommandSchemaError(
                    "safe-flatten advanced past an unverified cancellation"
                )
        if record.state in {
            ExecutorSafeFlattenState.FILLS_CONFIRMED,
            ExecutorSafeFlattenState.RECONCILING,
            ExecutorSafeFlattenState.FLAT_LATCHED,
        }:
            unverified_close = connection.execute(
                """
                SELECT leg_id FROM executor_safe_flatten_legs
                WHERE operation_id = ? AND kind = 'close_position'
                  AND state <> 'verified' LIMIT 1
                """,
                (record.operation_id,),
            ).fetchone()
            if unverified_close is not None:
                raise ExecutorCommandSchemaError(
                    "safe-flatten advanced past an unverified close"
                )
        if record.state is ExecutorSafeFlattenState.FLAT_LATCHED:
            _validated_safe_flatten_final_evidence(events[-1].metadata)
            pending = connection.execute(
                """
                SELECT leg_id FROM executor_safe_flatten_legs
                WHERE operation_id = ? AND state <> 'verified' LIMIT 1
                """,
                (record.operation_id,),
            ).fetchone()
            if pending is not None:
                raise ExecutorCommandSchemaError(
                    "flat-latched workflow has an unverified leg"
                )

    def _validate_safe_flatten_leg_history(
        self,
        connection: sqlite3.Connection,
        record: ExecutorSafeFlattenLegRecord,
    ) -> None:
        operation = self._safe_flatten_by_id(connection, record.operation_id)
        events = tuple(
            self._safe_flatten_leg_event_from_row(row)
            for row in connection.execute(
                """
                SELECT * FROM executor_safe_flatten_leg_events
                WHERE leg_id = ? ORDER BY sequence
                """,
                (record.leg_id,),
            ).fetchall()
        )
        if not events:
            raise ExecutorCommandSchemaError(
                "safe-flatten leg has no event history"
            )
        first = events[0]
        if (
            first.leg_id != record.leg_id
            or first.from_state is not None
            or first.to_state is not ExecutorSafeFlattenLegState.DISPATCHING
            or first.run_id != record.created_run_id
            or first.created_at != record.created_at
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten leg initial event is invalid"
            )
        state = ExecutorSafeFlattenLegState.DISPATCHING
        last_run_id = first.run_id
        previous_timestamp = _parse_timestamp(first.created_at)
        for event in events[1:]:
            event_timestamp = _parse_timestamp(event.created_at)
            if (
                event.leg_id != record.leg_id
                or event.from_state is not state
                or event.to_state not in _SAFE_FLATTEN_LEG_TRANSITIONS[state]
                or event_timestamp < previous_timestamp
            ):
                raise ExecutorCommandSchemaError(
                    "safe-flatten leg event chain is invalid"
                )
            state = event.to_state
            last_run_id = event.run_id
            previous_timestamp = event_timestamp
        if (
            state is not record.state
            or last_run_id != record.last_run_id
            or events[-1].created_at != record.updated_at
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten leg state disagrees with history"
            )

        command = self._command_by_id(connection, record.leg_id)
        self._validate_command_history(connection, command)
        _validated_safe_flatten_leg_target(
            record.kind,
            record.target,
            command_operation=command.operation,
            command_payload=command.payload,
        )
        if command.operation != record.command_operation:
            raise ExecutorCommandSchemaError(
                "safe-flatten leg command operation is inconsistent"
            )
        dispatch_event = next(
            (
                event
                for event in events[1:]
                if event.event_type == "dispatch_completed"
            ),
            None,
        )
        if dispatch_event is not None:
            if command.result is None:
                raise ExecutorCommandSchemaError(
                    "safe-flatten dispatch result is missing"
                )
            try:
                _validated_safe_flatten_dispatch_result(
                    record,
                    result=command.result,
                    accepted=(
                        dispatch_event.to_state
                        is ExecutorSafeFlattenLegState.ACCEPTED
                    ),
                    broker_order_id=record.broker_order_id,
                )
            except ExecutorCommandJournalError as exc:
                raise ExecutorCommandSchemaError(
                    "safe-flatten dispatch result disagrees with its leg"
                ) from exc
        expected_command_states = {
            ExecutorSafeFlattenLegState.DISPATCHING: {
                ExecutorCommandState.DISPATCHING,
            },
            ExecutorSafeFlattenLegState.OUTCOME_UNKNOWN: {
                ExecutorCommandState.OUTCOME_UNKNOWN,
            },
            ExecutorSafeFlattenLegState.ACCEPTED: {
                ExecutorCommandState.COMPLETED,
            },
            ExecutorSafeFlattenLegState.VERIFIED: {
                ExecutorCommandState.COMPLETED,
            },
            ExecutorSafeFlattenLegState.REJECTED: {
                ExecutorCommandState.COMPLETED,
            },
        }
        if command.state not in expected_command_states[record.state]:
            raise ExecutorCommandSchemaError(
                "safe-flatten leg and command states disagree"
            )
        last_event = events[-1]
        if last_event.from_state is ExecutorSafeFlattenLegState.ACCEPTED:
            try:
                _validated_safe_flatten_leg_evidence(
                    record,
                    verified=(
                        last_event.to_state
                        is ExecutorSafeFlattenLegState.VERIFIED
                    ),
                    broker_order_id=record.broker_order_id,
                    evidence=last_event.metadata,
                )
            except ExecutorCommandJournalError as exc:
                raise ExecutorCommandSchemaError(
                    "safe-flatten leg verification evidence is invalid"
                ) from exc
        if (
            record.kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER
            and operation.state
            in {
                ExecutorSafeFlattenState.LATCHED,
            }
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten cancel leg predates the cancel phase"
            )

    def _read_identity_without_pin(self) -> ExecutorLedgerIdentity:
        stat_identity = _private_file_identity(self.path)
        connection = self._connect()
        try:
            self._validate_schema(connection, full=False)
            return self._identity_from_connection(connection, stat_identity=stat_identity)
        finally:
            connection.close()

    def _identity_from_connection(
        self,
        connection: sqlite3.Connection,
        *,
        stat_identity: tuple[int, int],
    ) -> ExecutorLedgerIdentity:
        row = connection.execute(
            """
            SELECT ledger_id, account_scope_sha256, created_at
            FROM executor_command_metadata WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise ExecutorCommandSchemaError("executor ledger identity is missing")
        return ExecutorLedgerIdentity(
            device=stat_identity[0],
            inode=stat_identity[1],
            ledger_id=_validated_request_id(row[0], label="ledger_id"),
            account_scope_sha256=_validated_sha256(
                row[1],
                label="account_scope_sha256",
            ),
            created_at=_validated_timestamp(row[2]),
        )

    def _run_by_id(self, connection: sqlite3.Connection, run_id: str) -> ExecutorRunRecord:
        row = connection.execute(
            "SELECT * FROM executor_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ExecutorCommandTransitionError("unknown executor run id")
        return self._run_from_row(row)

    def _command_by_id(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> ExecutorCommandRecord:
        row = connection.execute(
            "SELECT * FROM executor_commands WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise ExecutorCommandTransitionError("unknown executor request id")
        return self._command_from_row(row)

    def _safe_flatten_by_id(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> ExecutorSafeFlattenRecord:
        row = connection.execute(
            "SELECT * FROM executor_safe_flatten_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ExecutorCommandTransitionError(
                "unknown safe-flatten operation id"
            )
        return self._safe_flatten_from_row(row)

    def _safe_flatten_leg_by_id(
        self,
        connection: sqlite3.Connection,
        leg_id: str,
    ) -> ExecutorSafeFlattenLegRecord:
        row = connection.execute(
            "SELECT * FROM executor_safe_flatten_legs WHERE leg_id = ?",
            (leg_id,),
        ).fetchone()
        if row is None:
            raise ExecutorCommandTransitionError("unknown safe-flatten leg id")
        return self._safe_flatten_leg_from_row(row)

    def _run_from_row(self, row: sqlite3.Row) -> ExecutorRunRecord:
        try:
            started = _validated_timestamp(row["started_at"])
            updated = _validated_timestamp(row["updated_at"])
            ended_raw = row["ended_at"]
            ended = None if ended_raw is None else _validated_timestamp(ended_raw)
            if _parse_timestamp(updated) < _parse_timestamp(started):
                raise ValueError("run timestamp order is invalid")
            if ended is not None and _parse_timestamp(ended) < _parse_timestamp(updated):
                raise ValueError("run end timestamp is invalid")
            return ExecutorRunRecord(
                run_id=_validated_request_id(row["run_id"], label="run_id"),
                account_scope_sha256=_validated_sha256(
                    row["account_scope_sha256"],
                    label="account_scope_sha256",
                ),
                fence_epoch=_validated_fence_epoch(row["fence_epoch"]),
                policy_sha256=_validated_sha256(row["policy_sha256"], label="policy_sha256"),
                pid=_validated_pid(row["pid"]),
                state=ExecutorRunState(str(row["state"])),
                started_at=started,
                updated_at=updated,
                ended_at=ended,
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError("executor run record is invalid") from exc

    def _command_from_row(self, row: sqlite3.Row) -> ExecutorCommandRecord:
        try:
            payload = _strict_json_object(row["payload_json"], label="command payload")
            payload_canonical = _canonical_json(
                payload,
                max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                label="command payload",
            )
            if payload_canonical != str(row["payload_json"]):
                raise ValueError("command payload is not canonical")
            outcome_raw = row["outcome_json"]
            outcome = None
            outcome_sha256 = None
            if outcome_raw is not None:
                outcome = _strict_json_object(outcome_raw, label="command outcome")
                outcome_canonical = _canonical_json(
                    outcome,
                    max_bytes=MAX_COMMAND_OUTCOME_BYTES,
                    label="command outcome",
                )
                if outcome_canonical != str(outcome_raw):
                    raise ValueError("command outcome is not canonical")
                outcome_sha256 = _validated_sha256(
                    row["outcome_sha256"],
                    label="outcome_sha256",
                )
                if _sha256_text(outcome_canonical) != outcome_sha256:
                    raise ValueError("command outcome hash is invalid")
            elif row["outcome_sha256"] is not None:
                raise ValueError("command outcome hash has no outcome")
            state = ExecutorCommandState(str(row["state"]))
            _validated_outcome(outcome, target=state)
            created = _validated_timestamp(row["created_at"])
            updated = _validated_timestamp(row["updated_at"])
            if _parse_timestamp(updated) < _parse_timestamp(created):
                raise ValueError("command timestamp order is invalid")
            record = ExecutorCommandRecord(
                request_id=_validated_request_id(row["request_id"]),
                operation=_validated_operation(row["operation"]),
                fingerprint_sha256=_validated_sha256(
                    row["fingerprint_sha256"],
                    label="fingerprint_sha256",
                ),
                payload=payload,
                state=state,
                outcome=outcome,
                outcome_sha256=outcome_sha256,
                fence_epoch=_validated_fence_epoch(row["fence_epoch"]),
                policy_sha256=_validated_sha256(row["policy_sha256"], label="policy_sha256"),
                created_run_id=_validated_request_id(
                    row["created_run_id"],
                    label="created_run_id",
                ),
                last_run_id=_validated_request_id(row["last_run_id"], label="last_run_id"),
                created_at=created,
                updated_at=updated,
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError("executor command record is invalid") from exc
        if command_fingerprint(record.operation, record.payload) != record.fingerprint_sha256:
            raise ExecutorCommandSchemaError("executor command fingerprint is invalid")
        return record

    def _safe_flatten_from_row(
        self,
        row: sqlite3.Row,
    ) -> ExecutorSafeFlattenRecord:
        try:
            created = _validated_timestamp(row["created_at"])
            updated = _validated_timestamp(row["updated_at"])
            terminal_raw = row["terminal_at"]
            terminal = (
                None
                if terminal_raw is None
                else _validated_timestamp(terminal_raw)
            )
            if _parse_timestamp(updated) < _parse_timestamp(created):
                raise ValueError("safe-flatten timestamp order is invalid")
            if terminal is not None and _parse_timestamp(terminal) != _parse_timestamp(updated):
                raise ValueError("safe-flatten terminal timestamp is invalid")
            resume_raw = row["resume_state"]
            error_raw = row["last_error_code"]
            record = ExecutorSafeFlattenRecord(
                operation_id=_validated_request_id(
                    row["operation_id"],
                    label="operation_id",
                ),
                initiating_request_id=_validated_request_id(
                    row["initiating_request_id"],
                    label="initiating_request_id",
                ),
                account_scope_sha256=_validated_sha256(
                    row["account_scope_sha256"],
                    label="account_scope_sha256",
                ),
                reason_code=_validated_reason_code(row["reason_code"]),
                control_generation=_validated_positive_integer(
                    row["control_generation"],
                    label="control_generation",
                ),
                state=ExecutorSafeFlattenState(str(row["state"])),
                state_version=_validated_positive_integer(
                    row["state_version"],
                    label="state_version",
                ),
                resume_state=(
                    None
                    if resume_raw is None
                    else ExecutorSafeFlattenState(str(resume_raw))
                ),
                last_error_code=(
                    None
                    if error_raw is None
                    else _validated_reason_code(error_raw)
                ),
                initiated_by_uid=_validated_positive_integer(
                    row["initiated_by_uid"],
                    label="initiated_by_uid",
                ),
                created_run_id=_validated_request_id(
                    row["created_run_id"],
                    label="created_run_id",
                ),
                last_run_id=_validated_request_id(
                    row["last_run_id"],
                    label="last_run_id",
                ),
                created_fence_epoch=_validated_fence_epoch(
                    row["created_fence_epoch"]
                ),
                last_fence_epoch=_validated_fence_epoch(
                    row["last_fence_epoch"]
                ),
                policy_sha256=_validated_sha256(
                    row["policy_sha256"],
                    label="policy_sha256",
                ),
                last_policy_sha256=_validated_sha256(
                    row["last_policy_sha256"],
                    label="last_policy_sha256",
                ),
                authz_policy_sha256=_validated_sha256(
                    row["authz_policy_sha256"],
                    label="authz_policy_sha256",
                ),
                created_at=created,
                updated_at=updated,
                terminal_at=terminal,
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError(
                "safe-flatten operation record is invalid"
            ) from exc
        if (
            record.operation_id != record.initiating_request_id
            or record.account_scope_sha256 != self.account_scope_sha256
            or record.last_fence_epoch < record.created_fence_epoch
            or record.terminal != (record.terminal_at is not None)
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten operation identity or lifecycle is invalid"
            )
        if record.state is ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN:
            if (
                record.resume_state
                not in {
                    ExecutorSafeFlattenState.CANCELING,
                    ExecutorSafeFlattenState.CLOSING,
                }
                or record.last_error_code != "command_outcome_unknown"
            ):
                raise ExecutorCommandSchemaError(
                    "ambiguous safe-flatten operation is invalid"
                )
        elif record.resume_state is not None:
            raise ExecutorCommandSchemaError(
                "safe-flatten operation has an unexpected resume state"
            )
        if (
            record.state is ExecutorSafeFlattenState.FAILED_LATCHED
            and record.last_error_code is None
        ):
            raise ExecutorCommandSchemaError(
                "failed safe-flatten operation lacks a safe error code"
            )
        if (
            record.state
            not in {
                ExecutorSafeFlattenState.BLOCKED_OUTCOME_UNKNOWN,
                ExecutorSafeFlattenState.FAILED_LATCHED,
            }
            and record.last_error_code is not None
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten operation has an unexpected error code"
            )
        return record

    def _safe_flatten_leg_from_row(
        self,
        row: sqlite3.Row,
    ) -> ExecutorSafeFlattenLegRecord:
        try:
            target = _strict_canonical_object(
                row["target_json"],
                label="safe-flatten leg target",
            )
            target_sha256 = _validated_sha256(
                row["target_sha256"],
                label="target_sha256",
            )
            target_json = _canonical_json(
                target,
                max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                label="safe-flatten leg target",
            )
            if _sha256_text(target_json) != target_sha256:
                raise ValueError("safe-flatten leg target hash is invalid")
            created = _validated_timestamp(row["created_at"])
            updated = _validated_timestamp(row["updated_at"])
            verified_raw = row["verified_at"]
            verified_at = (
                None
                if verified_raw is None
                else _validated_timestamp(verified_raw)
            )
            if _parse_timestamp(updated) < _parse_timestamp(created):
                raise ValueError("safe-flatten leg timestamp order is invalid")
            if verified_at is not None and _parse_timestamp(verified_at) != _parse_timestamp(updated):
                raise ValueError("safe-flatten leg verification timestamp is invalid")
            broker_raw = row["broker_order_id"]
            record = ExecutorSafeFlattenLegRecord(
                leg_id=_validated_request_id(row["leg_id"], label="leg_id"),
                operation_id=_validated_request_id(
                    row["operation_id"],
                    label="operation_id",
                ),
                ordinal=_validated_nonnegative_integer(
                    row["ordinal"],
                    label="ordinal",
                ),
                kind=ExecutorSafeFlattenLegKind(str(row["kind"])),
                target=target,
                target_sha256=target_sha256,
                command_operation=_validated_operation(row["command_operation"]),
                state=ExecutorSafeFlattenLegState(str(row["state"])),
                broker_order_id=(
                    None
                    if broker_raw is None
                    else _validated_text(broker_raw, label="broker_order_id")
                ),
                created_run_id=_validated_request_id(
                    row["created_run_id"],
                    label="created_run_id",
                ),
                last_run_id=_validated_request_id(
                    row["last_run_id"],
                    label="last_run_id",
                ),
                created_at=created,
                updated_at=updated,
                verified_at=verified_at,
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError(
                "safe-flatten leg record is invalid"
            ) from exc
        if (
            (record.state is ExecutorSafeFlattenLegState.VERIFIED)
            != (record.verified_at is not None)
        ):
            raise ExecutorCommandSchemaError(
                "safe-flatten leg verification lifecycle is invalid"
            )
        return record

    def _run_event_from_row(self, row: sqlite3.Row) -> ExecutorRunEvent:
        try:
            raw_from = row["from_state"]
            return ExecutorRunEvent(
                sequence=int(row["sequence"]),
                run_id=_validated_request_id(row["run_id"], label="run_id"),
                from_state=None if raw_from is None else ExecutorRunState(str(raw_from)),
                to_state=ExecutorRunState(str(row["to_state"])),
                metadata=_strict_canonical_object(row["metadata_json"], label="run metadata"),
                created_at=_validated_timestamp(row["created_at"]),
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError("executor run event is invalid") from exc

    def _command_event_from_row(self, row: sqlite3.Row) -> ExecutorCommandEvent:
        try:
            raw_from = row["from_state"]
            raw_outcome = row["outcome_sha256"]
            return ExecutorCommandEvent(
                sequence=int(row["sequence"]),
                request_id=_validated_request_id(row["request_id"]),
                run_id=_validated_request_id(row["run_id"], label="run_id"),
                event_type=_validated_reason_code(row["event_type"]),
                from_state=(
                    None if raw_from is None else ExecutorCommandState(str(raw_from))
                ),
                to_state=ExecutorCommandState(str(row["to_state"])),
                metadata=_strict_canonical_object(
                    row["metadata_json"],
                    label="command metadata",
                ),
                outcome_sha256=(
                    None
                    if raw_outcome is None
                    else _validated_sha256(raw_outcome, label="outcome_sha256")
                ),
                created_at=_validated_timestamp(row["created_at"]),
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError("executor command event is invalid") from exc

    def _safe_flatten_event_from_row(
        self,
        row: sqlite3.Row,
    ) -> ExecutorSafeFlattenEvent:
        try:
            raw_from = row["from_state"]
            return ExecutorSafeFlattenEvent(
                sequence=_validated_positive_integer(
                    row["sequence"],
                    label="event sequence",
                ),
                operation_id=_validated_request_id(
                    row["operation_id"],
                    label="operation_id",
                ),
                run_id=_validated_request_id(row["run_id"], label="run_id"),
                from_state=(
                    None
                    if raw_from is None
                    else ExecutorSafeFlattenState(str(raw_from))
                ),
                to_state=ExecutorSafeFlattenState(str(row["to_state"])),
                state_version=_validated_positive_integer(
                    row["state_version"],
                    label="state_version",
                ),
                event_type=_validated_reason_code(row["event_type"]),
                metadata=_strict_canonical_object(
                    row["metadata_json"],
                    label="safe-flatten metadata",
                ),
                created_at=_validated_timestamp(row["created_at"]),
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError(
                "safe-flatten operation event is invalid"
            ) from exc

    def _safe_flatten_leg_event_from_row(
        self,
        row: sqlite3.Row,
    ) -> ExecutorSafeFlattenLegEvent:
        try:
            raw_from = row["from_state"]
            return ExecutorSafeFlattenLegEvent(
                sequence=_validated_positive_integer(
                    row["sequence"],
                    label="event sequence",
                ),
                leg_id=_validated_request_id(row["leg_id"], label="leg_id"),
                run_id=_validated_request_id(row["run_id"], label="run_id"),
                from_state=(
                    None
                    if raw_from is None
                    else ExecutorSafeFlattenLegState(str(raw_from))
                ),
                to_state=ExecutorSafeFlattenLegState(str(row["to_state"])),
                event_type=_validated_reason_code(row["event_type"]),
                metadata=_strict_canonical_object(
                    row["metadata_json"],
                    label="safe-flatten leg metadata",
                ),
                created_at=_validated_timestamp(row["created_at"]),
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError(
                "safe-flatten leg event is invalid"
            ) from exc

    def _control_from_row(self, row: sqlite3.Row) -> ExecutorControlState:
        try:
            active_raw = row["kill_switch_active"]
            if type(active_raw) is not int or active_raw not in {0, 1}:
                raise ValueError("kill switch active value is invalid")
            reason_raw = row["reason_code"]
            request_raw = row["last_request_id"]
            generation = int(row["generation"])
            if generation < 0:
                raise ValueError("kill switch generation is invalid")
            return ExecutorControlState(
                kill_switch_active=bool(active_raw),
                reason_code=(
                    None if reason_raw is None else _validated_reason_code(reason_raw)
                ),
                generation=generation,
                last_request_id=(
                    None if request_raw is None else _validated_request_id(request_raw)
                ),
                updated_at=_validated_timestamp(row["updated_at"]),
            )
        except (ExecutorCommandJournalError, KeyError, TypeError, ValueError) as exc:
            raise ExecutorCommandSchemaError("executor control state is invalid") from exc

    @staticmethod
    def _append_run_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        from_state: ExecutorRunState | None,
        to_state: ExecutorRunState,
        metadata_json: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO executor_run_events (
                run_id, from_state, to_state, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                None if from_state is None else from_state.value,
                to_state.value,
                metadata_json,
                timestamp,
            ),
        )

    @staticmethod
    def _append_command_event(
        connection: sqlite3.Connection,
        *,
        request_id: str,
        run_id: str,
        event_type: str,
        from_state: ExecutorCommandState | None,
        to_state: ExecutorCommandState,
        metadata_json: str,
        outcome_sha256: str | None,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO executor_command_events (
                request_id, run_id, event_type, from_state, to_state,
                metadata_json, outcome_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                run_id,
                event_type,
                None if from_state is None else from_state.value,
                to_state.value,
                metadata_json,
                outcome_sha256,
                timestamp,
            ),
        )

    def _insert_recorded_command(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        operation: str,
        payload: Mapping[str, object],
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        timestamp: str,
    ) -> ExecutorCommandRecord:
        canonical = canonical_command_json(operation, payload)
        normalized_payload = _strict_json_object(
            canonical,
            label="executor command",
        )["payload"]
        assert isinstance(normalized_payload, dict)
        payload_json = _canonical_json(
            normalized_payload,
            max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            label="command payload",
        )
        connection.execute(
            """
            INSERT INTO executor_commands (
                request_id, operation, fingerprint_sha256, payload_json,
                state, outcome_json, outcome_sha256, fence_epoch,
                policy_sha256, created_run_id, last_run_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'recorded', NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                operation,
                _sha256_text(canonical),
                payload_json,
                fence_epoch,
                policy_sha256,
                run_id,
                run_id,
                timestamp,
                timestamp,
            ),
        )
        self._append_command_event(
            connection,
            request_id=request_id,
            run_id=run_id,
            event_type="recorded",
            from_state=None,
            to_state=ExecutorCommandState.RECORDED,
            metadata_json=_canonical_json(
                {"fence_epoch": fence_epoch, "policy_sha256": policy_sha256},
                max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                label="command metadata",
            ),
            outcome_sha256=None,
            timestamp=timestamp,
        )
        return self._command_by_id(connection, request_id)

    def _claim_command_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        run_id: str,
        timestamp: str,
        event_metadata: Mapping[str, object] | None = None,
    ) -> ExecutorCommandRecord:
        cursor = connection.execute(
            """
            UPDATE executor_commands
            SET state = 'dispatching', last_run_id = ?, updated_at = ?
            WHERE request_id = ? AND state = 'recorded'
            """,
            (run_id, timestamp, request_id),
        )
        if cursor.rowcount != 1:
            raise ExecutorCommandTransitionError(
                "executor command dispatch claim was not acquired"
            )
        self._append_command_event(
            connection,
            request_id=request_id,
            run_id=run_id,
            event_type="dispatch_claimed",
            from_state=ExecutorCommandState.RECORDED,
            to_state=ExecutorCommandState.DISPATCHING,
            metadata_json=_canonical_json(
                _normalize_json_object(
                    event_metadata or {},
                    location="command metadata",
                ),
                max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                label="command metadata",
            ),
            outcome_sha256=None,
            timestamp=timestamp,
        )
        return self._command_by_id(connection, request_id)

    def _complete_command_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        result: Mapping[str, object],
        run_id: str,
        timestamp: str,
        event_metadata: Mapping[str, object] | None = None,
    ) -> ExecutorCommandRecord:
        outcome = {
            "ok": True,
            "payload": _normalize_json_object(result, location="result"),
            "error": None,
        }
        outcome_json = _canonical_json(
            outcome,
            max_bytes=MAX_COMMAND_OUTCOME_BYTES,
            label="command outcome",
        )
        outcome_sha256 = _sha256_text(outcome_json)
        cursor = connection.execute(
            """
            UPDATE executor_commands
            SET state = 'completed', outcome_json = ?, outcome_sha256 = ?,
                last_run_id = ?, updated_at = ?
            WHERE request_id = ? AND state = 'dispatching'
            """,
            (outcome_json, outcome_sha256, run_id, timestamp, request_id),
        )
        if cursor.rowcount != 1:
            raise ExecutorCommandTransitionError(
                "executor command completion was not acquired"
            )
        self._append_command_event(
            connection,
            request_id=request_id,
            run_id=run_id,
            event_type="dispatch_completed",
            from_state=ExecutorCommandState.DISPATCHING,
            to_state=ExecutorCommandState.COMPLETED,
            metadata_json=_canonical_json(
                _normalize_json_object(
                    event_metadata or {},
                    location="command metadata",
                ),
                max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                label="command metadata",
            ),
            outcome_sha256=outcome_sha256,
            timestamp=timestamp,
        )
        return self._command_by_id(connection, request_id)

    @staticmethod
    def _append_safe_flatten_operation_event(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        run_id: str,
        event_type: str,
        from_state: ExecutorSafeFlattenState | None,
        to_state: ExecutorSafeFlattenState,
        state_version: int,
        metadata: Mapping[str, object],
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO executor_safe_flatten_operation_events (
                operation_id, run_id, event_type, from_state, to_state,
                state_version, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                run_id,
                _validated_reason_code(event_type),
                None if from_state is None else from_state.value,
                to_state.value,
                _validated_positive_integer(
                    state_version,
                    label="state_version",
                ),
                _canonical_json(
                    _normalize_json_object(
                        metadata,
                        location="safe-flatten metadata",
                    ),
                    max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                    label="safe-flatten metadata",
                ),
                timestamp,
            ),
        )

    @staticmethod
    def _append_safe_flatten_leg_event(
        connection: sqlite3.Connection,
        *,
        leg_id: str,
        run_id: str,
        event_type: str,
        from_state: ExecutorSafeFlattenLegState | None,
        to_state: ExecutorSafeFlattenLegState,
        metadata: Mapping[str, object],
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO executor_safe_flatten_leg_events (
                leg_id, run_id, event_type, from_state, to_state,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                leg_id,
                run_id,
                _validated_reason_code(event_type),
                None if from_state is None else from_state.value,
                to_state.value,
                _canonical_json(
                    _normalize_json_object(
                        metadata,
                        location="safe-flatten leg metadata",
                    ),
                    max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                    label="safe-flatten leg metadata",
                ),
                timestamp,
            ),
        )

    def _touch_safe_flatten_operation(
        self,
        connection: sqlite3.Connection,
        *,
        operation: ExecutorSafeFlattenRecord,
        run_id: str,
        fence_epoch: int,
        policy_sha256: str,
        event_type: str,
        metadata: Mapping[str, object],
        timestamp: str,
    ) -> ExecutorSafeFlattenRecord:
        if operation.terminal:
            raise ExecutorCommandTransitionError(
                "terminal safe-flatten operation cannot be touched"
            )
        version = operation.state_version + 1
        cursor = connection.execute(
            """
            UPDATE executor_safe_flatten_operations
            SET state_version = ?, last_run_id = ?, last_fence_epoch = ?,
                last_policy_sha256 = ?, updated_at = ?
            WHERE operation_id = ? AND state = ? AND state_version = ?
            """,
            (
                version,
                run_id,
                fence_epoch,
                policy_sha256,
                timestamp,
                operation.operation_id,
                operation.state.value,
                operation.state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ExecutorCommandStorageError(
                "safe-flatten operation changed concurrently"
            )
        self._append_safe_flatten_operation_event(
            connection,
            operation_id=operation.operation_id,
            run_id=run_id,
            event_type=event_type,
            from_state=operation.state,
            to_state=operation.state,
            state_version=version,
            metadata=metadata,
            timestamp=timestamp,
        )
        return self._safe_flatten_by_id(connection, operation.operation_id)

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ExecutorCommandStorageError("executor ledger clock must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _schema_objects() -> tuple[tuple[str, str, str], ...]:
    command_states = ",".join(f"'{state.value}'" for state in ExecutorCommandState)
    run_states = ",".join(f"'{state.value}'" for state in ExecutorRunState)
    flatten_states = ",".join(f"'{state.value}'" for state in ExecutorSafeFlattenState)
    flatten_leg_states = ",".join(
        f"'{state.value}'" for state in ExecutorSafeFlattenLegState
    )


    flatten_leg_kinds = ",".join(
        f"'{kind.value}'" for kind in ExecutorSafeFlattenLegKind
    )
    return (
        (
            "table",
            "executor_command_metadata",
            """
            CREATE TABLE executor_command_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                ledger_id TEXT NOT NULL UNIQUE,
                account_scope_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        ),
        (
            "table",
            "executor_runs",
            f"""
            CREATE TABLE executor_runs (
                run_id TEXT PRIMARY KEY,
                account_scope_sha256 TEXT NOT NULL,
                fence_epoch INTEGER NOT NULL UNIQUE CHECK (fence_epoch > 0),
                policy_sha256 TEXT NOT NULL,
                pid INTEGER NOT NULL CHECK (pid > 0),
                state TEXT NOT NULL CHECK (state IN ({run_states})),
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at TEXT
            )
            """,
        ),
        (
            "table",
            "executor_run_events",
            f"""
            CREATE TABLE executor_run_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                from_state TEXT CHECK (from_state IS NULL OR from_state IN ({run_states})),
                to_state TEXT NOT NULL CHECK (to_state IN ({run_states})),
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES executor_runs(run_id)
            )
            """,
        ),
        (
            "table",
            "executor_commands",
            f"""
            CREATE TABLE executor_commands (
                request_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                fingerprint_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({command_states})),
                outcome_json TEXT,
                outcome_sha256 TEXT,
                fence_epoch INTEGER NOT NULL CHECK (fence_epoch > 0),
                policy_sha256 TEXT NOT NULL,
                created_run_id TEXT NOT NULL,
                last_run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (created_run_id) REFERENCES executor_runs(run_id),
                FOREIGN KEY (last_run_id) REFERENCES executor_runs(run_id),
                CHECK (
                    (state IN ('completed','rejected')
                        AND outcome_json IS NOT NULL AND outcome_sha256 IS NOT NULL)
                    OR (state IN ('recorded','dispatching','outcome_unknown')
                        AND outcome_json IS NULL AND outcome_sha256 IS NULL)
                )
            )
            """,
        ),
        (
            "table",
            "executor_command_events",
            f"""
            CREATE TABLE executor_command_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT CHECK (from_state IS NULL OR from_state IN ({command_states})),
                to_state TEXT NOT NULL CHECK (to_state IN ({command_states})),
                metadata_json TEXT NOT NULL,
                outcome_sha256 TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (request_id) REFERENCES executor_commands(request_id),
                FOREIGN KEY (run_id) REFERENCES executor_runs(run_id)
            )
            """,
        ),
        (
            "table",
            "executor_control_state",
            """
            CREATE TABLE executor_control_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                kill_switch_active INTEGER NOT NULL CHECK (kill_switch_active IN (0,1)),
                reason_code TEXT,
                generation INTEGER NOT NULL CHECK (generation >= 0),
                last_request_id TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (last_request_id) REFERENCES executor_commands(request_id)
            )
            """,
        ),
        (
            "table",
            "executor_control_events",
            """
            CREATE TABLE executor_control_events (
                generation INTEGER PRIMARY KEY CHECK (generation > 0),
                event_type TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                request_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (request_id) REFERENCES executor_commands(request_id),
                FOREIGN KEY (run_id) REFERENCES executor_runs(run_id)
            )
            """,
        ),
        (
            "table",
            "executor_safe_flatten_operations",
            f"""
            CREATE TABLE executor_safe_flatten_operations (
                operation_id TEXT PRIMARY KEY,
                initiating_request_id TEXT NOT NULL UNIQUE,
                account_scope_sha256 TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                control_generation INTEGER NOT NULL CHECK (control_generation > 0),
                state TEXT NOT NULL CHECK (state IN ({flatten_states})),
                state_version INTEGER NOT NULL CHECK (state_version > 0),
                resume_state TEXT CHECK (
                    resume_state IS NULL OR resume_state IN ('canceling','closing')
                ),
                last_error_code TEXT,
                initiated_by_uid INTEGER NOT NULL CHECK (initiated_by_uid > 0),
                created_run_id TEXT NOT NULL,
                last_run_id TEXT NOT NULL,
                created_fence_epoch INTEGER NOT NULL CHECK (created_fence_epoch > 0),
                last_fence_epoch INTEGER NOT NULL CHECK (last_fence_epoch > 0),
                policy_sha256 TEXT NOT NULL,
                last_policy_sha256 TEXT NOT NULL,
                authz_policy_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                terminal_at TEXT,
                FOREIGN KEY (operation_id) REFERENCES executor_commands(request_id),
                FOREIGN KEY (initiating_request_id) REFERENCES executor_commands(request_id),
                FOREIGN KEY (created_run_id) REFERENCES executor_runs(run_id),
                FOREIGN KEY (last_run_id) REFERENCES executor_runs(run_id),
                CHECK (
                    (state IN ('failed_latched','flat_latched') AND terminal_at IS NOT NULL)
                    OR (state NOT IN ('failed_latched','flat_latched') AND terminal_at IS NULL)
                ),
                CHECK (
                    (state = 'blocked_outcome_unknown' AND resume_state IS NOT NULL
                        AND last_error_code = 'command_outcome_unknown')
                    OR state <> 'blocked_outcome_unknown'
                )
            )
            """,
        ),
        (
            "table",
            "executor_safe_flatten_operation_events",
            f"""
            CREATE TABLE executor_safe_flatten_operation_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT CHECK (from_state IS NULL OR from_state IN ({flatten_states})),
                to_state TEXT NOT NULL CHECK (to_state IN ({flatten_states})),
                state_version INTEGER NOT NULL CHECK (state_version > 0),
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (operation_id) REFERENCES executor_safe_flatten_operations(operation_id),
                FOREIGN KEY (run_id) REFERENCES executor_runs(run_id)
            )
            """,
        ),
        (
            "table",
            "executor_safe_flatten_legs",
            f"""
            CREATE TABLE executor_safe_flatten_legs (
                leg_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                kind TEXT NOT NULL CHECK (kind IN ({flatten_leg_kinds})),
                target_json TEXT NOT NULL,
                target_sha256 TEXT NOT NULL,
                command_operation TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({flatten_leg_states})),
                broker_order_id TEXT,
                created_run_id TEXT NOT NULL,
                last_run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                verified_at TEXT,
                FOREIGN KEY (leg_id) REFERENCES executor_commands(request_id),
                FOREIGN KEY (operation_id) REFERENCES executor_safe_flatten_operations(operation_id),
                FOREIGN KEY (created_run_id) REFERENCES executor_runs(run_id),
                FOREIGN KEY (last_run_id) REFERENCES executor_runs(run_id),
                UNIQUE (operation_id, kind, ordinal),
                UNIQUE (operation_id, kind, target_sha256),
                CHECK (
                    (state = 'verified' AND verified_at IS NOT NULL)
                    OR (state <> 'verified' AND verified_at IS NULL)
                )
            )
            """,
        ),
        (
            "table",
            "executor_safe_flatten_leg_events",
            f"""
            CREATE TABLE executor_safe_flatten_leg_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                leg_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT CHECK (from_state IS NULL OR from_state IN ({flatten_leg_states})),
                to_state TEXT NOT NULL CHECK (to_state IN ({flatten_leg_states})),
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (leg_id) REFERENCES executor_safe_flatten_legs(leg_id),
                FOREIGN KEY (run_id) REFERENCES executor_runs(run_id)
            )
            """,
        ),
        (
            "index",
            "executor_runs_state_epoch",
            "CREATE INDEX executor_runs_state_epoch ON executor_runs(state, fence_epoch)",
        ),
        (
            "index",
            "executor_run_events_run_sequence",
            "CREATE INDEX executor_run_events_run_sequence ON executor_run_events(run_id, sequence)",
        ),
        (
            "index",
            "executor_commands_state_created",
            "CREATE INDEX executor_commands_state_created ON executor_commands(state, created_at)",
        ),
        (
            "index",
            "executor_command_events_request_sequence",
            "CREATE INDEX executor_command_events_request_sequence ON executor_command_events(request_id, sequence)",
        ),
        (
            "index",
            "executor_safe_flatten_single_active",
            """
            CREATE UNIQUE INDEX executor_safe_flatten_single_active
            ON executor_safe_flatten_operations ((1))
            WHERE state <> 'flat_latched'
            """,
        ),
        (
            "index",
            "executor_safe_flatten_operation_events_sequence",
            """
            CREATE INDEX executor_safe_flatten_operation_events_sequence
            ON executor_safe_flatten_operation_events(operation_id, sequence)
            """,
        ),
        (
            "index",
            "executor_safe_flatten_legs_operation_ordinal",
            """
            CREATE INDEX executor_safe_flatten_legs_operation_ordinal
            ON executor_safe_flatten_legs(operation_id, ordinal)
            """,
        ),
        (
            "index",
            "executor_safe_flatten_leg_events_sequence",
            """
            CREATE INDEX executor_safe_flatten_leg_events_sequence
            ON executor_safe_flatten_leg_events(leg_id, sequence)
            """,
        ),
        (
            "trigger",
            "executor_run_events_no_update",
            """
            CREATE TRIGGER executor_run_events_no_update
            BEFORE UPDATE ON executor_run_events
            BEGIN SELECT RAISE(ABORT, 'executor_run_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_run_events_no_delete",
            """
            CREATE TRIGGER executor_run_events_no_delete
            BEFORE DELETE ON executor_run_events
            BEGIN SELECT RAISE(ABORT, 'executor_run_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_runs_no_delete",
            """
            CREATE TRIGGER executor_runs_no_delete
            BEFORE DELETE ON executor_runs
            BEGIN SELECT RAISE(ABORT, 'executor_runs_durable'); END
            """,
        ),
        (
            "trigger",
            "executor_runs_identity_immutable",
            """
            CREATE TRIGGER executor_runs_identity_immutable
            BEFORE UPDATE OF run_id, account_scope_sha256, fence_epoch,
                policy_sha256, pid, started_at
            ON executor_runs
            BEGIN SELECT RAISE(ABORT, 'executor_run_identity_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_runs_terminal_immutable",
            """
            CREATE TRIGGER executor_runs_terminal_immutable
            BEFORE UPDATE ON executor_runs
            WHEN OLD.state IN ('stopped','crashed','failed')
            BEGIN SELECT RAISE(ABORT, 'executor_run_terminal_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_command_events_no_update",
            """
            CREATE TRIGGER executor_command_events_no_update
            BEFORE UPDATE ON executor_command_events
            BEGIN SELECT RAISE(ABORT, 'executor_command_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_command_events_no_delete",
            """
            CREATE TRIGGER executor_command_events_no_delete
            BEFORE DELETE ON executor_command_events
            BEGIN SELECT RAISE(ABORT, 'executor_command_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_commands_no_delete",
            """
            CREATE TRIGGER executor_commands_no_delete
            BEFORE DELETE ON executor_commands
            BEGIN SELECT RAISE(ABORT, 'executor_commands_durable'); END
            """,
        ),
        (
            "trigger",
            "executor_commands_identity_immutable",
            """
            CREATE TRIGGER executor_commands_identity_immutable
            BEFORE UPDATE OF request_id, operation, fingerprint_sha256,
                payload_json, fence_epoch, policy_sha256, created_run_id, created_at
            ON executor_commands
            BEGIN SELECT RAISE(ABORT, 'executor_command_identity_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_commands_terminal_immutable",
            """
            CREATE TRIGGER executor_commands_terminal_immutable
            BEFORE UPDATE ON executor_commands
            WHEN OLD.state IN ('completed','rejected')
            BEGIN SELECT RAISE(ABORT, 'executor_command_terminal_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_control_events_no_update",
            """
            CREATE TRIGGER executor_control_events_no_update
            BEFORE UPDATE ON executor_control_events
            BEGIN SELECT RAISE(ABORT, 'executor_control_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_control_events_no_delete",
            """
            CREATE TRIGGER executor_control_events_no_delete
            BEFORE DELETE ON executor_control_events
            BEGIN SELECT RAISE(ABORT, 'executor_control_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_operation_events_no_update",
            """
            CREATE TRIGGER executor_safe_flatten_operation_events_no_update
            BEFORE UPDATE ON executor_safe_flatten_operation_events
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_operation_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_operation_events_no_delete",
            """
            CREATE TRIGGER executor_safe_flatten_operation_events_no_delete
            BEFORE DELETE ON executor_safe_flatten_operation_events
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_operation_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_operations_no_delete",
            """
            CREATE TRIGGER executor_safe_flatten_operations_no_delete
            BEFORE DELETE ON executor_safe_flatten_operations
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_operations_durable'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_operations_identity_immutable",
            """
            CREATE TRIGGER executor_safe_flatten_operations_identity_immutable
            BEFORE UPDATE OF operation_id, initiating_request_id,
                account_scope_sha256, reason_code, control_generation,
                initiated_by_uid, created_run_id, created_fence_epoch,
                policy_sha256, authz_policy_sha256, created_at
            ON executor_safe_flatten_operations
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_operation_identity_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_operations_terminal_immutable",
            """
            CREATE TRIGGER executor_safe_flatten_operations_terminal_immutable
            BEFORE UPDATE ON executor_safe_flatten_operations
            WHEN OLD.state IN ('failed_latched','flat_latched')
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_operation_terminal_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_leg_events_no_update",
            """
            CREATE TRIGGER executor_safe_flatten_leg_events_no_update
            BEFORE UPDATE ON executor_safe_flatten_leg_events
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_leg_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_leg_events_no_delete",
            """
            CREATE TRIGGER executor_safe_flatten_leg_events_no_delete
            BEFORE DELETE ON executor_safe_flatten_leg_events
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_leg_events_append_only'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_legs_no_delete",
            """
            CREATE TRIGGER executor_safe_flatten_legs_no_delete
            BEFORE DELETE ON executor_safe_flatten_legs
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_legs_durable'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_legs_identity_immutable",
            """
            CREATE TRIGGER executor_safe_flatten_legs_identity_immutable
            BEFORE UPDATE OF leg_id, operation_id, ordinal, kind, target_json,
                target_sha256, command_operation, created_run_id, created_at
            ON executor_safe_flatten_legs
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_leg_identity_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_safe_flatten_legs_terminal_immutable",
            """
            CREATE TRIGGER executor_safe_flatten_legs_terminal_immutable
            BEFORE UPDATE ON executor_safe_flatten_legs
            WHEN OLD.state IN ('verified','rejected')
            BEGIN SELECT RAISE(ABORT, 'executor_safe_flatten_leg_terminal_immutable'); END
            """,
        ),
        (
            "trigger",
            "executor_control_no_unlatch",
            """
            CREATE TRIGGER executor_control_no_unlatch
            BEFORE UPDATE OF kill_switch_active ON executor_control_state
            WHEN OLD.kill_switch_active = 1 AND NEW.kill_switch_active = 0
            BEGIN SELECT RAISE(ABORT, 'executor_kill_switch_cannot_unlatch_online'); END
            """,
        ),
    )


def _schema_objects_v3() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        item
        for item in _schema_objects()
        if not item[1].startswith("executor_safe_flatten")
    )


def _schema_definitions(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): _normalize_sql(str(row[2]))
        for row in connection.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index','trigger')
            """
        ).fetchall()
    }


def _coerce_safe_flatten_state(
    value: ExecutorSafeFlattenState | str,
) -> ExecutorSafeFlattenState:
    try:
        return (
            value
            if isinstance(value, ExecutorSafeFlattenState)
            else ExecutorSafeFlattenState(str(value))
        )
    except ValueError as exc:
        raise ExecutorCommandJournalError(
            "safe-flatten state is invalid"
        ) from exc


def _coerce_safe_flatten_leg_kind(
    value: ExecutorSafeFlattenLegKind | str,
) -> ExecutorSafeFlattenLegKind:
    try:
        return (
            value
            if isinstance(value, ExecutorSafeFlattenLegKind)
            else ExecutorSafeFlattenLegKind(str(value))
        )
    except ValueError as exc:
        raise ExecutorCommandJournalError(
            "safe-flatten leg kind is invalid"
        ) from exc


def _validated_positive_integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ExecutorCommandJournalError(f"{label} must be a positive integer")
    return value


def _validated_nonnegative_integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ExecutorCommandJournalError(
            f"{label} must be a nonnegative integer"
        )
    return value


def _validated_safe_flatten_leg_target(
    kind: ExecutorSafeFlattenLegKind,
    target: Mapping[str, object],
    *,
    command_operation: str,
    command_payload: Mapping[str, object],
) -> dict[str, object]:
    normalized = _normalize_json_object(
        target,
        location="safe-flatten leg target",
    )
    if kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER:
        if (
            command_operation != "cancel_order"
            or set(normalized) != {"order_id", "client_order_id", "symbol"}
        ):
            raise ExecutorCommandJournalError(
                "safe-flatten cancel target is invalid"
            )
        order_id = _validated_text(normalized["order_id"], label="order_id")
        client_raw = normalized["client_order_id"]
        client_order_id = (
            None
            if client_raw is None
            else _validated_text(client_raw, label="client_order_id")
        )
        symbol = _validated_text(normalized["symbol"], label="symbol")
        if set(command_payload) != {"order_id", "client_order_id"}:
            raise ExecutorCommandJournalError(
                "safe-flatten cancel command payload is invalid"
            )
        if (
            command_payload["order_id"] != order_id
            or command_payload["client_order_id"] is not None
        ):
            raise ExecutorCommandJournalError(
                "safe-flatten cancel command does not match its target"
            )
        return {
            "client_order_id": client_order_id,
            "order_id": order_id,
            "symbol": symbol,
        }

    if (
        command_operation != "submit_order"
        or set(normalized) != {"symbol", "side", "quantity", "client_order_id"}
    ):
        raise ExecutorCommandJournalError(
            "safe-flatten close target is invalid"
        )
    symbol = _validated_text(normalized["symbol"], label="symbol")
    side = _validated_text(normalized["side"], label="side").lower()
    if side not in {"buy", "sell"}:
        raise ExecutorCommandJournalError("safe-flatten close side is invalid")
    quantity_raw = normalized["quantity"]
    if isinstance(quantity_raw, bool) or not isinstance(quantity_raw, (int, float)):
        raise ExecutorCommandJournalError(
            "safe-flatten close quantity is invalid"
        )
    quantity = float(quantity_raw)
    if not math.isfinite(quantity) or quantity <= 0:
        raise ExecutorCommandJournalError(
            "safe-flatten close quantity is invalid"
        )
    client_order_id = _validated_text(
        normalized["client_order_id"],
        label="client_order_id",
    )
    try:
        order = command_payload["order"]
        if set(command_payload) != {"order"} or not isinstance(order, Mapping):
            raise ValueError("order envelope is invalid")
        if (
            order.get("symbol") != symbol
            or order.get("side") != side
            or order.get("client_order_id") != client_order_id
            or not _same_json_number(order.get("quantity"), quantity)
            or order.get("notional") is not None
            or order.get("reference_price") is not None
            or order.get("order_type") != "market"
            or order.get("limit_price") is not None
            or order.get("position_intent") != "close"
        ):
            raise ValueError("close order differs from target")
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutorCommandJournalError(
            "safe-flatten close command does not match its target"
        ) from exc
    return {
        "client_order_id": client_order_id,
        "quantity": quantity,
        "side": side,
        "symbol": symbol,
    }


def _same_json_number(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    left_number = float(left)
    right_number = float(right)
    return (
        math.isfinite(left_number)
        and math.isfinite(right_number)
        and abs(left_number - right_number) <= 1e-12
    )


def _validated_safe_flatten_dispatch_result(
    leg: ExecutorSafeFlattenLegRecord,
    *,
    result: Mapping[str, object],
    accepted: bool,
    broker_order_id: str | None,
) -> dict[str, object]:
    normalized = _normalize_json_object(
        result,
        location="safe-flatten dispatch result",
    )
    if set(normalized) != {"accepted", "status", "reasons", "dry_run"}:
        raise ExecutorCommandJournalError(
            "safe-flatten dispatch result fields are invalid"
        )
    if type(normalized["accepted"]) is not bool or normalized["dry_run"] is not False:
        raise ExecutorCommandJournalError(
            "safe-flatten dispatch result is invalid"
        )
    status = _validated_reason_code(normalized["status"])
    reasons = normalized["reasons"]
    if not isinstance(reasons, list):
        raise ExecutorCommandJournalError(
            "safe-flatten dispatch reasons are invalid"
        )
    clean_reasons = [_validated_reason_code(item) for item in reasons]
    broker_accepted = bool(normalized["accepted"])
    if leg.kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER:
        expected_accepted = (
            broker_accepted
            or status == "cancel_pending"
            or status.startswith("cancel_terminal_")
        )
        if broker_order_id != leg.target["order_id"]:
            raise ExecutorCommandJournalError(
                "safe-flatten cancel result has the wrong broker order id"
            )
    else:
        expected_accepted = broker_accepted
    if accepted is not expected_accepted:
        raise ExecutorCommandJournalError(
            "safe-flatten leg state disagrees with its broker result"
        )
    return {
        "accepted": broker_accepted,
        "dry_run": False,
        "reasons": clean_reasons,
        "status": status,
    }


def _validated_safe_flatten_leg_evidence(
    leg: ExecutorSafeFlattenLegRecord,
    *,
    verified: bool,
    broker_order_id: str | None,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    normalized = _normalize_json_object(
        evidence,
        location="safe-flatten leg evidence",
    )
    if broker_order_id is None:
        raise ExecutorCommandJournalError(
            "safe-flatten verification requires a broker order id"
        )
    if leg.kind is ExecutorSafeFlattenLegKind.CANCEL_ORDER:
        expected = {
            "broker_order_id",
            "observed_at",
            "observed_status",
            "order_id",
        }
        if set(normalized) != expected:
            raise ExecutorCommandJournalError(
                "safe-flatten cancellation evidence fields are invalid"
            )
        status = _validated_reason_code(normalized["observed_status"])
        if (
            not verified
            or status not in {"canceled", "expired", "filled", "rejected"}
            or normalized["order_id"] != leg.target["order_id"]
            or normalized["broker_order_id"] != broker_order_id
            or broker_order_id != leg.target["order_id"]
        ):
            raise ExecutorCommandJournalError(
                "safe-flatten cancellation evidence is inconsistent"
            )
    else:
        expected = {
            "broker_order_id",
            "client_order_id",
            "filled_quantity",
            "observed_at",
            "observed_status",
            "quantity",
            "side",
            "symbol",
        }
        if set(normalized) != expected:
            raise ExecutorCommandJournalError(
                "safe-flatten close evidence fields are invalid"
            )
        status = _validated_reason_code(normalized["observed_status"])
        filled_quantity = normalized["filled_quantity"]
        if (
            normalized["broker_order_id"] != broker_order_id
            or normalized["client_order_id"] != leg.target["client_order_id"]
            or normalized["symbol"] != leg.target["symbol"]
            or normalized["side"] != leg.target["side"]
            or not _same_json_number(normalized["quantity"], leg.target["quantity"])
            or isinstance(filled_quantity, bool)
            or not isinstance(filled_quantity, (int, float))
            or not math.isfinite(float(filled_quantity))
            or float(filled_quantity) < 0
        ):
            raise ExecutorCommandJournalError(
                "safe-flatten close evidence is inconsistent"
            )
        exact_fill = _same_json_number(filled_quantity, leg.target["quantity"])
        if verified:
            if status != "filled" or not exact_fill:
                raise ExecutorCommandJournalError(
                    "safe-flatten close evidence does not prove a full fill"
                )
        elif status not in {"canceled", "expired", "filled", "rejected"}:
            raise ExecutorCommandJournalError(
                "safe-flatten close rejection lacks a terminal broker state"
            )
    observed_at = _validated_text(normalized["observed_at"], label="observed_at")
    _parse_timestamp(observed_at)
    return normalized


def _validated_safe_flatten_final_evidence(
    value: Mapping[str, object],
) -> dict[str, object]:
    normalized = _normalize_json_object(
        value,
        location="safe-flatten final evidence",
    )
    expected = {
        "broker_open_order_count",
        "broker_position_count",
        "journal_reconciled",
        "journal_record_count",
        "journal_max_event_sequence",
        "journal_projection_sha256",
        "open_orders_sha256",
        "positions_sha256",
    }
    if set(normalized) != expected:
        raise ExecutorCommandJournalError(
            "safe-flatten final evidence fields are invalid"
        )
    if (
        type(normalized["broker_open_order_count"]) is not int
        or normalized["broker_open_order_count"] != 0
        or type(normalized["broker_position_count"]) is not int
        or normalized["broker_position_count"] != 0
        or normalized["journal_reconciled"] is not True
        or type(normalized["journal_record_count"]) is not int
        or normalized["journal_record_count"] < 0
        or type(normalized["journal_max_event_sequence"]) is not int
        or normalized["journal_max_event_sequence"] < 0
    ):
        raise ExecutorCommandJournalError(
            "safe-flatten final evidence does not prove a flat account"
        )
    _validated_sha256(
        normalized["journal_projection_sha256"],
        label="journal_projection_sha256",
    )
    _validated_sha256(
        normalized["open_orders_sha256"],
        label="open_orders_sha256",
    )
    _validated_sha256(
        normalized["positions_sha256"],
        label="positions_sha256",
    )
    return normalized


def _safe_error_outcome(code: str) -> dict[str, object]:
    clean_code = _validated_error_code(code)
    message = _SAFE_ERROR_MESSAGES.get(clean_code)
    if message is None:
        raise ExecutorCommandJournalError("executor error code is not in the safe catalog")
    return {"ok": False, "payload": None, "error": {"code": clean_code, "message": message}}


def _validated_outcome(
    value: Mapping[str, object] | None,
    *,
    target: ExecutorCommandState,
) -> None:
    terminal = target in {ExecutorCommandState.COMPLETED, ExecutorCommandState.REJECTED}
    if not terminal:
        if value is not None:
            raise ExecutorCommandSchemaError("nonterminal command has an outcome")
        return
    if not isinstance(value, Mapping) or set(value) != {"ok", "payload", "error"}:
        raise ExecutorCommandSchemaError("terminal executor outcome is invalid")
    if target is ExecutorCommandState.COMPLETED:
        if value["ok"] is not True or not isinstance(value["payload"], dict) or value["error"] is not None:
            raise ExecutorCommandSchemaError("completed executor outcome is invalid")
        return
    error = value["error"]
    if value["ok"] is not False or value["payload"] is not None or not isinstance(error, dict):
        raise ExecutorCommandSchemaError("rejected executor outcome is invalid")
    if set(error) != {"code", "message"}:
        raise ExecutorCommandSchemaError("rejected executor error is invalid")
    code = _validated_error_code(error["code"])
    if _SAFE_ERROR_MESSAGES.get(code) != error["message"]:
        raise ExecutorCommandSchemaError("rejected executor message is not canonical")


def _validated_reconciliation_evidence(value: Mapping[str, object]) -> dict[str, object]:
    normalized = _normalize_json_object(value, location="reconciliation evidence")
    expected = {
        "source",
        "client_order_id",
        "broker_order_id",
        "observed_status",
        "observed_at",
        "command_fingerprint_sha256",
        "intent_fingerprint_sha256",
    }
    if set(normalized) != expected:
        raise ExecutorCommandJournalError("reconciliation evidence fields are invalid")
    if normalized["source"] != "broker_order":
        raise ExecutorCommandJournalError("reconciliation evidence source is invalid")
    _validated_text(normalized["client_order_id"], label="client_order_id")
    broker_id = normalized["broker_order_id"]
    if broker_id is not None:
        _validated_text(broker_id, label="broker_order_id")
    _validated_reason_code(normalized["observed_status"])
    _validated_timestamp(normalized["observed_at"])
    _validated_sha256(
        normalized["command_fingerprint_sha256"],
        label="command_fingerprint_sha256",
    )
    _validated_sha256(
        normalized["intent_fingerprint_sha256"],
        label="intent_fingerprint_sha256",
    )
    return normalized


def _command_order_payload(record: ExecutorCommandRecord) -> dict[str, object]:
    try:
        if record.operation != "submit_order" or set(record.payload) != {"order"}:
            raise ValueError("submit command envelope is invalid")
        order = record.payload["order"]
        if not isinstance(order, dict):
            raise ValueError("submit order payload is invalid")
        expected = {
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
        if set(order) != expected:
            raise ValueError("submit order fields are invalid")
        return order
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutorCommandSchemaError("durable submit command payload is invalid") from exc


def _command_cancel_target(record: ExecutorCommandRecord) -> tuple[str | None, str | None]:
    try:
        if record.operation != "cancel_order" or set(record.payload) != {
            "order_id",
            "client_order_id",
        }:
            raise ValueError("cancel command envelope is invalid")
        raw_order_id = record.payload["order_id"]
        raw_client_id = record.payload["client_order_id"]
        if (raw_order_id is None) == (raw_client_id is None):
            raise ValueError("cancel target is ambiguous")
        order_id = None if raw_order_id is None else _validated_text(raw_order_id, label="order_id")
        client_id = (
            None
            if raw_client_id is None
            else _validated_text(raw_client_id, label="client_order_id")
        )
        return order_id, client_id
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutorCommandSchemaError("durable cancel command payload is invalid") from exc


def _business_intent_fingerprint(record: ExecutorCommandRecord) -> str:
    if record.operation == "submit_order":
        order = _command_order_payload(record)
        symbol = str(order["symbol"])
        return intent_fingerprint(
            {
                "symbol": symbol,
                "side": order["side"],
                "quantity": order["quantity"],
                "notional": order["notional"],
                "order_type": order["order_type"],
                "time_in_force": "gtc" if "/" in symbol else "day",
                "limit_price": order["limit_price"],
                "position_intent": order["position_intent"],
                "reference_price": order["reference_price"],
            }
        )
    if record.operation == "cancel_order":
        order_id, client_order_id = _command_cancel_target(record)
        return _sha256_text(
            _canonical_json(
                {"client_order_id": client_order_id, "order_id": order_id},
                max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
                label="cancel target",
            )
        )
    raise ExecutorCommandTransitionError(
        "executor operation does not have broker order recovery semantics"
    )


def _recovered_order_result(
    record: ExecutorCommandRecord,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    status = str(evidence["observed_status"])
    if record.operation == "submit_order":
        accepted = status not in {"canceled", "expired", "rejected"}
        return {
            "accepted": accepted,
            "status": f"recovered_submit_{status}",
            "reasons": [] if accepted else [f"broker_terminal_{status}"],
            "dry_run": False,
        }
    if record.operation == "cancel_order":
        accepted = status == "canceled"
        reason = (
            "cancel_already_pending"
            if status == "pending_cancel"
            else f"broker_terminal_{status}"
        )
        return {
            "accepted": accepted,
            "status": f"recovered_cancel_{status}",
            "reasons": [] if accepted else [reason],
            "dry_run": False,
        }
    raise ExecutorCommandTransitionError(
        "executor operation does not have broker order recovery semantics"
    )


def _is_nonreducing_submit(operation: str, payload: Mapping[str, object]) -> bool:
    if operation != "submit_order":
        return False
    try:
        order = payload["order"]
        if not isinstance(order, Mapping):
            raise ValueError("submit order payload is invalid")
        intent = order["position_intent"]
        if type(intent) is not str:
            raise ValueError("position intent is invalid")
        if intent not in {"open", "increase", "reduce", "close"}:
            raise ValueError("position intent is invalid")
        return intent not in {"reduce", "close"}
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutorCommandSchemaError("durable submit command payload is invalid") from exc


def _normalize_json_object(value: Any, *, location: str) -> dict[str, object]:
    normalized = _normalize_json_value(value, location=location, depth=0, counter=[0])
    if not isinstance(normalized, dict):
        raise ExecutorCommandJournalError(f"{location} must be an object")
    return normalized


def _normalize_json_value(
    value: Any,
    *,
    location: str,
    depth: int,
    counter: list[int],
) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise ExecutorCommandJournalError(f"{location} exceeds maximum JSON depth")
    counter[0] += 1
    if counter[0] > MAX_JSON_ITEMS:
        raise ExecutorCommandJournalError(f"{location} has too many JSON values")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        if len(value) > MAX_COMMAND_PAYLOAD_BYTES:
            raise ExecutorCommandJournalError(f"{location} string is too large")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ExecutorCommandJournalError(f"{location} contains a non-finite float")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ExecutorCommandJournalError(f"{location} contains a non-string key")
            normalized[key] = _normalize_json_value(
                item,
                location=f"{location}.{key}",
                depth=depth + 1,
                counter=counter,
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json_value(
                item,
                location=f"{location}[{index}]",
                depth=depth + 1,
                counter=counter,
            )
            for index, item in enumerate(value)
        ]
    raise ExecutorCommandJournalError(f"{location} contains unsupported JSON data")


def _canonical_json(value: Any, *, max_bytes: int, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutorCommandJournalError(f"{label} is not strict JSON") from exc
    if not encoded or len(encoded) > max_bytes:
        raise ExecutorCommandJournalError(f"{label} exceeds its size limit")
    return encoded.decode("utf-8")


def _strict_json_object(value: Any, *, label: str) -> dict[str, object]:
    if type(value) is not str:
        raise ExecutorCommandSchemaError(f"{label} JSON is invalid")
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except ExecutorCommandJournalError as exc:
        raise ExecutorCommandSchemaError(f"{label} JSON is invalid") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutorCommandSchemaError(f"{label} JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise ExecutorCommandSchemaError(f"{label} must be an object")
    return decoded


def _strict_canonical_object(value: Any, *, label: str) -> dict[str, object]:
    decoded = _strict_json_object(value, label=label)
    canonical = _canonical_json(
        decoded,
        max_bytes=MAX_COMMAND_PAYLOAD_BYTES,
        label=label,
    )
    if canonical != value:
        raise ExecutorCommandSchemaError(f"{label} is not canonical")
    return decoded


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutorCommandJournalError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ExecutorCommandJournalError(f"non-finite JSON constant is forbidden: {value}")


def _validated_operation(value: Any) -> str:
    if type(value) is not str or _OPERATION_RE.fullmatch(value) is None:
        raise ExecutorCommandJournalError("executor command operation is invalid")
    return value


def _validated_request_id(value: Any, *, label: str = "request_id") -> str:
    if type(value) is not str or len(value) != 32 or value == "0" * 32:
        raise ExecutorCommandJournalError(f"executor {label} is invalid")
    try:
        parsed = uuid.UUID(hex=value)
    except (AttributeError, ValueError) as exc:
        raise ExecutorCommandJournalError(f"executor {label} is invalid") from exc
    if parsed.hex != value:
        raise ExecutorCommandJournalError(f"executor {label} is not canonical")
    return value


def _validated_sha256(value: Any, *, label: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise ExecutorCommandSchemaError(f"executor {label} is invalid")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ExecutorCommandSchemaError(f"executor {label} is invalid") from exc
    if value.lower() != value:
        raise ExecutorCommandSchemaError(f"executor {label} is not canonical")
    return value


def _validated_fence_epoch(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ExecutorCommandJournalError("executor fence epoch is invalid")
    return value


def _validated_pid(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ExecutorCommandJournalError("executor pid is invalid")
    return value


def _validated_error_code(value: Any) -> str:
    if type(value) is not str or _ERROR_CODE_RE.fullmatch(value) is None:
        raise ExecutorCommandJournalError("executor error code is invalid")
    return value


def _validated_reason_code(value: Any) -> str:
    if type(value) is not str or _REASON_CODE_RE.fullmatch(value) is None:
        raise ExecutorCommandJournalError("executor reason code is invalid")
    return value


def _strict_integer(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("value is not an integer")
    return value


def _validated_text(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ExecutorCommandJournalError(f"executor {label} is invalid")
    clean = value.strip()
    if not clean or len(clean) > 256 or any(ord(character) < 32 for character in clean):
        raise ExecutorCommandJournalError(f"executor {label} is invalid")
    return clean


def _coerce_run_state(value: ExecutorRunState | str) -> ExecutorRunState:
    try:
        return ExecutorRunState(str(getattr(value, "value", value)))
    except ValueError as exc:
        raise ExecutorCommandTransitionError("executor run state is invalid") from exc


def _validated_timestamp(value: Any) -> str:
    if type(value) is not str:
        raise ExecutorCommandSchemaError("executor timestamp is invalid")
    parsed = _parse_timestamp(value)
    canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ExecutorCommandSchemaError("executor timestamp is not canonical UTC")
    return value


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutorCommandSchemaError("executor timestamp is invalid")
    return parsed


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_sql(value: str) -> str:
    return " ".join(value.split())


def _validate_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ExecutorCommandStorageError("executor ledger directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise ExecutorCommandStorageError("executor ledger directory must be a real directory")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExecutorCommandStorageError("executor ledger directory permissions are unsafe")


def _private_file_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ExecutorCommandStorageError("executor ledger is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ExecutorCommandStorageError("executor ledger must be a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ExecutorCommandStorageError("executor ledger ownership is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExecutorCommandStorageError("executor ledger permissions are unsafe")
    return int(metadata.st_dev), int(metadata.st_ino)


def _validate_private_file(path: Path) -> None:
    _private_file_identity(path)


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        connection.rollback()
