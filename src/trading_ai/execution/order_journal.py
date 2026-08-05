"""Durable, fail-closed order-intent journal.

The journal is deliberately small and broker-agnostic.  It persists an order
intent *before* a broker side effect, rejects reuse of a client order ID for a
different intent, and records every accepted state change in an append-only
event table.  Callers remain responsible for broker reconciliation and must
not interpret ``acknowledged`` as ``filled``.

SQLite is configured for WAL mode, ``synchronous=FULL`` and a bounded busy
timeout on every connection.  Any database, schema, integrity, or locking
error is surfaced as :class:`OrderJournalStorageError`; callers are expected
to block order execution rather than continue without the journal.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class OrderJournalError(RuntimeError):
    """Base class for journal errors that must block execution."""


class OrderJournalStorageError(OrderJournalError):
    """The durable journal could not be trusted, read, or updated."""


class OrderJournalSchemaError(OrderJournalStorageError):
    """The database schema is absent, unexpected, or unsupported."""


class InvalidOrderIntentError(OrderJournalError):
    """An order intent cannot be canonicalized safely."""


class OrderIntentCollisionError(OrderJournalError):
    """A client order ID was reused for a different canonical intent."""


class OrderIntentNotFoundError(OrderJournalError):
    """A requested client order ID does not exist in the journal."""


class InvalidOrderTransitionError(OrderJournalError):
    """A requested state transition is not allowed."""


class BrokerOrderIdCollisionError(OrderJournalError):
    """A journaled intent resolved to conflicting broker order IDs."""


class JournalState(StrEnum):
    """Finite order states understood by the durable journal."""

    INTENT_RECORDED = "intent_recorded"
    SUBMIT_ATTEMPTED = "submit_attempted"
    ACKNOWLEDGED = "acknowledged"
    SUBMIT_UNRESOLVED = "submit_unresolved"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_UNRESOLVED = "cancel_unresolved"
    CANCELED = "canceled"
    FILLED = "filled"
    EXPIRED = "expired"
    RECONCILED = "reconciled"


_TERMINAL_STATES = frozenset(
    {
        JournalState.REJECTED,
        JournalState.CANCELED,
        JournalState.FILLED,
        JournalState.EXPIRED,
    }
)

# A newly recorded intent may resolve directly to a broker state only when the
# caller's broker-first lookup found an existing order.  The journal records the
# transition but never decides whether that lookup is trustworthy.
_ALLOWED_TRANSITIONS: dict[JournalState, frozenset[JournalState]] = {
    JournalState.INTENT_RECORDED: frozenset(
        {
            JournalState.SUBMIT_ATTEMPTED,
            JournalState.ACKNOWLEDGED,
            JournalState.SUBMIT_UNRESOLVED,
            JournalState.PARTIALLY_FILLED,
            JournalState.REJECTED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }
    ),
    JournalState.SUBMIT_ATTEMPTED: frozenset(
        {
            JournalState.ACKNOWLEDGED,
            JournalState.SUBMIT_UNRESOLVED,
            JournalState.PARTIALLY_FILLED,
            JournalState.REJECTED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }
    ),
    JournalState.SUBMIT_UNRESOLVED: frozenset(
        {
            JournalState.ACKNOWLEDGED,
            JournalState.PARTIALLY_FILLED,
            JournalState.REJECTED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }
    ),
    JournalState.ACKNOWLEDGED: frozenset(
        {
            JournalState.PARTIALLY_FILLED,
            JournalState.REJECTED,
            JournalState.CANCEL_REQUESTED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }
    ),
    JournalState.PARTIALLY_FILLED: frozenset(
        {
            JournalState.CANCEL_REQUESTED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }
    ),
    JournalState.CANCEL_REQUESTED: frozenset(
        {
            JournalState.ACKNOWLEDGED,
            JournalState.PARTIALLY_FILLED,
            JournalState.REJECTED,
            JournalState.CANCEL_UNRESOLVED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }
    ),
    JournalState.CANCEL_UNRESOLVED: frozenset(
        {
            JournalState.ACKNOWLEDGED,
            JournalState.PARTIALLY_FILLED,
            JournalState.REJECTED,
            JournalState.CANCEL_REQUESTED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }
    ),
    JournalState.REJECTED: frozenset({JournalState.RECONCILED}),
    JournalState.CANCELED: frozenset({JournalState.RECONCILED}),
    JournalState.FILLED: frozenset({JournalState.RECONCILED}),
    JournalState.EXPIRED: frozenset({JournalState.RECONCILED}),
    JournalState.RECONCILED: frozenset(),
}

_BROKER_ID_REQUIRED_STATES = frozenset(
    {
        JournalState.ACKNOWLEDGED,
        JournalState.PARTIALLY_FILLED,
        JournalState.FILLED,
    }
)

_MARKER_ALLOWED_STATES: dict[str, frozenset[JournalState]] = {
    "submit_not_dispatched": frozenset({JournalState.INTENT_RECORDED}),
    "cancel_not_dispatched": frozenset(
        {JournalState.ACKNOWLEDGED, JournalState.PARTIALLY_FILLED}
    ),
    "cancel_dispatch_attempted": frozenset({JournalState.CANCEL_REQUESTED}),
    "cancel_request_accepted": frozenset({JournalState.CANCEL_REQUESTED}),
}
_ALLOWED_MARKER_EVENTS = frozenset(_MARKER_ALLOWED_STATES)


@dataclass(frozen=True)
class OrderJournalRecord:
    """Current durable state of one canonical order intent."""

    client_order_id: str
    fingerprint_sha256: str
    intent: dict[str, object]
    state: JournalState
    broker_order_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OrderJournalEvent:
    """One immutable journal event, ordered by ``sequence``."""

    sequence: int
    client_order_id: str
    event_type: str
    from_state: JournalState | None
    to_state: JournalState
    broker_order_id: str | None
    metadata: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class OrderJournalStorageIdentity:
    """Stable identity of the validated SQLite journal file."""

    device: int
    inode: int
    created_at: str


@dataclass(frozen=True)
class OrderJournalReconciliationAttestation:
    """Digest of a journal whose materialized intents are all reconciled."""

    record_count: int
    max_event_sequence: int
    projection_sha256: str


def canonical_intent_json(intent: Mapping[str, object]) -> str:
    """Serialize an intent deterministically for identity comparison.

    Mapping keys must be strings; tuples are normalized to arrays; ``None`` is
    retained as JSON ``null``; finite floats use Python's deterministic shortest
    round-trip JSON representation.  Negative zero is normalized to positive
    zero so equivalent zero quantities do not collide.  NaN and infinities are
    rejected because they are not valid financial order values.
    """

    if not isinstance(intent, Mapping):
        raise InvalidOrderIntentError("order intent must be a mapping")
    normalized = _normalize_json_value(intent, location="intent")
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # defensive: normalization is strict
        raise InvalidOrderIntentError("order intent is not canonical JSON") from exc


def intent_fingerprint(intent: Mapping[str, object]) -> str:
    """Return the SHA-256 fingerprint of a canonical order intent."""

    canonical = canonical_intent_json(intent)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DurableOrderJournal:
    """SQLite-backed order-intent journal with atomic state/event updates."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        raw_path = str(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise InvalidOrderIntentError("order journal must use a durable filesystem path")
        self.path = Path(path)
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._initialize()

    def record_intent(
        self,
        client_order_id: str,
        intent: Mapping[str, object],
    ) -> tuple[OrderJournalRecord, bool]:
        """Persist an intent, returning ``(record, created)``.

        Replaying the same ID and canonical intent returns the existing record
        and ``False`` without adding a duplicate event.  Reusing the ID for any
        different intent raises :class:`OrderIntentCollisionError`.
        """

        clean_id = _validate_identifier(client_order_id, label="client_order_id")
        intent_json = canonical_intent_json(intent)
        fingerprint = hashlib.sha256(intent_json.encode("utf-8")).hexdigest()
        timestamp = self._timestamp()

        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (clean_id,),
            ).fetchone()
            if row is not None:
                record = self._record_from_row(row)
                if record.fingerprint_sha256 != fingerprint or _canonical_from_record(record) != intent_json:
                    raise OrderIntentCollisionError(
                        f"client_order_id {clean_id!r} is already bound to a different intent"
                    )
                connection.commit()
                return record, False

            connection.execute(
                """
                INSERT INTO order_intents (
                    client_order_id, fingerprint_sha256, intent_json, state,
                    broker_order_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    clean_id,
                    fingerprint,
                    intent_json,
                    JournalState.INTENT_RECORDED.value,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                client_order_id=clean_id,
                event_type="intent_recorded",
                from_state=None,
                to_state=JournalState.INTENT_RECORDED,
                broker_order_id=None,
                metadata_json="{}",
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - SQLite transaction invariant
                raise OrderJournalStorageError("created order intent could not be read back")
            record = self._record_from_row(row)
            connection.commit()
            return record, True
        except OrderJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise OrderJournalStorageError("failed to record order intent; execution must remain blocked") from exc
        finally:
            connection.close()

    def get(self, client_order_id: str) -> OrderJournalRecord | None:
        """Return the current record for ``client_order_id``, if present."""

        clean_id = _validate_identifier(client_order_id, label="client_order_id")
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (clean_id,),
            ).fetchone()
            return None if row is None else self._record_from_row(row)
        except OrderJournalError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise OrderJournalStorageError("failed to read order journal; execution must remain blocked") from exc
        finally:
            connection.close()

    def records(self) -> tuple[OrderJournalRecord, ...]:
        """Return every current intent in deterministic identity order.

        Safe account-wide reconciliation must inspect the materialized current
        states, not infer them only from the append-only event stream.  Opening
        the connection retains the journal's schema, semantic-integrity and
        pinned-storage checks.
        """

        connection = self._open_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM order_intents ORDER BY client_order_id"
            ).fetchall()
            return tuple(self._record_from_row(row) for row in rows)
        except OrderJournalError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise OrderJournalStorageError(
                "failed to read order journal records; execution must remain blocked"
            ) from exc
        finally:
            connection.close()

    def reconcile_terminal_records(
        self,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> OrderJournalReconciliationAttestation:
        """Atomically reconcile every terminal intent or reject the batch.

        Broker observations remain the caller's responsibility.  This method
        only provides an all-or-nothing journal transition after the caller has
        refreshed nonterminal records from positive broker evidence.
        """

        metadata_json = _canonical_metadata_json(metadata)
        timestamp = self._timestamp()
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM order_intents ORDER BY client_order_id"
            ).fetchall()
            records = tuple(self._record_from_row(row) for row in rows)
            nonterminal = tuple(
                record.client_order_id
                for record in records
                if record.state not in _TERMINAL_STATES
                and record.state is not JournalState.RECONCILED
            )
            if nonterminal:
                raise InvalidOrderTransitionError(
                    "order journal contains nonterminal intents; reconciliation is blocked"
                )
            for record in records:
                if record.state is JournalState.RECONCILED:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE order_intents
                    SET state = 'reconciled', updated_at = ?
                    WHERE client_order_id = ? AND state = ?
                    """,
                    (timestamp, record.client_order_id, record.state.value),
                )
                if cursor.rowcount != 1:
                    raise OrderJournalStorageError(
                        "order journal changed during batch reconciliation"
                    )
                self._append_event(
                    connection,
                    client_order_id=record.client_order_id,
                    event_type="state_transition",
                    from_state=record.state,
                    to_state=JournalState.RECONCILED,
                    broker_order_id=record.broker_order_id,
                    metadata_json=metadata_json,
                    created_at=timestamp,
                )
            attestation = self._reconciliation_attestation_from_connection(connection)
            connection.commit()
            return attestation
        except OrderJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise OrderJournalStorageError(
                "failed to reconcile order journal; execution must remain blocked"
            ) from exc
        finally:
            connection.close()

    def reconciliation_attestation(self) -> OrderJournalReconciliationAttestation:
        """Return a stable attestation only when every intent is reconciled."""

        connection = self._open_connection()
        try:
            return self._reconciliation_attestation_from_connection(connection)
        except OrderJournalError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise OrderJournalStorageError(
                "failed to attest order journal reconciliation"
            ) from exc
        finally:
            connection.close()

    def transition(
        self,
        client_order_id: str,
        state: JournalState | str,
        *,
        broker_order_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
        expected_state: JournalState | str | None = None,
    ) -> tuple[OrderJournalRecord, bool]:
        """Atomically change state and append an event.

        Returns ``(record, changed)``.  Replaying the current state with a
        compatible broker ID returns ``False`` and does not add an event.  The
        optional ``expected_state`` is a compare-and-swap guard for concurrent
        supervisors; a mismatch fails closed.
        """

        clean_id = _validate_identifier(client_order_id, label="client_order_id")
        target = _coerce_state(state)
        expected = None if expected_state is None else _coerce_state(expected_state)
        clean_broker_id = None
        if broker_order_id is not None:
            clean_broker_id = _validate_identifier(broker_order_id, label="broker_order_id")
        metadata_json = _canonical_metadata_json(metadata)
        timestamp = self._timestamp()

        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise OrderIntentNotFoundError(f"unknown client_order_id {clean_id!r}")
            current = self._record_from_row(row)

            if expected is not None and current.state is not expected:
                raise InvalidOrderTransitionError(
                    f"stale state for {clean_id!r}: expected {expected.value}, found {current.state.value}"
                )
            if (
                current.broker_order_id is not None
                and clean_broker_id is not None
                and current.broker_order_id != clean_broker_id
            ):
                raise BrokerOrderIdCollisionError(
                    f"client_order_id {clean_id!r} is already bound to broker order "
                    f"{current.broker_order_id!r}"
                )
            if clean_broker_id is not None:
                collision = connection.execute(
                    """
                    SELECT client_order_id FROM order_intents
                    WHERE broker_order_id = ? AND client_order_id <> ?
                    """,
                    (clean_broker_id, clean_id),
                ).fetchone()
                if collision is not None:
                    raise BrokerOrderIdCollisionError(
                        f"broker_order_id {clean_broker_id!r} is already bound to another intent"
                    )

            resolved_broker_id = current.broker_order_id or clean_broker_id
            if current.state is target:
                if clean_broker_id is not None and resolved_broker_id != clean_broker_id:
                    raise BrokerOrderIdCollisionError(
                        f"client_order_id {clean_id!r} has a conflicting broker order ID"
                    )
                connection.commit()
                return current, False

            allowed = _ALLOWED_TRANSITIONS[current.state]
            if target not in allowed:
                raise InvalidOrderTransitionError(
                    f"transition {current.state.value!r} -> {target.value!r} is not allowed"
                )
            if target in _BROKER_ID_REQUIRED_STATES and resolved_broker_id is None:
                raise InvalidOrderTransitionError(
                    f"transition to {target.value!r} requires broker_order_id"
                )

            cursor = connection.execute(
                """
                UPDATE order_intents
                SET state = ?, broker_order_id = ?, updated_at = ?
                WHERE client_order_id = ? AND state = ?
                """,
                (
                    target.value,
                    resolved_broker_id,
                    timestamp,
                    clean_id,
                    current.state.value,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - write lock makes this defensive
                raise OrderJournalStorageError("journal state changed concurrently; execution must remain blocked")
            self._append_event(
                connection,
                client_order_id=clean_id,
                event_type="state_transition",
                from_state=current.state,
                to_state=target,
                broker_order_id=resolved_broker_id,
                metadata_json=metadata_json,
                created_at=timestamp,
            )
            updated_row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (clean_id,),
            ).fetchone()
            if updated_row is None:  # pragma: no cover - SQLite transaction invariant
                raise OrderJournalStorageError("transitioned order intent could not be read back")
            updated = self._record_from_row(updated_row)
            connection.commit()
            return updated, True
        except OrderJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise OrderJournalStorageError("failed to update order journal; execution must remain blocked") from exc
        finally:
            connection.close()

    def record_marker(
        self,
        client_order_id: str,
        event_type: str,
        *,
        expected_state: JournalState | str,
        metadata: Mapping[str, object] | None = None,
    ) -> OrderJournalRecord:
        """Append an allowlisted same-state side-effect marker atomically.

        A state transition alone cannot distinguish a prepared broker action
        from an accepted one when both observations keep the same order state.
        These markers make the exact boundary around cancel DELETE durable
        without changing the v2 SQLite schema used by existing paper journals.
        """

        clean_id = _validate_identifier(client_order_id, label="client_order_id")
        if event_type not in _ALLOWED_MARKER_EVENTS:
            raise InvalidOrderTransitionError("order journal marker type is not allowed")
        expected = _coerce_state(expected_state)
        if expected not in _MARKER_ALLOWED_STATES[event_type]:
            raise InvalidOrderTransitionError("order journal marker state is not allowed")
        metadata_json = _canonical_metadata_json(metadata)
        timestamp = self._timestamp()

        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise OrderIntentNotFoundError(f"unknown client_order_id {clean_id!r}")
            current = self._record_from_row(row)
            if current.state is not expected:
                raise InvalidOrderTransitionError(
                    f"stale state for {clean_id!r}: expected {expected.value}, "
                    f"found {current.state.value}"
                )
            self._append_event(
                connection,
                client_order_id=clean_id,
                event_type=event_type,
                from_state=current.state,
                to_state=current.state,
                broker_order_id=current.broker_order_id,
                metadata_json=metadata_json,
                created_at=timestamp,
            )
            connection.execute(
                "UPDATE order_intents SET updated_at = ? WHERE client_order_id = ? AND state = ?",
                (timestamp, clean_id, current.state.value),
            )
            updated_row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (clean_id,),
            ).fetchone()
            if updated_row is None:  # pragma: no cover - SQLite transaction invariant
                raise OrderJournalStorageError("marked order intent could not be read back")
            updated = self._record_from_row(updated_row)
            connection.commit()
            return updated
        except OrderJournalError:
            _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            _rollback_quietly(connection)
            raise OrderJournalStorageError(
                "failed to record broker side-effect marker; execution must remain blocked"
            ) from exc
        finally:
            connection.close()

    def events(self, client_order_id: str | None = None) -> tuple[OrderJournalEvent, ...]:
        """Return append-only events in durable sequence order."""

        clean_id = None
        if client_order_id is not None:
            clean_id = _validate_identifier(client_order_id, label="client_order_id")
        connection = self._open_connection()
        try:
            if clean_id is None:
                rows = connection.execute("SELECT * FROM order_events ORDER BY sequence").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM order_events WHERE client_order_id = ? ORDER BY sequence",
                    (clean_id,),
                ).fetchall()
            return tuple(self._event_from_row(row) for row in rows)
        except OrderJournalError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise OrderJournalStorageError("failed to read journal events; execution must remain blocked") from exc
        finally:
            connection.close()

    def storage_identity(self) -> OrderJournalStorageIdentity:
        """Return a validated file/metadata identity without creating a journal.

        Device/inode pins the filesystem object while ``created_at`` pins the
        journal metadata.  The before/after stat rejects replacement during
        the read itself; callers can retain this value and compare it before
        later safety decisions.
        """

        before = self._storage_stat()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT created_at FROM journal_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None or not isinstance(row[0], str) or not row[0].strip():
                raise OrderJournalSchemaError("order journal metadata identity is invalid")
            created_at = str(row[0])
        except OrderJournalError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise OrderJournalStorageError(
                "failed to read order journal identity; execution must remain blocked"
            ) from exc
        finally:
            connection.close()
        after = self._storage_stat()
        if before != after:
            raise OrderJournalStorageError(
                "order journal changed during identity read; execution must remain blocked"
            )
        return OrderJournalStorageIdentity(
            device=before[0],
            inode=before[1],
            created_at=created_at,
        )

    def _storage_stat(self) -> tuple[int, int]:
        try:
            stat_result = self.path.stat()
        except OSError as exc:
            raise OrderJournalStorageError(
                "order journal file is missing; execution must remain blocked"
            ) from exc
        if not self.path.is_file():
            raise OrderJournalStorageError(
                "order journal path is not a regular file; execution must remain blocked"
            )
        return int(stat_result.st_dev), int(stat_result.st_ino)

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OrderJournalStorageError("could not create order journal directory") from exc

        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect(allow_create=True)
            connection.execute("BEGIN IMMEDIATE")
            existing_tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            }
            if not existing_tables:
                self._create_schema(connection)
            self._validate_schema(connection)
            connection.commit()
        except OrderJournalError:
            if connection is not None:
                _rollback_quietly(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                _rollback_quietly(connection)
            raise OrderJournalStorageError(
                "order journal initialization failed; execution must remain blocked"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def _connect(self, *, allow_create: bool) -> sqlite3.Connection:
        if not allow_create and not self.path.is_file():
            raise OrderJournalStorageError("order journal file is missing; execution must remain blocked")
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            mode_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode_row is None or str(mode_row[0]).lower() != "wal":
                raise OrderJournalStorageError("order journal could not enable WAL mode")
            connection.execute("PRAGMA synchronous = FULL")
            sync_row = connection.execute("PRAGMA synchronous").fetchone()
            if sync_row is None or int(sync_row[0]) != 2:
                raise OrderJournalStorageError("order journal could not enable synchronous=FULL")
            return connection
        except Exception:
            connection.close()
            raise

    def _open_connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect(allow_create=False)
            # Revalidate on every operation so a replaced database, downgraded
            # schema, or removed append-only trigger cannot silently weaken the
            # safety boundary after this object was constructed.
            connection.execute("BEGIN")
            self._validate_schema(connection)
            connection.commit()
            return connection
        except OrderJournalError:
            if connection is not None:
                _rollback_quietly(connection)
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                _rollback_quietly(connection)
                connection.close()
            raise OrderJournalStorageError("order journal is unavailable; execution must remain blocked") from exc

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        allowed_states = ",".join(f"'{state.value}'" for state in JournalState)
        statements = (
            """
            CREATE TABLE journal_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE order_intents (
                client_order_id TEXT PRIMARY KEY,
                fingerprint_sha256 TEXT NOT NULL,
                intent_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({allowed_states})),
                broker_order_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE order_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT CHECK (from_state IS NULL OR from_state IN ({allowed_states})),
                to_state TEXT NOT NULL CHECK (to_state IN ({allowed_states})),
                broker_order_id TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (client_order_id) REFERENCES order_intents(client_order_id)
            )
            """,
            """
            CREATE INDEX order_events_client_sequence
            ON order_events(client_order_id, sequence)
            """,
            """
            CREATE UNIQUE INDEX order_intents_broker_order_id_unique
            ON order_intents(broker_order_id)
            WHERE broker_order_id IS NOT NULL
            """,
            """
            CREATE TRIGGER order_events_no_update
            BEFORE UPDATE ON order_events
            BEGIN SELECT RAISE(ABORT, 'order_events_append_only'); END
            """,
            """
            CREATE TRIGGER order_events_no_delete
            BEFORE DELETE ON order_events
            BEGIN SELECT RAISE(ABORT, 'order_events_append_only'); END
            """,
            """
            CREATE TRIGGER order_intents_no_delete
            BEFORE DELETE ON order_intents
            BEGIN SELECT RAISE(ABORT, 'order_intents_durable'); END
            """,
            """
            CREATE TRIGGER order_intents_identity_immutable
            BEFORE UPDATE OF client_order_id, fingerprint_sha256, intent_json, created_at
            ON order_intents
            BEGIN SELECT RAISE(ABORT, 'order_intent_identity_immutable'); END
            """,
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO journal_metadata (singleton, schema_version, created_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, self._timestamp()),
        )

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        expected_tables = {"journal_metadata", "order_intents", "order_events"}
        actual_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }
        if actual_tables != expected_tables:
            raise OrderJournalSchemaError(
                f"unexpected order journal tables: expected {sorted(expected_tables)}, found {sorted(actual_tables)}"
            )

        expected_columns = {
            "journal_metadata": ("singleton", "schema_version", "created_at"),
            "order_intents": (
                "client_order_id",
                "fingerprint_sha256",
                "intent_json",
                "state",
                "broker_order_id",
                "created_at",
                "updated_at",
            ),
            "order_events": (
                "sequence",
                "client_order_id",
                "event_type",
                "from_state",
                "to_state",
                "broker_order_id",
                "metadata_json",
                "created_at",
            ),
        }
        for table, columns in expected_columns.items():
            actual_columns = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
            if actual_columns != columns:
                raise OrderJournalSchemaError(f"unexpected schema for {table!r}")

        metadata_rows = connection.execute(
            "SELECT singleton, schema_version FROM journal_metadata"
        ).fetchall()
        if len(metadata_rows) != 1 or int(metadata_rows[0][0]) != 1:
            raise OrderJournalSchemaError("order journal metadata row is invalid")
        if int(metadata_rows[0][1]) != SCHEMA_VERSION:
            raise OrderJournalSchemaError(
                f"unsupported order journal schema version {metadata_rows[0][1]!r}"
            )

        expected_triggers = {
            "order_events_no_update",
            "order_events_no_delete",
            "order_intents_no_delete",
            "order_intents_identity_immutable",
        }
        actual_triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        if actual_triggers != expected_triggers:
            raise OrderJournalSchemaError("order journal append-only protections are missing or unexpected")

        expected_indexes = {
            "order_events_client_sequence",
            "order_intents_broker_order_id_unique",
        }
        actual_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if actual_indexes != expected_indexes:
            raise OrderJournalSchemaError("order journal uniqueness/index protections are missing or unexpected")

        integrity_row = connection.execute("PRAGMA quick_check").fetchone()
        if integrity_row is None or str(integrity_row[0]).lower() != "ok":
            raise OrderJournalSchemaError("order journal failed SQLite integrity check")
        self._validate_semantic_integrity(connection)

    def _validate_semantic_integrity(self, connection: sqlite3.Connection) -> None:
        records = {
            str(row["client_order_id"]): self._record_from_row(row)
            for row in connection.execute("SELECT * FROM order_intents")
        }
        events_by_id: dict[str, list[OrderJournalEvent]] = {client_id: [] for client_id in records}
        for row in connection.execute("SELECT * FROM order_events ORDER BY sequence"):
            event = self._event_from_row(row)
            if event.client_order_id not in events_by_id:
                raise OrderJournalSchemaError("order journal contains an orphan event")
            events_by_id[event.client_order_id].append(event)

        for client_id, record in records.items():
            events = events_by_id[client_id]
            if not events:
                raise OrderJournalSchemaError("order journal intent has no event history")
            first = events[0]
            if (
                first.event_type != "intent_recorded"
                or first.from_state is not None
                or first.to_state is not JournalState.INTENT_RECORDED
                or first.broker_order_id is not None
            ):
                raise OrderJournalSchemaError("order journal initial event is invalid")

            state = JournalState.INTENT_RECORDED
            broker_order_id: str | None = None
            for event in events[1:]:
                if event.event_type in _ALLOWED_MARKER_EVENTS:
                    if event.from_state is not state or event.to_state is not state:
                        raise OrderJournalSchemaError("order journal marker state is inconsistent")
                    if state not in _MARKER_ALLOWED_STATES[event.event_type]:
                        raise OrderJournalSchemaError("order journal marker state is not allowed")
                    if event.broker_order_id != broker_order_id:
                        raise OrderJournalSchemaError("order journal marker broker identity changed")
                    continue
                if event.event_type != "state_transition" or event.from_state is not state:
                    raise OrderJournalSchemaError("order journal event chain is inconsistent")
                if event.to_state not in _ALLOWED_TRANSITIONS[state]:
                    raise OrderJournalSchemaError("order journal event transition is invalid")
                if broker_order_id is not None and event.broker_order_id != broker_order_id:
                    raise OrderJournalSchemaError("order journal broker identity changed in event history")
                if event.broker_order_id is not None:
                    broker_order_id = event.broker_order_id
                if event.to_state in _BROKER_ID_REQUIRED_STATES and broker_order_id is None:
                    raise OrderJournalSchemaError("order journal event lacks required broker identity")
                state = event.to_state

            if state is not record.state or broker_order_id != record.broker_order_id:
                raise OrderJournalSchemaError("order journal record does not match its event history")

    def _record_from_row(self, row: sqlite3.Row) -> OrderJournalRecord:
        try:
            intent = json.loads(str(row["intent_json"]))
            if not isinstance(intent, dict):
                raise ValueError("intent_json is not an object")
            canonical = canonical_intent_json(intent)
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            stored_fingerprint = str(row["fingerprint_sha256"])
            if fingerprint != stored_fingerprint:
                raise ValueError("intent fingerprint mismatch")
            state = JournalState(str(row["state"]))
            broker_order_id = row["broker_order_id"]
            if state in _BROKER_ID_REQUIRED_STATES and not broker_order_id:
                raise ValueError("broker order ID missing for broker-confirmed state")
            return OrderJournalRecord(
                client_order_id=str(row["client_order_id"]),
                fingerprint_sha256=stored_fingerprint,
                intent=dict(intent),
                state=state,
                broker_order_id=None if broker_order_id is None else str(broker_order_id),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        except (InvalidOrderIntentError, KeyError, TypeError, ValueError) as exc:
            raise OrderJournalStorageError("order journal record failed integrity validation") from exc

    def _event_from_row(self, row: sqlite3.Row) -> OrderJournalEvent:
        try:
            metadata = json.loads(str(row["metadata_json"]))
            if not isinstance(metadata, dict):
                raise ValueError("event metadata is not an object")
            from_raw = row["from_state"]
            return OrderJournalEvent(
                sequence=int(row["sequence"]),
                client_order_id=str(row["client_order_id"]),
                event_type=str(row["event_type"]),
                from_state=None if from_raw is None else JournalState(str(from_raw)),
                to_state=JournalState(str(row["to_state"])),
                broker_order_id=None if row["broker_order_id"] is None else str(row["broker_order_id"]),
                metadata=dict(metadata),
                created_at=str(row["created_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OrderJournalStorageError("order journal event failed integrity validation") from exc

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        client_order_id: str,
        event_type: str,
        from_state: JournalState | None,
        to_state: JournalState,
        broker_order_id: str | None,
        metadata_json: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO order_events (
                client_order_id, event_type, from_state, to_state,
                broker_order_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_order_id,
                event_type,
                None if from_state is None else from_state.value,
                to_state.value,
                broker_order_id,
                metadata_json,
                created_at,
            ),
        )

    def _reconciliation_attestation_from_connection(
        self,
        connection: sqlite3.Connection,
    ) -> OrderJournalReconciliationAttestation:
        rows = connection.execute(
            "SELECT * FROM order_intents ORDER BY client_order_id"
        ).fetchall()
        records = tuple(self._record_from_row(row) for row in rows)
        if any(record.state is not JournalState.RECONCILED for record in records):
            raise InvalidOrderTransitionError(
                "order journal is not fully reconciled"
            )
        projection = [
            {
                "broker_order_id": record.broker_order_id,
                "client_order_id": record.client_order_id,
                "fingerprint_sha256": record.fingerprint_sha256,
                "state": record.state.value,
                "updated_at": record.updated_at,
            }
            for record in records
        ]
        projection_json = json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        sequence_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM order_events"
        ).fetchone()
        if sequence_row is None:
            raise OrderJournalStorageError(
                "order journal event sequence is unavailable"
            )
        return OrderJournalReconciliationAttestation(
            record_count=len(records),
            max_event_sequence=int(sequence_row[0]),
            projection_sha256=hashlib.sha256(
                b"order-journal-reconciliation-v1\0"
                + projection_json.encode("utf-8")
            ).hexdigest(),
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise OrderJournalStorageError("order journal clock must return a timezone-aware datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_from_record(record: OrderJournalRecord) -> str:
    return canonical_intent_json(record.intent)


def _canonical_metadata_json(metadata: Mapping[str, object] | None) -> str:
    if metadata is None:
        return "{}"
    if not isinstance(metadata, Mapping):
        raise InvalidOrderIntentError("journal event metadata must be a mapping")
    normalized = _normalize_json_value(metadata, location="metadata")
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # defensive: normalization is strict
        raise InvalidOrderIntentError("journal event metadata is not canonical JSON") from exc


def _normalize_json_value(value: object, *, location: str) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidOrderIntentError(f"{location} contains a non-finite float")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise InvalidOrderIntentError(f"{location} contains a non-string mapping key")
            normalized[key] = _normalize_json_value(nested, location=f"{location}.{key}")
        return normalized
    if isinstance(value, list | tuple):
        return [
            _normalize_json_value(nested, location=f"{location}[{index}]")
            for index, nested in enumerate(value)
        ]
    raise InvalidOrderIntentError(f"{location} contains unsupported value type {type(value).__name__!r}")


def _validate_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidOrderIntentError(f"{label} must be a string")
    if not value or value != value.strip() or "\x00" in value:
        raise InvalidOrderIntentError(f"{label} must be non-empty and free of surrounding whitespace/NUL")
    return value


def _coerce_state(value: JournalState | str) -> JournalState:
    try:
        return value if isinstance(value, JournalState) else JournalState(value)
    except (TypeError, ValueError) as exc:
        raise InvalidOrderTransitionError(f"unsupported order journal state {value!r}") from exc


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        with suppress(sqlite3.Error):
            connection.rollback()
