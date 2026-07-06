"""Signal-plan approval/veto registry (governance metadata; never executes orders).

This module records human approve/veto reviews of a specific ``signal_plan.json``
artifact produced by ``paper_signal_arbitration`` (see docs/autonomy-ladder.md,
Sprint A2). It is pure governance bookkeeping in the same idiom as
``autonomy_level``: state is persisted per ``as_of_date`` with an
``integrity_sha256`` checksum, reads fail closed to an empty registry on any
corruption/tamper, and a pure gate function answers "is this action allowed"
for other gates to consult. It never builds a broker client, never reads
credentials, and never submits an order.

A veto is a one-way door: once a plan is vetoed, no later approval can undo
it (fail-closed -- the veto always wins in ``evaluate_signal_approval_gate``).
A veto recorded after an approval is still appended, since the veto must be
able to override an earlier approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    paper_exit_code,
    read_json_artifact,
    write_json_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_REGISTRY_DIR = "reports/tmp/signal_approval"
MIN_HASH_PREFIX = 8
DEFAULT_VETO_WINDOW_MINUTES = 15
VERDICTS: tuple[str, ...] = ("approved", "vetoed")

_REAL_REQUESTED_ACTIONS: tuple[str, ...] = (
    "real_submit_approved",
    "real_submit_veto_window",
    "real_submit_auto",
)

_INTEGRITY_FIELD = "integrity_sha256"
_REGISTRY_FILENAME = "registry.json"
_DECISION_FILENAME = "decision_latest.json"


@dataclass(frozen=True)
class ApprovalDecision:
    """Outcome of recording (or rejecting) a signal-plan approve/veto review."""

    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def compute_plan_hash(plan_payload: Mapping[str, object]) -> str:
    """Return a stable sha256 hex digest of ``plan_payload``.

    The ``generated_at`` field is excluded so that two regenerations of the
    same logical plan (same inputs, same decision) hash identically; any
    other field change (decision, selected signal, reasons, ...) changes the
    hash.
    """

    body = {key: value for key, value in plan_payload.items() if key != "generated_at"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_signal_approval_registry(
    as_of_date: str, *, registry_dir: str | Path = DEFAULT_REGISTRY_DIR
) -> dict[str, object]:
    """Load the approval registry for ``as_of_date``, failing closed to an
    empty registry (with ``fail_closed=True``) on any sign the on-disk state
    cannot be trusted: corrupt JSON, a missing/tampered integrity checksum,
    or a malformed ``records`` field. A registry that has simply never been
    written yet (no reviews recorded for this date) is a normal empty
    registry with ``fail_closed=False``, not a failure.
    """

    path = _registry_path(registry_dir, as_of_date)
    if not path.exists():
        return _empty_registry(as_of_date, fail_closed=False)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_registry(as_of_date, fail_closed=True)
    if not isinstance(payload, Mapping):
        return _empty_registry(as_of_date, fail_closed=True)

    stored_checksum = payload.get(_INTEGRITY_FIELD)
    body = {key: value for key, value in payload.items() if key != _INTEGRITY_FIELD}
    if stored_checksum is None or stored_checksum != _checksum(body):
        return _empty_registry(as_of_date, fail_closed=True)

    records = payload.get("records")
    if not isinstance(records, list):
        return _empty_registry(as_of_date, fail_closed=True)

    return {
        "schema_version": str(payload.get("schema_version") or SCHEMA_VERSION),
        "as_of_date": str(payload.get("as_of_date") or as_of_date),
        "records": [dict(record) for record in records if isinstance(record, Mapping)],
        "fail_closed": False,
    }


def record_signal_plan_review(
    *,
    as_of_date: str,
    plan_hash_prefix: str,
    verdict: str,
    actor_user_id: str,
    actor_chat_id: str,
    source_update_id: object,
    reason: str,
    plan: Mapping[str, object] | str | Path,
    registry_dir: str | Path = DEFAULT_REGISTRY_DIR,
    generated_at: str | None = None,
) -> ApprovalDecision:
    """Record an approve/veto review of a signal plan, or report why it is
    blocked. Blockers are accumulated (not short-circuited) so a single call
    reports every gap at once. On success, the record is appended to the
    per-date registry with a fresh integrity checksum; on failure, the
    registry is left untouched.
    """

    generated = generated_at or _utc_now()
    output_path = _decision_path(registry_dir, as_of_date)

    verdict_clean = str(verdict).strip().lower()
    prefix_clean = str(plan_hash_prefix).strip().lower()
    reason_clean = str(reason or "").strip()
    actor_user_clean = str(actor_user_id or "").strip()
    actor_chat_clean = str(actor_chat_id or "").strip()

    blockers: list[str] = []
    if verdict_clean not in VERDICTS:
        blockers.append("invalid_verdict")

    prefix_valid = len(prefix_clean) >= MIN_HASH_PREFIX and _is_hex(prefix_clean)
    if not prefix_valid:
        blockers.append("invalid_plan_hash_prefix")

    plan_payload, plan_unreadable = _load_plan(plan)
    if plan_unreadable:
        blockers.append("plan_artifact_invalid")

    full_hash: str | None = None
    if plan_payload is not None:
        full_hash = compute_plan_hash(plan_payload)
        if prefix_valid and not full_hash.startswith(prefix_clean):
            blockers.append("plan_hash_mismatch")
        plan_as_of_date = str(plan_payload.get("as_of_date") or "")
        if plan_as_of_date != as_of_date:
            blockers.append("plan_as_of_date_mismatch")

    if verdict_clean == "vetoed" and not reason_clean:
        blockers.append("veto_reason_required")

    registry = load_signal_approval_registry(as_of_date, registry_dir=registry_dir)
    if registry.get("fail_closed"):
        blockers.append("approval_registry_fail_closed")

    records = list(registry.get("records") or [])
    if full_hash is not None:
        related = [record for record in records if str(record.get("plan_hash")) == full_hash]
        already_vetoed = any(str(record.get("verdict")) == "vetoed" for record in related)
        duplicate = any(
            str(record.get("verdict")) == verdict_clean and str(record.get("actor_user_id")) == actor_user_clean
            for record in related
        )
        if duplicate:
            blockers.append("duplicate_review")
        if verdict_clean == "approved" and already_vetoed:
            blockers.append("plan_already_vetoed")

    blockers = _dedupe(blockers)
    status = "BLOCKED" if blockers else "OK"
    payload_out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "plan_hash": full_hash,
        "plan_hash_prefix": prefix_clean,
        "verdict": verdict_clean,
        "actor_user_id": actor_user_clean,
        "actor_chat_id": actor_chat_clean,
        "source_update_id": source_update_id,
        "reason": reason_clean,
        "status": status,
        "blockers": blockers,
        "safety": _safety(),
    }

    if blockers:
        write_json_artifact(payload_out, output_path)
        return ApprovalDecision(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status="BLOCKED",
            output_path=output_path,
            payload=payload_out,
        )

    records.append(
        {
            "plan_hash": full_hash,
            "verdict": verdict_clean,
            "actor_user_id": actor_user_clean,
            "actor_chat_id": actor_chat_clean,
            "source_update_id": source_update_id,
            "reason": reason_clean,
            "recorded_at": generated,
        }
    )
    _save_registry(records, as_of_date=as_of_date, registry_dir=registry_dir)
    write_json_artifact(payload_out, output_path)
    return ApprovalDecision(
        exit_code=paper_exit_code(PAPER_OK), status="OK", output_path=output_path, payload=payload_out
    )


def evaluate_signal_approval_gate(
    *,
    plan_hash: str,
    registry_payload: Mapping[str, object],
    requested_action: str,
    plan_generated_at: str | datetime,
    now: str | datetime,
    veto_window_minutes: int = DEFAULT_VETO_WINDOW_MINUTES,
) -> list[str]:
    """Pure function: return the list of approval/veto blockers for
    ``requested_action`` given an already-loaded ``registry_payload``.
    Performs no I/O and never executes anything; execution gates elsewhere
    consult this. ``requested_action`` uses the ``autonomy_level`` vocabulary
    (``paper_auto``, ``real_submit_approved``, ``real_submit_veto_window``,
    ``real_submit_auto``).
    """

    if requested_action == "paper_auto":
        return []
    if requested_action not in _REAL_REQUESTED_ACTIONS:
        return ["unknown_requested_action"]

    if bool(registry_payload.get("fail_closed")):
        return ["approval_registry_fail_closed"]

    records = [
        record for record in _records(registry_payload) if str(record.get("plan_hash")) == plan_hash
    ]
    if any(str(record.get("verdict")) == "vetoed" for record in records):
        return ["signal_plan_vetoed"]

    approved = any(str(record.get("verdict")) == "approved" for record in records)

    if requested_action == "real_submit_approved":
        return [] if approved else ["signal_approval_missing"]

    if requested_action == "real_submit_veto_window":
        if approved:
            return []
        now_dt = _parse_dt(now)
        plan_dt = _parse_dt(plan_generated_at)
        window_end = plan_dt + timedelta(minutes=veto_window_minutes)
        return ["veto_window_open"] if now_dt < window_end else []

    # requested_action == "real_submit_auto": only the veto blocker (handled
    # above) applies.
    return []


def _load_plan(plan: Mapping[str, object] | str | Path) -> tuple[dict[str, object] | None, bool]:
    if isinstance(plan, Mapping):
        return dict(plan), False
    try:
        return read_json_artifact(plan), False
    except (OSError, json.JSONDecodeError, ValueError):
        return None, True


def _save_registry(records: list[dict[str, object]], *, as_of_date: str, registry_dir: str | Path) -> None:
    path = _registry_path(registry_dir, as_of_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": SCHEMA_VERSION, "as_of_date": as_of_date, "records": records}
    payload = {**body, _INTEGRITY_FIELD: _checksum(body)}
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp_name)
        raise


def _empty_registry(as_of_date: str, *, fail_closed: bool) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of_date": as_of_date,
        "records": [],
        "fail_closed": fail_closed,
    }


def _records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = payload.get("records")
    if not isinstance(raw, list):
        return []
    return [record for record in raw if isinstance(record, Mapping)]


def _registry_path(registry_dir: str | Path, as_of_date: str) -> Path:
    return Path(registry_dir) / as_of_date / _REGISTRY_FILENAME


def _decision_path(registry_dir: str | Path, as_of_date: str) -> Path:
    return Path(registry_dir) / as_of_date / _DECISION_FILENAME


def _checksum(body: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _is_hex(value: str) -> bool:
    if not value:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
        "live_execution_enabled": False,
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
