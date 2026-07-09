"""Governed read-only Alpaca market-data ingestion (never touches trading endpoints).

This module only reads process environment variables supplied by the operator
(the same paper-account credential names used elsewhere in the project). It
never imports ``TradingClient``, order/submit helpers, or any
``trading_ai.execution.live_*`` module -- it fetches daily OHLCV bars from the
Alpaca market-data API (IEX feed) for the approved universe only, validates
them with the shared OHLCV validator, and writes the canonical CSV plus a
JSON safety sidecar. Credential values are never logged or included in error
messages.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from pathlib import Path

from trading_ai.config import load_universe_config
from trading_ai.data.io import write_records
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.execution.paper_common import paper_exit_code, write_json_artifact

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "data/incoming/fresh_source.csv"

ALPACA_PAPER_API_KEY_ENV = "ALPACA_PAPER_API_KEY"
ALPACA_PAPER_SECRET_KEY_ENV = "ALPACA_PAPER_SECRET_KEY"  # noqa: S105


class AlpacaMarketDataError(RuntimeError):
    """Raised when governed Alpaca market-data ingestion fails.

    Messages never include credential values, only (at most) the names of
    missing environment variables.
    """


@dataclass(frozen=True)
class FetchResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def build_alpaca_market_data_client(*, env: Mapping[str, str] | None = None):
    """Build a lazily-imported ``StockHistoricalDataClient`` from paper credentials.

    Reads the same environment variables as
    :mod:`trading_ai.execution.alpaca_connection` (``ALPACA_PAPER_API_KEY`` /
    ``ALPACA_PAPER_SECRET_KEY``). This module intentionally does not import
    from ``trading_ai.execution`` so that ``data/`` stays free of any
    dependency on the execution/broker layer.
    """

    values = os.environ if env is None else env
    api_key = str(values.get(ALPACA_PAPER_API_KEY_ENV, "")).strip()
    secret_key = str(values.get(ALPACA_PAPER_SECRET_KEY_ENV, "")).strip()
    missing = []
    if not api_key:
        missing.append(ALPACA_PAPER_API_KEY_ENV)
    if not secret_key:
        missing.append(ALPACA_PAPER_SECRET_KEY_ENV)
    if missing:
        raise AlpacaMarketDataError(
            "missing Alpaca paper credential environment variables: " + ", ".join(missing)
        )

    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise AlpacaMarketDataError(
            "alpaca-py is not installed; install the broker optional dependency before market-data access"
        ) from exc
    return StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)


def build_alpaca_crypto_data_client(*, env: Mapping[str, str] | None = None):
    """Build a lazily-imported ``CryptoHistoricalDataClient`` for read-only crypto bars.

    Unlike the equities endpoint, the Alpaca crypto market-data API is public, so
    credentials are OPTIONAL: if both ``ALPACA_PAPER_API_KEY`` and
    ``ALPACA_PAPER_SECRET_KEY`` are present and non-empty they are forwarded
    (matching the equity client), otherwise the client is constructed without
    arguments. No error is raised for missing credentials.
    """

    values = os.environ if env is None else env
    api_key = str(values.get(ALPACA_PAPER_API_KEY_ENV, "")).strip()
    secret_key = str(values.get(ALPACA_PAPER_SECRET_KEY_ENV, "")).strip()

    try:
        from alpaca.data.historical.crypto import CryptoHistoricalDataClient
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise AlpacaMarketDataError(
            "alpaca-py is not installed; install the broker optional dependency before market-data access"
        ) from exc

    if api_key and secret_key:
        return CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    return CryptoHistoricalDataClient()


def fetch_crypto_daily_bars(
    *,
    symbols: Iterable[str],
    start: str | date,
    end: str | date,
    client: object | None = None,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Fetch daily OHLCV bars for crypto ``symbols`` between ``start`` and ``end``.

    Mirror of :func:`fetch_daily_bars` for the crypto endpoint. Symbols are
    normalized with strip+upper; the ``/`` separator is preserved (e.g.
    ``"btc/usd"`` → ``"BTC/USD"``). Deduplicated and sorted. Empty symbols and
    ``start`` > ``end`` raise :class:`ValueError`.
    """

    normalized_symbols = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if not normalized_symbols:
        raise ValueError("symbols must not be empty")

    start_date = _parse_date(start, "start")
    end_date = _parse_date(end, "end")
    if start_date > end_date:
        raise ValueError("start must not be after end")

    resolved_client = (
        client if client is not None else build_alpaca_crypto_data_client(env=env)
    )

    try:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame

        # Crypto endpoint does not accept a ``feed`` parameter.
        request: object = CryptoBarsRequest(
            symbol_or_symbols=normalized_symbols,
            timeframe=TimeFrame.Day,
            start=datetime(start_date.year, start_date.month, start_date.day),
            end=datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59),
        )
    except ModuleNotFoundError as exc:
        if client is None:
            raise AlpacaMarketDataError(
                "alpaca-py is required for real market-data fetches (pip install .[broker])"
            ) from exc
        # An injected client (tests/fakes) does not need the real request
        # classes; a duck-typed request keeps the no-broker-deps test gate
        # green, matching the repo's pure-python core rule.
        request = SimpleNamespace(
            symbol_or_symbols=normalized_symbols,
            timeframe="1Day",
            start=datetime(start_date.year, start_date.month, start_date.day),
            end=datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59),
        )
    response = resolved_client.get_crypto_bars(request)
    bars_by_symbol = _extract_bars(response)

    records: list[dict[str, object]] = []
    for symbol in normalized_symbols:
        for bar in bars_by_symbol.get(symbol, []):
            records.append(_normalize_bar(symbol, bar))

    records.sort(key=lambda row: (str(row["timestamp"]), str(row["symbol"])))
    return records


def fetch_daily_bars(
    *,
    symbols: Iterable[str],
    start: str | date,
    end: str | date,
    client: object | None = None,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Fetch daily OHLCV bars (IEX feed) for ``symbols`` between ``start`` and ``end``.

    Returns canonical records ``{timestamp, symbol, open, high, low, close, volume}``
    sorted by ``(timestamp, symbol)``. ``symbols`` are upper-cased and deduplicated.
    Raises ``ValueError`` if ``symbols`` is empty or ``start`` is after ``end``.
    """

    normalized_symbols = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if not normalized_symbols:
        raise ValueError("symbols must not be empty")

    start_date = _parse_date(start, "start")
    end_date = _parse_date(end, "end")
    if start_date > end_date:
        raise ValueError("start must not be after end")

    resolved_client = client if client is not None else build_alpaca_market_data_client(env=env)

    try:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        request: object = StockBarsRequest(
            symbol_or_symbols=normalized_symbols,
            timeframe=TimeFrame.Day,
            start=datetime(start_date.year, start_date.month, start_date.day),
            # end at 23:59:59: a bare date means "through that day"; midnight
            # would exclude the end day's bar entirely (found in production on
            # the campaign's first cycle: dataset_stale despite a same-day fetch).
            end=datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59),
            feed=DataFeed.IEX,
        )
    except ModuleNotFoundError as exc:
        if client is None:
            raise AlpacaMarketDataError(
                "alpaca-py is required for real market-data fetches (pip install .[broker])"
            ) from exc
        # An injected client (tests/fakes) does not need the real request
        # classes; a duck-typed request keeps the no-broker-deps test gate
        # green, matching the repo's pure-python core rule.
        request = SimpleNamespace(
            symbol_or_symbols=normalized_symbols,
            timeframe="1Day",
            start=datetime(start_date.year, start_date.month, start_date.day),
            end=datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59),
            feed="iex",
        )
    response = resolved_client.get_stock_bars(request)
    bars_by_symbol = _extract_bars(response)

    records: list[dict[str, object]] = []
    for symbol in normalized_symbols:
        for bar in bars_by_symbol.get(symbol, []):
            records.append(_normalize_bar(symbol, bar))

    records.sort(key=lambda row: (str(row["timestamp"]), str(row["symbol"])))
    return records


def run_market_data_fetch(
    *,
    config: str | Path,
    start: str | date,
    end: str | date,
    output: str | Path,
    client: object | None = None,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> FetchResult:
    """Fetch, validate, and persist governed daily bars for the approved universe.

    Writes the canonical CSV to ``output`` plus a ``<output>.fetch.json`` safety
    sidecar. Invalid data or zero total rows blocks the write and returns
    ``BLOCKED``. A universe symbol with no returned bars is reported as a
    ``symbol_missing_bars:<SYM>`` blocker with ``WARN`` status, but the CSV is
    still written for the remaining symbols.
    """

    universe = load_universe_config(config)
    output_path = Path(output)
    sidecar_path = output_path.with_name(output_path.name + ".fetch.json")

    if universe.asset_type == "crypto":
        records = fetch_crypto_daily_bars(
            symbols=universe.symbols, start=start, end=end, client=client, env=env
        )
        provider = "alpaca_crypto_data"
        feed = "us"
    else:
        records = fetch_daily_bars(
            symbols=universe.symbols, start=start, end=end, client=client, env=env
        )
        provider = "alpaca_market_data"
        feed = "iex"

    validation = validate_ohlcv_records(records, allowed_symbols=universe.symbols)

    per_symbol_row_counts: dict[str, int] = {symbol: 0 for symbol in universe.symbols}
    for record in records:
        symbol = str(record.get("symbol", "")).upper()
        if symbol in per_symbol_row_counts:
            per_symbol_row_counts[symbol] += 1

    missing_symbols = [symbol for symbol in universe.symbols if per_symbol_row_counts.get(symbol, 0) == 0]

    blockers: list[str] = list(validation.errors)
    blockers.extend(f"symbol_missing_bars:{symbol}" for symbol in missing_symbols)

    row_count = len(records)
    if not validation.valid or row_count == 0:
        status = "BLOCKED"
    elif missing_symbols:
        status = "WARN"
        write_records(records, output_path)
    else:
        status = "OK"
        write_records(records, output_path)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "start": _parse_date(start, "start").isoformat(),
        "end": _parse_date(end, "end").isoformat(),
        "symbols": list(universe.symbols),
        "row_count": row_count,
        "per_symbol_row_counts": per_symbol_row_counts,
        "provider": provider,
        "feed": feed,
        "status": status,
        "blockers": blockers,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "market_data_client_built": True,
            "credentials_read": True,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    write_json_artifact(payload, sidecar_path)

    return FetchResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=payload,
    )


def _extract_bars(response: object) -> Mapping[str, list[object]]:
    data = getattr(response, "data", None)
    if isinstance(data, Mapping):
        return data
    if isinstance(response, Mapping):
        return response
    raise AlpacaMarketDataError("unexpected response shape from Alpaca market-data client")


def _normalize_bar(symbol: str, bar: object) -> dict[str, object]:
    return {
        "timestamp": _bar_date(_bar_value(bar, "timestamp")),
        "symbol": symbol,
        "open": float(_bar_value(bar, "open")),
        "high": float(_bar_value(bar, "high")),
        "low": float(_bar_value(bar, "low")),
        "close": float(_bar_value(bar, "close")),
        "volume": float(_bar_value(bar, "volume")),
    }


def _bar_value(bar: object, key: str) -> object:
    if isinstance(bar, Mapping):
        return bar.get(key)
    return getattr(bar, key, None)


def _bar_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return text[:10]


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date in YYYY-MM-DD format") from exc


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
