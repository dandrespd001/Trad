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

import hashlib
import importlib.metadata
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from trading_ai.config import load_universe_config
from trading_ai.data.io import write_records
from trading_ai.data.market_calendar import (
    XNYS_CALENDAR_CONTRACT_VERSION,
    XNYS_CALENDAR_SHA256,
    XnysCalendarContractError,
    latest_closed_xnys_session,
    verified_xnys_trading_days,
    xnys_calendar_implementation_sha256,
)
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.execution.paper_common import paper_exit_code

SCHEMA_VERSION = "1.1"
EQUITY_PROVENANCE_CONTRACT_VERSION = "iex-1.0"
DEFAULT_OUTPUT = "data/incoming/fresh_source.csv"
EQUITY_DATA_FEED = "iex"
EQUITY_ADJUSTMENT_POLICY = "all"
EQUITY_CORPORATE_ACTION_POLICY = "alpaca_adjustment_all"
EQUITY_REQUEST_TIMEZONE = "UTC"
EQUITY_BAR_TIMESTAMP_TIMEZONE = "UTC"
EQUITY_TIMESTAMP_SEMANTICS = "XNYS_exchange_session_date"
EQUITY_EXCHANGE_CALENDAR = "XNYS"

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
    return _enforce_audited_market_data_transport(
        StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    )


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

    client = (
        CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        if api_key and secret_key
        else CryptoHistoricalDataClient()
    )
    return _enforce_audited_market_data_transport(client)


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

    resolved_client = _enforce_audited_market_data_transport(
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
            records.append(
                _normalize_bar(
                    symbol,
                    bar,
                    require_utc=client is None,
                )
            )

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

    resolved_client = _enforce_audited_market_data_transport(
        client if client is not None else build_alpaca_market_data_client(env=env)
    )

    try:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        request: object = StockBarsRequest(
            symbol_or_symbols=normalized_symbols,
            timeframe=TimeFrame.Day,
            start=datetime(
                start_date.year,
                start_date.month,
                start_date.day,
                tzinfo=UTC,
            ),
            # end at 23:59:59: a bare date means "through that day"; midnight
            # would exclude the end day's bar entirely (found in production on
            # the campaign's first cycle: dataset_stale despite a same-day fetch).
            end=datetime(
                end_date.year,
                end_date.month,
                end_date.day,
                23,
                59,
                59,
                tzinfo=UTC,
            ),
            feed=DataFeed.IEX,
            adjustment=Adjustment.ALL,
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
            start=datetime(
                start_date.year,
                start_date.month,
                start_date.day,
                tzinfo=UTC,
            ),
            end=datetime(
                end_date.year,
                end_date.month,
                end_date.day,
                23,
                59,
                59,
                tzinfo=UTC,
            ),
            feed=EQUITY_DATA_FEED,
            adjustment=EQUITY_ADJUSTMENT_POLICY,
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
    sidecar. Invalid, incomplete, stale, or out-of-range data blocks publication
    and returns ``BLOCKED``. A blocked fetch never replaces a previously
    published CSV; its diagnostic sidecar is still updated atomically.
    """

    universe = load_universe_config(config)
    output_path = Path(output)
    sidecar_path = output_path.with_name(output_path.name + ".fetch.json")
    start_date = _parse_date(start, "start")
    end_date = _parse_date(end, "end")
    generated_at_value = generated_at or _utc_now()
    expected_sessions: list[date] = []

    if universe.asset_type == "crypto":
        records = fetch_crypto_daily_bars(
            symbols=universe.symbols, start=start, end=end, client=client, env=env
        )
        provider = "alpaca_crypto_data"
        feed = "us"
        schema_version = SCHEMA_VERSION
        source_contract: dict[str, object] = {}
    else:
        try:
            expected_sessions = verified_xnys_trading_days(
                start_date,
                end_date,
                calendar_contract_version=XNYS_CALENDAR_CONTRACT_VERSION,
                calendar_sha256=XNYS_CALENDAR_SHA256,
            )
            closed_session_watermark = latest_closed_xnys_session(
                _parse_generated_at(generated_at_value)
            )
        except XnysCalendarContractError as exc:
            raise AlpacaMarketDataError(str(exc)) from exc
        if not expected_sessions:
            raise AlpacaMarketDataError(
                "requested range contains no governed XNYS sessions"
            )
        latest_requested_session = expected_sessions[-1]
        if latest_requested_session > closed_session_watermark:
            raise AlpacaMarketDataError(
                f"latest requested XNYS session "
                f"{latest_requested_session.isoformat()} exceeds closed XNYS "
                f"session watermark {closed_session_watermark.isoformat()}"
            )
        records = fetch_daily_bars(
            symbols=universe.symbols, start=start, end=end, client=client, env=env
        )
        provider = "alpaca_market_data"
        feed = EQUITY_DATA_FEED
        schema_version = SCHEMA_VERSION
        source_contract = {
            "provenance_contract_version": EQUITY_PROVENANCE_CONTRACT_VERSION,
            "frequency": "1d",
            "adjustment_policy": EQUITY_ADJUSTMENT_POLICY,
            "corporate_action_policy": EQUITY_CORPORATE_ACTION_POLICY,
            "request_timezone": EQUITY_REQUEST_TIMEZONE,
            "bar_timestamp_timezone": EQUITY_BAR_TIMESTAMP_TIMEZONE,
            "timestamp_semantics": EQUITY_TIMESTAMP_SEMANTICS,
            "exchange_calendar": EQUITY_EXCHANGE_CALENDAR,
            "calendar_contract_version": XNYS_CALENDAR_CONTRACT_VERSION,
            "calendar_sha256": XNYS_CALENDAR_SHA256,
            "calendar_implementation_sha256": (
                xnys_calendar_implementation_sha256()
            ),
            "closed_session_watermark": closed_session_watermark.isoformat(),
            "sdk_package": "alpaca-py",
            "sdk_version": _alpaca_sdk_version(),
            "universe_config_sha256": _file_sha256(Path(config)),
            "normalization_code_sha256": _file_sha256(Path(__file__)),
            "attestation_eligible": client is None and generated_at is None,
            "client_injected": client is not None,
            "clock_injected": generated_at is not None,
        }

    validation = validate_ohlcv_records(records, allowed_symbols=universe.symbols)

    per_symbol_row_counts: dict[str, int] = {symbol: 0 for symbol in universe.symbols}
    per_symbol_latest_dates: dict[str, str | None] = {
        symbol: None for symbol in universe.symbols
    }
    observed_dates: list[date] = []
    range_blockers: list[str] = []
    for record in records:
        symbol = str(record.get("symbol", "")).upper()
        if symbol in per_symbol_row_counts:
            per_symbol_row_counts[symbol] += 1

        record_date = _record_date(record.get("timestamp"))
        if record_date is None:
            continue
        observed_dates.append(record_date)
        if symbol in per_symbol_latest_dates:
            previous_latest = per_symbol_latest_dates[symbol]
            if previous_latest is None or record_date.isoformat() > previous_latest:
                per_symbol_latest_dates[symbol] = record_date.isoformat()
        if record_date < start_date or record_date > end_date:
            range_blockers.append(
                f"bar_outside_requested_range:{symbol}:{record_date.isoformat()}"
            )

    missing_symbols = [symbol for symbol in universe.symbols if per_symbol_row_counts.get(symbol, 0) == 0]

    if universe.asset_type == "crypto":
        expected_latest_date: date | None = end_date
    else:
        expected_latest_date = expected_sessions[-1] if expected_sessions else None

    latest_bar_blockers: list[str] = []
    if expected_latest_date is not None:
        expected_latest = expected_latest_date.isoformat()
        for symbol in universe.symbols:
            actual_latest = per_symbol_latest_dates[symbol]
            if actual_latest is not None and actual_latest != expected_latest:
                latest_bar_blockers.append(
                    "symbol_missing_expected_latest_bar:"
                    f"{symbol}:expected={expected_latest}:actual={actual_latest}"
                )

    session_set_blockers: list[str] = []
    if universe.asset_type != "crypto":
        expected_session_dates = {
            session.isoformat() for session in expected_sessions
        }
        actual_sessions_by_symbol = {
            symbol: {
                str(record["timestamp"])
                for record in records
                if str(record.get("symbol", "")).upper() == symbol
            }
            for symbol in universe.symbols
        }
        for symbol, actual_sessions in actual_sessions_by_symbol.items():
            missing_count = len(expected_session_dates - actual_sessions)
            unexpected_count = len(actual_sessions - expected_session_dates)
            if missing_count or unexpected_count:
                session_set_blockers.append(
                    "symbol_session_set_mismatch:"
                    f"{symbol}:missing={missing_count}:unexpected={unexpected_count}"
                )

    blockers: list[str] = list(validation.errors)
    blockers.extend(f"symbol_missing_bars:{symbol}" for symbol in missing_symbols)
    blockers.extend(latest_bar_blockers)
    blockers.extend(range_blockers)
    blockers.extend(session_set_blockers)
    blockers = _dedupe(blockers)

    row_count = len(records)
    if blockers or row_count == 0:
        status = "BLOCKED"
        source_sha256 = None
        published = False
    else:
        status = "OK"
        source_sha256 = _write_records_atomic(records, output_path)
        published = True

    payload = {
        "schema_version": schema_version,
        "generated_at": generated_at_value,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "symbols": list(universe.symbols),
        "row_count": row_count,
        "per_symbol_row_counts": per_symbol_row_counts,
        "per_symbol_latest_dates": per_symbol_latest_dates,
        "expected_latest_bar_date": (
            expected_latest_date.isoformat() if expected_latest_date is not None else None
        ),
        "observed_start": min(observed_dates).isoformat() if observed_dates else None,
        "observed_end": max(observed_dates).isoformat() if observed_dates else None,
        "provider": provider,
        "feed": feed,
        "status": status,
        "blockers": blockers,
        "source_sha256": source_sha256,
        "published": published,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "market_data_client_built": True,
            "credentials_read": True,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
        **source_contract,
    }
    _write_json_atomic(payload, sidecar_path)

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


def _normalize_bar(
    symbol: str,
    bar: object,
    *,
    require_utc: bool = False,
) -> dict[str, object]:
    return {
        "timestamp": _bar_date(
            _bar_value(bar, "timestamp"),
            require_utc=require_utc,
        ),
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


def _bar_date(value: object, *, require_utc: bool = False) -> str:
    resolved: datetime | None = None
    if isinstance(value, datetime):
        resolved = value
    elif isinstance(value, date):
        if require_utc:
            raise AlpacaMarketDataError(
                "market-data daily bar timestamp must be timezone-aware UTC"
            )
        return value.isoformat()
    if resolved is None:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            resolved = datetime.fromisoformat(text)
        except ValueError as exc:
            raise AlpacaMarketDataError(
                "invalid market-data bar timestamp"
            ) from exc
    if require_utc:
        offset = resolved.utcoffset()
        if offset is None or offset.total_seconds() != 0.0:
            raise AlpacaMarketDataError(
                "market-data daily bar timestamp must be timezone-aware UTC"
            )
    return resolved.date().isoformat()


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date in YYYY-MM-DD format") from exc


def _parse_generated_at(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        resolved = datetime.fromisoformat(text)
    except ValueError as exc:
        raise XnysCalendarContractError(
            "generated_at must be an ISO timezone-aware datetime"
        ) from exc
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise XnysCalendarContractError(
            "generated_at must include a timezone offset"
        )
    return resolved


def _enforce_audited_market_data_transport(client: object) -> object:
    """Apply the exact-version, fixed-timeout, zero-retry policy to real SDK clients.

    Test doubles remain portable.  A real ``RESTClient`` arriving through the
    public injection seam is hardened too, and an already guarded client is
    revalidated so session/retry tampering fails before the next HTTP request.
    """

    from trading_ai.execution.paper_account_executor import (  # noqa: PLC0415
        PaperAccountAuthorityError,
        enforce_audited_alpaca_http_transport,
    )

    try:
        return enforce_audited_alpaca_http_transport(client)
    except PaperAccountAuthorityError as exc:
        raise AlpacaMarketDataError(str(exc)) from exc


def _record_date(value: object) -> date | None:
    text = str(value).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _write_records_atomic(records: Iterable[Mapping[str, object]], path: Path) -> str:
    # Preserve the destination suffix so ``write_records`` keeps its CSV vs
    # Parquet format selection while writing the isolated temporary file.
    temp_path = _sibling_temp_path(path, suffix=path.suffix or ".tmp")
    try:
        write_records(records, temp_path)
        source_sha256 = _file_sha256(temp_path)
        os.replace(temp_path, path)
        return source_sha256
    finally:
        temp_path.unlink(missing_ok=True)


def _write_json_atomic(payload: Mapping[str, object], path: Path) -> None:
    temp_path = _sibling_temp_path(path)
    try:
        temp_path.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _sibling_temp_path(path: Path, *, suffix: str = ".tmp") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=suffix
    )
    try:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0o644
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    return Path(temp_name)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alpaca_sdk_version() -> str:
    try:
        return importlib.metadata.version("alpaca-py")
    except importlib.metadata.PackageNotFoundError:
        return "not-installed-injected-client"


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values))


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
