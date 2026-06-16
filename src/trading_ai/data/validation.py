"""OHLCV data quality checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


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
) -> ValidationResult:
    rows = list(records)
    errors: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    symbols: list[str] = []

    for index, row in enumerate(rows):
        missing = [column for column in REQUIRED_COLUMNS if column not in row or row[column] in {None, ""}]
        for column in missing:
            errors.append(f"row {index} missing required column: {column}")
        if missing:
            continue

        timestamp = str(row["timestamp"])
        symbol = str(row["symbol"]).upper()
        if symbol not in symbols:
            symbols.append(symbol)
        pair = (timestamp, symbol)
        if pair in seen_pairs:
            errors.append(f"duplicate timestamp/symbol pair: {timestamp} {symbol}")
        seen_pairs.add(pair)

        prices = {column: _to_float(row[column], index, column, errors) for column in PRICE_COLUMNS}
        volume = _to_float(row["volume"], index, "volume", errors)
        if any(value is None for value in prices.values()) or volume is None:
            continue
        if any(value < 0 for value in prices.values()) or volume < 0:
            errors.append(f"row {index} contains negative price or volume")
        if prices["high"] < max(prices["open"], prices["close"]):
            errors.append(f"row {index} high below open/close")
        if prices["low"] > min(prices["open"], prices["close"]):
            errors.append(f"row {index} low above open/close")
        if prices["high"] < prices["low"]:
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
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"row {index} invalid numeric value for {column}")
        return None
