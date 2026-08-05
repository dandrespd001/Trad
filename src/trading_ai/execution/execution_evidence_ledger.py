"""Durable, broker-agnostic execution-evidence ledger.

The ledger stores only evidence.  It cannot place orders, connect to a broker,
or decide whether trading should be promoted.  Policies are registered before
their evidence window starts, individual fills are keyed by an immutable
activity ID, incident state is derived from append-only events, and manifests
declare the complete set of fills expected for a policy window.

SQLite runs in WAL mode with full synchronization, foreign-key enforcement,
and a bounded busy timeout.  Canonical payload hashes and append-only hash
chains make accidental mutation or partial deletion detectable.  The hashes
are not signatures: a privileged writer able to replace the whole database can
also rebuild every hash, so callers that need tamper evidence across trust
boundaries must anchor a verification result outside this file.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_GENESIS_HASH = "0" * 64
_HASH_LENGTH = 64
_CHAIN_LAST_SQL = {
    "fills": "SELECT sequence, record_sha256 FROM fills ORDER BY sequence DESC LIMIT 1",
    "manifests": "SELECT sequence, record_sha256 FROM manifests ORDER BY sequence DESC LIMIT 1",
    "incident_events": (
        "SELECT sequence, record_sha256 FROM incident_events ORDER BY sequence DESC LIMIT 1"
    ),
}
_CHAIN_HEAD_SQL = {
    table: query.replace("sequence, ", "") for table, query in _CHAIN_LAST_SQL.items()
}
_COUNT_SQL_PREFIX = {
    "fills": "SELECT COUNT(*) FROM fills WHERE policy_sha256 IN ",
    "manifests": "SELECT COUNT(*) FROM manifests WHERE policy_sha256 IN ",
    "incident_events": "SELECT COUNT(*) FROM incident_events WHERE policy_sha256 IN ",
}


class ExecutionEvidenceLedgerError(RuntimeError):
    """Base class for fail-closed evidence-ledger failures."""


class EvidenceLedgerStorageError(ExecutionEvidenceLedgerError):
    """The durable store could not be opened, validated, or updated."""


class InvalidExecutionEvidenceError(ExecutionEvidenceLedgerError, ValueError):
    """Evidence is malformed, non-finite, temporally invalid, or incomplete."""


class ExecutionEvidenceConflictError(ExecutionEvidenceLedgerError):
    """A stable evidence ID was reused for different canonical content."""


class IncidentTransitionError(ExecutionEvidenceLedgerError):
    """An incident event is incompatible with its append-only history."""


class IncidentEventType(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class PolicyRegistration:
    policy_sha256: str
    policy_id: str
    policy: dict[str, object]
    effective_from: str
    effective_until: str
    registered_at: str


@dataclass(frozen=True)
class FillEvidence:
    sequence: int
    activity_id: str
    policy_sha256: str
    payload_sha256: str
    payload: dict[str, object]
    transaction_time: str
    recorded_at: str
    previous_record_sha256: str
    record_sha256: str


@dataclass(frozen=True)
class EvidenceManifest:
    sequence: int
    manifest_id: str
    policy_sha256: str
    window_start: str
    window_end: str
    captured_at: str
    expected_activity_ids: tuple[str, ...]
    metadata: dict[str, object]
    payload_sha256: str
    recorded_at: str
    previous_record_sha256: str
    record_sha256: str


@dataclass(frozen=True)
class IncidentEvent:
    sequence: int
    event_id: str
    incident_id: str
    policy_sha256: str
    event_type: IncidentEventType
    event_time: str
    payload: dict[str, object]
    payload_sha256: str
    recorded_at: str
    previous_record_sha256: str
    record_sha256: str


@dataclass(frozen=True)
class EvidenceVerification:
    """Read-only verification result.

    ``integrity_valid`` covers SQLite integrity, canonical payload hashes,
    foreign keys, temporal invariants, and every append-only hash chain.
    ``completeness_valid`` requires manifests to cover the full selected policy
    window and for each manifest to exactly enumerate the fills in its window.
    ``operationally_clear`` is false while any selected-policy incident remains
    open.  ``valid`` is the conjunction of all three properties.
    """

    valid: bool
    integrity_valid: bool
    completeness_valid: bool
    operationally_clear: bool
    issues: tuple[str, ...]
    policy_count: int
    fill_count: int
    manifest_count: int
    incident_event_count: int
    open_incident_ids: tuple[str, ...]
    chain_heads: dict[str, str]


def canonical_evidence_json(value: object) -> str:
    """Return strict deterministic JSON, rejecting NaN, infinity, and bad dates."""

    normalized = _normalize_json(value, location="evidence")
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # defensive: normalizer is strict
        raise InvalidExecutionEvidenceError("evidence is not canonical JSON") from exc


def evidence_sha256(value: object) -> str:
    """Hash canonical evidence with SHA-256."""

    return _sha256_text(canonical_evidence_json(value))


class DurableExecutionEvidenceLedger:
    """SQLite-backed append-only evidence ledger with fail-closed writes."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        raw_path = str(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise InvalidExecutionEvidenceError("ledger must use a durable filesystem path")
        if isinstance(busy_timeout_ms, bool) or busy_timeout_ms < 1:
            raise InvalidExecutionEvidenceError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._initialize()

    def register_policy(
        self,
        policy_id: str,
        policy: Mapping[str, object],
        *,
        effective_from: str | datetime,
        effective_until: str | datetime,
    ) -> tuple[PolicyRegistration, bool]:
        """Pre-register one immutable policy, returning ``(record, created)``.

        The ledger clock must be strictly earlier than ``effective_from``.
        Replaying the same ID and exact canonical envelope is idempotent;
        changing the policy or window for that ID is a blocking conflict.
        """

        clean_id = _identifier(policy_id, field="policy_id")
        if not isinstance(policy, Mapping) or not policy:
            raise InvalidExecutionEvidenceError("policy must be a non-empty mapping")
        normalized_policy = _mapping_from_canonical(policy, field="policy")
        start = _timestamp(effective_from, field="effective_from")
        end = _timestamp(effective_until, field="effective_until")
        start_dt = _parse_timestamp(start, field="effective_from")
        end_dt = _parse_timestamp(end, field="effective_until")
        if end_dt <= start_dt:
            raise InvalidExecutionEvidenceError("effective_until must follow effective_from")
        now = self._now()
        registered_at = _format_timestamp(now)
        policy_json = canonical_evidence_json(normalized_policy)
        envelope = {
            "effective_from": start,
            "effective_until": end,
            "policy": normalized_policy,
            "policy_id": clean_id,
        }
        fingerprint = evidence_sha256(envelope)

        connection = self._open_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_integrity(connection)
            by_id = connection.execute(
                "SELECT * FROM policies WHERE policy_id = ?",
                (clean_id,),
            ).fetchone()
            if by_id is not None:
                existing = self._policy_from_row(by_id)
                if existing.policy_sha256 != fingerprint:
                    raise ExecutionEvidenceConflictError(
                        f"policy_id {clean_id!r} is bound to different canonical content"
                    )
                connection.commit()
                return existing, False
            by_hash = connection.execute(
                "SELECT * FROM policies WHERE policy_sha256 = ?",
                (fingerprint,),
            ).fetchone()
            if by_hash is not None:
                existing = self._policy_from_row(by_hash)
                if existing.policy_id != clean_id:
                    raise ExecutionEvidenceConflictError(
                        "canonical policy hash is already bound to another policy_id"
                    )
                connection.commit()
                return existing, False
            if now >= start_dt:
                raise InvalidExecutionEvidenceError("policy must be registered before effective_from")
            connection.execute(
                """
                INSERT INTO policies (
                    policy_sha256, policy_id, policy_json, effective_from,
                    effective_until, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (fingerprint, clean_id, policy_json, start, end, registered_at),
            )
            connection.commit()
            return PolicyRegistration(
                policy_sha256=fingerprint,
                policy_id=clean_id,
                policy=normalized_policy,
                effective_from=start,
                effective_until=end,
                registered_at=registered_at,
            ), True
        except ExecutionEvidenceLedgerError:
            _rollback(connection)
            raise
        except sqlite3.Error as exc:
            _rollback(connection)
            raise EvidenceLedgerStorageError("could not register execution policy") from exc
        finally:
            connection.close()

    def record_fill(
        self,
        activity_id: str,
        policy_sha256: str,
        payload: Mapping[str, object],
    ) -> tuple[FillEvidence, bool]:
        """Append an individual fill activity or accept an identical replay.

        Required payload fields are ``order_id``, ``symbol``, ``side``,
        ``quantity``, ``price``, and timezone-aware ``transaction_time``.
        Quantities and prices are persisted as canonical decimal text.
        """

        clean_activity_id = _identifier(activity_id, field="activity_id")
        clean_policy_hash = _hash(policy_sha256, field="policy_sha256")
        normalized = _normalize_fill(clean_activity_id, payload)
        transaction_time = str(normalized["transaction_time"])
        payload_json = canonical_evidence_json(normalized)
        payload_hash = _sha256_text(payload_json)
        now = self._now()
        transaction_dt = _parse_timestamp(transaction_time, field="transaction_time")
        if transaction_dt > now:
            raise InvalidExecutionEvidenceError("transaction_time cannot be in the future")
        recorded_at = _format_timestamp(now)

        connection = self._open_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_integrity(connection)
            policy = self._policy_row(connection, clean_policy_hash)
            _require_within_policy(transaction_dt, policy, field="transaction_time")
            existing_row = connection.execute(
                "SELECT * FROM fills WHERE activity_id = ?",
                (clean_activity_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._fill_from_row(existing_row)
                if (
                    existing.policy_sha256 != clean_policy_hash
                    or existing.payload_sha256 != payload_hash
                    or canonical_evidence_json(existing.payload) != payload_json
                ):
                    raise ExecutionEvidenceConflictError(
                        f"activity_id {clean_activity_id!r} has conflicting evidence"
                    )
                connection.commit()
                return existing, False

            sequence, previous_hash = _next_chain(connection, "fills")
            record_fields = {
                "activity_id": clean_activity_id,
                "kind": "fill",
                "payload_sha256": payload_hash,
                "policy_sha256": clean_policy_hash,
                "previous_record_sha256": previous_hash,
                "recorded_at": recorded_at,
                "sequence": sequence,
                "transaction_time": transaction_time,
            }
            record_hash = evidence_sha256(record_fields)
            connection.execute(
                """
                INSERT INTO fills (
                    sequence, activity_id, policy_sha256, payload_json,
                    payload_sha256, transaction_time, recorded_at,
                    previous_record_sha256, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    clean_activity_id,
                    clean_policy_hash,
                    payload_json,
                    payload_hash,
                    transaction_time,
                    recorded_at,
                    previous_hash,
                    record_hash,
                ),
            )
            connection.commit()
            return FillEvidence(
                sequence=sequence,
                activity_id=clean_activity_id,
                policy_sha256=clean_policy_hash,
                payload_sha256=payload_hash,
                payload=normalized,
                transaction_time=transaction_time,
                recorded_at=recorded_at,
                previous_record_sha256=previous_hash,
                record_sha256=record_hash,
            ), True
        except ExecutionEvidenceLedgerError:
            _rollback(connection)
            raise
        except sqlite3.Error as exc:
            _rollback(connection)
            raise EvidenceLedgerStorageError("could not append fill evidence") from exc
        finally:
            connection.close()

    def read_policy(self, policy_id: str) -> PolicyRegistration | None:
        """Return one registered policy through a verified read-only handle."""

        clean_id = _identifier(policy_id, field="policy_id")
        connection = self._open_read_only()
        try:
            issues = _collect_integrity_issues(connection)
            if issues:
                raise EvidenceLedgerStorageError(
                    "ledger integrity check failed before policy read: " + "; ".join(issues[:3])
                )
            row = connection.execute(
                "SELECT * FROM policies WHERE policy_id = ?",
                (clean_id,),
            ).fetchone()
            return None if row is None else self._policy_from_row(row)
        except ExecutionEvidenceLedgerError:
            raise
        except sqlite3.Error as exc:
            raise EvidenceLedgerStorageError("could not read execution policy") from exc
        finally:
            connection.close()

    def record_manifest(
        self,
        manifest_id: str,
        policy_sha256: str,
        *,
        window_start: str | datetime,
        window_end: str | datetime,
        captured_at: str | datetime,
        expected_activity_ids: Sequence[str],
        metadata: Mapping[str, object] | None = None,
    ) -> tuple[EvidenceManifest, bool]:
        """Append a completeness manifest for a half-open ``[start, end)`` window."""

        clean_manifest_id = _identifier(manifest_id, field="manifest_id")
        clean_policy_hash = _hash(policy_sha256, field="policy_sha256")
        start = _timestamp(window_start, field="window_start")
        end = _timestamp(window_end, field="window_end")
        captured = _timestamp(captured_at, field="captured_at")
        start_dt = _parse_timestamp(start, field="window_start")
        end_dt = _parse_timestamp(end, field="window_end")
        captured_dt = _parse_timestamp(captured, field="captured_at")
        if end_dt <= start_dt:
            raise InvalidExecutionEvidenceError("window_end must follow window_start")
        if captured_dt < end_dt:
            raise InvalidExecutionEvidenceError("captured_at must be at or after window_end")
        now = self._now()
        if captured_dt > now:
            raise InvalidExecutionEvidenceError("captured_at cannot be in the future")
        ids = _activity_id_set(expected_activity_ids)
        normalized_metadata = _mapping_from_canonical(metadata or {}, field="metadata")
        payload = {
            "captured_at": captured,
            "expected_activity_ids": list(ids),
            "metadata": normalized_metadata,
            "window_end": end,
            "window_start": start,
        }
        payload_json = canonical_evidence_json(payload)
        payload_hash = _sha256_text(payload_json)
        recorded_at = _format_timestamp(now)

        connection = self._open_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_integrity(connection)
            policy = self._policy_row(connection, clean_policy_hash)
            policy_start = _parse_timestamp(policy["effective_from"], field="effective_from")
            policy_end = _parse_timestamp(policy["effective_until"], field="effective_until")
            if start_dt < policy_start or end_dt > policy_end:
                raise InvalidExecutionEvidenceError("manifest window must be inside the policy window")
            existing_row = connection.execute(
                "SELECT * FROM manifests WHERE manifest_id = ?",
                (clean_manifest_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._manifest_from_row(existing_row)
                if (
                    existing.policy_sha256 != clean_policy_hash
                    or existing.payload_sha256 != payload_hash
                    or canonical_evidence_json(_manifest_payload(existing)) != payload_json
                ):
                    raise ExecutionEvidenceConflictError(
                        f"manifest_id {clean_manifest_id!r} has conflicting evidence"
                    )
                connection.commit()
                return existing, False

            sequence, previous_hash = _next_chain(connection, "manifests")
            record_fields = {
                "kind": "manifest",
                "manifest_id": clean_manifest_id,
                "payload_sha256": payload_hash,
                "policy_sha256": clean_policy_hash,
                "previous_record_sha256": previous_hash,
                "recorded_at": recorded_at,
                "sequence": sequence,
            }
            record_hash = evidence_sha256(record_fields)
            connection.execute(
                """
                INSERT INTO manifests (
                    sequence, manifest_id, policy_sha256, window_start,
                    window_end, captured_at, expected_activity_ids_json,
                    metadata_json, payload_sha256, recorded_at,
                    previous_record_sha256, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    clean_manifest_id,
                    clean_policy_hash,
                    start,
                    end,
                    captured,
                    canonical_evidence_json(list(ids)),
                    canonical_evidence_json(normalized_metadata),
                    payload_hash,
                    recorded_at,
                    previous_hash,
                    record_hash,
                ),
            )
            connection.commit()
            return EvidenceManifest(
                sequence=sequence,
                manifest_id=clean_manifest_id,
                policy_sha256=clean_policy_hash,
                window_start=start,
                window_end=end,
                captured_at=captured,
                expected_activity_ids=ids,
                metadata=normalized_metadata,
                payload_sha256=payload_hash,
                recorded_at=recorded_at,
                previous_record_sha256=previous_hash,
                record_sha256=record_hash,
            ), True
        except ExecutionEvidenceLedgerError:
            _rollback(connection)
            raise
        except sqlite3.Error as exc:
            _rollback(connection)
            raise EvidenceLedgerStorageError("could not append completeness manifest") from exc
        finally:
            connection.close()

    def open_incident(
        self,
        incident_id: str,
        event_id: str,
        policy_sha256: str,
        *,
        occurred_at: str | datetime,
        details: Mapping[str, object],
    ) -> tuple[IncidentEvent, bool]:
        """Append an ``OPEN`` event; no mutable incident row is created."""

        return self._record_incident_event(
            incident_id=incident_id,
            event_id=event_id,
            policy_sha256=policy_sha256,
            event_type=IncidentEventType.OPEN,
            event_time=occurred_at,
            payload=details,
        )

    def resolve_incident(
        self,
        incident_id: str,
        event_id: str,
        *,
        resolved_at: str | datetime,
        resolution: Mapping[str, object],
    ) -> tuple[IncidentEvent, bool]:
        """Append a ``RESOLVED`` event; prior evidence is never updated."""

        return self._record_incident_event(
            incident_id=incident_id,
            event_id=event_id,
            policy_sha256=None,
            event_type=IncidentEventType.RESOLVED,
            event_time=resolved_at,
            payload=resolution,
        )

    def verify(
        self,
        *,
        policy_sha256: str | None = None,
    ) -> EvidenceVerification:
        """Verify integrity and manifest completeness using a read-only handle."""

        selected_hash = None if policy_sha256 is None else _hash(policy_sha256, field="policy_sha256")
        connection = self._open_read_only()
        try:
            integrity_issues = _collect_integrity_issues(connection)
            policy_rows = connection.execute("SELECT * FROM policies ORDER BY policy_id").fetchall()
            if selected_hash is not None:
                policy_rows = [row for row in policy_rows if row["policy_sha256"] == selected_hash]
            selected = {str(row["policy_sha256"]): row for row in policy_rows}
            completeness_issues = _completeness_issues(connection, selected)
            if selected_hash is not None and selected_hash not in selected:
                completeness_issues.append("selected policy hash is not registered")
            open_ids = _open_incident_ids(connection, frozenset(selected))
            operationally_clear = not open_ids
            operational_issues = (
                ["selected policy evidence has unresolved incidents"] if open_ids else []
            )
            selected_hashes = tuple(selected)
            fill_count = _count_for_policies(connection, "fills", selected_hashes)
            manifest_count = _count_for_policies(connection, "manifests", selected_hashes)
            incident_count = _count_for_policies(connection, "incident_events", selected_hashes)
            chain_heads = {
                table: _chain_head(connection, table)
                for table in ("fills", "manifests", "incident_events")
            }
            issues = tuple(integrity_issues + completeness_issues + operational_issues)
            integrity_valid = not integrity_issues
            completeness_valid = not completeness_issues
            return EvidenceVerification(
                valid=integrity_valid and completeness_valid and operationally_clear,
                integrity_valid=integrity_valid,
                completeness_valid=completeness_valid,
                operationally_clear=operationally_clear,
                issues=issues,
                policy_count=len(selected),
                fill_count=fill_count,
                manifest_count=manifest_count,
                incident_event_count=incident_count,
                open_incident_ids=tuple(open_ids),
                chain_heads=chain_heads,
            )
        except sqlite3.Error as exc:
            raise EvidenceLedgerStorageError("read-only ledger verification failed") from exc
        finally:
            connection.close()

    def _record_incident_event(
        self,
        *,
        incident_id: str,
        event_id: str,
        policy_sha256: str | None,
        event_type: IncidentEventType,
        event_time: str | datetime,
        payload: Mapping[str, object],
    ) -> tuple[IncidentEvent, bool]:
        clean_incident_id = _identifier(incident_id, field="incident_id")
        clean_event_id = _identifier(event_id, field="event_id")
        clean_policy_hash = None if policy_sha256 is None else _hash(policy_sha256, field="policy_sha256")
        normalized_payload = _mapping_from_canonical(payload, field="incident payload")
        if not normalized_payload:
            raise InvalidExecutionEvidenceError("incident payload must be non-empty")
        canonical_event_time = _timestamp(event_time, field="event_time")
        event_dt = _parse_timestamp(canonical_event_time, field="event_time")
        now = self._now()
        if event_dt > now:
            raise InvalidExecutionEvidenceError("incident event time cannot be in the future")
        payload_json = canonical_evidence_json(normalized_payload)
        payload_hash = _sha256_text(payload_json)
        recorded_at = _format_timestamp(now)

        connection = self._open_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_integrity(connection)
            prior = connection.execute(
                "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY sequence",
                (clean_incident_id,),
            ).fetchall()
            if event_type is IncidentEventType.OPEN:
                if clean_policy_hash is None:  # defensive
                    raise InvalidExecutionEvidenceError("OPEN incident requires a policy hash")
                policy = self._policy_row(connection, clean_policy_hash)
                _require_within_policy(event_dt, policy, field="occurred_at")
            elif not prior:
                raise IncidentTransitionError("cannot resolve an incident that is not open")
            else:
                clean_policy_hash = str(prior[0]["policy_sha256"])

            existing_row = connection.execute(
                "SELECT * FROM incident_events WHERE event_id = ?",
                (clean_event_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._incident_from_row(existing_row)
                if (
                    existing.incident_id != clean_incident_id
                    or existing.event_type is not event_type
                    or existing.policy_sha256 != clean_policy_hash
                    or existing.event_time != canonical_event_time
                    or existing.payload_sha256 != payload_hash
                    or canonical_evidence_json(existing.payload) != payload_json
                ):
                    raise ExecutionEvidenceConflictError(
                        f"event_id {clean_event_id!r} has conflicting incident evidence"
                    )
                connection.commit()
                return existing, False

            if event_type is IncidentEventType.OPEN:
                if prior:
                    raise IncidentTransitionError("incident_id is already present")
            else:
                if len(prior) != 1 or prior[0]["event_type"] != IncidentEventType.OPEN.value:
                    raise IncidentTransitionError("incident is not currently open")
                opened_at = _parse_timestamp(prior[0]["event_time"], field="occurred_at")
                if event_dt < opened_at:
                    raise IncidentTransitionError("resolved_at precedes occurred_at")
                manifest_id = normalized_payload.get("resolution_manifest_id")
                if not isinstance(manifest_id, str) or not manifest_id.strip():
                    raise IncidentTransitionError(
                        "resolution requires a posterior resolution_manifest_id"
                    )
                manifest = connection.execute(
                    "SELECT * FROM manifests WHERE manifest_id = ?",
                    (manifest_id.strip(),),
                ).fetchone()
                if manifest is None or str(manifest["policy_sha256"]) != clean_policy_hash:
                    raise IncidentTransitionError(
                        "resolution manifest is missing or belongs to another policy"
                    )
                manifest_recorded = _parse_timestamp(
                    manifest["recorded_at"],
                    field="manifest.recorded_at",
                )
                incident_recorded = _parse_timestamp(
                    prior[0]["recorded_at"],
                    field="incident.recorded_at",
                )
                manifest_captured = _parse_timestamp(
                    manifest["captured_at"],
                    field="manifest.captured_at",
                )
                if manifest_recorded <= incident_recorded or manifest_captured < opened_at:
                    raise IncidentTransitionError(
                        "resolution manifest must be recorded after the incident"
                    )
                policy_row = self._policy_row(connection, clean_policy_hash)
                if _completeness_issues(connection, {clean_policy_hash: policy_row}):
                    raise IncidentTransitionError(
                        "resolution manifest does not establish complete evidence"
                    )

            assert clean_policy_hash is not None
            sequence, previous_hash = _next_chain(connection, "incident_events")
            record_fields = {
                "event_id": clean_event_id,
                "event_time": canonical_event_time,
                "event_type": event_type.value,
                "incident_id": clean_incident_id,
                "kind": "incident_event",
                "payload_sha256": payload_hash,
                "policy_sha256": clean_policy_hash,
                "previous_record_sha256": previous_hash,
                "recorded_at": recorded_at,
                "sequence": sequence,
            }
            record_hash = evidence_sha256(record_fields)
            connection.execute(
                """
                INSERT INTO incident_events (
                    sequence, event_id, incident_id, policy_sha256, event_type,
                    event_time, payload_json, payload_sha256, recorded_at,
                    previous_record_sha256, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    clean_event_id,
                    clean_incident_id,
                    clean_policy_hash,
                    event_type.value,
                    canonical_event_time,
                    payload_json,
                    payload_hash,
                    recorded_at,
                    previous_hash,
                    record_hash,
                ),
            )
            connection.commit()
            return IncidentEvent(
                sequence=sequence,
                event_id=clean_event_id,
                incident_id=clean_incident_id,
                policy_sha256=clean_policy_hash,
                event_type=event_type,
                event_time=canonical_event_time,
                payload=normalized_payload,
                payload_sha256=payload_hash,
                recorded_at=recorded_at,
                previous_record_sha256=previous_hash,
                record_sha256=record_hash,
            ), True
        except ExecutionEvidenceLedgerError:
            _rollback(connection)
            raise
        except sqlite3.Error as exc:
            _rollback(connection)
            raise EvidenceLedgerStorageError("could not append incident evidence") from exc
        finally:
            connection.close()

    def _initialize(self) -> None:
        try:
            if self.path.exists() and self.path.is_symlink():
                raise EvidenceLedgerStorageError("ledger path cannot be a symbolic link")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            connection = self._connect(write=True)
            try:
                user_tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if not existed or not user_tables:
                    self._create_schema(connection)
                self._validate_schema(connection)
                issues = _collect_integrity_issues(connection)
                if issues:
                    raise EvidenceLedgerStorageError(
                        "ledger failed initialization integrity checks: " + "; ".join(issues[:3])
                    )
            finally:
                connection.close()
        except ExecutionEvidenceLedgerError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceLedgerStorageError("could not initialize execution evidence ledger") from exc

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        if write:
            connection = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1_000,
                isolation_level=None,
            )
        else:
            uri = f"file:{quote(str(self.path.resolve()), safe='/')}?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=self._busy_timeout_ms / 1_000,
                isolation_level=None,
            )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            if int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) != self._busy_timeout_ms:
                raise EvidenceLedgerStorageError("SQLite busy timeout was not applied")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise EvidenceLedgerStorageError("SQLite foreign-key enforcement is required")
            if write:
                mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
                if mode != "wal":
                    raise EvidenceLedgerStorageError("SQLite WAL mode is required")
                connection.execute("PRAGMA synchronous = FULL")
                if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
                    raise EvidenceLedgerStorageError("SQLite FULL synchronization is required")
            else:
                connection.execute("PRAGMA query_only = ON")
            return connection
        except Exception:
            connection.close()
            raise

    def _open_write(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect(write=True)
            self._validate_schema(connection)
            return connection
        except ExecutionEvidenceLedgerError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise EvidenceLedgerStorageError("could not open execution evidence ledger") from exc

    def _open_read_only(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect(write=False)
            self._validate_schema(connection)
            return connection
        except ExecutionEvidenceLedgerError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise EvidenceLedgerStorageError("could not open ledger read-only") from exc

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE ledger_metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            INSERT INTO ledger_metadata (key, value) VALUES ('schema_version', '1');

            CREATE TABLE policies (
                policy_sha256 TEXT PRIMARY KEY NOT NULL CHECK(length(policy_sha256) = 64),
                policy_id TEXT UNIQUE NOT NULL,
                policy_json TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_until TEXT NOT NULL,
                registered_at TEXT NOT NULL
            );

            CREATE TABLE fills (
                sequence INTEGER PRIMARY KEY NOT NULL CHECK(sequence > 0),
                activity_id TEXT UNIQUE NOT NULL,
                policy_sha256 TEXT NOT NULL REFERENCES policies(policy_sha256),
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                transaction_time TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                previous_record_sha256 TEXT NOT NULL CHECK(length(previous_record_sha256) = 64),
                record_sha256 TEXT UNIQUE NOT NULL CHECK(length(record_sha256) = 64)
            );

            CREATE TABLE manifests (
                sequence INTEGER PRIMARY KEY NOT NULL CHECK(sequence > 0),
                manifest_id TEXT UNIQUE NOT NULL,
                policy_sha256 TEXT NOT NULL REFERENCES policies(policy_sha256),
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                expected_activity_ids_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                recorded_at TEXT NOT NULL,
                previous_record_sha256 TEXT NOT NULL CHECK(length(previous_record_sha256) = 64),
                record_sha256 TEXT UNIQUE NOT NULL CHECK(length(record_sha256) = 64)
            );

            CREATE TABLE incident_events (
                sequence INTEGER PRIMARY KEY NOT NULL CHECK(sequence > 0),
                event_id TEXT UNIQUE NOT NULL,
                incident_id TEXT NOT NULL,
                policy_sha256 TEXT NOT NULL REFERENCES policies(policy_sha256),
                event_type TEXT NOT NULL CHECK(event_type IN ('OPEN', 'RESOLVED')),
                event_time TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                recorded_at TEXT NOT NULL,
                previous_record_sha256 TEXT NOT NULL CHECK(length(previous_record_sha256) = 64),
                record_sha256 TEXT UNIQUE NOT NULL CHECK(length(record_sha256) = 64)
            );
            CREATE INDEX incident_events_incident_idx
                ON incident_events (incident_id, sequence);

            CREATE TRIGGER ledger_metadata_no_update
            BEFORE UPDATE ON ledger_metadata BEGIN
                SELECT RAISE(ABORT, 'ledger_metadata_append_only');
            END;
            CREATE TRIGGER ledger_metadata_no_delete
            BEFORE DELETE ON ledger_metadata BEGIN
                SELECT RAISE(ABORT, 'ledger_metadata_append_only');
            END;
            CREATE TRIGGER policies_no_update
            BEFORE UPDATE ON policies BEGIN
                SELECT RAISE(ABORT, 'policies_append_only');
            END;
            CREATE TRIGGER policies_no_delete
            BEFORE DELETE ON policies BEGIN
                SELECT RAISE(ABORT, 'policies_append_only');
            END;
            CREATE TRIGGER fills_no_update
            BEFORE UPDATE ON fills BEGIN
                SELECT RAISE(ABORT, 'fills_append_only');
            END;
            CREATE TRIGGER fills_no_delete
            BEFORE DELETE ON fills BEGIN
                SELECT RAISE(ABORT, 'fills_append_only');
            END;
            CREATE TRIGGER manifests_no_update
            BEFORE UPDATE ON manifests BEGIN
                SELECT RAISE(ABORT, 'manifests_append_only');
            END;
            CREATE TRIGGER manifests_no_delete
            BEFORE DELETE ON manifests BEGIN
                SELECT RAISE(ABORT, 'manifests_append_only');
            END;
            CREATE TRIGGER incident_events_no_update
            BEFORE UPDATE ON incident_events BEGIN
                SELECT RAISE(ABORT, 'incident_events_append_only');
            END;
            CREATE TRIGGER incident_events_no_delete
            BEFORE DELETE ON incident_events BEGIN
                SELECT RAISE(ABORT, 'incident_events_append_only');
            END;
            COMMIT;
            """
        )

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        expected_tables = {
            "ledger_metadata": ("key", "value"),
            "policies": (
                "policy_sha256",
                "policy_id",
                "policy_json",
                "effective_from",
                "effective_until",
                "registered_at",
            ),
            "fills": (
                "sequence",
                "activity_id",
                "policy_sha256",
                "payload_json",
                "payload_sha256",
                "transaction_time",
                "recorded_at",
                "previous_record_sha256",
                "record_sha256",
            ),
            "manifests": (
                "sequence",
                "manifest_id",
                "policy_sha256",
                "window_start",
                "window_end",
                "captured_at",
                "expected_activity_ids_json",
                "metadata_json",
                "payload_sha256",
                "recorded_at",
                "previous_record_sha256",
                "record_sha256",
            ),
            "incident_events": (
                "sequence",
                "event_id",
                "incident_id",
                "policy_sha256",
                "event_type",
                "event_time",
                "payload_json",
                "payload_sha256",
                "recorded_at",
                "previous_record_sha256",
                "record_sha256",
            ),
        }
        for table, columns in expected_tables.items():
            actual = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
            if actual != columns:
                raise EvidenceLedgerStorageError(f"unexpected or missing schema for {table}")
        version = connection.execute(
            "SELECT value FROM ledger_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if version is None or str(version[0]) != str(SCHEMA_VERSION):
            raise EvidenceLedgerStorageError("unsupported execution evidence schema version")
        expected_triggers = {
            f"{table}_{operation}"
            for table in ("ledger_metadata", "policies", "fills", "manifests", "incident_events")
            for operation in ("no_update", "no_delete")
        }
        actual_triggers = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        if not expected_triggers.issubset(actual_triggers):
            raise EvidenceLedgerStorageError("append-only schema triggers are missing")

    def _assert_integrity(self, connection: sqlite3.Connection) -> None:
        issues = _collect_integrity_issues(connection)
        if issues:
            raise EvidenceLedgerStorageError(
                "ledger integrity check failed before append: " + "; ".join(issues[:3])
            )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise InvalidExecutionEvidenceError("ledger clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _policy_row(connection: sqlite3.Connection, policy_sha256: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM policies WHERE policy_sha256 = ?",
            (policy_sha256,),
        ).fetchone()
        if row is None:
            raise InvalidExecutionEvidenceError("policy_sha256 is not registered")
        return row

    @staticmethod
    def _policy_from_row(row: sqlite3.Row) -> PolicyRegistration:
        policy = json.loads(str(row["policy_json"]))
        if not isinstance(policy, dict):  # schema/integrity defense
            raise EvidenceLedgerStorageError("stored policy payload is not a mapping")
        return PolicyRegistration(
            policy_sha256=str(row["policy_sha256"]),
            policy_id=str(row["policy_id"]),
            policy=policy,
            effective_from=str(row["effective_from"]),
            effective_until=str(row["effective_until"]),
            registered_at=str(row["registered_at"]),
        )

    @staticmethod
    def _fill_from_row(row: sqlite3.Row) -> FillEvidence:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise EvidenceLedgerStorageError("stored fill payload is not a mapping")
        return FillEvidence(
            sequence=int(row["sequence"]),
            activity_id=str(row["activity_id"]),
            policy_sha256=str(row["policy_sha256"]),
            payload_sha256=str(row["payload_sha256"]),
            payload=payload,
            transaction_time=str(row["transaction_time"]),
            recorded_at=str(row["recorded_at"]),
            previous_record_sha256=str(row["previous_record_sha256"]),
            record_sha256=str(row["record_sha256"]),
        )

    @staticmethod
    def _manifest_from_row(row: sqlite3.Row) -> EvidenceManifest:
        ids = json.loads(str(row["expected_activity_ids_json"]))
        metadata = json.loads(str(row["metadata_json"]))
        if not isinstance(ids, list) or not isinstance(metadata, dict):
            raise EvidenceLedgerStorageError("stored manifest payload is malformed")
        return EvidenceManifest(
            sequence=int(row["sequence"]),
            manifest_id=str(row["manifest_id"]),
            policy_sha256=str(row["policy_sha256"]),
            window_start=str(row["window_start"]),
            window_end=str(row["window_end"]),
            captured_at=str(row["captured_at"]),
            expected_activity_ids=tuple(str(item) for item in ids),
            metadata=metadata,
            payload_sha256=str(row["payload_sha256"]),
            recorded_at=str(row["recorded_at"]),
            previous_record_sha256=str(row["previous_record_sha256"]),
            record_sha256=str(row["record_sha256"]),
        )

    @staticmethod
    def _incident_from_row(row: sqlite3.Row) -> IncidentEvent:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise EvidenceLedgerStorageError("stored incident payload is not a mapping")
        return IncidentEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            incident_id=str(row["incident_id"]),
            policy_sha256=str(row["policy_sha256"]),
            event_type=IncidentEventType(str(row["event_type"])),
            event_time=str(row["event_time"]),
            payload=payload,
            payload_sha256=str(row["payload_sha256"]),
            recorded_at=str(row["recorded_at"]),
            previous_record_sha256=str(row["previous_record_sha256"]),
            record_sha256=str(row["record_sha256"]),
        )


def _normalize_fill(activity_id: str, payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise InvalidExecutionEvidenceError("fill payload must be a mapping")
    normalized = _mapping_from_canonical(payload, field="fill payload")
    supplied_activity = normalized.get("activity_id")
    if supplied_activity is not None and _identifier(supplied_activity, field="payload.activity_id") != activity_id:
        raise InvalidExecutionEvidenceError("payload activity_id does not match the stable activity ID")
    normalized["activity_id"] = activity_id
    for field in ("order_id", "symbol"):
        normalized[field] = _identifier(normalized.get(field), field=field)
    if "client_order_id" in normalized and normalized["client_order_id"] is not None:
        normalized["client_order_id"] = _identifier(
            normalized["client_order_id"], field="client_order_id"
        )
    side = str(normalized.get("side", "")).strip().lower()
    if side not in {"buy", "sell"}:
        raise InvalidExecutionEvidenceError("side must be buy or sell")
    normalized["side"] = side
    normalized["quantity"] = _decimal_text(normalized.get("quantity"), field="quantity", positive=True)
    normalized["price"] = _decimal_text(normalized.get("price"), field="price", positive=True)
    for field in ("cumulative_quantity", "leaves_quantity"):
        if field in normalized and normalized[field] is not None:
            normalized[field] = _decimal_text(normalized[field], field=field, nonnegative=True)
    for field in ("fee_net_amount", "fee_cost"):
        if field in normalized and normalized[field] is not None:
            normalized[field] = _decimal_text(normalized[field], field=field)
    for field in ("decision_price", "benchmark_price", "arrival_price", "fill_mid"):
        if field in normalized and normalized[field] is not None:
            normalized[field] = _decimal_text(normalized[field], field=field, positive=True)
    normalized["transaction_time"] = _timestamp(
        normalized.get("transaction_time"), field="transaction_time"
    )
    if "submitted_at" in normalized and normalized["submitted_at"] is not None:
        submitted = _timestamp(normalized["submitted_at"], field="submitted_at")
        if _parse_timestamp(submitted, field="submitted_at") > _parse_timestamp(
            normalized["transaction_time"], field="transaction_time"
        ):
            raise InvalidExecutionEvidenceError("submitted_at follows transaction_time")
        normalized["submitted_at"] = submitted
    return _mapping_from_canonical(normalized, field="fill payload")


def _collect_integrity_issues(connection: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    try:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        if [str(row[0]).lower() for row in integrity_rows] != ["ok"]:
            issues.append("SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            issues.append("SQLite foreign-key integrity failed")
        issues.extend(_policy_integrity_issues(connection))
        issues.extend(_fill_integrity_issues(connection))
        issues.extend(_manifest_integrity_issues(connection))
        issues.extend(_incident_integrity_issues(connection))
    except (InvalidExecutionEvidenceError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        issues.append(f"stored evidence is not canonical or valid: {type(exc).__name__}")
    return issues


def _policy_integrity_issues(connection: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for row in connection.execute("SELECT * FROM policies ORDER BY policy_id"):
        policy_hash = str(row["policy_sha256"])
        policy_json = str(row["policy_json"])
        policy = json.loads(policy_json)
        if not isinstance(policy, dict) or canonical_evidence_json(policy) != policy_json:
            issues.append(f"policy {row['policy_id']} has non-canonical content")
            continue
        start = _timestamp(row["effective_from"], field="effective_from")
        end = _timestamp(row["effective_until"], field="effective_until")
        registered = _timestamp(row["registered_at"], field="registered_at")
        start_dt = _parse_timestamp(start, field="effective_from")
        end_dt = _parse_timestamp(end, field="effective_until")
        if end_dt <= start_dt or _parse_timestamp(registered, field="registered_at") >= start_dt:
            issues.append(f"policy {row['policy_id']} violates its pre-registration window")
        envelope = {
            "effective_from": start,
            "effective_until": end,
            "policy": policy,
            "policy_id": str(row["policy_id"]),
        }
        if evidence_sha256(envelope) != policy_hash:
            issues.append(f"policy {row['policy_id']} hash mismatch")
    return issues


def _fill_integrity_issues(connection: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    previous = _GENESIS_HASH
    expected_sequence = 1
    policies = {
        str(row["policy_sha256"]): row for row in connection.execute("SELECT * FROM policies")
    }
    for row in connection.execute("SELECT * FROM fills ORDER BY sequence"):
        activity_id = str(row["activity_id"])
        if int(row["sequence"]) != expected_sequence:
            issues.append("fill chain has a sequence gap")
            expected_sequence = int(row["sequence"])
        expected_sequence += 1
        payload_json = str(row["payload_json"])
        payload = json.loads(payload_json)
        normalized = _normalize_fill(activity_id, payload)
        payload_hash = _sha256_text(payload_json)
        if canonical_evidence_json(normalized) != payload_json:
            issues.append(f"fill {activity_id} payload is not canonical")
        if payload_hash != str(row["payload_sha256"]):
            issues.append(f"fill {activity_id} payload hash mismatch")
        if str(row["previous_record_sha256"]) != previous:
            issues.append(f"fill {activity_id} chain predecessor mismatch")
        transaction_time = _timestamp(row["transaction_time"], field="transaction_time")
        recorded_at = _timestamp(row["recorded_at"], field="recorded_at")
        if transaction_time != normalized["transaction_time"]:
            issues.append(f"fill {activity_id} transaction timestamp mismatch")
        if _parse_timestamp(transaction_time, field="transaction_time") > _parse_timestamp(
            recorded_at, field="recorded_at"
        ):
            issues.append(f"fill {activity_id} was recorded before its transaction")
        policy = policies.get(str(row["policy_sha256"]))
        if policy is not None:
            try:
                _require_within_policy(
                    _parse_timestamp(transaction_time, field="transaction_time"),
                    policy,
                    field="transaction_time",
                )
            except InvalidExecutionEvidenceError:
                issues.append(f"fill {activity_id} falls outside its policy window")
        fields = {
            "activity_id": activity_id,
            "kind": "fill",
            "payload_sha256": str(row["payload_sha256"]),
            "policy_sha256": str(row["policy_sha256"]),
            "previous_record_sha256": str(row["previous_record_sha256"]),
            "recorded_at": recorded_at,
            "sequence": int(row["sequence"]),
            "transaction_time": transaction_time,
        }
        expected_hash = evidence_sha256(fields)
        if expected_hash != str(row["record_sha256"]):
            issues.append(f"fill {activity_id} record hash mismatch")
        previous = str(row["record_sha256"])
    return issues


def _manifest_integrity_issues(connection: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    previous = _GENESIS_HASH
    expected_sequence = 1
    policies = {
        str(row["policy_sha256"]): row for row in connection.execute("SELECT * FROM policies")
    }
    for row in connection.execute("SELECT * FROM manifests ORDER BY sequence"):
        manifest_id = str(row["manifest_id"])
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            issues.append("manifest chain has a sequence gap")
            expected_sequence = sequence
        expected_sequence += 1
        ids_raw = json.loads(str(row["expected_activity_ids_json"]))
        metadata = json.loads(str(row["metadata_json"]))
        ids = _activity_id_set(ids_raw)
        normalized_metadata = _mapping_from_canonical(metadata, field="metadata")
        start = _timestamp(row["window_start"], field="window_start")
        end = _timestamp(row["window_end"], field="window_end")
        captured = _timestamp(row["captured_at"], field="captured_at")
        recorded = _timestamp(row["recorded_at"], field="recorded_at")
        start_dt = _parse_timestamp(start, field="window_start")
        end_dt = _parse_timestamp(end, field="window_end")
        if end_dt <= start_dt:
            issues.append(f"manifest {manifest_id} has an invalid window")
        if _parse_timestamp(captured, field="captured_at") < end_dt:
            issues.append(f"manifest {manifest_id} was captured before its window ended")
        if _parse_timestamp(captured, field="captured_at") > _parse_timestamp(
            recorded, field="recorded_at"
        ):
            issues.append(f"manifest {manifest_id} was recorded before capture")
        policy = policies.get(str(row["policy_sha256"]))
        if policy is not None:
            policy_start = _parse_timestamp(policy["effective_from"], field="effective_from")
            policy_end = _parse_timestamp(policy["effective_until"], field="effective_until")
            if start_dt < policy_start or end_dt > policy_end:
                issues.append(f"manifest {manifest_id} falls outside its policy window")
        payload = {
            "captured_at": captured,
            "expected_activity_ids": list(ids),
            "metadata": normalized_metadata,
            "window_end": end,
            "window_start": start,
        }
        payload_hash = evidence_sha256(payload)
        if payload_hash != str(row["payload_sha256"]):
            issues.append(f"manifest {manifest_id} payload hash mismatch")
        if str(row["previous_record_sha256"]) != previous:
            issues.append(f"manifest {manifest_id} chain predecessor mismatch")
        fields = {
            "kind": "manifest",
            "manifest_id": manifest_id,
            "payload_sha256": str(row["payload_sha256"]),
            "policy_sha256": str(row["policy_sha256"]),
            "previous_record_sha256": str(row["previous_record_sha256"]),
            "recorded_at": recorded,
            "sequence": sequence,
        }
        if evidence_sha256(fields) != str(row["record_sha256"]):
            issues.append(f"manifest {manifest_id} record hash mismatch")
        previous = str(row["record_sha256"])
    return issues


def _incident_integrity_issues(connection: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    previous = _GENESIS_HASH
    expected_sequence = 1
    state: dict[str, tuple[str, datetime, str, datetime]] = {}
    policies = {
        str(row["policy_sha256"]): row for row in connection.execute("SELECT * FROM policies")
    }
    for row in connection.execute("SELECT * FROM incident_events ORDER BY sequence"):
        event_id = str(row["event_id"])
        incident_id = str(row["incident_id"])
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            issues.append("incident chain has a sequence gap")
            expected_sequence = sequence
        expected_sequence += 1
        payload_json = str(row["payload_json"])
        payload = json.loads(payload_json)
        if not isinstance(payload, dict) or not payload:
            issues.append(f"incident event {event_id} payload is empty or malformed")
            payload = {}
        if canonical_evidence_json(payload) != payload_json:
            issues.append(f"incident event {event_id} payload is not canonical")
        if _sha256_text(payload_json) != str(row["payload_sha256"]):
            issues.append(f"incident event {event_id} payload hash mismatch")
        event_time = _timestamp(row["event_time"], field="event_time")
        recorded = _timestamp(row["recorded_at"], field="recorded_at")
        event_dt = _parse_timestamp(event_time, field="event_time")
        recorded_dt = _parse_timestamp(recorded, field="recorded_at")
        if event_dt > recorded_dt:
            issues.append(f"incident event {event_id} was recorded before occurrence")
        event_type = str(row["event_type"])
        policy_hash = str(row["policy_sha256"])
        prior = state.get(incident_id)
        if event_type == IncidentEventType.OPEN.value:
            if prior is not None:
                issues.append(f"incident {incident_id} was opened more than once")
            else:
                state[incident_id] = (event_type, event_dt, policy_hash, recorded_dt)
                policy = policies.get(policy_hash)
                if policy is not None:
                    try:
                        _require_within_policy(event_dt, policy, field="occurred_at")
                    except InvalidExecutionEvidenceError:
                        issues.append(f"incident {incident_id} opened outside its policy window")
        elif event_type == IncidentEventType.RESOLVED.value:
            if prior is None or prior[0] != IncidentEventType.OPEN.value:
                issues.append(f"incident {incident_id} resolved without one open event")
            elif prior[2] != policy_hash or event_dt < prior[1]:
                issues.append(f"incident {incident_id} has an invalid resolution event")
            else:
                manifest_id = payload.get("resolution_manifest_id")
                manifest = (
                    connection.execute(
                        "SELECT * FROM manifests WHERE manifest_id = ?",
                        (manifest_id.strip(),),
                    ).fetchone()
                    if isinstance(manifest_id, str) and manifest_id.strip()
                    else None
                )
                if (
                    manifest is None
                    or str(manifest["policy_sha256"]) != policy_hash
                    or _parse_timestamp(
                        manifest["recorded_at"],
                        field="manifest.recorded_at",
                    )
                    <= prior[3]
                    or _parse_timestamp(
                        manifest["captured_at"],
                        field="manifest.captured_at",
                    )
                    < prior[1]
                ):
                    issues.append(
                        f"incident {incident_id} resolution lacks posterior manifest evidence"
                    )
                state[incident_id] = (event_type, event_dt, policy_hash, recorded_dt)
        else:
            issues.append(f"incident event {event_id} has an unknown type")
        if str(row["previous_record_sha256"]) != previous:
            issues.append(f"incident event {event_id} chain predecessor mismatch")
        fields = {
            "event_id": event_id,
            "event_time": event_time,
            "event_type": event_type,
            "incident_id": incident_id,
            "kind": "incident_event",
            "payload_sha256": str(row["payload_sha256"]),
            "policy_sha256": policy_hash,
            "previous_record_sha256": str(row["previous_record_sha256"]),
            "recorded_at": recorded,
            "sequence": sequence,
        }
        if evidence_sha256(fields) != str(row["record_sha256"]):
            issues.append(f"incident event {event_id} record hash mismatch")
        previous = str(row["record_sha256"])
    return issues


def _completeness_issues(
    connection: sqlite3.Connection,
    policies: Mapping[str, sqlite3.Row],
) -> list[str]:
    issues: list[str] = []
    if not policies:
        return ["no registered policy selected for completeness verification"]
    for policy_hash, policy in policies.items():
        manifests = connection.execute(
            "SELECT * FROM manifests WHERE policy_sha256 = ? ORDER BY window_start, window_end",
            (policy_hash,),
        ).fetchall()
        if not manifests:
            issues.append(f"policy {policy['policy_id']} has no completeness manifest")
            continue
        policy_start = _parse_timestamp(policy["effective_from"], field="effective_from")
        policy_end = _parse_timestamp(policy["effective_until"], field="effective_until")
        cursor = policy_start
        for manifest in manifests:
            start = _parse_timestamp(manifest["window_start"], field="window_start")
            end = _parse_timestamp(manifest["window_end"], field="window_end")
            if start > cursor:
                issues.append(f"policy {policy['policy_id']} manifest coverage has a gap")
            if end > cursor:
                cursor = end
            expected_raw = json.loads(str(manifest["expected_activity_ids_json"]))
            expected = set(_activity_id_set(expected_raw))
            actual = {
                str(row["activity_id"])
                for row in connection.execute(
                    """
                    SELECT activity_id FROM fills
                    WHERE policy_sha256 = ? AND transaction_time >= ? AND transaction_time < ?
                    """,
                    (policy_hash, str(manifest["window_start"]), str(manifest["window_end"])),
                )
            }
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            if missing:
                issues.append(
                    f"manifest {manifest['manifest_id']} is missing declared fills: "
                    + ",".join(missing[:5])
                )
            if unexpected:
                issues.append(
                    f"manifest {manifest['manifest_id']} has undeclared fills: "
                    + ",".join(unexpected[:5])
                )
        if cursor < policy_end:
            issues.append(f"policy {policy['policy_id']} manifest coverage ends early")
    return issues


def _open_incident_ids(
    connection: sqlite3.Connection,
    policy_hashes: frozenset[str],
) -> list[str]:
    latest: dict[str, str] = {}
    for row in connection.execute("SELECT * FROM incident_events ORDER BY sequence"):
        if str(row["policy_sha256"]) in policy_hashes:
            latest[str(row["incident_id"])] = str(row["event_type"])
    return sorted(
        incident_id
        for incident_id, event_type in latest.items()
        if event_type == IncidentEventType.OPEN.value
    )


def _normalize_json(value: object, *, location: str, key: str | None = None) -> object:
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise InvalidExecutionEvidenceError(f"{location} keys must be non-empty strings")
            if raw_key in normalized:
                raise InvalidExecutionEvidenceError(f"duplicate key at {location}.{raw_key}")
            normalized[raw_key] = _normalize_json(
                item,
                location=f"{location}.{raw_key}",
                key=raw_key,
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, datetime):
        return _timestamp(value, field=location)
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and key is not None:
            if _timestamp_key(key):
                return _timestamp(value, field=location)
            if key.lower().endswith("_date"):
                try:
                    return date.fromisoformat(value).isoformat()
                except ValueError as exc:
                    raise InvalidExecutionEvidenceError(f"{location} must be an ISO date") from exc
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidExecutionEvidenceError(f"{location} must be finite")
        return {"$decimal": _canonical_decimal(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidExecutionEvidenceError(f"{location} must be finite")
        return 0.0 if value == 0 else value
    raise InvalidExecutionEvidenceError(f"unsupported evidence value at {location}")


def _mapping_from_canonical(value: object, *, field: str) -> dict[str, object]:
    normalized = _normalize_json(value, location=field)
    if not isinstance(normalized, dict):
        raise InvalidExecutionEvidenceError(f"{field} must be a mapping")
    return normalized


def _decimal_text(
    value: object,
    *,
    field: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> str:
    if isinstance(value, Mapping):
        if set(value) == {"$decimal"} and isinstance(value["$decimal"], str):
            value = value["$decimal"]
        else:
            raise InvalidExecutionEvidenceError(f"{field} must be a finite decimal")
    if isinstance(value, bool) or value is None:
        raise InvalidExecutionEvidenceError(f"{field} must be a finite decimal")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidExecutionEvidenceError(f"{field} must be a finite decimal") from exc
    if not decimal_value.is_finite():
        raise InvalidExecutionEvidenceError(f"{field} must be a finite decimal")
    if positive and decimal_value <= 0:
        raise InvalidExecutionEvidenceError(f"{field} must be positive")
    if nonnegative and decimal_value < 0:
        raise InvalidExecutionEvidenceError(f"{field} must be non-negative")
    return _canonical_decimal(decimal_value)


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _timestamp(value: object, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidExecutionEvidenceError(f"{field} must be an ISO timestamp") from exc
    else:
        raise InvalidExecutionEvidenceError(f"{field} must be an ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidExecutionEvidenceError(f"{field} must include a timezone")
    return _format_timestamp(parsed)


def _parse_timestamp(value: object, *, field: str) -> datetime:
    canonical = _timestamp(value, field=field)
    return datetime.fromisoformat(canonical.replace("Z", "+00:00"))


def _format_timestamp(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    rendered = utc_value.isoformat(timespec="microseconds")
    return rendered.replace("+00:00", "Z")


def _timestamp_key(key: str) -> bool:
    lowered = key.lower()
    return lowered.endswith(("_at", "_timestamp", "_from", "_until")) or lowered in {
        "timestamp",
        "transaction_time",
    }


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidExecutionEvidenceError(f"{field} must be a string")
    clean = value.strip()
    if not clean or len(clean) > 256 or any(ord(character) < 32 for character in clean):
        raise InvalidExecutionEvidenceError(f"{field} is invalid")
    return clean


def _hash(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidExecutionEvidenceError(f"{field} must be a SHA-256 hex digest")
    clean = value.strip().lower()
    if len(clean) != _HASH_LENGTH or any(character not in "0123456789abcdef" for character in clean):
        raise InvalidExecutionEvidenceError(f"{field} must be a SHA-256 hex digest")
    return clean


def _activity_id_set(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidExecutionEvidenceError("expected_activity_ids must be a sequence")
    normalized = tuple(_identifier(value, field="activity_id") for value in values)
    if len(set(normalized)) != len(normalized):
        raise InvalidExecutionEvidenceError("expected_activity_ids contains duplicates")
    return tuple(sorted(normalized))


def _require_within_policy(
    timestamp: datetime,
    policy: sqlite3.Row,
    *,
    field: str,
) -> None:
    start = _parse_timestamp(policy["effective_from"], field="effective_from")
    end = _parse_timestamp(policy["effective_until"], field="effective_until")
    if timestamp < start or timestamp >= end:
        raise InvalidExecutionEvidenceError(f"{field} falls outside the policy window")


def _next_chain(connection: sqlite3.Connection, table: str) -> tuple[int, str]:
    row = connection.execute(_CHAIN_LAST_SQL[table]).fetchone()
    if row is None:
        return 1, _GENESIS_HASH
    return int(row["sequence"]) + 1, str(row["record_sha256"])


def _chain_head(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(_CHAIN_HEAD_SQL[table]).fetchone()
    return _GENESIS_HASH if row is None else str(row["record_sha256"])


def _count_for_policies(
    connection: sqlite3.Connection,
    table: str,
    policy_hashes: tuple[str, ...],
) -> int:
    if not policy_hashes:
        return 0
    placeholders = ",".join("?" for _ in policy_hashes)
    row = connection.execute(_COUNT_SQL_PREFIX[table] + f"({placeholders})", policy_hashes).fetchone()
    return int(row[0])


def _manifest_payload(manifest: EvidenceManifest) -> dict[str, object]:
    return {
        "captured_at": manifest.captured_at,
        "expected_activity_ids": list(manifest.expected_activity_ids),
        "metadata": manifest.metadata,
        "window_end": manifest.window_end,
        "window_start": manifest.window_start,
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rollback(connection: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        connection.rollback()


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DurableExecutionEvidenceLedger",
    "EvidenceLedgerStorageError",
    "EvidenceManifest",
    "EvidenceVerification",
    "ExecutionEvidenceConflictError",
    "ExecutionEvidenceLedgerError",
    "FillEvidence",
    "IncidentEvent",
    "IncidentEventType",
    "IncidentTransitionError",
    "InvalidExecutionEvidenceError",
    "PolicyRegistration",
    "canonical_evidence_json",
    "evidence_sha256",
]
