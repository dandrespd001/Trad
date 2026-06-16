"""Deterministic sample data for local CLI smoke tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta


def generate_sample_ohlcv(
    *,
    symbols: tuple[str, ...],
    start: str,
    end: str,
) -> list[dict[str, object]]:
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    if end_date < start_date:
        raise ValueError("--to must be on or after --from")

    records: list[dict[str, object]] = []
    current = start_date
    day_index = 0
    while current <= end_date:
        if current.weekday() < 5:
            for symbol_index, symbol in enumerate(symbols):
                trend = 0.20 * day_index * (1 if symbol_index % 2 == 0 else -0.25)
                base = 100.0 + symbol_index * 3.0 + trend
                close = round(max(base, 1.0), 4)
                records.append(
                    {
                        "timestamp": current.isoformat(),
                        "symbol": symbol,
                        "open": round(close - 0.2, 4),
                        "high": round(close + 0.5, 4),
                        "low": round(close - 0.5, 4),
                        "close": close,
                        "volume": 1_000_000 + symbol_index * 10_000 + day_index * 1_000,
                    }
                )
            day_index += 1
        current += timedelta(days=1)
    return records


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()
