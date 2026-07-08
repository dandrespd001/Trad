"""Tests for wiring the LightGBM non-linear baseline into ``train`` (Sprint I1).

Drives the CLI end-to-end. The LightGBM-specific cases are skipped when the
optional ``ml`` extras are not installed (the stdlib gate runs on a Python
without lightgbm); the walk-forward generalization and argument-guard tests do
not need lightgbm and always run.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser
from trading_ai.data.io import write_records
from trading_ai.features.engineering import FeatureConfig, build_features
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    build_supervised_examples,
    train_logistic_baseline,
    walk_forward_evaluate,
)

_HAS_LIGHTGBM = importlib.util.find_spec("lightgbm") is not None


def _base_records(days: int = 160) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base_close = 100.0
    for index in range(days):
        month = 1 + index // 28
        day = 1 + index % 28
        close = base_close + index * 0.4 + (3.0 if index % 5 == 0 else 0.0)
        rows.append(
            {
                "timestamp": f"2024-{month:02d}-{day:02d}",
                "symbol": "SPY",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + index * 1000,
            }
        )
    return rows


def _feature_rows(days: int = 160) -> list[dict[str, Any]]:
    return build_features(_base_records(days=days), FeatureConfig())


class WalkForwardTrainFnTests(unittest.TestCase):
    """The train_fn generalization must not change the logistic default path."""

    def test_default_path_matches_explicit_logistic_train_fn(self) -> None:
        examples = build_supervised_examples(
            _feature_rows(), feature_names=("return_1d", "momentum_20", "momentum_60")
        )
        config = LogisticBaselineConfig(
            feature_names=("return_1d", "momentum_20", "momentum_60")
        )
        common = dict(min_train_size=40, test_size=20, embargo=0)

        default = walk_forward_evaluate(examples, config, **common)
        explicit = walk_forward_evaluate(
            examples,
            config,
            **common,
            train_fn=lambda rows: train_logistic_baseline(rows, config),
        )
        # Same models → identical window metrics and mean accuracy.
        self.assertEqual(default["window_count"], explicit["window_count"])
        self.assertAlmostEqual(
            float(default["mean_accuracy"]), float(explicit["mean_accuracy"]), places=12
        )


class TrainLightgbmCliTests(unittest.TestCase):
    def _run_train(self, dataset: Path, model_output: Path, run_output: Path, *extra: str) -> tuple[int, str]:
        argv = [
            "train",
            "--model",
            "lightgbm-baseline",
            "--dataset",
            str(dataset),
            "--output",
            str(model_output),
            "--run-output",
            str(run_output),
            *extra,
        ]
        parser = build_parser()
        args = parser.parse_args(argv)
        err_buffer = io.StringIO()
        with redirect_stderr(err_buffer):
            exit_code = args.func(args)
        return exit_code, err_buffer.getvalue()

    def test_standardize_flag_rejected_for_lightgbm(self) -> None:
        # This guard fires before any model import, so it runs without lightgbm.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            write_records(_feature_rows(), dataset)
            exit_code, stderr = self._run_train(
                dataset, root / "m.json", root / "r.json", "--standardize-features"
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("--standardize-features", stderr)
        self.assertIn("lightgbm-baseline", stderr)

    @unittest.skipUnless(_HAS_LIGHTGBM, "requires the 'ml' optional extras")
    def test_lightgbm_run_records_metrics_and_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            model_output = root / "model.json"
            run_output = root / "run.json"
            write_records(_feature_rows(), dataset)

            exit_code, stderr = self._run_train(
                dataset,
                model_output,
                run_output,
                "--feature-names",
                "return_1d,momentum_20,momentum_60",
            )
            self.assertEqual(exit_code, 0, msg=stderr)
            run_payload = json.loads(run_output.read_text(encoding="utf-8"))
            model_payload = json.loads(model_output.read_text(encoding="utf-8"))

        self.assertEqual(run_payload["model_type"], "lightgbm-baseline")
        self.assertEqual(run_payload["standardized"], False)
        for split_name in ("train", "test", "walk_forward"):
            self.assertIn(split_name, run_payload["metrics"])
        test_metrics = run_payload["metrics"]["test"]
        self.assertGreaterEqual(test_metrics["accuracy"], 0.0)
        self.assertLessEqual(test_metrics["accuracy"], 1.0)
        # The descriptor is not a logistic model artifact.
        self.assertEqual(model_payload["model_type"], "lightgbm-baseline")
        self.assertNotIn("coefficients", model_payload)

    @unittest.skipUnless(_HAS_LIGHTGBM, "requires the 'ml' optional extras")
    def test_lightgbm_supports_triple_barrier_labeling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            run_output = root / "run.json"
            write_records(_feature_rows(), dataset)

            exit_code, stderr = self._run_train(
                dataset,
                root / "model.json",
                run_output,
                "--feature-names",
                "return_1d,momentum_20,momentum_60,atr_14",
                "--labeling",
                "triple_barrier",
                "--label-horizon",
                "5",
            )
            self.assertEqual(exit_code, 0, msg=stderr)
            run_payload = json.loads(run_output.read_text(encoding="utf-8"))

        self.assertEqual(run_payload["labeling"], "triple_barrier")
        self.assertIn("label_positive_rate", run_payload)


if __name__ == "__main__":
    unittest.main()
