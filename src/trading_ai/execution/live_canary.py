"""Human-gated USD 1 live canary precheck and evidence writer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.execution.live_alpaca import LiveOrder
from trading_ai.execution.live_circuit_breaker import load_live_circuit_breaker
from trading_ai.execution.live_readiness import STATE_READY
from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact

DEFAULT_OUTPUT_DIR = "reports/tmp/live_canary"
ROLLBACK_COMMAND = "python -m trading_ai.cli live-safe-flatten --dry-run"


@dataclass(frozen=True)
class LiveCanaryResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def expected_live_canary_confirmation(*, as_of_date: str, symbol: str, reviewer: str, reason: str) -> str:
    return f"I confirm LIVE CANARY {as_of_date} {symbol.upper()} USD 1 reviewer={reviewer} reason={reason}"


def run_live_canary(
    *,
    as_of_date: str,
    symbol: str,
    notional_usd: float,
    readiness: str | Path,
    expected_readiness_hash: str,
    breaker_state_path: str | Path,
    rehearsal_summary: str | Path,
    rollback_evidence: str | Path,
    reviewer: str,
    reason: str,
    confirmation: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    market_open: bool = True,
    enable_real_submit: bool = False,
    broker: Any | None = None,
    generated_at: str | None = None,
) -> LiveCanaryResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "live_canary.json"
    markdown_path = output_root / "live_canary.md"
    generated = generated_at or datetime.now(UTC).isoformat()
    clean_symbol = symbol.upper()
    blockers: list[str] = []

    readiness_path = Path(readiness)
    readiness_hash = _sha256_or_none(readiness_path)
    readiness_payload = _read_json_or_none(readiness_path)
    rehearsal_payload = _read_json_or_none(rehearsal_summary)
    rollback_payload = _read_json_or_none(rollback_evidence)
    breaker_state = load_live_circuit_breaker(breaker_state_path)

    expected_confirmation = expected_live_canary_confirmation(
        as_of_date=as_of_date,
        symbol=clean_symbol,
        reviewer=reviewer,
        reason=reason,
    )
    if confirmation != expected_confirmation:
        blockers.append("confirmation_mismatch")
    if not reviewer.strip() or not reason.strip():
        blockers.append("human_review_required")
    if readiness_hash != expected_readiness_hash:
        blockers.append("readiness_hash_mismatch")
    if _mapping(readiness_payload).get("live_readiness_state") != STATE_READY:
        blockers.append("readiness_not_ready")
    readiness_safety = _mapping(_mapping(readiness_payload).get("safety"))
    if readiness_safety.get("orders_submitted") is True:
        blockers.append("readiness_orders_submitted")
    if readiness_safety.get("live_trading_authorized") is True:
        blockers.append("readiness_live_authority_present")
    if breaker_state.tripped:
        blockers.append(f"breaker_tripped:{breaker_state.reason or 'unknown'}")
    if not market_open:
        blockers.append("market_closed")
    if not _is_usd_one(notional_usd):
        blockers.append("notional_must_be_usd_1")
    if _mapping(rehearsal_payload).get("status") != "PASSED":
        blockers.append("s0_s11_evidence_missing")
    if not _rollback_prevalidated(rollback_payload):
        blockers.append("rollback_not_prevalidated")

    broker_client_built = broker is not None
    orders_submitted = False
    post_check: dict[str, object] = {
        "order_id": None,
        "fill_status": None,
        "position": None,
        "slippage_bps": None,
        "breaker_state": {
            "tripped": breaker_state.tripped,
            "reason": breaker_state.reason,
        },
        "alert_tier": "none",
    }
    command_evidence = [
        "trading-ai live-canary --enable-real-submit",
        ROLLBACK_COMMAND,
    ]
    status = "BLOCKED" if blockers else "READY_FOR_SUBMIT"

    if not blockers and enable_real_submit:
        if broker is None:
            blockers.append("live_broker_not_injected")
            status = "BLOCKED"
        else:
            order = LiveOrder(
                symbol=clean_symbol,
                side="buy",
                client_order_id=f"live-canary-{as_of_date}-{clean_symbol}".lower(),
                notional=1.0,
            )
            submit_result = broker.submit_order(order)
            if bool(getattr(submit_result, "accepted", False)):
                orders_submitted = True
                status = "SUBMITTED"
                response = getattr(submit_result, "broker_response", None)
                response_map = response if isinstance(response, Mapping) else {}
                post_check = {
                    **post_check,
                    "order_id": response_map.get("id"),
                    "fill_status": response_map.get("status", getattr(submit_result, "status", None)),
                    "raw_status": getattr(submit_result, "status", None),
                    "alert_tier": "canary",
                }
            else:
                blockers.extend(str(reason) for reason in getattr(submit_result, "reasons", ()))
                status = "BLOCKED"

    payload = {
        "schema_version": "1.0",
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "symbol": clean_symbol,
        "notional_usd": notional_usd,
        "max_orders": 1,
        "reviewer": reviewer,
        "reason": reason,
        "confirmation_expected": expected_confirmation,
        "readiness": str(readiness_path),
        "readiness_hash": readiness_hash,
        "expected_readiness_hash": expected_readiness_hash,
        "breaker_state": {
            "path": str(Path(breaker_state_path)),
            "tripped": breaker_state.tripped,
            "reason": breaker_state.reason,
        },
        "rehearsal_summary": str(Path(rehearsal_summary)),
        "rollback_evidence": str(Path(rollback_evidence)),
        "rollback_command": ROLLBACK_COMMAND,
        "command_evidence": command_evidence,
        "blockers": _dedupe(blockers),
        "post_check": post_check,
        "safety": {
            "human_confirmation_required": True,
            "exact_confirmation_matched": confirmation == expected_confirmation,
            "broker_client_built": broker_client_built,
            "credentials_read": False,
            "orders_submitted": orders_submitted,
            "live_trading_authorized": False,
            "live_execution_enabled": enable_real_submit,
        },
    }
    write_json_artifact(payload, output_path)
    write_text_artifact(render_live_canary_markdown(payload), markdown_path)
    return LiveCanaryResult(
        exit_code=_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def render_live_canary_markdown(payload: Mapping[str, object]) -> str:
    blockers = payload.get("blockers")
    blocker_lines = [f"- `{item}`" for item in blockers] if isinstance(blockers, list) and blockers else ["- none"]
    post_check = _mapping(payload.get("post_check"))
    return "\n".join(
        [
            "# Live Canary USD 1",
            "",
            f"Status: **{payload.get('status')}**",
            f"As of date: `{payload.get('as_of_date')}`",
            f"Symbol: `{payload.get('symbol')}`",
            f"Notional USD: `{payload.get('notional_usd')}`",
            f"Reviewer: `{payload.get('reviewer')}`",
            f"Readiness hash: `{payload.get('readiness_hash')}`",
            "",
            "## Blockers",
            "",
            *blocker_lines,
            "",
            "## Post Check",
            "",
            f"- Order id: `{post_check.get('order_id')}`",
            f"- Fill status: `{post_check.get('fill_status')}`",
            f"- Alert tier: `{post_check.get('alert_tier')}`",
            "",
            f"Rollback command: `{payload.get('rollback_command')}`",
            "",
        ]
    )


def _rollback_prevalidated(payload: object) -> bool:
    data = _mapping(payload)
    safety = _mapping(data.get("safety"))
    return data.get("status") == "DRY_RUN_READY" and safety.get("orders_submitted") is False


def _read_json_or_none(path: str | Path) -> dict[str, object] | None:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _sha256_or_none(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_usd_one(value: float) -> bool:
    return abs(float(value) - 1.0) < 0.000001


def _exit_code(status: str) -> int:
    if status in {"READY_FOR_SUBMIT", "SUBMITTED"}:
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
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
