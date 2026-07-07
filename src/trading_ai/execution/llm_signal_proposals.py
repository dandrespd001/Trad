"""Governed shadow LLM signal proposals for paper-only arbitration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.data.io import read_records
from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_payload,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.llm.factory import resolve_llm_model_route
from trading_ai.llm.model_policy import resolve_openai_model
from trading_ai.llm.openai_client import LLMGuardrailError, OpenAIResearchClient
from trading_ai.llm.schemas import validate_against_schema, validate_llm_authority

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/llm_signal_proposals"
DEFAULT_PROMPT_VERSION = "signal_proposal_auditor:v1"
DETERMINISTIC_MODEL_ID = "deterministic-shadow"
ENTRY_ACTIONS = {"buy", "hold", "no_action"}
MANAGEMENT_ACTIONS = {"close", "reduce", "tighten_stop", "update_take_profit"}
# Actions that represent an actual trading decision and therefore must be grounded
# in cited indicators whenever a citable vocabulary is available. Hold/no_action
# proposals make no claim, so they are exempt from the "missing evidence" rule
# (they are still subject to the unknown/malformed/unverifiable anti-hallucination
# rules below, since even a hold proposal must not cite a fabricated indicator).
ACTIONABLE_EVIDENCE_ACTIONS = {"buy"} | MANAGEMENT_ACTIONS
MAX_INDICATOR_EVIDENCE = 4


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
    ai_features: str | Path | None = None,
    forecast_features: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    use_openai: bool = False,
    confirm_llm: bool = False,
    context_digest: str | Path | None = None,
    llm_model_alias: str | Path | None = None,
    model: str | None = None,
    available_indicators: Iterable[str] | None = None,
    generated_at: str | None = None,
) -> LLMSignalProposalsResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "llm_signal_proposals.json"
    markdown_path = output_root / "llm_signal_proposals.md"
    indicator_vocabulary = _normalize_indicator_vocabulary(available_indicators)
    sources = _sources(
        readiness,
        features,
        model_signals,
        context_digest=context_digest,
        ai_features=ai_features,
        forecast_features=forecast_features,
    )
    input_hashes = _input_hashes(
        readiness,
        features,
        model_signals,
        context_digest=context_digest,
        ai_features=ai_features,
        forecast_features=forecast_features,
    )
    model_policy = resolve_openai_model(model)
    if model_policy.get("status") == "BLOCKED":
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=sources,
            input_hashes=input_hashes,
            errors=[
                _error(
                    str(model_policy.get("reason") or "invalid_model_policy"),
                    "OpenAI model policy could not resolve a safe model",
                )
            ],
            use_openai=use_openai,
            model=str(model_policy.get("model") or ""),
            model_policy=model_policy,
            indicator_vocabulary=indicator_vocabulary,
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
            sources=sources,
            input_hashes=input_hashes,
            errors=[_error("invalid_input_artifact", str(exc))],
            use_openai=use_openai,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
            indicator_vocabulary=indicator_vocabulary,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    if llm_route.get("route_state") == "BLOCKED":
        payload = _report(
            as_of_date=as_of_date,
            generated_at=generated_at,
            status="BLOCKED",
            proposals=[],
            errors=[_error("llm_model_alias_blocked", f"LLM model alias route blocked: {llm_route.get('reason')}")],
            sources=sources,
            input_hashes=input_hashes,
            context_digest=context_payload,
            use_openai=use_openai,
            model=effective_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
            indicator_vocabulary=indicator_vocabulary,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    if use_openai and not confirm_llm:
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=sources,
            input_hashes=input_hashes,
            errors=[_error("missing_confirm_llm", "--use-openai requires --confirm-llm")],
            use_openai=True,
            readiness=readiness_payload,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
            indicator_vocabulary=indicator_vocabulary,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)
    if use_openai:
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=sources,
            input_hashes=input_hashes,
            errors=[_error("external_llm_api_disabled", "--use-openai is disabled; use local LLM commands")],
            use_openai=True,
            readiness=readiness_payload,
            model=None,
            llm_model_route=llm_route,
            model_policy=model_policy,
            indicator_vocabulary=indicator_vocabulary,
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
            sources=sources,
            input_hashes=input_hashes,
            context_digest=context_payload,
            use_openai=use_openai,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
            indicator_vocabulary=indicator_vocabulary,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    signals = _signal_list(signals_payload)
    prompt_traces: list[dict[str, object]] = []
    try:
        if use_openai:
            proposals, prompt_traces = _openai_proposals(
                signals,
                feature_rows=feature_rows,
                model=effective_model,
                input_hashes=input_hashes,
                available_indicators=indicator_vocabulary,
            )
        else:
            proposals = _deterministic_proposals(
                signals,
                feature_rows=feature_rows,
                input_hashes=input_hashes,
                available_indicators=indicator_vocabulary,
            )
    except (LLMGuardrailError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        payload = _error_payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            sources=sources,
            input_hashes=input_hashes,
            errors=[_error("llm_signal_proposal_failed", str(exc))],
            use_openai=use_openai,
            readiness=readiness_payload,
            model=resolved_model,
            llm_model_route=llm_route,
            model_policy=model_policy,
            indicator_vocabulary=indicator_vocabulary,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    payload = _report(
        as_of_date=as_of_date,
        generated_at=generated_at,
        status="OK",
        proposals=proposals,
        errors=[],
        sources=sources,
        input_hashes=input_hashes,
        context_digest=context_payload,
        use_openai=use_openai,
        model=effective_model,
        prompt_traces=prompt_traces,
        llm_model_route=llm_route,
        indicator_vocabulary=indicator_vocabulary,
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
        f"OpenAI used: `{payload.get('external_llm_used') is True}`",
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
    input_hashes: Mapping[str, object],
    available_indicators: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    latest_features = _latest_feature_rows(feature_rows)
    proposals: list[dict[str, object]] = []
    for signal in signals:
        symbol = str(signal.get("symbol") or "").upper()
        if not symbol:
            continue
        action = _proposal_action(signal.get("action"))
        confidence = _bounded_float(signal.get("probability"), default=0.0)
        timestamp = str(signal.get("timestamp") or "")
        evidence_refs = [f"model_signal:{symbol}:{timestamp}"]
        if symbol in latest_features:
            evidence_refs.append(f"feature_row:{symbol}:{latest_features[symbol].get('timestamp')}")
        proposal = {
            "proposal_kind": _proposal_kind(action),
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "time_horizon": str(signal.get("time_horizon") or "1d"),
            "thesis": (
                "Shadow proposal mirrors the deterministic baseline buy signal."
                if action == "buy"
                else "Shadow proposal mirrors deterministic position-management guidance."
                if action in MANAGEMENT_ACTIONS
                else "Shadow proposal holds because the deterministic baseline is not a buy."
            ),
            "risk_notes": [
                "paper-only shadow proposal",
                "not authorized to submit orders or change risk",
            ],
            "evidence_refs": evidence_refs,
            "model_id": DETERMINISTIC_MODEL_ID,
            "prompt_version": DEFAULT_PROMPT_VERSION,
            "input_hashes": dict(input_hashes),
            "llm_authority": "none",
            "indicator_evidence": _auto_indicator_evidence(
                action, latest_features.get(symbol), available_indicators=available_indicators
            ),
        }
        proposal = _apply_indicator_evidence_gate(proposal, available_indicators=available_indicators)
        validate_against_schema("LLMSignalProposal", proposal)
        proposals.append(proposal)
    return proposals


def _auto_indicator_evidence(
    action: str,
    feature_row: Mapping[str, object] | None,
    *,
    available_indicators: frozenset[str],
) -> list[str]:
    """Ground the deterministic shadow proposal in real, present indicator values.

    Only names that are BOTH in the caller-supplied vocabulary AND present as a
    real (non-null) value in the latest feature row for the symbol are cited —
    never fabricated. If nothing qualifies, evidence stays empty and the gate
    below will degrade actionable proposals rather than let them stand uncited.
    """
    if not available_indicators or action not in ACTIONABLE_EVIDENCE_ACTIONS or feature_row is None:
        return []
    cited: list[str] = []
    for name in sorted(available_indicators):
        if feature_row.get(name) in (None, ""):
            continue
        cited.append(name)
        if len(cited) >= MAX_INDICATOR_EVIDENCE:
            break
    return cited


def _openai_proposals(
    signals: Iterable[Mapping[str, object]],
    *,
    feature_rows: list[dict[str, object]],
    model: str,
    input_hashes: Mapping[str, object],
    available_indicators: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    client = OpenAIResearchClient(model=model)
    proposals: list[dict[str, object]] = []
    prompt_traces: list[dict[str, object]] = []
    latest_features = _latest_feature_rows(feature_rows)
    if available_indicators:
        vocabulary_clause = (
            "You MUST include \"indicator_evidence\": a list of 2 to 4 indicator names, "
            "chosen ONLY from this exact vocabulary, that support your action: "
            f"{sorted(available_indicators)}. Never invent or cite a name outside this list; "
            "doing so will cause the proposal to be discarded."
        )
    else:
        vocabulary_clause = (
            "No indicator vocabulary is available for this context pack, so you MUST set "
            '"indicator_evidence" to an empty list ([]).'
        )
    for signal in signals:
        symbol = str(signal.get("symbol") or "").upper()
        prompt = (
            "Create one paper-only shadow signal proposal. "
            "The proposal has no broker, credential, risk-limit, or execution authority. "
            "Return proposal_kind, action, time_horizon, model_id, prompt_version, input_hashes, "
            "and llm_authority='none'. Position-management actions are advisory only. "
            f"{vocabulary_clause} "
            f"Model signal: {json.dumps(dict(signal), sort_keys=True)}. "
            f"Latest features: {json.dumps(dict(latest_features.get(symbol, {})), sort_keys=True)}."
        )
        result = client.create_structured_output(schema_name="LLMSignalProposal", user_input=prompt)
        proposal = dict(result.data)
        # Security gate: validate authority BEFORE using any field of the response.
        validate_llm_authority(proposal)
        proposal["symbol"] = str(proposal.get("symbol") or symbol).upper()
        proposal["action"] = _proposal_action(proposal.get("action"))
        proposal["proposal_kind"] = str(proposal.get("proposal_kind") or _proposal_kind(str(proposal["action"])))
        proposal["time_horizon"] = str(proposal.get("time_horizon") or "1d")
        proposal["model_id"] = str(proposal.get("model_id") or model)
        proposal["prompt_version"] = str(proposal.get("prompt_version") or DEFAULT_PROMPT_VERSION)
        proposal["input_hashes"] = dict(input_hashes)
        proposal["llm_authority"] = "none"
        proposal = _apply_indicator_evidence_gate(proposal, available_indicators=available_indicators)
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
    external_llm_requested: bool | None = None,
    external_llm_used: bool = False,
    indicator_vocabulary: Iterable[str] | None = None,
) -> dict[str, object]:
    llm_requested = use_openai if external_llm_requested is None else external_llm_requested
    return _redact_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at or _utc_now(),
            "as_of_date": as_of_date,
            "status": status,
            "use_openai": external_llm_used,
            "external_llm_requested": llm_requested,
            "external_llm_used": external_llm_used,
            "model": model if external_llm_used else None,
            "model_policy": dict(model_policy or {}),
            "proposals": proposals,
            "errors": errors,
            "sources": dict(sources),
            "input_hashes": dict(input_hashes),
            "llm_model_route": dict(llm_model_route or {}),
            "prompt_traces": [dict(trace) for trace in prompt_traces or []],
            "context_digest": _context_summary(context_digest),
            "indicator_vocabulary": sorted(_normalize_indicator_vocabulary(indicator_vocabulary)),
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
    indicator_vocabulary: Iterable[str] | None = None,
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
        indicator_vocabulary=indicator_vocabulary,
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
    ai_features: str | Path | None = None,
    forecast_features: str | Path | None = None,
) -> dict[str, object]:
    sources: dict[str, object] = {
        "readiness": str(Path(readiness)),
        "features": str(Path(features)),
        "model_signals": str(Path(model_signals)),
    }
    if ai_features is not None:
        sources["ai_features"] = str(Path(ai_features))
    if forecast_features is not None:
        sources["forecast_features"] = str(Path(forecast_features))
    if context_digest is not None:
        sources["context_digest"] = str(Path(context_digest))
    return sources


def _input_hashes(
    readiness: str | Path,
    features: str | Path,
    model_signals: str | Path,
    *,
    context_digest: str | Path | None = None,
    ai_features: str | Path | None = None,
    forecast_features: str | Path | None = None,
) -> dict[str, object]:
    hashes: dict[str, object] = {
        "readiness": _source_hash(readiness),
        "features": _source_hash(features),
        "model_signals": _source_hash(model_signals),
    }
    if ai_features is not None:
        hashes["ai_features"] = _source_hash(ai_features)
    if forecast_features is not None:
        hashes["forecast_features"] = _source_hash(forecast_features)
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
    redacted = redact_payload(value, env={})
    if not isinstance(redacted, dict):
        raise LLMSignalProposalsOperationalError("LLM signal proposals must be a JSON object")
    return redacted


def _bounded_float(value: object, *, default: float) -> float:
    parsed = _float_value(value, default=default)
    return max(0.0, min(1.0, parsed))


def _proposal_action(value: object) -> str:
    action = str(value or "").lower()
    if action in ENTRY_ACTIONS or action in MANAGEMENT_ACTIONS:
        return action
    return "hold"


def _proposal_kind(action: str) -> str:
    if action in MANAGEMENT_ACTIONS:
        return "position_management"
    return "entry"


def _normalize_indicator_vocabulary(available_indicators: Iterable[str] | None) -> frozenset[str]:
    if not available_indicators:
        return frozenset()
    return frozenset(str(name) for name in available_indicators if str(name))


def _apply_indicator_evidence_gate(
    proposal: dict[str, object], *, available_indicators: frozenset[str]
) -> dict[str, object]:
    """Deterministically validate indicator_evidence citations (anti-hallucination gate).

    Runs in the same place action/proposal_kind are already normalized, right before
    schema validation. Any citation that cannot be trusted degrades the proposal to
    action="no_action" (fail-closed) rather than letting an unverified claim through:

      - indicator_evidence_malformed: more than 4 entries, or a non-string entry.
      - indicator_evidence_unverifiable: no vocabulary was supplied (old-style caller)
        but the proposal still cites something — cannot be checked, so it is untrusted.
      - indicator_evidence_unknown: a cited name is not in the supplied vocabulary
        (i.e. hallucinated) — invalidates the WHOLE proposal, not just that name.
      - indicator_evidence_missing: a buy/management decision cites nothing at all
        even though a vocabulary was available to cite from.

    hold/no_action proposals are exempt from the "missing" rule (they assert nothing)
    but are still subject to the other three, since even a hold must not fabricate a
    citation.
    """
    action = str(proposal.get("action") or "")
    raw_evidence = proposal.get("indicator_evidence")
    has_vocabulary = bool(available_indicators)

    # `None`/absent evidence is simply "no citation" (evidence_list == []), NOT malformed.
    # Only a present-but-wrong-shaped value (not a list, too long, or non-string items)
    # counts as structurally malformed.
    malformed_shape = raw_evidence is not None and not isinstance(raw_evidence, list)
    evidence_list: list[object] = [] if malformed_shape or raw_evidence is None else list(raw_evidence)

    reason: str | None = None
    normalized_evidence: list[str]
    if (
        malformed_shape
        or len(evidence_list) > MAX_INDICATOR_EVIDENCE
        or any(not isinstance(item, str) for item in evidence_list)
    ):
        reason = "indicator_evidence_malformed"
        normalized_evidence = [] if malformed_shape else [str(item) for item in evidence_list]
    else:
        normalized_evidence = [str(item) for item in evidence_list]
        if not has_vocabulary:
            if normalized_evidence:
                reason = "indicator_evidence_unverifiable"
        elif any(item not in available_indicators for item in normalized_evidence):
            reason = "indicator_evidence_unknown"
        elif action in ACTIONABLE_EVIDENCE_ACTIONS and not normalized_evidence:
            reason = "indicator_evidence_missing"

    if reason is None:
        proposal["indicator_evidence"] = normalized_evidence
        return proposal

    proposal["original_action"] = action
    proposal["action"] = "no_action"
    proposal["proposal_kind"] = _proposal_kind("no_action")
    proposal["degraded"] = True
    proposal["degradation_reason"] = reason
    proposal["indicator_evidence"] = normalized_evidence
    return proposal


def _float_value(value: object, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
