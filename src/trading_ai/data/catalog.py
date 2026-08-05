"""Governed imports for approved local market datasets."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.config import ConfigError, load_universe_config, load_yaml_file
from trading_ai.data.io import ensure_parquet_support, read_records, write_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.market_calendar import (
    XNYS_CALENDAR_CONTRACT_VERSION,
    XNYS_CALENDAR_SHA256,
    XnysCalendarContractError,
    latest_closed_xnys_session,
    verified_xnys_trading_days,
    xnys_calendar_implementation_sha256,
)
from trading_ai.data.validation import ValidationResult, validate_ohlcv_records

APPROVED_DATA_SCHEMA_VERSION = 1
SUPPORTED_FREQUENCIES = ("1d", "1h")
DEFAULT_DATA_SOURCES_CONFIG = "configs/data_sources.yml"
DATASET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ATTESTED_API_PROVIDER = "alpaca_market_data"
ATTESTED_API_SCHEMA_VERSION = "1.1"
ATTESTED_SOURCE_ARTIFACT_NAME = "source.csv"
ATTESTED_SOURCE_SIDECAR_NAME = "source_attestation.json"
ATTESTED_API_CONTRACT = {
    "provenance_contract_version": "iex-1.0",
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
    "calendar_implementation_sha256": xnys_calendar_implementation_sha256(),
    "clock_injected": False,
}


class ApprovedDataImportError(RuntimeError):
    """Base class for operational approved-data import failures."""


class ApprovedDataValidationError(ValueError):
    """Raised when approved-data validation rejects an import."""

    def __init__(self, errors: list[str] | tuple[str, ...]) -> None:
        super().__init__("approved data validation failed")
        self.errors = tuple(errors)


@dataclass(frozen=True)
class ApprovedDataImportResult:
    dataset_path: Path
    manifest_path: Path
    catalog_entry_path: Path
    manifest: dict[str, object]
    catalog_entry: dict[str, object]


def import_approved_data(
    *,
    source: str | Path,
    dataset_id: str,
    frequency: str,
    config: str | Path = "configs/universe.yml",
    provider: str = "manual_csv",
    license_note: str,
    output_dir: str | Path = "data/raw/approved",
    as_of_date: str | date,
    data_sources_config: str | Path = DEFAULT_DATA_SOURCES_CONFIG,
    source_attestation: str | Path | None = None,
) -> ApprovedDataImportResult:
    """Validate and version an approved local OHLCV CSV as canonical Parquet."""

    _validate_dataset_id(dataset_id)
    if frequency not in SUPPORTED_FREQUENCIES:
        raise ApprovedDataImportError(f"unsupported frequency: {frequency}")
    if not str(license_note).strip():
        raise ApprovedDataImportError("license_note is required")

    provider_config = _load_provider_config(data_sources_config, provider)
    _validate_provider_for_import(
        provider,
        provider_config,
        frequency,
        source_attestation_present=source_attestation is not None,
    )

    source_path = Path(source)
    if not source_path.is_file() or source_path.is_symlink():
        raise ApprovedDataImportError(f"source file not found: {source_path}")
    if source_path.suffix.lower() != ".csv":
        raise ApprovedDataImportError("import-approved-data currently accepts approved manual CSV sources only")

    source_sha256_before = _file_sha256(source_path)
    universe_config_path = Path(config)
    universe_config_sha256_before = _file_sha256(universe_config_path)
    attestation_path = Path(source_attestation) if source_attestation is not None else None
    attestation_sha256_before = (
        _file_sha256(attestation_path) if attestation_path is not None else None
    )

    universe = load_universe_config(universe_config_path)
    raw_records = read_records(source_path)
    normalized_records, normalization_errors = _normalize_ohlcv_records(raw_records, frequency=frequency)
    validation = validate_ohlcv_records(normalized_records, allowed_symbols=universe.symbols)
    validation_errors = [*normalization_errors, *validation.errors]
    if validation_errors:
        raise ApprovedDataValidationError(validation_errors)
    attestation_summary = _validated_source_attestation(
        source_attestation,
        provider=provider,
        frequency=frequency,
        source_path=source_path,
        universe_config_path=universe_config_path,
        universe_symbols=universe.symbols,
        records=normalized_records,
        as_of_date=_parse_date(as_of_date, "as_of_date"),
    )
    if _file_sha256(source_path) != source_sha256_before:
        raise ApprovedDataImportError("source changed during import")
    if _file_sha256(universe_config_path) != universe_config_sha256_before:
        raise ApprovedDataImportError("universe config changed during import")
    if attestation_path is not None and (
        _file_sha256(attestation_path) != attestation_sha256_before
    ):
        raise ApprovedDataImportError("source_attestation changed during import")

    ensure_parquet_support()

    dataset_dir = Path(output_dir) / dataset_id / frequency
    dataset_path = dataset_dir / "ohlcv.parquet"
    manifest_path = dataset_dir / "manifest.json"
    catalog_entry_path = dataset_dir / "catalog_entry.json"

    manifest = _build_approved_manifest(
        normalized_records,
        dataset_id=dataset_id,
        provider=provider,
        provider_config=provider_config,
        frequency=frequency,
        source_path=source_path,
        license_note=license_note,
        as_of_date=_parse_date(as_of_date, "as_of_date").isoformat(),
        validation=validation,
        source_attestation=attestation_summary,
    )
    catalog_entry = _build_catalog_entry(
        dataset_path=dataset_path,
        manifest_path=manifest_path,
        manifest=manifest,
        provider_config=provider_config,
    )

    if provider == ATTESTED_API_PROVIDER:
        _write_immutable_approved_package(
            records=normalized_records,
            dataset_dir=dataset_dir,
            manifest=manifest,
            catalog_entry=catalog_entry,
            source_path=source_path,
            attestation_path=attestation_path,
        )
    else:
        dataset_dir.mkdir(parents=True, exist_ok=True)
        _write_parquet_atomic(normalized_records, dataset_path)
        _write_json_atomic(manifest, manifest_path)
        _write_json_atomic(catalog_entry, catalog_entry_path)

    return ApprovedDataImportResult(
        dataset_path=dataset_path,
        manifest_path=manifest_path,
        catalog_entry_path=catalog_entry_path,
        manifest=manifest,
        catalog_entry=catalog_entry,
    )


def _load_provider_config(config_path: str | Path, provider: str) -> dict[str, object]:
    try:
        payload = load_yaml_file(config_path)
    except ConfigError as exc:
        raise ApprovedDataImportError(str(exc)) from exc
    providers = payload.get("providers", payload)
    if not isinstance(providers, Mapping):
        raise ApprovedDataImportError("data sources config must define provider mappings")
    provider_config = providers.get(provider)
    if not isinstance(provider_config, Mapping):
        raise ApprovedDataImportError(f"unknown data provider: {provider}")
    return dict(provider_config)


def _validate_provider_for_import(
    provider: str,
    provider_config: Mapping[str, object],
    frequency: str,
    *,
    source_attestation_present: bool,
) -> None:
    if provider not in {"manual_csv", ATTESTED_API_PROVIDER}:
        if provider == "api_placeholder":
            raise ApprovedDataImportError("api_provider_not_enabled")
        raise ApprovedDataImportError(f"provider not enabled for approved imports: {provider}")
    if provider_config.get("enabled") is not True:
        raise ApprovedDataImportError(f"provider disabled: {provider}")
    if provider == "manual_csv" and provider_config.get("network_allowed") is not False:
        raise ApprovedDataImportError(f"provider must not allow network access: {provider}")
    if provider == "manual_csv" and source_attestation_present:
        raise ApprovedDataImportError(
            "source_attestation is only valid with alpaca_market_data"
        )
    if provider == ATTESTED_API_PROVIDER:
        if not source_attestation_present:
            raise ApprovedDataImportError(
                "alpaca_market_data import requires source_attestation"
            )
        if provider_config.get("read_only_market_data") is not True:
            raise ApprovedDataImportError(
                "alpaca_market_data provider must be read-only"
            )
    raw_frequencies = provider_config.get("frequencies", [])
    frequencies = raw_frequencies if isinstance(raw_frequencies, (list, tuple, set)) else []
    if frequency not in {str(value) for value in frequencies}:
        raise ApprovedDataImportError(f"provider {provider} does not support frequency {frequency}")


def _normalize_ohlcv_records(
    records: list[dict[str, object]],
    *,
    frequency: str,
) -> tuple[list[dict[str, object]], list[str]]:
    normalized: list[dict[str, object]] = []
    errors: list[str] = []
    for index, row in enumerate(records):
        next_row = dict(row)
        if "symbol" in next_row and next_row["symbol"] not in {None, ""}:
            next_row["symbol"] = str(next_row["symbol"]).strip().upper()
        if "timestamp" not in next_row or next_row["timestamp"] in {None, ""}:
            errors.append(f"row {index} missing required column: timestamp")
        else:
            timestamp = _normalize_timestamp(next_row["timestamp"], frequency=frequency)
            if timestamp is None:
                errors.append(f"row {index} invalid {frequency} timestamp: {next_row['timestamp']}")
            else:
                next_row["timestamp"] = timestamp
        normalized.append(next_row)
    return sorted(normalized, key=lambda row: (str(row.get("timestamp", "")), str(row.get("symbol", "")))), errors


def _normalize_timestamp(value: object, *, frequency: str) -> str | None:
    if frequency == "1d":
        resolved = _coerce_datetime(value, allow_date_only=True)
        if resolved is None:
            return None
        if any((resolved.hour, resolved.minute, resolved.second, resolved.microsecond)):
            return None
        return resolved.date().isoformat()
    if frequency == "1h":
        resolved = _coerce_datetime(value, allow_date_only=False)
        if resolved is None:
            return None
        if resolved.minute != 0 or resolved.second != 0 or resolved.microsecond != 0:
            return None
        return resolved.isoformat(timespec="seconds")
    return None


def _coerce_datetime(value: object, *, allow_date_only: bool) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        converted = to_pydatetime()
        if isinstance(converted, datetime):
            return converted

    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    has_time = "T" in raw or " " in raw
    if not has_time:
        if not allow_date_only:
            return None
        try:
            parsed_date = date.fromisoformat(raw)
        except ValueError:
            return None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _build_approved_manifest(
    records: list[dict[str, object]],
    *,
    dataset_id: str,
    provider: str,
    provider_config: Mapping[str, object],
    frequency: str,
    source_path: Path,
    license_note: str,
    as_of_date: str,
    validation: ValidationResult,
    source_attestation: Mapping[str, object] | None,
) -> dict[str, object]:
    base = build_dataset_manifest(records, source=str(source_path))
    manifest: dict[str, object] = {
        "schema_version": APPROVED_DATA_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "provider": provider,
        "provider_kind": str(provider_config.get("kind", "manual")),
        "frequency": frequency,
        "source_path": (
            ATTESTED_SOURCE_ARTIFACT_NAME
            if provider == ATTESTED_API_PROVIDER
            else str(source_path)
        ),
        "source_sha256": _file_sha256(source_path),
        "dataset_hash": base["dataset_hash"],
        "symbols": base["symbols"],
        "row_count": base["row_count"],
        "start": base["start"],
        "end": base["end"],
        "columns": base["columns"],
        "license_note": license_note,
        "imported_at": _utc_now(),
        "as_of_date": as_of_date,
        "validation": _validation_to_dict(validation),
    }
    if source_attestation is not None:
        manifest["source_attestation"] = dict(source_attestation)
    return manifest


def _build_catalog_entry(
    *,
    dataset_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, object],
    provider_config: Mapping[str, object],
) -> dict[str, object]:
    catalog_entry: dict[str, object] = {
        "schema_version": APPROVED_DATA_SCHEMA_VERSION,
        "dataset_id": manifest["dataset_id"],
        "provider": manifest["provider"],
        "provider_kind": manifest["provider_kind"],
        "frequency": manifest["frequency"],
        "dataset_path": str(dataset_path),
        "manifest_path": str(manifest_path),
        "dataset_hash": manifest["dataset_hash"],
        "symbols": manifest["symbols"],
        "row_count": manifest["row_count"],
        "start": manifest["start"],
        "end": manifest["end"],
        "as_of_date": manifest["as_of_date"],
        "license_note": manifest["license_note"],
        "network_allowed": False,
        "credentials_allowed": False,
        "import_adapter": (
            "attested_local_csv"
            if manifest["provider"] == ATTESTED_API_PROVIDER
            else "manual_csv"
        ),
        "imported_at": manifest["imported_at"],
    }
    if "source_attestation" in manifest:
        catalog_entry["source_attestation"] = manifest["source_attestation"]
    return catalog_entry


def _validated_source_attestation(
    path: str | Path | None,
    *,
    provider: str,
    frequency: str,
    source_path: Path,
    universe_config_path: Path,
    universe_symbols: tuple[str, ...],
    records: list[dict[str, object]],
    as_of_date: date,
) -> dict[str, object] | None:
    if provider == "manual_csv":
        return None
    if provider != ATTESTED_API_PROVIDER or path is None:
        raise ApprovedDataImportError("unsupported source attestation provider")

    attestation_path = Path(path)
    if not attestation_path.is_file() or attestation_path.is_symlink():
        raise ApprovedDataImportError(
            "source_attestation must be a regular non-symlink file"
        )
    try:
        attestation_bytes = attestation_path.read_bytes()
        payload = json.loads(attestation_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovedDataImportError("invalid source_attestation JSON") from exc
    if not isinstance(payload, Mapping):
        raise ApprovedDataImportError("source_attestation must contain a JSON object")

    expected_fields: dict[str, object] = {
        "schema_version": ATTESTED_API_SCHEMA_VERSION,
        "provider": provider,
        "status": "OK",
        "published": True,
        **ATTESTED_API_CONTRACT,
    }
    for field, expected in expected_fields.items():
        actual = payload.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise ApprovedDataImportError(
                f"source_attestation mismatch:{field}:expected={expected}"
            )
    if payload.get("blockers") != []:
        raise ApprovedDataImportError("source_attestation blockers must be empty")

    source_sha256 = _file_sha256(source_path)
    if payload.get("source_sha256") != source_sha256:
        raise ApprovedDataImportError("source_attestation source_sha256 mismatch")
    if payload.get("universe_config_sha256") != _file_sha256(universe_config_path):
        raise ApprovedDataImportError(
            "source_attestation universe_config_sha256 mismatch"
        )
    normalization_hash = payload.get("normalization_code_sha256")
    current_normalization_hash = _file_sha256(
        Path(__file__).with_name("alpaca_market_data.py")
    )
    if normalization_hash != current_normalization_hash:
        raise ApprovedDataImportError(
            "source_attestation normalization_code_sha256 mismatch"
        )
    if payload.get("sdk_package") != "alpaca-py":
        raise ApprovedDataImportError("source_attestation sdk_package mismatch")
    try:
        expected_sdk_version = importlib.metadata.version("alpaca-py")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ApprovedDataImportError(
            "alpaca-py must be installed to verify source_attestation"
        ) from exc
    sdk_version = payload.get("sdk_version")
    if sdk_version != expected_sdk_version:
        raise ApprovedDataImportError("source_attestation sdk_version mismatch")
    if payload.get("attestation_eligible") is not True:
        raise ApprovedDataImportError(
            "source_attestation is not eligible for governed import"
        )
    if payload.get("client_injected") is not False:
        raise ApprovedDataImportError(
            "source_attestation injected clients are not eligible"
        )
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list) or tuple(raw_symbols) != universe_symbols:
        raise ApprovedDataImportError("source_attestation symbols mismatch")
    row_count = payload.get("row_count")
    if type(row_count) is not int or row_count != len(records):
        raise ApprovedDataImportError("source_attestation row_count mismatch")

    start = _parse_date(str(payload.get("start", "")), "source_attestation.start")
    end = _parse_date(str(payload.get("end", "")), "source_attestation.end")
    generated_at_value = payload.get("generated_at")
    generated_at = _coerce_datetime(generated_at_value, allow_date_only=False)
    if (
        not isinstance(generated_at_value, str)
        or generated_at is None
        or generated_at.tzinfo is None
        or generated_at.utcoffset() is None
    ):
        raise ApprovedDataImportError(
            "source_attestation generated_at must be timezone-aware"
        )
    closed_session_watermark = _parse_date(
        str(payload.get("closed_session_watermark", "")),
        "source_attestation.closed_session_watermark",
    )
    try:
        governed_sessions = verified_xnys_trading_days(
            start,
            end,
            calendar_contract_version=str(payload["calendar_contract_version"]),
            calendar_sha256=str(payload["calendar_sha256"]),
        )
        expected_watermark = latest_closed_xnys_session(generated_at)
    except XnysCalendarContractError as exc:
        raise ApprovedDataImportError(str(exc)) from exc
    if not governed_sessions:
        raise ApprovedDataImportError(
            "source_attestation range contains no governed XNYS sessions"
        )
    latest_attested_session = governed_sessions[-1]
    if closed_session_watermark < latest_attested_session:
        raise ApprovedDataImportError(
            "source_attestation closed_session_watermark precedes latest session"
        )
    if closed_session_watermark != expected_watermark:
        raise ApprovedDataImportError(
            "source_attestation closed_session_watermark does not match generated_at"
        )
    observed_dates = tuple(str(record["timestamp"]) for record in records)
    if not observed_dates:
        raise ApprovedDataImportError("source_attestation records must not be empty")
    observed_start = min(observed_dates)
    observed_end = max(observed_dates)
    if payload.get("observed_start") != observed_start:
        raise ApprovedDataImportError("source_attestation observed_start mismatch")
    if payload.get("observed_end") != observed_end:
        raise ApprovedDataImportError("source_attestation observed_end mismatch")
    if as_of_date < end:
        raise ApprovedDataImportError("as_of_date must not precede attested end")

    expected_sessions = {session.isoformat() for session in governed_sessions}
    sessions_by_symbol = {
        symbol: {
            str(record["timestamp"])
            for record in records
            if str(record["symbol"]) == symbol
        }
        for symbol in universe_symbols
    }
    coverage_errors = []
    for symbol, actual_sessions in sessions_by_symbol.items():
        missing = expected_sessions - actual_sessions
        unexpected = actual_sessions - expected_sessions
        if missing or unexpected:
            coverage_errors.append(
                "source_attestation_session_set_mismatch:"
                f"{symbol}:missing={len(missing)}:unexpected={len(unexpected)}"
            )
    if coverage_errors:
        raise ApprovedDataValidationError(coverage_errors)

    required_hash_fields = (
        "source_sha256",
        "universe_config_sha256",
        "normalization_code_sha256",
        "calendar_sha256",
        "calendar_implementation_sha256",
    )
    if any(
        not isinstance(payload.get(field), str)
        or SHA256_PATTERN.fullmatch(str(payload[field])) is None
        for field in required_hash_fields
    ):
        raise ApprovedDataImportError(
            "source_attestation contains an invalid sha256 field"
        )

    return {
        "path": ATTESTED_SOURCE_SIDECAR_NAME,
        "source_path": ATTESTED_SOURCE_ARTIFACT_NAME,
        "sha256": hashlib.sha256(attestation_bytes).hexdigest(),
        "schema_version": payload["schema_version"],
        "generated_at": payload["generated_at"],
        "upstream_provider": payload["provider"],
        "feed": payload["feed"],
        "frequency": payload["frequency"],
        "adjustment_policy": payload["adjustment_policy"],
        "corporate_action_policy": payload["corporate_action_policy"],
        "request_timezone": payload["request_timezone"],
        "bar_timestamp_timezone": payload["bar_timestamp_timezone"],
        "timestamp_semantics": payload["timestamp_semantics"],
        "exchange_calendar": payload["exchange_calendar"],
        "calendar_contract_version": payload["calendar_contract_version"],
        "calendar_sha256": payload["calendar_sha256"],
        "calendar_implementation_sha256": payload[
            "calendar_implementation_sha256"
        ],
        "closed_session_watermark": payload["closed_session_watermark"],
        "sdk_package": payload.get("sdk_package"),
        "sdk_version": sdk_version,
        "source_sha256": payload["source_sha256"],
        "universe_config_sha256": payload["universe_config_sha256"],
        "normalization_code_sha256": normalization_hash,
        "symbols": list(payload["symbols"]),
        "row_count": payload["row_count"],
        "start": payload["start"],
        "end": payload["end"],
        "observed_start": payload["observed_start"],
        "observed_end": payload["observed_end"],
        "status": payload["status"],
        "published": payload["published"],
        "blockers": list(payload["blockers"]),
        "attestation_eligible": payload["attestation_eligible"],
        "client_injected": payload["client_injected"],
        "clock_injected": payload["clock_injected"],
        "provenance_contract_version": payload["provenance_contract_version"],
    }


def _write_immutable_approved_package(
    *,
    records: list[dict[str, object]],
    dataset_dir: Path,
    manifest: Mapping[str, object],
    catalog_entry: Mapping[str, object],
    source_path: Path,
    attestation_path: Path | None,
) -> None:
    if attestation_path is None:
        raise ApprovedDataImportError(
            "attested approved dataset requires a source attestation artifact"
        )
    if dataset_dir.exists():
        raise ApprovedDataImportError(
            "attested approved dataset target already exists; use a new dataset_id"
        )
    dataset_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            dir=dataset_dir.parent,
            prefix=f".{dataset_dir.parent.name}-staging-",
        )
    )
    try:
        copied_source = staging_dir / ATTESTED_SOURCE_ARTIFACT_NAME
        copied_attestation = staging_dir / ATTESTED_SOURCE_SIDECAR_NAME
        shutil.copyfile(source_path, copied_source)
        shutil.copyfile(attestation_path, copied_attestation)
        source_attestation = manifest.get("source_attestation")
        if not isinstance(source_attestation, Mapping):
            raise ApprovedDataImportError(
                "attested approved dataset manifest lost source_attestation"
            )
        if _file_sha256(copied_source) != manifest.get("source_sha256"):
            raise ApprovedDataImportError(
                "copied approved source hash does not match manifest"
            )
        if _file_sha256(copied_attestation) != source_attestation.get("sha256"):
            raise ApprovedDataImportError(
                "copied source_attestation hash does not match manifest"
            )
        _write_parquet_atomic(records, staging_dir / "ohlcv.parquet")
        _write_json_atomic(manifest, staging_dir / "manifest.json")
        _write_json_atomic(catalog_entry, staging_dir / "catalog_entry.json")
        os.replace(staging_dir, dataset_dir)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)


def _validation_to_dict(validation: ValidationResult) -> dict[str, object]:
    return {
        "valid": validation.valid,
        "errors": list(validation.errors),
        "row_count": validation.row_count,
        "symbols": list(validation.symbols),
    }


def _write_parquet_atomic(records: list[dict[str, object]], path: Path) -> None:
    temp_path = path.with_name(f".{path.stem}.tmp.parquet")
    try:
        write_records(records, temp_path)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_json_atomic(payload: Mapping[str, object], path: Path) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ApprovedDataImportError(f"invalid {field_name}: {value}") from exc


def _validate_dataset_id(dataset_id: str) -> None:
    if not DATASET_ID_PATTERN.fullmatch(dataset_id):
        raise ApprovedDataImportError("dataset_id must contain only letters, numbers, dots, underscores, or dashes")
    if ".." in dataset_id:
        raise ApprovedDataImportError("dataset_id must not contain path traversal")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
