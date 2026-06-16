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
        self.assertEqual(result.daily_returns, run_momentum_vol_target_backtest(sample_records(), result.config).daily_returns)


if __name__ == "__main__":
    unittest.main()
