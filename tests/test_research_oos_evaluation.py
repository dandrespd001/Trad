import ast
import math
import random
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from trading_ai.research.oos_evaluation import (
    buy_and_hold_next_open_returns,
    evaluate_oos,
    seal_trial_record,
)


class BuyAndHoldBenchmarkTests(unittest.TestCase):
    def test_enters_on_second_oos_open_charges_once_and_has_no_terminal_cost(self) -> None:
        returns = buy_and_hold_next_open_returns(
            ("2026-01-02", "2026-01-05", "2026-01-06"),
            (100.0, 110.0, 120.0),
            (105.0, 121.0, 132.0),
            one_way_cost_bps=2.0,
        )

        self.assertEqual(returns[0], 0.0)
        self.assertAlmostEqual(returns[1], (1.0 - 0.0002) * (121.0 / 110.0) - 1.0)
        self.assertAlmostEqual(returns[2], 132.0 / 121.0 - 1.0)

    def test_rejects_alignment_invalid_prices_and_invalid_costs(self) -> None:
        sessions = ("2026-01-02", "2026-01-05")
        cases = (
            ((100.0,), (101.0, 102.0), 2.0),
            ((100.0, 0.0), (101.0, 102.0), 2.0),
            ((100.0, 101.0), (101.0, math.nan), 2.0),
            ((100.0, 101.0), (101.0, 102.0), True),
            ((100.0, 101.0), (101.0, 102.0), -1.0),
            ((100.0, 101.0), (101.0, 102.0), 10_000.0),
        )
        for opens, closes, cost in cases:
            with self.subTest(opens=opens, closes=closes, cost=cost), self.assertRaises(
                (TypeError, ValueError)
            ):
                buy_and_hold_next_open_returns(
                    sessions,
                    opens,
                    closes,
                    one_way_cost_bps=cost,
                )


class OosEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = ("2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07")
        self.strategy = (0.01, -0.02, 0.03, 0.0)
        self.benchmark = (0.005, -0.01, 0.01, 0.0)
        self.folds = ("fold_1", "fold_1", "fold_2", "fold_2")
        self.kwargs = {
            "periods_per_year": 252,
            "block_size": 2,
            "n_resamples": 25,
            "seed": 20260727,
        }

    def evaluate(self, **overrides: object):
        inputs = {
            "session_ids": self.sessions,
            "strategy_returns": self.strategy,
            "benchmark_returns": self.benchmark,
            "fold_ids": self.folds,
            **self.kwargs,
            **overrides,
        }
        return evaluate_oos(**inputs)  # type: ignore[arg-type]

    def test_valid_series_reports_aligned_aggregate_and_fold_metrics(self) -> None:
        result = self.evaluate()

        self.assertEqual(result.strategy.observations, 4)
        self.assertEqual(result.active_returns, (0.005, -0.01, 0.019999999999999997, 0.0))
        self.assertEqual(len(result.folds), 2)
        self.assertEqual(result.folds[0].start_session, "2026-01-02")
        self.assertEqual(result.folds[0].end_session, "2026-01-05")
        self.assertEqual(result.folds[1].start_session, "2026-01-06")
        self.assertEqual(result.folds[1].end_session, "2026-01-07")
        strategy_equity = math.prod(1.0 + value for value in self.strategy)
        benchmark_equity = math.prod(1.0 + value for value in self.benchmark)
        self.assertAlmostEqual(
            result.compound_active_return,
            strategy_equity / benchmark_equity - 1.0,
        )
        fold_average = sum(fold.active_sharpe for fold in result.folds) / len(result.folds)
        self.assertNotAlmostEqual(result.active_sharpe, fold_average)

    def test_inputs_are_copied_and_result_is_frozen(self) -> None:
        strategy = list(self.strategy)
        result = self.evaluate(strategy_returns=strategy)
        strategy[0] = 0.99

        self.assertEqual(result.strategy_returns, self.strategy)
        with self.assertRaises(FrozenInstanceError):
            result.input_sha256 = "changed"  # type: ignore[misc]

    def test_identical_strategy_and_benchmark_have_zero_active_result(self) -> None:
        result = self.evaluate(strategy_returns=self.benchmark)

        self.assertEqual(result.compound_active_return, 0.0)
        self.assertTrue(all(value == 0.0 for value in result.active_returns))
        self.assertEqual(result.bootstrap.compound_active_return_p05, 0.0)
        self.assertEqual(result.bootstrap.probability_compound_active_positive, 0.0)

    def test_uniformly_inferior_strategy_has_negative_active_result(self) -> None:
        result = self.evaluate(
            strategy_returns=(-0.01, -0.01, -0.01, -0.01),
            benchmark_returns=(0.0, 0.0, 0.0, 0.0),
        )

        self.assertLess(result.compound_active_return, 0.0)
        self.assertLess(result.bootstrap.compound_active_return_p95, 0.0)

    def test_bootstrap_and_input_hash_are_deterministic(self) -> None:
        random_state = random.getstate()
        first = self.evaluate()
        second = self.evaluate()

        self.assertEqual(first, second)
        self.assertEqual(random.getstate(), random_state)
        changed = self.evaluate(seed=20260728)
        self.assertNotEqual(first.input_sha256, changed.input_sha256)
        self.assertNotEqual(first.bootstrap, changed.bootstrap)

    def test_rejects_misaligned_or_noncontiguous_folds(self) -> None:
        cases = (
            {"benchmark_returns": self.benchmark[:-1]},
            {"fold_ids": ("fold_1", "fold_2", "fold_1", "fold_2")},
            {"fold_ids": ("fold_1",) * 4},
            {"fold_ids": ("fold_1", "fold_2", "fold_2", "fold_2")},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.evaluate(**overrides)

    def test_rejects_invalid_sessions(self) -> None:
        cases = (
            (),
            ("2026-01-02", "2026-01-02", "2026-01-06", "2026-01-07"),
            ("2026-01-02", "", "2026-01-06", "2026-01-07"),
            "2026-01-02",
        )
        for sessions in cases:
            with self.subTest(sessions=sessions), self.assertRaises((TypeError, ValueError)):
                self.evaluate(session_ids=sessions)

    def test_rejects_nonfinite_non_numeric_boolean_and_total_loss_returns(self) -> None:
        invalid_values = (math.nan, math.inf, -math.inf, -1.0, -1.01, True, "0.1")
        for field in ("strategy_returns", "benchmark_returns"):
            for invalid in invalid_values:
                values = list(self.strategy if field == "strategy_returns" else self.benchmark)
                values[1] = invalid  # type: ignore[assignment]
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    self.evaluate(**{field: values})

    def test_rejects_invalid_integer_controls_and_work_bounds(self) -> None:
        cases = (
            {"periods_per_year": True},
            {"periods_per_year": 0},
            {"block_size": 0},
            {"block_size": 5},
            {"n_resamples": 0},
            {"n_resamples": 100_001},
            {"seed": False},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises((TypeError, ValueError)):
                self.evaluate(**overrides)


class TrialRecordTests(unittest.TestCase):
    def test_mapping_order_and_negative_zero_are_canonical(self) -> None:
        first = seal_trial_record(
            {"trial": "etf-001", "nested": {"b": -0.0, "a": [1, 2.0]}, "research_only": True}
        )
        second = seal_trial_record(
            {"research_only": True, "nested": {"a": (1, 2.0), "b": 0.0}, "trial": "etf-001"}
        )

        self.assertEqual(first, second)
        self.assertNotIn("-0.0", first.canonical_json)
        self.assertRegex(first.sha256, r"^[0-9a-f]{64}$")

    def test_material_input_change_changes_digest(self) -> None:
        base = {"trial": "etf-001", "seed": 1, "returns": [0.01, -0.01]}
        first = seal_trial_record(base)

        for key, value in (
            ("seed", 2),
            ("returns", [0.01, -0.02]),
            ("cost_multiplier", 1.5),
        ):
            changed = dict(base)
            changed[key] = value
            with self.subTest(key=key):
                self.assertNotEqual(first.sha256, seal_trial_record(changed).sha256)

    def test_rejects_nonfinite_keys_and_unsupported_types(self) -> None:
        invalid_payloads = (
            {1: "not-a-string-key"},
            {"value": math.nan},
            {"value": math.inf},
            {"value": {1, 2}},
            {"value": object()},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises((TypeError, ValueError)):
                seal_trial_record(payload)  # type: ignore[arg-type]


class OosEvaluationStaticBoundaryTests(unittest.TestCase):
    def test_module_keeps_the_pure_research_import_boundary(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "trading_ai"
            / "research"
            / "oos_evaluation.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module)

        allowed = {
            "__future__",
            "collections.abc",
            "dataclasses",
            "hashlib",
            "json",
            "math",
            "trading_ai.research.block_bootstrap",
            "trading_ai.research.metrics",
            "typing",
        }
        self.assertLessEqual(imported_roots, allowed)
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "trading_ai.execution",
            "trading_ai.risk",
            "trading_ai.config",
            "subprocess",
            "pathlib",
            "socket",
            "requests",
            "os.environ",
            "open(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
