"""Deterministic momentum plus volatility-target backtest."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import stdev
from typing import Iterable, Mapping

from trading_ai.research.metrics import annualized_sharpe, cumulative_return, max_drawdown


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


def run_momentum_vol_target_backtest(
    records: Iterable[Mapping[str, object]],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    cfg = config or BacktestConfig()
    by_symbol = _records_by_symbol(records)
    dates = sorted({timestamp for rows in by_symbol.values() for timestamp in rows})
    close_by_symbol = {
        symbol: {timestamp: float(row["close"]) for timestamp, row in rows.items()}
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

    for date_index in range(1, len(dates)):
        current_date = dates[date_index]
        previous_date = dates[date_index - 1]
        target_weights = _target_weights(close_by_symbol, dates, date_index - 1, cfg)
        turnover = _turnover(weights, target_weights)
        cost = turnover * (cfg.cost_bps + cfg.slippage_bps) / 10_000.0
        total_cost += cost

        for symbol in sorted(set(weights) | set(target_weights)):
            old_weight = weights.get(symbol, 0.0)
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

        period_return = -cost
        for symbol, weight in target_weights.items():
            symbol_closes = close_by_symbol[symbol]
            if current_date in symbol_closes and previous_date in symbol_closes:
                period_return += weight * (symbol_closes[current_date] / symbol_closes[previous_date] - 1.0)

        equity *= 1.0 + period_return
        daily_returns.append(period_return)
        equity_curve.append(equity)
        weights = target_weights
        turnovers.append(turnover)
        positions.append(
            PositionSnapshot(
                timestamp=current_date,
                weights=dict(sorted(weights.items())),
                exposure=sum(abs(weight) for weight in weights.values()),
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
    )


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
    if decision_index < cfg.momentum_window:
        return {}
    decision_date = dates[decision_index]
    lookback_date = dates[decision_index - cfg.momentum_window]
    ranked: list[tuple[float, str]] = []
    for symbol, closes in close_by_symbol.items():
        if decision_date in closes and lookback_date in closes:
            momentum = closes[decision_date] / closes[lookback_date] - 1.0
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
                selected_returns.append(closes[current_date] / closes[previous_date] - 1.0)
        if selected_returns:
            returns.append(_average(selected_returns))
    return stdev(returns) * math.sqrt(cfg.periods_per_year) if len(returns) >= 2 else 0.0


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return sum(abs(new.get(symbol, 0.0) - old.get(symbol, 0.0)) for symbol in set(old) | set(new))


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
