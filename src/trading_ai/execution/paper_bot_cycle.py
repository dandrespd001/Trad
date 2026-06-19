"""Thin paper-only bot cycle wrapper around existing paper commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.execution.paper_autopilot_plan import (
    ACTION_BLOCKED,
    ACTION_ELIGIBLE_FOR_PAPER_CONFIRMED,
    ACTION_REQUEST_REVIEW,
    ACTION_RUN_OFFLINE_DAILY,
    ACTION_RUN_READINESS,
    PaperAutopilotPlanResult,
    run_paper_autopilot_plan,
)
from trading_ai.execution.paper_common import read_json_artifact, redact_secrets, write_json_artifact, write_text_artifact
from trading_ai.execution.paper_daily import PaperDailyFromReadinessResult, run_paper_daily_from_readiness
from trading_ai.execution.paper_safety import aggregate_safety


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_bot_cycle"

STATE_OFFLINE_ONLY = "OFFLINE_ONLY"
STATE_NEEDS_REVIEW = "NEEDS_REVIEW"
STATE_ELIGIBLE_FOR_PAPER = "ELIGIBLE_FOR_PAPER"
STATE_PAPER_SUBMITTED = "PAPER_SUBMITTED"
STATE_PAPER_CLOSED = "PAPER_CLOSED"
STATE_BLOCKED = "BLOCKED"


class PaperBotCycleOperationalError(RuntimeError):
    """Raised when a paper bot cycle cannot be written."""


@dataclass(frozen=True)
class PaperBotCycleResult:
    exit_code: int
    state: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_bot_cycle(
    *,
    as_of_date: str,
    readiness: str | Path,
    human_review: str | Path,
    llm_review: str | Path | None = None,
    ops_check: str | Path | None = None,
    evidence_index: str | Path | None = None,
    signal_plan: str | Path | None = None,
    permissions: str | Path = "configs/permissions.yml",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    confirm_readiness: bool = False,
    confirm_paper: bool = False,
    confirm_auto_submit: bool = False,
    confirm_auto_close: bool = False,
    generated_at: str | None = None,
) -> PaperBotCycleResult:
    output_root = Path(output_dir) / as_of_date
    autopilot_dir = output_root / "autopilot"
    confirmations = {
        "confirm_readiness": confirm_readiness,
        "confirm_paper": confirm_paper,
        "confirm_auto_submit": confirm_auto_submit,
        "confirm_auto_close": confirm_auto_close,
    }
    autopilot = run_paper_autopilot_plan(
        as_of_date=as_of_date,
        readiness=readiness,
        ops_check=ops_check,
        evidence_index=evidence_index,
        llm_review=llm_review,
        human_review=human_review,
        permissions=permissions,
        output_dir=autopilot_dir,
        generated_at=generated_at,
    )
    paper_daily: PaperDailyFromReadinessResult | None = None
    state = _state_from_autopilot(autopilot)
    reasons = _reason_list(autopilot.payload.get("reasons"))
    signal_plan_artifact = _read_signal_plan(signal_plan)
    if signal_plan_artifact["present"] is True and signal_plan_artifact["eligible_for_paper"] is not True:
        reasons.append(
            {
                "severity": "INFO",
                "code": "signal_plan_not_eligible",
                "message": "signal arbitration did not mark a paper signal eligible",
            }
        )
        if state == STATE_ELIGIBLE_FOR_PAPER:
            state = STATE_NEEDS_REVIEW
    if signal_plan_artifact["status"] == "ERROR":
        reasons.append(
            {
                "severity": "ERROR",
                "code": "invalid_signal_plan",
                "message": "signal plan artifact could not be read",
            }
        )
        state = STATE_BLOCKED

    if state == STATE_ELIGIBLE_FOR_PAPER and _all_confirmed(confirmations):
        broker_dir = output_root / "broker_confirmed"
        paper_daily = run_paper_daily_from_readiness(
            readiness_path=readiness,
            confirm_readiness=confirm_readiness,
            confirm_paper=confirm_paper,
            confirm_auto_close=confirm_auto_close,
            confirm_auto_submit=confirm_auto_submit,
            output_dir=broker_dir,
        )
        state = _state_from_paper_daily(paper_daily, confirm_auto_close=confirm_auto_close)
        reasons.extend(_paper_daily_reasons(paper_daily))

    safety = aggregate_safety(autopilot.payload, paper_daily.payload if paper_daily is not None else None)
    orders_submitted_by_cycle = paper_daily is not None and bool(safety.get("orders_submitted"))
    payload = _redact_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at or _utc_now(),
            "as_of_date": as_of_date,
            "state": state,
            "exit_code": _exit_code_for_state(state, autopilot=autopilot, paper_daily=paper_daily),
            "confirmations": confirmations,
            "sources": {
                "readiness": str(Path(readiness)),
                "human_review": str(Path(human_review)),
                "llm_review": str(Path(llm_review)) if llm_review is not None else None,
                "ops_check": str(Path(ops_check)) if ops_check is not None else None,
                "evidence_index": str(Path(evidence_index)) if evidence_index is not None else None,
                "signal_plan": str(Path(signal_plan)) if signal_plan is not None else None,
                "permissions": str(Path(permissions)),
            },
            "autopilot": {
                "status": autopilot.status,
                "exit_code": autopilot.exit_code,
                "action": autopilot.payload.get("action"),
                "output_path": str(autopilot.output_path),
                "markdown_path": str(autopilot.markdown_path),
            },
            "artifacts": {
                "cycle_json": str(output_root / "cycle.json"),
                "cycle_markdown": str(output_root / "cycle.md"),
                "autopilot_plan": {
                    "status": autopilot.status,
                    "path": str(autopilot.output_path),
                    "markdown_path": str(autopilot.markdown_path),
                },
                "paper_daily_from_readiness": _paper_daily_artifact(paper_daily),
                "signal_plan": signal_plan_artifact,
            },
            "reasons": reasons,
            "authority": {
                "llm_authority": "none",
                "orders_submitted_by_cycle": orders_submitted_by_cycle,
                "observed_child_orders_submitted": bool(safety.get("orders_submitted")),
                "risk_changed": False,
                "live_trading_authorized": False,
            },
            "safety": safety,
        }
    )
    output_path = output_root / "cycle.json"
    markdown_path = output_root / "cycle.md"
    write_json_artifact(payload, output_path)
    write_text_artifact(render_paper_bot_cycle_markdown(payload), markdown_path)
    return PaperBotCycleResult(
        exit_code=_int_value(payload.get("exit_code"), default=2),
        state=str(payload.get("state") or STATE_BLOCKED),
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def render_paper_bot_cycle_markdown(payload: Mapping[str, object]) -> str:
    reasons = _object_list(payload.get("reasons"))
    confirmations = _mapping(payload.get("confirmations"))
    lines = [
        "# Paper Bot Cycle",
        "",
        f"State: **{payload.get('state') or STATE_BLOCKED}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        "",
        "## Confirmations",
        "",
        "| Confirmation | Value |",
        "| --- | --- |",
    ]
    for name in ("confirm_readiness", "confirm_paper", "confirm_auto_submit", "confirm_auto_close"):
        lines.append(f"| `{name}` | `{confirmations.get(name) is True}` |")
    lines.extend(["", "## Reasons", "", "| Severity | Code | Message |", "| --- | --- | --- |"])
    if reasons:
        for reason in reasons:
            if isinstance(reason, Mapping):
                lines.append(
                    "| "
                    f"`{_escape(reason.get('severity') or '')}` "
                    f"| `{_escape(reason.get('code') or '')}` "
                    f"| {_escape(reason.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | Cycle has no blocking reasons. |")
    lines.extend(
        [
            "",
            "LLM authority: `none`",
            "Broker client built by cycle: `False`",
            "Credentials read by cycle: `False`",
            "Live trading allowed: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _state_from_autopilot(result: PaperAutopilotPlanResult) -> str:
    action = str(result.payload.get("action") or "")
    status = str(result.payload.get("status") or "").upper()
    if status in {"BLOCKED", "ERROR"} or action == ACTION_BLOCKED:
        return STATE_BLOCKED
    if action == ACTION_ELIGIBLE_FOR_PAPER_CONFIRMED:
        return STATE_ELIGIBLE_FOR_PAPER
    if action == ACTION_REQUEST_REVIEW:
        return STATE_NEEDS_REVIEW
    if action in {ACTION_RUN_READINESS, ACTION_RUN_OFFLINE_DAILY}:
        return STATE_OFFLINE_ONLY
    return STATE_BLOCKED


def _state_from_paper_daily(result: PaperDailyFromReadinessResult, *, confirm_auto_close: bool) -> str:
    if result.exit_code != 0:
        return STATE_BLOCKED
    return STATE_PAPER_CLOSED if confirm_auto_close else STATE_PAPER_SUBMITTED


def _exit_code_for_state(
    state: str,
    *,
    autopilot: PaperAutopilotPlanResult,
    paper_daily: PaperDailyFromReadinessResult | None,
) -> int:
    if paper_daily is not None:
        return paper_daily.exit_code
    if state == STATE_BLOCKED:
        return autopilot.exit_code if autopilot.exit_code in {1, 2} else 1
    return 0


def _all_confirmed(confirmations: Mapping[str, object]) -> bool:
    return all(confirmations.get(name) is True for name in confirmations)


def _paper_daily_artifact(result: PaperDailyFromReadinessResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "status": result.status,
        "exit_code": result.exit_code,
        "path": str(result.output_path),
        "markdown_path": str(result.markdown_path),
    }


def _read_signal_plan(path: str | Path | None) -> dict[str, object]:
    if path is None:
        return {"present": False, "path": None, "status": "NOT_PROVIDED", "eligible_for_paper": None}
    signal_path = Path(path)
    try:
        payload = read_json_artifact(signal_path)
    except (OSError, ValueError):
        return {"present": True, "path": str(signal_path), "status": "ERROR", "eligible_for_paper": False}
    return {
        "present": True,
        "path": str(signal_path),
        "status": payload.get("decision") or payload.get("status") or "UNKNOWN",
        "eligible_for_paper": payload.get("eligible_for_paper") is True,
        "selected_symbol": payload.get("selected_symbol"),
    }


def _reason_list(value: object) -> list[dict[str, object]]:
    return [dict(reason) for reason in _object_list(value) if isinstance(reason, Mapping)]


def _paper_daily_reasons(result: PaperDailyFromReadinessResult) -> list[dict[str, object]]:
    reasons: list[dict[str, object]] = []
    for reason in _object_list(result.payload.get("reasons")):
        reasons.append({"severity": "INFO", "code": str(reason), "message": str(reason)})
    return reasons


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperBotCycleOperationalError("paper bot cycle must be a JSON object")
    return redacted


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


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int_value(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
