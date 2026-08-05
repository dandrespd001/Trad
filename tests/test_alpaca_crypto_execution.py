"""Tests for the crypto-aware paper execution path (Sprint M2).

These tests verify that ``AlpacaPaperBroker`` honours the Alpaca crypto
contracts end-to-end (24/7 trading-day bypass, $10 minimum notional, routing
the price-sanity gate to the crypto market-data client, GTC time-in-force)
without changing the byte-identical equity path.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

from trading_ai.execution.alpaca_connection import (
    ALPACA_PAPER_API_KEY_ENV,
    ALPACA_PAPER_SECRET_KEY_ENV,
    build_alpaca_crypto_market_data_client,
)
from trading_ai.execution.alpaca_paper import (
    CRYPTO_MIN_NOTIONAL_USD,
    AlpacaPaperBroker,
    PaperOrder,
    is_crypto_symbol,
)
from trading_ai.risk.policy import RiskLimits


class _NotFoundError(RuntimeError):
    status_code = 404


class FakeAlpacaPaperTradingClient:
    """Duck-typed trading client that captures the submitted payload."""

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.orders_by_client_id: dict[str, dict[str, Any]] = {}
        self.lookup_calls = 0

    def submit_order(self, **kwargs: Any) -> dict[str, Any]:
        self.orders.append(kwargs)
        response = {"id": f"broker-{len(self.orders)}", "status": "accepted", **kwargs}
        self.orders_by_client_id[str(kwargs["client_order_id"])] = response
        return response

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any]:
        self.lookup_calls += 1
        try:
            return self.orders_by_client_id[client_order_id]
        except KeyError as exc:
            raise _NotFoundError("order not found") from exc


class FakeAlpacaPyOrderRequestClient:
    """Trading client that expects an alpaca-py MarketOrderRequest object (not kwargs)."""

    def __init__(self) -> None:
        self.orders: list[Any] = []

    def submit_order(self, order_data: Any) -> dict[str, Any]:
        self.orders.append(order_data)
        return {"id": f"broker-{len(self.orders)}", "order_data": order_data}


class FakeStockLatestTradeClient:
    """Stock latest-trade client that returns a fixed price for any symbol."""

    def __init__(self, *, price: float) -> None:
        self.price = price
        self.requests: list[Any] = []

    def get_stock_latest_trade(self, request: Any) -> dict[str, Any]:
        self.requests.append(request)

        class Trade:
            pass

        trade = Trade()
        trade.price = self.price  # type: ignore[attr-defined]
        symbol = getattr(request, "symbol_or_symbols", "SPY")
        if isinstance(symbol, list):
            symbol = symbol[0]
        return {symbol: trade}


class FakeCryptoLatestTradeClient:
    """Crypto latest-trade client mirroring ``alpaca.data``'s public endpoint."""

    def __init__(self, *, price_by_symbol: dict[str, float]) -> None:
        self._price_by_symbol = dict(price_by_symbol)
        self.requests: list[Any] = []

    def get_crypto_latest_trade(self, request: Any) -> dict[str, Any]:
        self.requests.append(request)
        symbol = getattr(request, "symbol_or_symbols", "BTC/USD")
        if isinstance(symbol, list):
            symbol = symbol[0]

        class Trade:
            pass

        trade = Trade()
        trade.price = self._price_by_symbol.get(symbol, 0.0)  # type: ignore[attr-defined]
        return {symbol: trade}


class IsCryptoSymbolUnitTests(unittest.TestCase):
    def test_slash_marks_crypto_symbols(self) -> None:
        # Spec idiom: ``"/" in symbol`` is the single, deterministic detector.
        self.assertTrue(is_crypto_symbol("BTC/USD"))
        self.assertTrue(is_crypto_symbol("ETH/USD"))
        self.assertFalse(is_crypto_symbol("SPY"))
        self.assertFalse(is_crypto_symbol(""))
        self.assertTrue(is_crypto_symbol("/"))  # "/" in "/" → True, by spec

    def test_crypto_min_notional_constant_is_alpaca_default(self) -> None:
        self.assertEqual(CRYPTO_MIN_NOTIONAL_USD, 10.0)


class CryptoTwentyFourSevenTests(unittest.TestCase):
    def test_saturday_buy_of_equity_is_blocked(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2026, 7, 11),  # Saturday
        )

        result = broker.submit_order(
            PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-sat-equity")
        )

        self.assertFalse(result.accepted)
        self.assertIn("market_closed_not_a_trading_day", result.reasons)

    def test_saturday_buy_of_crypto_is_accepted_in_dry_run(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2026, 7, 11),  # Saturday — crypto trades 24/7
        )

        result = broker.submit_order(
            PaperOrder(
                symbol="BTC/USD",
                side="buy",
                notional=20.0,
                client_order_id="o-sat-crypto",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "dry_run_accepted")


class CryptoMinimumNotionalTests(unittest.TestCase):
    def test_below_minimum_notional_is_rejected_for_crypto(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2024, 4, 1),
        )

        result = broker.submit_order(
            PaperOrder(
                symbol="BTC/USD",
                side="buy",
                notional=5.0,
                client_order_id="o-below-min",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("crypto_notional_below_minimum",))

    def test_at_or_above_minimum_notional_is_accepted_for_crypto(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2024, 4, 1),
        )

        result = broker.submit_order(
            PaperOrder(
                symbol="BTC/USD",
                side="buy",
                notional=CRYPTO_MIN_NOTIONAL_USD,
                client_order_id="o-at-min",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "dry_run_accepted")

    def test_equity_small_notional_is_not_blocked_by_crypto_rule_regression(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2024, 4, 1),
        )

        result = broker.submit_order(
            PaperOrder(symbol="SPY", side="buy", notional=5.0, client_order_id="o-equity-small")
        )

        self.assertTrue(result.accepted)
        self.assertNotIn("crypto_notional_below_minimum", result.reasons)


class CryptoPriceSanityRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.order_journal_path = Path(self._temp_dir.name) / "orders.sqlite3"

    def test_buy_rejected_when_crypto_market_data_client_is_missing(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaPaperTradingClient(),
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),
            market_data=FakeStockLatestTradeClient(price=100.0),
            crypto_market_data=None,
            order_journal_path=self.order_journal_path,
        )

        result = broker.submit_order(
            PaperOrder(
                symbol="BTC/USD",
                side="buy",
                notional=50.0,
                client_order_id="o-no-crypto-md",
                reference_price=100.0,
            )
        )

        self.assertFalse(result.accepted)
        self.assertIn("market_data_unavailable", result.reasons)

    def test_buy_routed_to_crypto_client_when_provided(self) -> None:
        stock_client = FakeStockLatestTradeClient(price=100.0)
        crypto_client = FakeCryptoLatestTradeClient(price_by_symbol={"BTC/USD": 100.0})
        trading_client = FakeAlpacaPaperTradingClient()

        broker = AlpacaPaperBroker(
            client=trading_client,
            allowlist=("BTC/USD",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),
            market_data=stock_client,
            crypto_market_data=crypto_client,
            order_journal_path=self.order_journal_path,
        )

        result = broker.submit_order(
            PaperOrder(
                symbol="BTC/USD",
                side="buy",
                notional=50.0,
                client_order_id="o-crypto-md",
                reference_price=100.0,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(crypto_client.requests), 1)
        # Stock client must NOT have been called when the symbol is crypto.
        self.assertEqual(stock_client.requests, [])
        self.assertEqual(len(trading_client.orders), 1)
        self.assertEqual(trading_client.lookup_calls, 1)

    def test_buy_routed_to_stock_client_for_equity_regression(self) -> None:
        stock_client = FakeStockLatestTradeClient(price=100.0)
        crypto_client = FakeCryptoLatestTradeClient(price_by_symbol={"BTC/USD": 100.0})
        trading_client = FakeAlpacaPaperTradingClient()

        broker = AlpacaPaperBroker(
            client=trading_client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),
            market_data=stock_client,
            crypto_market_data=crypto_client,
            order_journal_path=self.order_journal_path,
        )

        result = broker.submit_order(
            PaperOrder(
                symbol="SPY",
                side="buy",
                notional=50.0,
                client_order_id="o-equity-md",
                reference_price=100.0,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(stock_client.requests), 1)
        # Crypto client must NOT have been called for an equity symbol.
        self.assertEqual(crypto_client.requests, [])
        self.assertEqual(len(trading_client.orders), 1)
        self.assertEqual(trading_client.lookup_calls, 1)


class TimeInForceRoutingTests(unittest.TestCase):
    def test_crypto_payload_uses_gtc(self) -> None:
        trading_client = FakeAlpacaPaperTradingClient()
        # Inspect the payload construction helper directly to keep the test offline.
        from trading_ai.execution.alpaca_paper import _submit_market_order

        _submit_market_order(
            trading_client,
            symbol="BTC/USD",
            order=PaperOrder(
                symbol="BTC/USD",
                side="buy",
                notional=50.0,
                client_order_id="o-tif-crypto",
            ),
        )

        self.assertEqual(len(trading_client.orders), 1)
        self.assertEqual(trading_client.orders[0]["time_in_force"], "gtc")
        self.assertEqual(trading_client.orders[0]["symbol"], "BTC/USD")

    def test_equity_payload_uses_day(self) -> None:
        trading_client = FakeAlpacaPaperTradingClient()
        from trading_ai.execution.alpaca_paper import _submit_market_order

        _submit_market_order(
            trading_client,
            symbol="SPY",
            order=PaperOrder(
                symbol="SPY",
                side="buy",
                notional=50.0,
                client_order_id="o-tif-equity",
            ),
        )

        self.assertEqual(len(trading_client.orders), 1)
        self.assertEqual(trading_client.orders[0]["time_in_force"], "day")
        self.assertEqual(trading_client.orders[0]["symbol"], "SPY")

    def test_alpaca_py_request_object_maps_crypto_tif_to_gtc(self) -> None:
        from trading_ai.execution.alpaca_paper import _build_alpaca_order_request

        try:
            from alpaca.trading.enums import TimeInForce  # type: ignore[import-not-found]
        except ImportError:
            self.skipTest("alpaca-py is not installed")

        payload = {
            "symbol": "BTC/USD",
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
            "client_order_id": "o-alpaca-py-gtc",
            "notional": 50,
        }

        request = _build_alpaca_order_request(payload)

        self.assertEqual(request.time_in_force, TimeInForce.GTC)
        self.assertEqual(request.symbol, "BTC/USD")

    def test_alpaca_py_request_object_maps_equity_tif_to_day(self) -> None:
        from trading_ai.execution.alpaca_paper import _build_alpaca_order_request

        try:
            from alpaca.trading.enums import TimeInForce  # type: ignore[import-not-found]
        except ImportError:
            self.skipTest("alpaca-py is not installed")

        payload = {
            "symbol": "SPY",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": "o-alpaca-py-day",
            "notional": 50,
        }

        request = _build_alpaca_order_request(payload)

        self.assertEqual(request.time_in_force, TimeInForce.DAY)


class BuildAlpacaCryptoMarketDataClientTests(unittest.TestCase):
    def test_empty_env_lets_client_construct_without_arguments(self) -> None:
        captured: dict[str, Any] = {}

        def _factory(*args: Any, **kwargs: Any) -> SimpleNamespace:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(kind="crypto", args=args, kwargs=kwargs)

        crypto_module = ModuleType("alpaca.data.historical.crypto")
        crypto_module.CryptoHistoricalDataClient = _factory  # type: ignore[attr-defined]
        historical_pkg = ModuleType("alpaca.data.historical")
        historical_pkg.crypto = crypto_module  # type: ignore[attr-defined]
        data_pkg = ModuleType("alpaca.data")
        data_pkg.historical = historical_pkg  # type: ignore[attr-defined]
        alpaca_pkg = ModuleType("alpaca")
        alpaca_pkg.data = data_pkg  # type: ignore[attr-defined]

        with mock.patch.dict(
            sys.modules,
            {
                "alpaca": alpaca_pkg,
                "alpaca.data": data_pkg,
                "alpaca.data.historical": historical_pkg,
                "alpaca.data.historical.crypto": crypto_module,
            },
        ):
            client = build_alpaca_crypto_market_data_client(env={})

        self.assertEqual(client.kind, "crypto")
        self.assertEqual(captured["args"], ())
        self.assertEqual(captured["kwargs"], {})

    def test_present_credentials_are_forwarded_to_constructor(self) -> None:
        captured: dict[str, Any] = {}

        def _factory(*args: Any, **kwargs: Any) -> SimpleNamespace:
            captured["kwargs"] = kwargs
            return SimpleNamespace(kind="crypto", kwargs=kwargs)

        crypto_module = ModuleType("alpaca.data.historical.crypto")
        crypto_module.CryptoHistoricalDataClient = _factory  # type: ignore[attr-defined]
        historical_pkg = ModuleType("alpaca.data.historical")
        historical_pkg.crypto = crypto_module  # type: ignore[attr-defined]
        data_pkg = ModuleType("alpaca.data")
        data_pkg.historical = historical_pkg  # type: ignore[attr-defined]
        alpaca_pkg = ModuleType("alpaca")
        alpaca_pkg.data = data_pkg  # type: ignore[attr-defined]

        env = {
            ALPACA_PAPER_API_KEY_ENV: "fake-key",
            ALPACA_PAPER_SECRET_KEY_ENV: "fake-secret",
        }

        with mock.patch.dict(
            sys.modules,
            {
                "alpaca": alpaca_pkg,
                "alpaca.data": data_pkg,
                "alpaca.data.historical": historical_pkg,
                "alpaca.data.historical.crypto": crypto_module,
            },
        ):
            build_alpaca_crypto_market_data_client(env=env)

        self.assertEqual(
            captured["kwargs"],
            {"api_key": "fake-key", "secret_key": "fake-secret"},
        )


if __name__ == "__main__":
    unittest.main()
