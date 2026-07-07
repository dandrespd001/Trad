"""Swing-position declarations registry (governance metadata; never executes orders).

Fail-closed here means EOD flatten: an untrusted registry contributes no
exemptions. A position that survives to end-of-day and has no trustworthy
swing declaration on file is flattened -- the safe direction. This module
never builds a broker client, never reads credentials, and never submits an
order; it only records/loads the swing-declaration ledger consulted by
``paper_eod_position_plan``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    paper_exit_code,
    write_json_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_REGISTRY_DIR = "reports/tmp/swing_declarations"
MAX_OVERNIGHT_LOSS_PCT_LIMIT = 5.0
MIN_PLAN_HASH_LEN = 8
MIN_THESIS_LEN = 20

_INTEGRITY_FIELD = "integrity_sha256"
_REGISTRY_FILENAME = "registry.json"
_DECISION_FILENAME = "decision_latest.json"


@dataclass(frozen=True)
class SwingDeclarationDecision:
    """Outcome of recording (or rejecting) a swing-position declaration."""

    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def record_swing_declaration(
    *,
    as_of_date: str,
    symbol: str,
    plan_hash: str,
    thesis: str,
    max_overnight_loss_pct: object,
    expires_on: str,
    registry_dir: str | Path = DEFAULT_REGISTRY_DIR,
    generated_at: str | None = None,
) -> SwingDeclarationDecision:
    """Record a swing-position declaration, or report why it is blocked.

    Blockers are accumulated (not short-circuited) so a single call reports
    every gap at once. On success, the record is appended to the per-date
    registry with a fresh integrity checksum; on failure, the registry is
    left untouched (fail-closed: no partial/ambiguous state is ever
    persisted).
    """

    generated = generated_at or _utc_now()
    symbol_clean = str(symbol or "").strip().upper()
    plan_hash_clean = str(plan_hash or "").strip().lower()
    thesis_clean = str(thesis or "").strip()
    expires_on_clean = str(expires_on or "").strip()
    output_path = _decision_path(registry_dir, as_of_date)

    blockers: list[str] = []

    if not symbol_clean:
        blockers.append("symbol_required")

    if not plan_hash_clean or len(plan_hash_clean) < MIN_PLAN_HASH_LEN or not _is_hex(plan_hash_clean):
        blockers.append("plan_hash_invalid")

    if len(thesis_clean) < MIN_THESIS_LEN:
        blockers.append("thesis_too_short")

    pct_value = _float_or_none(max_overnight_loss_pct)
    if pct_value is None or pct_value <= 0 or pct_value > MAX_OVERNIGHT_LOSS_PCT_LIMIT:
        blockers.append("overnight_loss_pct_invalid")

    as_of_date_value = _parse_date(as_of_date)
    expires_on_value = _parse_date(expires_on_clean)
    if expires_on_value is None or as_of_date_value is None or expires_on_value <= as_of_date_value:
        blockers.append("expires_on_invalid")

    registry = load_swing_declarations(as_of_date, registry_dir=registry_dir)
    if registry.get("fail_closed"):
        blockers.append("registry_fail_closed")

    records = list(registry.get("records") or [])
    if symbol_clean and any(str(record.get("symbol")) == symbol_clean for record in records):
        blockers.append("duplicate_declaration")

    blockers = _dedupe(blockers)
    status = "BLOCKED" if blockers else "OK"
    payload_out: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "symbol": symbol_clean,
        "plan_hash": plan_hash_clean,
        "thesis": thesis_clean,
        "max_overnight_loss_pct": pct_value,
        "expires_on": expires_on_clean,
        "status": status,
        "blockers": blockers,
        "safety": _safety(),
    }

    if blockers:
        write_json_artifact(payload_out, output_path)
        return SwingDeclarationDecision(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status="BLOCKED",
            output_path=output_path,
            payload=payload_out,
        )

    records.append(
        {
            "symbol": symbol_clean,
            "declared_at": generated,
            "plan_hash": plan_hash_clean,
            "thesis": thesis_clean,
            "max_overnight_loss_pct": pct_value,
            "expires_on": expires_on_clean,
        }
    )
    _save_registry(records, as_of_date=as_of_date, registry_dir=registry_dir)
    write_json_artifact(payload_out, output_path)
    return SwingDeclarationDecision(
        exit_code=paper_exit_code(PAPER_OK), status="OK", output_path=output_path, payload=payload_out
    )


def load_swing_declarations(
    as_of_date: str, *, registry_dir: str | Path = DEFAULT_REGISTRY_DIR
) -> dict[str, object]:
    """Load the swing-declaration registry for ``as_of_date``, failing closed
    to an empty registry (with ``fail_closed=True``) on any sign the on-disk
    state cannot be trusted: corrupt JSON, a missing/tampered integrity
    checksum, or a malformed ``records`` field. A registry that has simply
    never been written yet (no declarations recorded for this date) is a
    normal empty registry with ``fail_closed=False``, not a failure.
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


def active_swing_symbols(registry_payload: Mapping[str, object], *, as_of_date: str) -> dict[str, dict[str, object]]:
    """Return the symbol -> record map of non-expired declarations.

    Fail-closed: if ``registry_payload`` is marked ``fail_closed``, this
    always returns an empty mapping, regardless of any records it may
    contain -- an untrusted registry contributes zero exemptions. Records
    whose ``expires_on`` is missing/unparseable are also excluded (no
    exemption rather than an exemption of unknown duration).
    """

    if bool(registry_payload.get("fail_closed")):
        return {}

    as_of_date_value = _parse_date(as_of_date)
    if as_of_date_value is None:
        return {}

    active: dict[str, dict[str, object]] = {}
    raw_records = registry_payload.get("records")
    if not isinstance(raw_records, list):
        return active
    for record in raw_records:
        if not isinstance(record, Mapping):
            continue
        symbol = str(record.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        expires_on_value = _parse_date(record.get("expires_on"))
        if expires_on_value is None or expires_on_value < as_of_date_value:
            continue
        active[symbol] = dict(record)
    return active


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


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


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
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
