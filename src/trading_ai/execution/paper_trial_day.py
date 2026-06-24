"""Daily paper-trial gate that summarizes operational recovery needs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import load_risk_config
from trading_ai.execution.paper_common import read_json_artifact, reason_codes, write_json_artifact, write_text_artifact
from trading_ai.execution.paper_graduation import evaluate_paper_graduation

DEFAULT_OUTPUT_DIR = "reports/tmp/paper_trial_day"
STATE_OK = "TRIAL_DAY_OK"
STATE_WARN = "TRIAL_DAY_WARN"
STATE_RECOVERY = "RECOVERY_REQUIRED"
STATE_ERROR = "ERROR"


@dataclass(frozen=True)
class PaperTrialDayResult:
    exit_code: int
    trial_state: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_trial_day(
    *,
    as_of_date: str,
    cycle: str | Path,
    monitor: str | Path,
    performance: str | Path,
    shadow_outcome: str | Path,
    risk: str | Path = "configs/risk.yml",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperTrialDayResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "trial_day.json"
    markdown_path = output_root / "trial_day.md"
    generated = generated_at or datetime.now(UTC).isoformat()
    sources = {
        "cycle": str(Path(cycle)),
        "monitor": str(Path(monitor)),
        "performance": str(Path(performance)),
        "shadow_outcome": str(Path(shadow_outcome)),
        "risk": str(Path(risk)),
    }
    paper_graduation = evaluate_paper_graduation(risk_limits=load_risk_config(risk))
    try:
        cycle_payload = read_json_artifact(cycle)
        monitor_payload = read_json_artifact(monitor)
        performance_payload = read_json_artifact(performance)
        shadow_payload = read_json_artifact(shadow_outcome)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload = _payload(
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_ERROR,
            blockers=["artifact_read_error"],
            warnings=[],
            sources=sources,
            paper_graduation=paper_graduation,
            details={"error": str(exc)},
        )
        return _write(payload, output_path, markdown_path)

    blockers = _dedupe(
        [
            *_cycle_blockers(cycle_payload),
            *_monitor_blockers(monitor_payload),
            *_performance_blockers(performance_payload),
            *_shadow_blockers(shadow_payload),
        ]
    )
    warnings = _dedupe([*_cycle_warnings(cycle_payload), *_shadow_warnings(shadow_payload)])
    state = STATE_RECOVERY if blockers else STATE_WARN if warnings else STATE_OK
    payload = _payload(
        as_of_date=as_of_date,
        generated_at=generated,
        state=state,
        blockers=blockers,
        warnings=warnings,
        sources=sources,
        paper_graduation=paper_graduation,
        details={
            "cycle_state": cycle_payload.get("state"),
            "monitor_status": monitor_payload.get("status"),
            "performance_status": performance_payload.get("status"),
            "shadow_state": shadow_payload.get("state"),
        },
    )
    return _write(payload, output_path, markdown_path)


def _cycle_blockers(payload: Mapping[str, object]) -> list[str]:
    state = str(payload.get("state") or "").upper()
    blockers: list[str] = []
    if state in {"BLOCKED", "ERROR"}:
        blockers.append("cycle_blocked" if state == "BLOCKED" else "cycle_error")
    blockers.extend(reason_codes(payload.get("reasons")))
    return blockers


def _cycle_warnings(payload: Mapping[str, object]) -> list[str]:
    state = str(payload.get("state") or "").upper()
    if state in {"EVIDENCE_ONLY", "NO_TRADE_REVIEW"}:
        return [state.lower()]
    return []


def _monitor_blockers(payload: Mapping[str, object]) -> list[str]:
    blockers: list[str] = []
    status = str(payload.get("status") or "").upper()
    if status in {"CRITICAL", "ERROR"}:
        blockers.append("monitor_critical" if status == "CRITICAL" else "monitor_error")
    counts = _mapping(_mapping(payload.get("broker_snapshot")).get("counts"))
    if _float_value(counts.get("orders")) > 0.0:
        blockers.append("open_broker_orders")
    if _float_value(counts.get("positions")) > 0.0:
        blockers.append("open_broker_positions")
    for alert in _object_list(payload.get("alerts")):
        if isinstance(alert, Mapping) and str(alert.get("severity") or "").upper() == "CRITICAL":
            code = str(alert.get("code") or "").strip()
            if code:
                blockers.append(code)
    return blockers


def _performance_blockers(payload: Mapping[str, object]) -> list[str]:
    blockers = reason_codes(payload.get("blockers"))
    status = str(payload.get("status") or "").upper()
    if status in {"CRITICAL", "ERROR"}:
        blockers.append("paper_performance_critical" if status == "CRITICAL" else "paper_performance_error")
    metrics = _mapping(payload.get("paper_metrics"))
    if _float_value(metrics.get("pending_closeouts")) > 0.0:
        blockers.append("closeout_pending")
    if _float_value(metrics.get("unmatched_closeouts")) > 0.0:
        blockers.append("closeout_unmatched")
    reconciliation = _mapping(payload.get("statement_reconciliation"))
    if str(reconciliation.get("status") or "").upper() not in {"", "MATCHED", "OK"}:
        blockers.append("statement_mismatch")
    if _float_value(reconciliation.get("missing_fills")) > 0.0:
        blockers.append("fills_unreconciled")
    return blockers


def _shadow_blockers(payload: Mapping[str, object]) -> list[str]:
    if str(payload.get("state") or "").upper() == "BLOCKED":
        return ["shadow_outcome_blocked", *reason_codes(payload.get("reasons"))]
    return []


def _shadow_warnings(payload: Mapping[str, object]) -> list[str]:
    if str(payload.get("state") or "").upper() == "NO_SHADOW_SIGNAL":
        return ["no_shadow_signal"]
    return []


def _payload(
    *,
    as_of_date: str,
    generated_at: str,
    state: str,
    blockers: list[str],
    warnings: list[str],
    sources: Mapping[str, object],
    paper_graduation: Mapping[str, object],
    details: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "trial_state": state,
        "status": state,
        "ready_for_next_trial_day": state in {STATE_OK, STATE_WARN},
        "recovery_required": state == STATE_RECOVERY,
        "blockers": blockers,
        "warnings": warnings,
        "sources": dict(sources),
        "paper_graduation": dict(paper_graduation),
        "details": dict(details),
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
        },
    }


def _write(payload: dict[str, object], output_path: Path, markdown_path: Path) -> PaperTrialDayResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(_render(payload), markdown_path)
    state = str(payload.get("trial_state") or STATE_ERROR)
    exit_code = 0 if state in {STATE_OK, STATE_WARN} else 1 if state == STATE_RECOVERY else 2
    return PaperTrialDayResult(exit_code, state, output_path, markdown_path, payload)


def _render(payload: Mapping[str, object]) -> str:
    graduation = _mapping(payload.get("paper_graduation"))
    lines = [
        "# Paper Trial Day",
        "",
        f"State: **{payload.get('trial_state')}**",
        f"As of date: `{payload.get('as_of_date')}`",
        "",
        "## Paper Graduation",
        "",
        f"- Stage: `{graduation.get('stage') or ''}`",
        f"- Notional: `{graduation.get('paper_notional_usd') or ''}`",
        f"- Allowed: `{graduation.get('allowed')}`",
        "",
        "## Blockers",
    ]
    blockers = reason_codes(payload.get("blockers"))
    lines.extend([f"- `{blocker}`" for blocker in blockers] or ["- none"])
    lines.append("")
    return "\n".join(lines)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _float_value(value: object) -> float:
    if value in {None, ""}:
        return 0.0
    return float(str(value))
