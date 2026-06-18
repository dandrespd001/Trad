"""Champion/challenger governance report for paper candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.execution.paper_common import redact_secrets, read_json_artifact, write_json_artifact, write_text_artifact


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/model_challenger"
STATUS_REVIEWABLE = "REVIEWABLE"
STATUS_REJECTED = "REJECTED"
STATUS_BLOCKED = "BLOCKED"
STATUS_ERROR = "ERROR"


class ModelChallengerOperationalError(RuntimeError):
    """Raised when the model challenger report cannot be written."""


@dataclass(frozen=True)
class ModelChallengerReportResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_model_challenger_report(
    *,
    evaluation_dir: str | Path,
    paper_performance: str | Path | None = None,
    mlflow_review: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> ModelChallengerReportResult:
    report = build_model_challenger_report(
        evaluation_dir=evaluation_dir,
        paper_performance=paper_performance,
        mlflow_review=mlflow_review,
        generated_at=generated_at,
    )
    output_root = Path(output_dir)
    output_path = output_root / "challenger_report.json"
    markdown_path = output_root / "challenger_report.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_model_challenger_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or STATUS_ERROR)
    return ModelChallengerReportResult(
        exit_code=_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_model_challenger_report(
    *,
    evaluation_dir: str | Path,
    paper_performance: str | Path | None = None,
    mlflow_review: str | Path | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    evaluation_root = Path(evaluation_dir)
    blockers: list[dict[str, object]] = []
    artifacts = _load_required_artifacts(evaluation_root, blockers=blockers)
    paper = _paper_performance_evidence(paper_performance, blockers=blockers)
    mlflow = _mlflow_evidence(mlflow_review, blockers=blockers)
    if any(blocker["severity"] == "ERROR" for blocker in blockers):
        status = STATUS_ERROR
    else:
        blockers.extend(_governance_blockers(artifacts=artifacts, paper=paper, mlflow=mlflow))
        status = _classify(blockers)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "status": status,
        "sources": {
            "evaluation_dir": str(evaluation_root),
            "paper_performance": str(paper_performance) if paper_performance is not None else None,
            "mlflow_review": str(mlflow_review) if mlflow_review is not None else None,
        },
        "evidence": {
            "evaluation_summary": _evaluation_summary_evidence(artifacts.get("evaluation_summary")),
            "promotion_decision": _promotion_evidence(artifacts.get("promotion_decision")),
            "walk_forward": _walk_forward_evidence(artifacts.get("walk_forward")),
            "regime_slices": _regime_evidence(artifacts.get("regime_slices")),
            "paper_performance": paper,
            "mlflow_review": mlflow,
        },
        "blockers": _dedupe_blockers(blockers),
        "authority": {
            "mutates_latest_model": False,
            "automatic_champion_replacement": False,
            "human_review_required": True,
            "latest_model_path": "models/latest_model.json",
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_model_challenger_markdown(report: Mapping[str, object]) -> str:
    evidence = _mapping(report.get("evidence"))
    evaluation = _mapping(evidence.get("evaluation_summary"))
    promotion = _mapping(evidence.get("promotion_decision"))
    paper = _mapping(evidence.get("paper_performance"))
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    lines = [
        "# Model Challenger Report",
        "",
        f"Status: **{report.get('status') or STATUS_ERROR}**",
        f"Generated at: `{report.get('generated_at') or ''}`",
        "",
        "## Evidence",
        "",
        f"Evaluation status: `{evaluation.get('status') or ''}`",
        f"Eligible for paper challenger: `{promotion.get('eligible_for_paper_challenger')}`",
        f"Paper compatible: `{paper.get('compatible')}`",
        "",
        "## Blockers",
        "",
        "| Severity | Code | Message |",
        "| --- | --- | --- |",
    ]
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(
                    "| "
                    f"`{_escape_markdown(blocker.get('severity') or '')}` "
                    f"| `{_escape_markdown(blocker.get('code') or '')}` "
                    f"| {_escape_markdown(blocker.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | Candidate is ready for human challenger review. |")
    lines.extend(
        [
            "",
            "## Authority",
            "",
            "Mutates latest model: `False`",
            "Automatic champion replacement: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _load_required_artifacts(root: Path, *, blockers: list[dict[str, object]]) -> dict[str, Mapping[str, object]]:
    specs = {
        "evaluation_summary": root / "evaluation_summary.json",
        "promotion_decision": root / "promotion_decision.json",
        "walk_forward": root / "walk_forward.json",
        "regime_slices": root / "regime_slices.json",
    }
    artifacts: dict[str, Mapping[str, object]] = {}
    for name, path in specs.items():
        try:
            artifacts[name] = read_json_artifact(path)
        except FileNotFoundError:
            blockers.append(_blocker("ERROR", f"missing_{name}", f"required artifact is missing: {path}", path))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(_blocker("ERROR", f"invalid_{name}", f"invalid artifact JSON: {exc}", path))
    return artifacts


def _governance_blockers(
    *,
    artifacts: Mapping[str, Mapping[str, object]],
    paper: Mapping[str, object],
    mlflow: Mapping[str, object],
) -> list[dict[str, object]]:
    summary = _mapping(artifacts.get("evaluation_summary"))
    promotion = _mapping(artifacts.get("promotion_decision"))
    walk_forward = _mapping(artifacts.get("walk_forward"))
    regimes = _mapping(artifacts.get("regime_slices"))
    blockers: list[dict[str, object]] = []

    if str(summary.get("status") or "").upper() == "BLOCKED":
        blockers.append(_blocker("CRITICAL", "evaluation_blocked", "evaluation summary is BLOCKED"))
    elif str(summary.get("status") or "").upper() != "APPROVED":
        blockers.append(_blocker("REJECT", "evaluation_not_approved", "evaluation summary is not APPROVED"))
    if promotion.get("eligible_for_paper_challenger") is not True:
        blockers.append(_blocker("REJECT", "promotion_not_eligible", "promotion decision is not eligible"))

    reasons = _string_list(summary.get("reasons")) + _string_list(promotion.get("reasons"))
    for reason in reasons:
        if "leakage" in reason:
            blockers.append(_blocker("REJECT", "temporal_leakage_detected", "temporal leakage evidence rejects candidate"))
        elif "cost" in reason or "slippage" in reason:
            blockers.append(_blocker("REJECT", reason, "cost/slippage evidence rejects candidate"))

    metrics = _mapping(summary.get("metrics"))
    trade_count = _float_or_none(metrics.get("trade_count"))
    if trade_count is None or trade_count < 1:
        blockers.append(_blocker("REJECT", "insufficient_trade_count", "candidate needs at least one evaluated trade"))
    max_drawdown = _float_or_none(metrics.get("max_drawdown"))
    if max_drawdown is not None and abs(max_drawdown) > 0.5:
        blockers.append(_blocker("REJECT", "max_drawdown_excessive", "candidate drawdown exceeds tolerance"))

    costs = _mapping(promotion.get("costs"))
    net_cagr = _float_or_none(costs.get("net_cagr_after_estimated_costs"))
    if net_cagr is None:
        blockers.append(_blocker("REJECT", "missing_cost_evidence", "cost-adjusted return evidence is required"))
    elif net_cagr < 0:
        blockers.append(_blocker("REJECT", "costs_slippage_turn_candidate_negative", "cost-adjusted return is negative"))

    walk_summary = _mapping(walk_forward.get("summary"))
    if int(_float_or_none(walk_summary.get("window_count")) or 0) < 1:
        blockers.append(_blocker("REJECT", "missing_oos_walk_forward", "walk-forward OOS evidence is required"))
    if walk_summary.get("robust_lift") is not True:
        blockers.append(_blocker("REJECT", "walk_forward_lift_not_robust", "walk-forward lift is not robust"))

    regime_summary = _mapping(regimes.get("summary"))
    if int(_float_or_none(regime_summary.get("slice_count")) or 0) < 1:
        blockers.append(_blocker("REJECT", "missing_regime_slices", "regime slice evidence is required"))

    if paper.get("status") == "NOT_PROVIDED":
        blockers.append(_blocker("CRITICAL", "missing_paper_performance", "paper performance evidence is required"))
    elif paper.get("compatible") is not True:
        blockers.append(_blocker("CRITICAL", "paper_performance_incompatible", "paper performance is not compatible"))

    if mlflow.get("status") == "FAILED":
        blockers.append(_blocker("CRITICAL", "mlflow_review_failed", "optional MLflow review was provided and failed"))
    return blockers


def _paper_performance_evidence(
    paper_performance: str | Path | None,
    *,
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    if paper_performance is None:
        return {"status": "NOT_PROVIDED", "compatible": False, "path": None}
    path = Path(paper_performance)
    try:
        payload = read_json_artifact(path)
    except FileNotFoundError:
        return {"status": "MISSING", "compatible": False, "path": str(path)}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        blockers.append(_blocker("ERROR", "invalid_paper_performance", f"invalid paper performance JSON: {exc}", path))
        return {"status": "ERROR", "compatible": False, "path": str(path)}
    metrics = _mapping(payload.get("paper_metrics"))
    compatible = (
        str(payload.get("status") or "").upper() not in {"ERROR", "CRITICAL"}
        and int(_float_or_none(metrics.get("fills")) or 0) > 0
        and int(_float_or_none(metrics.get("pending_closeouts")) or 0) == 0
        and int(_float_or_none(metrics.get("unmatched_closeouts")) or 0) == 0
        and int(_float_or_none(metrics.get("rejections")) or 0) == 0
        and not _object_list(payload.get("blockers"))
    )
    return {
        "status": str(payload.get("status") or "UNKNOWN"),
        "compatible": compatible,
        "path": str(path),
        "fills": metrics.get("fills", 0),
        "pnl_source": _mapping(metrics.get("pnl")).get("source"),
        "warnings": _string_list(payload.get("warnings")),
    }


def _mlflow_evidence(
    mlflow_review: str | Path | None,
    *,
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    if mlflow_review is None:
        return {"status": "NOT_PROVIDED", "optional": True, "path": None}
    path = Path(mlflow_review)
    try:
        payload = read_json_artifact(path)
    except FileNotFoundError:
        return {"status": "MISSING_OPTIONAL", "optional": True, "path": str(path)}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        blockers.append(_blocker("ERROR", "invalid_mlflow_review", f"invalid MLflow review JSON: {exc}", path))
        return {"status": "ERROR", "optional": True, "path": str(path)}
    return {
        "status": str(payload.get("status") or "UNKNOWN"),
        "optional": True,
        "path": str(path),
        "failures": _string_list(payload.get("failures")),
        "warnings": _string_list(payload.get("warnings")),
    }


def _evaluation_summary_evidence(payload: Mapping[str, object] | None) -> dict[str, object]:
    payload = _mapping(payload)
    return {
        "status": payload.get("status"),
        "eligible_for_paper_challenger": payload.get("eligible_for_paper_challenger"),
        "metrics": dict(_mapping(payload.get("metrics"))),
        "reasons": _string_list(payload.get("reasons")),
    }


def _promotion_evidence(payload: Mapping[str, object] | None) -> dict[str, object]:
    payload = _mapping(payload)
    return {
        "eligible_for_paper_challenger": payload.get("eligible_for_paper_challenger"),
        "approved": payload.get("approved"),
        "reasons": _string_list(payload.get("reasons")),
        "costs": dict(_mapping(payload.get("costs"))),
        "authority": dict(_mapping(payload.get("authority"))),
    }


def _walk_forward_evidence(payload: Mapping[str, object] | None) -> dict[str, object]:
    payload = _mapping(payload)
    return dict(_mapping(payload.get("summary")))


def _regime_evidence(payload: Mapping[str, object] | None) -> dict[str, object]:
    payload = _mapping(payload)
    return dict(_mapping(payload.get("summary")))


def _classify(blockers: list[Mapping[str, object]]) -> str:
    if any(blocker.get("severity") == "ERROR" for blocker in blockers):
        return STATUS_ERROR
    if any(blocker.get("severity") == "REJECT" for blocker in blockers):
        return STATUS_REJECTED
    if any(blocker.get("severity") == "CRITICAL" for blocker in blockers):
        return STATUS_BLOCKED
    return STATUS_REVIEWABLE


def _exit_code(status: str) -> int:
    if status == STATUS_ERROR:
        return 2
    if status in {STATUS_REJECTED, STATUS_BLOCKED}:
        return 1
    return 0


def _blocker(severity: str, code: str, message: str, source_path: object = None) -> dict[str, object]:
    payload: dict[str, object] = {"severity": severity, "code": code, "message": message}
    if source_path not in {None, ""}:
        payload["source_path"] = str(source_path)
    return payload


def _dedupe_blockers(blockers: list[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for blocker in blockers:
        normalized = dict(blocker)
        key = (
            str(normalized.get("severity") or ""),
            str(normalized.get("code") or ""),
            str(normalized.get("source_path") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise ModelChallengerOperationalError("model challenger report must be a JSON object")
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
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in {None, ""}]
    return [str(value)]


def _float_or_none(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
