import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.data.io import write_records
from trading_ai.data.manifest import build_dataset_manifest, dataset_hash


def small_records() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2024-01-01",
            "symbol": "SPY",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        },
        {
            "timestamp": "2024-01-02",
            "symbol": "SPY",
            "open": 100.5,
            "high": 102.0,
            "low": 100.0,
            "close": 101.5,
            "volume": 1100,
        },
        {
            "timestamp": "2024-01-03",
            "symbol": "SPY",
            "open": 101.5,
            "high": 103.0,
            "low": 101.0,
            "close": 102.5,
            "volume": 1200,
        },
    ]


class ReproducibilityTests(unittest.TestCase):
    def test_dataset_hash_is_stable_for_same_rows_in_different_order(self) -> None:
        records = small_records()

        self.assertEqual(dataset_hash(records), dataset_hash(reversed(records)))

    def test_dataset_manifest_records_hash_shape_and_symbols(self) -> None:
        manifest = build_dataset_manifest(small_records(), source="unit-test")

        self.assertEqual(manifest["source"], "unit-test")
        self.assertEqual(manifest["row_count"], 3)
        self.assertEqual(manifest["symbols"], ["SPY"])
        self.assertEqual(manifest["start"], "2024-01-01")
        self.assertEqual(manifest["end"], "2024-01-03")
        self.assertEqual(len(manifest["dataset_hash"]), 64)

    def test_backtest_cli_writes_dataset_hash_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.csv"
            output = root / "backtest.json"
            report = root / "report.md"
            write_records(small_records(), dataset)

            exit_code = main(
                [
                    "backtest",
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(output),
                    "--report-output",
                    str(report),
                ]
            )

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["metadata"]["dataset_path"], str(dataset))
        self.assertEqual(payload["metadata"]["dataset_hash"], dataset_hash(small_records()))

    def test_manifest_cli_writes_dataset_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.csv"
            output = root / "manifest.json"
            write_records(small_records(), dataset)

            exit_code = main(["manifest", "--dataset", str(dataset), "--output", str(output)])

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["dataset_path"], str(dataset))
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["dataset_hash"], dataset_hash(small_records()))


if __name__ == "__main__":
    unittest.main()
