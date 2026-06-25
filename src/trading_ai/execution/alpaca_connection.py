"""Optional Alpaca paper connection helpers.

This module only reads process environment variables supplied by the operator.
It does not read `.env` files and does not log credential values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ALPACA_PAPER_API_KEY_ENV = "ALPACA_PAPER_API_KEY"
ALPACA_PAPER_SECRET_KEY_ENV = "ALPACA_PAPER_SECRET_KEY"  # noqa: S105


class AlpacaPaperConnectionError(RuntimeError):
    """Raised when Alpaca paper connection prerequisites are missing."""


@dataclass(frozen=True)
class AlpacaPaperCredentials:
    api_key: str
    secret_key: str


def load_alpaca_paper_credentials(env: Mapping[str, str] | None = None) -> AlpacaPaperCredentials:
    values = os.environ if env is None else env
    api_key = values.get(ALPACA_PAPER_API_KEY_ENV, "").strip()
    secret_key = values.get(ALPACA_PAPER_SECRET_KEY_ENV, "").strip()
    missing = []
    if not api_key:
        missing.append(ALPACA_PAPER_API_KEY_ENV)
    if not secret_key:
        missing.append(ALPACA_PAPER_SECRET_KEY_ENV)
    if missing:
        raise AlpacaPaperConnectionError("missing Alpaca paper credential environment variables: " + ", ".join(missing))
    return AlpacaPaperCredentials(api_key=api_key, secret_key=secret_key)


def build_alpaca_paper_client(
    *,
    env: Mapping[str, str] | None = None,
    trading_client_cls: type | None = None,
):
    credentials = load_alpaca_paper_credentials(env)
    client_cls = trading_client_cls
    if client_cls is None:
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise AlpacaPaperConnectionError(
                "alpaca-py is not installed; install the broker optional dependency before real paper access"
            ) from exc
        client_cls = TradingClient
    return client_cls(api_key=credentials.api_key, secret_key=credentials.secret_key, paper=True)


def build_alpaca_market_data_client(
    *,
    env: Mapping[str, str] | None = None,
    client_cls: type | None = None,
):
    credentials = load_alpaca_paper_credentials(env)
    resolved_cls = client_cls
    if resolved_cls is None:
        try:
            from alpaca.data.historical.stock import StockHistoricalDataClient
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise AlpacaPaperConnectionError(
                "alpaca-py is not installed; install the broker optional dependency before real paper access"
            ) from exc
        resolved_cls = StockHistoricalDataClient
    return resolved_cls(api_key=credentials.api_key, secret_key=credentials.secret_key)
