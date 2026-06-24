"""Pure-Python metrics for broker-free research notebooks and tests."""

from __future__ import annotations

import math
from collections.abc import Iterable


def _as_float_list(values: Iterable[float]) -> list[float]:
    result = [float(value) for value in values]
    if not result:
        raise ValueError("at least one return is required")
    return result


def cumulative_return(period_returns: Iterable[float]) -> float:
    """Compound period returns into a total return."""

    equity = 1.0
    for period_return in _as_float_list(period_returns):
        equity *= 1.0 + period_return
    return equity - 1.0


def max_drawdown(period_returns: Iterable[float]) -> float:
    """Return the largest peak-to-trough loss as a positive fraction."""

    equity = 1.0
    peak = 1.0
    worst = 0.0
    for period_return in _as_float_list(period_returns):
        equity *= 1.0 + period_return
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def annualized_sharpe(
    period_returns: Iterable[float],
    *,
    periods_per_year: int,
    risk_free_rate_per_period: float = 0.0,
) -> float:
    """Compute annualized Sharpe using sample volatility."""

    returns = [value - risk_free_rate_per_period for value in _as_float_list(period_returns)]
    if len(returns) < 2:
        return 0.0

    mean_return = sum(returns) / len(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
    volatility = math.sqrt(variance)
    if volatility == 0.0:
        return 0.0
    return mean_return / volatility * math.sqrt(periods_per_year)


def estimate_slippage_bps(*, fill_price: float, reference_price: float, side: str) -> float:
    """Realized slippage in basis points; positive means a worse-than-reference fill.

    For a buy, paying above the reference is adverse (positive); for a sell,
    receiving below the reference is adverse (positive). Use this to compare real
    paper fills against the simulated cost assumption before scaling capital.
    """

    if reference_price <= 0:
        return 0.0
    raw = (fill_price - reference_price) / reference_price
    signed = raw if side.strip().lower() == "buy" else -raw
    return signed * 10_000.0


def volatility_target_weight(
    *,
    realized_annual_volatility: float,
    target_annual_volatility: float,
    max_leverage: float,
) -> float:
    """Return a non-negative volatility-target position weight capped by leverage."""

    if realized_annual_volatility <= 0.0 or target_annual_volatility <= 0.0:
        return 0.0
    if max_leverage < 0.0:
        raise ValueError("max_leverage must be non-negative")
    return min(target_annual_volatility / realized_annual_volatility, max_leverage)
