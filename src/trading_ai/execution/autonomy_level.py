"""Autonomy ladder state machine (governance metadata; never executes orders).

This module tracks, per market, which rung of the N0-N3 autonomy ladder that
market is certified at (see ``docs/autonomy-ladder.md``). It is pure
governance bookkeeping: it persists state, appends an append-only ledger, and
answers pure "is this action allowed" queries for other gates to consult. It
never builds a broker client, never reads credentials, and never submits an
order.

State is stored per market at ``<state_dir>/<market>/state.json`` with an
``integrity_sha256`` checksum, in the same fail-closed idiom as
``paper_risk_state``: a missing file, corrupt JSON, tampered checksum, or
unknown level is always read back as ``N0_PAPER_AUTO`` with ``fail_closed``
set. Every promotion, demotion, and incident is appended to
``<state_dir>/<market>/ledger.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_CRITICAL,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    write_json_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_STATE_DIR = "reports/tmp/autonomy"

AUTONOMY_LEVELS: tuple[str, ...] = (
    "N0_PAPER_AUTO",
    "N1_REAL_CANARY",
    "N2_REAL_SEMI_AUTO",
    "N3_REAL_AUTO",
)
AUTONOMY_MARKETS: tuple[str, ...] = ("equities", "futures", "forex")

# market -> (required_market, minimum_level_of_required_market)
MARKET_PRECONDITIONS: dict[str, tuple[str, str]] = {
    "futures": ("equities", "N2_REAL_SEMI_AUTO"),
    "forex": ("futures", "N2_REAL_SEMI_AUTO"),
}

PROMOTION_EVIDENCE_REQUIREMENTS: dict[str, dict[str, object]] = {
    "N1_REAL_CANARY": {"min_clean_days": 20, "evidence_kind": "paper_certification"},
    "N2_REAL_SEMI_AUTO": {"min_clean_days": 10, "evidence_kind": "real_canary_certification"},
    "N3_REAL_AUTO": {"min_clean_days": 15, "evidence_kind": "real_semi_auto_certification"},
}

_AUTONOMY_INCIDENT_SEVERITIES = ("grave", "warning", "info")
_REQUIRED_LEVEL_BY_ACTION: dict[str, str] = {
    "paper_auto": "N0_PAPER_AUTO",
    "real_submit_approved": "N1_REAL_CANARY",
    "real_submit_veto_window": "N2_REAL_SEMI_AUTO",
    "real_submit_auto": "N3_REAL_AUTO",
}

_INTEGRITY_FIELD = "integrity_sha256"
_STATE_FILENAME = "state.json"
_LEDGER_FILENAME = "ledger.jsonl"
_DECISION_FILENAME = "decision_latest.json"


@dataclass(frozen=True)
class AutonomyState:
    """Persisted autonomy ladder state for a single market."""

    market: str
    level: str = AUTONOMY_LEVELS[0]
    certified_at: str | None = None
    certified_by: str | None = None
    evidence_hash: str | None = None
    open_incident: bool = False
    fail_closed: bool = False
    updated_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "market": self.market,
            "level": self.level,
            "certified_at": self.certified_at,
            "certified_by": self.certified_by,
            "evidence_hash": self.evidence_hash,
            "open_incident": self.open_incident,
            "fail_closed": self.fail_closed,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AutonomyState:
        return cls(
            market=_str_or_none(payload.get("market")) or "",
            level=_str_or_none(payload.get("level")) or AUTONOMY_LEVELS[0],
            certified_at=_str_or_none(payload.get("certified_at")),
            certified_by=_str_or_none(payload.get("certified_by")),
            evidence_hash=_str_or_none(payload.get("evidence_hash")),
            open_incident=bool(payload.get("open_incident", False)),
            fail_closed=bool(payload.get("fail_closed", False)),
            updated_at=_str_or_none(payload.get("updated_at")),
        )


@dataclass(frozen=True)
class AutonomyDecision:
    """Outcome of a governance operation (certify/incident/resolve)."""

    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def load_autonomy_state(market: str, *, state_dir: str | Path = DEFAULT_STATE_DIR) -> AutonomyState:
    """Load the persisted autonomy state for ``market``, failing closed to
    ``N0_PAPER_AUTO`` on any sign the on-disk state cannot be trusted: missing
    file, corrupt JSON, a tampered/missing integrity checksum, or an unknown
    level. Unlike ``paper_risk_state``, a checksum is mandatory here -- there
    is no "legacy file predates the checksum" allowance, since no autonomy
    state file predates this module.

    A "fail_closed_read" ledger event is appended only when a state file
    existed but could not be trusted (not for a market that has simply never
    been certified yet, which is expected to be absent).
    """

    _validate_market(market)
    state_path = _state_path(state_dir, market)
    if not state_path.exists():
        return AutonomyState(market=market, level=AUTONOMY_LEVELS[0], fail_closed=True)

    def _fail_closed(reason: str, *, stored_level: str | None) -> AutonomyState:
        _append_ledger_event(
            state_dir,
            market,
            event="fail_closed_read",
            from_level=stored_level,
            to_level=AUTONOMY_LEVELS[0],
            actor="system",
            reason=reason,
            evidence_hash=None,
            severity=None,
        )
        return AutonomyState(market=market, level=AUTONOMY_LEVELS[0], fail_closed=True)

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _fail_closed("state_corrupt_fail_closed", stored_level=None)
    if not isinstance(payload, Mapping):
        return _fail_closed("state_corrupt_fail_closed", stored_level=None)

    stored_level = payload.get("level") if isinstance(payload.get("level"), str) else None
    stored_checksum = payload.get(_INTEGRITY_FIELD)
    body = {key: value for key, value in payload.items() if key != _INTEGRITY_FIELD}
    if stored_checksum is None or stored_checksum != _checksum(body):
        return _fail_closed("state_integrity_mismatch_fail_closed", stored_level=stored_level)
    if payload.get("level") not in AUTONOMY_LEVELS:
        return _fail_closed("state_level_unknown_fail_closed", stored_level=stored_level)

    return AutonomyState.from_dict(payload)


def save_autonomy_state(state: AutonomyState, *, state_dir: str | Path = DEFAULT_STATE_DIR) -> None:
    """Atomically persist ``state`` with a fresh integrity checksum."""

    _validate_market(state.market)
    state_path = _state_path(state_dir, state.market)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    body = state.to_dict()
    payload = {**body, _INTEGRITY_FIELD: _checksum(body)}
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(state_path.parent), prefix=f".{state_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_name, state_path)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp_name)
        raise


def certify_autonomy_promotion(
    *,
    market: str,
    target_level: str,
    evidence: Mapping[str, object],
    reviewer: str,
    reason: str,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    output: str | Path | None = None,
    generated_at: str | None = None,
) -> AutonomyDecision:
    """Certify a single-rung promotion for ``market``, or report why it is
    blocked. Blockers are accumulated (not short-circuited) so a single call
    reports every gap at once. On success, the new state is persisted and a
    "promotion" event is appended to the ledger; on failure, state is left
    untouched.
    """

    _validate_market(market)
    generated = generated_at or _utc_now()
    state_path = _state_path(state_dir, market)
    file_existed_before = state_path.exists()
    current_state = load_autonomy_state(market, state_dir=state_dir)

    blockers: list[str] = []
    target_known = target_level in AUTONOMY_LEVELS
    if not target_known:
        blockers.append("unknown_target_level")

    current_index = (
        AUTONOMY_LEVELS.index(current_state.level) if current_state.level in AUTONOMY_LEVELS else 0
    )
    if target_known and AUTONOMY_LEVELS.index(target_level) != current_index + 1:
        blockers.append("invalid_level_jump")

    reviewer_clean = str(reviewer).strip()
    reason_clean = str(reason).strip()
    if not reviewer_clean:
        blockers.append("reviewer_required")
    if not reason_clean:
        blockers.append("reason_required")

    clean_days_raw = evidence.get("clean_days") if isinstance(evidence, Mapping) else None
    evidence_kind_raw = evidence.get("evidence_kind") if isinstance(evidence, Mapping) else None
    artifact_hash_raw = evidence.get("artifact_hash") if isinstance(evidence, Mapping) else None

    clean_days_valid = (
        isinstance(clean_days_raw, int) and not isinstance(clean_days_raw, bool) and clean_days_raw >= 0
    )
    if not clean_days_valid:
        blockers.append("evidence_clean_days_invalid")
    evidence_kind_valid = isinstance(evidence_kind_raw, str) and evidence_kind_raw.strip() != ""
    if not evidence_kind_valid:
        blockers.append("evidence_kind_invalid")
    artifact_hash_clean = artifact_hash_raw.strip() if isinstance(artifact_hash_raw, str) else ""
    if not artifact_hash_clean:
        blockers.append("evidence_artifact_hash_invalid")

    requirement = PROMOTION_EVIDENCE_REQUIREMENTS.get(target_level) if target_known else None
    if requirement is not None:
        min_clean_days = int(str(requirement["min_clean_days"]))
        required_kind = str(requirement["evidence_kind"])
        if clean_days_valid and isinstance(clean_days_raw, int) and clean_days_raw < min_clean_days:
            blockers.append("insufficient_clean_days")
        if evidence_kind_valid and evidence_kind_raw != required_kind:
            blockers.append("evidence_kind_mismatch")

    if current_state.open_incident:
        blockers.append("open_incident_blocks_promotion")

    # A fail-closed state that came from a genuinely corrupted/tampered file
    # (as opposed to a market that has simply never been certified, which is
    # legitimately absent) can never be silently recertified: an operator
    # must reset the on-disk state out of band first. This is deliberately
    # the most fail-closed reading of "exige recertificacion desde N0".
    if current_state.fail_closed and file_existed_before:
        blockers.append("fail_closed_state_requires_recertification_from_n0")

    if target_level == AUTONOMY_LEVELS[1] and market in MARKET_PRECONDITIONS:
        required_market, min_level = MARKET_PRECONDITIONS[market]
        required_state = load_autonomy_state(required_market, state_dir=state_dir)
        min_index = AUTONOMY_LEVELS.index(min_level)
        required_index = (
            AUTONOMY_LEVELS.index(required_state.level)
            if required_state.level in AUTONOMY_LEVELS
            else 0
        )
        if required_index < min_index or required_state.open_incident:
            blockers.append(f"market_precondition_not_met:{required_market}")

    output_path = Path(output) if output is not None else _decision_path(state_dir, market)

    if blockers:
        payload = _decision_payload(
            market=market,
            action="certify_promotion",
            generated_at=generated,
            status="BLOCKED",
            current_level=current_state.level,
            target_level=target_level,
            reviewer=reviewer_clean,
            reason=reason_clean,
            blockers=blockers,
            extra={"evidence": dict(evidence) if isinstance(evidence, Mapping) else {}},
        )
        write_json_artifact(payload, output_path)
        return AutonomyDecision(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status="BLOCKED",
            output_path=output_path,
            payload=payload,
        )

    new_state = AutonomyState(
        market=market,
        level=target_level,
        certified_at=generated,
        certified_by=reviewer_clean,
        evidence_hash=artifact_hash_clean,
        open_incident=False,
        fail_closed=False,
        updated_at=generated,
    )
    save_autonomy_state(new_state, state_dir=state_dir)
    _append_ledger_event(
        state_dir,
        market,
        event="promotion",
        from_level=current_state.level,
        to_level=target_level,
        actor=reviewer_clean,
        reason=reason_clean,
        evidence_hash=artifact_hash_clean,
        severity=None,
        timestamp=generated,
    )
    payload = _decision_payload(
        market=market,
        action="certify_promotion",
        generated_at=generated,
        status="OK",
        current_level=target_level,
        target_level=target_level,
        reviewer=reviewer_clean,
        reason=reason_clean,
        blockers=[],
        extra={"evidence": dict(evidence) if isinstance(evidence, Mapping) else {}},
    )
    write_json_artifact(payload, output_path)
    return AutonomyDecision(
        exit_code=paper_exit_code(PAPER_OK), status="OK", output_path=output_path, payload=payload
    )


def record_autonomy_incident(
    *,
    market: str,
    severity: str,
    source: str,
    reason: str,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    output: str | Path | None = None,
    generated_at: str | None = None,
) -> AutonomyDecision:
    """Record an incident for ``market``. ``severity="grave"`` degrades the
    market one rung (floor N0) and opens an incident flag that blocks further
    real-money gating until resolved; lesser severities only append to the
    ledger.
    """

    _validate_market(market)
    generated = generated_at or _utc_now()
    severity_clean = str(severity).strip().lower()
    source_clean = str(source).strip()
    reason_clean = str(reason).strip()
    output_path = Path(output) if output is not None else _decision_path(state_dir, market)

    blockers: list[str] = []
    if severity_clean not in _AUTONOMY_INCIDENT_SEVERITIES:
        blockers.append("invalid_severity")
    if not source_clean:
        blockers.append("source_required")
    if not reason_clean:
        blockers.append("reason_required")

    current_state = load_autonomy_state(market, state_dir=state_dir)

    if blockers:
        payload = _decision_payload(
            market=market,
            action="record_incident",
            generated_at=generated,
            status="BLOCKED",
            current_level=current_state.level,
            target_level=current_state.level,
            reviewer=source_clean,
            reason=reason_clean,
            blockers=blockers,
            extra={"severity": severity_clean},
        )
        write_json_artifact(payload, output_path)
        return AutonomyDecision(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status="BLOCKED",
            output_path=output_path,
            payload=payload,
        )

    current_index = (
        AUTONOMY_LEVELS.index(current_state.level) if current_state.level in AUTONOMY_LEVELS else 0
    )

    if severity_clean == "grave":
        new_level = AUTONOMY_LEVELS[max(0, current_index - 1)]
        new_state = AutonomyState(
            market=market,
            level=new_level,
            certified_at=current_state.certified_at,
            certified_by=current_state.certified_by,
            evidence_hash=current_state.evidence_hash,
            open_incident=True,
            fail_closed=current_state.fail_closed,
            updated_at=generated,
        )
        save_autonomy_state(new_state, state_dir=state_dir)
        _append_ledger_event(
            state_dir,
            market,
            event="demotion",
            from_level=current_state.level,
            to_level=new_level,
            actor=source_clean,
            reason=reason_clean,
            evidence_hash=None,
            severity=None,
            timestamp=generated,
        )
        _append_ledger_event(
            state_dir,
            market,
            event="incident",
            from_level=new_level,
            to_level=new_level,
            actor=source_clean,
            reason=reason_clean,
            evidence_hash=None,
            severity="grave",
            timestamp=generated,
        )
        status = "CRITICAL"
        result_level = new_level
    else:
        new_state = AutonomyState(
            market=market,
            level=current_state.level,
            certified_at=current_state.certified_at,
            certified_by=current_state.certified_by,
            evidence_hash=current_state.evidence_hash,
            open_incident=current_state.open_incident,
            fail_closed=current_state.fail_closed,
            updated_at=generated,
        )
        save_autonomy_state(new_state, state_dir=state_dir)
        _append_ledger_event(
            state_dir,
            market,
            event="incident",
            from_level=current_state.level,
            to_level=current_state.level,
            actor=source_clean,
            reason=reason_clean,
            evidence_hash=None,
            severity=severity_clean,
            timestamp=generated,
        )
        status = "WARN" if severity_clean == "warning" else "OK"
        result_level = current_state.level

    payload = _decision_payload(
        market=market,
        action="record_incident",
        generated_at=generated,
        status=status,
        current_level=result_level,
        target_level=result_level,
        reviewer=source_clean,
        reason=reason_clean,
        blockers=[],
        extra={"severity": severity_clean},
    )
    write_json_artifact(payload, output_path)
    exit_code = {
        "CRITICAL": paper_exit_code(PAPER_CRITICAL),
        "WARN": paper_exit_code(PAPER_WARN),
        "OK": paper_exit_code(PAPER_OK),
    }[status]
    return AutonomyDecision(exit_code=exit_code, status=status, output_path=output_path, payload=payload)


def resolve_autonomy_incident(
    *,
    market: str,
    reviewer: str,
    reason: str,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    generated_at: str | None = None,
) -> AutonomyDecision:
    """Clear the open-incident flag for ``market`` without changing its
    level. Required before the market can be recertified upward again after a
    grave incident degraded it.
    """

    _validate_market(market)
    generated = generated_at or _utc_now()
    reviewer_clean = str(reviewer).strip()
    reason_clean = str(reason).strip()
    output_path = _decision_path(state_dir, market)

    current_state = load_autonomy_state(market, state_dir=state_dir)

    blockers: list[str] = []
    if not reviewer_clean:
        blockers.append("reviewer_required")
    if not reason_clean:
        blockers.append("reason_required")

    if blockers:
        payload = _decision_payload(
            market=market,
            action="resolve_incident",
            generated_at=generated,
            status="BLOCKED",
            current_level=current_state.level,
            target_level=current_state.level,
            reviewer=reviewer_clean,
            reason=reason_clean,
            blockers=blockers,
            extra={},
        )
        write_json_artifact(payload, output_path)
        return AutonomyDecision(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status="BLOCKED",
            output_path=output_path,
            payload=payload,
        )

    new_state = AutonomyState(
        market=market,
        level=current_state.level,
        certified_at=current_state.certified_at,
        certified_by=current_state.certified_by,
        evidence_hash=current_state.evidence_hash,
        open_incident=False,
        fail_closed=current_state.fail_closed,
        updated_at=generated,
    )
    save_autonomy_state(new_state, state_dir=state_dir)
    _append_ledger_event(
        state_dir,
        market,
        event="incident",
        from_level=current_state.level,
        to_level=current_state.level,
        actor=reviewer_clean,
        reason=reason_clean,
        evidence_hash=None,
        severity="resolved",
        timestamp=generated,
    )
    payload = _decision_payload(
        market=market,
        action="resolve_incident",
        generated_at=generated,
        status="OK",
        current_level=current_state.level,
        target_level=current_state.level,
        reviewer=reviewer_clean,
        reason=reason_clean,
        blockers=[],
        extra={},
    )
    write_json_artifact(payload, output_path)
    return AutonomyDecision(
        exit_code=paper_exit_code(PAPER_OK), status="OK", output_path=output_path, payload=payload
    )


def evaluate_autonomy_gate(
    *, market: str, requested_action: str, state: AutonomyState
) -> list[str]:
    """Pure function: return the list of autonomy blockers for
    ``requested_action`` given an already-loaded ``state``. Performs no I/O
    and never executes anything; execution gates elsewhere consult this.
    """

    del market  # kept for interface parity / future market-specific rules
    required_level = _REQUIRED_LEVEL_BY_ACTION.get(requested_action)
    if required_level is None:
        return ["unknown_requested_action"]

    blockers: list[str] = []
    state_index = AUTONOMY_LEVELS.index(state.level) if state.level in AUTONOMY_LEVELS else 0
    required_index = AUTONOMY_LEVELS.index(required_level)
    if state_index < required_index:
        blockers.append("autonomy_level_insufficient")

    is_real_action = requested_action != "paper_auto"
    if is_real_action and state.open_incident:
        blockers.append("autonomy_open_incident")
    if is_real_action and state.fail_closed:
        blockers.append("autonomy_state_fail_closed")
    return blockers


def _decision_payload(
    *,
    market: str,
    action: str,
    generated_at: str,
    status: str,
    current_level: str,
    target_level: str,
    reviewer: str,
    reason: str,
    blockers: list[str],
    extra: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "market": market,
        "action": action,
        "status": status,
        "current_level": current_level,
        "target_level": target_level,
        "reviewer": reviewer,
        "reason": reason,
        "blockers": blockers,
        **extra,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_allowed": False,
            "live_trading_authorized": False,
        },
    }


def _validate_market(market: str) -> None:
    if market not in AUTONOMY_MARKETS:
        raise ValueError(f"unknown autonomy market: {market!r}")


def _state_path(state_dir: str | Path, market: str) -> Path:
    return Path(state_dir) / market / _STATE_FILENAME


def _ledger_path(state_dir: str | Path, market: str) -> Path:
    return Path(state_dir) / market / _LEDGER_FILENAME


def _decision_path(state_dir: str | Path, market: str) -> Path:
    return Path(state_dir) / market / _DECISION_FILENAME


def _append_ledger_event(
    state_dir: str | Path,
    market: str,
    *,
    event: str,
    from_level: str | None,
    to_level: str | None,
    actor: str,
    reason: str,
    evidence_hash: str | None,
    severity: str | None,
    timestamp: str | None = None,
) -> None:
    ledger_path = _ledger_path(state_dir, market)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp or _utc_now(),
        "market": market,
        "event": event,
        "from_level": from_level,
        "to_level": to_level,
        "actor": actor,
        "reason": reason,
        "evidence_hash": evidence_hash,
        "severity": severity,
    }
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")


def _checksum(body: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
