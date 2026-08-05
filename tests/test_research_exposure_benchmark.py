"""Tests for the pure exposure-matched benchmark helpers."""

from __future__ import annotations

import ast
import math
import unittest
from pathlib import Path

from trading_ai.research.exposure_benchmark import exposure_matched_spy_returns


class ExposureMatchedSpyReturnsTests(unittest.TestCase):
    def test_full_exposure_charges_only_initial_entry(self) -> None:
        actual = exposure_matched_spy_returns(
            (0.10, -0.05, 0.02),
            (1.0, 1.0, 1.0),
            one_way_cost_bps=2.0,
        )

        self.assertAlmostEqual(actual[0], (1.0 - 0.0002) * 1.10 - 1.0)
        self.assertAlmostEqual(actual[1], -0.05)
        self.assertAlmostEqual(actual[2], 0.02)

    def test_zero_exposure_remains_cash(self) -> None:
        self.assertEqual(
            exposure_matched_spy_returns(
                (0.10, -0.90, 0.25),
                (0.0, 0.0, 0.0),
                one_way_cost_bps=25.0,
            ),
            (0.0, 0.0, 0.0),
        )

    def test_exposure_changes_charge_absolute_turnover_both_directions(self) -> None:
        actual = exposure_matched_spy_returns(
            (0.10, 0.10, 0.10),
            (0.25, 0.75, 0.50),
            one_way_cost_bps=10.0,
        )

        expected = (
            (1.0 - 0.25 * 0.001) * (1.0 + 0.25 * 0.10) - 1.0,
            (1.0 - 0.50 * 0.001) * (1.0 + 0.75 * 0.10) - 1.0,
            (1.0 - 0.25 * 0.001) * (1.0 + 0.50 * 0.10) - 1.0,
        )
        for observed, wanted in zip(actual, expected, strict=True):
            self.assertAlmostEqual(observed, wanted)

    def test_inputs_are_copied_and_result_is_immutable_tuple(self) -> None:
        returns = [0.01, 0.02]
        exposures = [0.5, 0.5]
        actual = exposure_matched_spy_returns(
            iter(returns),
            iter(exposures),
            one_way_cost_bps=0.0,
        )
        returns[0] = 0.99
        exposures[0] = 1.0

        self.assertIsInstance(actual, tuple)
        self.assertAlmostEqual(actual[0], 0.005)
        self.assertAlmostEqual(actual[1], 0.01)

    def test_rejects_empty_misaligned_and_string_iterables(self) -> None:
        cases = (
            ((), (), 0.0),
            ((0.01,), (0.5, 0.5), 0.0),
            ("0.01", (0.5,), 0.0),
            ((0.01,), b"0.5", 0.0),
        )
        for returns, exposures, cost in cases:
            with self.subTest(returns=returns, exposures=exposures), self.assertRaises(
                (TypeError, ValueError)
            ):
                exposure_matched_spy_returns(
                    returns,  # type: ignore[arg-type]
                    exposures,  # type: ignore[arg-type]
                    one_way_cost_bps=cost,
                )

    def test_rejects_invalid_returns_exposures_and_costs(self) -> None:
        cases = (
            ((True,), (0.5,), 0.0),
            (("0.1",), (0.5,), 0.0),
            ((math.nan,), (0.5,), 0.0),
            ((-1.0,), (0.5,), 0.0),
            ((-1.01,), (0.5,), 0.0),
            ((0.1,), (True,), 0.0),
            ((0.1,), ("0.5",), 0.0),
            ((0.1,), (math.inf,), 0.0),
            ((0.1,), (-0.01,), 0.0),
            ((0.1,), (1.01,), 0.0),
            ((0.1,), (0.5,), True),
            ((0.1,), (0.5,), math.nan),
            ((0.1,), (0.5,), -0.01),
            ((0.1,), (0.5,), 10_000.0),
        )
        for returns, exposures, cost in cases:
            with self.subTest(
                returns=returns, exposures=exposures, cost=cost
            ), self.assertRaises((TypeError, ValueError)):
                exposure_matched_spy_returns(
                    returns,  # type: ignore[arg-type]
                    exposures,  # type: ignore[arg-type]
                    one_way_cost_bps=cost,
                )


class ExposureBenchmarkStaticBoundaryTests(unittest.TestCase):
    def test_module_is_pure_stdlib_without_authority_surfaces(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "trading_ai"
            / "research"
            / "exposure_benchmark.py"
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)

        self.assertLessEqual(
            imports,
            {"__future__", "collections.abc", "math", "typing"},
        )
        for forbidden in (
            "trading_ai.execution",
            "trading_ai.risk",
            "trading_ai.config",
            "approved_data",
            "subprocess",
            "pathlib",
            "socket",
            "requests",
            "urllib",
            "os.environ",
            "open(",
            "getattr(",
            "__builtins__",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
