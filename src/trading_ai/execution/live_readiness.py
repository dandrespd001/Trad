"""Read-only readiness report for a future human-approved live canary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import ConfigError, load_yaml_file
from trading_ai.execution.paper_common import read_json_artifact, reason_codes, write_json_artifact, write_text_artifact

DEFAULT_OUTPUT_DIR = "reports/tmp/live_readiness"
STATE_READY = "READY_FOR_LIVE_CANARY"
STATE_BLOCKED = "BLOCKED"
STATE_ERROR = "ERROR"


@dataclass(frozen=True)
class LiveReadinessResult:
    exit_code: int
    state: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_live_readiness_report(
    *,
    as_of_date: str,
    phase_review: str | Path,
    campaign_report: str | Path,
    performance_report: str | Path,
    permissions: str | Path,
    reviewer: str,
    reason: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> LiveReadinessResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "live_readiness.json"
    markdown_path = output_root / "live_readiness.md"
    generated = generated_at or datetime.now(UTC).isoformat()
    sources = {
        "phase_review": str(Path(phase_review)),
        "campaign_report": str(Path(campaign_report)),
        "performance_report": str(Path(performance_report)),
        "permissions": str(Path(permissions)),
    }
    try:
        phase = read_json_artifact(phase_review)
        campaign = read_json_artifact(campaign_report)
        performance = read_json_artifact(performance_report)
        permission_payload = load_yaml_file(permissions)
    except (OSError, json.JSONDecodeError, ValueError, ConfigError) as exc:
        payload = _payload(
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_ERROR,
            reviewer=reviewer,
            reason=reason,
            blockers=["artifact_read_error"],
            sources=sources,
            details={"error": str(exc)},
        )
        return _write(payload, output_path, markdown_path)

    blockers = _blockers(
        phase=phase,
        campaign=campaign,
        performance=performance,
        permissions=permission_payload,
        reviewer=reviewer,
        reason=reason,
    )
    state = STATE_BLOCKED if blockers else STATE_READY
    payload = _payload(
        as_of_date=as_of_date,
        generated_at=generated,
        state=state,
        reviewer=reviewer,
        reason=reason,
        blockers=blockers,
        sources=sources,
        details={
            "phase_status": phase.get("phase_status"),
            "campaign_status": campaign.get("status"),
            "paper_evidence_state": _real_money_state(campaign, phase),
            "performance_status": performance.get("status"),
        },
    )
    return _write(payload, output_path, markdown_path)


def _blockers(
    *,
    phase: Mapping[str, object],
    campaign: Mapping[str, object],
    performance: Mapping[str, object],
    permissions: Mapping[str, object],
    reviewer: str,
    reason: str,
) -> list[str]:
    blockers: list[str] = []
    if str(phase.get("phase_status") or "").upper() != "READY_FOR_REVIEW":
        blockers.append("phase_review_not_ready")
    if _real_money_state(campaign, phase) != "PAPER_EVIDENCE_READY":
        blockers.append("paper_evidence_not_ready")
    if str(performance.get("status") or "").upper() in {"CRITICAL", "ERROR", "BLOCKED"}:
        blockers.append("paper_performance_not_clean")
    blockers.extend(reason_codes(performance.get("blockers")))
    paper_metrics = _mapping(performance.get("paper_metrics"))
    if _int_value(paper_metrics.get("complete_sessions")) < 60:
        blockers.append("paper_stability_sessions_insufficient")
    if paper_metrics.get("performance_stable") is not True:
        blockers.append("paper_performance_not_stable")
    if _live_permission_enabled(permissions):
        blockers.append("live_permissions_must_remain_disabled")
    if not reviewer.strip() or not reason.strip():
        blockers.append("human_review_required")
    return _dedupe(blockers)


def _real_money_state(campaign: Mapping[str, object], phase: Mapping[str, object]) -> str:
    campaign_state = str(_mapping(campaign.get("real_money_consideration")).get("state") or "").upper()
    if campaign_state:
        return campaign_state
    return str(_mapping(phase.get("real_money_consideration")).get("state") or "").upper()


def _live_permission_enabled(payload: Mapping[str, object]) -> bool:
    flattened = [payload]
    for value in payload.values():
        if isinstance(value, Mapping):
            flattened.append(value)
    for mapping in flattened:
        for key in ("live_trading_allowed", "live_trading_authorized", "live_execution_enabled", "broker_live_enabled"):
            if mapping.get(key) is True:
                return True
    return False


def _payload(
    *,
    as_of_date: str,
    generated_at: str,
    state: str,
    reviewer: str,
    reason: str,
    blockers: list[str],
    sources: Mapping[str, object],
    details: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "live_readiness_state": state,
        "status": "OK" if state == STATE_READY else state,
        "next_action": "human_canary_approval_required" if state == STATE_READY else "resolve_live_readiness_blockers",
        "reviewer": reviewer,
        "reason": reason,
        "blockers": blockers,
        "sources": dict(sources),
        "details": dict(details),
        "authority": {
            "review_only": True,
            "llm_authority": "none",
            "human_review_required": True,
            "orders_submitted": False,
            "risk_changed": False,
            "live_trading_authorized": False,
        },
        "safety": {
            "paper_only": False,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_execution_enabled": False,
            "live_trading_allowed": False,
        },
        "canary_plan": {
            "approval_required": "human_canary_approval",
            "live_adapter_implemented": False,
            "orders_enabled": False,
            "max_orders_per_day": 1,
            "max_notional_usd": 1.0,
            "allowed_market": "alpaca_us_etfs",
            "requires_separate_execution_implementation": True,
            "rollback_required": True,
        },
    }


def _write(payload: dict[str, object], output_path: Path, markdown_path: Path) -> LiveReadinessResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(_render(payload), markdown_path)
    state = str(payload.get("live_readiness_state") or STATE_ERROR)
    exit_code = 0 if state == STATE_READY else 1 if state == STATE_BLOCKED else 2
    return LiveReadinessResult(exit_code, state, output_path, markdown_path, payload)


def _render(payload: Mapping[str, object]) -> str:
    blockers = reason_codes(payload.get("blockers"))
    lines = [
        "# Live Readiness",
        "",
        f"State: **{payload.get('live_readiness_state')}**",
        f"As of date: `{payload.get('as_of_date')}`",
        f"Next action: `{payload.get('next_action')}`",
        "",
        "## Blockers",
    ]
    lines.extend([f"- `{blocker}`" for blocker in blockers] or ["- none"])
    lines.append("")
    lines.append("Live trading authorized: `False`")
    lines.append("")
    return "\n".join(lines)


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


def _int_value(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
