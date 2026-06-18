"""Governed shadow LLM signal proposals for paper-only arbitration."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from trading_ai.data.io import read_records
from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.llm.openai_client import LLMGuardrailError, OpenAIResearchClient
from trading_ai.llm.factory import resolve_llm_model_route
from trading_ai.llm.model_policy import resolve_openai_model
from trading_ai.llm.schemas import validate_against_schema


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/llm_signal_proposals"


class LLMSignalProposalsOperationalError(RuntimeError):
    """Raised when LLM signal proposals cannot be written."""


@dataclass(frozen=True)
class LLMSignalProposalsResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_llm_signal_proposals(
    *,
    as_of_date: str,
    readiness: str | Path,
    features: str | Path,
    model_signals: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    use_openai: bool = False,
    confirm_llm: bool = False,
    context_digest: str | Path | None = None,
    llm_model_alias: str | Path | None = None,
    model: str | None = None,
    generated_at: str | None = None,
) -> LLMSignalProposalsResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "llm_signal_proposals.json"
    markdown_path = output_root / "llm_signal_proposals.md"
    input_hashes = _input_hashes(readiness, features, model_signals, context_digest=context_digest)
    model_policy = resolve_openai_model(model)
    if model_policy.get("status") == "BLOCKED":
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=_sources(readiness, features, model_signals, context_digest=context_digest),
            input_hashes=input_hashes,
            errors=[_error(str(model_policy.get("reason") or "invalid_model_policy"), "OpenAI model policy could not resolve a safe model")],
            use_openai=use_openai,
            model=str(model_policy.get("model") or ""),
            model_policy=model_policy,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)
    resolved_model = str(model_policy.get("model") or "")
    llm_route = resolve_llm_model_route(
        role="signal_proposal_auditor",
        default_model=resolved_model,
        llm_model_alias=llm_model_alias,
        as_of_date=as_of_date,
    )
    effective_model = str(llm_route.get("active_model") or resolved_model)

    try:
        readiness_payload = read_json_artifact(readiness)
        signals_payload = read_json_artifact(model_signals)
        feature_rows = read_records(features)
        context_payload = read_json_artifact(context_digest) if context_digest is not None else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=_sources(readiness, features, model_signals, context_digest=context_digest),
            input_hashes=input_hashes,
            errors=[_error("invalid_input_artifact", str(exc))],
            use_openai=use_openai,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    if llm_route.get("route_state") == "BLOCKED":
        payload = _report(
            as_of_date=as_of_date,
            generated_at=generated_at,
            status="BLOCKED",
            proposals=[],
            errors=[_error("llm_model_alias_blocked", f"LLM model alias route blocked: {llm_route.get('reason')}")],
            sources=_sources(readiness, features, model_signals, context_digest=context_digest),
            input_hashes=input_hashes,
            context_digest=context_payload,
            use_openai=use_openai,
            model=effective_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    if use_openai and not confirm_llm:
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=_sources(readiness, features, model_signals, context_digest=context_digest),
            input_hashes=input_hashes,
            errors=[_error("missing_confirm_llm", "--use-openai requires --confirm-llm")],
            use_openai=True,
            readiness=readiness_payload,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    errors = _readiness_errors(readiness_payload)
    if errors:
        payload = _report(
            as_of_date=as_of_date,
            generated_at=generated_at,
            status="BLOCKED",
            proposals=[],
            errors=errors,
            sources=_sources(readiness, features, model_signals, context_digest=context_digest),
            input_hashes=input_hashes,
            context_digest=context_payload,
            use_openai=use_openai,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    signals = _signal_list(signals_payload)
    prompt_traces: list[dict[str, object]] = []
    try:
        if use_openai:
            proposals, prompt_traces = _openai_proposals(signals, feature_rows=feature_rows, model=effective_model)
        else:
            proposals = _deterministic_proposals(signals, feature_rows=feature_rows)
    except (LLMGuardrailError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=_sources(readiness, features, model_signals, context_digest=context_digest),
            input_hashes=input_hashes,
            errors=[_error("llm_signal_proposal_failed", str(exc))],
            use_openai=use_openai,
            readiness=readiness_payload,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    payload = _report(
        as_of_date=as_of_date,
        generated_at=generated_at,
        status="OK",
        proposals=proposals,
        errors=[],
        sources=_sources(readiness, features, model_signals, context_digest=context_digest),
        input_hashes=input_hashes,
        context_digest=context_payload,
        use_openai=use_openai,
        model=effective_model,
        prompt_traces=prompt_traces,
        llm_model_route=llm_route,
        model_policy=model_policy,
    )
    return _write_result(payload, output_path=output_path, markdown_path=markdown_path)


def render_llm_signal_proposals_markdown(payload: Mapping[str, object]) -> str:
    proposals = _object_list(payload.get("proposals"))
    errors = _object_list(payload.get("errors"))
    lines = [
        "# LLM Signal Proposals",
        "",
        f"Status: **{payload.get('status') or 'ERROR'}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"OpenAI used: `{payload.get('use_openai') is True}`",
        "",
        "## Proposals",
        "",
        "| Symbol | Action | Confidence | LLM authority |",
        "| --- | --- | --- | --- |",
    ]
    if proposals:
        for proposal in proposals:
            if not isinstance(proposal, Mapping):
                continue
            lines.append(
                "| "
                f"`{_escape(proposal.get('symbol') or '')}` "
                f"| `{_escape(proposal.get('action') or '')}` "
                f"| `{_escape(proposal.get('confidence') or 0)}` "
                f"| `{_escape(proposal.get('llm_authority') or 'none')}` |"
            )
    else:
        lines.append("| none | hold | 0 | none |")
    lines.extend(["", "## Errors", "", "| Code | Message |", "| --- | --- |"])
    if errors:
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(f"| `{_escape(error.get('code') or '')}` | {_escape(error.get('message') or '')} |")
    else:
        lines.append("| none | No proposal errors. |")
    lines.extend(["", "LLM authority: `none`", "Broker client built: `False`", "Credentials read: `False`", ""])
    return "\n".join(lines)


def _deterministic_proposals(
    signals: Iterable[Mapping[str, object]],
    *,
    feature_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    latest_features = _latest_feature_rows(feature_rows)
    proposals: list[dict[str, object]] = []
    for signal in signals:
        symbol = str(signal.get("symbol") or "").upper()
        if not symbol:
            continue
        action = "buy" if str(signal.get("action") or "").lower() == "buy" else "hold"
        confidence = _bounded_float(signal.get("probability"), default=0.0)
        timestamp = str(signal.get("timestamp") or "")
        evidence_refs = [f"model_signal:{symbol}:{timestamp}"]
        if symbol in latest_features:
            evidence_refs.append(f"feature_row:{symbol}:{latest_features[symbol].get('timestamp')}")
        proposal = {
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "thesis": (
                "Shadow proposal mirrors the deterministic baseline buy signal."
                if action == "buy"
                else "Shadow proposal holds because the deterministic baseline is not a buy."
            ),
            "risk_notes": [
                "paper-only shadow proposal",
                "not authorized to submit orders or change risk",
            ],
            "evidence_refs": evidence_refs,
            "llm_authority": "none",
        }
        validate_against_schema("LLMSignalProposal", proposal)
        proposals.append(proposal)
    return proposals


def _openai_proposals(
    signals: Iterable[Mapping[str, object]],
    *,
    feature_rows: list[dict[str, object]],
    model: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    client = OpenAIResearchClient(model=model)
    proposals: list[dict[str, object]] = []
    prompt_traces: list[dict[str, object]] = []
    latest_features = _latest_feature_rows(feature_rows)
    for signal in signals:
        symbol = str(signal.get("symbol") or "").upper()
        prompt = (
            "Create one paper-only shadow signal proposal. "
            "The proposal has no broker, credential, risk-limit, or execution authority. "
            f"Model signal: {json.dumps(dict(signal), sort_keys=True)}. "
            f"Latest features: {json.dumps(dict(latest_features.get(symbol, {})), sort_keys=True)}."
        )
        result = client.create_structured_output(schema_name="LLMSignalProposal", user_input=prompt)
        proposal = dict(result.data)
        proposal["symbol"] = str(proposal.get("symbol") or symbol).upper()
        proposal["llm_authority"] = "none"
        validate_against_schema("LLMSignalProposal", proposal)
        proposals.append(proposal)
        prompt_traces.append(
            {
                "symbol": symbol,
                "schema_name": "LLMSignalProposal",
                "model": model,
                "prompt_hash": result.prompt_hash,
                "prompt_cache_key": result.prompt_cache_key,
            }
        )
    return proposals, prompt_traces


def _report(
    *,
    as_of_date: str,
    generated_at: str | None,
    status: str,
    proposals: list[dict[str, object]],
    errors: list[dict[str, object]],
    sources: Mapping[str, object],
    input_hashes: Mapping[str, object],
    use_openai: bool,
    context_digest: Mapping[str, object] | None = None,
    model: str | None = None,
    prompt_traces: list[dict[str, object]] | None = None,
    llm_model_route: Mapping[str, object] | None = None,
    model_policy: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return _redact_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at or _utc_now(),
            "as_of_date": as_of_date,
            "status": status,
            "use_openai": use_openai,
            "model": model if use_openai else None,
            "model_policy": dict(model_policy or {}),
            "proposals": proposals,
            "errors": errors,
            "sources": dict(sources),
            "input_hashes": dict(input_hashes),
            "llm_model_route": dict(llm_model_route or {}),
            "prompt_traces": [dict(trace) for trace in prompt_traces or []],
            "context_digest": _context_summary(context_digest),
            "authority": {
                "llm_authority": "none",
                "orders_submitted": False,
                "risk_changed": False,
                "live_trading_authorized": False,
            },
            "safety": _safety(),
        }
    )


def _error_payload(
    *,
    as_of_date: str,
    generated_at: str | None,
    sources: Mapping[str, object],
    errors: list[dict[str, object]],
    use_openai: bool,
    input_hashes: Mapping[str, object] | None = None,
    readiness: Mapping[str, object] | None = None,
    model: str | None = None,
    llm_model_route: Mapping[str, object] | None = None,
    model_policy: Mapping[str, object] | None = None,
) -> dict[str, object]:
    status = "ERROR"
    if readiness is not None and str(readiness.get("status") or "").upper() == "BLOCKED":
        status = "BLOCKED"
    return _report(
        as_of_date=as_of_date,
        generated_at=generated_at,
        status=status,
        proposals=[],
        errors=errors,
        sources=sources,
        input_hashes=input_hashes or {},
        use_openai=use_openai,
        model=model,
        context_digest=None,
        llm_model_route=llm_model_route,
        model_policy=model_policy,
    )


def _write_result(payload: dict[str, object], *, output_path: Path, markdown_path: Path) -> LLMSignalProposalsResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(render_llm_signal_proposals_markdown(payload), markdown_path)
    status = str(payload.get("status") or "ERROR")
    return LLMSignalProposalsResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _readiness_errors(readiness: Mapping[str, object]) -> list[dict[str, object]]:
    if str(readiness.get("status") or "").upper() == "READY" and readiness.get("ready_for_paper_daily") is True:
        return []
    return [_error("readiness_not_ready", "readiness must be READY before proposing signals")]


def _signal_list(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    signals = payload.get("signals")
    if isinstance(signals, list):
        return [signal for signal in signals if isinstance(signal, Mapping)]
    selected = payload.get("selected_signal")
    if isinstance(selected, Mapping):
        return [selected]
    return []


def _latest_feature_rows(rows: Iterable[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    latest: dict[str, Mapping[str, object]] = {}
    for row in sorted(rows, key=lambda item: (str(item.get("timestamp", "")), str(item.get("symbol", "")).upper())):
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            latest[symbol] = row
    return latest


def _sources(
    readiness: str | Path,
    features: str | Path,
    model_signals: str | Path,
    *,
    context_digest: str | Path | None = None,
) -> dict[str, object]:
    sources: dict[str, object] = {
        "readiness": str(Path(readiness)),
        "features": str(Path(features)),
        "model_signals": str(Path(model_signals)),
    }
    if context_digest is not None:
        sources["context_digest"] = str(Path(context_digest))
    return sources


def _input_hashes(
    readiness: str | Path,
    features: str | Path,
    model_signals: str | Path,
    *,
    context_digest: str | Path | None = None,
) -> dict[str, object]:
    hashes = {
        "readiness": _source_hash(readiness),
        "features": _source_hash(features),
        "model_signals": _source_hash(model_signals),
    }
    if context_digest is not None:
        hashes["context_digest"] = _source_hash(context_digest)
    return hashes


def _source_hash(path: str | Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _context_summary(payload: Mapping[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    items = payload.get("items")
    return {
        "status": str(payload.get("status") or ""),
        "item_count": len(items) if isinstance(items, list) else 0,
        "llm_authority": _mapping(payload.get("authority")).get("llm_authority") or "none",
    }


def _safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
    }


def _error(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": redact_secrets(message, env={})}


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise LLMSignalProposalsOperationalError("LLM signal proposals must be a JSON object")
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


def _bounded_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
