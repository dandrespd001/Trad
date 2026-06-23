"""Simple paper-only strategy quality report."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_auto_sessions import classify_paper_auto_session, read_paper_auto_session_records
from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_strategy_quality"


class PaperStrategyQualityOperationalError(RuntimeError):
    """Raised when paper strategy quality cannot be produced."""


@dataclass(frozen=True)
class PaperStrategyQualityResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_strategy_quality(
    *,
    as_of_date: str,
    model_signals: str | Path,
    signal_plan: str | Path,
    performance: str | Path,
    challenger_report: str | Path | None = None,
    ledger_inputs: Iterable[str | Path] = (),
    lookback_sessions: int = 60,
    min_clean_sessions: int = 20,
    min_paper_fills: int = 20,
    max_cost_drag_bps: float | None = None,
    max_trade_count_gap_pct: float | None = None,
    max_blocker_rate_pct: float | None = None,
    max_llm_disagreement_rate_pct: float | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperStrategyQualityResult:
    generated = generated_at or _utc_now()
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "strategy_quality.json"
    markdown_path = output_root / "strategy_quality.md"
    try:
        payload = build_paper_strategy_quality(
            as_of_date=as_of_date,
            model_signals=model_signals,
            signal_plan=signal_plan,
            performance=performance,
            challenger_report=challenger_report,
            ledger_inputs=ledger_inputs,
            lookback_sessions=lookback_sessions,
            min_clean_sessions=min_clean_sessions,
            min_paper_fills=min_paper_fills,
            max_cost_drag_bps=max_cost_drag_bps,
            max_trade_count_gap_pct=max_trade_count_gap_pct,
            max_blocker_rate_pct=max_blocker_rate_pct,
            max_llm_disagreement_rate_pct=max_llm_disagreement_rate_pct,
            generated_at=generated,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload = _error_payload(as_of_date=as_of_date, generated_at=generated, message=str(exc))
    write_json_artifact(payload, output_path)
    write_text_artifact(render_paper_strategy_quality_markdown(payload), markdown_path)
    status = str(payload.get("status") or "ERROR")
    return PaperStrategyQualityResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def build_paper_strategy_quality(
    *,
    as_of_date: str,
    model_signals: str | Path,
    signal_plan: str | Path,
    performance: str | Path,
    challenger_report: str | Path | None,
    ledger_inputs: Iterable[str | Path] = (),
    lookback_sessions: int = 60,
    min_clean_sessions: int = 20,
    min_paper_fills: int = 20,
    max_cost_drag_bps: float | None = None,
    max_trade_count_gap_pct: float | None = None,
    max_blocker_rate_pct: float | None = None,
    max_llm_disagreement_rate_pct: float | None = None,
    generated_at: str,
) -> dict[str, object]:
    ledger_paths = [Path(path) for path in ledger_inputs]
    signals_payload = read_json_artifact(model_signals)
    signal_plan_payload = read_json_artifact(signal_plan)
    performance_payload = read_json_artifact(performance)
    challenger_payload = read_json_artifact(challenger_report) if challenger_report is not None else None
    thresholds = {
        "min_clean_sessions": int(min_clean_sessions),
        "min_paper_fills": int(min_paper_fills),
        "lookback_sessions": int(lookback_sessions),
        "max_cost_drag_bps": max_cost_drag_bps,
        "max_trade_count_gap_pct": max_trade_count_gap_pct,
        "max_blocker_rate_pct": max_blocker_rate_pct,
        "max_llm_disagreement_rate_pct": max_llm_disagreement_rate_pct,
    }
    baseline = _baseline_summary(signals_payload)
    arbitration = _arbitration_summary(signal_plan_payload)
    challenger = _challenger_summary(challenger_payload)
    cost_adjusted = _cost_summary(performance_payload)
    quality_trend = _quality_trend(
        ledger_paths,
        lookback_sessions=lookback_sessions,
        min_paper_fills=min_paper_fills,
        paper_fills=_int_value(cost_adjusted.get("paper_fills"), default=0),
    )
    warnings, blockers = _quality_findings(
        baseline=baseline,
        arbitration=arbitration,
        performance=performance_payload,
        costs=cost_adjusted,
        thresholds=thresholds,
    )
    trend_warnings, trend_blockers = _trend_findings(quality_trend=quality_trend, thresholds=thresholds)
    warnings = _dedupe([*warnings, *trend_warnings])
    blockers = _dedupe([*blockers, *trend_blockers])
    quality_status = _quality_status(warnings=warnings, blockers=blockers, costs=cost_adjusted, thresholds=thresholds)
    status = _status_for_quality(quality_status)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": status,
        "sources": {
            "model_signals": str(Path(model_signals)),
            "signal_plan": str(Path(signal_plan)),
            "performance": str(Path(performance)),
            "challenger_report": str(Path(challenger_report)) if challenger_report is not None else None,
            "ledger_inputs": [str(path) for path in ledger_paths],
        },
        "quality_status": quality_status,
        "thresholds": thresholds,
        "baseline": baseline,
        "arbitration": arbitration,
        "challenger": challenger,
        "cost_adjusted": cost_adjusted,
        "quality_trend": quality_trend,
        "warnings": warnings,
        "blockers": blockers,
        "authority": {
            "model_promoted": False,
            "risk_changed": False,
            "orders_submitted": False,
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


def render_paper_strategy_quality_markdown(payload: Mapping[str, object]) -> str:
    baseline = _mapping(payload.get("baseline"))
    arbitration = _mapping(payload.get("arbitration"))
    challenger = _mapping(payload.get("challenger"))
    costs = _mapping(payload.get("cost_adjusted"))
    trend = _mapping(payload.get("quality_trend"))
    lines = [
        "# Paper Strategy Quality",
        "",
        f"Status: **{payload.get('status') or 'ERROR'}**",
        f"Quality status: `{payload.get('quality_status') or ''}`",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        "",
        "## Baseline",
        "",
        f"Selected symbol: `{baseline.get('selected_symbol') or ''}`",
        f"Selected action: `{baseline.get('selected_action') or ''}`",
        f"Buy signals: `{baseline.get('buy_signals', 0)}`",
        "",
        "## Arbitration",
        "",
        f"Decision: `{arbitration.get('decision') or ''}`",
        f"Eligible for paper: `{arbitration.get('eligible_for_paper')}`",
        "",
        "## Challenger",
        "",
        f"Decision: `{challenger.get('decision') or ''}`",
        "",
        "## Costs",
        "",
        f"Estimated costs: `{costs.get('estimated_costs')}`",
        f"Trade count gap: `{costs.get('trade_count_gap')}`",
        "",
        "## Quality Trend",
        "",
        f"Lookback sessions: `{trend.get('lookback_sessions', 0)}`",
        f"Blocker rate pct: `{trend.get('blocker_rate_pct')}`",
        f"LLM disagreement rate pct: `{trend.get('llm_disagreement_rate_pct')}`",
        f"Clean-session trend: `{trend.get('clean_session_trend') or ''}`",
        "",
        "Model promoted: `False`",
        "Risk changed: `False`",
        "Live trading authorized: `False`",
        "",
    ]
    return "\n".join(lines)


def _baseline_summary(payload: Mapping[str, object]) -> dict[str, object]:
    raw_signals = payload.get("signals")
    signals = (
        [item for item in raw_signals if isinstance(item, Mapping)]
        if isinstance(raw_signals, list)
        else []
    )
    raw_selected = payload.get("selected_signal")
    selected = (
        raw_selected
        if isinstance(raw_selected, Mapping)
        else (signals[0] if signals else {})
    )
    return {
        "signal_count": len(signals),
        "buy_signals": sum(1 for signal in signals if str(signal.get("action") or "").lower() == "buy"),
        "hold_signals": sum(1 for signal in signals if str(signal.get("action") or "").lower() != "buy"),
        "selected_symbol": str(selected.get("symbol") or "").upper() if isinstance(selected, Mapping) else "",
        "selected_action": str(selected.get("action") or "").lower() if isinstance(selected, Mapping) else "",
        "selected_probability": selected.get("probability") if isinstance(selected, Mapping) else None,
    }


def _challenger_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "decision": "NOT_PROVIDED", "status": "NOT_PROVIDED"}
    return {
        "present": True,
        "status": str(payload.get("status") or "UNKNOWN"),
        "decision": str(payload.get("decision") or payload.get("status") or "UNKNOWN"),
    }


def _arbitration_summary(payload: Mapping[str, object]) -> dict[str, object]:
    raw_discrepancies = payload.get("discrepancies")
    return {
        "decision": str(payload.get("decision") or "UNKNOWN"),
        "eligible_for_paper": payload.get("eligible_for_paper") is True,
        "discrepancies": [dict(item) for item in raw_discrepancies if isinstance(item, Mapping)]
        if isinstance(raw_discrepancies, list)
        else [],
    }


def _cost_summary(payload: Mapping[str, object]) -> dict[str, object]:
    gap = _mapping(payload.get("paper_vs_backtest"))
    metrics = _mapping(gap.get("backtest_metrics"))
    paper = _mapping(payload.get("paper_metrics"))
    estimated_costs = metrics.get("estimated_costs")
    estimated_costs_bps = metrics.get("estimated_costs_bps")
    if estimated_costs_bps is None:
        numeric_cost = _float_or_none(estimated_costs)
        estimated_costs_bps = numeric_cost * 10000.0 if numeric_cost is not None else None
    trade_count_gap = _float_or_none(gap.get("trade_count_gap"))
    backtest_trade_count = _float_or_none(metrics.get("trade_count"))
    trade_count_gap_pct = (
        abs(trade_count_gap) / backtest_trade_count * 100.0
        if trade_count_gap is not None and backtest_trade_count is not None and backtest_trade_count != 0
        else None
    )
    return {
        "backtest_available": gap.get("backtest_available") is True,
        "estimated_costs": metrics.get("estimated_costs"),
        "estimated_costs_bps": estimated_costs_bps,
        "backtest_trade_count": metrics.get("trade_count"),
        "paper_fills": paper.get("fills"),
        "clean_sessions": _mapping(payload.get("paper_auto_sessions")).get(
            "clean_sessions", paper.get("complete_sessions")
        ),
        "trade_count_gap": gap.get("trade_count_gap"),
        "trade_count_gap_pct": trade_count_gap_pct,
        "sharpe": metrics.get("sharpe"),
    }


def _quality_findings(
    *,
    baseline: Mapping[str, object],
    arbitration: Mapping[str, object],
    performance: Mapping[str, object],
    costs: Mapping[str, object],
    thresholds: Mapping[str, object],
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    blockers: list[str] = []
    performance_status = str(performance.get("status") or "UNKNOWN").upper()
    if performance_status == "ERROR":
        blockers.append("performance_error")
    for blocker in _string_list(performance.get("blockers")):
        blockers.append(blocker)
    metrics = _mapping(performance.get("paper_metrics"))
    if _int_value(metrics.get("pending_closeouts"), default=0) > 0:
        blockers.append("closeout_pending")
    statement = _mapping(performance.get("statement_status"))
    statement_status = str(statement.get("status") or "").upper()
    if statement_status in {"DIFFERENCES", "MISMATCH", "ERROR", "MISSING"}:
        blockers.append("statement_mismatch")
    if _int_value(statement.get("unreconciled_fills"), default=0) > 0:
        blockers.append("fills_unreconciled")
    for discrepancy in _object_list(arbitration.get("discrepancies")):
        if isinstance(discrepancy, Mapping):
            code = str(discrepancy.get("code") or "llm_baseline_disagreement")
            blockers.append(code)
    if baseline.get("selected_action") == "buy" and arbitration.get("eligible_for_paper") is False:
        blockers.append("llm_baseline_disagreement")

    max_cost = _float_or_none(thresholds.get("max_cost_drag_bps"))
    cost_bps = _float_or_none(costs.get("estimated_costs_bps"))
    if max_cost is not None and cost_bps is not None and cost_bps > max_cost:
        warnings.append("cost_drag_exceeds_threshold")
    max_gap = _float_or_none(thresholds.get("max_trade_count_gap_pct"))
    gap_pct = _float_or_none(costs.get("trade_count_gap_pct"))
    if max_gap is not None and gap_pct is not None and gap_pct > max_gap:
        warnings.append("trade_count_gap_exceeds_threshold")
    return _dedupe(warnings), _dedupe(blockers)


def _quality_status(
    *,
    warnings: list[str],
    blockers: list[str],
    costs: Mapping[str, object],
    thresholds: Mapping[str, object],
) -> str:
    if blockers:
        return "BLOCKED"
    clean_sessions = _int_value(costs.get("clean_sessions"), default=0)
    fills = _int_value(costs.get("paper_fills"), default=0)
    if clean_sessions < _int_value(thresholds.get("min_clean_sessions"), default=20):
        return "DEFER"
    if fills < _int_value(thresholds.get("min_paper_fills"), default=20):
        return "DEFER"
    if warnings:
        return "WARN"
    return "PASS"


def _quality_trend(
    ledger_inputs: Iterable[str | Path],
    *,
    lookback_sessions: int,
    min_paper_fills: int,
    paper_fills: int,
) -> dict[str, object]:
    records, diagnostics = read_paper_auto_session_records(ledger_inputs)
    lookback = max(int(lookback_sessions), 1)
    window = records[-lookback:]
    classified: list[dict[str, object]] = []
    blocker_count = 0
    disagreement_count = 0
    clean_count = 0
    for record in window:
        classification, reasons = classify_paper_auto_session(record)
        if classification == "CLEAN":
            clean_count += 1
        else:
            blocker_count += 1
        if "llm_baseline_disagreement" in set(reasons):
            disagreement_count += 1
        classified.append(
            {
                "session_id": record.get("session_id"),
                "as_of_date": record.get("as_of_date"),
                "classification": classification,
                "blockers": reasons,
            }
        )
    total = len(window)
    return {
        "lookback_sessions": lookback,
        "total_sessions": total,
        "clean_sessions": clean_count,
        "blocked_sessions": blocker_count,
        "blocker_rate_pct": _rate_pct(blocker_count, total),
        "llm_disagreement_sessions": disagreement_count,
        "llm_disagreement_rate_pct": _rate_pct(disagreement_count, total),
        "clean_session_trend": _clean_session_trend(classified),
        "fill_sufficiency": {
            "paper_fills": paper_fills,
            "min_paper_fills": int(min_paper_fills),
            "sufficient": paper_fills >= int(min_paper_fills),
        },
        "diagnostics": diagnostics,
        "records": classified,
    }


def _trend_findings(
    *,
    quality_trend: Mapping[str, object],
    thresholds: Mapping[str, object],
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    blockers: list[str] = []
    for diagnostic in _object_list(quality_trend.get("diagnostics")):
        if isinstance(diagnostic, Mapping) and str(diagnostic.get("severity") or "").upper() == "ERROR":
            blockers.append(str(diagnostic.get("code") or "session_ledger_error"))
    max_blocker = _float_or_none(thresholds.get("max_blocker_rate_pct"))
    blocker_rate = _float_or_none(quality_trend.get("blocker_rate_pct"))
    if max_blocker is not None and blocker_rate is not None and blocker_rate > max_blocker:
        blockers.append("blocker_rate_exceeds_threshold")
    max_disagreement = _float_or_none(thresholds.get("max_llm_disagreement_rate_pct"))
    disagreement_rate = _float_or_none(quality_trend.get("llm_disagreement_rate_pct"))
    if max_disagreement is not None and disagreement_rate is not None and disagreement_rate > max_disagreement:
        blockers.append("llm_disagreement_rate_exceeds_threshold")
    return _dedupe(warnings), _dedupe(blockers)


def _clean_session_trend(records: Sequence[Mapping[str, object]]) -> str:
    if len(records) < 2:
        return "INSUFFICIENT_DATA"
    midpoint = len(records) // 2
    first = records[:midpoint]
    second = records[midpoint:]
    first_rate = _clean_rate(first)
    second_rate = _clean_rate(second)
    if second_rate > first_rate + 0.05:
        return "IMPROVING"
    if second_rate < first_rate - 0.05:
        return "DECLINING"
    return "STABLE"


def _clean_rate(records: Sequence[Mapping[str, object]]) -> float:
    if not records:
        return 0.0
    clean = sum(1 for record in records if record.get("classification") == "CLEAN")
    return clean / len(records)


def _rate_pct(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return count / total * 100.0


def _status_for_quality(quality_status: str) -> str:
    if quality_status == "PASS":
        return "OK"
    if quality_status == "BLOCKED":
        return "CRITICAL"
    return "WARN"


def _error_payload(*, as_of_date: str, generated_at: str, message: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": "ERROR",
        "errors": [{"code": "invalid_strategy_quality_input", "message": redact_secrets(message, env={})}],
        "authority": {
            "model_promoted": False,
            "risk_changed": False,
            "orders_submitted": False,
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


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in {None, ""}]
    return [str(value)]


def _int_value(value: object, *, default: int) -> int:
    if value in {None, ""}:
        return default
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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
