"""Human review decisions for model challenger reports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/model_challenger_decisions"
DECISION_APPROVE = "APPROVE_FOR_NEXT_PAPER_CYCLE"
DECISION_REJECT = "REJECT"
DECISION_DEFER = "DEFER"
VALID_DECISIONS = {DECISION_APPROVE, DECISION_REJECT, DECISION_DEFER}
APPROVABLE_STATUS = "REVIEWABLE"
REVIEWABLE_STATUSES = {"REVIEWABLE", "REJECTED", "BLOCKED"}


class ModelReviewDecisionOperationalError(RuntimeError):
    """Raised when the model review decision cannot be written."""


@dataclass(frozen=True)
class ModelReviewDecisionResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_model_review_decision(
    *,
    challenger_report: str | Path,
    decision: str,
    reviewer: str,
    reason: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> ModelReviewDecisionResult:
    report = build_model_review_decision(
        challenger_report=challenger_report,
        decision=decision,
        reviewer=reviewer,
        reason=reason,
        generated_at=generated_at,
    )
    decision_date = str(report.get("decision_date") or date.today().isoformat())
    output_root = Path(output_dir) / decision_date
    output_path = output_root / "decision.json"
    markdown_path = output_root / "decision.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_model_review_decision_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return ModelReviewDecisionResult(
        exit_code=0 if status == "RECORDED" else 2,
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_model_review_decision(
    *,
    challenger_report: str | Path,
    decision: str,
    reviewer: str,
    reason: str,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    path = Path(challenger_report)
    errors: list[dict[str, object]] = []
    challenger: Mapping[str, object] | None = None
    try:
        challenger = read_json_artifact(path)
    except FileNotFoundError:
        errors.append(_error("missing_challenger_report", f"challenger report is missing: {path}"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(_error("invalid_challenger_report", f"invalid challenger report JSON: {exc}"))
    normalized_decision = str(decision).upper()
    if normalized_decision not in VALID_DECISIONS:
        errors.append(_error("invalid_decision", f"unsupported review decision: {decision}"))
    challenger_status = str(_mapping(challenger).get("status") or "UNKNOWN").upper()
    if challenger is not None:
        if normalized_decision == DECISION_APPROVE and challenger_status != APPROVABLE_STATUS:
            errors.append(
                _error(
                    "approve_requires_reviewable_report",
                    "APPROVE_FOR_NEXT_PAPER_CYCLE requires model-challenger-report status REVIEWABLE",
                )
            )
        elif normalized_decision in {DECISION_REJECT, DECISION_DEFER} and challenger_status not in REVIEWABLE_STATUSES:
            errors.append(
                _error(
                    "decision_requires_reviewable_rejected_or_blocked",
                    "decision cannot be recorded for this challenger status",
                )
            )
    decision_date = _decision_date(_mapping(challenger), generated)
    status = "ERROR" if errors else "RECORDED"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "decision_date": decision_date,
        "status": status,
        "decision": normalized_decision,
        "reviewer": reviewer,
        "reason": reason,
        "challenger_report": {
            "path": str(path),
            "status": challenger_status,
        },
        "artifacts": {
            "challenger_report": _artifact_summary(path),
        },
        "errors": errors,
        "authority": {
            "mutates_latest_model": False,
            "automatic_champion_replacement": False,
            "latest_model_path": "models/latest_model.json",
            "promotion_authorized": normalized_decision == DECISION_APPROVE and not errors,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_model_review_decision_markdown(report: Mapping[str, object]) -> str:
    raw_errors = report.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) else []
    lines = [
        "# Model Review Decision",
        "",
        f"Status: **{report.get('status') or 'ERROR'}**",
        f"Decision: **{report.get('decision') or ''}**",
        f"Reviewer: `{report.get('reviewer') or ''}`",
        f"Reason: {_escape(report.get('reason') or '')}",
        "",
        "## Errors",
        "",
        "| Code | Message |",
        "| --- | --- |",
    ]
    if errors:
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(f"| `{_escape(error.get('code') or '')}` | {_escape(error.get('message') or '')} |")
    else:
        lines.append("| none | Decision recorded for next paper-cycle governance. |")
    lines.extend(["", "Mutates latest model: `False`", "Live trading allowed: `False`", ""])
    return "\n".join(lines)


def _artifact_summary(path: Path) -> dict[str, object]:
    payload: dict[str, object] = {"path": str(path), "sha256": None}
    if path.exists() and path.is_file():
        payload["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return payload


def _decision_date(challenger: Mapping[str, object], generated_at: str) -> str:
    for candidate in (challenger.get("generated_at"), generated_at):
        if candidate not in {None, ""}:
            text = str(candidate)
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                if len(text) >= 10:
                    return text[:10]
    return date.today().isoformat()


def _error(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message}


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise ModelReviewDecisionOperationalError("model review decision must be a JSON object")
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
