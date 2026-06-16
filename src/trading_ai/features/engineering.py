"""Leakage-aware OHLCV feature engineering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import stdev
from typing import Iterable, Mapping


@dataclass(frozen=True)
class FeatureConfig:
    momentum_windows: tuple[int, ...] = (20, 60, 120)
    volatility_window: int = 20
    drawdown_window: int = 20
    moving_average_windows: tuple[int, ...] = (20, 60)
    relative_volume_window: int = 20
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
        for row in symbol_rows:
            close = float(row["close"])
            volume = float(row["volume"])
            output = dict(row)

            if closes:
                one_day_return = close / closes[-1] - 1.0
                returns.append(one_day_return)
                output["return_1d"] = one_day_return
            else:
                output["return_1d"] = None

            closes.append(close)
            volumes.append(volume)

            for window in cfg.momentum_windows:
                output[f"momentum_{window}"] = close / closes[-window - 1] - 1.0 if len(closes) > window else None
            for window in cfg.moving_average_windows:
                output[f"sma_{window}"] = _mean(closes[-window:]) if len(closes) >= window else None

            recent_returns = returns[-cfg.volatility_window :]
            output[f"realized_volatility_{cfg.volatility_window}"] = (
                stdev(recent_returns) * math.sqrt(cfg.periods_per_year) if len(recent_returns) >= 2 else None
            )
            recent_closes = closes[-cfg.drawdown_window :]
            output[f"rolling_drawdown_{cfg.drawdown_window}"] = _window_drawdown(recent_closes)
            output["daily_range"] = (float(row["high"]) - float(row["low"])) / close if close else None
            recent_volumes = volumes[-cfg.relative_volume_window :]
            output[f"relative_volume_{cfg.relative_volume_window}"] = (
                volume / _mean(recent_volumes) if recent_volumes else None
            )

            featured.append(output)
    return sorted(featured, key=lambda row: (str(row["timestamp"]), str(row["symbol"])))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _window_drawdown(closes: list[float]) -> float:
    if not closes:
        return 0.0
    peak = max(closes)
    if peak <= 0:
        return 0.0
    return max((peak - close) / peak for close in closes)
