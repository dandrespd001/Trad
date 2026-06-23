"""OHLCV data quality checks."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

REQUIRED_COLUMNS = ("timestamp", "symbol", "open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...]
    row_count: int
    symbols: tuple[str, ...]


def validate_ohlcv_records(
    records: Iterable[Mapping[str, object]],
    *,
    expected_symbols: Iterable[str] | None = None,
    allowed_symbols: Iterable[str] | None = None,
) -> ValidationResult:
    rows = list(records)
    errors: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    symbols: list[str] = []
    allowed = {symbol.upper() for symbol in allowed_symbols} if allowed_symbols is not None else None

    for index, row in enumerate(rows):
        missing = [column for column in REQUIRED_COLUMNS if column not in row or row[column] in {None, ""}]
        for column in missing:
            errors.append(f"row {index} missing required column: {column}")
        if missing:
            continue

        timestamp = str(row["timestamp"])
        if not _is_valid_timestamp(row["timestamp"]):
            errors.append(f"row {index} invalid timestamp: {timestamp}")
        symbol = str(row["symbol"]).upper()
        if symbol not in symbols:
            symbols.append(symbol)
        if allowed is not None and symbol not in allowed:
            errors.append(f"unexpected symbol outside universe: {symbol}")
        pair = (timestamp, symbol)
        if pair in seen_pairs:
            errors.append(f"duplicate timestamp/symbol pair: {timestamp} {symbol}")
        seen_pairs.add(pair)

        prices = {column: _to_float(row[column], index, column, errors) for column in PRICE_COLUMNS}
        volume = _to_float(row["volume"], index, "volume", errors)
        open_price = prices["open"]
        high_price = prices["high"]
        low_price = prices["low"]
        close_price = prices["close"]
        if (
            open_price is None
            or high_price is None
            or low_price is None
            or close_price is None
            or volume is None
        ):
            continue
        for column, value in (
            ("open", open_price),
            ("high", high_price),
            ("low", low_price),
            ("close", close_price),
        ):
            if value <= 0:
                errors.append(f"row {index} {column} must be greater than zero")
        if volume < 0:
            errors.append(f"row {index} contains negative price or volume")
        if high_price < max(open_price, close_price):
            errors.append(f"row {index} high below open/close")
        if low_price > min(open_price, close_price):
            errors.append(f"row {index} low above open/close")
        if high_price < low_price:
            errors.append(f"row {index} high below low")

    if expected_symbols is not None:
        expected = {symbol.upper() for symbol in expected_symbols}
        actual = set(symbols)
        missing_symbols = sorted(expected - actual)
        extra_symbols = sorted(actual - expected)
        for symbol in missing_symbols:
            errors.append(f"missing expected symbol: {symbol}")
        for symbol in extra_symbols:
            errors.append(f"unexpected symbol: {symbol}")

    return ValidationResult(
        valid=not errors,
        errors=tuple(errors),
        row_count=len(rows),
        symbols=tuple(symbols),
    )


def _to_float(value: object, index: int, column: str, errors: list[str]) -> float | None:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        errors.append(f"row {index} invalid numeric value for {column}")
        return None
    if not math.isfinite(number):
        errors.append(f"row {index} invalid numeric value for {column}")
        return None
    return number


def _is_valid_timestamp(value: object) -> bool:
    if isinstance(value, datetime | date):
        return True
    timestamp = str(value).strip()
    if not timestamp:
        return False
    try:
        date.fromisoformat(timestamp)
        return True
    except ValueError:
        pass
    if timestamp.endswith("Z"):
        timestamp = f"{timestamp[:-1]}+00:00"
    try:
        datetime.fromisoformat(timestamp)
        return True
    except ValueError:
        return False
