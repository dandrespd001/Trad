import hashlib
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.data import alpaca_market_data
from trading_ai.data.catalog import ApprovedDataValidationError, import_approved_data
from trading_ai.data.io import (
    PARQUET_DEPENDENCY_MESSAGE,
    ParquetDependencyError,
    ensure_parquet_support,
    read_records,
    write_records,
)
from trading_ai.data.manifest import dataset_hash
from trading_ai.data.market_calendar import (
    XNYS_CALENDAR_CONTRACT_VERSION,
    XNYS_CALENDAR_SHA256,
    xnys_calendar_implementation_sha256,
)
from trading_ai.evaluation.approved_data import _validate_approved_metadata


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


def daily_rows(*, symbol: str = "spy") -> list[dict[str, Any]]:
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


def hourly_rows() -> list[dict[str, Any]]:
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


def write_fake_parquet(records: list[dict[str, Any]], path: Path) -> None:
    path.write_bytes(b"PAR1 fake parquet placeholder")


def _parquet_available() -> bool:
    try:
        ensure_parquet_support()
    except ParquetDependencyError:
        return False
    return True


def write_iex_attestation(
    path: Path,
    *,
    source: Path,
    universe_config: Path,
    source_sha256: str | None = None,
) -> Path:
    normalization_path = Path(str(alpaca_market_data.__file__))
    payload = {
        "schema_version": "1.1",
        "provenance_contract_version": "iex-1.0",
        "generated_at": "2026-06-16T20:00:00Z",
        "start": "2026-06-15",
        "end": "2026-06-16",
        "symbols": ["SPY"],
        "row_count": 2,
        "observed_start": "2026-06-15",
        "observed_end": "2026-06-16",
        "provider": "alpaca_market_data",
        "feed": "iex",
        "frequency": "1d",
        "adjustment_policy": "all",
        "corporate_action_policy": "alpaca_adjustment_all",
        "request_timezone": "UTC",
        "bar_timestamp_timezone": "UTC",
        "timestamp_semantics": "XNYS_exchange_session_date",
        "exchange_calendar": "XNYS",
        "calendar_contract_version": XNYS_CALENDAR_CONTRACT_VERSION,
        "calendar_sha256": XNYS_CALENDAR_SHA256,
        "calendar_implementation_sha256": (
            xnys_calendar_implementation_sha256()
        ),
        "closed_session_watermark": "2026-06-16",
        "sdk_package": "alpaca-py",
        "sdk_version": "0.43.4",
        "attestation_eligible": True,
        "client_injected": False,
        "clock_injected": False,
        "universe_config_sha256": hashlib.sha256(
            universe_config.read_bytes()
        ).hexdigest(),
        "normalization_code_sha256": hashlib.sha256(
            normalization_path.read_bytes()
        ).hexdigest(),
        "source_sha256": (
            source_sha256
            if source_sha256 is not None
            else hashlib.sha256(source.read_bytes()).hexdigest()
        ),
        "status": "OK",
        "blockers": [],
        "published": True,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ApprovedDataCatalogTests(unittest.TestCase):
    def test_daily_csv_import_writes_canonical_artifacts_and_manifest_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "approved"
            write_records(daily_rows(), source)

            with (
                mock.patch("trading_ai.data.catalog.ensure_parquet_support"),
                mock.patch(
                    "trading_ai.data.catalog._write_parquet_atomic",
                    side_effect=write_fake_parquet,
                ),
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

        expected_records = [{**row, "timestamp": row["timestamp"][:10], "symbol": "SPY"} for row in daily_rows()]
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

    def test_attested_alpaca_import_preserves_upstream_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "approved"
            write_records(daily_rows(), source)
            attestation = write_iex_attestation(
                root / "source.csv.fetch.json",
                source=source,
                universe_config=config,
            )

            with (
                mock.patch("trading_ai.data.catalog.ensure_parquet_support"),
                mock.patch(
                    "trading_ai.data.catalog._write_parquet_atomic",
                    side_effect=write_fake_parquet,
                ),
            ):
                result = import_approved_data(
                    source=source,
                    dataset_id="core_etfs_iex",
                    frequency="1d",
                    config=config,
                    provider="alpaca_market_data",
                    source_attestation=attestation,
                    license_note="read-only IEX research source",
                    output_dir=output_dir,
                    as_of_date="2026-06-16",
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "target already exists",
                ):
                    import_approved_data(
                        source=source,
                        dataset_id="core_etfs_iex",
                        frequency="1d",
                        config=config,
                        provider="alpaca_market_data",
                        source_attestation=attestation,
                        license_note="read-only IEX research source",
                        output_dir=output_dir,
                        as_of_date="2026-06-16",
                    )
            packaged_source = result.dataset_path.parent / "source.csv"
            packaged_attestation = (
                result.dataset_path.parent / "source_attestation.json"
            )
            packaged_source_exists = packaged_source.is_file()
            packaged_attestation_exists = packaged_attestation.is_file()
            packaged_source_sha256 = hashlib.sha256(
                packaged_source.read_bytes()
            ).hexdigest()
            packaged_attestation_sha256 = hashlib.sha256(
                packaged_attestation.read_bytes()
            ).hexdigest()

        source_contract = result.manifest["source_attestation"]
        self.assertEqual(result.manifest["provider"], "alpaca_market_data")
        self.assertEqual(source_contract["upstream_provider"], "alpaca_market_data")
        self.assertEqual(source_contract["feed"], "iex")
        self.assertEqual(source_contract["adjustment_policy"], "all")
        self.assertEqual(source_contract["provenance_contract_version"], "iex-1.0")
        self.assertEqual(
            source_contract["calendar_contract_version"],
            XNYS_CALENDAR_CONTRACT_VERSION,
        )
        self.assertEqual(
            source_contract["calendar_sha256"],
            XNYS_CALENDAR_SHA256,
        )
        self.assertEqual(
            source_contract["closed_session_watermark"],
            "2026-06-16",
        )
        self.assertEqual(
            source_contract["generated_at"],
            "2026-06-16T20:00:00Z",
        )
        self.assertFalse(source_contract["clock_injected"])
        self.assertEqual(source_contract["source_sha256"], result.manifest["source_sha256"])
        self.assertEqual(source_contract["status"], "OK")
        self.assertTrue(source_contract["published"])
        self.assertEqual(source_contract["blockers"], [])
        self.assertTrue(source_contract["attestation_eligible"])
        self.assertFalse(source_contract["client_injected"])
        self.assertEqual(source_contract["symbols"], result.manifest["symbols"])
        self.assertEqual(source_contract["row_count"], result.manifest["row_count"])
        self.assertEqual(source_contract["observed_start"], result.manifest["start"])
        self.assertEqual(source_contract["observed_end"], result.manifest["end"])
        self.assertEqual(
            result.catalog_entry["source_attestation"],
            source_contract,
        )
        _validate_approved_metadata(result.manifest, result.catalog_entry)
        self.assertFalse(result.catalog_entry["network_allowed"])
        self.assertFalse(result.catalog_entry["credentials_allowed"])
        self.assertEqual(
            result.catalog_entry["import_adapter"],
            "attested_local_csv",
        )
        self.assertTrue(packaged_source_exists)
        self.assertTrue(packaged_attestation_exists)
        self.assertEqual(
            packaged_source_sha256,
            result.manifest["source_sha256"],
        )
        self.assertEqual(
            packaged_attestation_sha256,
            source_contract["sha256"],
        )
        self.assertEqual(source_contract["path"], "source_attestation.json")
        self.assertEqual(source_contract["source_path"], "source.csv")

    def test_alpaca_import_fails_closed_without_valid_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            write_records(daily_rows(), source)

            with self.assertRaisesRegex(
                RuntimeError,
                "requires source_attestation",
            ):
                import_approved_data(
                    source=source,
                    dataset_id="core_etfs_iex",
                    frequency="1d",
                    config=config,
                    provider="alpaca_market_data",
                    license_note="read-only IEX research source",
                    output_dir=root / "approved",
                    as_of_date="2026-06-16",
                )

            bad_attestation = write_iex_attestation(
                root / "source.csv.fetch.json",
                source=source,
                universe_config=config,
                source_sha256="0" * 64,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "source_sha256 mismatch",
            ):
                import_approved_data(
                    source=source,
                    dataset_id="core_etfs_iex",
                    frequency="1d",
                    config=config,
                    provider="alpaca_market_data",
                    source_attestation=bad_attestation,
                    license_note="read-only IEX research source",
                    output_dir=root / "approved",
                    as_of_date="2026-06-16",
                )

            injected_attestation = write_iex_attestation(
                root / "source.csv.fetch.json",
                source=source,
                universe_config=config,
            )
            injected_payload = json.loads(
                injected_attestation.read_text(encoding="utf-8")
            )
            injected_payload["attestation_eligible"] = False
            injected_payload["client_injected"] = True
            injected_attestation.write_text(
                json.dumps(injected_payload),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "not eligible",
            ):
                import_approved_data(
                    source=source,
                    dataset_id="core_etfs_iex",
                    frequency="1d",
                    config=config,
                    provider="alpaca_market_data",
                    source_attestation=injected_attestation,
                    license_note="read-only IEX research source",
                    output_dir=root / "approved",
                    as_of_date="2026-06-16",
                )

    def test_alpaca_import_rejects_calendar_or_watermark_drift(self) -> None:
        cases = (
            (
                "calendar_contract_version",
                "xnys-unreviewed-v2",
                "mismatch:calendar_contract_version",
            ),
            (
                "calendar_sha256",
                "0" * 64,
                "mismatch:calendar_sha256",
            ),
            (
                "closed_session_watermark",
                "2026-06-15",
                "closed_session_watermark precedes latest session",
            ),
            (
                "generated_at",
                "2026-06-16T19:59:00Z",
                "closed_session_watermark does not match generated_at",
            ),
            (
                "generated_at",
                "2026-06-16T20:00:00",
                "generated_at must be timezone-aware",
            ),
            (
                "clock_injected",
                True,
                "mismatch:clock_injected",
            ),
        )

        for field, value, expected_error in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "source.csv"
                config = write_universe(root / "universe.yml", ("SPY",))
                write_records(daily_rows(), source)
                attestation = write_iex_attestation(
                    root / "source.csv.fetch.json",
                    source=source,
                    universe_config=config,
                )
                payload = json.loads(attestation.read_text(encoding="utf-8"))
                payload[field] = value
                attestation.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(
                    RuntimeError,
                    expected_error,
                ):
                    import_approved_data(
                        source=source,
                        dataset_id="core_etfs_iex",
                        frequency="1d",
                        config=config,
                        provider="alpaca_market_data",
                        source_attestation=attestation,
                        license_note="read-only IEX research source",
                        output_dir=root / "approved",
                        as_of_date="2026-06-16",
                    )

    def test_attested_alpaca_import_requires_complete_session_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            write_records(daily_rows()[:1], source)
            attestation = write_iex_attestation(
                root / "source.csv.fetch.json",
                source=source,
                universe_config=config,
            )
            payload = json.loads(attestation.read_text(encoding="utf-8"))
            payload["row_count"] = 1
            payload["observed_end"] = "2026-06-15"
            attestation.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(ApprovedDataValidationError) as ctx:
                import_approved_data(
                    source=source,
                    dataset_id="core_etfs_iex",
                    frequency="1d",
                    config=config,
                    provider="alpaca_market_data",
                    source_attestation=attestation,
                    license_note="read-only IEX research source",
                    output_dir=root / "approved",
                    as_of_date="2026-06-16",
                )

        self.assertEqual(
            ctx.exception.errors,
            (
                "source_attestation_session_set_mismatch:"
                "SPY:missing=1:unexpected=0",
            ),
        )

    def test_hourly_csv_import_accepts_hour_timestamps_and_normalizes_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            config = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "approved"
            write_records(hourly_rows(), source)

            with (
                mock.patch("trading_ai.data.catalog.ensure_parquet_support"),
                mock.patch(
                    "trading_ai.data.catalog._write_parquet_atomic",
                    side_effect=write_fake_parquet,
                ),
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
                "/tmp/source.csv",  # noqa: S108
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
        self.assertIsNone(args.source_attestation)

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
