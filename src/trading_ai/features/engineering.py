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
    "true_range",
    "atr_14",
    "relative_volume_20",
    "close_to_sma_20",
    "close_to_sma_60",
    "vol_adjusted_momentum_20",
    "vol_adjusted_momentum_60",
    "vol_adjusted_momentum_120",
    # Short-window features keep unit-test and research fixtures usable.
    "momentum_2",
    "realized_volatility_3",
    "relative_volume_2",
    "close_to_sma_2",
    "vol_adjusted_momentum_2",
)

# Additional technical indicator features (not in baseline model — require retraining).
EXTENDED_FEATURE_CANDIDATES: tuple[str, ...] = (
    "rsi_14",
    "macd_hist",
    "bb_pct_b",
)


def default_model_feature_names(records: list[dict[str, object]]) -> tuple[str, ...]:
    names = tuple(name for name in DEFAULT_MODEL_FEATURE_CANDIDATES if has_finite_feature_value(records, name))
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
    # Extended technical indicator windows (disabled by default to preserve baseline model compatibility)
    rsi_window: int = 0       # 0 = disabled; set to 14 to enable
    macd_fast: int = 0        # 0 = disabled; set to 12 to enable (requires macd_slow and macd_signal)
    macd_slow: int = 26
    macd_signal: int = 9
    bb_window: int = 0        # 0 = disabled; set to 20 to enable
    bb_n_std: float = 2.0


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
                output[f"close_to_sma_{window}"] = close / moving_average - 1.0 if moving_average else None

            recent_returns = returns[-cfg.volatility_window :]
            volatility = stdev(recent_returns) * math.sqrt(cfg.periods_per_year) if len(recent_returns) >= 2 else None
            output[f"realized_volatility_{cfg.volatility_window}"] = volatility
            for window, momentum in momentum_values.items():
                output[f"vol_adjusted_momentum_{window}"] = (
                    momentum / volatility if momentum is not None and volatility and volatility > 0 else None
                )
            recent_closes = closes[-cfg.drawdown_window :]
            output[f"rolling_drawdown_{cfg.drawdown_window}"] = _window_drawdown(recent_closes)
            output["daily_range"] = (_as_float(row["high"]) - _as_float(row["low"])) / close if close else None
            recent_volumes = volumes[-cfg.relative_volume_window :]
            recent_volume_mean = _mean(recent_volumes) if recent_volumes else None
            output[f"relative_volume_{cfg.relative_volume_window}"] = (
                volume / recent_volume_mean if recent_volume_mean is not None and recent_volume_mean > 0 else None
            )

            # Extended technical indicators (only computed when window > 0 in config)
            if cfg.rsi_window > 0:
                output[f"rsi_{cfg.rsi_window}"] = _rsi(closes, period=cfg.rsi_window)
            if cfg.macd_fast > 0:
                output["macd_hist"] = _macd_hist(closes, fast=cfg.macd_fast, slow=cfg.macd_slow, signal_period=cfg.macd_signal)
            if cfg.bb_window > 0:
                output["bb_pct_b"] = _bb_pct_b(closes, period=cfg.bb_window, n_std=cfg.bb_n_std)

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


def _true_range(*, high: float, low: float, previous_close: float | None) -> float:
    if previous_close is None:
        return max(high - low, 0.0)
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def has_finite_feature_value(records: list[dict[str, object]], name: str) -> bool:
    """Return True if any record carries a finite numeric value for ``name``.

    Single source of truth for "is this feature usable?"; shared by the default
    feature selection here and by the CLI's explicit ``--feature-names``
    validation so the two never drift apart.
    """
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


def _ewm_last(values: list[float], alpha: float) -> float | None:
    """EWM (exponential weighted mean) of a series, returning the last value."""
    if not values:
        return None
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1.0 - alpha) * result
    return result


def _rsi(closes: list[float], period: int) -> float | None:
    """RSI using Wilder smoothing (alpha=1/period). Returns None until period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    alpha = 1.0 / period
    avg_gain = _ewm_last(gains, alpha)
    avg_loss = _ewm_last(losses, alpha)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd_hist(closes: list[float], fast: int, slow: int, signal_period: int) -> float | None:
    """MACD histogram = (MACD line) - (signal line). Returns None until enough data."""
    if len(closes) < slow:
        return None
    alpha_fast = 2.0 / (fast + 1)
    alpha_slow = 2.0 / (slow + 1)
    alpha_sig = 2.0 / (signal_period + 1)
    ema_fast = closes[0]
    ema_slow = closes[0]
    macd_values: list[float] = []
    for price in closes[1:]:
        ema_fast = alpha_fast * price + (1.0 - alpha_fast) * ema_fast
        ema_slow = alpha_slow * price + (1.0 - alpha_slow) * ema_slow
        macd_values.append(ema_fast - ema_slow)
    if not macd_values:
        return None
    signal_line = macd_values[0]
    for m in macd_values[1:]:
        signal_line = alpha_sig * m + (1.0 - alpha_sig) * signal_line
    return macd_values[-1] - signal_line


def _bb_pct_b(closes: list[float], period: int, n_std: float) -> float | None:
    """Bollinger Band %B = (close - lower) / (upper - lower). Returns None until period closes."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = _mean(window)
    variance = sum((x - mid) ** 2 for x in window) / len(window)
    std = math.sqrt(variance)
    if std == 0.0:
        return None
    upper = mid + n_std * std
    lower = mid - n_std * std
    band_width = upper - lower
    if band_width == 0.0:
        return None
    return (closes[-1] - lower) / band_width


DEFAULT_CROSS_SECTIONAL_COLUMNS: tuple[str, ...] = (
    "return_1d",
    "momentum_20",
    "momentum_60",
    "rsi_14",
)


def add_cross_sectional_features(
    records: list[dict[str, object]],
    *,
    columns: tuple[str, ...] = DEFAULT_CROSS_SECTIONAL_COLUMNS,
) -> list[dict[str, object]]:
    """Add cross-sectional (within-date) rank and z-score features.

    For each requested base ``col`` and each date (``timestamp``) group with at
    least 2 rows that carry a finite value for ``col``, two features are added
    to those rows (rows whose ``col`` is None or non-finite are skipped for
    that col — the key is omitted, consistent with how ``_extract_features``
    handles absent feature values):

    * ``xs_rank_<col>`` — average percentile rank in ``[0.0, 1.0]``:
      for a group of size ``N`` and a value ``v`` ranked ``r`` (1-based,
      ties get the average of the tied positions), the output is
      ``(r - 1) / (N - 1)`` when ``N > 1`` (and ``0.5`` when ``N == 1``,
      though single-row groups are skipped per the degeneracy rule below).
      Ties therefore collapse to a single shared rank that is the midpoint
      of their tied positions, normalized by ``(N - 1)``.
    * ``xs_z_<col>`` — z-score using the **population** mean and std
      (``std = sqrt(sum((x - mean)^2) / N)``) over the group's finite
      values. When ``std <= 1e-12`` (degenerate constant column on that
      date), the z-score is set to ``0.0`` for all rows in the group.

    Degenerate cases: a date group with fewer than 2 rows that carry a
    finite value for ``col`` does not produce ``xs_rank_<col>`` /
    ``xs_z_<col>`` for that col. The input rows are never mutated; new
    shallow-copied dicts are returned and the output preserves the input
    order.

    Anti-leakage: these features use only information from the SAME
    ``timestamp`` group (contemporaneous cross-section). No future
    timestamps contribute to a row's cross-sectional features.

    NOTE: rank normalization uses ``(N - 1)`` so the strict minimum (0.0) and
    strict maximum (1.0) are reachable. With average ranks for ties the
    tied value lands exactly at the midpoint of its tied positions.
    """
    out: list[dict[str, object]] = [dict(row) for row in records]
    # Group indices (in the output list) by timestamp.
    groups: dict[object, list[int]] = {}
    for index, row in enumerate(out):
        groups.setdefault(row.get("timestamp"), []).append(index)

    for col in columns:
        rank_key = f"xs_rank_{col}"
        z_key = f"xs_z_{col}"
        for indices in groups.values():
            # Collect (output_index, value) for rows with a finite value.
            finite: list[tuple[int, float]] = []
            for idx in indices:
                value = _as_finite_or_none(out[idx].get(col))
                if value is not None:
                    finite.append((idx, value))
            n = len(finite)
            if n < 2:
                # Degenerate group/col: skip — no cross-section to rank.
                continue

            # Average rank (1-based) for ties, then normalize by (N - 1).
            # We sort values; tied values share the mean of their sorted positions.
            ordered = sorted(finite, key=lambda item: item[1])
            ranks_1_based: list[float] = [0.0] * n
            i = 0
            while i < n:
                j = i
                while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
                    j += 1
                # Positions i..j (1-based) are tied; assign their mean.
                avg = (i + 1 + j + 1) / 2.0  # mean of 1-based positions
                for k in range(i, j + 1):
                    ranks_1_based[k] = avg
                i = j + 1

            # Population mean and std for z-score.
            values = [v for _, v in ordered]
            mean = sum(values) / n
            variance = sum((v - mean) ** 2 for v in values) / n
            std = math.sqrt(variance)
            denom = n - 1
            for (idx, value), rank1 in zip(ordered, ranks_1_based):
                out[idx][rank_key] = (rank1 - 1.0) / denom
                out[idx][z_key] = 0.0 if std <= 1e-12 else (value - mean) / std

    return out


def _as_finite_or_none(value: object) -> float | None:
    """Return a finite float or None (None / non-finite / non-numeric → None)."""
    if value is None or value == "":
        return None
    try:
        as_float = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(as_float):
        return None
    return as_float
