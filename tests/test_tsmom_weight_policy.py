import unittest
from datetime import date, timedelta
from typing import Any

from trading_ai.backtest.engine import (
    BacktestConfig,
    _target_weights,
    run_momentum_vol_target_backtest,
)


def _dates(count: int) -> list[str]:
    start = date(2021, 1, 4)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _records(series: dict[str, list[float]]) -> list[dict[str, Any]]:
    """Build OHLCV rows where each session opens at the previous close."""

    lengths = {len(closes) for closes in series.values()}
    if len(lengths) != 1:
        raise ValueError("all symbols need the same number of sessions")
    timestamps = _dates(lengths.pop())
    rows: list[dict[str, Any]] = []
    for symbol, closes in series.items():
        for index, close in enumerate(closes):
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": timestamps[index],
                    "open": closes[index - 1] if index else close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1_000.0,
                }
            )
    return rows


def _trending(count: int, *, drift: float, wobble: float, start: float = 100.0) -> list[float]:
    """Deterministic series with a drift and an alternating wobble."""

    closes = [start]
    for index in range(1, count):
        step = drift + (wobble if index % 2 else -wobble)
        closes.append(closes[-1] * (1.0 + step))
    return closes


def _close_by_symbol(series: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    timestamps = _dates(len(next(iter(series.values()))))
    return {
        symbol: dict(zip(timestamps, closes, strict=True)) for symbol, closes in series.items()
    }


class TimeSeriesMomentumPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = 400
        self.series = {
            "AAA": _trending(self.sessions, drift=0.0009, wobble=0.004),
            "BBB": _trending(self.sessions, drift=0.0006, wobble=0.010),
            "CCC": _trending(self.sessions, drift=-0.0010, wobble=0.005),
        }
        self.cfg = BacktestConfig(
            weight_policy="time_series_momentum",
            target_annual_volatility=0.12,
            tsmom_lookbacks=(60, 120, 250),
            tsmom_instrument_vol_window=60,
            tsmom_max_instrument_weight=0.20,
            max_gross_exposure=1.0,
        )

    def test_default_policy_is_cross_sectional(self) -> None:
        self.assertEqual(BacktestConfig().weight_policy, "cross_sectional_momentum")

    def test_unknown_policy_is_rejected(self) -> None:
        cfg = BacktestConfig(weight_policy="martingale")
        with self.assertRaises(ValueError):
            _target_weights(_close_by_symbol(self.series), _dates(self.sessions), 300, cfg)

    def test_no_weights_before_warmup(self) -> None:
        closes = _close_by_symbol(self.series)
        dates = _dates(self.sessions)
        # The longest lookback is 250 sessions, so 249 cannot produce a target.
        self.assertEqual(_target_weights(closes, dates, 249, self.cfg), {})
        self.assertNotEqual(_target_weights(closes, dates, 250, self.cfg), {})

    def test_downtrending_symbol_is_excluded(self) -> None:
        weights = _target_weights(_close_by_symbol(self.series), _dates(self.sessions), 380, self.cfg)
        self.assertNotIn("CCC", weights)
        self.assertIn("AAA", weights)

    def test_weights_ignore_data_after_the_decision_date(self) -> None:
        """Causality: mutating the future must not change today's target."""

        decision_index = 300
        baseline = _target_weights(_close_by_symbol(self.series), _dates(self.sessions), decision_index, self.cfg)
        tampered = {
            symbol: list(closes) for symbol, closes in self.series.items()
        }
        for closes in tampered.values():
            for index in range(decision_index + 1, self.sessions):
                closes[index] *= 5.0
        after = _target_weights(_close_by_symbol(tampered), _dates(self.sessions), decision_index, self.cfg)
        self.assertEqual(baseline, after)

    def test_per_instrument_cap_clips_final_weights(self) -> None:
        """A binding cap must under-deploy rather than re-concentrate.

        Renormalizing after the cap would hand two surviving names 0.50 each,
        which is exactly what the cap exists to prevent.
        """

        cfg = BacktestConfig(
            weight_policy="time_series_momentum",
            tsmom_lookbacks=(60,),
            tsmom_instrument_vol_window=60,
            tsmom_max_instrument_weight=0.10,
            # A target this high makes the scalar enormous, so the cap binds.
            target_annual_volatility=50.0,
        )
        weights = _target_weights(_close_by_symbol(self.series), _dates(self.sessions), 380, cfg)
        self.assertGreater(sum(weights.values()), 0.0)
        for weight in weights.values():
            self.assertLessEqual(weight, 0.10 + 1e-9)
        # Two surviving instruments capped at 0.10 leave the book at 0.20 gross,
        # far below max_gross_exposure: the shortfall stays in cash.
        self.assertLess(sum(weights.values()), 1.0)

    def test_gross_exposure_respects_the_cap(self) -> None:
        cfg = BacktestConfig(
            weight_policy="time_series_momentum",
            tsmom_lookbacks=(60,),
            tsmom_instrument_vol_window=60,
            target_annual_volatility=5.0,
            max_gross_exposure=0.75,
        )
        weights = _target_weights(_close_by_symbol(self.series), _dates(self.sessions), 380, cfg)
        self.assertLessEqual(sum(abs(weight) for weight in weights.values()), 0.75 + 1e-9)

    def test_scalar_can_raise_exposure_above_the_single_position_cap(self) -> None:
        """The whole point of the pivot.

        The cross-sectional policy takes min(gross/n, max_single_position), so a
        0.02 cap with top_n 3 pins gross exposure at 6% and the volatility target
        can never bind. The time-series policy must be able to deploy far more.
        """

        records = _records(self.series)
        cross = run_momentum_vol_target_backtest(
            records,
            BacktestConfig(momentum_window=20, volatility_window=20, max_single_position=0.02, top_n=3),
        )
        tsmom = run_momentum_vol_target_backtest(records, self.cfg)
        self.assertLessEqual(cross.metrics["average_exposure"], 0.06 + 1e-9)
        self.assertGreater(tsmom.metrics["average_exposure"], cross.metrics["average_exposure"] * 3)

    def test_spaced_rebalancing_reduces_turnover(self) -> None:
        records = _records(self.series)
        daily = run_momentum_vol_target_backtest(records, self.cfg)
        spaced = run_momentum_vol_target_backtest(
            records,
            BacktestConfig(
                weight_policy="time_series_momentum",
                target_annual_volatility=0.12,
                tsmom_lookbacks=(60, 120, 250),
                tsmom_instrument_vol_window=60,
                tsmom_max_instrument_weight=0.20,
                rebalance_every_n_days=21,
            ),
        )
        self.assertLess(spaced.metrics["turnover"], daily.metrics["turnover"])

    def test_spaced_rebalancing_only_reads_older_data(self) -> None:
        """Snapping the index backwards must never look forward."""

        cfg = BacktestConfig(
            weight_policy="time_series_momentum",
            tsmom_lookbacks=(60,),
            tsmom_instrument_vol_window=60,
            rebalance_every_n_days=21,
        )
        closes = _close_by_symbol(self.series)
        dates = _dates(self.sessions)
        snapped = _target_weights(closes, dates, 314, cfg)
        boundary = _target_weights(closes, dates, 294, cfg)
        self.assertEqual(snapped, boundary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
