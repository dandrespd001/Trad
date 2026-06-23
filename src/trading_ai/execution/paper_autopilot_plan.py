"""Deterministic next-step planner for paper operations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import ConfigError, load_yaml_file
from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_review_decision import (
    DECISION_APPROVE_PAPER_CONFIRMATION,
    DECISION_DEFER,
    DECISION_REJECT,
)
from trading_ai.execution.paper_safety import aggregate_authority, aggregate_safety

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_autopilot_plan"

ACTION_RUN_READINESS = "RUN_READINESS"
ACTION_RUN_OFFLINE_DAILY = "RUN_OFFLINE_DAILY"
ACTION_REQUEST_REVIEW = "REQUEST_REVIEW"
ACTION_ELIGIBLE_FOR_PAPER_CONFIRMED = "ELIGIBLE_FOR_PAPER_CONFIRMED"
ACTION_BLOCKED = "BLOCKED"


class PaperAutopilotPlanOperationalError(RuntimeError):
    """Raised when the paper autopilot plan cannot be written."""


@dataclass(frozen=True)
class PaperAutopilotPlanResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_autopilot_plan(
    *,
    as_of_date: str,
    readiness: str | Path,
    ops_check: str | Path | None = None,
    evidence_index: str | Path | None = None,
    llm_review: str | Path | None = None,
    human_review: str | Path | None = None,
    permissions: str | Path = "configs/permissions.yml",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperAutopilotPlanResult:
    report = build_paper_autopilot_plan(
        as_of_date=as_of_date,
        readiness=readiness,
        ops_check=ops_check,
        evidence_index=evidence_index,
        llm_review=llm_review,
        human_review=human_review,
        permissions=permissions,
        generated_at=generated_at,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "autopilot_plan.json"
    markdown_path = output_root / "autopilot_plan.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_autopilot_plan_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return PaperAutopilotPlanResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_paper_autopilot_plan(
    *,
    as_of_date: str,
    readiness: str | Path,
    ops_check: str | Path | None = None,
    evidence_index: str | Path | None = None,
    llm_review: str | Path | None = None,
    human_review: str | Path | None = None,
    permissions: str | Path = "configs/permissions.yml",
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    errors: list[dict[str, object]] = []
    readiness_artifact = _read_required(readiness, "readiness", errors)
    ops_artifact = _read_optional(ops_check, "ops_check", errors)
    evidence_artifact = _read_optional(evidence_index, "evidence_index", errors)
    llm_artifact = _read_optional(llm_review, "llm_review", errors)
    human_artifact = _read_optional(human_review, "human_review", errors)
    permission_summary = _permission_summary(permissions, errors)

    if errors:
        action = ACTION_BLOCKED
        status = "ERROR"
        reasons = [_reason("ERROR", error["code"], error["message"]) for error in errors]
    else:
        action, status, reasons = _decide_action(
            readiness=_payload(readiness_artifact),
            ops_check=_payload(ops_artifact),
            evidence_index=_payload(evidence_artifact),
            llm_review=_payload(llm_artifact),
            human_review=human_artifact,
            permissions=permission_summary,
            ops_present=bool(ops_artifact.get("present")),
            evidence_present=bool(evidence_artifact.get("present")),
        )
    return _report(
        as_of_date=as_of_date,
        generated_at=generated,
        status=status,
        action=action,
        reasons=reasons,
        sources={
            "readiness": str(Path(readiness)),
            "ops_check": str(Path(ops_check)) if ops_check is not None else None,
            "evidence_index": str(Path(evidence_index)) if evidence_index is not None else None,
            "llm_review": str(Path(llm_review)) if llm_review is not None else None,
            "human_review": str(Path(human_review)) if human_review is not None else None,
            "permissions": str(Path(permissions)),
        },
        artifacts={
            "readiness": readiness_artifact,
            "ops_check": ops_artifact,
            "evidence_index": evidence_artifact,
            "llm_review": llm_artifact,
            "human_review": human_artifact,
        },
        human_review=human_artifact,
        llm_review=llm_artifact,
        permissions=permission_summary,
        errors=errors,
    )


def render_paper_autopilot_plan_markdown(report: Mapping[str, object]) -> str:
    reasons = _object_list(report.get("reasons"))
    lines = [
        "# Paper Autopilot Plan",
        "",
        f"Status: **{report.get('status') or 'ERROR'}**",
        f"Action: **{report.get('action') or ACTION_BLOCKED}**",
        f"As of date: `{report.get('as_of_date') or ''}`",
        "",
        "## Reasons",
        "",
        "| Severity | Code | Message |",
        "| --- | --- | --- |",
    ]
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
        lines.append("| OK | none | Plan is eligible for the next paper-only step. |")
    lines.extend(
        [
            "",
            "LLM authority: `none`",
            "Broker client built: `False`",
            "Credentials read: `False`",
            "Orders submitted: `False`",
            "Live trading allowed: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _decide_action(
    *,
    readiness: Mapping[str, object],
    ops_check: Mapping[str, object],
    evidence_index: Mapping[str, object],
    llm_review: Mapping[str, object],
    human_review: Mapping[str, object],
    permissions: Mapping[str, object],
    ops_present: bool,
    evidence_present: bool,
) -> tuple[str, str, list[dict[str, object]]]:
    reasons: list[dict[str, object]] = []
    if bool(permissions.get("live_trading_allowed")):
        return (
            ACTION_BLOCKED,
            "BLOCKED",
            [_reason("CRITICAL", "live_permission_not_allowed", "permissions must keep live trading disabled")],
        )
    safety_reasons = _safety_reasons(readiness, ops_check, evidence_index, llm_review)
    if safety_reasons:
        return ACTION_BLOCKED, "BLOCKED", safety_reasons
    readiness_status = str(readiness.get("status") or "UNKNOWN").upper()
    if readiness_status != "READY" or readiness.get("ready_for_paper_daily") is not True:
        return (
            ACTION_RUN_READINESS,
            "OK",
            [_reason("INFO", "readiness_not_ready", "run or regenerate readiness before planning paper confirmation")],
        )
    if not ops_present or not evidence_present:
        missing = []
        if not ops_present:
            missing.append("ops_check")
        if not evidence_present:
            missing.append("evidence_index")
        return (
            ACTION_RUN_OFFLINE_DAILY,
            "OK",
            [_reason("INFO", "offline_evidence_missing", f"run offline daily evidence first: {', '.join(missing)}")],
        )
    ops_status = str(ops_check.get("status") or "UNKNOWN").upper()
    if ops_status in {"CRITICAL", "ERROR", "BLOCKED"}:
        return (
            ACTION_BLOCKED,
            "BLOCKED",
            [_reason("CRITICAL", "ops_check_blocking", f"paper ops check is {ops_status}")],
        )
    evidence_status = str(evidence_index.get("status") or "UNKNOWN").upper()
    if evidence_status == "ERROR":
        return (
            ACTION_BLOCKED,
            "BLOCKED",
            [_reason("ERROR", "evidence_index_error", "paper evidence index is ERROR")],
        )
    review_decision = _human_review_decision(human_review)
    if review_decision is None:
        reasons.extend(_review_reasons(ops_check, evidence_index))
        reasons.append(_reason("INFO", "human_review_missing", "human review is required before paper confirmation"))
        return ACTION_REQUEST_REVIEW, "OK", _dedupe_reasons(reasons)
    if review_decision == DECISION_DEFER:
        reasons.extend(_review_reasons(ops_check, evidence_index))
        reasons.append(_reason("INFO", "human_review_deferred", "human review deferred paper confirmation"))
        return ACTION_REQUEST_REVIEW, "OK", _dedupe_reasons(reasons)
    if review_decision == DECISION_REJECT:
        return (
            ACTION_BLOCKED,
            "BLOCKED",
            [_reason("CRITICAL", "human_review_rejected", "human review rejected paper confirmation")],
        )
    if review_decision != DECISION_APPROVE_PAPER_CONFIRMATION:
        reasons.extend(_review_reasons(ops_check, evidence_index))
        reasons.append(
            _reason("WARNING", "human_review_not_approving", "human review did not approve paper confirmation")
        )
        return ACTION_REQUEST_REVIEW, "OK", _dedupe_reasons(reasons)
    reasons.extend(_review_reasons(ops_check, evidence_index))
    if not reasons:
        reasons.append(_reason("OK", "human_review_recorded", "human review is recorded"))
    return ACTION_ELIGIBLE_FOR_PAPER_CONFIRMED, "OK", _dedupe_reasons(reasons)


def _review_reasons(ops_check: Mapping[str, object], evidence_index: Mapping[str, object]) -> list[dict[str, object]]:
    reasons: list[dict[str, object]] = []
    for issue in _object_list(ops_check.get("issues")) + _object_list(evidence_index.get("issues")):
        if not isinstance(issue, Mapping):
            continue
        severity = str(issue.get("severity") or "WARNING")
        code = str(issue.get("code") or "paper_review_warning")
        message = str(issue.get("message") or code)
        reasons.append(_reason(severity, code, message))
    return reasons


def _safety_reasons(*payloads: Mapping[str, object]) -> list[dict[str, object]]:
    reasons: list[dict[str, object]] = []
    for payload in payloads:
        safety = _mapping(payload.get("safety"))
        if bool(safety.get("broker_client_built")):
            reasons.append(
                _reason("CRITICAL", "broker_client_built", "input evidence reports a broker client was built")
            )
        if bool(safety.get("credentials_read")):
            reasons.append(_reason("CRITICAL", "credentials_read", "input evidence reports credentials were read"))
        if bool(safety.get("orders_submitted")):
            reasons.append(_reason("CRITICAL", "orders_submitted", "input evidence reports orders were submitted"))
        if bool(safety.get("live_trading_allowed")) or bool(safety.get("live_trading_authorized")):
            reasons.append(
                _reason("CRITICAL", "live_trading_not_allowed", "input evidence reports live trading enabled")
            )
    return _dedupe_reasons(reasons)


def _report(
    *,
    as_of_date: str,
    generated_at: str,
    status: str,
    action: str,
    reasons: Iterable[Mapping[str, object]],
    sources: Mapping[str, object],
    artifacts: Mapping[str, object],
    human_review: Mapping[str, object],
    llm_review: Mapping[str, object],
    permissions: Mapping[str, object],
    errors: list[dict[str, object]],
) -> dict[str, object]:
    safety = aggregate_safety(*[_payload(value) for value in artifacts.values()])
    authority = aggregate_authority(
        orders_submitted=bool(safety.get("orders_submitted")),
        broker_client_built=bool(safety.get("broker_client_built")),
        extra={"orders_submitted": bool(safety.get("orders_submitted"))},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": status,
        "action": action,
        "reasons": [dict(reason) for reason in reasons],
        "sources": dict(sources),
        "artifacts": {key: _artifact_summary(value) for key, value in artifacts.items()},
        "human_review": {
            "present": _human_review_present(human_review),
            "path": human_review.get("path"),
            "status": _payload(human_review).get("status") or _payload(human_review).get("decision"),
            "decision": _payload(human_review).get("decision"),
        },
        "llm_review": {
            "present": bool(llm_review.get("present")),
            "path": llm_review.get("path"),
            "recommendation": _mapping(_payload(llm_review).get("review")).get("recommendation"),
            "llm_authority": _mapping(_payload(llm_review).get("review")).get("llm_authority", "none"),
        },
        "permissions": dict(permissions),
        "errors": errors,
        "authority": authority,
        "safety": safety,
    }


def _permission_summary(path: str | Path, errors: list[dict[str, object]]) -> dict[str, object]:
    try:
        payload = load_yaml_file(path)
    except (ConfigError, OSError) as exc:
        errors.append(_error("invalid_permissions", str(exc)))
        return {"path": str(Path(path)), "live_trading_allowed": False}
    risk_limits = _mapping(payload.get("risk_limits"))
    capabilities = _mapping(payload.get("capabilities"))
    live_prohibited = _mapping(capabilities.get("live_prohibited"))
    return {
        "path": str(Path(path)),
        "live_trading_allowed": bool(risk_limits.get("live_trading_allowed", False)),
        "live_prohibited": bool(live_prohibited.get("prohibited", [])),
    }


def _read_required(path: str | Path, name: str, errors: list[dict[str, object]]) -> dict[str, object]:
    artifact_path = Path(path)
    try:
        return {"present": True, "path": str(artifact_path), "payload": read_json_artifact(artifact_path)}
    except FileNotFoundError:
        errors.append(_error(f"missing_{name}", f"{name} artifact is missing: {artifact_path}"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(_error(f"invalid_{name}", f"invalid {name} JSON: {exc}"))
    return {"present": False, "path": str(artifact_path), "payload": {}}


def _read_optional(path: str | Path | None, name: str, errors: list[dict[str, object]]) -> dict[str, object]:
    if path is None:
        return {"present": False, "path": None, "payload": {}}
    artifact_path = Path(path)
    try:
        return {"present": True, "path": str(artifact_path), "payload": read_json_artifact(artifact_path)}
    except FileNotFoundError:
        return {"present": False, "path": str(artifact_path), "payload": {}}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(_error(f"invalid_{name}", f"invalid {name} JSON: {exc}"))
        return {"present": True, "path": str(artifact_path), "payload": {}}


def _artifact_summary(value: object) -> dict[str, object]:
    artifact = _mapping(value)
    payload = _payload(artifact)
    return {
        "present": bool(artifact.get("present")),
        "path": artifact.get("path"),
        "status": payload.get("status") or payload.get("decision") or "UNKNOWN",
    }


def _payload(value: object) -> Mapping[str, object]:
    return _mapping(_mapping(value).get("payload"))


def _human_review_present(artifact: Mapping[str, object]) -> bool:
    if not bool(artifact.get("present")):
        return False
    payload = _payload(artifact)
    status = str(payload.get("status") or "").upper()
    return status in {"RECORDED", "OK"} and payload.get("decision") not in {None, ""}


def _human_review_decision(artifact: Mapping[str, object]) -> str | None:
    if not _human_review_present(artifact):
        return None
    return str(_payload(artifact).get("decision") or "").upper()


def _reason(severity: str, code: object, message: object) -> dict[str, object]:
    return {"severity": str(severity), "code": str(code), "message": str(message)}


def _error(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message}


def _dedupe_reasons(reasons: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for reason in reasons:
        normalized = dict(reason)
        key = (str(normalized.get("severity") or ""), str(normalized.get("code") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperAutopilotPlanOperationalError("paper autopilot plan must be a JSON object")
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
