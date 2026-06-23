import json
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path

from trading_ai.cli import main
from trading_ai.data.freshness import evaluate_ohlcv_freshness
from trading_ai.data.io import ParquetDependencyError, ensure_parquet_support, write_records
from trading_ai.data.market_data import ApprovedCsvMarketDataProvider, MarketDataRequest
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.models.baseline import LogisticBaselineModel, save_model


def write_universe(path: Path, symbols: tuple[str, ...]) -> None:
    path.write_text(
        textwrap.dedent(
            f"""
            universe:
              symbols: [{", ".join(symbols)}]
            """
        ),
        encoding="utf-8",
    )


def _parquet_available() -> bool:
    try:
        ensure_parquet_support()
    except ParquetDependencyError:
        return False
    return True


class DataRefreshTests(unittest.TestCase):
    def test_approved_csv_provider_filters_normalizes_and_sorts_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.csv"
            write_records(
                [
                    {
                        "timestamp": "2026-06-03",
                        "symbol": "qqq",
                        "open": 101,
                        "high": 102,
                        "low": 100,
                        "close": 101.5,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-05-31",
                        "symbol": "SPY",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-02",
                        "symbol": "spy",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-01",
                        "symbol": "TSLA",
                        "open": 200,
                        "high": 201,
                        "low": 199,
                        "close": 200.5,
                        "volume": 1000,
                    },
                ],
                source,
            )

            records = ApprovedCsvMarketDataProvider(source).load(
                MarketDataRequest(symbols=("SPY", "QQQ"), start="2026-06-01", end="2026-06-03")
            )

        self.assertEqual(
            [(row["timestamp"], row["symbol"]) for row in records],
            [("2026-06-02", "SPY"), ("2026-06-03", "QQQ")],
        )

    def test_freshness_allows_all_symbols_with_recent_valid_rows(self) -> None:
        result = evaluate_ohlcv_freshness(
            [
                {"timestamp": "2026-06-15", "symbol": "SPY"},
                {"timestamp": "2026-06-16", "symbol": "QQQ"},
            ],
            expected_symbols=("SPY", "QQQ"),
            as_of_date=date(2026, 6, 16),
            max_age_days=5,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.reasons, ())

    def test_freshness_blocks_empty_inputs(self) -> None:
        result = evaluate_ohlcv_freshness(
            [],
            expected_symbols=("SPY",),
            as_of_date=date(2026, 6, 16),
            max_age_days=5,
        )

        self.assertFalse(result.allowed)
        self.assertIn("empty_dataset", result.reasons)

    def test_freshness_blocks_stale_missing_future_and_invalid_timestamps(self) -> None:
        result = evaluate_ohlcv_freshness(
            [
                {"timestamp": "2026-06-01", "symbol": "SPY"},
                {"timestamp": "2026-06-17", "symbol": "QQQ"},
                {"timestamp": "not-a-date", "symbol": "TLT"},
            ],
            expected_symbols=("SPY", "QQQ", "TLT", "GLD"),
            as_of_date=date(2026, 6, 16),
            max_age_days=5,
        )

        self.assertFalse(result.allowed)
        self.assertIn("stale_symbol", result.reasons)
        self.assertIn("future_timestamp", result.reasons)
        self.assertIn("invalid_timestamp", result.reasons)
        self.assertIn("missing_symbol", result.reasons)

    def test_refresh_data_cli_blocks_invalid_source_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = root / "universe.yml"
            model = root / "model.json"
            output_dir = root / "fresh_data"
            write_universe(config, ("SPY",))
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
                str(model),
            )
            source.write_text(
                "timestamp,symbol,open,high,low,close,volume\nnot-a-date,SPY,1,1,1,1,100\n",
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "refresh-data",
                    "--source-csv",
                    str(source),
                    "--config",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--from",
                    "2026-06-01",
                    "--to",
                    "2026-06-16",
                    "--as-of-date",
                    "2026-06-16",
                    "--output-dir",
                    str(output_dir),
                ]
            )

            freshness = json.loads((output_dir / "freshness.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(freshness["allowed"])
        self.assertEqual(freshness["feature_names"], [])
        self.assertFalse(freshness["validation"]["valid"])
        self.assertIn("row 0 invalid timestamp: not-a-date", freshness["validation"]["errors"])

    def test_refresh_data_cli_writes_artifacts_for_fresh_approved_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = root / "universe.yml"
            model = root / "model.json"
            output_dir = root / "fresh_data"
            write_universe(config, ("SPY", "QQQ"))
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
                str(model),
            )
            write_records(
                generate_sample_ohlcv(symbols=("SPY", "QQQ", "TSLA"), start="2026-03-01", end="2026-06-16"),
                source,
            )

            exit_code = main(
                [
                    "refresh-data",
                    "--source-csv",
                    str(source),
                    "--from",
                    "2026-03-01",
                    "--to",
                    "2026-06-16",
                    "--config",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--as-of-date",
                    "2026-06-16",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            freshness = json.loads((output_dir / "freshness.json").read_text(encoding="utf-8"))
            artifacts = {
                name: (output_dir / name).exists()
                for name in ("raw.csv", "features.csv", "raw_manifest.json", "features_manifest.json")
            }

        self.assertEqual(exit_code, 0)
        self.assertTrue(freshness["allowed"])
        self.assertEqual(freshness["reasons"], [])
        self.assertTrue(artifacts["raw.csv"])
        self.assertTrue(artifacts["features.csv"])
        self.assertTrue(artifacts["raw_manifest.json"])
        self.assertTrue(artifacts["features_manifest.json"])
        self.assertEqual(freshness["symbols"]["SPY"]["timestamp"], "2026-06-16")
        self.assertEqual(freshness["symbols"]["QQQ"]["timestamp"], "2026-06-16")

    def test_refresh_data_accepts_source_alias_for_approved_local_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = root / "universe.yml"
            model = root / "model.json"
            output_dir = root / "fresh_data"
            write_universe(config, ("SPY",))
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
                str(model),
            )
            write_records(generate_sample_ohlcv(symbols=("SPY",), start="2026-03-01", end="2026-06-16"), source)

            exit_code = main(
                [
                    "refresh-data",
                    "--source",
                    str(source),
                    "--from",
                    "2026-03-01",
                    "--to",
                    "2026-06-16",
                    "--config",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--as-of-date",
                    "2026-06-16",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            freshness = json.loads((output_dir / "freshness.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(freshness["allowed"])

    @unittest.skipUnless(_parquet_available(), "pandas/pyarrow research extras are not installed")
    def test_refresh_data_source_csv_accepts_canonical_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "ohlcv.parquet"
            config = root / "universe.yml"
            model = root / "model.json"
            output_dir = root / "fresh_data"
            write_universe(config, ("SPY",))
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
                str(model),
            )
            write_records(generate_sample_ohlcv(symbols=("SPY",), start="2026-03-01", end="2026-06-16"), source)

            exit_code = main(
                [
                    "refresh-data",
                    "--source-csv",
                    str(source),
                    "--from",
                    "2026-03-01",
                    "--to",
                    "2026-06-16",
                    "--config",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--as-of-date",
                    "2026-06-16",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            freshness = json.loads((output_dir / "freshness.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(freshness["allowed"])

    def test_refresh_data_cli_exits_one_and_records_stale_symbol_for_old_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = root / "universe.yml"
            model = root / "model.json"
            output_dir = root / "fresh_data"
            write_universe(config, ("SPY",))
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
                str(model),
            )
            write_records(generate_sample_ohlcv(symbols=("SPY",), start="2026-03-01", end="2026-06-01"), source)

            exit_code = main(
                [
                    "refresh-data",
                    "--source-csv",
                    str(source),
                    "--from",
                    "2026-03-01",
                    "--to",
                    "2026-06-01",
                    "--config",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--as-of-date",
                    "2026-06-16",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            freshness = json.loads((output_dir / "freshness.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("stale_symbol", freshness["reasons"])
        self.assertEqual(freshness["symbols"]["SPY"]["age_days"], 15)

    def test_refresh_data_cli_exits_one_and_records_missing_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = root / "universe.yml"
            model = root / "model.json"
            output_dir = root / "fresh_data"
            write_universe(config, ("SPY", "QQQ"))
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
                str(model),
            )
            write_records(generate_sample_ohlcv(symbols=("SPY",), start="2026-03-01", end="2026-06-16"), source)

            exit_code = main(
                [
                    "refresh-data",
                    "--source-csv",
                    str(source),
                    "--from",
                    "2026-03-01",
                    "--to",
                    "2026-06-16",
                    "--config",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--as-of-date",
                    "2026-06-16",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            freshness = json.loads((output_dir / "freshness.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("missing_symbol", freshness["reasons"])
        self.assertEqual(freshness["symbols"]["QQQ"]["status"], "missing")

    def test_paper_dry_run_allows_signal_order_after_refresh_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = root / "universe.yml"
            model = root / "model.json"
            output_dir = root / "fresh_data"
            paper_report = root / "paper.json"
            write_universe(config, ("SPY",))
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
                str(model),
            )
            write_records(generate_sample_ohlcv(symbols=("SPY",), start="2026-03-01", end="2026-06-16"), source)

            refresh_exit = main(
                [
                    "refresh-data",
                    "--source-csv",
                    str(source),
                    "--from",
                    "2026-03-01",
                    "--to",
                    "2026-06-16",
                    "--config",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--as-of-date",
                    "2026-06-16",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            paper_exit = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--universe",
                    str(config),
                    "--signal-model",
                    str(model),
                    "--features",
                    str(output_dir / "features.csv"),
                    "--submit-signal-order",
                    "--as-of-date",
                    "2026-06-16",
                    "--output",
                    str(paper_report),
                ]
            )
            payload = json.loads(paper_report.read_text(encoding="utf-8"))

        self.assertEqual(refresh_exit, 0)
        self.assertEqual(paper_exit, 0)
        self.assertTrue(payload["preflight"]["allowed"])
        self.assertNotIn("stale_features", payload["preflight"]["reasons"])
        self.assertTrue(payload["submitted"])


if __name__ == "__main__":
    unittest.main()
