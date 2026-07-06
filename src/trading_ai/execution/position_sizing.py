"""Position sizing for paper opens.

``fixed_notional`` keeps the governed CANARY behaviour (a constant USD notional).
``vol_target`` scales the position to a target annual volatility using the
existing :func:`trading_ai.research.metrics.volatility_target_weight`, capped by
the single-position weight and the stage notional cap.

Note: ``vol_target`` requires live account equity and the symbol's realized
volatility at *order-build* time. The current governance flow builds the order
intent offline (dry-run, equity unknown) and replays it with a hash-checked fixed
notional, so enabling ``vol_target`` end-to-end also requires moving sizing to
execute-time and relaxing the fixed-notional approval check. Until then this
helper returns the fixed notional whenever it cannot size safely, so wiring it in
is behaviour-preserving for CANARY.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact
from trading_ai.research.metrics import volatility_target_weight

FIXED_NOTIONAL = "fixed_notional"
VOL_TARGET = "vol_target"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SizingDecision:
    notional_usd: float
    cap_usd: float
    bankroll_usd: float
    stop_loss_pct: float
    slippage_bps: float
    cost_bps: float
    fees_usd: float
    expected_edge_usd: float
    slippage_usd: float
    cost_usd: float
    max_loss_usd: float
    net_edge_usd: float
    blockers: list[str]
    rationale: str
    future_scale_range_usd: list[float]


@dataclass(frozen=True)
class CanarySizingArtifacts:
    json_path: Path
    markdown_path: Path
    payload: dict[str, object]


def compute_open_notional(
    *,
    sizing_mode: str,
    paper_notional_usd: float,
    account_equity: float = 0.0,
    realized_annual_volatility: float | None = None,
    target_volatility: float = 0.0,
    max_leverage: float = 1.0,
    max_single_position: float = 1.0,
    stage_cap_usd: float | None = None,
    simulated_equity_usd: float | None = None,
) -> float:
    """Return the USD notional for a new long position.

    Falls back to ``paper_notional_usd`` whenever ``vol_target`` cannot be applied
    (missing equity, missing/zero realized volatility, or no target volatility).

    ``simulated_equity_usd`` provides an explicit offline equity substitute for
    vol_target in dry-run / research mode. When account_equity <= 0 and
    simulated_equity_usd > 0, sizing uses the simulated value and logs a WARNING.
    When both are <= 0, sizing falls back to fixed_notional with a WARNING so the
    caller is aware the intended vol_target mode was not applied.
    """

    if sizing_mode != VOL_TARGET:
        return float(paper_notional_usd)

    effective_equity = account_equity
    if effective_equity <= 0:
        if simulated_equity_usd is not None and simulated_equity_usd > 0:
            _log.warning(
                "vol_target: account_equity=%.2f; using simulated_equity_usd=%.2f for sizing",
                account_equity,
                simulated_equity_usd,
            )
            effective_equity = simulated_equity_usd
        else:
            _log.warning(
                "vol_target: account_equity=%.2f and no simulated_equity_usd; "
                "falling back to fixed_notional=%.2f",
                account_equity,
                paper_notional_usd,
            )
            return float(paper_notional_usd)

    if (
        realized_annual_volatility is None
        or realized_annual_volatility <= 0
        or target_volatility <= 0
    ):
        return float(paper_notional_usd)
    weight = volatility_target_weight(
        realized_annual_volatility=realized_annual_volatility,
        target_annual_volatility=target_volatility,
        max_leverage=max_leverage,
    )
    if max_single_position > 0:
        weight = min(weight, max_single_position)
    notional = effective_equity * weight
    if stage_cap_usd is not None:
        notional = min(notional, float(stage_cap_usd))
    return max(0.0, notional)


def build_canary_sizing_decision(
    *,
    bankroll_usd: float,
    risk_budget_pct: float,
    stop_loss_pct: float,
    slippage_bps: float,
    cost_bps: float,
    fixed_fees_usd: float,
    expected_edge_bps: float,
    stage_cap_usd: float,
) -> SizingDecision:
    blockers: list[str] = []
    bankroll = float(bankroll_usd)
    risk_budget = float(risk_budget_pct)
    stop = float(stop_loss_pct)
    cap = max(0.0, float(stage_cap_usd))
    fees = max(0.0, float(fixed_fees_usd))
    slippage = max(0.0, float(slippage_bps))
    costs = max(0.0, float(cost_bps))
    edge_bps = float(expected_edge_bps)

    if bankroll <= 0:
        blockers.append("bankroll_usd_invalid")
    if risk_budget <= 0:
        blockers.append("risk_budget_pct_invalid")
    if stop <= 0:
        blockers.append("stop_loss_pct_invalid")
    if cap <= 0:
        blockers.append("stage_cap_usd_invalid")

    if blockers:
        return SizingDecision(
            notional_usd=0.0,
            cap_usd=cap,
            bankroll_usd=bankroll,
            stop_loss_pct=stop,
            slippage_bps=slippage,
            cost_bps=costs,
            fees_usd=fees,
            expected_edge_usd=0.0,
            slippage_usd=0.0,
            cost_usd=0.0,
            max_loss_usd=0.0,
            net_edge_usd=0.0,
            blockers=blockers,
            rationale="Sizing blocked because core bankroll or stop-loss inputs are invalid.",
            future_scale_range_usd=[],
        )

    risk_loss_budget_usd = bankroll * risk_budget
    risk_cap_usd = risk_loss_budget_usd / stop
    cap_usd = min(cap, risk_cap_usd)
    notional_usd = min(1.0, cap_usd)
    expected_edge_usd = notional_usd * edge_bps / 10_000.0
    slippage_usd = notional_usd * slippage / 10_000.0
    cost_usd = notional_usd * costs / 10_000.0
    max_loss_usd = notional_usd * stop
    net_edge_usd = expected_edge_usd - slippage_usd - cost_usd - fees
    if net_edge_usd <= 0:
        blockers.append("edge_net_not_positive")

    future_scale = [50.0, 100.0] if not blockers and cap_usd >= 50.0 else []
    rationale = (
        "First live canary remains USD 1; USD 50-100 scale-up is conditional on clean live evidence, "
        "positive net edge after fixed fees, slippage and costs, and later S13 approval."
    )
    return SizingDecision(
        notional_usd=round(notional_usd, 6),
        cap_usd=round(cap_usd, 6),
        bankroll_usd=bankroll,
        stop_loss_pct=stop,
        slippage_bps=slippage,
        cost_bps=costs,
        fees_usd=fees,
        expected_edge_usd=round(expected_edge_usd, 8),
        slippage_usd=round(slippage_usd, 8),
        cost_usd=round(cost_usd, 8),
        max_loss_usd=round(max_loss_usd, 8),
        net_edge_usd=round(net_edge_usd, 8),
        blockers=blockers,
        rationale=rationale,
        future_scale_range_usd=future_scale,
    )


def write_canary_sizing_report(
    *,
    output_dir: str | Path = "reports/tmp/canary_sizing",
    as_of_date: str,
    **kwargs: object,
) -> CanarySizingArtifacts:
    decision = build_canary_sizing_decision(**kwargs)
    output_root = Path(output_dir) / as_of_date
    json_path = output_root / "sizing.json"
    markdown_path = output_root / "sizing.md"
    payload = _sizing_payload(decision, as_of_date=as_of_date)
    write_json_artifact(payload, json_path)
    write_text_artifact(render_canary_sizing_markdown(payload), markdown_path)
    return CanarySizingArtifacts(json_path=json_path, markdown_path=markdown_path, payload=payload)


def render_canary_sizing_markdown(payload: Mapping[str, object]) -> str:
    decision = payload.get("decision")
    recommendation = payload.get("recommendation")
    decision_map = decision if isinstance(decision, Mapping) else {}
    recommendation_map = recommendation if isinstance(recommendation, Mapping) else {}
    blockers = decision_map.get("blockers")
    blocker_lines = [f"- {item}" for item in blockers] if isinstance(blockers, list) and blockers else ["- none"]
    return "\n".join(
        [
            "# Canary Sizing",
            "",
            f"Status: **{payload.get('status')}**",
            f"First live notional: USD {recommendation_map.get('first_live_notional_usd')}",
            f"Future scale range: {recommendation_map.get('future_scale_range_usd')}",
            "",
            "## Units",
            "",
            f"- Bankroll USD: {decision_map.get('bankroll_usd')}",
            f"- Stop loss pct: {decision_map.get('stop_loss_pct')}",
            f"- Slippage bps: {decision_map.get('slippage_bps')}",
            f"- Cost bps: {decision_map.get('cost_bps')}",
            f"- Fixed fees USD: {decision_map.get('fees_usd')}",
            f"- Expected edge USD: {decision_map.get('expected_edge_usd')}",
            f"- Net edge USD: {decision_map.get('net_edge_usd')}",
            "",
            "## Blockers",
            "",
            *blocker_lines,
            "",
        ]
    )


def _sizing_payload(decision: SizingDecision, *, as_of_date: str) -> dict[str, object]:
    return {
        "as_of_date": as_of_date,
        "status": "READY_FOR_CANARY_REVIEW" if not decision.blockers else "BLOCKED",
        "decision": asdict(decision),
        "recommendation": {
            "first_live_notional_usd": 1.0 if not decision.blockers else None,
            "future_scale_range_usd": decision.future_scale_range_usd,
            "future_scale_condition": "S13 only after clean USD 1 live evidence and fresh review",
        },
        "safety": {
            "recommendation_only": True,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "config_mutated": False,
        },
    }
