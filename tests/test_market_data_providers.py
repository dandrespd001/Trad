import tempfile
import unittest
from pathlib import Path

from trading_ai.data.market_data import ApprovedLocalMarketDataProvider, InMemoryMarketDataProvider, MarketDataRequest


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

    def test_approved_local_provider_rejects_invalid_timestamp_for_requested_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.csv"
            source.write_text(
                "timestamp,symbol,open,high,low,close,volume\nnot-a-date,SPY,1,1,1,1,100\n",
                encoding="utf-8",
            )

            provider = ApprovedLocalMarketDataProvider(source)

            with self.assertRaisesRegex(ValueError, "row 0 invalid timestamp: not-a-date"):
                provider.load(MarketDataRequest(symbols=("SPY",), start="2026-06-01", end="2026-06-16"))


if __name__ == "__main__":
    unittest.main()
