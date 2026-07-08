import math
import unittest

from trading_ai.research.metrics import (
    annualized_sharpe,
    annualized_sortino,
    cumulative_return,
    directional_bias,
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

    def test_annualized_sortino_exceeds_sharpe_for_positive_skew(self) -> None:
        """Strategies with small losses and large gains must score higher on
        Sortino than on Sharpe because the numerator is the same but the
        denominator only counts downside variation."""

        small_losses_large_gains = [0.10, 0.08, -0.005, -0.003, 0.07, 0.06]
        sortino = annualized_sortino(small_losses_large_gains, periods_per_year=252)
        sharpe = annualized_sharpe(small_losses_large_gains, periods_per_year=252)
        self.assertGreater(sortino, sharpe)

    def test_annualized_sortino_zero_for_empty_series(self) -> None:
        """Empty input must fail closed (0.0) like ``annualized_sharpe`` with
        zero volatility -- never ``+inf`` or ``NaN``."""

        self.assertEqual(annualized_sortino([], periods_per_year=252), 0.0)

    def test_annualized_sortino_zero_when_all_returns_meet_target(self) -> None:
        """Documented degenerate case: no downside variation. Follows the repo
        convention used by ``annualized_sharpe`` (zero volatility -> 0.0) so
        callers can treat zero-volatility series uniformly."""

        all_positive = [0.01, 0.02, 0.015, 0.005]
        self.assertEqual(annualized_sortino(all_positive, periods_per_year=252), 0.0)

        # Single-observation series is also degenerate (len < 2).
        self.assertEqual(annualized_sortino([0.01], periods_per_year=252), 0.0)

    def test_annualized_sortino_matches_textbook_formula(self) -> None:
        """Reference computation:
        ``downside_dev = sqrt(sum(min(r - target, 0)^2)/N)``;
        ``sortino = (mean - target) / downside_dev * sqrt(P)``.
        """

        returns = [0.05, 0.03, -0.02, -0.01, 0.04]
        result = annualized_sortino(returns, periods_per_year=252, target_return=0.0)
        n = len(returns)
        mean = sum(returns) / n
        downside_sq = sum(min(value, 0.0) ** 2 for value in returns) / n
        expected = mean / math.sqrt(downside_sq) * math.sqrt(252)
        self.assertAlmostEqual(result, expected, places=6)

    def test_directional_bias_thirds_pattern(self) -> None:
        """Two positive + one negative out of three -> (2/3 - 1/3) = 1/3."""

        self.assertAlmostEqual(directional_bias([0.01, 0.02, -0.01]), 1.0 / 3.0, places=12)

    def test_directional_bias_zero_returns_are_ignored(self) -> None:
        """Zero-return periods contribute to neither side: 1 positive, 1 zero,
        1 negative still yields (1/3 - 1/3) = 0."""

        self.assertEqual(directional_bias([0.01, 0.0, -0.01]), 0.0)

    def test_directional_bias_empty_series(self) -> None:
        """Empty input returns 0.0 (balanced unknown) rather than raising."""

        self.assertEqual(directional_bias([]), 0.0)

    def test_directional_bias_all_negative_returns(self) -> None:
        """Every period negative -> -1.0."""

        self.assertEqual(directional_bias([-0.01, -0.005, -0.002]), -1.0)


if __name__ == "__main__":
    unittest.main()
