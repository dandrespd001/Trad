import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.data.io import write_records
from trading_ai.data.manifest import dataset_hash
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    build_supervised_examples,
    evaluate_classifier,
    temporal_train_test_split,
    train_logistic_baseline,
)


def feature_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    closes = [100.0, 101.0, 102.0, 101.0, 100.0, 99.0, 101.0, 103.0]
    momentums = [0.02, 0.03, -0.02, -0.03, -0.02, 0.04, 0.05, 0.01]
    for index, (close, momentum) in enumerate(zip(closes, momentums, strict=True), start=1):
        rows.append(
            {
                "timestamp": f"2024-01-{index:02d}",
                "symbol": "SPY",
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000 + index,
                "momentum_2": momentum,
                "realized_volatility_3": 0.10,
                "relative_volume_2": 1.0,
            }
        )
    return rows


class ModelsBaselineTests(unittest.TestCase):
    def test_supervised_examples_use_next_close_without_future_features(self) -> None:
        examples = build_supervised_examples(feature_rows(), feature_names=("momentum_2",))

        self.assertEqual(len(examples), 7)
        self.assertEqual(examples[0].timestamp, "2024-01-01")
        self.assertEqual(examples[0].features, (0.02,))
        self.assertEqual(examples[0].target, 1)
        self.assertEqual(examples[2].target, 0)

    def test_temporal_split_keeps_training_rows_before_test_rows(self) -> None:
        examples = build_supervised_examples(feature_rows(), feature_names=("momentum_2",))

        split = temporal_train_test_split(examples, test_fraction=0.30)

        self.assertLess(max(sample.timestamp for sample in split.train), min(sample.timestamp for sample in split.test))
        self.assertEqual(len(split.train) + len(split.test), len(examples))

    def test_embargo_purges_boundary_training_rows(self) -> None:
        examples = build_supervised_examples(feature_rows(), feature_names=("momentum_2",))

        no_embargo = temporal_train_test_split(examples, test_fraction=0.30, embargo=0)
        embargoed = temporal_train_test_split(examples, test_fraction=0.30, embargo=1)

        # Same test set, but the embargo drops the last training example.
        self.assertEqual(embargoed.test, no_embargo.test)
        self.assertEqual(len(embargoed.train), len(no_embargo.train) - 1)
        self.assertEqual(embargoed.train, no_embargo.train[:-1])

    def test_embargo_negative_rejected(self) -> None:
        examples = build_supervised_examples(feature_rows(), feature_names=("momentum_2",))
        with self.assertRaises(ValueError):
            temporal_train_test_split(examples, test_fraction=0.30, embargo=-1)

    def test_embargo_cannot_consume_all_training_rows(self) -> None:
        examples = build_supervised_examples(feature_rows(), feature_names=("momentum_2",))
        with self.assertRaises(ValueError):
            temporal_train_test_split(examples, test_fraction=0.30, embargo=99)

    def test_logistic_baseline_trains_and_evaluates_on_directional_sample(self) -> None:
        examples = build_supervised_examples(feature_rows(), feature_names=("momentum_2",))
        split = temporal_train_test_split(examples, test_fraction=0.30)

        model = train_logistic_baseline(
            split.train,
            LogisticBaselineConfig(feature_names=("momentum_2",), learning_rate=0.5, epochs=200),
        )
        metrics = evaluate_classifier(model, split.test)

        self.assertEqual(model.feature_names, ("momentum_2",))
        self.assertGreaterEqual(metrics["sample_count"], 2)
        self.assertIn("accuracy", metrics)
        self.assertIn("log_loss", metrics)

    def test_train_and_evaluate_cli_write_reproducible_run_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "features.csv"
            model_output = root / "model.json"
            run_output = root / "run.json"
            eval_output = root / "eval.json"
            write_records(feature_rows(), dataset)

            train_exit = main(
                [
                    "train",
                    "--model",
                    "logistic-baseline",
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(model_output),
                    "--run-output",
                    str(run_output),
                ]
            )
            eval_exit = main(["evaluate", "--run-id", str(run_output), "--output", str(eval_output)])
            run_payload = json.loads(run_output.read_text(encoding="utf-8"))
            eval_payload = json.loads(eval_output.read_text(encoding="utf-8"))

        self.assertEqual(train_exit, 0)
        self.assertEqual(eval_exit, 0)
        self.assertEqual(run_payload["model_type"], "logistic-baseline")
        self.assertEqual(run_payload["dataset_hash"], dataset_hash(feature_rows()))
        self.assertEqual(eval_payload["run_id"], str(run_output))
        self.assertIn("test", eval_payload["metrics"])


if __name__ == "__main__":
    unittest.main()
