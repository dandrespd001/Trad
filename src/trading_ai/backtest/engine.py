"""Deterministic momentum plus volatility-target backtest."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from statistics import stdev
from typing import Any, cast

from trading_ai.research.metrics import (
    annualized_sharpe,
    cumulative_return,
    max_drawdown,
)

ENGINE_VERSION = "next_open_v2"
EXECUTION_TIMING = "signal_close_execute_next_open"


@dataclass(frozen=True)
class BacktestConfig:
    momentum_window: int = 20
    volatility_window: int = 20
    target_annual_volatility: float = 0.12
    max_gross_exposure: float = 1.0
    max_single_position: float = 0.30
    top_n: int = 3
    periods_per_year: int = 252
    cost_bps: float = 1.0
    slippage_bps: float = 1.0
    # Opt-in deterministic, causal regime filter (Sprint K1). When enabled, the
    # strategy goes flat on any decision date the benchmark is in a risk-off
    # regime: benchmark below its SMA (bear) OR benchmark short-window realized
    # vol above the expanding median of past vols (high-vol). Default off keeps
    # the backtest byte-identical.
    regime_filter_enabled: bool = False
    regime_benchmark: str = "SPY"
    regime_sma_window: int = 200
    regime_vol_window: int = 20
    regime_vol_warmup: int = 120
    # Optional exact session boundary for a cash-reset OOS evaluation. History
    # before this session remains available for causal indicators, but no
    # position or return is carried into the scored window.
    evaluation_start: str | None = None
    # Weight policy. "cross_sectional_momentum" keeps the original top-N ranking
    # and is the default, so an unchanged config stays byte-identical.
    #
    # "time_series_momentum" sizes every instrument independently on the sign of
    # its own trend, weights by inverse volatility, and then scales the whole
    # book toward ``target_annual_volatility``. Unlike the cross-sectional
    # policy, that scalar may raise exposure as well as cut it, bounded by
    # ``max_gross_exposure``; the cross-sectional policy can only de-risk, which
    # is why its realized volatility sits far below target whenever
    # ``max_single_position`` binds first.
    weight_policy: str = "cross_sectional_momentum"
    # Lookbacks blended into the time-series trend signal. Multiple horizons
    # avoid betting the strategy on one window surviving out of sample.
    tsmom_lookbacks: tuple[int, ...] = (60, 120, 250)
    # Volatility window used for the per-instrument inverse-volatility weight.
    tsmom_instrument_vol_window: int = 60
    # Per-instrument cap applied before the portfolio-level volatility scalar.
    tsmom_max_instrument_weight: float = 0.20
    # Refresh the target only every N decision dates, holding it in between.
    # A slow signal re-priced daily still churns, and turnover is what the
    # measured 15 bps all-in cost actually taxes. Snapping the decision index
    # back to the last boundary uses strictly older data, so causality is
    # unaffected. 1 preserves the original per-session behaviour.
    rebalance_every_n_days: int = 1


@dataclass(frozen=True)
class PositionSnapshot:
    timestamp: str
    weights: dict[str, float]
    exposure: float


@dataclass(frozen=True)
class TradeRecord:
    timestamp: str
    symbol: str
    old_weight: float
    new_weight: float
    turnover: float


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    daily_returns: tuple[float, ...]
    equity_curve: tuple[float, ...]
    positions: tuple[PositionSnapshot, ...]
    trades: tuple[TradeRecord, ...]
    metrics: dict[str, float]
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "config": asdict(self.config),
            "daily_returns": list(self.daily_returns),
            "equity_curve": list(self.equity_curve),
            "positions": [asdict(position) for position in self.positions],
            "trades": [asdict(trade) for trade in self.trades],
            "metrics": self.metrics,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class _NextOpenStep:
    """One close-to-close accounting step with a rebalance at the next open."""

    period_return: float
    turnover: float
    cost: float
    gap_return: float
    pretrade_weights: dict[str, float]
    close_weights: dict[str, float]


def run_momentum_vol_target_backtest(
    records: Iterable[Mapping[str, object]],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    cfg = config or BacktestConfig()
    by_symbol = _records_by_symbol(records)
    dates = sorted({timestamp for rows in by_symbol.values() for timestamp in rows})
    evaluation_start = _validated_evaluation_start(cfg.evaluation_start, dates)
    close_by_symbol = {
        symbol: {timestamp: _as_float(row["close"]) for timestamp, row in rows.items()}
        for symbol, rows in by_symbol.items()
    }
    open_by_symbol = {
        symbol: {
            timestamp: _required_row_price(
                row,
                field="open",
                symbol=symbol,
                timestamp=timestamp,
            )
            for timestamp, row in rows.items()
        }
        for symbol, rows in by_symbol.items()
    }

    weights: dict[str, float] = {}
    daily_returns: list[float] = []
    equity_curve: list[float] = []
    positions: list[PositionSnapshot] = []
    trades: list[TradeRecord] = []
    turnovers: list[float] = []
    total_cost = 0.0
    equity = 1.0

    risk_off_dates = _risk_off_dates(close_by_symbol, dates, cfg) if cfg.regime_filter_enabled else frozenset()

    for date_index in range(1, len(dates)):
        current_date = dates[date_index]
        previous_date = dates[date_index - 1]
        if evaluation_start is not None and current_date < evaluation_start:
            continue
        # The decision is taken on ``previous_date``; if that date is risk-off,
        # go flat. Flattening still flows through _turnover below, so the
        # transition cost of exiting to cash is charged (no free lunch).
        decision_is_oos = evaluation_start is None or previous_date >= evaluation_start
        if not decision_is_oos or previous_date in risk_off_dates:
            target_weights: dict[str, float] = {}
        else:
            target_weights = _target_weights(close_by_symbol, dates, date_index - 1, cfg)
        step = _next_open_step(
            old_weights=weights,
            target_weights=target_weights,
            close_by_symbol=close_by_symbol,
            open_by_symbol=open_by_symbol,
            decision_date=previous_date,
            execution_date=current_date,
            total_cost_bps=_total_cost_bps(cfg),
        )
        # ``step.cost`` is a fraction of equity available at the execution
        # open.  Scale it by that equity so the metric is an effective cash
        # debit in units of the initial (1.0) portfolio, not a sum of unrelated
        # per-period percentages.
        total_cost += equity * (1.0 + step.gap_return) * step.cost

        for symbol in sorted(set(step.pretrade_weights) | set(target_weights)):
            old_weight = step.pretrade_weights.get(symbol, 0.0)
            new_weight = target_weights.get(symbol, 0.0)
            if abs(old_weight - new_weight) > 1e-12:
                trades.append(
                    TradeRecord(
                        timestamp=current_date,
                        symbol=symbol,
                        old_weight=old_weight,
                        new_weight=new_weight,
                        turnover=abs(new_weight - old_weight),
                    )
                )

        equity *= 1.0 + step.period_return
        daily_returns.append(step.period_return)
        equity_curve.append(equity)
        # Keep the actual closing book for the next overnight gap.  Public
        # position snapshots intentionally remain the targets executed at the
        # open (see metadata) so snapshot/backtest anti-drift callers retain
        # their established semantics.
        weights = step.close_weights
        turnovers.append(step.turnover)
        positions.append(
            PositionSnapshot(
                timestamp=current_date,
                weights=dict(sorted(target_weights.items())),
                exposure=sum(abs(weight) for weight in target_weights.values()),
            )
        )

    metrics = compute_backtest_metrics(
        daily_returns,
        equity_curve,
        turnovers,
        trade_count=len(trades),
        average_exposure=_average([position.exposure for position in positions]),
        estimated_costs=total_cost,
        periods_per_year=cfg.periods_per_year,
    )
    return BacktestResult(
        config=cfg,
        daily_returns=tuple(daily_returns),
        equity_curve=tuple(equity_curve),
        positions=tuple(positions),
        trades=tuple(trades),
        metrics=metrics,
        metadata=_execution_metadata(cfg),
    )


def compute_target_weights_snapshot(
    records: Iterable[Mapping[str, object]],
    config: BacktestConfig | None = None,
) -> dict[str, object]:
    """Causal snapshot of the strategy's target weights on the last dataset date.

    Mirrors the indexing used by :func:`run_momentum_vol_target_backtest`
    (same ``_records_by_symbol`` grouping and ``close_by_symbol`` coercion) and
    delegates to the private :func:`_target_weights` so the snapshot stays
    byte-identical with the backtest's internal weight computation for the
    same configuration. Used by the live rebalance cycle (§M3) to decide the
    target weights without replaying the full backtest.
    """

    cfg = config or BacktestConfig()
    by_symbol = _records_by_symbol(records)
    dates = sorted({timestamp for rows in by_symbol.values() for timestamp in rows})
    close_by_symbol = {
        symbol: {timestamp: _as_float(row["close"]) for timestamp, row in rows.items()}
        for symbol, rows in by_symbol.items()
    }

    if len(dates) <= cfg.momentum_window:
        return {
            "as_of": dates[-1] if dates else None,
            "weights": {},
            "sufficient_history": False,
        }

    weights = _target_weights(close_by_symbol, dates, len(dates) - 1, cfg)
    return {"as_of": dates[-1], "weights": weights, "sufficient_history": True}


def compute_backtest_metrics(
    daily_returns: list[float],
    equity_curve: list[float],
    turnovers: list[float],
    *,
    trade_count: int,
    average_exposure: float,
    estimated_costs: float,
    periods_per_year: int,
) -> dict[str, float]:
    if not daily_returns:
        return {
            "cumulative_return": 0.0,
            "cagr": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0,
            "trade_count": 0.0,
            "average_exposure": 0.0,
            "estimated_costs": 0.0,
        }
    final_equity = equity_curve[-1] if equity_curve else 1.0
    years = len(daily_returns) / periods_per_year
    downside = [value for value in daily_returns if value < 0]
    downside_vol = stdev(downside) * math.sqrt(periods_per_year) if len(downside) >= 2 else 0.0
    mean_return = _average(daily_returns) * periods_per_year
    return {
        "cumulative_return": cumulative_return(daily_returns),
        "cagr": final_equity ** (1.0 / years) - 1.0 if final_equity > 0 and years > 0 else 0.0,
        "sharpe": annualized_sharpe(daily_returns, periods_per_year=periods_per_year),
        "sortino": mean_return / downside_vol if downside_vol > 0 else 0.0,
        "max_drawdown": max_drawdown(daily_returns),
        "turnover": sum(turnovers),
        "trade_count": float(trade_count),
        "average_exposure": average_exposure,
        "estimated_costs": estimated_costs,
    }


def _records_by_symbol(records: Iterable[Mapping[str, object]]) -> dict[str, dict[str, Mapping[str, object]]]:
    by_symbol: dict[str, dict[str, Mapping[str, object]]] = {}
    for row in records:
        symbol = str(row["symbol"]).upper()
        timestamp = str(row["timestamp"])
        by_symbol.setdefault(symbol, {})[timestamp] = row
    return by_symbol


def _target_weights(
    close_by_symbol: dict[str, dict[str, float]],
    dates: list[str],
    decision_index: int,
    cfg: BacktestConfig,
) -> dict[str, float]:
    """Dispatch to the configured weight policy.

    Both policies read only ``dates[decision_index]`` and earlier, so the
    causality guarantee of the caller is unchanged.
    """

    if cfg.weight_policy == "time_series_momentum":
        if cfg.rebalance_every_n_days > 1:
            decision_index -= decision_index % cfg.rebalance_every_n_days
        return _tsmom_target_weights(close_by_symbol, dates, decision_index, cfg)
    if cfg.weight_policy != "cross_sectional_momentum":
        raise ValueError(f"unknown weight_policy: {cfg.weight_policy!r}")
    return _cross_sectional_target_weights(close_by_symbol, dates, decision_index, cfg)


def _cross_sectional_target_weights(
    close_by_symbol: dict[str, dict[str, float]],
    dates: list[str],
    decision_index: int,
    cfg: BacktestConfig,
) -> dict[str, float]:
    if decision_index < cfg.momentum_window:
        return {}
    decision_date = dates[decision_index]
    lookback_date = dates[decision_index - cfg.momentum_window]
    ranked: list[tuple[float, str]] = []
    for symbol, closes in close_by_symbol.items():
        if decision_date in closes and lookback_date in closes:
            momentum = _safe_return(closes[decision_date], closes[lookback_date])
            if momentum is None:
                continue
            if momentum > 0:
                ranked.append((momentum, symbol))
    selected = [symbol for _, symbol in sorted(ranked, reverse=True)[: cfg.top_n]]
    if not selected:
        return {}

    raw_weight = min(cfg.max_gross_exposure / len(selected), cfg.max_single_position)
    weights = {symbol: raw_weight for symbol in selected}
    gross = sum(abs(weight) for weight in weights.values())
    realized_vol = _portfolio_realized_vol(close_by_symbol, selected, dates, decision_index, cfg)
    if realized_vol > 0:
        scalar = min(1.0, cfg.target_annual_volatility / realized_vol)
        weights = {symbol: weight * scalar for symbol, weight in weights.items()}
    if gross > cfg.max_gross_exposure:
        scale = cfg.max_gross_exposure / gross
        weights = {symbol: weight * scale for symbol, weight in weights.items()}
    return {symbol: weight for symbol, weight in weights.items() if abs(weight) > 1e-12}


def _tsmom_target_weights(
    close_by_symbol: dict[str, dict[str, float]],
    dates: list[str],
    decision_index: int,
    cfg: BacktestConfig,
) -> dict[str, float]:
    """Long-only time-series momentum sized by inverse volatility.

    Each instrument is judged against its own past rather than ranked against
    its peers, so breadth replaces selection: the book carries every instrument
    whose own trend is positive instead of concentrating in ``top_n`` names.
    Conviction blends several lookbacks so the result does not depend on one
    window surviving out of sample.

    Reads only ``dates[decision_index]`` and earlier.
    """

    lookbacks = tuple(sorted({int(window) for window in cfg.tsmom_lookbacks if int(window) > 0}))
    if not lookbacks:
        return {}
    warmup = max(max(lookbacks), cfg.tsmom_instrument_vol_window)
    if decision_index < warmup:
        return {}

    decision_date = dates[decision_index]
    raw: dict[str, float] = {}
    for symbol, closes in close_by_symbol.items():
        if decision_date not in closes:
            continue
        votes: list[float] = []
        for lookback in lookbacks:
            lookback_date = dates[decision_index - lookback]
            if lookback_date not in closes:
                continue
            trend = _safe_return(closes[decision_date], closes[lookback_date])
            if trend is None:
                continue
            votes.append(1.0 if trend > 0 else 0.0)
        if not votes:
            continue
        conviction = _average(votes)
        if conviction <= 0.0:
            continue
        volatility = _instrument_realized_vol(closes, dates, decision_index, cfg)
        if volatility <= 0.0:
            continue
        raw[symbol] = conviction / volatility
    if not raw:
        return {}

    total_raw = sum(raw.values())
    relative = {symbol: weight / total_raw for symbol, weight in raw.items()}

    # Scale the whole book toward the volatility target. This scalar may exceed
    # 1.0, which is the point: a policy that can only de-risk never reaches its
    # target and leaves the portfolio structurally parked in cash.
    portfolio_vol = _weighted_portfolio_realized_vol(
        close_by_symbol, relative, dates, decision_index, cfg
    )
    if portfolio_vol <= 0.0:
        return {}
    scalar = cfg.target_annual_volatility / portfolio_vol

    # Clip each final weight instead of renormalizing after the cap. With few
    # surviving instruments, renormalizing restores exactly the concentration
    # the cap exists to prevent: two names capped at 0.10 come back as 0.50
    # each. Under-deploying is the conservative outcome, so the volatility
    # target is allowed to undershoot whenever the cap binds.
    weights = {
        symbol: min(weight * scalar, cfg.tsmom_max_instrument_weight)
        for symbol, weight in relative.items()
    }

    gross = sum(abs(weight) for weight in weights.values())
    if gross > cfg.max_gross_exposure:
        shrink = cfg.max_gross_exposure / gross
        weights = {symbol: weight * shrink for symbol, weight in weights.items()}
    return {symbol: weight for symbol, weight in weights.items() if abs(weight) > 1e-12}


def _instrument_realized_vol(
    closes: dict[str, float],
    dates: list[str],
    decision_index: int,
    cfg: BacktestConfig,
) -> float:
    window = max(2, cfg.tsmom_instrument_vol_window)
    returns: list[float] = []
    for index in range(max(1, decision_index - window + 1), decision_index + 1):
        current_date = dates[index]
        previous_date = dates[index - 1]
        if current_date in closes and previous_date in closes:
            symbol_return = _safe_return(closes[current_date], closes[previous_date])
            if symbol_return is not None:
                returns.append(symbol_return)
    return stdev(returns) * math.sqrt(cfg.periods_per_year) if len(returns) >= 2 else 0.0


def _weighted_portfolio_realized_vol(
    close_by_symbol: dict[str, dict[str, float]],
    weights: dict[str, float],
    dates: list[str],
    decision_index: int,
    cfg: BacktestConfig,
) -> float:
    window = max(2, cfg.tsmom_instrument_vol_window)
    returns: list[float] = []
    for index in range(max(1, decision_index - window + 1), decision_index + 1):
        current_date = dates[index]
        previous_date = dates[index - 1]
        period_return = 0.0
        covered = 0.0
        for symbol, weight in weights.items():
            closes = close_by_symbol[symbol]
            if current_date in closes and previous_date in closes:
                symbol_return = _safe_return(closes[current_date], closes[previous_date])
                if symbol_return is not None:
                    period_return += weight * symbol_return
                    covered += weight
        if covered > 0.0:
            returns.append(period_return)
    return stdev(returns) * math.sqrt(cfg.periods_per_year) if len(returns) >= 2 else 0.0


def _portfolio_realized_vol(
    close_by_symbol: dict[str, dict[str, float]],
    selected: list[str],
    dates: list[str],
    decision_index: int,
    cfg: BacktestConfig,
) -> float:
    returns: list[float] = []
    start = max(1, decision_index - cfg.volatility_window + 1)
    for index in range(start, decision_index + 1):
        current_date = dates[index]
        previous_date = dates[index - 1]
        selected_returns = []
        for symbol in selected:
            closes = close_by_symbol[symbol]
            if current_date in closes and previous_date in closes:
                symbol_return = _safe_return(closes[current_date], closes[previous_date])
                if symbol_return is not None:
                    selected_returns.append(symbol_return)
        if selected_returns:
            returns.append(_average(selected_returns))
    return stdev(returns) * math.sqrt(cfg.periods_per_year) if len(returns) >= 2 else 0.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _risk_off_dates(
    close_by_symbol: dict[str, dict[str, float]],
    dates: list[str],
    cfg: BacktestConfig,
) -> frozenset[str]:
    """Return the set of decision dates in a risk-off regime (causal).

    A date is risk-off when the benchmark closes below its trailing SMA
    (``regime_sma_window``) OR its trailing realized vol over
    ``regime_vol_window`` days exceeds the expanding median of all prior such
    vols (after a ``regime_vol_warmup`` warmup during which no vol filter is
    applied). Every statistic uses only closes up to and including the date
    itself, so no future information leaks into the regime label. Dates before
    enough history for the SMA are treated as NOT risk-off (the strategy's own
    momentum warmup already keeps it flat early on).
    """

    closes_by_date = close_by_symbol.get(cfg.regime_benchmark)
    if not closes_by_date:
        return frozenset()
    series = [(d, closes_by_date[d]) for d in dates if d in closes_by_date]
    ordered_dates = [d for d, _ in series]
    prices = [p for _, p in series]

    # Trailing realized vol of benchmark daily returns over regime_vol_window.
    vols: dict[str, float] = {}
    for i in range(cfg.regime_vol_window, len(prices)):
        window = [
            prices[j] / prices[j - 1] - 1.0
            for j in range(i - cfg.regime_vol_window + 1, i + 1)
            if prices[j - 1] > 0
        ]
        if len(window) >= 2:
            vols[ordered_dates[i]] = stdev(window)

    risk_off: set[str] = set()
    seen_vols: list[float] = []
    for i, date in enumerate(ordered_dates):
        bear = False
        if i + 1 >= cfg.regime_sma_window:
            sma = sum(prices[i - cfg.regime_sma_window + 1 : i + 1]) / cfg.regime_sma_window
            bear = prices[i] < sma
        high_vol = False
        if date in vols:
            v = vols[date]
            if len(seen_vols) >= cfg.regime_vol_warmup and v > _median(seen_vols):
                high_vol = True
            seen_vols.append(v)
        if bear or high_vol:
            risk_off.add(date)
    return frozenset(risk_off)


def run_signal_policy_backtest(
    feature_records: Iterable[Mapping[str, object]],
    model: Any,
    *,
    threshold: float = 0.5,
    min_signal_margin: float = 0.0,
    max_buy_signals: int = 0,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Backtest the *deployed* single-name logistic signal policy.

    This mirrors the live decision path (``generate_model_signals`` ->
    highest-probability buy with margin/quality filters, hold/rotate/close on
    signal change) so the risk-adjusted metrics (Sharpe, drawdown, turnover)
    describe the strategy that actually trades, not the momentum reference
    strategy. Protective ATR stops are applied at execution time (see
    ``paper_position_plan``) and are intentionally out of scope here, which keeps
    these metrics a conservative floor on the signal's standalone edge.
    """

    cfg = config or BacktestConfig()
    feature_names = tuple(str(name) for name in getattr(model, "feature_names", ()))
    by_symbol = _records_by_symbol(feature_records)
    dates = sorted({timestamp for rows in by_symbol.values() for timestamp in rows})
    close_by_symbol = {
        symbol: {timestamp: _as_float(row["close"]) for timestamp, row in rows.items()}
        for symbol, rows in by_symbol.items()
    }
    open_by_symbol = {
        symbol: {
            timestamp: _required_row_price(
                row,
                field="open",
                symbol=symbol,
                timestamp=timestamp,
            )
            for timestamp, row in rows.items()
        }
        for symbol, rows in by_symbol.items()
    }

    weights: dict[str, float] = {}
    daily_returns: list[float] = []
    equity_curve: list[float] = []
    turnovers: list[float] = []
    exposures: list[float] = []
    total_cost = 0.0
    trade_count = 0
    equity = 1.0

    for date_index in range(1, len(dates)):
        decision_date = dates[date_index - 1]
        current_date = dates[date_index]
        target = _select_policy_symbol(
            by_symbol=by_symbol,
            decision_date=decision_date,
            model=model,
            feature_names=feature_names,
            threshold=threshold,
            min_signal_margin=min_signal_margin,
            max_buy_signals=max_buy_signals,
        )
        new_weights = {target: 1.0} if target else {}
        step = _next_open_step(
            old_weights=weights,
            target_weights=new_weights,
            close_by_symbol=close_by_symbol,
            open_by_symbol=open_by_symbol,
            decision_date=decision_date,
            execution_date=current_date,
            total_cost_bps=_total_cost_bps(cfg),
        )
        if step.turnover > 0 and target is not None:
            trade_count += 1
        total_cost += equity * (1.0 + step.gap_return) * step.cost

        equity *= 1.0 + step.period_return
        daily_returns.append(step.period_return)
        equity_curve.append(equity)
        turnovers.append(step.turnover)
        exposures.append(1.0 if target else 0.0)
        weights = step.close_weights

    metrics = compute_backtest_metrics(
        daily_returns,
        equity_curve,
        turnovers,
        trade_count=trade_count,
        average_exposure=_average(exposures),
        estimated_costs=total_cost,
        periods_per_year=cfg.periods_per_year,
    )
    return BacktestResult(
        config=cfg,
        daily_returns=tuple(daily_returns),
        equity_curve=tuple(equity_curve),
        positions=(),
        trades=(),
        metrics=metrics,
        metadata={"strategy": "signal_policy_single_name", **_execution_metadata(cfg)},
    )


def _select_policy_symbol(
    *,
    by_symbol: dict[str, dict[str, Mapping[str, object]]],
    decision_date: str,
    model: Any,
    feature_names: tuple[str, ...],
    threshold: float,
    min_signal_margin: float,
    max_buy_signals: int,
) -> str | None:
    buys: list[tuple[float, str]] = []
    for symbol, rows in by_symbol.items():
        row = rows.get(decision_date)
        if row is None:
            continue
        features = _policy_features(row, feature_names)
        if features is None:
            continue
        probability = model.predict_probability(features)
        if probability >= threshold:
            buys.append((probability, symbol))
    if not buys:
        return None
    if max_buy_signals > 0 and len(buys) > max_buy_signals:
        return None
    eligible = [(probability, symbol) for probability, symbol in buys if probability - threshold >= min_signal_margin]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], item[1]))[1]


def _policy_features(row: Mapping[str, object], feature_names: tuple[str, ...]) -> tuple[float, ...] | None:
    values: list[float] = []
    for name in feature_names:
        value = row.get(name)
        if value in {None, ""}:
            return None
        try:
            values.append(float(cast(Any, value)))
        except (TypeError, ValueError):
            return None
    return tuple(values)


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return sum(abs(new.get(symbol, 0.0) - old.get(symbol, 0.0)) for symbol in set(old) | set(new))


def _next_open_step(
    *,
    old_weights: Mapping[str, float],
    target_weights: Mapping[str, float],
    close_by_symbol: Mapping[str, Mapping[str, float]],
    open_by_symbol: Mapping[str, Mapping[str, float]],
    decision_date: str,
    execution_date: str,
    total_cost_bps: float,
) -> _NextOpenStep:
    """Account for the old book through the gap, then rebalance at the open.

    A signal observed at ``decision_date`` close cannot own the preceding
    close-to-open move.  Existing positions do own that gap.  Turnover and its
    cost are therefore computed from the gap-drifted book at
    ``execution_date`` open; the new target earns only open-to-close return.
    """

    gap_returns: dict[str, float] = {}
    gap_return = 0.0
    for symbol, weight in old_weights.items():
        previous_close = _required_price(close_by_symbol, symbol, decision_date, field="close")
        execution_open = _required_price(open_by_symbol, symbol, execution_date, field="open")
        symbol_gap = execution_open / previous_close - 1.0
        gap_returns[symbol] = symbol_gap
        gap_return += float(weight) * symbol_gap

    open_equity_multiplier = 1.0 + gap_return
    if not math.isfinite(open_equity_multiplier) or open_equity_multiplier <= 0.0:
        raise ValueError(f"invalid_open_equity_multiplier:{execution_date}")
    pretrade_weights = {
        symbol: float(weight) * (1.0 + gap_returns[symbol]) / open_equity_multiplier
        for symbol, weight in old_weights.items()
        if abs(float(weight)) > 1e-12
    }
    turnover = _turnover(pretrade_weights, dict(target_weights))
    cost = turnover * total_cost_bps / 10_000.0
    if cost >= 1.0:
        raise ValueError(f"execution_cost_exhausts_equity:{execution_date}")

    intraday_return = 0.0
    intraday_multipliers: dict[str, float] = {}
    for symbol, weight in target_weights.items():
        execution_open = _required_price(open_by_symbol, symbol, execution_date, field="open")
        execution_close = _required_price(close_by_symbol, symbol, execution_date, field="close")
        symbol_multiplier = execution_close / execution_open
        intraday_multipliers[symbol] = symbol_multiplier
        intraday_return += float(weight) * (symbol_multiplier - 1.0)

    close_equity_multiplier = 1.0 + intraday_return
    if not math.isfinite(close_equity_multiplier) or close_equity_multiplier <= 0.0:
        raise ValueError(f"invalid_close_equity_multiplier:{execution_date}")
    close_weights = {
        symbol: float(weight) * intraday_multipliers[symbol] / close_equity_multiplier
        for symbol, weight in target_weights.items()
        if abs(float(weight)) > 1e-12
    }

    period_multiplier = open_equity_multiplier * (1.0 - cost) * close_equity_multiplier
    if not math.isfinite(period_multiplier) or period_multiplier < 0.0:
        raise ValueError(f"invalid_period_equity_multiplier:{execution_date}")
    return _NextOpenStep(
        period_return=period_multiplier - 1.0,
        turnover=turnover,
        cost=cost,
        gap_return=gap_return,
        pretrade_weights=pretrade_weights,
        close_weights=close_weights,
    )


def _required_price(
    prices_by_symbol: Mapping[str, Mapping[str, float]],
    symbol: str,
    timestamp: str,
    *,
    field: str,
) -> float:
    try:
        value = float(prices_by_symbol[symbol][timestamp])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"missing_{field}:{symbol}:{timestamp}") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"invalid_{field}:{symbol}:{timestamp}")
    return value


def _required_row_price(
    row: Mapping[str, object],
    *,
    field: str,
    symbol: str,
    timestamp: str,
) -> float:
    try:
        value = _as_float(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"missing_{field}:{symbol}:{timestamp}") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"invalid_{field}:{symbol}:{timestamp}")
    return value


def _total_cost_bps(cfg: BacktestConfig) -> float:
    cost_bps = float(cfg.cost_bps)
    slippage_bps = float(cfg.slippage_bps)
    total = cost_bps + slippage_bps
    if not math.isfinite(total) or cost_bps < 0.0 or slippage_bps < 0.0:
        raise ValueError("backtest_costs_must_be_finite_and_non_negative")
    return total


def _execution_metadata(cfg: BacktestConfig) -> dict[str, object]:
    return {
        "engine_version": ENGINE_VERSION,
        "execution_timing": EXECUTION_TIMING,
        "evaluation_start": cfg.evaluation_start,
        "evaluation_start_semantics": (
            "cash_reset_no_pre_boundary_decision_or_position"
            if cfg.evaluation_start is not None
            else "full_available_history"
        ),
        "signal_time": "session_close",
        "execution_time": "next_session_open",
        "valuation_time": "session_close",
        "position_snapshots": "target_weights_executed_at_session_open",
        "cost_model": {
            "cost_bps": float(cfg.cost_bps),
            "slippage_bps": float(cfg.slippage_bps),
            "total_one_way_bps": _total_cost_bps(cfg),
            "charged_on": "execution_turnover",
            "estimated_costs_unit": "fraction_of_initial_equity_debited_at_execution",
        },
    }


def _validated_evaluation_start(value: object, dates: list[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("evaluation_start_must_be_a_non_empty_session")
    if value not in dates:
        raise ValueError(f"evaluation_start_not_in_dataset:{value}")
    return value


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _as_float(value: object) -> float:
    return float(cast(Any, value))


def _safe_return(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator - 1.0
