"""Pure exposure-matched benchmark helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable


def exposure_matched_spy_returns(
    spy_returns: Iterable[float],
    gross_exposures: Iterable[float],
    *,
    one_way_cost_bps: float,
) -> tuple[float, ...]:
    """Return net SPY returns under an already-aligned exposure schedule.

    The caller owns session alignment and next-open timing. This helper starts
    from cash, charges one-way cost only on changes in effective exposure, and
    deliberately does not add a terminal liquidation.
    """

    returns = _finite_numeric_tuple(spy_returns, name="spy_returns")
    exposures = _finite_numeric_tuple(gross_exposures, name="gross_exposures")
    if not returns:
        raise ValueError("spy_returns and gross_exposures must not be empty")
    if len(returns) != len(exposures):
        raise ValueError("spy_returns and gross_exposures must have identical lengths")

    cost_bps = _finite_number(one_way_cost_bps, name="one_way_cost_bps")
    if cost_bps < 0.0 or cost_bps >= 10_000.0:
        raise ValueError("one_way_cost_bps must be in [0, 10000)")

    result: list[float] = []
    previous_exposure = 0.0
    for index, (spy_return, exposure) in enumerate(
        zip(returns, exposures, strict=True)
    ):
        if spy_return <= -1.0:
            raise ValueError(f"spy_returns[{index}] must be greater than -1")
        if exposure < 0.0 or exposure > 1.0:
            raise ValueError(f"gross_exposures[{index}] must be in [0, 1]")
        turnover_cost = (
            abs(exposure - previous_exposure) * cost_bps / 10_000.0
        )
        net_return = (1.0 - turnover_cost) * (
            1.0 + exposure * spy_return
        ) - 1.0
        if not math.isfinite(net_return) or net_return <= -1.0:
            raise ValueError(f"benchmark return at index {index} is invalid")
        result.append(net_return)
        previous_exposure = exposure
    return tuple(result)


def _finite_numeric_tuple(
    values: Iterable[float],
    *,
    name: str,
) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of finite numbers")
    try:
        materialized = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable of finite numbers") from exc
    return tuple(
        _finite_number(value, name=f"{name}[{index}]")
        for index, value in enumerate(materialized)
    )


def _finite_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted
