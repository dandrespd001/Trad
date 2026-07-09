"""Multi-sleeve risk-parity portfolio combination (Sprint L1).

Combines the daily-return streams of several independent strategy "sleeves"
(e.g. an ETF momentum sleeve and a crypto momentum sleeve) into one portfolio
using causal volatility normalisation with a leverage cap. Diversification
across weakly-correlated sleeves plus de-risking in high-vol regimes (never
levering up) is what lifts the combined risk-adjusted return above any single
sleeve — see docs/evidence-2026-07-08-extended-champion-decision.md §28.

Pure Python (stdlib only); operates on already-computed per-sleeve return
series so it reuses the tested single-universe backtest engine unchanged.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SleeveCombinationResult:
    timestamps: tuple[str, ...]
    daily_returns: tuple[float, ...]
    sleeve_weights_note: str


def _causal_vol_normalized(
    series: Mapping[str, float],
    ordered_dates: list[str],
    *,
    target_daily_vol: float,
    vol_window: int,
    leverage_cap: float,
    warmup: int,
) -> dict[str, float]:
    """Scale each day's return toward ``target_daily_vol`` using ONLY past
    returns (trailing ``vol_window``). The scale is capped at ``leverage_cap``
    so the sleeve is de-risked in high vol but never levered up beyond the cap.
    During the warmup the raw return is used unscaled.
    """
    out: dict[str, float] = {}
    history: list[float] = []
    for date in ordered_dates:
        value = series.get(date)
        if value is None:
            continue
        if len(history) >= warmup:
            trailing = history[-vol_window:]
            vol = statistics.pstdev(trailing) if len(trailing) >= 2 else 0.0
            scale = leverage_cap if vol <= 0.0 else min(target_daily_vol / vol, leverage_cap)
            out[date] = value * scale
        else:
            out[date] = value
        history.append(value)
    return out


def combine_risk_parity_sleeves(
    sleeves: Mapping[str, Mapping[str, float]],
    *,
    target_daily_vol: float = 0.01,
    vol_window: int = 60,
    leverage_cap: float = 1.0,
    warmup: int = 20,
    start_date: str | None = None,
) -> SleeveCombinationResult:
    """Combine per-sleeve daily-return series into one risk-parity portfolio.

    Each sleeve is causally vol-normalised (equal risk contribution) with a
    leverage cap, then the sleeves present on each date are averaged equally.
    On dates where only some sleeves trade (e.g. weekends for a crypto sleeve
    alongside an ETF sleeve) only the available sleeves contribute.

    ``leverage_cap=1.0`` means "de-risk only, never lever up" — the setting that
    kept the ETF+crypto combination's drawdown at ~4% (§28). Raising it above 1
    permits leverage and materially increases tail risk.
    """
    if not sleeves:
        raise ValueError("at least one sleeve is required")
    if target_daily_vol <= 0.0:
        raise ValueError("target_daily_vol must be positive")
    if vol_window < 2:
        raise ValueError("vol_window must be >= 2")
    if leverage_cap <= 0.0:
        raise ValueError("leverage_cap must be positive")

    all_dates = sorted({date for series in sleeves.values() for date in series})
    if start_date is not None:
        all_dates = [date for date in all_dates if date >= start_date]

    normalized = {
        name: _causal_vol_normalized(
            series,
            all_dates,
            target_daily_vol=target_daily_vol,
            vol_window=vol_window,
            leverage_cap=leverage_cap,
            warmup=warmup,
        )
        for name, series in sleeves.items()
    }

    timestamps: list[str] = []
    combined: list[float] = []
    for date in all_dates:
        parts = [normalized[name][date] for name in sleeves if date in normalized[name]]
        if not parts:
            continue
        timestamps.append(date)
        combined.append(sum(parts) / len(parts))

    return SleeveCombinationResult(
        timestamps=tuple(timestamps),
        daily_returns=tuple(combined),
        sleeve_weights_note=(
            f"equal-weight of {len(sleeves)} causally vol-normalized sleeves "
            f"(target_daily_vol={target_daily_vol}, leverage_cap={leverage_cap})"
        ),
    )
