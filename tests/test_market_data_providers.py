import unittest

from trading_ai.data.market_data import InMemoryMarketDataProvider, MarketDataRequest


class MarketDataProviderTests(unittest.TestCase):
    def test_fake_provider_filters_symbols_and_dates_without_network(self) -> None:
        provider = InMemoryMarketDataProvider(
            [
                {"timestamp": "2026-06-15", "symbol": "SPY", "close": 100.0},
                {"timestamp": "2026-06-16", "symbol": "SPY", "close": 101.0},
                {"timestamp": "2026-06-16", "symbol": "QQQ", "close": 201.0},
            ]
        )

        rows = provider.load(MarketDataRequest(symbols=("SPY",), start="2026-06-16", end="2026-06-16"))

        self.assertEqual(rows, [{"timestamp": "2026-06-16", "symbol": "SPY", "close": 101.0}])
        self.assertFalse(provider.network_used)


if __name__ == "__main__":
    unittest.main()
