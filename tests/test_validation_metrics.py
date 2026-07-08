"""Tests for statistical-validation metrics: PSR, DSR, Monte Carlo (Sprint J1)."""

from __future__ import annotations

import math
import unittest

from trading_ai.research.metrics import (
    _normal_cdf,
    _normal_ppf,
    deflated_sharpe_ratio,
    monte_carlo_drawdown,
    probabilistic_sharpe_ratio,
)


class NormalPpfTests(unittest.TestCase):
    def test_ppf_inverts_cdf(self) -> None:
        for p in (0.01, 0.1, 0.5, 0.9, 0.99):
            self.assertAlmostEqual(_normal_cdf(_normal_ppf(p)), p, places=8)

    def test_ppf_rejects_out_of_range(self) -> None:
        for bad in (0.0, 1.0, -0.1, 1.1):
            with self.assertRaises(ValueError):
                _normal_ppf(bad)


class ProbabilisticSharpeTests(unittest.TestCase):
    def test_range_and_midpoint(self) -> None:
        # observed == benchmark → numerator 0 → PSR 0.5.
        psr = probabilistic_sharpe_ratio(
            observed_sharpe=0.1, n_observations=100, benchmark_sharpe=0.1
        )
        self.assertAlmostEqual(psr, 0.5, places=10)

    def test_monotonic_in_observed_sharpe(self) -> None:
        low = probabilistic_sharpe_ratio(observed_sharpe=0.05, n_observations=250)
        high = probabilistic_sharpe_ratio(observed_sharpe=0.20, n_observations=250)
        self.assertLess(low, high)
        for value in (low, high):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_known_normal_value(self) -> None:
        # Canonical PSR denominator with skew=0, kurtosis=3 (normal) is
        # sqrt(1 + ((3-1)/4)*SR^2) = sqrt(1 + 0.5*SR^2), NOT 1.
        sr, n = 0.1, 101
        denom = math.sqrt(1.0 + 0.5 * sr ** 2)
        expected = _normal_cdf(sr * math.sqrt(n - 1) / denom)
        got = probabilistic_sharpe_ratio(observed_sharpe=sr, n_observations=n)
        self.assertAlmostEqual(got, expected, places=10)

    def test_rejects_small_n_and_bad_moments(self) -> None:
        with self.assertRaises(ValueError):
            probabilistic_sharpe_ratio(observed_sharpe=0.1, n_observations=1)
        with self.assertRaises(ValueError):
            # Degenerate moments driving the denominator non-positive.
            probabilistic_sharpe_ratio(
                observed_sharpe=5.0, n_observations=100, skew=1.0, kurtosis=1.0
            )


class DeflatedSharpeTests(unittest.TestCase):
    def test_single_trial_equals_psr_against_zero(self) -> None:
        common = dict(observed_sharpe=0.15, n_observations=250)
        dsr = deflated_sharpe_ratio(**common, n_trials=1, variance_of_trial_sharpes=0.04)
        psr = probabilistic_sharpe_ratio(**common, benchmark_sharpe=0.0)
        self.assertAlmostEqual(dsr, psr, places=10)

    def test_more_trials_deflate(self) -> None:
        common = dict(observed_sharpe=0.15, n_observations=250)
        psr = probabilistic_sharpe_ratio(**common, benchmark_sharpe=0.0)
        dsr = deflated_sharpe_ratio(**common, n_trials=50, variance_of_trial_sharpes=0.04)
        # A positive multiple-testing benchmark must lower the probability.
        self.assertLess(dsr, psr)

    def test_rejects_bad_args(self) -> None:
        with self.assertRaises(ValueError):
            deflated_sharpe_ratio(
                observed_sharpe=0.1, n_observations=250, n_trials=0,
                variance_of_trial_sharpes=0.01,
            )
        with self.assertRaises(ValueError):
            deflated_sharpe_ratio(
                observed_sharpe=0.1, n_observations=250, n_trials=5,
                variance_of_trial_sharpes=-0.01,
            )


class MonteCarloDrawdownTests(unittest.TestCase):
    RETURNS = [0.01, -0.02, 0.015, -0.03, 0.02, -0.01, 0.005, -0.04, 0.03, -0.015]

    def test_deterministic_given_seed(self) -> None:
        a = monte_carlo_drawdown(self.RETURNS, n_simulations=200, seed=7)
        b = monte_carlo_drawdown(self.RETURNS, n_simulations=200, seed=7)
        self.assertEqual(a, b)

    def test_ordered_percentiles_and_bounds(self) -> None:
        result = monte_carlo_drawdown(self.RETURNS, n_simulations=500, seed=1)
        self.assertLessEqual(result["p5"], result["p50"])
        self.assertLessEqual(result["p50"], result["p95"])
        self.assertLessEqual(result["p95"], result["worst"])
        for key in ("p5", "p50", "p95", "mean", "worst"):
            self.assertGreaterEqual(result[key], 0.0)
            self.assertLessEqual(result[key], 1.0)
        self.assertEqual(result["n_simulations"], 500.0)

    def test_shuffle_preserves_multiset(self) -> None:
        # With shuffle, every path is a permutation → identical cumulative return,
        # so its max drawdown distribution is bounded by the same multiset.
        result = monte_carlo_drawdown(self.RETURNS, n_simulations=50, seed=3, method="shuffle")
        self.assertGreaterEqual(result["worst"], result["p50"])

    def test_rejects_bad_args(self) -> None:
        with self.assertRaises(ValueError):
            monte_carlo_drawdown(self.RETURNS, n_simulations=0)
        with self.assertRaises(ValueError):
            monte_carlo_drawdown(self.RETURNS, method="bogus")
        with self.assertRaises(ValueError):
            monte_carlo_drawdown([])


if __name__ == "__main__":
    unittest.main()
