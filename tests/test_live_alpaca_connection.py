import unittest
from pathlib import Path

from trading_ai.execution.live_connection import (
    AlpacaLiveConnectionError,
    AlpacaLiveRuntime,
    AlpacaLivePriceResult,
    build_alpaca_live_runtime,
    build_alpaca_live_client,
    build_alpaca_market_data_client,
    load_alpaca_live_credentials,
)


class FakeTradingClient:
    def __init__(self, *, api_key: str, secret_key: str, paper: bool) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper


class FakeMarketDataClient:
    def __init__(self, *, api_key: str, secret_key: str) -> None:
        self.api_key = api_key
        self.secret_key = secret_key


class FakeMarketDataResponseClient:
    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc

    def get_stock_latest_trade(self, request):
        if self.exc is not None:
            raise self.exc
        return self.response


class AlpacaLiveConnectionTests(unittest.TestCase):
    def test_credentials_loader_requires_live_key_and_secret_without_exposing_values(self) -> None:
        with self.assertRaises(AlpacaLiveConnectionError) as raised:
            load_alpaca_live_credentials({"ALPACA_LIVE_API_KEY": "live-key"})

        message = str(raised.exception)
        self.assertIn("ALPACA_LIVE_SECRET_KEY", message)
        self.assertNotIn("live-key", message)

    def test_build_alpaca_live_client_uses_live_mode_with_supplied_client_class(self) -> None:
        client = build_alpaca_live_client(
            env={
                "ALPACA_LIVE_API_KEY": "live-key",
                "ALPACA_LIVE_SECRET_KEY": "live-secret",
            },
            trading_client_cls=FakeTradingClient,
        )

        self.assertIsInstance(client, FakeTradingClient)
        self.assertEqual(client.api_key, "live-key")
        self.assertEqual(client.secret_key, "live-secret")
        self.assertFalse(client.paper)

    def test_build_market_data_client_uses_live_credentials_without_paper_flag(self) -> None:
        client = build_alpaca_market_data_client(
            env={
                "ALPACA_LIVE_API_KEY": "live-key",
                "ALPACA_LIVE_SECRET_KEY": "live-secret",
            },
            data_client_cls=FakeMarketDataClient,
        )

        self.assertIsInstance(client, FakeMarketDataClient)
        self.assertEqual(client.api_key, "live-key")
        self.assertEqual(client.secret_key, "live-secret")

    def test_build_runtime_contains_trading_and_data_clients(self) -> None:
        runtime = build_alpaca_live_runtime(
            env={
                "ALPACA_LIVE_API_KEY": "live-key",
                "ALPACA_LIVE_SECRET_KEY": "live-secret",
            },
            trading_client_cls=FakeTradingClient,
            data_client_cls=FakeMarketDataClient,
        )

        self.assertIsInstance(runtime, AlpacaLiveRuntime)
        self.assertIsInstance(runtime.trading_client, FakeTradingClient)
        self.assertIsInstance(runtime.market_data_client, FakeMarketDataClient)
        self.assertFalse(runtime.trading_client.paper)

    def test_live_price_result_maps_market_data_exception_without_exposing_secret_message(self) -> None:
        runtime = AlpacaLiveRuntime(
            trading_client=object(),
            market_data_client=FakeMarketDataResponseClient(exc=RuntimeError("token=SHOULD_NOT_APPEAR")),
        )

        result = runtime.live_price_result("SPY")

        self.assertEqual(result, AlpacaLivePriceResult(price=None, error_code="market_data_unavailable"))
        self.assertNotIn("SHOULD_NOT_APPEAR", repr(result))
        self.assertNotIn("token=", repr(result))

    def test_live_price_keeps_float_compatibility_for_valid_market_data_response(self) -> None:
        runtime = AlpacaLiveRuntime(
            trading_client=object(),
            market_data_client=FakeMarketDataResponseClient(response={"SPY": {"price": "101.25"}}),
        )

        self.assertEqual(runtime.live_price("SPY"), 101.25)

    def test_live_paper_false_boundary_is_confined_to_live_connection_source(self) -> None:
        matches = []
        for path in Path("src").rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "paper=False" in line and "confirm_paper=False" not in line:
                    matches.append(path.as_posix())

        self.assertEqual(matches, ["src/trading_ai/execution/live_connection.py"])


if __name__ == "__main__":
    unittest.main()
