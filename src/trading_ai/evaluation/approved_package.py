"""Read-only validation for governed approved-data research packages."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trading_ai.data import market_calendar
from trading_ai.data.io import read_records
from trading_ai.data.manifest import build_dataset_manifest

REQUIRED_FILES = ("ohlcv.parquet", "manifest.json", "catalog_entry.json")
IEX_PROVIDER = "alpaca_market_data"
SUPPORTED_PROVIDERS = frozenset({"manual_csv", IEX_PROVIDER})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ApprovedPackageError(RuntimeError):
    """Raised before research output exists when an approved package is invalid."""


@dataclass(frozen=True)
class ValidatedApprovedPackage:
    paths: Mapping[str, Path]
    manifest: Mapping[str, object]
    catalog_entry: Mapping[str, object]
    metadata: Mapping[str, object]
    records: list[dict[str, Any]]


def load_validated_approved_package(
    approved_dir: str | Path,
    *,
    config: str | Path,
    requested_as_of_date: date,
    required_provider: str | None = None,
    record_reader: Callable[[str | Path], list[dict[str, Any]]] = read_records,
) -> ValidatedApprovedPackage:
    """Load and validate an immutable package without creating any output."""

    root = Path(approved_dir)
    paths = _approved_paths(root)
    manifest = _read_json(paths["manifest"])
    catalog = _read_json(paths["catalog_entry"])
    metadata = _validated_metadata(
        manifest,
        catalog,
        approved_dir=root,
        requested_as_of_date=requested_as_of_date,
        required_provider=required_provider,
    )
    iex_source_dataset_hash: str | None = None
    if metadata["provider"] == IEX_PROVIDER:
        iex_source_dataset_hash = _validate_iex_provenance(
            root,
            manifest=manifest,
            catalog=catalog,
            config=Path(config),
            requested_as_of_date=requested_as_of_date,
        )

    records = record_reader(paths["dataset"])
    actual = build_dataset_manifest(records, source=str(paths["dataset"]))
    for field in ("dataset_hash", "row_count", "start", "end", "symbols", "columns"):
        expected = manifest.get(field)
        observed = actual.get(field)
        if expected != observed:
            raise ApprovedPackageError(
                f"approved dataset {field} mismatch: manifest={expected} actual={observed}"
            )
    if (
        iex_source_dataset_hash is not None
        and actual["dataset_hash"] != iex_source_dataset_hash
    ):
        raise ApprovedPackageError(
            "IEX approved dataset values do not match packaged source.csv"
        )
    return ValidatedApprovedPackage(
        paths=paths,
        manifest=manifest,
        catalog_entry=catalog,
        metadata=metadata,
        records=records,
    )


def _approved_paths(root: Path) -> dict[str, Path]:
    missing = [
        name
        for name in REQUIRED_FILES
        if not (root / name).is_file() or (root / name).is_symlink()
    ]
    if missing:
        raise ApprovedPackageError(
            "approved dataset package is missing required file(s): " + ", ".join(missing)
        )
    return {
        "dataset": root / "ohlcv.parquet",
        "manifest": root / "manifest.json",
        "catalog_entry": root / "catalog_entry.json",
    }


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovedPackageError(f"invalid approved dataset JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ApprovedPackageError(f"approved dataset JSON must be an object: {path}")
    return payload


def _validated_metadata(
    manifest: Mapping[str, object],
    catalog: Mapping[str, object],
    *,
    approved_dir: Path,
    requested_as_of_date: date,
    required_provider: str | None,
) -> dict[str, object]:
    required = (
        "dataset_id",
        "frequency",
        "dataset_hash",
        "source_sha256",
        "row_count",
        "start",
        "end",
        "symbols",
        "columns",
        "as_of_date",
        "provider",
    )
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ApprovedPackageError("manifest missing required field(s): " + ", ".join(missing))
    for field in (
        "dataset_id",
        "frequency",
        "dataset_hash",
        "row_count",
        "start",
        "end",
        "symbols",
        "as_of_date",
        "provider",
    ):
        if catalog.get(field) != manifest.get(field):
            raise ApprovedPackageError(f"catalog entry does not match manifest field: {field}")
    provider = str(manifest["provider"])
    if provider not in SUPPORTED_PROVIDERS:
        raise ApprovedPackageError(f"unsupported approved dataset provider: {provider}")
    if required_provider is not None and provider != required_provider:
        raise ApprovedPackageError(
            f"approved dataset provider mismatch: required={required_provider} actual={provider}"
        )
    for field in ("dataset_hash", "source_sha256"):
        if not isinstance(manifest.get(field), str) or not SHA256_PATTERN.fullmatch(
            str(manifest[field])
        ):
            raise ApprovedPackageError(f"manifest {field} must be a SHA-256 hex digest")
    approved_as_of = _parse_date(manifest["as_of_date"], "approved dataset as_of_date")
    if approved_as_of != requested_as_of_date:
        raise ApprovedPackageError(
            "approved dataset as_of_date mismatch: "
            f"requested={requested_as_of_date.isoformat()} approved={approved_as_of.isoformat()}"
        )
    dataset_end = _parse_date(manifest["end"], "approved dataset end")
    if dataset_end > requested_as_of_date:
        raise ApprovedPackageError(
            "approved dataset end exceeds requested as_of_date: "
            f"end={dataset_end.isoformat()} as_of={requested_as_of_date.isoformat()}"
        )
    source_attestation = manifest.get("source_attestation")
    metadata: dict[str, object] = {
        "schema_version": 1,
        "dataset_id": str(manifest["dataset_id"]),
        "frequency": str(manifest["frequency"]),
        "dataset_hash": str(manifest["dataset_hash"]),
        "source_sha256": str(manifest["source_sha256"]),
        "as_of_date": approved_as_of.isoformat(),
        "start": manifest["start"],
        "end": manifest["end"],
        "symbols": list(manifest["symbols"]) if isinstance(manifest["symbols"], list) else [],
        "row_count": manifest["row_count"],
        "columns": list(manifest["columns"]) if isinstance(manifest["columns"], list) else [],
        "provider": provider,
        "approved_dir": str(approved_dir),
        "dataset_path": str(approved_dir / "ohlcv.parquet"),
    }
    if isinstance(source_attestation, Mapping):
        metadata["source_attestation"] = dict(source_attestation)
    return metadata


def _validate_iex_provenance(
    root: Path,
    *,
    manifest: Mapping[str, object],
    catalog: Mapping[str, object],
    config: Path,
    requested_as_of_date: date,
) -> str:
    manifest_attestation = manifest.get("source_attestation")
    catalog_attestation = catalog.get("source_attestation")
    if not isinstance(manifest_attestation, Mapping) or dict(manifest_attestation) != dict(
        catalog_attestation if isinstance(catalog_attestation, Mapping) else {}
    ):
        raise ApprovedPackageError(
            "IEX source_attestation must match in manifest and catalog entry"
        )
    sidecar_path = root / "source_attestation.json"
    source_path = root / "source.csv"
    if (
        not sidecar_path.is_file()
        or not source_path.is_file()
        or sidecar_path.is_symlink()
        or source_path.is_symlink()
    ):
        raise ApprovedPackageError(
            "IEX approved package requires local source_attestation.json and source.csv"
        )
    if _file_sha256(source_path) != manifest.get("source_sha256"):
        raise ApprovedPackageError("IEX local source.csv SHA-256 mismatch")
    if _file_sha256(sidecar_path) != manifest_attestation.get("sha256"):
        raise ApprovedPackageError("IEX local source_attestation.json SHA-256 mismatch")
    sidecar = _read_json(sidecar_path)
    if manifest_attestation.get("path") != "source_attestation.json":
        raise ApprovedPackageError("IEX source_attestation path must be package-relative")
    if manifest_attestation.get("source_path") != "source.csv":
        raise ApprovedPackageError("IEX source path must be package-relative")

    aliases = {"upstream_provider": "provider"}
    ignored_summary_fields = {"path", "source_path", "sha256"}
    for field, expected in manifest_attestation.items():
        resolved_field = str(field)
        if resolved_field in ignored_summary_fields:
            continue
        sidecar_field = aliases.get(resolved_field, resolved_field)
        if sidecar.get(sidecar_field) != expected:
            raise ApprovedPackageError(f"IEX preserved source_attestation mismatch: {field}")

    exact_contract: dict[str, object] = {
        "schema_version": "1.1",
        "provenance_contract_version": "iex-1.0",
        "provider": IEX_PROVIDER,
        "feed": "iex",
        "frequency": "1d",
        "adjustment_policy": "all",
        "corporate_action_policy": "alpaca_adjustment_all",
        "request_timezone": "UTC",
        "bar_timestamp_timezone": "UTC",
        "timestamp_semantics": "XNYS_exchange_session_date",
        "exchange_calendar": "XNYS",
        "calendar_contract_version": market_calendar.XNYS_CALENDAR_CONTRACT_VERSION,
        "calendar_sha256": market_calendar.XNYS_CALENDAR_SHA256,
        "status": "OK",
        "published": True,
        "blockers": [],
        "attestation_eligible": True,
        "client_injected": False,
        "clock_injected": False,
    }
    for field, expected in exact_contract.items():
        actual = sidecar.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise ApprovedPackageError(f"invalid IEX source_attestation field: {field}")
    if manifest_attestation.get("source_sha256") != manifest.get("source_sha256"):
        raise ApprovedPackageError(
            "IEX source_attestation source_sha256 does not match manifest"
        )

    implementation_hash = getattr(
        market_calendar, "XNYS_CALENDAR_IMPLEMENTATION_SHA256", None
    )
    implementation_hasher = getattr(
        market_calendar, "xnys_calendar_implementation_sha256", None
    )
    if not isinstance(implementation_hash, str) or not callable(implementation_hasher):
        raise ApprovedPackageError("XNYS calendar implementation contract is unavailable")
    if implementation_hasher() != implementation_hash:
        raise ApprovedPackageError("XNYS calendar implementation SHA-256 drift")
    if sidecar.get("calendar_implementation_sha256") != implementation_hash:
        raise ApprovedPackageError("IEX calendar_implementation_sha256 mismatch")
    try:
        market_calendar.verified_xnys_trading_days(
            _parse_date(sidecar.get("start"), "source_attestation.start"),
            _parse_date(sidecar.get("end"), "source_attestation.end"),
            calendar_contract_version=str(sidecar.get("calendar_contract_version")),
            calendar_sha256=str(sidecar.get("calendar_sha256")),
        )
    except ValueError as exc:
        raise ApprovedPackageError(str(exc)) from exc

    normalizer = Path(__file__).parent.parent / "data" / "alpaca_market_data.py"
    for field, path in (
        ("universe_config_sha256", config),
        ("normalization_code_sha256", normalizer),
    ):
        if sidecar.get(field) != _file_sha256(path):
            raise ApprovedPackageError(f"IEX provenance hash drift: {field}")
    try:
        sdk_version = importlib.metadata.version("alpaca-py")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ApprovedPackageError(
            "alpaca-py must be installed to verify IEX provenance"
        ) from exc
    if sidecar.get("sdk_package") != "alpaca-py" or sidecar.get("sdk_version") != sdk_version:
        raise ApprovedPackageError("IEX SDK provenance mismatch")

    generated_at = _parse_datetime(sidecar.get("generated_at"))
    expected_watermark = market_calendar.latest_closed_xnys_session(generated_at).isoformat()
    if generated_at.date() > requested_as_of_date:
        raise ApprovedPackageError("IEX generated_at exceeds requested as_of_date")
    if date.fromisoformat(expected_watermark) > requested_as_of_date:
        raise ApprovedPackageError(
            "IEX closed_session_watermark exceeds requested as_of_date"
        )
    if sidecar.get("closed_session_watermark") != expected_watermark:
        raise ApprovedPackageError(
            "IEX closed_session_watermark does not match generated_at"
        )
    expected_sessions = {
        day.isoformat()
        for day in market_calendar.verified_xnys_trading_days(
            _parse_date(sidecar["start"], "source_attestation.start"),
            _parse_date(sidecar["end"], "source_attestation.end"),
            calendar_contract_version=str(sidecar["calendar_contract_version"]),
            calendar_sha256=str(sidecar["calendar_sha256"]),
        )
    }
    source_records = read_records(source_path)
    source_manifest = build_dataset_manifest(source_records, source=str(source_path))
    if sidecar.get("row_count") != source_manifest.get("row_count"):
        raise ApprovedPackageError("IEX source_attestation row_count mismatch")
    if sidecar.get("observed_start") != source_manifest.get("start"):
        raise ApprovedPackageError("IEX source_attestation observed_start mismatch")
    if sidecar.get("observed_end") != source_manifest.get("end"):
        raise ApprovedPackageError("IEX source_attestation observed_end mismatch")
    if sidecar.get("row_count") != manifest.get("row_count"):
        raise ApprovedPackageError("IEX source row_count does not match manifest")
    sidecar_symbols = _canonical_symbols(sidecar.get("symbols"))
    source_symbols = _canonical_symbols(source_manifest.get("symbols"))
    manifest_symbols = _canonical_symbols(manifest.get("symbols"))
    if sidecar_symbols != source_symbols:
        raise ApprovedPackageError("IEX source_attestation symbols mismatch")
    if sidecar_symbols != manifest_symbols:
        raise ApprovedPackageError("IEX source symbols do not match manifest")
    if sidecar.get("observed_start") != manifest.get("start"):
        raise ApprovedPackageError("IEX source start does not match manifest")
    if sidecar.get("observed_end") != manifest.get("end"):
        raise ApprovedPackageError("IEX source end does not match manifest")
    symbols = sidecar.get("symbols")
    if not isinstance(symbols, list):
        raise ApprovedPackageError("IEX source_attestation symbols must be a list")
    for symbol in symbols:
        actual = {
            _normalized_session(row.get("timestamp"))
            for row in source_records
            if str(row.get("symbol", "")).upper() == str(symbol).upper()
        }
        if actual != expected_sessions:
            raise ApprovedPackageError(
                f"IEX source session coverage mismatch: {symbol}:"
                f"missing={len(expected_sessions - actual)}:"
                f"unexpected={len(actual - expected_sessions)}"
            )
    return str(source_manifest["dataset_hash"])


def _parse_date(value: object, field: str) -> date:
    text = str(value).strip()
    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ApprovedPackageError(f"invalid {field}: {value}") from exc


def _parse_datetime(value: object) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        resolved = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApprovedPackageError("invalid IEX generated_at") from exc
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ApprovedPackageError("IEX generated_at must be timezone-aware")
    return resolved


def _normalized_session(value: object) -> str:
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ApprovedPackageError(f"invalid IEX source session timestamp: {value}") from exc


def _canonical_symbols(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(symbol, str) or not symbol.strip() for symbol in value
    ):
        raise ApprovedPackageError("IEX symbols must be a non-empty string list")
    normalized = tuple(sorted(symbol.strip().upper() for symbol in value))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ApprovedPackageError("IEX symbols must be unique")
    return normalized


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ApprovedPackageError(f"unable to hash approved provenance file: {path}") from exc
