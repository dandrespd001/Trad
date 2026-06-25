"""Champion/challenger promotion policy for model runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

ECONOMIC_STATUS_REVIEWABLE = "REVIEWABLE"
ECONOMIC_STATUS_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class PromotionPolicy:
    min_accuracy_lift: float = 0.02
    min_test_samples: int = 30


@dataclass(frozen=True)
class PromotionDecision:
    approved: bool
    reasons: tuple[str, ...]
    actions: tuple[str, ...]
    challenger_accuracy: float
    baseline_accuracy: float
    accuracy_lift: float
    test_samples: int

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["actions"] = list(self.actions)
        return payload


@dataclass(frozen=True)
class EconomicPromotionPolicy:
    primary_metric: str = "calmar"
    min_calmar: float = 0.0
    min_net_return_after_costs: float = 0.0
    max_drawdown_pct: float = 0.12
    max_turnover: float = 200.0
    max_estimated_costs: float = 0.05
    min_trade_count: float = 100.0
    min_walk_forward_stability: float = 0.50
    min_oos_windows: float = 0.0


@dataclass(frozen=True)
class EconomicPromotionDecision:
    reviewable: bool
    status: str
    reasons: tuple[str, ...]
    actions: tuple[str, ...]
    primary_metric: str
    calmar: float
    net_return_after_costs: float
    max_drawdown: float
    turnover: float
    estimated_costs: float
    trade_count: float
    walk_forward_stability: float
    walk_forward_window_count: float

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["actions"] = list(self.actions)
        payload["metrics"] = {
            "primary_metric": self.primary_metric,
            "calmar": self.calmar,
            "net_return_after_costs": self.net_return_after_costs,
            "max_drawdown": self.max_drawdown,
            "turnover": self.turnover,
            "estimated_costs": self.estimated_costs,
            "trade_count": self.trade_count,
            "walk_forward_stability": self.walk_forward_stability,
            "walk_forward_window_count": self.walk_forward_window_count,
        }
        return payload


def evaluate_promotion(
    *,
    challenger_metrics: Mapping[str, object],
    baseline_metrics: Mapping[str, object],
    policy: PromotionPolicy,
) -> PromotionDecision:
    challenger_accuracy = _float_metric(challenger_metrics.get("accuracy", 0.0))
    baseline_accuracy = _float_metric(baseline_metrics.get("accuracy", 0.0))
    sample_count = int(_float_metric(challenger_metrics.get("sample_count", 0)))
    lift = challenger_accuracy - baseline_accuracy
    reasons: list[str] = []

    if sample_count < policy.min_test_samples:
        reasons.append("insufficient_test_samples")
    if lift < policy.min_accuracy_lift:
        reasons.append("insufficient_accuracy_lift")

    if reasons:
        return PromotionDecision(
            approved=False,
            reasons=tuple(reasons),
            actions=("keep_current_champion",),
            challenger_accuracy=challenger_accuracy,
            baseline_accuracy=baseline_accuracy,
            accuracy_lift=lift,
            test_samples=sample_count,
        )
    return PromotionDecision(
        approved=True,
        reasons=(),
        actions=("eligible_for_paper_challenger",),
        challenger_accuracy=challenger_accuracy,
        baseline_accuracy=baseline_accuracy,
        accuracy_lift=lift,
        test_samples=sample_count,
    )


def evaluate_economic_promotion(
    *,
    metrics: Mapping[str, object],
    policy: EconomicPromotionPolicy,
) -> EconomicPromotionDecision:
    net_return = _float_metric(
        metrics.get("net_return_after_costs", metrics.get("net_cagr_after_estimated_costs", 0.0))
    )
    max_drawdown = abs(_float_metric(metrics.get("max_drawdown", 0.0)))
    turnover = _float_metric(metrics.get("turnover", 0.0))
    estimated_costs = _float_metric(metrics.get("estimated_costs", 0.0))
    trade_count = _float_metric(metrics.get("trade_count", 0.0))
    walk_forward_stability = _float_metric(metrics.get("walk_forward_stability", 0.0))
    walk_forward_window_count = _float_metric(
        metrics.get("walk_forward_window_count", metrics.get("oos_window_count", 0.0))
    )
    calmar = _calmar(net_return, max_drawdown)

    reasons: list[str] = []
    if calmar < policy.min_calmar:
        reasons.append("calmar_below_minimum")
    if net_return <= 0.0:
        reasons.append("net_return_after_costs_not_positive")
    elif net_return < policy.min_net_return_after_costs:
        reasons.append("net_return_after_costs_below_minimum")
    if max_drawdown > policy.max_drawdown_pct:
        reasons.append("max_drawdown_above_limit")
    if turnover > policy.max_turnover:
        reasons.append("turnover_above_limit")
    if estimated_costs > policy.max_estimated_costs:
        reasons.append("estimated_costs_above_limit")
    if trade_count < policy.min_trade_count:
        reasons.append("insufficient_trade_count")
    if walk_forward_stability < policy.min_walk_forward_stability:
        reasons.append("walk_forward_stability_below_minimum")
    if walk_forward_window_count < policy.min_oos_windows:
        reasons.append("insufficient_oos_windows")

    reviewable = not reasons
    return EconomicPromotionDecision(
        reviewable=reviewable,
        status=ECONOMIC_STATUS_REVIEWABLE if reviewable else ECONOMIC_STATUS_BLOCKED,
        reasons=tuple(reasons),
        actions=("review_paper_challenger",) if reviewable else ("keep_current_champion",),
        primary_metric=policy.primary_metric,
        calmar=calmar,
        net_return_after_costs=net_return,
        max_drawdown=max_drawdown,
        turnover=turnover,
        estimated_costs=estimated_costs,
        trade_count=trade_count,
        walk_forward_stability=walk_forward_stability,
        walk_forward_window_count=walk_forward_window_count,
    )


def rank_economic_candidates(
    candidates: Sequence[Mapping[str, object]],
    *,
    policy: EconomicPromotionPolicy | None = None,
) -> list[dict[str, object]]:
    resolved_policy = policy or EconomicPromotionPolicy()
    ranked: list[dict[str, object]] = []
    for candidate in candidates:
        payload = dict(candidate)
        metrics = dict(_mapping(payload.get("metrics")))
        gate = _mapping(payload.get("economic_gate"))
        if gate:
            gate_metrics = _mapping(gate.get("metrics"))
            metrics = {**metrics, **gate_metrics}
            reviewable = bool(gate.get("reviewable")) or str(gate.get("status")) == ECONOMIC_STATUS_REVIEWABLE
        else:
            decision = evaluate_economic_promotion(metrics=metrics, policy=resolved_policy)
            gate = decision.to_dict()
            metrics = {**metrics, **_mapping(gate.get("metrics"))}
            reviewable = decision.reviewable
        payload["economic_gate"] = dict(gate)
        payload["calmar"] = _float_metric(metrics.get("calmar", 0.0))
        payload["net_return_after_costs"] = _float_metric(metrics.get("net_return_after_costs", 0.0))
        payload["max_drawdown"] = abs(_float_metric(metrics.get("max_drawdown", 0.0)))
        payload["turnover"] = _float_metric(metrics.get("turnover", 0.0))
        payload["walk_forward_stability"] = _float_metric(metrics.get("walk_forward_stability", 0.0))
        payload["_economic_sort"] = (
            reviewable,
            payload["calmar"],
            payload["net_return_after_costs"],
            -float(cast(Any, payload["max_drawdown"])),
            -_float_metric(metrics.get("estimated_costs", 0.0)),
            -float(cast(Any, payload["turnover"])),
            payload["walk_forward_stability"],
        )
        ranked.append(payload)

    ranked.sort(key=lambda item: cast(tuple[object, ...], item["_economic_sort"]), reverse=True)
    for index, candidate in enumerate(ranked, start=1):
        candidate.pop("_economic_sort", None)
        candidate["economic_rank"] = index
    return ranked


def _float_metric(value: object) -> float:
    return float(cast(Any, value or 0.0))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _calmar(net_return: float, max_drawdown: float) -> float:
    if max_drawdown > 1e-12:
        return net_return / max_drawdown
    if net_return > 0:
        return net_return / 1e-12
    return 0.0
