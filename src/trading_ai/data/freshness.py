"""Freshness checks for model-usable market data rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping


REASON_ORDER = (
    "empty_dataset",
    "missing_symbol",
    "stale_symbol",
    "future_timestamp",
    "invalid_timestamp",
)


@dataclass(frozen=True)
class SymbolFreshness:
    symbol: str
    status: str
    timestamp: str | None = None
    age_days: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "timestamp": self.timestamp,
            "age_days": self.age_days,
        }


@dataclass(frozen=True)
class FreshnessResult:
    allowed: bool
    reasons: tuple[str, ...]
    as_of_date: date
    max_age_days: int
    symbols: dict[str, SymbolFreshness]

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "as_of_date": self.as_of_date.isoformat(),
            "max_age_days": self.max_age_days,
            "symbols": {symbol: detail.to_dict() for symbol, detail in sorted(self.symbols.items())},
        }


def evaluate_ohlcv_freshness(
    records: Iterable[Mapping[str, object]],
    *,
    expected_symbols: Iterable[str],
    as_of_date: date,
    max_age_days: int,
) -> FreshnessResult:
    rows = list(records)
    expected = tuple(dict.fromkeys(str(symbol).upper() for symbol in expected_symbols))
    reasons: set[str] = set()
    latest_valid: dict[str, tuple[date, str]] = {}
    invalid_symbols: set[str] = set()

    if not rows:
        reasons.add("empty_dataset")

    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if symbol not in expected:
            continue
        timestamp = str(row.get("timestamp", ""))
        parsed_timestamp = _parse_timestamp_date(timestamp)
        if parsed_timestamp is None:
            invalid_symbols.add(symbol)
            reasons.add("invalid_timestamp")
            continue
        previous = latest_valid.get(symbol)
        if previous is None or parsed_timestamp > previous[0]:
            latest_valid[symbol] = (parsed_timestamp, timestamp)

    symbol_status: dict[str, SymbolFreshness] = {}
    for symbol in expected:
        latest = latest_valid.get(symbol)
        if latest is None:
            if symbol in invalid_symbols:
                symbol_status[symbol] = SymbolFreshness(symbol=symbol, status="invalid_timestamp")
            else:
                reasons.add("missing_symbol")
                symbol_status[symbol] = SymbolFreshness(symbol=symbol, status="missing")
            continue

        latest_date, timestamp = latest
        age_days = (as_of_date - latest_date).days
        if age_days < 0:
            reasons.add("future_timestamp")
            symbol_status[symbol] = SymbolFreshness(
                symbol=symbol,
                status="future_timestamp",
                timestamp=timestamp,
                age_days=age_days,
            )
        elif age_days > max_age_days:
            reasons.add("stale_symbol")
            symbol_status[symbol] = SymbolFreshness(
                symbol=symbol,
                status="stale",
                timestamp=timestamp,
                age_days=age_days,
            )
        else:
            symbol_status[symbol] = SymbolFreshness(
                symbol=symbol,
                status="fresh",
                timestamp=timestamp,
                age_days=age_days,
            )

    ordered_reasons = tuple(reason for reason in REASON_ORDER if reason in reasons)
    return FreshnessResult(
        allowed=not ordered_reasons,
        reasons=ordered_reasons,
        as_of_date=as_of_date,
        max_age_days=max_age_days,
        symbols=symbol_status,
    )


def _parse_timestamp_date(timestamp: str) -> date | None:
    value = timestamp.strip()
    if len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
