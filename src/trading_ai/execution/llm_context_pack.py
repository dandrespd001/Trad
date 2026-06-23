"""Local read-only context pack for governed LLM paper workflows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.llm.factory import resolve_llm_model_route
from trading_ai.llm.model_policy import resolve_openai_model

SCHEMA_VERSION = "1.0"
DEFAULT_CYCLE_ROOT = "reports/tmp/paper_auto_cycle"
DEFAULT_OUTPUT_DIR = "reports/tmp/llm_context_pack"


class LlmContextPackOperationalError(RuntimeError):
    """Raised when the LLM context pack cannot be produced."""


@dataclass(frozen=True)
class LlmContextPackResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_llm_context_pack(
    *,
    as_of_date: str,
    cycle_root: str | Path = DEFAULT_CYCLE_ROOT,
    campaign_status: str | Path | None = None,
    performance_report: str | Path | None = None,
    phase_review: str | Path | None = None,
    training_cycle: str | Path | None = None,
    challenger_report: str | Path | None = None,
    shadow_plan: str | Path | None = None,
    shadow_scorecard: str | Path | None = None,
    paper_model_alias: str | Path | None = None,
    llm_model_alias: str | Path | None = None,
    evidence_index: str | Path | None = None,
    weekly_summary: str | Path | None = None,
    operator_status: str | Path,
    quality_report: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> LlmContextPackResult:
    generated = generated_at or _utc_now()
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "context_pack.json"
    markdown_path = output_root / "context_pack.md"
    payload = build_llm_context_pack(
        as_of_date=as_of_date,
        cycle_root=cycle_root,
        campaign_status=campaign_status,
        performance_report=performance_report,
        phase_review=phase_review,
        training_cycle=training_cycle,
        challenger_report=challenger_report,
        shadow_plan=shadow_plan,
        shadow_scorecard=shadow_scorecard,
        paper_model_alias=paper_model_alias,
        llm_model_alias=llm_model_alias,
        evidence_index=evidence_index,
        weekly_summary=weekly_summary,
        operator_status=operator_status,
        quality_report=quality_report,
        generated_at=generated,
    )
    write_json_artifact(payload, output_path)
    write_text_artifact(render_llm_context_pack_markdown(payload), markdown_path)
    status = str(payload.get("status") or "ERROR")
    return LlmContextPackResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def build_llm_context_pack(
    *,
    as_of_date: str,
    cycle_root: str | Path,
    campaign_status: str | Path | None,
    performance_report: str | Path | None,
    phase_review: str | Path | None,
    training_cycle: str | Path | None,
    challenger_report: str | Path | None,
    shadow_plan: str | Path | None,
    shadow_scorecard: str | Path | None,
    paper_model_alias: str | Path | None,
    llm_model_alias: str | Path | None,
    evidence_index: str | Path | None,
    weekly_summary: str | Path | None,
    operator_status: str | Path,
    quality_report: str | Path,
    generated_at: str,
) -> dict[str, object]:
    blockers: list[dict[str, object]] = []
    items: list[dict[str, object]] = []
    model_policy = resolve_openai_model(None)
    llm_route = resolve_llm_model_route(
        role="incident_runbook_assistant",
        default_model=str(model_policy.get("model") or ""),
        llm_model_alias=llm_model_alias,
        as_of_date=as_of_date,
    )
    if llm_route.get("route_state") == "BLOCKED":
        blockers.append(
            _blocker(
                "CRITICAL",
                "llm_model_alias_blocked",
                f"LLM model alias route blocked: {llm_route.get('reason')}",
                source_path=llm_model_alias,
            )
        )
    cycle_path = Path(cycle_root) / as_of_date / "cycle.json"
    cycle = _read_optional_json(cycle_path)
    if cycle is not None:
        items.append(_json_item("paper_auto_cycle", "cycle", cycle_path, cycle))
        blockers.extend(_artifact_blockers(cycle_path, cycle))
    else:
        items.append(_missing_item("paper_auto_cycle", "cycle", cycle_path))

    optional_artifacts: tuple[tuple[str, str, Path], ...] = tuple(
        item
        for item in (
            ("campaign_status", "campaign_status", Path(campaign_status)) if campaign_status is not None else None,
            ("performance_report", "performance_report", Path(performance_report))
            if performance_report is not None
            else None,
            ("phase_review", "phase_review", Path(phase_review)) if phase_review is not None else None,
            ("training_cycle", "training_cycle", Path(training_cycle)) if training_cycle is not None else None,
            ("challenger_report", "challenger_report", Path(challenger_report))
            if challenger_report is not None
            else None,
            ("shadow_plan", "shadow_plan", Path(shadow_plan)) if shadow_plan is not None else None,
            ("shadow_scorecard", "shadow_scorecard", Path(shadow_scorecard)) if shadow_scorecard is not None else None,
            ("paper_model_alias", "paper_model_alias", Path(paper_model_alias))
            if paper_model_alias is not None
            else None,
            ("llm_model_alias", "llm_model_alias", Path(llm_model_alias)) if llm_model_alias is not None else None,
            ("evidence_index", "evidence_index", Path(evidence_index)) if evidence_index is not None else None,
            ("weekly_summary", "weekly_summary", Path(weekly_summary)) if weekly_summary is not None else None,
        )
        if item is not None
    )
    for item_id, kind, path in (
        *optional_artifacts,
        ("operator_status", "operator_status", Path(operator_status)),
        ("strategy_quality", "strategy_quality", Path(quality_report)),
    ):
        payload = _read_optional_json(path)
        if payload is None:
            blockers.append(
                _blocker("ERROR", f"{item_id}_invalid", f"{item_id} artifact is missing or invalid", source_path=path)
            )
            items.append(_missing_item(item_id, kind, path))
            continue
        items.append(_json_item(item_id, kind, path, payload))
        blockers.extend(_artifact_blockers(path, payload))
        blockers.extend(_instruction_blockers(path, json.dumps(payload, sort_keys=True)))

    for item_id, path in _doc_paths():
        if path.exists():
            items.append({"id": item_id, "kind": "runbook", "path": str(path), "status": "PRESENT"})

    blockers = _dedupe_blockers(blockers)
    status = (
        "BLOCKED"
        if any(str(item.get("severity") or "").upper() == "CRITICAL" for item in blockers)
        else "ERROR"
        if blockers
        else "OK"
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": status,
        "items": items,
        "evidence_refs": _evidence_refs(items),
        "guardrail_results": _guardrail_results(blockers),
        "llm_model_route": llm_route,
        "model_policy": dict(model_policy),
        "blockers": blockers,
        "authority": {
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "live_trading_authorized": False,
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
    return _redact_payload(payload)


def render_llm_context_pack_markdown(payload: Mapping[str, object]) -> str:
    items = _object_list(payload.get("items"))
    blockers = _object_list(payload.get("blockers"))
    lines = [
        "# LLM Context Pack",
        "",
        f"Status: **{payload.get('status') or 'ERROR'}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        "",
        "| ID | Kind | Status | Path |",
        "| --- | --- | --- | --- |",
    ]
    for item in items:
        if isinstance(item, Mapping):
            lines.append(
                "| "
                f"`{_escape(item.get('id') or '')}` "
                f"| `{_escape(item.get('kind') or '')}` "
                f"| `{_escape(item.get('status') or '')}` "
                f"| `{_escape(item.get('path') or '')}` |"
            )
    lines.extend(["", "## Blockers", "", "| Code | Message |", "| --- | --- |"])
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(f"| `{_escape(blocker.get('code') or '')}` | {_escape(blocker.get('message') or '')} |")
    else:
        lines.append("| none | No context blockers. |")
    guardrails = _mapping(payload.get("guardrail_results"))
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            f"LLM authority: `{guardrails.get('llm_authority') or 'none'}`",
            f"Orders blocked: `{guardrails.get('orders_blocked') is True}`",
            f"Secret access blocked: `{guardrails.get('secret_access_blocked') is True}`",
            "",
            "Broker client built: `False`",
            "Credentials read: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _json_item(item_id: str, kind: str, path: Path, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": item_id,
        "kind": kind,
        "path": str(path),
        "status": str(payload.get("status") or payload.get("state") or "OK"),
        "as_of_date": str(payload.get("as_of_date") or ""),
    }


def _missing_item(item_id: str, kind: str, path: Path) -> dict[str, object]:
    return {"id": item_id, "kind": kind, "path": str(path), "status": "MISSING"}


def _artifact_blockers(path: Path, payload: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    safety = _mapping(payload.get("safety"))
    authority = _mapping(payload.get("authority"))
    if safety.get("credentials_read") is True:
        blockers.append(
            _blocker("CRITICAL", "credentials_read", "context artifact says credentials were read", source_path=path)
        )
    if safety.get("broker_client_built") is True:
        blockers.append(
            _blocker(
                "CRITICAL", "broker_client_built", "context artifact says broker client was built", source_path=path
            )
        )
    if safety.get("orders_submitted") is True:
        blockers.append(
            _blocker("CRITICAL", "orders_submitted", "context artifact says orders were submitted", source_path=path)
        )
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        blockers.append(
            _blocker(
                "CRITICAL",
                "live_trading_not_allowed",
                "context artifact attempts to authorize live trading",
                source_path=path,
            )
        )
    if authority.get("llm_authority") not in {None, "", "none"}:
        blockers.append(
            _blocker("CRITICAL", "llm_authority_not_none", "context artifact grants LLM authority", source_path=path)
        )
    return blockers


def _instruction_blockers(path: Path, text: str) -> list[dict[str, object]]:
    lowered = text.lower()
    checks = (
        (
            "live_trading_instruction",
            ("submit live order", "send live order", "live trading authorized", "live trading enabled"),
        ),
        (
            "order_submission_instruction",
            ("submit order", "submit live order", "send order", "send live order", "place order"),
        ),
        ("risk_change_instruction", ("change risk", "raise risk", "increase risk", "modify risk")),
        (
            "broker_access_instruction",
            ("build broker client", "call broker", "read broker credentials", "use alpaca credentials"),
        ),
        (
            "phase_bypass_instruction",
            (
                "bypass 60 sessions",
                "skip 60 sessions",
                "ignore 60 sessions",
                "ignore sixty sessions",
                "bypass sixty sessions",
            ),
        ),
        (
            "model_promotion_instruction",
            ("auto promote", "promote the model", "mutate latest_model.json", "replace champion", "automatic champion"),
        ),
        (
            "alias_activation_instruction",
            (
                "activate alias without scorecard",
                "enable paper alias without scorecard",
                "skip scorecard",
                "alias without approval",
            ),
        ),
        (
            "continuous_training_instruction",
            ("continuous training", "train continuously", "retrain every tick", "online learning without gate"),
        ),
        ("human_review_bypass_instruction", ("skip human review", "bypass human review", "without human review")),
        ("secret_access_instruction", ("read .env", "read secrets", "load secrets", "expose api key")),
    )
    blockers: list[dict[str, object]] = []
    for code, phrases in checks:
        if any(phrase in lowered for phrase in phrases):
            blockers.append(
                _blocker("CRITICAL", code, f"context contains disallowed instruction: {code}", source_path=path)
            )
    return blockers


def _doc_paths() -> tuple[tuple[str, Path], ...]:
    return (
        ("readme", Path("README.md")),
        ("paper_runbook", Path("docs/paper-real-runbook.md")),
        ("systems_guide", Path("docs/trading-bot-systems-guide.md")),
    )


def _evidence_refs(items: list[dict[str, object]]) -> dict[str, object]:
    refs: dict[str, object] = {}
    for item in items:
        item_id = item.get("id")
        path = item.get("path")
        if item_id not in {None, ""} and path not in {None, ""}:
            path_obj = Path(str(path))
            refs[str(item_id)] = {
                "path": str(path_obj),
                "present": path_obj.exists(),
                "sha256": _sha256(path_obj) if path_obj.exists() and path_obj.is_file() else None,
            }
    return refs


def _guardrail_results(blockers: list[dict[str, object]]) -> dict[str, object]:
    codes = {str(blocker.get("code") or "") for blocker in blockers}
    return {
        "llm_authority": "none",
        "orders_blocked": True,
        "risk_change_blocked": True,
        "broker_access_blocked": True,
        "phase_bypass_blocked": True,
        "auto_promotion_blocked": True,
        "continuous_training_blocked": True,
        "human_review_bypass_blocked": True,
        "secret_access_blocked": True,
        "openai_disabled_by_default": True,
        "blocked_codes": sorted(code for code in codes if code),
    }


def _blocker(severity: str, code: str, message: str, *, source_path: object = None) -> dict[str, object]:
    item: dict[str, object] = {"severity": severity, "code": code, "message": redact_secrets(message, env={})}
    if source_path not in {None, ""}:
        item["source_path"] = str(source_path)
    return item


def _dedupe_blockers(blockers: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for blocker in blockers:
        key = (str(blocker.get("code") or ""), str(blocker.get("source_path") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(blocker)
    return result


def _read_optional_json(path: str | Path) -> dict[str, object] | None:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(_redact_value(payload)))


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {redact_secrets(str(key), env={}): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, env={})
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
