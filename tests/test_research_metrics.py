import math
import unittest

from trading_ai.research.metrics import (
    annualized_sharpe,
    cumulative_return,
    max_drawdown,
    volatility_target_weight,
)


class ResearchMetricsTests(unittest.TestCase):
    def test_cumulative_return_compounds_period_returns(self) -> None:
        result = cumulative_return([0.10, -0.05, 0.02])

        self.assertAlmostEqual(result, 0.0659, places=6)

    def test_max_drawdown_returns_largest_peak_to_trough_loss(self) -> None:
        result = max_drawdown([0.10, -0.20, 0.05, -0.10])

        self.assertAlmostEqual(result, 0.244, places=6)

    def test_annualized_sharpe_uses_sample_volatility(self) -> None:
        result = annualized_sharpe([0.01, 0.02, -0.01, 0.00], periods_per_year=252)
        expected_mean = 0.005
        expected_std = math.sqrt(sum((value - expected_mean) ** 2 for value in [0.01, 0.02, -0.01, 0.00]) / 3)
        expected = expected_mean / expected_std * math.sqrt(252)

        self.assertAlmostEqual(result, expected, places=6)

    def test_volatility_target_weight_caps_leverage(self) -> None:
        result = volatility_target_weight(
            realized_annual_volatility=0.05,
            target_annual_volatility=0.12,
            max_leverage=1.5,
        )

        self.assertEqual(result, 1.5)

    def test_volatility_target_weight_returns_zero_when_volatility_is_invalid(self) -> None:
        self.assertEqual(
            volatility_target_weight(
                realized_annual_volatility=0.0,
                target_annual_volatility=0.12,
                max_leverage=1.5,
            ),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
