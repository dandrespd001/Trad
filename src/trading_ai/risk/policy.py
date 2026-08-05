"""Deterministic risk gates shared by research and future paper trading."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    max_gross_exposure: float = 1.0
    max_single_position: float = 0.30
    live_trading_allowed: bool = False
    paper_notional_usd: float = 1.0
    paper_stage: str = "CANARY"
    paper_stage_reviewer: str | None = None
    paper_stage_reason: str | None = None
    min_signal_margin: float = 0.05
    max_buy_signals: int = 3
    max_consecutive_error_days: int = 0
    stop_loss_atr_mult: float = 0.0
    take_profit_atr_mult: float = 0.0
    trailing_atr_mult: float = 0.0
    breakeven_trigger_atr_mult: float = 0.0
    breakeven_buffer_atr_mult: float = 0.0
    sizing_mode: str = "fixed_notional"
    target_volatility: float = 0.0
    max_leverage: float = 1.0
    max_price_deviation_pct: float = 0.05


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: list[str]
    actions: list[str]


def evaluate_risk_state(
    *,
    daily_pnl_pct: float,
    current_drawdown_pct: float,
    gross_exposure: float,
    largest_position_weight: float,
    mode: str,
    limits: RiskLimits,
) -> RiskDecision:
    """Evaluate whether a strategy state may continue trading."""

    reasons: list[str] = []
    actions: list[str] = []

    normalized_mode = mode.strip().lower() if isinstance(mode, str) else ""
    if normalized_mode == "live" and not limits.live_trading_allowed:
        reasons.append("live_trading_not_authorized")
        actions.append("disable_trading")
    elif normalized_mode not in {"research", "paper", "live"}:
        reasons.append("unknown_trading_mode")
        actions.append("disable_trading")

    if not _valid_risk_limits(limits):
        reasons.append("invalid_risk_limits")
        actions.append("disable_trading")

    state_values = (
        daily_pnl_pct,
        current_drawdown_pct,
        gross_exposure,
        largest_position_weight,
    )
    if not all(_finite_number(value) for value in state_values):
        reasons.append("risk_state_nonfinite")
        actions.append("disable_trading")
        return RiskDecision(allowed=False, reasons=reasons, actions=_dedupe(actions))
    if not -1 <= daily_pnl_pct <= 1:
        reasons.append("daily_pnl_out_of_range")
        actions.append("disable_trading")
    if not 0 <= current_drawdown_pct <= 1:
        reasons.append("drawdown_out_of_range")
        actions.append("disable_trading")
    if gross_exposure < 0:
        reasons.append("gross_exposure_negative")
        actions.append("disable_trading")
    if largest_position_weight < 0:
        reasons.append("single_position_weight_negative")
        actions.append("disable_trading")

    if "invalid_risk_limits" in reasons:
        return RiskDecision(allowed=False, reasons=reasons, actions=_dedupe(actions))

    if daily_pnl_pct <= -abs(limits.max_daily_loss_pct):
        reasons.append("daily_loss_limit_breached")
        actions.append("disable_trading")

    if current_drawdown_pct >= abs(limits.max_drawdown_pct):
        reasons.append("drawdown_limit_breached")
        actions.append("disable_trading")

    if gross_exposure > limits.max_gross_exposure:
        reasons.append("gross_exposure_limit_breached")
        actions.append("reduce_positions")

    if largest_position_weight > limits.max_single_position:
        reasons.append("single_position_limit_breached")
        actions.append("reduce_positions")

    if not reasons:
        return RiskDecision(allowed=True, reasons=[], actions=["allow"])

    return RiskDecision(allowed=False, reasons=reasons, actions=_dedupe(actions))


def _valid_risk_limits(limits: RiskLimits) -> bool:
    fractions = (
        limits.max_daily_loss_pct,
        limits.max_drawdown_pct,
        limits.max_gross_exposure,
        limits.max_single_position,
    )
    return (
        type(limits.live_trading_allowed) is bool
        and all(_finite_number(value) and 0 <= value <= 1 for value in fractions)
        and limits.max_single_position <= limits.max_gross_exposure
    )


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
