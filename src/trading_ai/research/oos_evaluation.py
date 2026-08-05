"""Pure, deterministic evaluation helpers for aligned OOS return series."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from trading_ai.research.block_bootstrap import iter_circular_block_bootstrap_indices
from trading_ai.research.metrics import (
    annualized_sharpe,
    annualized_sortino,
    cumulative_return,
    max_drawdown,
)

_MAX_BOOTSTRAP_WORK = 10_000_000
_MAX_RESAMPLES = 100_000


@dataclass(frozen=True, slots=True)
class ReturnMetrics:
    observations: int
    cumulative_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float


@dataclass(frozen=True, slots=True)
class FoldEvaluation:
    fold_id: str
    start_session: str
    end_session: str
    observations: int
    strategy: ReturnMetrics
    benchmark: ReturnMetrics
    compound_active_return: float
    active_sharpe: float
    active_sortino: float


@dataclass(frozen=True, slots=True)
class BootstrapSummary:
    method: str
    block_size: int
    n_resamples: int
    seed: int
    compound_active_return_p05: float
    compound_active_return_p50: float
    compound_active_return_p95: float
    strategy_max_drawdown_p95: float
    probability_compound_active_positive: float


@dataclass(frozen=True, slots=True)
class OosEvaluation:
    input_sha256: str
    sessions: tuple[str, ...]
    strategy_returns: tuple[float, ...]
    benchmark_returns: tuple[float, ...]
    active_returns: tuple[float, ...]
    fold_ids: tuple[str, ...]
    strategy: ReturnMetrics
    benchmark: ReturnMetrics
    compound_active_return: float
    active_sharpe: float
    active_sortino: float
    folds: tuple[FoldEvaluation, ...]
    bootstrap: BootstrapSummary


@dataclass(frozen=True, slots=True)
class SealedTrialRecord:
    canonical_json: str
    sha256: str


def buy_and_hold_next_open_returns(
    session_ids: Iterable[str],
    opens: Iterable[float],
    closes: Iterable[float],
    *,
    one_way_cost_bps: float,
) -> tuple[float, ...]:
    """Build a cash-reset buy-and-hold benchmark on an OOS session window.

    The first OOS close is the decision point. SPY is bought at the following
    session's open, entry cost is charged once, and the position is then marked
    close-to-close without an artificial terminal liquidation.
    """

    sessions = _validated_sessions(session_ids)
    open_values = _validated_prices(opens, name="opens")
    close_values = _validated_prices(closes, name="closes")
    lengths = {len(sessions), len(open_values), len(close_values)}
    if len(lengths) != 1:
        raise ValueError("session_ids, opens and closes must have identical lengths")
    if (
        isinstance(one_way_cost_bps, bool)
        or not isinstance(one_way_cost_bps, (int, float))
    ):
        raise TypeError("one_way_cost_bps must be a finite number")
    cost_bps = float(one_way_cost_bps)
    if not math.isfinite(cost_bps) or cost_bps < 0.0 or cost_bps >= 10_000.0:
        raise ValueError("one_way_cost_bps must be finite and in [0, 10000)")

    result = [0.0]
    entry_return = (1.0 - cost_bps / 10_000.0) * (
        close_values[1] / open_values[1]
    ) - 1.0
    result.append(_finite_result(entry_return, name="benchmark_entry_return"))
    for index in range(2, len(sessions)):
        close_return = close_values[index] / close_values[index - 1] - 1.0
        result.append(_finite_result(close_return, name="benchmark_close_return"))
    return tuple(result)


def evaluate_oos(
    session_ids: Iterable[str],
    strategy_returns: Iterable[float],
    benchmark_returns: Iterable[float],
    fold_ids: Iterable[str],
    *,
    periods_per_year: int,
    block_size: int,
    n_resamples: int,
    seed: int,
) -> OosEvaluation:
    """Evaluate already-net, aligned OOS strategy and benchmark returns.

    ``fold_ids`` must describe at least two contiguous, non-repeated folds.
    Bootstrap samples preserve strategy/benchmark pairing. This function does
    not establish causality or data provenance; callers must freeze and verify
    those contracts separately.
    """

    sessions = _validated_sessions(session_ids)
    strategy = _validated_returns(strategy_returns, name="strategy_returns")
    benchmark = _validated_returns(benchmark_returns, name="benchmark_returns")
    folds = _validated_fold_ids(fold_ids)
    _validate_aligned_lengths(sessions, strategy, benchmark, folds)
    _validate_positive_int(periods_per_year, name="periods_per_year")
    _validate_positive_int(block_size, name="block_size")
    _validate_positive_int(n_resamples, name="n_resamples")
    _validate_int(seed, name="seed")
    if block_size > len(sessions):
        raise ValueError("block_size must not exceed the OOS series length")
    if n_resamples > _MAX_RESAMPLES:
        raise ValueError(f"n_resamples must not exceed {_MAX_RESAMPLES}")
    if n_resamples * len(sessions) > _MAX_BOOTSTRAP_WORK:
        raise ValueError("n_resamples * observations exceeds the deterministic work limit")

    active = tuple(
        _finite_result(strategy_return - benchmark_return, name="active_return")
        for strategy_return, benchmark_return in zip(strategy, benchmark, strict=True)
    )
    fold_slices = _contiguous_fold_slices(folds)
    if len(fold_slices) < 2:
        raise ValueError("fold_ids must contain at least two folds")

    fold_results = tuple(
        _evaluate_fold(
            fold_id=fold_id,
            sessions=sessions[start:stop],
            strategy=strategy[start:stop],
            benchmark=benchmark[start:stop],
            active=active[start:stop],
            periods_per_year=periods_per_year,
        )
        for fold_id, start, stop in fold_slices
    )
    strategy_metrics = _return_metrics(strategy, periods_per_year=periods_per_year)
    benchmark_metrics = _return_metrics(benchmark, periods_per_year=periods_per_year)
    compound_active = _compound_active_return(strategy, benchmark)
    active_sharpe = _finite_result(
        annualized_sharpe(active, periods_per_year=periods_per_year),
        name="active_sharpe",
    )
    active_sortino = _finite_result(
        annualized_sortino(active, periods_per_year=periods_per_year),
        name="active_sortino",
    )
    bootstrap = _bootstrap_summary(
        strategy,
        benchmark,
        block_size=block_size,
        n_resamples=n_resamples,
        seed=seed,
    )
    input_record = seal_trial_record(
        {
            "benchmark_returns": benchmark,
            "block_size": block_size,
            "fold_ids": folds,
            "n_resamples": n_resamples,
            "periods_per_year": periods_per_year,
            "seed": seed,
            "session_ids": sessions,
            "strategy_returns": strategy,
        }
    )
    return OosEvaluation(
        input_sha256=input_record.sha256,
        sessions=sessions,
        strategy_returns=strategy,
        benchmark_returns=benchmark,
        active_returns=active,
        fold_ids=folds,
        strategy=strategy_metrics,
        benchmark=benchmark_metrics,
        compound_active_return=compound_active,
        active_sharpe=active_sharpe,
        active_sortino=active_sortino,
        folds=fold_results,
        bootstrap=bootstrap,
    )


def seal_trial_record(payload: Mapping[str, object]) -> SealedTrialRecord:
    """Return canonical JSON and its SHA-256 digest without performing I/O."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    normalized = _canonicalize(payload, path="payload")
    canonical_json = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return SealedTrialRecord(canonical_json=canonical_json, sha256=digest)


def _evaluate_fold(
    *,
    fold_id: str,
    sessions: tuple[str, ...],
    strategy: tuple[float, ...],
    benchmark: tuple[float, ...],
    active: tuple[float, ...],
    periods_per_year: int,
) -> FoldEvaluation:
    if len(sessions) < 2:
        raise ValueError(f"fold {fold_id!r} must contain at least two observations")
    return FoldEvaluation(
        fold_id=fold_id,
        start_session=sessions[0],
        end_session=sessions[-1],
        observations=len(sessions),
        strategy=_return_metrics(strategy, periods_per_year=periods_per_year),
        benchmark=_return_metrics(benchmark, periods_per_year=periods_per_year),
        compound_active_return=_compound_active_return(strategy, benchmark),
        active_sharpe=_finite_result(
            annualized_sharpe(active, periods_per_year=periods_per_year),
            name=f"{fold_id}.active_sharpe",
        ),
        active_sortino=_finite_result(
            annualized_sortino(active, periods_per_year=periods_per_year),
            name=f"{fold_id}.active_sortino",
        ),
    )


def _return_metrics(values: tuple[float, ...], *, periods_per_year: int) -> ReturnMetrics:
    total_return = _finite_result(cumulative_return(values), name="cumulative_return")
    terminal_equity = 1.0 + total_return
    if terminal_equity <= 0.0:
        raise ValueError("terminal equity must remain positive")
    try:
        cagr = terminal_equity ** (periods_per_year / len(values)) - 1.0
    except OverflowError as exc:
        raise ValueError("cagr is non-finite") from exc
    cagr = _finite_result(cagr, name="cagr")
    drawdown = _finite_result(max_drawdown(values), name="max_drawdown")
    sharpe = _finite_result(
        annualized_sharpe(values, periods_per_year=periods_per_year),
        name="sharpe",
    )
    sortino = _finite_result(
        annualized_sortino(values, periods_per_year=periods_per_year),
        name="sortino",
    )
    calmar = _finite_result(cagr / drawdown if drawdown > 0.0 else 0.0, name="calmar")
    return ReturnMetrics(
        observations=len(values),
        cumulative_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=drawdown,
        calmar=calmar,
    )


def _compound_active_return(
    strategy: tuple[float, ...],
    benchmark: tuple[float, ...],
) -> float:
    strategy_equity = 1.0 + _finite_result(
        cumulative_return(strategy),
        name="strategy_cumulative_return",
    )
    benchmark_equity = 1.0 + _finite_result(
        cumulative_return(benchmark),
        name="benchmark_cumulative_return",
    )
    if strategy_equity <= 0.0 or benchmark_equity <= 0.0:
        raise ValueError("strategy and benchmark terminal equity must remain positive")
    return _finite_result(strategy_equity / benchmark_equity - 1.0, name="compound_active_return")


def _bootstrap_summary(
    strategy: tuple[float, ...],
    benchmark: tuple[float, ...],
    *,
    block_size: int,
    n_resamples: int,
    seed: int,
) -> BootstrapSummary:
    active_values: list[float] = []
    drawdowns: list[float] = []
    for indices in iter_circular_block_bootstrap_indices(
        len(strategy),
        block_size=block_size,
        n_resamples=n_resamples,
        seed=seed,
    ):
        sampled_strategy = tuple(strategy[index] for index in indices)
        sampled_benchmark = tuple(benchmark[index] for index in indices)
        active_values.append(_compound_active_return(sampled_strategy, sampled_benchmark))
        drawdowns.append(
            _finite_result(max_drawdown(sampled_strategy), name="bootstrap_max_drawdown")
        )
    active_values.sort()
    drawdowns.sort()
    return BootstrapSummary(
        method="circular_moving_block",
        block_size=block_size,
        n_resamples=n_resamples,
        seed=seed,
        compound_active_return_p05=_nearest_rank(active_values, 0.05),
        compound_active_return_p50=_nearest_rank(active_values, 0.50),
        compound_active_return_p95=_nearest_rank(active_values, 0.95),
        strategy_max_drawdown_p95=_nearest_rank(drawdowns, 0.95),
        probability_compound_active_positive=sum(value > 0.0 for value in active_values)
        / n_resamples,
    )


def _nearest_rank(sorted_values: list[float], fraction: float) -> float:
    rank = min(len(sorted_values) - 1, max(0, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[rank]


def _validated_sessions(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("session_ids must be an iterable of strings")
    sessions = tuple(values)
    if len(sessions) < 2:
        raise ValueError("session_ids must contain at least two observations")
    for index, value in enumerate(sessions):
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"session_ids[{index}] must be a non-empty string")
        if index and value <= sessions[index - 1]:
            raise ValueError("session_ids must be strictly increasing")
    return sessions


def _validated_returns(values: Iterable[float], *, name: str) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of finite numbers")
    result: list[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name}[{index}] must be a finite number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{name}[{index}] must be finite")
        if number <= -1.0:
            raise ValueError(f"{name}[{index}] must be greater than -1.0")
        result.append(_clean_zero(number))
    if len(result) < 2:
        raise ValueError(f"{name} must contain at least two observations")
    return tuple(result)


def _validated_prices(values: Iterable[float], *, name: str) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of finite positive numbers")
    result: list[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name}[{index}] must be a finite positive number")
        number = float(value)
        if not math.isfinite(number) or number <= 0.0:
            raise ValueError(f"{name}[{index}] must be finite and positive")
        result.append(number)
    if len(result) < 2:
        raise ValueError(f"{name} must contain at least two observations")
    return tuple(result)


def _validated_fold_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("fold_ids must be an iterable of strings")
    result = tuple(values)
    for index, value in enumerate(result):
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"fold_ids[{index}] must be a non-empty string")
    return result


def _validate_aligned_lengths(
    sessions: tuple[str, ...],
    strategy: tuple[float, ...],
    benchmark: tuple[float, ...],
    folds: tuple[str, ...],
) -> None:
    lengths = {len(sessions), len(strategy), len(benchmark), len(folds)}
    if len(lengths) != 1:
        raise ValueError("session_ids, returns and fold_ids must have identical lengths")


def _contiguous_fold_slices(fold_ids: tuple[str, ...]) -> tuple[tuple[str, int, int], ...]:
    slices: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    start = 0
    for index in range(1, len(fold_ids) + 1):
        if index < len(fold_ids) and fold_ids[index] == fold_ids[start]:
            continue
        fold_id = fold_ids[start]
        if fold_id in seen:
            raise ValueError("each fold_id must occupy exactly one contiguous block")
        seen.add(fold_id)
        slices.append((fold_id, start, index))
        start = index
    return tuple(slices)


def _validate_positive_int(value: int, *, name: str) -> None:
    _validate_int(value, name=name)
    if value < 1:
        raise ValueError(f"{name} must be at least 1")


def _validate_int(value: int, *, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")


def _finite_result(value: float, *, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return _clean_zero(value)


def _clean_zero(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _canonicalize(value: object, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return _clean_zero(value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            normalized[key] = _canonicalize(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _canonicalize(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported type {type(value).__name__}")
