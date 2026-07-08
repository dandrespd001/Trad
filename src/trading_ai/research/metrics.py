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


def annualized_sortino(
    period_returns: Iterable[float],
    *,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
    target_return: float = 0.0,
) -> float:
    """Compute annualized Sortino using downside deviation.

    Follows the standard finance-text convention: downside deviation is
    ``sqrt(mean(min(r - target, 0)^2))`` computed over the *full* series
    (zero-contributing for non-downside periods), then annualized via
    ``sqrt(periods_per_year)``. The numerator is ``mean - target``.

    Degenerate cases fail-closed (``0.0``) mirroring ``annualized_sharpe``'s
    zero-volatility convention: an empty/single-observation series, and any
    series with no downside variance (all returns at or above target),
    return ``0.0`` rather than ``+inf``/``NaN``. This avoids unbounded
    ratios when the strategy has no observed losses.
    """

    returns = [float(value) - risk_free_rate for value in period_returns]
    if len(returns) < 2:
        return 0.0

    mean_return = sum(returns) / len(returns)
    excess = mean_return - target_return
    downside_sq = sum(min(value - target_return, 0.0) ** 2 for value in returns) / len(returns)
    downside_deviation = math.sqrt(downside_sq)
    if downside_deviation == 0.0:
        return 0.0
    return excess / downside_deviation * math.sqrt(periods_per_year)


def directional_bias(period_returns: Iterable[float]) -> float:
    """Return the fraction of positive returns minus the fraction of negative
    returns, in the closed interval ``[-1, 1]``.

    A value of ``1.0`` means every period had a positive return, ``-1.0``
    means every period had a negative return, and ``0.0`` indicates either
    a balanced series or no data. Zero-return periods are ignored (they do
    not contribute to either side). Empty input yields ``0.0`` (balanced
    unknown) so callers can use the result without an ``is None`` check.
    """

    values = [float(value) for value in period_returns]
    if not values:
        return 0.0
    positive = sum(1 for value in values if value > 0)
    negative = sum(1 for value in values if value < 0)
    total = len(values)
    return (positive - negative) / total


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
