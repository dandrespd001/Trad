"""Reproducibility metadata for versioned datasets and runs."""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping


def dataset_hash(records: Iterable[Mapping[str, object]]) -> str:
    """Return a stable SHA-256 hash for a tabular dataset."""

    canonical_rows = [_canonical_row(row) for row in records]
    canonical_rows.sort(key=lambda row: (str(row.get("timestamp", "")), str(row.get("symbol", "")), json.dumps(row, sort_keys=True)))
    payload = json.dumps(canonical_rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_dataset_manifest(records: Iterable[Mapping[str, object]], *, source: str) -> dict[str, object]:
    rows = [dict(row) for row in records]
    timestamps = sorted(str(row["timestamp"]) for row in rows if "timestamp" in row)
    symbols = sorted({str(row["symbol"]).upper() for row in rows if "symbol" in row})
    columns = sorted({str(column) for row in rows for column in row})
    return {
        "schema_version": 1,
        "source": source,
        "dataset_hash": dataset_hash(rows),
        "row_count": len(rows),
        "symbols": symbols,
        "columns": columns,
        "start": timestamps[0] if timestamps else None,
        "end": timestamps[-1] if timestamps else None,
    }


def _canonical_row(row: Mapping[str, object]) -> dict[str, object]:
    canonical: dict[str, object] = {}
    for key, value in row.items():
        if key == "symbol":
            canonical[key] = str(value).upper()
        elif key == "timestamp":
            canonical[key] = str(value)
        elif value in {None, ""}:
            canonical[key] = value
        else:
            canonical[key] = _canonical_scalar(value)
    return canonical


def _canonical_scalar(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value
