"""Governed imports for approved local market datasets."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.config import ConfigError, load_universe_config, load_yaml_file
from trading_ai.data.io import ensure_parquet_support, read_records, write_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.validation import ValidationResult, validate_ohlcv_records

APPROVED_DATA_SCHEMA_VERSION = 1
SUPPORTED_FREQUENCIES = ("1d", "1h")
DEFAULT_DATA_SOURCES_CONFIG = "configs/data_sources.yml"
DATASET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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
) -> ApprovedDataImportResult:
    """Validate and version an approved local OHLCV CSV as canonical Parquet."""

    _validate_dataset_id(dataset_id)
    if frequency not in SUPPORTED_FREQUENCIES:
        raise ApprovedDataImportError(f"unsupported frequency: {frequency}")
    if not str(license_note).strip():
        raise ApprovedDataImportError("license_note is required")

    provider_config = _load_provider_config(data_sources_config, provider)
    _validate_provider_for_import(provider, provider_config, frequency)

    source_path = Path(source)
    if not source_path.exists():
        raise ApprovedDataImportError(f"source file not found: {source_path}")
    if source_path.suffix.lower() != ".csv":
        raise ApprovedDataImportError("import-approved-data currently accepts approved manual CSV sources only")

    universe = load_universe_config(config)
    raw_records = read_records(source_path)
    normalized_records, normalization_errors = _normalize_ohlcv_records(raw_records, frequency=frequency)
    validation = validate_ohlcv_records(normalized_records, allowed_symbols=universe.symbols)
    validation_errors = [*normalization_errors, *validation.errors]
    if validation_errors:
        raise ApprovedDataValidationError(validation_errors)

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
    )
    catalog_entry = _build_catalog_entry(
        dataset_path=dataset_path,
        manifest_path=manifest_path,
        manifest=manifest,
        provider_config=provider_config,
    )

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


def _validate_provider_for_import(provider: str, provider_config: Mapping[str, object], frequency: str) -> None:
    if provider != "manual_csv":
        if provider == "api_placeholder":
            raise ApprovedDataImportError("api_provider_not_enabled")
        raise ApprovedDataImportError(f"provider not enabled for approved imports: {provider}")
    if provider_config.get("enabled") is not True:
        raise ApprovedDataImportError(f"provider disabled: {provider}")
    if provider_config.get("network_allowed") is not False:
        raise ApprovedDataImportError(f"provider must not allow network access: {provider}")
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
) -> dict[str, object]:
    base = build_dataset_manifest(records, source=str(source_path))
    return {
        "schema_version": APPROVED_DATA_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "provider": provider,
        "provider_kind": str(provider_config.get("kind", "manual")),
        "frequency": frequency,
        "source_path": str(source_path),
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


def _build_catalog_entry(
    *,
    dataset_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, object],
    provider_config: Mapping[str, object],
) -> dict[str, object]:
    return {
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
        "network_allowed": bool(provider_config.get("network_allowed", False)),
        "imported_at": manifest["imported_at"],
    }


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
