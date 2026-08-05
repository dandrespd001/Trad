import unittest
from dataclasses import replace
from typing import Any

try:
    import pytest
except ImportError:
    import pytest_shim as pytest

from trading_ai.backtest.engine import BacktestConfig, run_momentum_vol_target_backtest
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.features.engineering import FeatureConfig, _bb_pct_b, _ewm_last, _macd_hist, _rsi, build_features
from trading_ai.research.metrics import (
    annualized_sharpe,
    cumulative_return,
    estimate_slippage_bps,
    max_drawdown,
    volatility_target_weight,
)


def sample_records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    spy_closes = [100, 101, 102, 104, 106, 109]
    tlt_closes = [100, 99, 98, 97, 96, 95]
    for index, close in enumerate(spy_closes, start=1):
        rows.append(
            {
                "timestamp": f"2024-01-0{index}",
                "symbol": "SPY",
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000 + index * 10,
            }
        )
    for index, close in enumerate(tlt_closes, start=1):
        rows.append(
            {
                "timestamp": f"2024-01-0{index}",
                "symbol": "TLT",
                "open": close + 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 900 + index * 10,
            }
        )
    return rows


def single_symbol_records(
    opens: list[float],
    closes: list[float],
    *,
    symbol: str = "SPY",
) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": f"2024-02-{index + 1:02d}",
            "symbol": symbol,
            "open": open_price,
            "high": max(open_price, close) + 1.0,
            "low": min(open_price, close) - 1.0,
            "close": close,
            "volume": 1_000,
        }
        for index, (open_price, close) in enumerate(zip(opens, closes, strict=True))
    ]


def next_open_config(
    *,
    cost_bps: float = 0.0,
    slippage_bps: float = 0.0,
    top_n: int = 1,
    max_single_position: float = 1.0,
) -> BacktestConfig:
    return BacktestConfig(
        momentum_window=1,
        volatility_window=1,
        target_annual_volatility=10.0,
        max_gross_exposure=1.0,
        max_single_position=max_single_position,
        top_n=top_n,
        periods_per_year=252,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )


def as_float(value: Any) -> float:
    return float(value)


class DataFeatureBacktestTests(unittest.TestCase):
    def test_validation_accepts_clean_ohlcv_records(self) -> None:
        result = validate_ohlcv_records(sample_records(), expected_symbols=["SPY", "TLT"])

        self.assertTrue(result.valid)
        self.assertEqual(result.row_count, 12)
        self.assertEqual(result.symbols, ("SPY", "TLT"))
        self.assertEqual(result.errors, ())

    def test_validation_rejects_duplicate_symbol_timestamp(self) -> None:
        rows = sample_records()
        rows.append(dict(rows[0]))

        result = validate_ohlcv_records(rows)

        self.assertFalse(result.valid)
        self.assertIn("duplicate timestamp/symbol pair: 2024-01-01 SPY", result.errors)

    def test_validation_rejects_bad_price_ranges(self) -> None:
        rows = sample_records()
        rows[0]["high"] = 99

        result = validate_ohlcv_records(rows)

        self.assertFalse(result.valid)
        self.assertIn("row 0 high below open/close", result.errors)

    def test_validation_rejects_zero_prices(self) -> None:
        for column in ("open", "high", "low", "close"):
            with self.subTest(column=column):
                rows = sample_records()
                rows[0][column] = 0

                result = validate_ohlcv_records(rows)

                self.assertFalse(result.valid)
                self.assertIn(f"row 0 {column} must be greater than zero", result.errors)

    def test_validation_rejects_invalid_timestamp(self) -> None:
        rows = sample_records()
        rows[0]["timestamp"] = "not-a-date"

        result = validate_ohlcv_records(rows)

        self.assertFalse(result.valid)
        self.assertIn("row 0 invalid timestamp: not-a-date", result.errors)

    def test_validation_rejects_non_finite_ohlcv_values(self) -> None:
        for value in ("NaN", "inf"):
            with self.subTest(value=value):
                rows = sample_records()
                rows[0]["close"] = value

                result = validate_ohlcv_records(rows)

                self.assertFalse(result.valid)
                self.assertIn("row 0 invalid numeric value for close", result.errors)

    def test_feature_builder_ignores_zero_denominators(self) -> None:
        rows = [
            {
                "timestamp": "2024-01-01",
                "symbol": "SPY",
                "open": 1,
                "high": 1,
                "low": 0,
                "close": 0,
                "volume": 1000,
            },
            {
                "timestamp": "2024-01-02",
                "symbol": "SPY",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1000,
            },
        ]

        features = build_features(rows, FeatureConfig(momentum_windows=(1,), moving_average_windows=(1,)))

        self.assertIsNone(features[1]["return_1d"])
        self.assertIsNone(features[1]["momentum_1"])

    def test_feature_builder_reports_none_for_zero_mean_relative_volume(self) -> None:
        rows = [
            {
                "timestamp": "2024-01-01",
                "symbol": "SPY",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 0,
            },
            {
                "timestamp": "2024-01-02",
                "symbol": "SPY",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 0,
            },
        ]

        features = build_features(rows, FeatureConfig(relative_volume_window=2))

        self.assertIsNone(features[1]["relative_volume_2"])

    def test_feature_builder_uses_only_past_rows_for_momentum_and_volume(self) -> None:
        features = build_features(
            sample_records(),
            FeatureConfig(
                momentum_windows=(2,),
                volatility_window=3,
                drawdown_window=3,
                moving_average_windows=(2,),
                relative_volume_window=2,
            ),
        )
        spy_rows = [row for row in features if row["symbol"] == "SPY"]

        self.assertIsNone(spy_rows[0]["return_1d"])
        self.assertAlmostEqual(as_float(spy_rows[2]["momentum_2"]), 0.02)
        self.assertAlmostEqual(as_float(spy_rows[2]["sma_2"]), 101.5)
        self.assertAlmostEqual(as_float(spy_rows[2]["relative_volume_2"]), 1030 / 1025)
        self.assertIn("realized_volatility_3", spy_rows[3])
        self.assertIn("rolling_drawdown_3", spy_rows[3])

    def test_feature_builder_adds_normalized_research_features_without_future_rows(self) -> None:
        features = build_features(
            sample_records(),
            FeatureConfig(
                momentum_windows=(2,),
                volatility_window=3,
                drawdown_window=3,
                moving_average_windows=(2,),
                relative_volume_window=2,
            ),
        )
        spy_rows = [row for row in features if row["symbol"] == "SPY"]

        self.assertIsNone(spy_rows[0]["close_to_sma_2"])
        self.assertAlmostEqual(as_float(spy_rows[2]["close_to_sma_2"]), 102 / 101.5 - 1.0)
        self.assertIsNotNone(spy_rows[2]["realized_volatility_3"])
        self.assertAlmostEqual(
            as_float(spy_rows[2]["vol_adjusted_momentum_2"]),
            as_float(spy_rows[2]["momentum_2"]) / as_float(spy_rows[2]["realized_volatility_3"]),
        )

    def test_momentum_vol_target_backtest_is_reproducible_and_profitable_on_simple_sample(self) -> None:
        result = run_momentum_vol_target_backtest(
            sample_records(),
            BacktestConfig(
                momentum_window=2,
                volatility_window=3,
                target_annual_volatility=0.20,
                max_gross_exposure=1.0,
                max_single_position=1.0,
                top_n=1,
                periods_per_year=252,
                cost_bps=1.0,
                slippage_bps=1.0,
            ),
        )

        self.assertGreater(result.metrics["cumulative_return"], 0.0)
        self.assertGreaterEqual(result.metrics["trade_count"], 1)
        self.assertLessEqual(result.metrics["max_drawdown"], 0.10)
        self.assertEqual(
            result.daily_returns,
            run_momentum_vol_target_backtest(sample_records(), result.config).daily_returns,
        )

    def test_next_open_entry_does_not_capture_the_prior_overnight_gap(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 110.0, 200.0],
            closes=[100.0, 110.0, 202.0],
        )

        result = run_momentum_vol_target_backtest(records, next_open_config())

        self.assertEqual(result.daily_returns[0], 0.0)
        self.assertAlmostEqual(result.daily_returns[1], 202.0 / 200.0 - 1.0, places=12)
        self.assertEqual(result.metadata["execution_timing"], "signal_close_execute_next_open")

    def test_oos_boundary_resets_to_cash_and_rejects_pre_boundary_decisions(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 101.0, 102.0, 103.0, 104.0],
            closes=[100.0, 101.0, 102.0, 103.0, 105.0],
        )
        config = replace(next_open_config(), evaluation_start="2024-02-03")

        result = run_momentum_vol_target_backtest(records, config)

        self.assertEqual(
            tuple(position.timestamp for position in result.positions),
            ("2024-02-03", "2024-02-04", "2024-02-05"),
        )
        self.assertEqual(result.daily_returns[0], 0.0)
        self.assertEqual(result.positions[0].weights, {})
        self.assertFalse(any(trade.timestamp < "2024-02-04" for trade in result.trades))
        self.assertTrue(any(trade.timestamp == "2024-02-04" for trade in result.trades))
        self.assertEqual(
            result.metadata["evaluation_start_semantics"],
            "cash_reset_no_pre_boundary_decision_or_position",
        )

    def test_oos_boundary_must_be_an_exact_dataset_session(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 101.0, 102.0],
            closes=[100.0, 101.0, 102.0],
        )
        config = replace(next_open_config(), evaluation_start="2024-02-04")

        with self.assertRaisesRegex(ValueError, "evaluation_start_not_in_dataset"):
            run_momentum_vol_target_backtest(records, config)

    def test_next_open_means_the_next_available_bar_across_calendar_gaps(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 110.0, 200.0],
            closes=[100.0, 110.0, 202.0],
        )
        records[0]["timestamp"] = "2024-02-02"  # Friday
        records[1]["timestamp"] = "2024-02-05"  # Monday
        records[2]["timestamp"] = "2024-02-07"  # Wednesday; Tuesday absent

        result = run_momentum_vol_target_backtest(records, next_open_config())

        self.assertAlmostEqual(result.daily_returns[-1], 202.0 / 200.0 - 1.0, places=12)
        self.assertEqual(result.positions[-1].timestamp, "2024-02-07")

    def test_existing_position_owns_the_gap_until_the_open_rebalance(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 100.0, 110.0, 60.5],
            closes=[100.0, 110.0, 121.0, 60.5],
        )

        result = run_momentum_vol_target_backtest(records, next_open_config())

        self.assertAlmostEqual(result.daily_returns[-1], -0.5, places=12)

    def test_next_open_transaction_cost_is_charged_once(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 110.0, 200.0],
            closes=[100.0, 110.0, 202.0],
        )

        result = run_momentum_vol_target_backtest(
            records,
            next_open_config(cost_bps=25.0),
        )

        expected_return = (1.0 - 0.0025) * (202.0 / 200.0) - 1.0
        self.assertAlmostEqual(result.daily_returns[1], expected_return, places=12)
        self.assertAlmostEqual(result.metrics["estimated_costs"], 0.0025, places=12)

    def test_intraday_weight_drift_is_rebalanced_on_the_following_open(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 100.0, 110.0, 220.0],
            closes=[100.0, 110.0, 220.0, 220.0],
            symbol="SPY",
        )
        records.extend(
            single_symbol_records(
                opens=[100.0, 100.0, 110.0, 121.0],
                closes=[100.0, 110.0, 121.0, 121.0],
                symbol="TLT",
            )
        )
        config = next_open_config(top_n=2, max_single_position=0.5)

        result = run_momentum_vol_target_backtest(records, config)

        closing_spy_weight = (0.5 * 2.0) / (1.0 + 0.5 * 1.0 + 0.5 * 0.1)
        expected_rebalance = 2.0 * abs(closing_spy_weight - 0.5)
        last_rebalance = sum(
            trade.turnover
            for trade in result.trades
            if trade.timestamp == "2024-02-04"
        )
        self.assertAlmostEqual(last_rebalance, expected_rebalance, places=12)

    def test_invalid_execution_open_fails_closed(self) -> None:
        records = single_symbol_records(
            opens=[100.0, 110.0, 0.0],
            closes=[100.0, 110.0, 202.0],
        )

        with self.assertRaisesRegex(ValueError, "invalid_open:SPY:2024-02-03"):
            run_momentum_vol_target_backtest(records, next_open_config())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# features/engineering.py — pure pytest tests
# ---------------------------------------------------------------------------

def _closes(n: int = 30, start: float = 100.0, step: float = 0.5) -> list[float]:
    return [start + i * step for i in range(n)]


# --- _ewm_last ---

def test_ewm_last_empty_returns_none() -> None:
    assert _ewm_last([], alpha=0.1) is None


def test_ewm_last_single_value() -> None:
    result = _ewm_last([5.0], alpha=0.1)
    assert result == pytest.approx(5.0)


def test_ewm_last_converges() -> None:
    values = [1.0] * 100 + [2.0] * 100
    result = _ewm_last(values, alpha=0.1)
    assert result is not None
    assert result > 1.9  # converged toward 2.0


# --- _rsi ---

@pytest.mark.parametrize("n_closes", [14, 20, 30])
def test_rsi_in_valid_range(n_closes: int) -> None:
    closes = _closes(n_closes)
    result = _rsi(closes, period=14)
    if result is not None:
        assert 0.0 <= result <= 100.0


def test_rsi_too_few_bars_returns_none() -> None:
    assert _rsi([100.0, 101.0], period=14) is None


def test_rsi_all_up_near_100() -> None:
    closes = [float(x) for x in range(100, 130)]  # monotonically increasing
    result = _rsi(closes, period=14)
    if result is not None:
        assert result > 70.0  # strong uptrend → high RSI


def test_rsi_all_down_near_0() -> None:
    closes = [float(x) for x in range(130, 100, -1)]  # monotonically decreasing
    result = _rsi(closes, period=14)
    if result is not None:
        assert result < 30.0  # strong downtrend → low RSI


# --- _macd_hist ---

def test_macd_hist_requires_enough_bars() -> None:
    assert _macd_hist([100.0] * 10, fast=12, slow=26, signal_period=9) is None


def test_macd_hist_returns_float_with_enough_bars() -> None:
    closes = _closes(60)
    result = _macd_hist(closes, fast=12, slow=26, signal_period=9)
    assert result is not None
    assert isinstance(result, float)


def test_macd_hist_uptrend_positive() -> None:
    closes = [100.0 + i * 2.0 for i in range(60)]
    result = _macd_hist(closes, fast=12, slow=26, signal_period=9)
    # Accelerating uptrend → histogram should be positive
    assert result is not None and result > 0


# --- _bb_pct_b ---

def test_bb_pct_b_requires_enough_bars() -> None:
    assert _bb_pct_b([100.0] * 5, period=20, n_std=2.0) is None


def test_bb_pct_b_flat_series_near_0_5() -> None:
    closes = [100.0] * 30
    result = _bb_pct_b(closes, period=20, n_std=2.0)
    # Flat → price at middle band → pct_b near 0.5 (or None if std=0)
    # With std=0, division by zero → returns None
    assert result is None or abs(result - 0.5) < 0.01


def test_bb_pct_b_above_upper_band() -> None:
    base = [100.0] * 25
    spike = [200.0]  # far above upper band
    result = _bb_pct_b(base + spike, period=20, n_std=2.0)
    if result is not None:
        assert result > 1.0  # above upper band → pct_b > 1


# --- build_features with extended indicators ---

def _sample_records(n: int = 30) -> list[dict]:
    base = 100.0
    records = []
    for i in range(n):
        close = base + i * 0.3
        records.append({
            "timestamp": f"2023-{(i // 28 + 1):02d}-{(i % 28 + 1):02d}",
            "symbol": "SPY",
            "open": close - 0.05,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 50_000_000.0,
        })
    return records


def test_build_features_base_keys_present() -> None:
    records = _sample_records(30)
    features = list(build_features(records, FeatureConfig()))
    assert len(features) > 0
    first = features[0]
    assert "return_1d" in first or "momentum_2" in first


def test_build_features_rsi_disabled_by_default() -> None:
    records = _sample_records(30)
    features = list(build_features(records, FeatureConfig()))
    assert all("rsi_" not in k for row in features for k in row)


def test_build_features_rsi_enabled() -> None:
    records = _sample_records(30)
    cfg = FeatureConfig(rsi_window=14)
    features = list(build_features(records, cfg))
    keys = {k for row in features for k in row}
    assert "rsi_14" in keys


def test_build_features_macd_enabled() -> None:
    records = _sample_records(60)
    cfg = FeatureConfig(macd_fast=12)
    features = list(build_features(records, cfg))
    keys = {k for row in features for k in row}
    assert "macd_hist" in keys


def test_build_features_bb_enabled() -> None:
    records = _sample_records(30)
    cfg = FeatureConfig(bb_window=20)
    features = list(build_features(records, cfg))
    keys = {k for row in features for k in row}
    assert "bb_pct_b" in keys


# ---------------------------------------------------------------------------
# research/metrics.py — stdlib unittest tests
# ---------------------------------------------------------------------------

class ResearchMetricsTests(unittest.TestCase):
    def test_cumulative_return(self) -> None:
        cases = [
            ([0.01, 0.02, -0.01], (1.01 * 1.02 * 0.99) - 1),
            ([0.0], 0.0),
            ([-0.5, 1.0], 0.0),
        ]
        for returns, expected in cases:
            with self.subTest(returns=returns):
                self.assertAlmostEqual(cumulative_return(returns), expected, places=6)

    def test_cumulative_return_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            cumulative_return([])

    def test_max_drawdown(self) -> None:
        cases = [
            ([0.1, 0.1, 0.1], 0.0),
            ([-0.5], 0.5),
            ([0.1, -0.2, 0.1], 0.2),
        ]
        for returns, expected in cases:
            with self.subTest(returns=returns):
                self.assertAlmostEqual(max_drawdown(returns), expected, places=4)

    def test_max_drawdown_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            max_drawdown([])

    def test_annualized_sharpe_constant_returns(self) -> None:
        result = annualized_sharpe([0.001] * 10, periods_per_year=252)
        self.assertEqual(result, 0.0)

    def test_annualized_sharpe_positive_trend(self) -> None:
        returns = [0.01 if i % 2 == 0 else 0.005 for i in range(50)]
        result = annualized_sharpe(returns, periods_per_year=252)
        self.assertGreater(result, 0.0)

    def test_annualized_sharpe_single_return(self) -> None:
        self.assertEqual(annualized_sharpe([0.01], periods_per_year=252), 0.0)

    def test_annualized_sharpe_negative_mean(self) -> None:
        returns = [-0.01] * 20 + [0.001] * 5
        result = annualized_sharpe(returns, periods_per_year=252)
        self.assertLess(result, 0.0)

    def test_estimate_slippage_bps_signs(self) -> None:
        cases = [
            (100.1, 100.0, "buy", 1),
            (99.9, 100.0, "sell", 1),
            (99.9, 100.0, "buy", -1),
            (100.0, 100.0, "buy", 0),
        ]
        for fill, ref, side, expected_sign in cases:
            with self.subTest(fill=fill, ref=ref, side=side):
                result = estimate_slippage_bps(fill_price=fill, reference_price=ref, side=side)
                if expected_sign == 0:
                    self.assertAlmostEqual(result, 0.0)
                elif expected_sign > 0:
                    self.assertGreater(result, 0.0)
                else:
                    self.assertLess(result, 0.0)

    def test_estimate_slippage_bps_zero_ref(self) -> None:
        self.assertEqual(estimate_slippage_bps(fill_price=100.0, reference_price=0.0, side="buy"), 0.0)

    def test_volatility_target_weight(self) -> None:
        cases = [
            (0.2, 0.15, 2.0, 0.75),
            (0.1, 0.15, 2.0, 1.5),
            (0.05, 0.15, 1.0, 1.0),
            (0.0, 0.15, 2.0, 0.0),
            (0.2, 0.0, 2.0, 0.0),
        ]
        for realized_vol, target_vol, max_lev, expected in cases:
            with self.subTest(realized_vol=realized_vol, target_vol=target_vol, max_lev=max_lev):
                result = volatility_target_weight(
                    realized_annual_volatility=realized_vol,
                    target_annual_volatility=target_vol,
                    max_leverage=max_lev,
                )
                self.assertAlmostEqual(result, expected)

    def test_volatility_target_weight_negative_leverage_raises(self) -> None:
        with self.assertRaises(ValueError):
            volatility_target_weight(realized_annual_volatility=0.2, target_annual_volatility=0.15, max_leverage=-1.0)
