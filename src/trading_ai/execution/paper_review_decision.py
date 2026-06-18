"""Human review decisions for paper-confirmed operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.execution.paper_common import redact_secrets, write_json_artifact, write_text_artifact


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_reviews"
DECISION_APPROVE_PAPER_CONFIRMATION = "APPROVE_PAPER_CONFIRMATION"
DECISION_DEFER = "DEFER"
DECISION_REJECT = "REJECT"
VALID_DECISIONS = {DECISION_APPROVE_PAPER_CONFIRMATION, DECISION_DEFER, DECISION_REJECT}


class PaperReviewDecisionOperationalError(RuntimeError):
    """Raised when a paper review decision cannot be written."""


@dataclass(frozen=True)
class PaperReviewDecisionResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_review_decision(
    *,
    as_of_date: str,
    decision: str,
    reviewer: str,
    reason: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperReviewDecisionResult:
    report = build_paper_review_decision(
        as_of_date=as_of_date,
        decision=decision,
        reviewer=reviewer,
        reason=reason,
        generated_at=generated_at,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "review.json"
    markdown_path = output_root / "review.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_review_decision_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return PaperReviewDecisionResult(
        exit_code=0 if status == "RECORDED" else 2,
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_paper_review_decision(
    *,
    as_of_date: str,
    decision: str,
    reviewer: str,
    reason: str,
    generated_at: str | None = None,
) -> dict[str, object]:
    normalized_decision = str(decision).upper()
    errors: list[dict[str, object]] = []
    if normalized_decision not in VALID_DECISIONS:
        errors.append(_error("invalid_decision", f"unsupported paper review decision: {decision}"))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "as_of_date": as_of_date,
        "status": "ERROR" if errors else "RECORDED",
        "decision": normalized_decision,
        "reviewer": reviewer,
        "reason": reason,
        "errors": errors,
        "authority": {
            "paper_confirmation_authorized": normalized_decision == DECISION_APPROVE_PAPER_CONFIRMATION and not errors,
            "orders_submitted": False,
            "broker_client_built": False,
            "credentials_read": False,
            "risk_changed": False,
            "llm_authority": "none",
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_paper_review_decision_markdown(report: Mapping[str, object]) -> str:
    errors = [error for error in _object_list(report.get("errors")) if isinstance(error, Mapping)]
    lines = [
        "# Paper Review Decision",
        "",
        f"Status: **{report.get('status') or 'ERROR'}**",
        f"Decision: **{report.get('decision') or ''}**",
        f"As of date: `{report.get('as_of_date') or ''}`",
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
            lines.append(f"| `{_escape(error.get('code') or '')}` | {_escape(error.get('message') or '')} |")
    else:
        lines.append("| none | Paper review decision recorded. |")
    lines.extend(
        [
            "",
            "Paper only: `True`",
            "Broker client built: `False`",
            "Orders submitted: `False`",
            "Live trading allowed: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _error(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message}


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperReviewDecisionOperationalError("paper review decision must be a JSON object")
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


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
