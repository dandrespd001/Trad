"""Tests for the governed Alpaca crypto market-data path (Sprint M1).

The crypto ingest path mirrors ``fetch_daily_bars`` but routes through
``CryptoHistoricalDataClient.get_crypto_bars`` and writes the sidecar with
``provider="alpaca_crypto_data"`` / ``feed="us"``. Equity paths must stay
byte-identical (regression test included).
"""

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.data.alpaca_market_data import (
    ALPACA_PAPER_API_KEY_ENV,
    ALPACA_PAPER_SECRET_KEY_ENV,
    AlpacaMarketDataError,
    fetch_crypto_daily_bars,
    run_market_data_fetch,
)
from trading_ai.data.io import read_records
from trading_ai.data.validation import validate_ohlcv_records

CRYPTO_UNIVERSE_YAML = """
universe:
  name: test_crypto_universe
  asset_type: crypto
  market: crypto
  symbols:
    - BTC/USD
    - ETH/USD
"""

TEST_API_KEY = "fake-key"  # noqa: S105 - inert test credential
TEST_SECRET_KEY = "fake-secret"  # noqa: S105 - inert test credential

EQUITY_UNIVERSE_YAML = """
universe:
  name: test_equity_universe
  asset_type: etf
  market: us_equities
  symbols:
    - SPY
    - QQQ
"""

CRYPTO_UNIVERSE_YAML_SINGLE_MISSING = """
universe:
  name: test_crypto_universe_missing
  asset_type: crypto
  market: crypto
  symbols:
    - BTC/USD
    - DOGE/USD
"""


class FakeBar:
    """Mimics the attribute shape of ``alpaca.data.models.bars.Bar``."""

    def __init__(  # noqa: A002
        self,
        *,
        symbol: str,
        timestamp: datetime,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        self.symbol = symbol
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class FakeBarSet:
    """Mimics ``alpaca.data.models.bars.BarSet`` (``.data`` keyed by symbol)."""

    def __init__(self, data: dict[str, list[FakeBar]]) -> None:
        self.data = data


class FakeCryptoHistoricalDataClient:
    def __init__(self, bars_by_symbol: dict[str, list[FakeBar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.requests: list[Any] = []

    def get_crypto_bars(self, request: Any) -> FakeBarSet:
        self.requests.append(request)
        return FakeBarSet(dict(self.bars_by_symbol))


class FakeStockHistoricalDataClient:
    def __init__(self, bars_by_symbol: dict[str, list[FakeBar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.requests: list[Any] = []

    def get_stock_bars(self, request: Any) -> FakeBarSet:
        self.requests.append(request)
        return FakeBarSet(dict(self.bars_by_symbol))


def _write_universe(root: Path, yaml_text: str) -> Path:
    path = root / "universe.yml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


class FetchCryptoDailyBarsTests(unittest.TestCase):
    def test_normalizes_sorts_dedupes_and_preserves_slash(self) -> None:
        client = FakeCryptoHistoricalDataClient(
            {
                "BTC/USD": [
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2024, 1, 3),
                        open=42_000,
                        high=43_000,
                        low=41_000,
                        close=42_500,
                        volume=100,
                    ),
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2024, 1, 2),
                        open=41_000,
                        high=42_000,
                        low=40_000,
                        close=41_500,
                        volume=50,
                    ),
                ],
                "ETH/USD": [
                    FakeBar(
                        symbol="ETH/USD",
                        timestamp=datetime(2024, 1, 2),
                        open=2_000,
                        high=2_100,
                        low=1_950,
                        close=2_050,
                        volume=25,
                    ),
                ],
            }
        )

        records = fetch_crypto_daily_bars(
            symbols=["btc/usd", "BTC/USD", "eth/usd"],
            start="2024-01-01",
            end="2024-01-05",
            client=client,
        )

        self.assertEqual(len(client.requests), 1)
        # The "/" is preserved while the letters are upper-cased and dedup applied.
        self.assertEqual(list(client.requests[0].symbol_or_symbols), ["BTC/USD", "ETH/USD"])
        self.assertEqual(
            records,
            [
                {
                    "timestamp": "2024-01-02",
                    "symbol": "BTC/USD",
                    "open": 41_000.0,
                    "high": 42_000.0,
                    "low": 40_000.0,
                    "close": 41_500.0,
                    "volume": 50.0,
                },
                {
                    "timestamp": "2024-01-02",
                    "symbol": "ETH/USD",
                    "open": 2_000.0,
                    "high": 2_100.0,
                    "low": 1_950.0,
                    "close": 2_050.0,
                    "volume": 25.0,
                },
                {
                    "timestamp": "2024-01-03",
                    "symbol": "BTC/USD",
                    "open": 42_000.0,
                    "high": 43_000.0,
                    "low": 41_000.0,
                    "close": 42_500.0,
                    "volume": 100.0,
                },
            ],
        )

    def test_empty_symbols_raises_value_error(self) -> None:
        client = FakeCryptoHistoricalDataClient({})
        with self.assertRaises(ValueError):
            fetch_crypto_daily_bars(
                symbols=[], start="2024-01-01", end="2024-01-05", client=client
            )

    def test_start_after_end_raises_value_error(self) -> None:
        client = FakeCryptoHistoricalDataClient({})
        with self.assertRaises(ValueError):
            fetch_crypto_daily_bars(
                symbols=["BTC/USD"], start="2024-02-01", end="2024-01-01", client=client
            )

    def test_request_end_covers_the_full_end_day(self) -> None:
        """Regression parity with the equity path: end at 23:59:59 covers the end day."""
        client = FakeCryptoHistoricalDataClient(
            {
                "BTC/USD": [
                    FakeBar(symbol="BTC/USD", timestamp=datetime(2026, 7, 7), open=1, high=2, low=1, close=2, volume=5),
                ],
            }
        )
        fetch_crypto_daily_bars(
            symbols=["BTC/USD"], start="2026-07-01", end="2026-07-07", client=client
        )

        request = client.requests[0]
        self.assertEqual((request.end.hour, request.end.minute, request.end.second), (23, 59, 59))
        self.assertEqual(request.end.date().isoformat(), "2026-07-07")


class RunMarketDataFetchCryptoTests(unittest.TestCase):
    def test_happy_path_writes_csv_with_provider_and_feed_us(self) -> None:
        client = FakeCryptoHistoricalDataClient(
            {
                "BTC/USD": [
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2024, 1, 5),
                        open=42_000,
                        high=43_000,
                        low=41_000,
                        close=42_500,
                        volume=100,
                    ),
                ],
                "ETH/USD": [
                    FakeBar(
                        symbol="ETH/USD",
                        timestamp=datetime(2024, 1, 5),
                        open=2_000,
                        high=2_100,
                        low=1_950,
                        close=2_050,
                        volume=25,
                    ),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root, CRYPTO_UNIVERSE_YAML)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-05",
                end="2024-01-05",
                output=output_path,
                client=client,
                generated_at="2026-07-09T00:00:00Z",
            )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(output_path.exists())

            records = read_records(output_path)
            validation = validate_ohlcv_records(records, allowed_symbols=("BTC/USD", "ETH/USD"))
            self.assertTrue(validation.valid, validation.errors)

            sidecar_path = root / "fresh_source.csv.fetch.json"
            self.assertTrue(sidecar_path.exists())
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["provider"], "alpaca_crypto_data")
            self.assertEqual(payload["feed"], "us")
            self.assertEqual(payload["schema_version"], "1.1")
            self.assertEqual(payload["row_count"], 2)
            self.assertEqual(payload["per_symbol_row_counts"], {"BTC/USD": 1, "ETH/USD": 1})
            self.assertEqual(
                payload["per_symbol_latest_dates"],
                {"BTC/USD": "2024-01-05", "ETH/USD": "2024-01-05"},
            )
            self.assertEqual(payload["expected_latest_bar_date"], "2024-01-05")
            self.assertEqual(payload["observed_start"], "2024-01-05")
            self.assertEqual(payload["observed_end"], "2024-01-05")
            self.assertTrue(payload["published"])
            self.assertEqual(
                payload["source_sha256"], hashlib.sha256(output_path.read_bytes()).hexdigest()
            )
            self.assertEqual(
                payload["safety"],
                {
                    "paper_only": True,
                    "broker_client_built": False,
                    "market_data_client_built": True,
                    "credentials_read": True,
                    "orders_submitted": False,
                    "live_trading_authorized": False,
                    "live_trading_allowed": False,
                },
            )
            self.assertEqual(payload["blockers"], [])

    def test_missing_symbol_blocks_and_does_not_publish_partial_csv(self) -> None:
        client = FakeCryptoHistoricalDataClient(
            {
                "BTC/USD": [
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2024, 1, 5),
                        open=42_000,
                        high=43_000,
                        low=41_000,
                        close=42_500,
                        volume=100,
                    ),
                ],
                "DOGE/USD": [],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root, CRYPTO_UNIVERSE_YAML_SINGLE_MISSING)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-05",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("symbol_missing_bars:DOGE/USD", result.payload["blockers"])
            self.assertFalse(output_path.exists())
            self.assertFalse(result.payload["published"])
            self.assertIsNone(result.payload["source_sha256"])

            sidecar = json.loads(
                (root / "fresh_source.csv.fetch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sidecar["status"], "BLOCKED")

    def test_partial_fetch_preserves_previous_good_csv(self) -> None:
        client = FakeCryptoHistoricalDataClient(
            {
                "BTC/USD": [
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2024, 1, 5),
                        open=42_000,
                        high=43_000,
                        low=41_000,
                        close=42_500,
                        volume=100,
                    ),
                ],
                "DOGE/USD": [],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "fresh_source.csv"
            previous = b"timestamp,symbol,open,high,low,close,volume\n2024-01-04,BTC/USD,41000,42000,40000,41500,50\n"
            output_path.write_bytes(previous)

            result = run_market_data_fetch(
                config=_write_universe(root, CRYPTO_UNIVERSE_YAML_SINGLE_MISSING),
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(output_path.read_bytes(), previous)
            self.assertFalse(result.payload["published"])

    def test_symbol_missing_expected_latest_bar_blocks(self) -> None:
        client = FakeCryptoHistoricalDataClient(
            {
                "BTC/USD": [
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2024, 1, 4),
                        open=42_000,
                        high=43_000,
                        low=41_000,
                        close=42_500,
                        volume=100,
                    ),
                ],
                "ETH/USD": [
                    FakeBar(
                        symbol="ETH/USD",
                        timestamp=datetime(2024, 1, 5),
                        open=2_000,
                        high=2_100,
                        low=1_950,
                        close=2_050,
                        volume=25,
                    ),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "fresh_source.csv"
            result = run_market_data_fetch(
                config=_write_universe(root, CRYPTO_UNIVERSE_YAML),
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(output_path.exists())
            self.assertEqual(result.payload["expected_latest_bar_date"], "2024-01-05")
            self.assertEqual(
                result.payload["per_symbol_latest_dates"],
                {"BTC/USD": "2024-01-04", "ETH/USD": "2024-01-05"},
            )
            self.assertIn(
                "symbol_missing_expected_latest_bar:BTC/USD:expected=2024-01-05:actual=2024-01-04",
                result.payload["blockers"],
            )

    def test_bar_outside_requested_range_blocks(self) -> None:
        client = FakeCryptoHistoricalDataClient(
            {
                "BTC/USD": [
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2023, 12, 31),
                        open=41_000,
                        high=42_000,
                        low=40_000,
                        close=41_500,
                        volume=50,
                    ),
                    FakeBar(
                        symbol="BTC/USD",
                        timestamp=datetime(2024, 1, 5),
                        open=42_000,
                        high=43_000,
                        low=41_000,
                        close=42_500,
                        volume=100,
                    ),
                ],
                "ETH/USD": [
                    FakeBar(
                        symbol="ETH/USD",
                        timestamp=datetime(2024, 1, 5),
                        open=2_000,
                        high=2_100,
                        low=1_950,
                        close=2_050,
                        volume=25,
                    ),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "fresh_source.csv"
            result = run_market_data_fetch(
                config=_write_universe(root, CRYPTO_UNIVERSE_YAML),
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(output_path.exists())
            self.assertIn(
                "bar_outside_requested_range:BTC/USD:2023-12-31",
                result.payload["blockers"],
            )


class RunMarketDataFetchEquityRegressionTests(unittest.TestCase):
    def test_equity_universe_keeps_provider_and_feed_iex(self) -> None:
        """Regression: the equity path must remain byte-identical (provider/feed)."""
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(
                        symbol="SPY",
                        timestamp=datetime(2024, 1, 5),
                        open=10,
                        high=11,
                        low=9,
                        close=10,
                        volume=100,
                    ),
                ],
                "QQQ": [
                    FakeBar(
                        symbol="QQQ",
                        timestamp=datetime(2024, 1, 5),
                        open=20,
                        high=21,
                        low=19,
                        close=20,
                        volume=200,
                    ),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root, EQUITY_UNIVERSE_YAML)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-05",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(output_path.exists())

            sidecar_path = root / "fresh_source.csv.fetch.json"
            self.assertTrue(sidecar_path.exists())
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["provider"], "alpaca_market_data")
            self.assertEqual(payload["feed"], "iex")


class BuildAlpacaCryptoDataClientTests(unittest.TestCase):
    def test_no_credentials_does_not_raise_alpaca_market_data_error(self) -> None:
        """The crypto endpoint is public; absent credentials are not an error.

        We stub the alpaca module into ``sys.modules`` so the test stays offline
        (no real package import) and we can observe the construction path.
        """
        from types import ModuleType, SimpleNamespace

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

        stub_modules = {
            "alpaca": alpaca_pkg,
            "alpaca.data": data_pkg,
            "alpaca.data.historical": historical_pkg,
            "alpaca.data.historical.crypto": crypto_module,
        }

        with mock.patch.dict(sys.modules, stub_modules):
            from trading_ai.data import alpaca_market_data

            client = alpaca_market_data.build_alpaca_crypto_data_client(env={})

        self.assertEqual(client.kind, "crypto")
        # No credentials forwarded when env is empty.
        self.assertEqual(captured["kwargs"], {})

    def test_present_credentials_are_forwarded_to_constructor(self) -> None:
        from types import ModuleType, SimpleNamespace

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

        stub_modules = {
            "alpaca": alpaca_pkg,
            "alpaca.data": data_pkg,
            "alpaca.data.historical": historical_pkg,
            "alpaca.data.historical.crypto": crypto_module,
        }

        env = {
            ALPACA_PAPER_API_KEY_ENV: "fake-key",
            ALPACA_PAPER_SECRET_KEY_ENV: "fake-secret",
        }

        with mock.patch.dict(sys.modules, stub_modules):
            from trading_ai.data import alpaca_market_data

            alpaca_market_data.build_alpaca_crypto_data_client(env=env)

        self.assertEqual(
            captured["kwargs"],
            {"api_key": "fake-key", "secret_key": "fake-secret"},
        )

    def test_import_error_when_alpaca_not_installed(self) -> None:
        # Remove any stubbed alpaca modules so the real ``ImportError`` is raised.
        blocked_modules = {
            key: None
            for key in list(sys.modules)
            if key == "alpaca" or key.startswith("alpaca.")
        }
        with (
            mock.patch.dict(sys.modules, blocked_modules),
            self.assertRaises(AlpacaMarketDataError) as ctx,
        ):
            from trading_ai.data import alpaca_market_data

            alpaca_market_data.build_alpaca_crypto_data_client(env={})

        self.assertIn("alpaca-py is not installed", str(ctx.exception))

    def test_real_daily_data_builders_apply_timeout_and_zero_retry(self) -> None:
        try:
            from requests import ConnectTimeout, ReadTimeout

            from trading_ai.execution import paper_account_executor as executor_module
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        from trading_ai.data import alpaca_market_data

        env = {
            ALPACA_PAPER_API_KEY_ENV: "fake-key",
            ALPACA_PAPER_SECRET_KEY_ENV: "fake-secret",
        }
        builders = (
            alpaca_market_data.build_alpaca_market_data_client,
            alpaca_market_data.build_alpaca_crypto_data_client,
        )
        for builder in builders:
            for error_type in (ConnectTimeout, ReadTimeout):
                with self.subTest(builder=builder.__name__, error=error_type.__name__), mock.patch(
                    "requests.sessions.Session.request",
                    side_effect=error_type("inert timeout"),
                ) as request:
                    client = builder(env=env)
                    with self.assertRaises(error_type):
                        client.get("/inert-bars")

                request.assert_called_once()
                self.assertEqual(
                    request.call_args.kwargs["timeout"],
                    executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
                )
                self.assertEqual(client._retry, 0)  # noqa: SLF001
                self.assertEqual(client._retry_codes, [])  # noqa: SLF001

    def test_injected_real_daily_data_clients_cannot_bypass_transport_guard(self) -> None:
        try:
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
            from alpaca.data.historical.stock import StockHistoricalDataClient
            from requests import ConnectTimeout

            from trading_ai.execution import paper_account_executor as executor_module
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        from trading_ai.data import alpaca_market_data

        cases = (
            (
                StockHistoricalDataClient(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                ),
                alpaca_market_data.fetch_daily_bars,
                {"symbols": ("SPY",)},
            ),
            (
                CryptoHistoricalDataClient(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                ),
                alpaca_market_data.fetch_crypto_daily_bars,
                {"symbols": ("BTC/USD",)},
            ),
        )
        for client, fetcher, kwargs in cases:
            with self.subTest(fetcher=fetcher.__name__), mock.patch(
                "requests.sessions.Session.request",
                side_effect=ConnectTimeout("inert timeout"),
            ) as request, self.assertRaises(ConnectTimeout):
                fetcher(
                    start="2026-07-14",
                    end="2026-07-15",
                    client=client,
                    **kwargs,
                )

            request.assert_called_once()
            self.assertEqual(
                request.call_args.kwargs["timeout"],
                executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
            )
            self.assertEqual(client._retry, 0)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
