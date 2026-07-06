"""Incremental-value attribution for AI-derived trading features."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact

SCHEMA_VERSION = 1
STATUS_READY = "AI_VALUE_READY"
STATUS_INSUFFICIENT = "AI_VALUE_INSUFFICIENT"
STATUS_BLOCKED = "BLOCKED"


class AiFeatureAttributionOperationalError(RuntimeError):
    """Raised when attribution inputs cannot be read or written."""


@dataclass(frozen=True)
class AiFeatureAttributionResult:
    exit_code: int
    status: str
    output_dir: Path
    report_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_ai_feature_attribution_report(
    *,
    as_of_date: str,
    baseline_ranking: str | Path,
    ai_ranking: str | Path,
    output_dir: str | Path = "reports/tmp/ai_feature_attribution",
    min_sharpe_delta: float = 0.05,
    max_drawdown_worsening: float = 0.02,
    max_cost_delta: float = 0.01,
) -> AiFeatureAttributionResult:
    baseline_path = Path(baseline_ranking)
    ai_path = Path(ai_ranking)
    baseline = read_json_artifact(baseline_path)
    ai = read_json_artifact(ai_path)
    run_dir = Path(output_dir) / as_of_date
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "ai_feature_attribution.json"
    markdown_path = run_dir / "ai_feature_attribution.md"

    payload = _build_report(
        as_of_date=as_of_date,
        baseline=baseline,
        ai=ai,
        baseline_path=baseline_path,
        ai_path=ai_path,
        min_sharpe_delta=min_sharpe_delta,
        max_drawdown_worsening=max_drawdown_worsening,
        max_cost_delta=max_cost_delta,
    )
    write_json_artifact(payload, report_path)
    write_text_artifact(_render_markdown(payload), markdown_path)
    status = str(payload.get("status") or STATUS_BLOCKED)
    exit_code = 0 if status == STATUS_READY else 1 if status in {STATUS_INSUFFICIENT, STATUS_BLOCKED} else 2
    return AiFeatureAttributionResult(exit_code, status, run_dir, report_path, markdown_path, payload)


def _build_report(
    *,
    as_of_date: str,
    baseline: Mapping[str, object],
    ai: Mapping[str, object],
    baseline_path: Path,
    ai_path: Path,
    min_sharpe_delta: float,
    max_drawdown_worsening: float,
    max_cost_delta: float,
) -> dict[str, object]:
    blockers = _input_blockers(as_of_date=as_of_date, baseline=baseline, ai=ai)
    baseline_candidate = _baseline_candidate(baseline)
    ai_candidates = _ai_candidates(ai)
    if baseline_candidate is None:
        blockers.append("baseline_candidate_missing")
    if not ai_candidates:
        blockers.append("ai_candidate_missing")

    candidate_reports: list[dict[str, object]] = []
    baseline_candidate_id = _candidate_id(baseline_candidate)
    best_ai_candidate_id: str | None = None
    best_ai_delta: dict[str, object] = {}
    if baseline_candidate is not None:
        baseline_metrics = _metrics(baseline_candidate)
        candidate_reports = [
            _candidate_report(
                candidate,
                baseline_metrics=baseline_metrics,
                thresholds={
                    "min_sharpe_delta": min_sharpe_delta,
                    "max_drawdown_worsening": max_drawdown_worsening,
                    "max_cost_delta": max_cost_delta,
                },
            )
            for candidate in ai_candidates
        ]
        selected = _best_candidate_report(candidate_reports)
        if selected is not None:
            best_ai_candidate_id = str(selected.get("candidate_id") or "")
            best_ai_delta = dict(_mapping(selected.get("delta")))

    if blockers:
        status = STATUS_BLOCKED
        decision = "BLOCK_FOR_PAPER_EVIDENCE"
    else:
        eligible = [row for row in candidate_reports if row.get("passes_thresholds") is True]
        if eligible:
            selected = _best_candidate_report(eligible) or eligible[0]
            best_ai_candidate_id = str(selected.get("candidate_id") or "")
            best_ai_delta = dict(_mapping(selected.get("delta")))
            status = STATUS_READY
            decision = "ACCEPT_FOR_PAPER_EVIDENCE"
        else:
            blockers.append("ai_candidate_did_not_clear_thresholds")
            status = STATUS_INSUFFICIENT
            decision = "REJECT_FOR_PAPER_EVIDENCE"

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "as_of_date": as_of_date,
        "objective": "incremental_ai_feature_value",
        "baseline_candidate_id": baseline_candidate_id,
        "best_ai_candidate_id": best_ai_candidate_id,
        "best_ai_delta": best_ai_delta,
        "thresholds": {
            "min_sharpe_delta": min_sharpe_delta,
            "max_drawdown_worsening": max_drawdown_worsening,
            "max_cost_delta": max_cost_delta,
        },
        "incremental_value": {
            "decision": decision,
            "eligible_candidate_count": len([row for row in candidate_reports if row.get("passes_thresholds") is True]),
            "evaluated_ai_candidate_count": len(candidate_reports),
        },
        "candidate_reports": candidate_reports,
        "blockers": _dedupe(blockers),
        "input_hashes": {
            "baseline_ranking_path": str(baseline_path),
            "baseline_ranking_sha256": _file_sha256(baseline_path),
            "ai_ranking_path": str(ai_path),
            "ai_ranking_sha256": _file_sha256(ai_path),
        },
        "feature_sources": {
            "baseline": dict(_mapping(baseline.get("feature_sources"))),
            "ai": dict(_mapping(ai.get("feature_sources"))),
        },
        "authority": _authority(),
        "safety": _safety(),
    }


def _input_blockers(
    *,
    as_of_date: str,
    baseline: Mapping[str, object],
    ai: Mapping[str, object],
) -> list[str]:
    blockers: list[str] = []
    baseline_date = _ranking_as_of_date(baseline)
    ai_date = _ranking_as_of_date(ai)
    if baseline_date and baseline_date != as_of_date:
        blockers.append("baseline_as_of_date_mismatch")
    if ai_date and ai_date != as_of_date:
        blockers.append("ai_as_of_date_mismatch")
    if str(baseline.get("status") or "").upper() not in {"CANDIDATE_READY", "OK"}:
        blockers.append("baseline_ranking_not_ready")
    if str(ai.get("status") or "").upper() not in {"CANDIDATE_READY", "OK"}:
        blockers.append("ai_ranking_not_ready")
    baseline_dataset = _mapping(baseline.get("approved_dataset"))
    ai_dataset = _mapping(ai.get("approved_dataset"))
    for key in ("dataset_id", "frequency", "dataset_hash"):
        baseline_value = str(baseline_dataset.get(key) or "")
        ai_value = str(ai_dataset.get(key) or "")
        if baseline_value and ai_value and baseline_value != ai_value:
            blockers.append(f"{key}_mismatch")
    return blockers


def _baseline_candidate(ranking: Mapping[str, object]) -> Mapping[str, object] | None:
    candidates = _candidate_list(ranking)
    preferred_id = str(ranking.get("best_candidate_id") or "")
    preferred = next(
        (
            candidate
            for candidate in candidates
            if _candidate_id(candidate) == preferred_id and not _is_ai_candidate(candidate) and _is_candidate_ok(candidate)
        ),
        None,
    )
    if preferred is not None:
        return preferred
    return _best_candidate(
        [candidate for candidate in candidates if not _is_ai_candidate(candidate) and _is_candidate_ok(candidate)]
    )


def _ai_candidates(ranking: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [candidate for candidate in _candidate_list(ranking) if _is_candidate_ok(candidate) and _is_ai_candidate(candidate)]


def _candidate_report(
    candidate: Mapping[str, object],
    *,
    baseline_metrics: Mapping[str, float],
    thresholds: Mapping[str, float],
) -> dict[str, object]:
    metrics = _metrics(candidate)
    delta = {
        "sharpe": metrics["sharpe"] - baseline_metrics["sharpe"],
        "calmar": metrics["calmar"] - baseline_metrics["calmar"],
        "max_drawdown": metrics["max_drawdown"] - baseline_metrics["max_drawdown"],
        "estimated_costs": metrics["estimated_costs"] - baseline_metrics["estimated_costs"],
    }
    drawdown_worsening = max(0.0, baseline_metrics["max_drawdown"] - metrics["max_drawdown"])
    cost_delta = metrics["estimated_costs"] - baseline_metrics["estimated_costs"]
    failure_reasons: list[str] = []
    if delta["sharpe"] < float(thresholds["min_sharpe_delta"]):
        failure_reasons.append("sharpe_delta_below_threshold")
    if drawdown_worsening > float(thresholds["max_drawdown_worsening"]):
        failure_reasons.append("drawdown_worsening_above_threshold")
    if cost_delta > float(thresholds["max_cost_delta"]):
        failure_reasons.append("cost_delta_above_threshold")
    return {
        "candidate_id": _candidate_id(candidate),
        "feature_names": list(_feature_names(candidate)),
        "metrics": dict(metrics),
        "delta": delta,
        "risk_checks": {
            "drawdown_worsening": drawdown_worsening,
            "cost_delta": cost_delta,
        },
        "passes_thresholds": not failure_reasons,
        "failure_reasons": failure_reasons,
        "score": _candidate_score(candidate),
    }


def _best_candidate_report(rows: list[dict[str, object]]) -> dict[str, object] | None:
    if not rows:
        return None
    return sorted(rows, key=lambda row: _finite_float(row.get("score")), reverse=True)[0]


def _best_candidate(candidates: list[Mapping[str, object]]) -> Mapping[str, object] | None:
    if not candidates:
        return None
    return sorted(candidates, key=_candidate_score, reverse=True)[0]


def _candidate_list(ranking: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = ranking.get("candidates")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, Mapping)]


def _is_candidate_ok(candidate: Mapping[str, object]) -> bool:
    return str(candidate.get("status") or "").upper() == "OK"


def _is_ai_candidate(candidate: Mapping[str, object]) -> bool:
    candidate_id = _candidate_id(candidate).lower()
    if "ai_features" in candidate_id or "forecast_challenger" in candidate_id:
        return True
    return any(name.startswith(("ai_", "forecast_")) for name in _feature_names(candidate))


def _candidate_id(candidate: Mapping[str, object] | None) -> str | None:
    if candidate is None:
        return None
    value = str(candidate.get("candidate_id") or "").strip()
    return value or None


def _feature_names(candidate: Mapping[str, object]) -> tuple[str, ...]:
    raw = candidate.get("feature_names")
    if not isinstance(raw, list):
        raw = candidate.get("features")
    if not isinstance(raw, list):
        return ()
    return tuple(str(name) for name in raw if str(name).strip())


def _metrics(candidate: Mapping[str, object]) -> dict[str, float]:
    metrics = _mapping(candidate.get("metrics"))
    return {
        "sharpe": _finite_float(metrics.get("sharpe")),
        "calmar": _finite_float(metrics.get("calmar")),
        "max_drawdown": _finite_float(metrics.get("max_drawdown")),
        "estimated_costs": _finite_float(metrics.get("estimated_costs")),
    }


def _candidate_score(candidate: Mapping[str, object]) -> float:
    score = _finite_float(candidate.get("score"), default=float("nan"))
    if math.isfinite(score):
        return score
    metrics = _metrics(candidate)
    return metrics["sharpe"] + 0.25 * metrics["calmar"] + metrics["max_drawdown"] - metrics["estimated_costs"]


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _ranking_as_of_date(payload: Mapping[str, object]) -> str:
    dataset = _mapping(payload.get("approved_dataset"))
    return str(payload.get("as_of_date") or dataset.get("as_of_date") or "")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authority() -> dict[str, object]:
    return {
        "review_only": True,
        "llm_authority": "none",
        "orders_submitted": False,
        "risk_changed": False,
        "mutates_latest_model": False,
        "live_trading_authorized": False,
    }


def _safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_execution_enabled": False,
        "live_trading_allowed": False,
        "external_api_used": False,
    }


def _render_markdown(payload: Mapping[str, object]) -> str:
    blockers = payload.get("blockers")
    blocker_rows = blockers if isinstance(blockers, list) else []
    lines = [
        "# AI Feature Attribution",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Baseline candidate: `{payload.get('baseline_candidate_id')}`",
        f"- Best AI candidate: `{payload.get('best_ai_candidate_id')}`",
        "",
        "## Blockers",
    ]
    lines.extend(f"- `{blocker}`" for blocker in blocker_rows)
    if not blocker_rows:
        lines.append("- `none`")
    lines.extend(["", "Orders submitted: `False`", "LLM authority: `none`", ""])
    return "\n".join(lines)
