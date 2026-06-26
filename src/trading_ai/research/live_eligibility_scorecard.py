"""Quant/data/model scorecard for live-readiness review.

This module is analysis-only. It does not construct broker clients, read
credentials, or submit orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact


@dataclass(frozen=True)
class LiveEligibilityArtifacts:
    json_path: Path
    markdown_path: Path
    payload: dict[str, object]


def build_live_eligibility_scorecard(
    *,
    data_cutoff: str,
    timezone: str,
    universe: Sequence[str],
    benchmark: str | None,
    fees_bps: float | None,
    slippage_bps: float | None,
    estimated_edge_bps: float | None,
    max_drawdown: float | None,
    turnover: float | None,
    exposure: float | None,
    hit_rate: float | None,
    sharpe: float | None,
    oos_period: Mapping[str, object] | None,
    leakage_checks: Mapping[str, bool] | None,
    assumptions: Sequence[str] = (),
    failure_modes: Sequence[str] = (),
) -> dict[str, object]:
    blockers: list[str] = []
    clean_universe = [str(symbol).strip().upper() for symbol in universe if str(symbol).strip()]
    clean_benchmark = str(benchmark).strip() if benchmark else ""
    fee_value = _float_or_none(fees_bps)
    slippage_value = _float_or_none(slippage_bps)
    edge_value = _float_or_none(estimated_edge_bps)
    clean_oos = dict(oos_period or {})
    clean_leakage = {str(key): bool(value) for key, value in dict(leakage_checks or {}).items()}

    if not clean_universe:
        blockers.append("universe_missing")
    if not clean_benchmark:
        blockers.append("benchmark_missing")
    if fee_value is None:
        blockers.append("fees_bps_missing")
    elif fee_value < 0:
        blockers.append("fees_bps_negative")
    if slippage_value is None:
        blockers.append("slippage_bps_missing")
    elif slippage_value < 0:
        blockers.append("slippage_bps_negative")
    if edge_value is None:
        blockers.append("estimated_edge_bps_missing")
    if not clean_oos.get("start") or not clean_oos.get("end"):
        blockers.append("oos_period_missing")
    if not clean_leakage:
        blockers.append("leakage_checks_missing")
    for name, passed in sorted(clean_leakage.items()):
        if not passed:
            blockers.append(f"leakage_check_failed:{name}")
    for name, value in (
        ("max_drawdown_missing", max_drawdown),
        ("turnover_missing", turnover),
        ("exposure_missing", exposure),
        ("hit_rate_missing", hit_rate),
        ("sharpe_missing", sharpe),
    ):
        if _float_or_none(value) is None:
            blockers.append(name)

    net_edge_bps: float | None = None
    if edge_value is not None and fee_value is not None and slippage_value is not None:
        net_edge_bps = round(edge_value - fee_value - slippage_value, 6)
        if net_edge_bps <= 0:
            blockers.append("edge_not_positive_after_costs")

    return {
        "status": "ELIGIBLE_FOR_LIVE_REVIEW" if not blockers else "BLOCKED",
        "eligible_for_live_review": not blockers,
        "data_cutoff": str(data_cutoff),
        "timezone": str(timezone),
        "universe": clean_universe,
        "benchmark": clean_benchmark or None,
        "fees_bps": fee_value,
        "slippage_bps": slippage_value,
        "estimated_edge_bps": edge_value,
        "net_edge_bps": net_edge_bps,
        "max_drawdown": _float_or_none(max_drawdown),
        "turnover": _float_or_none(turnover),
        "exposure": _float_or_none(exposure),
        "hit_rate": _float_or_none(hit_rate),
        "sharpe": _float_or_none(sharpe),
        "oos_period": clean_oos,
        "leakage_checks": clean_leakage,
        "assumptions": [str(item) for item in assumptions],
        "failure_modes": [str(item) for item in failure_modes],
        "blockers": blockers,
        "safety": {
            "analysis_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
        },
    }


def write_live_eligibility_scorecard(
    *,
    output_dir: str | Path = "reports/tmp/live_eligibility",
    as_of_date: str = "latest",
    **kwargs: Any,
) -> LiveEligibilityArtifacts:
    payload = build_live_eligibility_scorecard(**kwargs)
    directory = Path(output_dir)
    json_path = directory / f"{as_of_date}.json"
    markdown_path = directory / f"{as_of_date}.md"
    write_json_artifact(payload, json_path)
    write_text_artifact(render_live_eligibility_markdown(payload), markdown_path)
    return LiveEligibilityArtifacts(json_path=json_path, markdown_path=markdown_path, payload=payload)


def render_live_eligibility_markdown(payload: Mapping[str, object]) -> str:
    lines = [
        "# Live Eligibility Scorecard",
        "",
        f"- status: {payload.get('status')}",
        f"- eligible_for_live_review: {str(bool(payload.get('eligible_for_live_review'))).lower()}",
        f"- data_cutoff: {payload.get('data_cutoff')}",
        f"- timezone: {payload.get('timezone')}",
        f"- universe: {', '.join(str(item) for item in _sequence(payload.get('universe')))}",
        f"- benchmark: {payload.get('benchmark')}",
        f"- fees_bps: {payload.get('fees_bps')}",
        f"- slippage_bps: {payload.get('slippage_bps')}",
        f"- estimated_edge_bps: {payload.get('estimated_edge_bps')}",
        f"- net_edge_bps: {payload.get('net_edge_bps')}",
        "",
        "## Quant Metrics",
        "",
        f"- sharpe: {payload.get('sharpe')}",
        f"- max_drawdown: {payload.get('max_drawdown')}",
        f"- turnover: {payload.get('turnover')}",
        f"- exposure: {payload.get('exposure')}",
        f"- hit_rate: {payload.get('hit_rate')}",
        "",
        "## Blockers",
        "",
    ]
    blockers = _sequence(payload.get("blockers"))
    if blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("- none")
    lines.extend(["", "## Safety", ""])
    safety = payload.get("safety")
    if isinstance(safety, Mapping):
        lines.extend(f"- {key}: {str(value).lower()}" for key, value in sorted(safety.items()))
    return "\n".join(lines) + "\n"


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sequence(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []
