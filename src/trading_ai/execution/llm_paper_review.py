"""Audited paper-ops review with optional research-only LLM explanation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from trading_ai.execution.paper_common import (
    paper_exit_code,
    redact_secrets,
    read_json_artifact,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.llm.openai_client import OpenAIResearchClient
from trading_ai.llm.factory import resolve_llm_model_route
from trading_ai.llm.model_policy import resolve_openai_model
from trading_ai.llm.schemas import validate_against_schema


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/llm_paper_review"

RECOMMEND_CONTINUE_OFFLINE = "CONTINUE_OFFLINE"
RECOMMEND_DEFER_MODEL = "DEFER_MODEL"
RECOMMEND_READY_FOR_PAPER_CONFIRMATION = "READY_FOR_PAPER_CONFIRMATION"
RECOMMEND_BLOCK = "BLOCK"


class LlmPaperReviewOperationalError(RuntimeError):
    """Raised when the LLM paper review cannot be written."""


@dataclass(frozen=True)
class LlmPaperReviewResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_llm_paper_review(
    *,
    as_of_date: str,
    readiness: str | Path,
    ops_check: str | Path,
    evidence_index: str | Path,
    performance: str | Path | None = None,
    challenger_report: str | Path | None = None,
    shadow_scorecard: str | Path | None = None,
    paper_model_alias: str | Path | None = None,
    llm_model_alias: str | Path | None = None,
    cycle_report: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    use_openai: bool = False,
    confirm_llm: bool = False,
    model: str | None = None,
    llm_client: OpenAIResearchClient | None = None,
    generated_at: str | None = None,
) -> LlmPaperReviewResult:
    report = build_llm_paper_review(
        as_of_date=as_of_date,
        readiness=readiness,
        ops_check=ops_check,
        evidence_index=evidence_index,
        performance=performance,
        challenger_report=challenger_report,
        shadow_scorecard=shadow_scorecard,
        paper_model_alias=paper_model_alias,
        llm_model_alias=llm_model_alias,
        cycle_report=cycle_report,
        use_openai=use_openai,
        confirm_llm=confirm_llm,
        model=model,
        llm_client=llm_client,
        generated_at=generated_at,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "llm_paper_review.json"
    markdown_path = output_root / "llm_paper_review.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_llm_paper_review_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return LlmPaperReviewResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_llm_paper_review(
    *,
    as_of_date: str,
    readiness: str | Path,
    ops_check: str | Path,
    evidence_index: str | Path,
    performance: str | Path | None = None,
    challenger_report: str | Path | None = None,
    shadow_scorecard: str | Path | None = None,
    paper_model_alias: str | Path | None = None,
    llm_model_alias: str | Path | None = None,
    cycle_report: str | Path | None = None,
    use_openai: bool = False,
    confirm_llm: bool = False,
    model: str | None = None,
    llm_client: OpenAIResearchClient | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    errors: list[dict[str, object]] = []
    model_policy = resolve_openai_model(model)
    resolved_model = str(model_policy.get("model") or "")
    sources = {
        "readiness": str(Path(readiness)),
        "ops_check": str(Path(ops_check)),
        "evidence_index": str(Path(evidence_index)),
        "performance": str(Path(performance)) if performance is not None else None,
        "challenger_report": str(Path(challenger_report)) if challenger_report is not None else None,
        "shadow_scorecard": str(Path(shadow_scorecard)) if shadow_scorecard is not None else None,
        "paper_model_alias": str(Path(paper_model_alias)) if paper_model_alias is not None else None,
        "llm_model_alias": str(Path(llm_model_alias)) if llm_model_alias is not None else None,
        "cycle_report": str(Path(cycle_report)) if cycle_report is not None else None,
    }
    artifacts = {
        "readiness": _read_required(readiness, "readiness", errors),
        "ops_check": _read_required(ops_check, "ops_check", errors),
        "evidence_index": _read_required(evidence_index, "evidence_index", errors),
        "performance": _read_optional(performance, "performance", errors),
        "challenger_report": _read_optional(challenger_report, "challenger_report", errors),
        "shadow_scorecard": _read_optional(shadow_scorecard, "shadow_scorecard", errors),
        "paper_model_alias": _read_optional(paper_model_alias, "paper_model_alias", errors),
        "llm_model_alias": _read_optional(llm_model_alias, "llm_model_alias", errors),
        "cycle_report": _read_optional(cycle_report, "cycle_report", errors),
    }
    llm_route = resolve_llm_model_route(
        role="paper_ops_reviewer",
        default_model=resolved_model,
        llm_model_alias=llm_model_alias,
        as_of_date=as_of_date,
    )
    effective_model = str(llm_route.get("active_model") or resolved_model)
    if model_policy.get("status") == "BLOCKED":
        review = _review_payload(
            operational_status="ERROR",
            recommendation=RECOMMEND_BLOCK,
            risks=["OpenAI model policy is invalid"],
            blockers=[_blocker("ERROR", str(model_policy.get("reason") or "invalid_model_policy"), "OpenAI model policy is invalid")],
            reasoning="The configured OpenAI model slug is invalid.",
            human_review_required=True,
        )
        return _report(
            as_of_date=as_of_date,
            generated_at=generated,
            status="ERROR",
            mode="openai" if use_openai else "deterministic",
            model=resolved_model if use_openai else None,
            sources=sources,
            artifacts=artifacts,
            review=review,
            errors=[_error(str(model_policy.get("reason") or "invalid_model_policy"), "OpenAI model policy is invalid")],
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
    if llm_route.get("route_state") == "BLOCKED":
        review = _review_payload(
            operational_status="BLOCKED",
            recommendation=RECOMMEND_BLOCK,
            risks=["LLM model alias route is blocked"],
            blockers=[_blocker("CRITICAL", "llm_model_alias_blocked", f"LLM model alias route blocked: {llm_route.get('reason')}")],
            reasoning="Explicit LLM model alias was invalid, expired, or unsafe; no fallback is allowed.",
            human_review_required=True,
        )
        return _report(
            as_of_date=as_of_date,
            generated_at=generated,
            status="BLOCKED",
            mode="openai" if use_openai else "deterministic",
            model=effective_model if use_openai else None,
            sources=sources,
            artifacts=artifacts,
            review=review,
            errors=[],
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
    if errors:
        review = _review_payload(
            operational_status="ERROR",
            recommendation=RECOMMEND_BLOCK,
            risks=["required paper operations evidence could not be read"],
            blockers=[_blocker("ERROR", "invalid_review_inputs", "required input artifact is missing or invalid")],
            reasoning="Required paper operations evidence is missing or invalid.",
            human_review_required=True,
        )
        return _report(
            as_of_date=as_of_date,
            generated_at=generated,
            status="ERROR",
            mode="openai" if use_openai else "deterministic",
            model=resolved_model if use_openai else None,
            sources=sources,
            artifacts=artifacts,
            review=review,
            errors=errors,
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
    review = _deterministic_review(artifacts=artifacts)
    mode = "deterministic"
    if use_openai:
        mode = "openai"
        if not confirm_llm:
            review = _review_payload(
                operational_status="ERROR",
                recommendation=RECOMMEND_BLOCK,
                risks=["OpenAI review mode was requested without explicit LLM confirmation"],
                blockers=[_blocker("ERROR", "missing_confirm_llm", "--use-openai requires --confirm-llm")],
                reasoning="Optional LLM review is disabled until the operator confirms the LLM-only audit step.",
                human_review_required=True,
            )
            return _report(
                as_of_date=as_of_date,
                generated_at=generated,
                status="ERROR",
                mode=mode,
                model=resolved_model,
                sources=sources,
                artifacts=artifacts,
                review=review,
                errors=[_error("missing_confirm_llm", "--use-openai requires --confirm-llm")],
                llm_model_route=llm_route,
                model_policy=model_policy,
            )
        try:
            client = llm_client or OpenAIResearchClient(model=effective_model)
            llm_result = client.create_structured_output(
                schema_name="PaperOpsReview",
                user_input=_llm_prompt_context(artifacts=artifacts),
                reasoning_effort="low",
                verbosity="low",
            )
            llm_review = dict(llm_result.data)
            llm_review["llm_authority"] = "none"
            validate_against_schema("PaperOpsReview", llm_review)
            review = _merge_llm_audit(deterministic=review, llm_review=llm_review)
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            review = _review_payload(
                operational_status="ERROR",
                recommendation=RECOMMEND_BLOCK,
                risks=["OpenAI review request failed"],
                blockers=[_blocker("ERROR", "llm_paper_review_failed", str(exc))],
                reasoning="The optional OpenAI paper review failed; no fallback is used for a requested OpenAI path.",
                human_review_required=True,
            )
            return _report(
                as_of_date=as_of_date,
                generated_at=generated,
                status="ERROR",
                mode=mode,
                model=effective_model,
                sources=sources,
                artifacts=artifacts,
                review=review,
                errors=[_error("llm_paper_review_failed", str(exc))],
                llm_model_route=llm_route,
                model_policy=model_policy,
            )
    status = _status_for_review(review)
    return _report(
        as_of_date=as_of_date,
        generated_at=generated,
        status=status,
        mode=mode,
        model=effective_model if use_openai else None,
        sources=sources,
        artifacts=artifacts,
        review=review,
        errors=[],
        llm_model_route=llm_route,
        model_policy=model_policy,
    )


def render_llm_paper_review_markdown(report: Mapping[str, object]) -> str:
    review = _mapping(report.get("review"))
    blockers = _object_list(review.get("blockers"))
    risks = _string_list(review.get("risks"))
    errors = _object_list(report.get("errors"))
    lines = [
        "# LLM Paper Review",
        "",
        f"Status: **{report.get('status') or 'ERROR'}**",
        f"Recommendation: **{review.get('recommendation') or RECOMMEND_BLOCK}**",
        f"LLM authority: `{review.get('llm_authority') or 'none'}`",
        f"As of date: `{report.get('as_of_date') or ''}`",
        f"Mode: `{report.get('mode') or 'deterministic'}`",
        "",
        "## Risks",
        "",
    ]
    if risks:
        lines.extend(f"- {_escape(risk)}" for risk in risks)
    else:
        lines.append("- none")
    lines.extend(["", "## Blockers", "", "| Severity | Code | Message |", "| --- | --- | --- |"])
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(
                    "| "
                    f"`{_escape(blocker.get('severity') or '')}` "
                    f"| `{_escape(blocker.get('code') or '')}` "
                    f"| {_escape(blocker.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | No blocking LLM-review findings. |")
    if errors:
        lines.extend(["", "## Errors", "", "| Code | Message |", "| --- | --- |"])
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(f"| `{_escape(error.get('code') or '')}` | {_escape(error.get('message') or '')} |")
    lines.extend(
        [
            "",
            "Broker client built: `False`",
            "Credentials read: `False`",
            "Orders submitted: `False`",
            "Live trading allowed: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _deterministic_review(*, artifacts: Mapping[str, object]) -> dict[str, object]:
    readiness = _payload(artifacts.get("readiness"))
    ops = _payload(artifacts.get("ops_check"))
    evidence = _payload(artifacts.get("evidence_index"))
    performance = _payload(artifacts.get("performance"))
    challenger = _payload(artifacts.get("challenger_report"))
    scorecard = _payload(artifacts.get("shadow_scorecard"))
    alias = _payload(artifacts.get("paper_model_alias"))
    cycle = _payload(artifacts.get("cycle_report"))
    blockers: list[dict[str, object]] = []
    risks: list[str] = []

    readiness_status = str(readiness.get("status") or "UNKNOWN").upper()
    ops_status = str(ops.get("status") or "UNKNOWN").upper()
    evidence_status = str(evidence.get("status") or "UNKNOWN").upper()
    if readiness_status != "READY" or readiness.get("ready_for_paper_daily") is not True:
        blockers.append(_blocker("CRITICAL", "readiness_not_ready", "readiness is not READY"))
    if ops_status in {"CRITICAL", "ERROR", "BLOCKED"}:
        blockers.append(_blocker("CRITICAL", "ops_check_blocking", f"paper ops check is {ops_status}"))
    if evidence_status == "ERROR":
        blockers.append(_blocker("ERROR", "evidence_index_error", "paper evidence index is ERROR"))
    blockers.extend(_safety_blockers(readiness, ops, evidence, performance, challenger, scorecard, alias, cycle))
    blockers.extend(_alias_blockers(scorecard=scorecard, alias=alias))

    ops_issue_codes = _issue_codes(ops)
    evidence_issue_codes = _issue_codes(evidence)
    if ops_status == "WARN" or evidence_status == "WARN":
        risks.extend(sorted(ops_issue_codes | evidence_issue_codes))
        risks.extend(_issue_messages(ops))
        risks.extend(_issue_messages(evidence))
    fills = _performance_fills(performance)
    if fills == 0:
        risks.append("paper performance has no broker-confirmed fills")
    model_deferred = _model_deferred(challenger=challenger, cycle=cycle)
    if model_deferred:
        blockers.append(_blocker("WARNING", "model_governance_deferred", "model governance is deferred or blocked"))

    if any(str(blocker.get("severity") or "").upper() in {"ERROR", "CRITICAL"} for blocker in blockers):
        recommendation = RECOMMEND_BLOCK
        reasoning = "Required paper operations evidence has a blocking condition."
    elif model_deferred:
        recommendation = RECOMMEND_DEFER_MODEL
        reasoning = "Paper can continue offline, but model challenger governance remains deferred."
    elif ops_status == "OK" and evidence_status in {"OK", "UNKNOWN"}:
        recommendation = RECOMMEND_READY_FOR_PAPER_CONFIRMATION
        reasoning = "Readiness and paper operations evidence are clean enough for human paper confirmation review."
    else:
        recommendation = RECOMMEND_CONTINUE_OFFLINE
        reasoning = "Paper operations have reviewable warnings; continue offline and collect broker-confirmed evidence."
    operational_status = "BLOCKED" if recommendation == RECOMMEND_BLOCK else (ops_status if ops_status in {"OK", "WARN"} else "UNKNOWN")
    return _review_payload(
        operational_status=operational_status,
        recommendation=recommendation,
        risks=_dedupe_strings(risks),
        blockers=_dedupe_blockers(blockers),
        reasoning=reasoning,
        human_review_required=True,
    )


def _merge_llm_audit(*, deterministic: Mapping[str, object], llm_review: Mapping[str, object]) -> dict[str, object]:
    merged = dict(deterministic)
    merged["llm_authority"] = "none"
    if deterministic.get("recommendation") == RECOMMEND_READY_FOR_PAPER_CONFIRMATION:
        merged["recommendation"] = RECOMMEND_READY_FOR_PAPER_CONFIRMATION
    validate_against_schema("PaperOpsReview", merged)
    return merged


def _report(
    *,
    as_of_date: str,
    generated_at: str,
    status: str,
    mode: str,
    model: str | None,
    sources: Mapping[str, object],
    artifacts: Mapping[str, object],
    review: Mapping[str, object],
    errors: list[dict[str, object]],
    llm_model_route: Mapping[str, object] | None = None,
    model_policy: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": status,
        "mode": mode,
        "model": model,
        "sources": dict(sources),
        "artifacts": {key: _artifact_summary(value) for key, value in artifacts.items()},
        "llm_model_route": dict(llm_model_route or {}),
        "model_policy": dict(model_policy or {}),
        "review": dict(review),
        "errors": errors,
        "authority": {
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "live_approval_authority": False,
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


def _review_payload(
    *,
    operational_status: str,
    recommendation: str,
    risks: Iterable[str],
    blockers: Iterable[Mapping[str, object]],
    reasoning: str,
    human_review_required: bool,
) -> dict[str, object]:
    payload = {
        "operational_status": operational_status,
        "risks": list(risks),
        "blockers": [dict(blocker) for blocker in blockers],
        "recommendation": recommendation,
        "reasoning": reasoning,
        "human_review_required": human_review_required,
        "llm_authority": "none",
    }
    validate_against_schema("PaperOpsReview", payload)
    return payload


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
    payload = _payload(value)
    return {
        "present": bool(artifact.get("present")),
        "path": artifact.get("path"),
        "status": payload.get("status") or payload.get("decision") or payload.get("recommended_next_state") or "UNKNOWN",
    }


def _payload(value: object) -> Mapping[str, object]:
    artifact = _mapping(value)
    return _mapping(artifact.get("payload"))


def _status_for_review(review: Mapping[str, object]) -> str:
    recommendation = str(review.get("recommendation") or RECOMMEND_BLOCK)
    if recommendation == RECOMMEND_BLOCK:
        return "BLOCKED"
    if recommendation in {RECOMMEND_CONTINUE_OFFLINE, RECOMMEND_DEFER_MODEL}:
        return "WARN"
    return "OK"


def _safety_blockers(*payloads: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for payload in payloads:
        safety = _mapping(payload.get("safety"))
        if bool(safety.get("credentials_read")):
            blockers.append(_blocker("CRITICAL", "credentials_were_read", "an input artifact reports credentials were read"))
        if bool(safety.get("orders_submitted")):
            blockers.append(_blocker("CRITICAL", "orders_already_submitted", "an input artifact reports orders were submitted"))
        if bool(safety.get("live_trading_allowed")) or bool(safety.get("live_trading_authorized")):
            blockers.append(_blocker("CRITICAL", "live_trading_not_allowed", "an input artifact reports live trading enabled"))
    return blockers


def _alias_blockers(*, scorecard: Mapping[str, object], alias: Mapping[str, object]) -> list[dict[str, object]]:
    if not alias:
        return []
    blockers: list[dict[str, object]] = []
    if str(alias.get("alias_state") or "").upper() == "ACTIVE_PAPER_ALIAS":
        if str(scorecard.get("scorecard_state") or "").upper() != "READY_FOR_PAPER_ALIAS":
            blockers.append(_blocker("CRITICAL", "alias_without_ready_scorecard", "paper alias requires READY_FOR_PAPER_ALIAS scorecard evidence"))
        latest = _mapping(alias.get("latest_model"))
        if latest.get("mutated") is True:
            blockers.append(_blocker("CRITICAL", "latest_model_mutated", "paper alias must not mutate latest_model"))
    return blockers


def _model_deferred(*, challenger: Mapping[str, object], cycle: Mapping[str, object]) -> bool:
    challenger_status = str(challenger.get("status") or "").upper()
    cycle_state = str(cycle.get("recommended_next_state") or cycle.get("decision") or "").upper()
    return challenger_status == "BLOCKED" or cycle_state == "DEFERRED"


def _performance_fills(performance: Mapping[str, object]) -> int | None:
    if not performance:
        return None
    metrics = _mapping(performance.get("paper_metrics"))
    try:
        return int(float(metrics.get("fills") or 0))
    except (TypeError, ValueError):
        return None


def _llm_prompt_context(*, artifacts: Mapping[str, object]) -> str:
    summary = {key: _artifact_summary(value) for key, value in artifacts.items()}
    return json.dumps(summary, sort_keys=True)


def _issue_codes(payload: Mapping[str, object]) -> set[str]:
    codes: set[str] = set()
    for issue in _object_list(payload.get("issues")):
        if isinstance(issue, Mapping) and issue.get("code") not in {None, ""}:
            codes.add(str(issue.get("code")))
    return codes


def _issue_messages(payload: Mapping[str, object]) -> list[str]:
    messages: list[str] = []
    for issue in _object_list(payload.get("issues")):
        if isinstance(issue, Mapping) and issue.get("message") not in {None, ""}:
            messages.append(str(issue.get("message")))
    return messages


def _blocker(severity: str, code: str, message: str) -> dict[str, object]:
    return {"severity": severity, "code": code, "message": message}


def _error(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message}


def _dedupe_blockers(blockers: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for blocker in blockers:
        normalized = dict(blocker)
        key = (str(normalized.get("severity") or ""), str(normalized.get("code") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise LlmPaperReviewOperationalError("LLM paper review must be a JSON object")
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


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
