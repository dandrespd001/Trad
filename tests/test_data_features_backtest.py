import unittest

from trading_ai.backtest.engine import BacktestConfig, run_momentum_vol_target_backtest
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.features.engineering import FeatureConfig, build_features


def sample_records() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
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
        self.assertAlmostEqual(spy_rows[2]["momentum_2"], 0.02)
        self.assertAlmostEqual(spy_rows[2]["sma_2"], 101.5)
        self.assertAlmostEqual(spy_rows[2]["relative_volume_2"], 1030 / 1025)
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
        self.assertAlmostEqual(spy_rows[2]["close_to_sma_2"], 102 / 101.5 - 1.0)
        self.assertIsNotNone(spy_rows[2]["realized_volatility_3"])
        self.assertAlmostEqual(
            spy_rows[2]["vol_adjusted_momentum_2"],
            spy_rows[2]["momentum_2"] / spy_rows[2]["realized_volatility_3"],
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


if __name__ == "__main__":
    unittest.main()
