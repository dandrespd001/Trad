"""Local live observability events for dry-run and readiness gates."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import redact_secrets


class LiveJsonlEventWriter:
    """Append-only local JSONL sink. No network transport is used."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, event: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = redact_live_event({**dict(event), "sink": "local_jsonl"})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def build_live_observability_event(
    *,
    event_type: str,
    gate_status: str,
    readiness_state: str,
    breaker_state: str,
    order_intent_hash: str,
    slippage_bps: float,
    latency_ms: float,
    message: str = "",
    generated_at: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "gate_status": gate_status,
        "readiness_state": readiness_state,
        "breaker_state": breaker_state,
        "order_intent_hash": order_intent_hash,
        "slippage_bps": float(slippage_bps),
        "latency_ms": float(latency_ms),
        "alert_tier": alert_tier(gate_status=gate_status, readiness_state=readiness_state, breaker_state=breaker_state),
        "message": message,
        "safety": {
            "dry_run_only": True,
            "orders_submitted": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "network_transport": False,
        },
    }


def alert_tier(*, gate_status: str, readiness_state: str, breaker_state: str) -> str:
    if str(breaker_state).upper() in {"TRIPPED", "MISSING", "CORRUPT"}:
        return "CRITICAL"
    if str(gate_status).upper() in {"BLOCKED", "ERROR", "CRITICAL"}:
        return "CRITICAL"
    if str(readiness_state).upper() in {"BLOCKED", "ERROR"}:
        return "CRITICAL"
    if str(gate_status).upper() in {"WARN", "WARNING"}:
        return "WARN"
    return "INFO"


def redact_live_event(payload: Mapping[str, object]) -> dict[str, object]:
    redacted = _redact_value(payload)
    return redacted if isinstance(redacted, dict) else {}


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {redact_secrets(str(key), env={}): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, env={})
    return value
