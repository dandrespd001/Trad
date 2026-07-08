"""Pure-Python metrics for broker-free research notebooks and tests."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable

# Euler-Mascheroni constant, used for the expected maximum of N standard-normal
# trials in the Deflated Sharpe Ratio benchmark (López de Prado 2014).
_EULER_MASCHERONI = 0.5772156649015329


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


def _normal_cdf(x: float) -> float:
    """Standard-normal CDF Φ(x)."""

    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _normal_ppf(p: float) -> float:
    """Inverse standard-normal CDF Φ⁻¹(p) via Acklam's rational approximation.

    Relative error < 1.15e-9 across the open interval (0, 1) (Acklam 2000),
    which is well within what the DSR benchmark needs. Raises for p outside (0, 1).
    """

    if not 0.0 < p < 1.0:
        raise ValueError("p must be in the open interval (0, 1)")
    # Coefficients for Acklam's approximation.
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def probabilistic_sharpe_ratio(
    *,
    observed_sharpe: float,
    n_observations: int,
    benchmark_sharpe: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Probabilistic Sharpe Ratio (Bailey & López de Prado).

    Probability that the true Sharpe exceeds ``benchmark_sharpe`` given the
    observed Sharpe and the sample's skew/kurtosis. ``observed_sharpe`` and
    ``benchmark_sharpe`` must be expressed in the SAME frequency as
    ``n_observations`` (i.e. per-observation, NOT annualized).
    """

    if n_observations < 2:
        raise ValueError("n_observations must be >= 2")
    denominator = 1.0 - skew * observed_sharpe + ((kurtosis - 1.0) / 4.0) * observed_sharpe ** 2
    if denominator <= 0.0:
        raise ValueError("PSR denominator is non-positive (degenerate moments)")
    numerator = (observed_sharpe - benchmark_sharpe) * math.sqrt(n_observations - 1)
    return _normal_cdf(numerator / math.sqrt(denominator))


def deflated_sharpe_ratio(
    *,
    observed_sharpe: float,
    n_observations: int,
    n_trials: int,
    variance_of_trial_sharpes: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Deflated Sharpe Ratio: PSR against a multiple-testing benchmark.

    The benchmark is the expected maximum Sharpe of ``n_trials`` independent
    trials under the null (López de Prado 2014):

        SR_bench = sqrt(var) * ((1-γ)·Φ⁻¹(1 - 1/N) + γ·Φ⁻¹(1 - 1/(N·e)))

    With ``n_trials == 1`` the benchmark is 0 and DSR reduces to PSR against 0.
    """

    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if variance_of_trial_sharpes < 0.0:
        raise ValueError("variance_of_trial_sharpes must be non-negative")
    if n_trials == 1:
        benchmark = 0.0
    else:
        n = float(n_trials)
        gamma = _EULER_MASCHERONI
        expected_max = (1.0 - gamma) * _normal_ppf(1.0 - 1.0 / n) + gamma * _normal_ppf(
            1.0 - 1.0 / (n * math.e)
        )
        benchmark = math.sqrt(variance_of_trial_sharpes) * expected_max
    return probabilistic_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        n_observations=n_observations,
        benchmark_sharpe=benchmark,
        skew=skew,
        kurtosis=kurtosis,
    )


def monte_carlo_drawdown(
    returns: Iterable[float],
    *,
    n_simulations: int = 1000,
    seed: int = 0,
    method: str = "resample",
) -> dict[str, float]:
    """Monte Carlo distribution of max drawdown over reordered return paths.

    ``method="resample"`` bootstraps with replacement; ``method="shuffle"``
    permutes the exact multiset of returns. Deterministic given ``seed``.
    Returns p5/p50/p95/mean/worst of the simulated max-drawdown distribution.
    """

    values = _as_float_list(returns)
    if n_simulations < 1:
        raise ValueError("n_simulations must be >= 1")
    if method not in ("resample", "shuffle"):
        raise ValueError("method must be 'resample' or 'shuffle'")
    rng = random.Random(seed)
    size = len(values)
    drawdowns: list[float] = []
    for _ in range(n_simulations):
        if method == "resample":
            path = [values[rng.randrange(size)] for _ in range(size)]
        else:
            path = values[:]
            rng.shuffle(path)
        drawdowns.append(max_drawdown(path))
    drawdowns.sort()

    def _percentile(fraction: float) -> float:
        # Nearest-rank percentile on the sorted sample.
        rank = min(size_dd - 1, max(0, int(math.ceil(fraction * size_dd)) - 1))
        return drawdowns[rank]

    size_dd = len(drawdowns)
    return {
        "p5": _percentile(0.05),
        "p50": _percentile(0.50),
        "p95": _percentile(0.95),
        "mean": sum(drawdowns) / size_dd,
        "worst": drawdowns[-1],
        "n_simulations": float(n_simulations),
    }
