"""Cycle-level report for human model challenger review decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.execution.paper_common import redact_secrets, read_json_artifact, write_json_artifact, write_text_artifact


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/model_challenger_cycles"


class ModelReviewCycleOperationalError(RuntimeError):
    """Raised when the model review cycle report cannot be written."""


@dataclass(frozen=True)
class ModelReviewCycleReportResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_model_review_cycle_report(
    *,
    challenger_report: str | Path,
    review_decision: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> ModelReviewCycleReportResult:
    report = build_model_review_cycle_report(
        challenger_report=challenger_report,
        review_decision=review_decision,
        generated_at=generated_at,
    )
    report_date = str(report.get("cycle_date") or date.today().isoformat())
    output_root = Path(output_dir) / report_date
    output_path = output_root / "cycle_report.json"
    markdown_path = output_root / "cycle_report.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_model_review_cycle_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return ModelReviewCycleReportResult(
        exit_code=0 if status == "OK" else 2,
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_model_review_cycle_report(
    *,
    challenger_report: str | Path,
    review_decision: str | Path,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    challenger_path = Path(challenger_report)
    decision_path = Path(review_decision)
    errors: list[dict[str, object]] = []
    challenger = _read_artifact(challenger_path, "challenger_report", errors)
    decision = _read_artifact(decision_path, "review_decision", errors)
    challenger_status = str(_mapping(challenger).get("status") or "UNKNOWN").upper()
    decision_status = str(_mapping(decision).get("status") or "UNKNOWN").upper()
    review = str(_mapping(decision).get("decision") or "UNKNOWN").upper()

    if decision is not None and decision_status != "RECORDED":
        errors.append(_error("review_decision_not_recorded", "review decision status must be RECORDED"))
    if review == "APPROVE_FOR_NEXT_PAPER_CYCLE" and challenger_status != "REVIEWABLE":
        errors.append(_error("approve_requires_reviewable_report", "approval requires challenger status REVIEWABLE"))
    recommended = _recommended_next_state(review, errors=errors)
    cycle_date = _cycle_date(_mapping(decision), generated)
    status = "ERROR" if errors else "OK"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "cycle_date": cycle_date,
        "status": status,
        "recommended_next_state": "ERROR" if errors else recommended,
        "challenger_report": {
            "path": str(challenger_path),
            "status": challenger_status,
        },
        "review_decision": {
            "path": str(decision_path),
            "status": decision_status,
            "decision": review,
        },
        "artifacts": {
            "challenger_report": _artifact_summary(challenger_path),
            "review_decision": _artifact_summary(decision_path),
        },
        "errors": errors,
        "authority": {
            "mutates_latest_model": False,
            "automatic_champion_replacement": False,
            "latest_model_path": "models/latest_model.json",
            "promotion_authorized": False,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_model_review_cycle_markdown(report: Mapping[str, object]) -> str:
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    lines = [
        "# Model Review Cycle",
        "",
        f"Status: **{report.get('status') or 'ERROR'}**",
        f"Recommended next state: **{report.get('recommended_next_state') or 'ERROR'}**",
        f"Cycle date: `{report.get('cycle_date') or ''}`",
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
        lines.append("| none | Cycle report recorded without champion mutation. |")
    lines.extend(["", "Mutates latest model: `False`", "Live trading allowed: `False`", ""])
    return "\n".join(lines)


def _read_artifact(path: Path, name: str, errors: list[dict[str, object]]) -> Mapping[str, object] | None:
    try:
        return read_json_artifact(path)
    except FileNotFoundError:
        errors.append(_error(f"missing_{name}", f"{name} is missing: {path}"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(_error(f"invalid_{name}", f"invalid {name} JSON: {exc}"))
    return None


def _recommended_next_state(review: str, *, errors: list[dict[str, object]]) -> str:
    if review == "APPROVE_FOR_NEXT_PAPER_CYCLE":
        return "READY_FOR_NEXT_PAPER_CYCLE"
    if review == "REJECT":
        return "REJECTED_NO_PROMOTION"
    if review == "DEFER":
        return "DEFERRED"
    errors.append(_error("unsupported_review_decision", f"unsupported review decision: {review}"))
    return "ERROR"


def _artifact_summary(path: Path) -> dict[str, object]:
    payload: dict[str, object] = {"path": str(path), "sha256": None}
    if path.exists() and path.is_file():
        payload["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return payload


def _cycle_date(decision: Mapping[str, object], generated_at: str) -> str:
    for candidate in (decision.get("decision_date"), decision.get("generated_at"), generated_at):
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
        raise ModelReviewCycleOperationalError("model review cycle report must be a JSON object")
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
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
