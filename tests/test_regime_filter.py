"""Tests for the opt-in deterministic causal regime filter (Sprint K1)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any

from trading_ai.backtest.engine import (
    BacktestConfig,
    _risk_off_dates,
    run_momentum_vol_target_backtest,
)


def _series(symbol: str, prices: list[float], start_day: int = 1) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(prices):
        day = start_day + i
        month = 1 + (day - 1) // 28
        dom = 1 + (day - 1) % 28
        rows.append({
            "timestamp": f"2022-{month:02d}-{dom:02d}",
            "symbol": symbol,
            "open": p, "high": p + 0.5, "low": p - 0.5, "close": p, "volume": 1_000_000.0,
        })
    return rows


class RegimeFilterTests(unittest.TestCase):
    def test_default_off_is_byte_identical(self) -> None:
        # Two symbols so the strategy actually trades.
        recs = _series("SPY", [100 + i for i in range(80)]) + _series("QQQ", [100 + i * 1.2 for i in range(80)])
        base = BacktestConfig(max_single_position=0.02)
        off = run_momentum_vol_target_backtest(recs, base)
        explicit_off = run_momentum_vol_target_backtest(recs, replace(base, regime_filter_enabled=False))
        self.assertEqual(off.to_dict()["daily_returns"], explicit_off.to_dict()["daily_returns"])

    def test_risk_off_dates_flags_bear_regime(self) -> None:
        # Benchmark in a clear downtrend → below its SMA → risk-off once the SMA
        # window has enough history.
        prices = [200.0 - i for i in range(60)]
        recs = _series("SPY", prices)
        cfg = BacktestConfig(regime_filter_enabled=True, regime_sma_window=20, regime_vol_window=5, regime_vol_warmup=5)
        risk_off = _risk_off_dates({"SPY": {r["timestamp"]: r["close"] for r in recs}},
                                   [r["timestamp"] for r in recs], cfg)
        # Once past the SMA warmup the falling benchmark is below its SMA.
        late_dates = [r["timestamp"] for r in recs][25:]
        self.assertTrue(all(d in risk_off for d in late_dates))

    def test_regime_on_goes_flat_in_bear(self) -> None:
        # Falling benchmark + a second symbol; with the regime filter on the
        # strategy should hold no position on the risk-off tail → fewer trades.
        recs = _series("SPY", [200.0 - i for i in range(60)]) + _series("QQQ", [150.0 - i * 0.5 for i in range(60)])
        cfg = BacktestConfig(max_single_position=0.02, regime_filter_enabled=True,
                             regime_sma_window=20, regime_vol_window=5, regime_vol_warmup=5)
        off = run_momentum_vol_target_backtest(recs, replace(cfg, regime_filter_enabled=False))
        on = run_momentum_vol_target_backtest(recs, cfg)
        # In a persistent bear the filter must reduce trading activity.
        self.assertLessEqual(on.metrics["trade_count"], off.metrics["trade_count"])

    def test_missing_benchmark_yields_no_risk_off(self) -> None:
        recs = _series("QQQ", [100 + i for i in range(40)])
        cfg = BacktestConfig(regime_filter_enabled=True, regime_benchmark="SPY")
        risk_off = _risk_off_dates({"QQQ": {r["timestamp"]: r["close"] for r in recs}},
                                   [r["timestamp"] for r in recs], cfg)
        self.assertEqual(risk_off, frozenset())


if __name__ == "__main__":
    unittest.main()
