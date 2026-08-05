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
    from trading_ai.execution.paper_account_executor import (  # noqa: PLC0415
        PaperAccountAuthorityError,
        build_supervised_alpaca_paper_client,
    )

    try:
        return build_supervised_alpaca_paper_client(
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
            trading_client_cls=trading_client_cls,
        )
    except PaperAccountAuthorityError as exc:
        raise AlpacaPaperConnectionError(str(exc)) from exc


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
    client = resolved_cls(api_key=credentials.api_key, secret_key=credentials.secret_key)
    return _enforce_audited_market_data_transport(client)


def build_alpaca_crypto_market_data_client(
    *,
    env: Mapping[str, str] | None = None,
    client_cls: type | None = None,
):
    """Build a read-only crypto market-data client (used by the price-sanity gate).

    Mirrors :func:`build_alpaca_market_data_client` but for the Alpaca crypto
    market-data endpoint. Unlike the equities endpoint, the crypto endpoint is
    public, so credentials are OPTIONAL: when both ``ALPACA_PAPER_API_KEY`` and
    ``ALPACA_PAPER_SECRET_KEY`` are present and non-empty they are forwarded to
    the client (matching the stock-path semantics); otherwise the client is
    constructed without arguments — no :class:`AlpacaPaperConnectionError` is
    raised.
    """
    resolved_cls = client_cls
    if resolved_cls is None:
        try:
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise AlpacaPaperConnectionError(
                "alpaca-py is not installed; install the broker optional dependency before real paper access"
            ) from exc
        resolved_cls = CryptoHistoricalDataClient

    values = os.environ if env is None else env
    api_key = str(values.get(ALPACA_PAPER_API_KEY_ENV, "")).strip()
    secret_key = str(values.get(ALPACA_PAPER_SECRET_KEY_ENV, "")).strip()
    client = (
        resolved_cls(api_key=api_key, secret_key=secret_key)
        if api_key and secret_key
        else resolved_cls()
    )
    return _enforce_audited_market_data_transport(client)


def _enforce_audited_market_data_transport(client):
    """Harden real Alpaca SDK data clients while leaving test doubles portable."""

    from trading_ai.execution.paper_account_executor import (  # noqa: PLC0415
        PaperAccountAuthorityError,
        enforce_audited_alpaca_http_transport,
    )

    try:
        return enforce_audited_alpaca_http_transport(client)
    except PaperAccountAuthorityError as exc:
        raise AlpacaPaperConnectionError(str(exc)) from exc
