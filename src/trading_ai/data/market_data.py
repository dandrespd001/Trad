"""Market data provider boundaries for offline data refreshes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from trading_ai.data.io import read_records


@dataclass(frozen=True)
class MarketDataRequest:
    symbols: tuple[str, ...]
    start: str | date
    end: str | date


class MarketDataProvider(Protocol):
    def load(self, request: MarketDataRequest) -> list[dict[str, object]]:
        """Return OHLCV rows for a bounded request."""


class ApprovedLocalMarketDataProvider:
    """Read market data from an approved local file without network access."""

    def __init__(self, source_path: str | Path) -> None:
        self.source_path = Path(source_path)

    def load(self, request: MarketDataRequest) -> list[dict[str, object]]:
        start = _parse_request_date(request.start, "start")
        end = _parse_request_date(request.end, "end")
        if end < start:
            raise ValueError("request end must be on or after start")

        allowed = {symbol.upper() for symbol in request.symbols}
        records: list[dict[str, object]] = []
        for index, row in enumerate(read_records(self.source_path)):
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in allowed:
                continue

            timestamp = str(row.get("timestamp", ""))
            row_date = _parse_row_date(timestamp)
            if row_date is None:
                raise ValueError(f"row {index} invalid timestamp: {timestamp}")
            if not start <= row_date <= end:
                continue

            normalized = dict(row)
            normalized["symbol"] = symbol
            records.append(normalized)

        return sorted(
            records,
            key=lambda row: (str(row.get("timestamp", "")), str(row.get("symbol", ""))),
        )


ApprovedCsvMarketDataProvider = ApprovedLocalMarketDataProvider


class InMemoryMarketDataProvider:
    """Deterministic provider for API-boundary tests without network access."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = [dict(record) for record in records]
        self.network_used = False

    def load(self, request: MarketDataRequest) -> list[dict[str, object]]:
        start = _parse_request_date(request.start, "start")
        end = _parse_request_date(request.end, "end")
        if end < start:
            raise ValueError("request end must be on or after start")

        allowed = {symbol.upper() for symbol in request.symbols}
        records: list[dict[str, object]] = []
        for row in self._records:
            symbol = str(row.get("symbol", "")).upper()
            row_date = _parse_row_date(str(row.get("timestamp", "")))
            if symbol not in allowed or row_date is None or not start <= row_date <= end:
                continue
            normalized = dict(row)
            normalized["symbol"] = symbol
            records.append(normalized)
        return sorted(records, key=lambda row: (str(row.get("timestamp", "")), str(row.get("symbol", ""))))


def _parse_request_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid {field_name} date: {value}") from exc


def _parse_row_date(timestamp: str) -> date | None:
    if len(timestamp) < 10:
        return None
    try:
        return date.fromisoformat(timestamp[:10])
    except ValueError:
        return None
