"""Optional Alpaca live connection helpers.

This module only reads process environment variables supplied by the operator.
It does not read `.env` files and does not log credential values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

ALPACA_LIVE_API_KEY_ENV = "ALPACA_LIVE_API_KEY"
ALPACA_LIVE_SECRET_KEY_ENV = "ALPACA_LIVE_SECRET_KEY"  # noqa: S105


class AlpacaLiveConnectionError(RuntimeError):
    """Raised when Alpaca live connection prerequisites are missing."""


@dataclass(frozen=True)
class AlpacaLiveCredentials:
    api_key: str
    secret_key: str


@dataclass(frozen=True)
class AlpacaLivePriceResult:
    price: float | None
    error_code: str | None = None


@dataclass(frozen=True)
class AlpacaLiveRuntime:
    trading_client: Any
    market_data_client: Any
    credentials_read: bool = True

    def market_clock(self) -> Any:
        return self.trading_client.get_clock()

    def live_price(self, symbol: str) -> float | None:
        return self.live_price_result(symbol).price

    def live_price_result(self, symbol: str) -> AlpacaLivePriceResult:
        request = _build_latest_trade_request(symbol.upper())
        try:
            response = self.market_data_client.get_stock_latest_trade(request)
        except Exception:
            return AlpacaLivePriceResult(price=None, error_code="market_data_unavailable")
        price = _extract_latest_trade_price(response)
        if price is None:
            return AlpacaLivePriceResult(price=None, error_code="market_data_price_missing")
        return AlpacaLivePriceResult(price=price, error_code=None)


def load_alpaca_live_credentials(env: Mapping[str, str] | None = None) -> AlpacaLiveCredentials:
    values = os.environ if env is None else env
    api_key = values.get(ALPACA_LIVE_API_KEY_ENV, "").strip()
    secret_key = values.get(ALPACA_LIVE_SECRET_KEY_ENV, "").strip()
    missing = []
    if not api_key:
        missing.append(ALPACA_LIVE_API_KEY_ENV)
    if not secret_key:
        missing.append(ALPACA_LIVE_SECRET_KEY_ENV)
    if missing:
        raise AlpacaLiveConnectionError("missing Alpaca live credential environment variables: " + ", ".join(missing))
    return AlpacaLiveCredentials(api_key=api_key, secret_key=secret_key)


def build_alpaca_live_client(
    *,
    env: Mapping[str, str] | None = None,
    trading_client_cls: type | None = None,
):
    credentials = load_alpaca_live_credentials(env)
    client_cls = trading_client_cls
    if client_cls is None:
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise AlpacaLiveConnectionError(
                "alpaca-py is not installed; install the broker optional dependency before live access"
            ) from exc
        client_cls = TradingClient
    return client_cls(api_key=credentials.api_key, secret_key=credentials.secret_key, paper=False)


def build_alpaca_market_data_client(
    *,
    env: Mapping[str, str] | None = None,
    data_client_cls: type | None = None,
):
    credentials = load_alpaca_live_credentials(env)
    client_cls = data_client_cls
    if client_cls is None:
        try:
            from alpaca.data.historical.stock import StockHistoricalDataClient
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise AlpacaLiveConnectionError(
                "alpaca-py is not installed; install the broker optional dependency before live market data access"
            ) from exc
        client_cls = StockHistoricalDataClient
    return client_cls(api_key=credentials.api_key, secret_key=credentials.secret_key)


def build_alpaca_live_runtime(
    *,
    env: Mapping[str, str] | None = None,
    trading_client_cls: type | None = None,
    data_client_cls: type | None = None,
) -> AlpacaLiveRuntime:
    return AlpacaLiveRuntime(
        trading_client=build_alpaca_live_client(env=env, trading_client_cls=trading_client_cls),
        market_data_client=build_alpaca_market_data_client(env=env, data_client_cls=data_client_cls),
        credentials_read=True,
    )


def _build_latest_trade_request(symbol: str) -> Any:
    try:
        from alpaca.data.requests import StockLatestTradeRequest
    except ImportError:  # pragma: no cover - depends on optional package
        return SimpleNamespace(symbol_or_symbols=symbol)
    return StockLatestTradeRequest(symbol_or_symbols=symbol)


def _extract_latest_trade_price(response: Any) -> float | None:
    trade = None
    if isinstance(response, Mapping):
        trade = response.get(next(iter(response), ""), None)
    elif hasattr(response, "values"):
        trade = next(iter(response.values()), None)
    else:
        trade = response
    price = getattr(trade, "price", None)
    if price is None and isinstance(trade, Mapping):
        price = trade.get("price")
    try:
        return float(price)
    except (TypeError, ValueError):
        return None
