"""Deterministic arbitration between baseline signals and LLM proposals."""

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
from trading_ai.llm.schemas import validate_against_schema

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_signal_arbitration"

DECISION_ELIGIBLE_FOR_PAPER = "ELIGIBLE_FOR_PAPER"
DECISION_NO_TRADE_REVIEW = "NO_TRADE_REVIEW"
DECISION_BLOCKED = "BLOCKED"


class PaperSignalArbitrationOperationalError(RuntimeError):
    """Raised when signal arbitration cannot be written."""


@dataclass(frozen=True)
class PaperSignalArbitrationResult:
    exit_code: int
    decision: str
    eligible_for_paper: bool
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_signal_arbitration(
    *,
    as_of_date: str,
    model_signals: str | Path,
    llm_proposals: str | Path,
    readiness: str | Path,
    features: str | Path | None = None,
    shadow_plan: str | Path | None = None,
    challenger_signals: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperSignalArbitrationResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "signal_plan.json"
    markdown_path = output_root / "signal_plan.md"
    try:
        readiness_payload = read_json_artifact(readiness)
        model_payload = read_json_artifact(model_signals)
        proposal_payload = read_json_artifact(llm_proposals)
        shadow_payload = read_json_artifact(shadow_plan) if shadow_plan is not None else None
        challenger_payload = read_json_artifact(challenger_signals) if challenger_signals is not None else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload = _payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            decision=DECISION_BLOCKED,
            selected_signal=None,
            selected_proposal=None,
            reasons=[_reason("ERROR", "invalid_input_artifact", str(exc))],
            sources=_sources(model_signals, llm_proposals, readiness, features, shadow_plan, challenger_signals),
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    readiness_reasons = _readiness_reasons(readiness_payload, as_of_date=as_of_date)
    if readiness_reasons:
        payload = _payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            decision=DECISION_BLOCKED,
            selected_signal=None,
            selected_proposal=None,
            reasons=readiness_reasons,
            sources=_sources(model_signals, llm_proposals, readiness, features, shadow_plan, challenger_signals),
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    artifact_reasons = _proposal_artifact_reasons(
        proposal_payload,
        as_of_date=as_of_date,
        readiness=readiness,
        features=features,
        model_signals=model_signals,
    )
    if artifact_reasons:
        payload = _payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            decision=DECISION_BLOCKED,
            selected_signal=None,
            selected_proposal=None,
            reasons=artifact_reasons,
            sources=_sources(model_signals, llm_proposals, readiness, features, shadow_plan, challenger_signals),
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)

    allowlist = _allowlist(readiness_payload)
    signals, signal_reasons, signal_collisions = _signals_by_symbol(model_payload)
    proposals, proposal_reasons, proposal_collisions = _proposals_by_symbol(proposal_payload)
    collision_reasons = signal_reasons + proposal_reasons
    collisions = signal_collisions + proposal_collisions
    if collision_reasons:
        payload = _payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            decision=DECISION_BLOCKED,
            selected_signal=None,
            selected_proposal=None,
            reasons=collision_reasons,
            sources=_sources(model_signals, llm_proposals, readiness, features, shadow_plan, challenger_signals),
            collisions=collisions,
        )
        return _write_result(payload, output_path=output_path, markdown_path=markdown_path)
    selected_signal: Mapping[str, object] | None = None
    selected_proposal: Mapping[str, object] | None = None
    reasons: list[dict[str, object]] = []

    buy_signals = [
        signal
        for signal in signals.values()
        if str(signal.get("action") or "").lower() == "buy" and str(signal.get("symbol") or "").upper() in allowlist
    ]
    buy_signals.sort(
        key=lambda signal: (_float_value(signal.get("probability")), str(signal.get("symbol") or "")), reverse=True
    )
    for signal in buy_signals:
        symbol = str(signal.get("symbol") or "").upper()
        proposal = proposals.get(symbol)
        if proposal is not None and str(proposal.get("action") or "").lower() == "buy":
            selected_signal = signal
            selected_proposal = proposal
            break

    if selected_signal is not None:
        decision = DECISION_ELIGIBLE_FOR_PAPER
        reasons.append(_reason("OK", "baseline_llm_buy_match", "baseline and LLM proposal both indicate buy"))
    else:
        decision = DECISION_NO_TRADE_REVIEW
        if any(str(proposal.get("action") or "").lower() == "buy" for proposal in proposals.values()):
            reasons.append(
                _reason("INFO", "baseline_llm_disagree", "LLM buy proposal does not match an eligible baseline buy")
            )
        elif buy_signals:
            reasons.append(_reason("INFO", "baseline_llm_disagree", "baseline buy does not match an LLM buy proposal"))
        else:
            reasons.append(_reason("INFO", "no_baseline_buy", "no eligible baseline buy signal"))

    payload = _payload(
        as_of_date=as_of_date,
        generated_at=generated_at,
        decision=decision,
        selected_signal=selected_signal,
        selected_proposal=selected_proposal,
        reasons=reasons,
        sources=_sources(model_signals, llm_proposals, readiness, features, shadow_plan, challenger_signals),
        model_signals=list(signals.values()),
        llm_proposals=list(proposals.values()),
        collisions=collisions,
        shadow=_shadow_record(shadow_payload, challenger_payload=challenger_payload, selected_signal=selected_signal)
        if shadow_payload is not None or challenger_payload is not None
        else None,
    )
    return _write_result(payload, output_path=output_path, markdown_path=markdown_path)


def render_paper_signal_arbitration_markdown(payload: Mapping[str, object]) -> str:
    reasons = _object_list(payload.get("reasons"))
    lines = [
        "# Paper Signal Arbitration",
        "",
        f"Decision: **{payload.get('decision') or DECISION_BLOCKED}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Selected symbol: `{payload.get('selected_symbol') or ''}`",
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
        lines.append("| OK | none | No arbitration reasons. |")
    lines.extend(["", "LLM authority: `none`", "Max new orders: `1`", "Paper notional USD: `1`", ""])
    return "\n".join(lines)


def _payload(
    *,
    as_of_date: str,
    generated_at: str | None,
    decision: str,
    selected_signal: Mapping[str, object] | None,
    selected_proposal: Mapping[str, object] | None,
    reasons: list[Mapping[str, object]],
    sources: Mapping[str, object],
    model_signals: list[Mapping[str, object]] | None = None,
    llm_proposals: list[Mapping[str, object]] | None = None,
    shadow: Mapping[str, object] | None = None,
    collisions: list[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    selected_symbol = str(selected_signal.get("symbol") or "").upper() if selected_signal is not None else None
    return _redact_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at or _utc_now(),
            "as_of_date": as_of_date,
            "decision": decision,
            "eligible_for_paper": decision == DECISION_ELIGIBLE_FOR_PAPER,
            "selected_symbol": selected_symbol,
            "selected_signal": dict(selected_signal) if selected_signal is not None else None,
            "selected_llm_proposal": dict(selected_proposal) if selected_proposal is not None else None,
            "model_signals": [dict(signal) for signal in model_signals or []],
            "llm_proposals": [dict(proposal) for proposal in llm_proposals or []],
            "order_constraints": {
                "max_new_orders_per_cycle": 1,
                "notional_usd": 1.0,
                "paper_only": True,
            },
            "shadow": dict(shadow) if shadow is not None else None,
            "collisions": [dict(collision) for collision in collisions or []],
            "sources": dict(sources),
            "reasons": [dict(reason) for reason in reasons],
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
    )


def _write_result(
    payload: dict[str, object], *, output_path: Path, markdown_path: Path
) -> PaperSignalArbitrationResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(render_paper_signal_arbitration_markdown(payload), markdown_path)
    decision = str(payload.get("decision") or DECISION_BLOCKED)
    return PaperSignalArbitrationResult(
        exit_code=1 if decision == DECISION_BLOCKED else 0,
        decision=decision,
        eligible_for_paper=payload.get("eligible_for_paper") is True,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _readiness_reasons(readiness: Mapping[str, object], *, as_of_date: str) -> list[dict[str, object]]:
    reasons: list[dict[str, object]] = []
    if str(readiness.get("status") or "").upper() != "READY" or readiness.get("ready_for_paper_daily") is not True:
        reasons.append(_reason("ERROR", "readiness_not_ready", "readiness is not READY"))
    approved = _mapping(readiness.get("approved_dataset"))
    latest = str(
        approved.get("end") or _mapping(readiness.get("inputs")).get("to") or readiness.get("as_of_date") or ""
    )
    if latest and _date_token(latest) < _date_token(as_of_date):
        reasons.append(_reason("ERROR", "dataset_stale", f"dataset latest date {latest} is before {as_of_date}"))
    safety = _mapping(readiness.get("safety"))
    if safety.get("credentials_read") is True:
        reasons.append(_reason("CRITICAL", "credentials_read", "readiness reports credentials were read"))
    if safety.get("live_trading_allowed") is True or safety.get("live_trading_authorized") is True:
        reasons.append(_reason("CRITICAL", "live_trading_not_allowed", "live trading must remain disabled"))
    return reasons


def _allowlist(readiness: Mapping[str, object]) -> set[str]:
    symbols = _mapping(readiness.get("approved_dataset")).get("symbols")
    if isinstance(symbols, list):
        return {str(symbol).upper() for symbol in symbols}
    return set()


def _signals_by_symbol(
    payload: Mapping[str, object],
) -> tuple[dict[str, Mapping[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    result: dict[str, Mapping[str, object]] = {}
    reasons: list[dict[str, object]] = []
    collisions: list[dict[str, object]] = []
    signals = payload.get("signals")
    if isinstance(signals, list):
        for signal in signals:
            if isinstance(signal, Mapping):
                symbol = str(signal.get("symbol") or "").upper()
                if symbol:
                    _record_by_symbol(
                        result,
                        collisions,
                        reasons,
                        symbol=symbol,
                        item=signal,
                        source="model_signals",
                        reason_code="duplicate_model_signal_symbol_conflict",
                    )
    selected = payload.get("selected_signal")
    if isinstance(selected, Mapping):
        symbol = str(selected.get("symbol") or "").upper()
        if symbol:
            _record_by_symbol(
                result,
                collisions,
                reasons,
                symbol=symbol,
                item=selected,
                source="model_selected_signal",
                reason_code="duplicate_model_signal_symbol_conflict",
            )
    return result, reasons, collisions


def _proposal_artifact_reasons(
    payload: Mapping[str, object],
    *,
    as_of_date: str,
    readiness: str | Path,
    features: str | Path | None,
    model_signals: str | Path,
) -> list[dict[str, object]]:
    reasons: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    if status and status != "OK":
        reasons.append(_reason("ERROR", "llm_proposals_not_ok", f"LLM proposals status is {status}"))
    proposal_date = str(payload.get("as_of_date") or "")
    if proposal_date and proposal_date != as_of_date:
        reasons.append(
            _reason(
                "ERROR",
                "llm_proposals_date_mismatch",
                f"LLM proposals date {proposal_date} does not match {as_of_date}",
            )
        )
    schema_version = str(payload.get("schema_version") or "")
    if schema_version and schema_version != SCHEMA_VERSION:
        reasons.append(
            _reason(
                "ERROR",
                "llm_proposals_schema_mismatch",
                f"LLM proposals schema version {schema_version} is not {SCHEMA_VERSION}",
            )
        )
    input_hashes = _mapping(payload.get("input_hashes"))
    if input_hashes:
        expected = {
            "readiness": _source_hash(readiness),
            "model_signals": _source_hash(model_signals),
        }
        if "features" in input_hashes:
            if features is None:
                reasons.append(
                    _reason(
                        "CRITICAL",
                        "features_hash_unverifiable",
                        "features path is required to verify LLM proposal provenance",
                    )
                )
            else:
                expected["features"] = _source_hash(features)
        elif schema_version == SCHEMA_VERSION:
            reasons.append(
                _reason("CRITICAL", "features_hash_missing", "LLM proposal provenance does not include features hash")
            )
        for name, expected_hash in expected.items():
            actual_hash = input_hashes.get(name)
            if actual_hash and expected_hash and actual_hash != expected_hash:
                reasons.append(
                    _reason("CRITICAL", f"{name}_hash_mismatch", f"{name} hash does not match LLM proposal provenance")
                )
    authority = _mapping(payload.get("authority"))
    if authority.get("llm_authority") not in {None, "", "none"}:
        reasons.append(_reason("CRITICAL", "llm_authority_not_none", "LLM proposal artifact grants LLM authority"))
    safety = _mapping(payload.get("safety"))
    if safety.get("credentials_read") is True:
        reasons.append(_reason("CRITICAL", "credentials_read", "LLM proposal artifact reports credentials were read"))
    if safety.get("broker_client_built") is True:
        reasons.append(
            _reason("CRITICAL", "broker_client_built", "LLM proposal artifact reports broker client was built")
        )
    if safety.get("orders_submitted") is True:
        reasons.append(_reason("CRITICAL", "orders_submitted", "LLM proposal artifact reports orders were submitted"))
    if safety.get("live_trading_allowed") is True or safety.get("live_trading_authorized") is True:
        reasons.append(
            _reason("CRITICAL", "live_trading_not_allowed", "LLM proposal artifact attempts to authorize live trading")
        )
    return reasons


def _proposals_by_symbol(
    payload: Mapping[str, object],
) -> tuple[dict[str, Mapping[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    result: dict[str, Mapping[str, object]] = {}
    reasons: list[dict[str, object]] = []
    collisions: list[dict[str, object]] = []
    proposals = payload.get("proposals")
    if isinstance(proposals, list):
        for index, proposal in enumerate(proposals):
            if not isinstance(proposal, Mapping):
                reasons.append(_reason("ERROR", "invalid_llm_proposal", f"proposal {index} is not a JSON object"))
                continue
            item = dict(proposal)
            symbol = str(item.get("symbol") or "").upper()
            item["symbol"] = symbol
            try:
                validate_against_schema("LLMSignalProposal", item)
            except ValueError as exc:
                reasons.append(_reason("ERROR", "invalid_llm_proposal_schema", str(exc)))
                continue
            if not symbol:
                reasons.append(_reason("ERROR", "invalid_llm_proposal_symbol", f"proposal {index} has an empty symbol"))
                continue
            _record_by_symbol(
                result,
                collisions,
                reasons,
                symbol=symbol,
                item=item,
                source="llm_proposals",
                reason_code="duplicate_llm_proposal_symbol_conflict",
            )
    elif proposals is not None:
        reasons.append(_reason("ERROR", "invalid_llm_proposals", "LLM proposals must be a list"))
    return result, reasons, collisions


def _record_by_symbol(
    result: dict[str, Mapping[str, object]],
    collisions: list[dict[str, object]],
    reasons: list[dict[str, object]],
    *,
    symbol: str,
    item: Mapping[str, object],
    source: str,
    reason_code: str,
) -> None:
    existing = result.get(symbol)
    if existing is None:
        result[symbol] = item
        return
    collision = {"symbol": symbol, "source": source, "conflicting": _canonical(existing) != _canonical(item)}
    collisions.append(collision)
    if collision["conflicting"]:
        reasons.append(_reason("ERROR", reason_code, f"conflicting duplicate symbol {symbol} in {source}"))


def _source_hash(path: str | Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _sources(
    model_signals: str | Path,
    llm_proposals: str | Path,
    readiness: str | Path,
    features: str | Path | None = None,
    shadow_plan: str | Path | None = None,
    challenger_signals: str | Path | None = None,
) -> dict[str, object]:
    return {
        "model_signals": str(Path(model_signals)),
        "llm_proposals": str(Path(llm_proposals)),
        "readiness": str(Path(readiness)),
        "features": str(Path(features)) if features is not None else None,
        "shadow_plan": str(Path(shadow_plan)) if shadow_plan is not None else None,
        "challenger_signals": str(Path(challenger_signals)) if challenger_signals is not None else None,
    }


def _shadow_record(
    payload: Mapping[str, object] | None,
    *,
    challenger_payload: Mapping[str, object] | None = None,
    selected_signal: Mapping[str, object] | None,
) -> dict[str, object]:
    state = str(_mapping(payload).get("shadow_state") or _mapping(challenger_payload).get("status") or "BLOCKED")
    challenger_signal = _mapping(_mapping(challenger_payload).get("selected_signal"))
    selected_symbol = str(challenger_signal.get("symbol") or "").upper()
    if not selected_symbol:
        selected_symbol = str(selected_signal.get("symbol") or "").upper() if selected_signal is not None else None
    return {
        "state": state,
        "shadow_only": _mapping(_mapping(payload).get("challenger")).get("shadow_only") is True
        or _mapping(challenger_payload).get("shadow_only") is True,
        "selected_symbol": selected_symbol,
        "selected_signal": dict(challenger_signal)
        if challenger_signal
        else dict(selected_signal)
        if selected_signal is not None
        else None,
        "action": challenger_signal.get("action"),
        "probability": challenger_signal.get("probability"),
        "affects_paper_order": False,
        "orders_submitted": False,
    }


def _reason(severity: str, code: str, message: str) -> dict[str, object]:
    return {"severity": severity, "code": code, "message": redact_secrets(message, env={})}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _date_token(value: object) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return date.min


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str)


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperSignalArbitrationOperationalError("paper signal arbitration must be a JSON object")
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
