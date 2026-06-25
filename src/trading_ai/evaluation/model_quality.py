"""Model quality policies shared by offline evaluation and paper readiness."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from trading_ai.config import ConfigError, load_yaml_file

QUALITY_MODE_CLASSIFICATION = "classification"
QUALITY_MODE_TRADING_FIRST = "trading_first"


@dataclass(frozen=True)
class ModelQualityPolicy:
    mode: str = QUALITY_MODE_CLASSIFICATION
    primary_metric: str = "calmar"
    min_calmar: float = 0.0
    min_sharpe: float = 1.0
    min_net_cagr: float = 0.05
    max_drawdown_pct: float = 0.12
    max_turnover: float = 200.0
    max_estimated_costs: float = 0.05
    min_trade_count: float = 100.0
    min_walk_forward_stability: float = 0.50
    min_oos_windows: float = 0.0
    intraday: dict[str, object] = field(default_factory=dict)


def load_model_quality_policy(path: str | Path) -> ModelQualityPolicy:
    """Load optional model-quality thresholds from a risk config."""

    try:
        payload = load_yaml_file(path)
    except ConfigError:
        raise
    raw_policy = payload.get("model_quality", {})
    if raw_policy is None or raw_policy == "":
        raw_policy = {}
    if not isinstance(raw_policy, Mapping):
        raise ConfigError("model_quality config must be a mapping")
    mode = str(raw_policy.get("mode", QUALITY_MODE_CLASSIFICATION)).strip().lower()
    if mode not in {QUALITY_MODE_CLASSIFICATION, QUALITY_MODE_TRADING_FIRST}:
        raise ConfigError("model_quality.mode must be classification or trading_first")
    raw_intraday = raw_policy.get("intraday", {})
    if raw_intraday is None or raw_intraday == "":
        raw_intraday = {}
    if not isinstance(raw_intraday, Mapping):
        raise ConfigError("model_quality.intraday config must be a mapping")
    return ModelQualityPolicy(
        mode=mode,
        primary_metric=str(raw_policy.get("primary_metric", "calmar")).strip().lower() or "calmar",
        min_calmar=_non_negative_float(raw_policy, "min_calmar", 0.0),
        min_sharpe=_non_negative_float(raw_policy, "min_sharpe", 1.0),
        min_net_cagr=_non_negative_float(raw_policy, "min_net_cagr", 0.05),
        max_drawdown_pct=_positive_float(raw_policy, "max_drawdown_pct", 0.12),
        max_turnover=_positive_float(raw_policy, "max_turnover", 200.0),
        max_estimated_costs=_non_negative_float(raw_policy, "max_estimated_costs", 0.05),
        min_trade_count=_non_negative_float(raw_policy, "min_trade_count", 100.0),
        min_walk_forward_stability=_non_negative_float(raw_policy, "min_walk_forward_stability", 0.50),
        min_oos_windows=_non_negative_float(raw_policy, "min_oos_windows", 0.0),
        intraday=dict(raw_intraday),
    )


def classification_gate_payload(promotion: Mapping[str, object]) -> dict[str, object]:
    reasons = _string_list(promotion.get("reasons"))
    return {
        "status": "PASS" if bool(promotion.get("eligible_for_paper_challenger")) else "FAIL",
        "blocking": True,
        "reasons": reasons,
        "metrics": {
            "accuracy": _float(promotion.get("challenger_accuracy")),
            "baseline_accuracy": _float(promotion.get("baseline_accuracy")),
            "accuracy_lift": _float(promotion.get("accuracy_lift")),
            "test_samples": _float(promotion.get("test_samples")),
        },
        "policy": dict(_mapping(promotion.get("policy"))),
    }


def trading_gate_payload(
    *,
    backtest_metrics: Mapping[str, object],
    costs: Mapping[str, object],
    policy: ModelQualityPolicy,
) -> dict[str, object]:
    sharpe = _float(backtest_metrics.get("sharpe"))
    max_drawdown = _float(backtest_metrics.get("max_drawdown"))
    turnover = _float(backtest_metrics.get("turnover"))
    trade_count = _float(backtest_metrics.get("trade_count"))
    estimated_costs = _float(backtest_metrics.get("estimated_costs"))
    net_cagr = _float(costs.get("net_cagr_after_estimated_costs"))
    reasons: list[str] = []
    if sharpe < policy.min_sharpe:
        reasons.append("sharpe_below_minimum")
    if net_cagr < policy.min_net_cagr:
        reasons.append("net_cagr_below_minimum")
    if max_drawdown > policy.max_drawdown_pct:
        reasons.append("max_drawdown_above_limit")
    if turnover > policy.max_turnover:
        reasons.append("turnover_above_limit")
    if estimated_costs > policy.max_estimated_costs:
        reasons.append("estimated_costs_above_limit")
    if trade_count < policy.min_trade_count:
        reasons.append("insufficient_trade_count")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "blocking": policy.mode == QUALITY_MODE_TRADING_FIRST,
        "reasons": reasons,
        "metrics": {
            "sharpe": sharpe,
            "net_cagr_after_estimated_costs": net_cagr,
            "max_drawdown": max_drawdown,
            "turnover": turnover,
            "estimated_costs": estimated_costs,
            "trade_count": trade_count,
        },
        "thresholds": {
            "min_sharpe": policy.min_sharpe,
            "min_net_cagr": policy.min_net_cagr,
            "max_drawdown_pct": policy.max_drawdown_pct,
            "max_turnover": policy.max_turnover,
            "max_estimated_costs": policy.max_estimated_costs,
            "min_trade_count": policy.min_trade_count,
        },
    }


def quality_policy_payload(policy: ModelQualityPolicy) -> dict[str, object]:
    return {
        "mode": policy.mode,
        "primary_metric": policy.primary_metric,
        "min_calmar": policy.min_calmar,
        "min_sharpe": policy.min_sharpe,
        "min_net_cagr": policy.min_net_cagr,
        "max_drawdown_pct": policy.max_drawdown_pct,
        "max_turnover": policy.max_turnover,
        "max_estimated_costs": policy.max_estimated_costs,
        "min_trade_count": policy.min_trade_count,
        "min_walk_forward_stability": policy.min_walk_forward_stability,
        "min_oos_windows": policy.min_oos_windows,
        "intraday": dict(policy.intraday),
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if item not in {None, ""}]


def _float(value: object) -> float:
    if isinstance(value, bool) or value in {None, ""}:
        return 0.0
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _non_negative_float(payload: Mapping[str, object], key: str, default: float) -> float:
    value = _float(payload.get(key, default))
    if value < 0:
        raise ConfigError(f"model_quality.{key} must be non-negative")
    return value


def _positive_float(payload: Mapping[str, object], key: str, default: float) -> float:
    value = _float(payload.get(key, default))
    if value <= 0:
        raise ConfigError(f"model_quality.{key} must be positive")
    return value
