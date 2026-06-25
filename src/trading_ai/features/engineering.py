"""Leakage-aware OHLCV feature engineering."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import stdev
from typing import Any, cast

DEFAULT_MODEL_FEATURE_CANDIDATES: tuple[str, ...] = (
    "return_1d",
    "momentum_20",
    "momentum_60",
    "momentum_120",
    "realized_volatility_20",
    "rolling_drawdown_20",
    "daily_range",
    "intraday_range",
    "true_range",
    "atr_14",
    "relative_volume_20",
    "close_to_sma_20",
    "close_to_sma_60",
    "trend_regime_20",
    "trend_regime_60",
    "vol_adjusted_momentum_20",
    "vol_adjusted_momentum_60",
    "vol_adjusted_momentum_120",
    # Short-window features keep unit-test and research fixtures usable.
    "momentum_2",
    "realized_volatility_3",
    "relative_volume_2",
    "close_to_sma_2",
    "trend_regime_2",
    "vol_adjusted_momentum_2",
)


def default_model_feature_names(records: list[dict[str, object]]) -> tuple[str, ...]:
    names = tuple(name for name in DEFAULT_MODEL_FEATURE_CANDIDATES if _has_finite_feature_value(records, name))
    if not names:
        raise ValueError("dataset does not contain supported feature columns")
    return names


@dataclass(frozen=True)
class FeatureConfig:
    momentum_windows: tuple[int, ...] = (20, 60, 120)
    volatility_window: int = 20
    drawdown_window: int = 20
    moving_average_windows: tuple[int, ...] = (20, 60)
    relative_volume_window: int = 20
    atr_window: int = 14
    periods_per_year: int = 252


def build_features(
    records: Iterable[Mapping[str, object]],
    config: FeatureConfig | None = None,
) -> list[dict[str, object]]:
    cfg = config or FeatureConfig()
    rows = sorted((dict(row) for row in records), key=lambda row: (str(row["symbol"]), str(row["timestamp"])))
    by_symbol: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)

    featured: list[dict[str, object]] = []
    for symbol_rows in by_symbol.values():
        closes: list[float] = []
        volumes: list[float] = []
        returns: list[float] = []
        true_ranges: list[float] = []
        for row in symbol_rows:
            close = _as_float(row["close"])
            volume = _as_float(row["volume"])
            output = dict(row)

            if closes:
                one_day_return = _safe_return(close, closes[-1])
                if one_day_return is not None:
                    returns.append(one_day_return)
                output["return_1d"] = one_day_return
            else:
                output["return_1d"] = None

            previous_close = closes[-1] if closes else None
            true_range = _true_range(
                high=_as_float(row["high"]),
                low=_as_float(row["low"]),
                previous_close=previous_close,
            )
            true_ranges.append(true_range)
            output["true_range"] = true_range
            recent_true_ranges = true_ranges[-cfg.atr_window :]
            output[f"atr_{cfg.atr_window}"] = (
                _mean(recent_true_ranges) if len(recent_true_ranges) >= cfg.atr_window else None
            )

            closes.append(close)
            volumes.append(volume)

            momentum_values: dict[int, float | None] = {}
            for window in cfg.momentum_windows:
                momentum = _safe_return(close, closes[-window - 1]) if len(closes) > window else None
                momentum_values[window] = momentum
                output[f"momentum_{window}"] = momentum
            for window in cfg.moving_average_windows:
                moving_average = _mean(closes[-window:]) if len(closes) >= window else None
                output[f"sma_{window}"] = moving_average
                close_to_sma = close / moving_average - 1.0 if moving_average else None
                output[f"close_to_sma_{window}"] = close_to_sma
                output[f"trend_regime_{window}"] = _trend_regime(close_to_sma)

            recent_returns = returns[-cfg.volatility_window :]
            volatility = stdev(recent_returns) * math.sqrt(cfg.periods_per_year) if len(recent_returns) >= 2 else None
            output[f"realized_volatility_{cfg.volatility_window}"] = volatility
            for window, momentum in momentum_values.items():
                output[f"vol_adjusted_momentum_{window}"] = (
                    momentum / volatility if momentum is not None and volatility and volatility > 0 else None
                )
            recent_closes = closes[-cfg.drawdown_window :]
            output[f"rolling_drawdown_{cfg.drawdown_window}"] = _window_drawdown(recent_closes)
            daily_range = (_as_float(row["high"]) - _as_float(row["low"])) / close if close else None
            output["daily_range"] = daily_range
            output["intraday_range"] = daily_range
            recent_volumes = volumes[-cfg.relative_volume_window :]
            recent_volume_mean = _mean(recent_volumes) if recent_volumes else None
            output[f"relative_volume_{cfg.relative_volume_window}"] = (
                volume / recent_volume_mean if recent_volume_mean is not None and recent_volume_mean > 0 else None
            )

            featured.append(output)
    return sorted(featured, key=lambda row: (str(row["timestamp"]), str(row["symbol"])))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _as_float(value: object) -> float:
    return float(cast(Any, value))


def _safe_return(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator - 1.0


def _trend_regime(close_to_average: float | None) -> float | None:
    if close_to_average is None:
        return None
    if close_to_average > 0:
        return 1.0
    if close_to_average < 0:
        return -1.0
    return 0.0


def _true_range(*, high: float, low: float, previous_close: float | None) -> float:
    if previous_close is None:
        return max(high - low, 0.0)
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def _has_finite_feature_value(records: list[dict[str, object]], name: str) -> bool:
    for row in records:
        value = row.get(name)
        if value in {None, ""}:
            continue
        try:
            if math.isfinite(_as_float(value)):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _window_drawdown(closes: list[float]) -> float:
    if not closes:
        return 0.0
    peak = max(closes)
    if peak <= 0:
        return 0.0
    return max((peak - close) / peak for close in closes)
