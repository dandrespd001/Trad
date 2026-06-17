"""Small dataset IO helpers with optional Parquet support."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping


NUMERIC_COLUMNS = {"open", "high", "low", "close", "volume"}
PARQUET_DEPENDENCY_MESSAGE = 'Parquet support requires pandas and pyarrow. Install with: pip install -e ".[research]"'


class ParquetDependencyError(RuntimeError):
    """Raised when optional Parquet dependencies are unavailable."""


def read_records(path: str | Path) -> list[dict[str, object]]:
    dataset_path = Path(path)
    if dataset_path.suffix.lower() == ".parquet":
        return _read_parquet(dataset_path)
    return read_csv_records(dataset_path)


def write_records(records: Iterable[Mapping[str, object]], path: str | Path) -> None:
    dataset_path = Path(path)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [dict(row) for row in records]
    if dataset_path.suffix.lower() == ".parquet":
        _write_parquet(rows, dataset_path)
        return
    write_csv_records(rows, dataset_path)


def read_csv_records(path: str | Path) -> list[dict[str, object]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [_coerce_row(row) for row in reader]


def write_csv_records(records: Iterable[Mapping[str, object]], path: str | Path) -> None:
    rows = [dict(row) for row in records]
    if not rows:
        raise ValueError("cannot write an empty dataset")
    fieldnames = list(rows[0].keys())
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _coerce_row(row: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in row.items():
        if key in NUMERIC_COLUMNS and value not in {None, ""}:
            result[key] = float(value)
        else:
            result[key] = value
    return result


def _read_parquet(path: Path) -> list[dict[str, object]]:
    pd = ensure_parquet_support()
    return pd.read_parquet(path).to_dict(orient="records")


def _write_parquet(records: list[dict[str, object]], path: Path) -> None:
    pd = ensure_parquet_support()
    pd.DataFrame.from_records(records).to_parquet(path, index=False)


def ensure_parquet_support():
    try:
        import pandas as pd
        import pyarrow  # noqa: F401
    except ModuleNotFoundError as exc:
        raise ParquetDependencyError(PARQUET_DEPENDENCY_MESSAGE) from exc
    return pd
