"""Persistent live circuit breaker with fail-closed checksum validation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = "1.0"
DEFAULT_LIVE_BREAKER_PATH = "reports/tmp/live_circuit_breaker/state.json"
_CHECKSUM_FIELD = "checksum_sha256"


class LiveCircuitBreakerError(RuntimeError):
    """Raised when breaker state cannot be safely changed."""


@dataclass(frozen=True)
class LiveCircuitBreakerState:
    tripped: bool
    reason: str | None
    reviewer: str | None = None
    reset_reason: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "tripped": self.tripped,
            "reason": self.reason,
            "reviewer": self.reviewer,
            "reset_reason": self.reset_reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LiveCircuitBreakerState:
        return cls(
            tripped=bool(payload.get("tripped")),
            reason=_str_or_none(payload.get("reason")),
            reviewer=_str_or_none(payload.get("reviewer")),
            reset_reason=_str_or_none(payload.get("reset_reason")),
            updated_at=_str_or_none(payload.get("updated_at")),
        )


def load_live_circuit_breaker(path: str | Path = DEFAULT_LIVE_BREAKER_PATH) -> LiveCircuitBreakerState:
    state_path = Path(path)
    if not state_path.exists():
        return LiveCircuitBreakerState(tripped=True, reason="breaker_missing_fail_closed")
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LiveCircuitBreakerState(tripped=True, reason="breaker_corrupt_fail_closed")
    if not isinstance(payload, Mapping):
        return LiveCircuitBreakerState(tripped=True, reason="breaker_corrupt_fail_closed")
    checksum = payload.get(_CHECKSUM_FIELD)
    if not isinstance(checksum, str):
        return LiveCircuitBreakerState(tripped=True, reason="breaker_checksum_missing_fail_closed")
    body = {key: value for key, value in payload.items() if key != _CHECKSUM_FIELD}
    if checksum != _checksum(body):
        return LiveCircuitBreakerState(tripped=True, reason="breaker_checksum_mismatch_fail_closed")
    return LiveCircuitBreakerState.from_dict(body)


def save_live_circuit_breaker(
    state: LiveCircuitBreakerState,
    path: str | Path = DEFAULT_LIVE_BREAKER_PATH,
) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stamped = replace(state, updated_at=datetime.now(UTC).isoformat())
    body = stamped.to_dict()
    payload = {**body, _CHECKSUM_FIELD: _checksum(body)}
    fd, tmp_name = tempfile.mkstemp(dir=str(state_path.parent), prefix=f".{state_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp_name, state_path)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp_name)
        raise


def reset_live_circuit_breaker(
    path: str | Path = DEFAULT_LIVE_BREAKER_PATH,
    *,
    reviewer: str,
    reason: str,
) -> LiveCircuitBreakerState:
    if not reviewer.strip() or not reason.strip():
        raise LiveCircuitBreakerError("reset requires reviewer and reason")
    current = load_live_circuit_breaker(path)
    if current.reason in {
        "breaker_missing_fail_closed",
        "breaker_corrupt_fail_closed",
        "breaker_checksum_missing_fail_closed",
        "breaker_checksum_mismatch_fail_closed",
    }:
        raise LiveCircuitBreakerError(f"cannot reset invalid breaker state: {current.reason}")
    updated = LiveCircuitBreakerState(
        tripped=False,
        reason=None,
        reviewer=reviewer,
        reset_reason=reason,
    )
    save_live_circuit_breaker(updated, path)
    return updated


def _checksum(body: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(body), sort_keys=True).encode("utf-8")).hexdigest()


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
