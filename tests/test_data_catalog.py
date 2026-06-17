import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.data.catalog import ApprovedDataValidationError, import_approved_data
from trading_ai.data.io import (
    PARQUET_DEPENDENCY_MESSAGE,
    ParquetDependencyError,
    ensure_parquet_support,
    read_records,
    write_records,
)
from trading_ai.data.manifest import dataset_hash


def write_universe(path: Path, symbols: tuple[str, ...]) -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            universe:
              symbols: [{", ".join(symbols)}]
            """
        ),
        encoding="utf-8",
    )
    return path


def daily_rows(*, symbol: str = "spy") -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-06-15T00:00:00",
            "symbol": symbol,
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
        },
        {
            "timestamp": "2026-06-16",
            "symbol": symbol,
            "open": 101,
            "high": 103,
            "low": 100,
            "close": 102,
            "volume": 1100,
        },
    ]


def hourly_rows() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-06-16T14:00:00",
            "symbol": "spy",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
        },
        {
            "timestamp": "2026-06-16T15:00:00",
            "symbol": "SPY",
            "open": 101,
            "high": 103,
            "low": 100,
            "close": 102,
            "volume": 1100,
        },
    ]


def write_fake_parquet(records: list[dict[str, object]], path: Path) -> None:
    path.write_bytes(b"PAR1 fake parquet placeholder")


def _parquet_available() -> bool:
    try:
        ensure_parquet_support()
    except ParquetDependencyError:
        return False
    return True


class ApprovedDataCatalogTests(unittest.TestCase):
    def test_daily_csv_import_writes_canonical_artifacts_and_manifest_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "approved"
            write_records(daily_rows(), source)

            with mock.patch("trading_ai.data.catalog.ensure_parquet_support"), mock.patch(
                "trading_ai.data.catalog._write_parquet_atomic",
                side_effect=write_fake_parquet,
            ):
                result = import_approved_data(
                    source=source,
                    dataset_id="core_etfs",
                    frequency="1d",
                    config=config,
                    provider="manual_csv",
                    license_note="manual download approved for research use",
                    output_dir=output_dir,
                    as_of_date="2026-06-16",
                )

            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            catalog_entry = json.loads(result.catalog_entry_path.read_text(encoding="utf-8"))
            dataset_exists = result.dataset_path.exists()

        expected_records = [
            {**row, "timestamp": row["timestamp"][:10], "symbol": "SPY"}
            for row in daily_rows()
        ]
        self.assertEqual(result.dataset_path, output_dir / "core_etfs" / "1d" / "ohlcv.parquet")
        self.assertTrue(dataset_exists)
        self.assertEqual(manifest["dataset_id"], "core_etfs")
        self.assertEqual(manifest["provider"], "manual_csv")
        self.assertEqual(manifest["frequency"], "1d")
        self.assertEqual(manifest["dataset_hash"], dataset_hash(expected_records))
        self.assertEqual(len(manifest["source_sha256"]), 64)
        self.assertEqual(manifest["symbols"], ["SPY"])
        self.assertTrue(manifest["validation"]["valid"])
        self.assertEqual(catalog_entry["dataset_path"], str(result.dataset_path))
        self.assertFalse(catalog_entry["network_allowed"])

    def test_hourly_csv_import_accepts_hour_timestamps_and_normalizes_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "approved"
            write_records(hourly_rows(), source)

            with mock.patch("trading_ai.data.catalog.ensure_parquet_support"), mock.patch(
                "trading_ai.data.catalog._write_parquet_atomic",
                side_effect=write_fake_parquet,
            ):
                result = import_approved_data(
                    source=source,
                    dataset_id="core_etfs",
                    frequency="1h",
                    config=config,
                    provider="manual_csv",
                    license_note="manual download approved for research use",
                    output_dir=output_dir,
                    as_of_date="2026-06-16",
                )

        self.assertEqual(result.manifest["start"], "2026-06-16T14:00:00")
        self.assertEqual(result.manifest["end"], "2026-06-16T15:00:00")
        self.assertEqual(result.manifest["symbols"], ["SPY"])
        self.assertEqual(result.manifest["frequency"], "1h")

    def test_invalid_rows_block_import_without_parquet_partial(self) -> None:
        cases = {
            "unexpected_symbol": daily_rows(symbol="TSLA"),
            "duplicate": [*daily_rows(), daily_rows()[0]],
            "wrong_daily_frequency": [{**daily_rows()[0], "timestamp": "2026-06-16T13:00:00"}],
            "bad_ohlc": [{**daily_rows()[0], "high": 98}],
        }
        for case_name, rows in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "source.csv"
                config = write_universe(root / "universe.yml", ("SPY",))
                output_dir = root / "approved"
                write_records(rows, source)

                with self.assertRaises(ApprovedDataValidationError):
                    import_approved_data(
                        source=source,
                        dataset_id="core_etfs",
                        frequency="1d",
                        config=config,
                        provider="manual_csv",
                        license_note="manual download approved for research use",
                        output_dir=output_dir,
                        as_of_date="2026-06-16",
                    )

                self.assertFalse((output_dir / "core_etfs" / "1d" / "ohlcv.parquet").exists())
                self.assertFalse((output_dir / "core_etfs" / "1d" / "manifest.json").exists())

    def test_import_cli_reports_missing_parquet_dependency_as_operational_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "approved"
            write_records(daily_rows(), source)

            with mock.patch(
                "trading_ai.data.catalog.ensure_parquet_support",
                side_effect=ParquetDependencyError(PARQUET_DEPENDENCY_MESSAGE),
            ):
                exit_code = main(
                    [
                        "import-approved-data",
                        "--source",
                        str(source),
                        "--dataset-id",
                        "core_etfs",
                        "--frequency",
                        "1d",
                        "--config",
                        str(config),
                        "--provider",
                        "manual_csv",
                        "--license-note",
                        "manual download approved for research use",
                        "--output-dir",
                        str(output_dir),
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertFalse((output_dir / "core_etfs" / "1d" / "ohlcv.parquet").exists())

    def test_api_placeholder_is_rejected_without_reading_source_or_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_universe(root / "universe.yml", ("SPY",))

            exit_code = main(
                [
                    "import-approved-data",
                    "--source",
                    str(root / "does-not-need-to-exist.csv"),
                    "--dataset-id",
                    "core_etfs",
                    "--frequency",
                    "1d",
                    "--config",
                    str(config),
                    "--provider",
                    "api_placeholder",
                    "--license-note",
                    "future provider disabled",
                    "--output-dir",
                    str(root / "approved"),
                    "--as-of-date",
                    "2026-06-16",
                ]
            )

        self.assertEqual(exit_code, 2)

    def test_import_approved_data_parser_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "import-approved-data",
                "--source",
                "/tmp/source.csv",
                "--dataset-id",
                "core_etfs",
                "--frequency",
                "1d",
                "--provider",
                "manual_csv",
                "--license-note",
                "manual download approved",
                "--as-of-date",
                "2026-06-16",
            ]
        )

        self.assertEqual(args.config, "configs/universe.yml")
        self.assertEqual(args.output_dir, "data/raw/approved")
        self.assertEqual(args.provider, "manual_csv")

    @unittest.skipUnless(_parquet_available(), "pandas/pyarrow research extras are not installed")
    def test_valid_daily_import_writes_readable_parquet_when_research_extras_are_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "approved"
            write_records(daily_rows(), source)

            result = import_approved_data(
                source=source,
                dataset_id="core_etfs",
                frequency="1d",
                config=config,
                provider="manual_csv",
                license_note="manual download approved for research use",
                output_dir=output_dir,
                as_of_date="2026-06-16",
            )
            records = read_records(result.dataset_path)

        self.assertEqual(
            [(row["timestamp"], row["symbol"]) for row in records],
            [("2026-06-15", "SPY"), ("2026-06-16", "SPY")],
        )

if __name__ == "__main__":
    unittest.main()
