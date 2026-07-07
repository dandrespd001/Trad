"""Sync operational failure events into autonomy-ladder incidents (governance
metadata; never executes orders).

This module closes the loop left open by ``autonomy_level.record_autonomy_incident``:
that function exists but nothing calls it automatically, so a tripped live
circuit breaker, a blocked reconciliation, or an active paper kill-switch
never actually degrades the autonomy ladder unless an operator remembers to
run ``autonomy-incident`` by hand. This module reads the (already-persisted,
pure-state) artifacts those three subsystems maintain, turns each *new*
failure signal into a "grave" autonomy incident, and skips signals it has
already synced so the same event never degrades the ladder twice.

Design decisions worth calling out:

- **Sources are opt-in by path.** Each of ``breaker_state``,
  ``reconciliation_report``, and ``risk_state`` is only inspected if its path
  is provided. A market that has never had a live circuit breaker (e.g. it is
  still N0) must not spontaneously generate a breaker incident just because
  this sync ran; the caller decides which sources are relevant to the market
  being synced.
- **A provided-but-missing/corrupt source is fail-closed, not silent.** Once a
  path *is* provided, ``load_live_circuit_breaker`` and ``load_risk_state``
  already fail closed to a tripped/kill-switch-active state on a missing or
  corrupt file, so that case is handled for free -- it flows through the same
  "tripped"/"kill_switch_active" branch as a real trip. The reconciliation
  report has no such built-in fail-closed loader (``read_json_artifact`` just
  raises), so this module explicitly treats an unreadable reconciliation
  artifact as its own grave candidate (``reconciliation_artifact_invalid``).
- **Breaker incident identity.** ``LiveCircuitBreakerState`` has no
  ``tripped_at`` field -- the only timestamp it carries is ``updated_at``,
  which ``save_live_circuit_breaker`` stamps on every save (including a real
  trip) but which fail-closed states constructed directly by the loader
  (missing/corrupt/checksum-mismatch) leave as ``None``. So the identity uses
  ``updated_at`` when present and falls back to the reason string itself when
  it is not, per spec.
- **Idempotency, not correctness-at-all-costs.** The sync state
  (``processed_identities`` in ``<output_dir>/<market>/sync_state.json``) is
  itself checksummed in the same idiom as ``autonomy_level``. If that file is
  corrupt or its checksum is tampered, it is treated as *empty* rather than
  raising -- an event that has already been synced would be re-processed
  (degrading the ladder again), which is the fail-closed direction to err in:
  losing a genuine incident silently is worse than an extra, spurious
  demotion an operator can resolve. The payload surfaces this via
  ``sync_state_fail_closed: true`` so it is never a silent failure mode.
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

from trading_ai.execution.autonomy_level import (
    AUTONOMY_MARKETS,
    DEFAULT_STATE_DIR as AUTONOMY_DEFAULT_STATE_DIR,
    load_autonomy_state,
    record_autonomy_incident,
)
from trading_ai.execution.live_circuit_breaker import load_live_circuit_breaker
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_CRITICAL,
    PAPER_OK,
    paper_exit_code,
    read_json_artifact,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_risk_state import load_risk_state

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/autonomy_incident_sync"
DEFAULT_SYNC_STATE_FILENAME = "sync_state.json"

_SYNC_INTEGRITY_FIELD = "integrity_sha256"
_MAX_PROCESSED_IDENTITIES = 200


@dataclass(frozen=True)
class IncidentSyncResult:
    """Outcome of a run of the autonomy incident sync."""

    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


@dataclass(frozen=True)
class _Candidate:
    source: str
    reason: str
    identity: str


def run_autonomy_incident_sync(
    *,
    as_of_date: str,
    market: str,
    breaker_state: str | Path | None = None,
    reconciliation_report: str | Path | None = None,
    risk_state: str | Path | None = None,
    autonomy_state_dir: str | Path = AUTONOMY_DEFAULT_STATE_DIR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> IncidentSyncResult:
    if market not in AUTONOMY_MARKETS:
        raise ValueError(f"unknown autonomy market: {market!r}")

    generated = generated_at or _utc_now()
    as_of_clean = str(as_of_date).strip()
    output_root = Path(output_dir) / market / str(as_of_date)
    output_path = output_root / "incident_sync.json"
    markdown_path = output_root / "incident_sync.md"

    current_state = load_autonomy_state(market, state_dir=autonomy_state_dir)

    if not as_of_clean:
        payload = _build_payload(
            generated=generated,
            as_of_date=as_of_date,
            market=market,
            status="BLOCKED",
            sources_evaluated=[],
            incidents_filed=[],
            skipped_already_synced=[],
            sync_state_fail_closed=False,
            autonomy_level_after=current_state.level,
            open_incident_after=current_state.open_incident,
            blockers=["as_of_date_required"],
        )
        write_json_artifact(payload, output_path)
        write_text_artifact(render_incident_sync_markdown(payload), markdown_path)
        return IncidentSyncResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status="BLOCKED",
            output_path=output_path,
            payload=payload,
        )

    sources_evaluated: list[str] = []
    candidates: list[_Candidate] = []

    if breaker_state is not None:
        sources_evaluated.append("live_circuit_breaker")
        candidate = _detect_breaker_candidate(breaker_state)
        if candidate is not None:
            candidates.append(candidate)

    if reconciliation_report is not None:
        sources_evaluated.append("live_reconciliation")
        candidate = _detect_reconciliation_candidate(reconciliation_report)
        if candidate is not None:
            candidates.append(candidate)

    if risk_state is not None:
        sources_evaluated.append("paper_kill_switch")
        candidate = _detect_kill_switch_candidate(risk_state)
        if candidate is not None:
            candidates.append(candidate)

    sync_state_path = _sync_state_path(output_dir, market)
    processed_identities, sync_state_fail_closed = _load_sync_state(sync_state_path)
    processed_set = set(processed_identities)

    incidents_filed: list[dict[str, object]] = []
    skipped_already_synced: list[dict[str, object]] = []
    updated_identities: list[str] = list(processed_identities)

    for candidate in candidates:
        if candidate.identity in processed_set:
            skipped_already_synced.append(
                {
                    "source": candidate.source,
                    "reason": candidate.reason,
                    "identity": candidate.identity,
                }
            )
            continue

        decision = record_autonomy_incident(
            market=market,
            severity="grave",
            source=candidate.source,
            reason=candidate.reason,
            state_dir=autonomy_state_dir,
            generated_at=generated,
        )
        incidents_filed.append(
            {
                "source": candidate.source,
                "reason": candidate.reason,
                "identity": candidate.identity,
                "resulting_level": decision.payload.get("current_level"),
            }
        )
        processed_set.add(candidate.identity)
        updated_identities.append(candidate.identity)

    _save_sync_state(sync_state_path, market=market, processed_identities=updated_identities)

    final_state = load_autonomy_state(market, state_dir=autonomy_state_dir)
    status = "CRITICAL" if incidents_filed else "OK"

    payload = _build_payload(
        generated=generated,
        as_of_date=as_of_date,
        market=market,
        status=status,
        sources_evaluated=sources_evaluated,
        incidents_filed=incidents_filed,
        skipped_already_synced=skipped_already_synced,
        sync_state_fail_closed=sync_state_fail_closed,
        autonomy_level_after=final_state.level,
        open_incident_after=final_state.open_incident,
        blockers=[],
    )
    write_json_artifact(payload, output_path)
    write_text_artifact(render_incident_sync_markdown(payload), markdown_path)

    exit_code = paper_exit_code(PAPER_CRITICAL) if status == "CRITICAL" else paper_exit_code(PAPER_OK)
    return IncidentSyncResult(exit_code=exit_code, status=status, output_path=output_path, payload=payload)


def render_incident_sync_markdown(payload: Mapping[str, object]) -> str:
    sources = payload.get("sources_evaluated")
    sources_list = sources if isinstance(sources, list) else []
    incidents = payload.get("incidents_filed")
    incidents_list = incidents if isinstance(incidents, list) else []
    skipped = payload.get("skipped_already_synced")
    skipped_list = skipped if isinstance(skipped, list) else []
    blockers = payload.get("blockers")
    blockers_list = blockers if isinstance(blockers, list) else []

    lines = [
        "# Autonomy Incident Sync",
        "",
        f"Status: **{payload.get('status') or 'UNKNOWN'}**",
        f"Generated at: `{payload.get('generated_at') or ''}`",
        f"Market: `{payload.get('market') or ''}`",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Autonomy level after: `{payload.get('autonomy_level_after') or ''}`",
        f"Open incident after: `{payload.get('open_incident_after')}`",
        f"Sync state fail-closed: `{payload.get('sync_state_fail_closed')}`",
        "",
        "## Sources evaluated",
        "",
    ]
    lines.extend([f"- `{source}`" for source in sources_list] or ["- none"])
    lines.extend(["", "## Incidents filed", ""])
    if incidents_list:
        for item in incidents_list:
            lines.append(
                f"- `{item.get('source')}` reason=`{item.get('reason')}` "
                f"resulting_level=`{item.get('resulting_level')}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Skipped (already synced)", ""])
    if skipped_list:
        for item in skipped_list:
            lines.append(f"- `{item.get('source')}` reason=`{item.get('reason')}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Blockers", ""])
    if blockers_list:
        lines.extend([f"- `{blocker}`" for blocker in blockers_list])
    else:
        lines.append("- none")
    lines.extend(["", "Live trading allowed: `False`", ""])
    return "\n".join(lines)


def _detect_breaker_candidate(path: str | Path) -> _Candidate | None:
    state = load_live_circuit_breaker(path)
    if not state.tripped:
        return None
    reason = state.reason or "breaker_tripped"
    temporal = state.updated_at if state.updated_at else reason
    identity = _identity("live_circuit_breaker", reason, temporal)
    return _Candidate(source="live_circuit_breaker", reason=reason, identity=identity)


def _detect_reconciliation_candidate(path: str | Path) -> _Candidate | None:
    try:
        payload = read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError):
        reason = "reconciliation_artifact_invalid"
        identity = _identity("live_reconciliation", reason, str(path))
        return _Candidate(source="live_reconciliation", reason=reason, identity=identity)

    if payload.get("status") != "BLOCKED":
        return None

    divergences = payload.get("divergences")
    divergences_list = divergences if isinstance(divergences, list) else []
    canonical = json.dumps(divergences_list, sort_keys=True, separators=(",", ":"), default=str)
    report_generated_at = payload.get("generated_at")
    extra_suffix = str(report_generated_at) if report_generated_at not in (None, "") else ""
    extra = f"{canonical}|{extra_suffix}"
    reason = "reconciliation_divergence"
    identity = _identity("live_reconciliation", reason, extra)
    return _Candidate(source="live_reconciliation", reason=reason, identity=identity)


def _detect_kill_switch_candidate(path: str | Path) -> _Candidate | None:
    state = load_risk_state(path)
    if not state.kill_switch_active:
        return None
    reason = state.kill_switch_reason or "kill_switch_active"
    extra = state.kill_switch_tripped_at or ""
    identity = _identity("paper_kill_switch", reason, extra)
    return _Candidate(source="paper_kill_switch", reason=reason, identity=identity)


def _identity(source: str, reason: str, extra: str) -> str:
    return hashlib.sha256(f"{source}|{reason}|{extra}".encode("utf-8")).hexdigest()


def _sync_state_path(output_dir: str | Path, market: str) -> Path:
    return Path(output_dir) / market / DEFAULT_SYNC_STATE_FILENAME


def _load_sync_state(path: Path) -> tuple[list[str], bool]:
    if not path.exists():
        return [], False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], True
    if not isinstance(payload, Mapping):
        return [], True
    stored_checksum = payload.get(_SYNC_INTEGRITY_FIELD)
    body = {key: value for key, value in payload.items() if key != _SYNC_INTEGRITY_FIELD}
    if not isinstance(stored_checksum, str) or stored_checksum != _checksum(body):
        return [], True
    processed = body.get("processed_identities")
    if not isinstance(processed, list):
        return [], True
    cleaned = [str(item) for item in processed if isinstance(item, str)]
    return cleaned, False


def _save_sync_state(path: Path, *, market: str, processed_identities: list[str]) -> None:
    trimmed = processed_identities[-_MAX_PROCESSED_IDENTITIES:]
    body = {
        "schema_version": SCHEMA_VERSION,
        "market": market,
        "processed_identities": trimmed,
    }
    payload = {**body, _SYNC_INTEGRITY_FIELD: _checksum(body)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp_name)
        raise


def _build_payload(
    *,
    generated: str,
    as_of_date: str,
    market: str,
    status: str,
    sources_evaluated: list[str],
    incidents_filed: list[dict[str, object]],
    skipped_already_synced: list[dict[str, object]],
    sync_state_fail_closed: bool,
    autonomy_level_after: str,
    open_incident_after: bool,
    blockers: list[str],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "market": market,
        "status": status,
        "sources_evaluated": sources_evaluated,
        "incidents_filed": incidents_filed,
        "skipped_already_synced": skipped_already_synced,
        "sync_state_fail_closed": sync_state_fail_closed,
        "autonomy_level_after": autonomy_level_after,
        "open_incident_after": open_incident_after,
        "blockers": blockers,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_allowed": False,
            "live_trading_authorized": False,
        },
    }


def _checksum(body: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
