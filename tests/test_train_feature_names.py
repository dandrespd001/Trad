"""Tests for the ``train`` subcommand's --feature-names opt-in (Sprint G3).

Drives the CLI end-to-end (no edits to internal modules); the existing
``test_models_baseline.py::test_train_and_evaluate_cli_write_reproducible_run_files``
covers the default path and remains untouched.
"""

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


def _base_records(days: int = 120) -> list[dict[str, Any]]:
    """Deterministic OHLCV fixture: monotonic uptrend for SPY, ``days`` bars."""
    rows: list[dict[str, Any]] = []
    base_close = 100.0
    for index in range(days):
        timestamp = f"2024-01-{(index % 28) + 1:02d}"
        # Only the first ~28 indices span one month; bump month to keep
        # timestamps unique and monotonically increasing.
        if index >= 28:
            timestamp = f"2024-02-{(index - 28) + 1:02d}"
        close = base_close + index * 0.5
        rows.append(
            {
                "timestamp": timestamp,
                "symbol": "SPY",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + index * 1000,
            }
        )
    return rows


def _baseline_feature_rows(days: int = 120) -> list[dict[str, Any]]:
    """Run ``build_features`` with default FeatureConfig so the dataset carries
    baseline-candidate columns (``return_1d``, ``momentum_*``, ...).
    """
    return build_features(_base_records(days=days), FeatureConfig())


def _extended_feature_rows(days: int = 120) -> list[dict[str, Any]]:
    """Build rows that include rsi_14/macd_hist/bb_pct_b via extended FeatureConfig."""
    return build_features(
        _base_records(days=days),
        FeatureConfig(rsi_window=14, macd_fast=12, bb_window=20),
    )


class TrainFeatureNamesTests(unittest.TestCase):
    def _run_train(self, dataset: Path, model_output: Path, run_output: Path, *extra: str) -> tuple[int, str]:
        argv = [
            "train",
            "--model",
            "logistic-baseline",
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
        # Drive ``_train`` directly so we can capture stderr deterministically
        # (no need to fight ``sys.exit`` propagation through the CLI wrapper).
        err_buffer = io.StringIO()
        with redirect_stderr(err_buffer):
            exit_code = args.func(args)
        return exit_code, err_buffer.getvalue()

    def test_feature_names_default_still_records_feature_source_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            model_output = root / "model.json"
            run_output = root / "run.json"
            write_records(_baseline_feature_rows(), dataset)

            exit_code, stderr = self._run_train(dataset, model_output, run_output)
            self.assertEqual(exit_code, 0, msg=stderr)
            payload = json.loads(run_output.read_text(encoding="utf-8"))
            model_payload = json.loads(model_output.read_text(encoding="utf-8"))

        self.assertEqual(payload["feature_source"], "default")
        # The default path must remain byte-identical for legacy callers:
        # ``feature_names`` is the union of candidates that carry finite values.
        self.assertIn("return_1d", payload["feature_names"])
        self.assertTrue(payload["feature_names"], "default path must yield at least one feature")
        # Model artifact carries the same names (no surprise drift downstream).
        self.assertEqual(model_payload["feature_names"], payload["feature_names"])

    def test_feature_names_explicit_extended_records_feature_source_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            model_output = root / "model.json"
            run_output = root / "run.json"
            # Build a fixture that actually contains the extended indicators.
            write_records(_extended_feature_rows(), dataset)

            exit_code, stderr = self._run_train(
                dataset,
                model_output,
                run_output,
                "--feature-names",
                "rsi_14,bb_pct_b",
            )
            self.assertEqual(exit_code, 0, msg=stderr)
            payload = json.loads(run_output.read_text(encoding="utf-8"))
            model_payload = json.loads(model_output.read_text(encoding="utf-8"))

        self.assertEqual(payload["feature_source"], "explicit")
        self.assertEqual(payload["feature_names"], ["rsi_14", "bb_pct_b"])
        # Model artifact itself must carry the same names so downstream
        # ``evaluate`` / ``promote`` does not silently drift.
        self.assertEqual(model_payload["feature_names"], ["rsi_14", "bb_pct_b"])

    def test_feature_names_unknown_name_exits_two_and_lists_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            model_output = root / "model.json"
            run_output = root / "run.json"
            write_records(_baseline_feature_rows(), dataset)

            exit_code, stderr = self._run_train(
                dataset,
                model_output,
                run_output,
                "--feature-names",
                "totally_made_up_indicator",
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--feature-names", stderr)
        self.assertIn("totally_made_up_indicator", stderr)
        # The error path must not silently fall through to writing a model.
        self.assertFalse(model_output.exists())
        self.assertFalse(run_output.exists())


if __name__ == "__main__":
    unittest.main()