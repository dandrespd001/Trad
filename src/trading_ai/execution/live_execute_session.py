"""Dry-run live execution session gate.

S8 intentionally cannot submit real orders. It validates readiness/risk evidence
and writes audit artifacts for later go-live review.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import ConfigError, load_risk_config
from trading_ai.execution.live_circuit_breaker import load_live_circuit_breaker
from trading_ai.execution.live_readiness import STATE_READY
from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact

DEFAULT_OUTPUT_DIR = "reports/tmp/live_execute_session"


@dataclass(frozen=True)
class LiveExecuteSessionResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_live_execute_session(
    *,
    as_of_date: str,
    readiness: str | Path,
    risk: str | Path,
    reviewer: str,
    reason: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    expected_readiness_hash: str | None = None,
    dry_run: bool = True,
    command_evidence: Sequence[str] = (),
    generated_at: str | None = None,
    fake_broker: object | None = None,
    breaker_state_path: str | Path | None = None,
) -> LiveExecuteSessionResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "live_execute_session.json"
    markdown_path = output_root / "live_execute_session.md"
    generated = generated_at or datetime.now(UTC).isoformat()
    blockers: list[str] = []
    status = "DRY_RUN_READY"
    readiness_hash = _sha256_or_none(readiness)
    risk_hash = _sha256_or_none(risk)
    readiness_payload: dict[str, object] = {}

    try:
        readiness_payload = read_json_artifact(readiness)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        blockers.append("readiness_read_error")
        status = "ERROR"
        readiness_payload = {"error": str(exc)}

    if status != "ERROR":
        readiness_state = str(readiness_payload.get("live_readiness_state") or "")
        if readiness_state != STATE_READY:
            blockers.append("readiness_not_ready")
        readiness_safety = _mapping(readiness_payload.get("safety"))
        if readiness_safety.get("orders_submitted") is True:
            blockers.append("readiness_orders_submitted")
        if readiness_safety.get("live_trading_authorized") is True:
            blockers.append("readiness_live_authority_present")

    if expected_readiness_hash and readiness_hash != expected_readiness_hash:
        blockers.append("readiness_hash_mismatch")
    if not reviewer.strip() or not reason.strip():
        blockers.append("human_review_required")
    if not dry_run:
        blockers.append("dry_run_required")
    breaker_state = None
    if breaker_state_path is not None:
        breaker_state = load_live_circuit_breaker(breaker_state_path)
        if breaker_state.tripped:
            blockers.append(f"breaker_tripped:{breaker_state.reason or 'unknown'}")

    try:
        load_risk_config(risk, allow_live=False)
    except (OSError, ConfigError, ValueError) as exc:
        blockers.append("risk_config_invalid")
        if status != "ERROR":
            status = "ERROR"
        readiness_payload.setdefault("risk_error", str(exc))

    if status != "ERROR" and blockers:
        status = "BLOCKED"

    payload = {
        "schema_version": "1.0",
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "dry_run": True,
        "reviewer": reviewer,
        "reason": reason,
        "readiness": str(Path(readiness)),
        "risk": str(Path(risk)),
        "readiness_hash": readiness_hash,
        "expected_readiness_hash": expected_readiness_hash,
        "risk_hash": risk_hash,
        "breaker_state": (
            {
                "path": str(Path(breaker_state_path)),
                "tripped": breaker_state.tripped,
                "reason": breaker_state.reason,
            }
            if breaker_state_path is not None and breaker_state is not None
            else None
        ),
        "command_evidence": list(command_evidence),
        "blockers": _dedupe(blockers),
        "readiness_state": readiness_payload.get("live_readiness_state"),
        "authority": {
            "human_review_required": True,
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "live_trading_authorized": False,
        },
        "safety": {
            "dry_run_only": True,
            "fake_broker_used": fake_broker is not None,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_execution_enabled": False,
            "live_trading_allowed": False,
        },
    }
    write_json_artifact(payload, output_path)
    write_text_artifact(render_live_execute_session_markdown(payload), markdown_path)
    return LiveExecuteSessionResult(
        exit_code=_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def render_live_execute_session_markdown(payload: Mapping[str, object]) -> str:
    blockers = payload.get("blockers")
    blocker_lines = [f"- `{item}`" for item in blockers] if isinstance(blockers, list) and blockers else ["- none"]
    return "\n".join(
        [
            "# Live Execute Session",
            "",
            f"Status: **{payload.get('status')}**",
            f"As of date: `{payload.get('as_of_date')}`",
            f"Dry-run: `{payload.get('dry_run')}`",
            f"Reviewer: `{payload.get('reviewer')}`",
            f"Readiness hash: `{payload.get('readiness_hash')}`",
            f"Risk hash: `{payload.get('risk_hash')}`",
            "",
            "## Blockers",
            "",
            *blocker_lines,
            "",
            "Orders submitted: `False`",
            "",
        ]
    )


def _sha256_or_none(path: str | Path) -> str | None:
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exit_code(status: str) -> int:
    if status == "DRY_RUN_READY":
        return 0
    if status == "BLOCKED":
        return 1
    return 2


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
