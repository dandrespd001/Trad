import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser
from trading_ai.data.alpaca_market_data import (
    AlpacaMarketDataError,
    fetch_daily_bars,
    run_market_data_fetch,
)
from trading_ai.data.io import read_records
from trading_ai.data.validation import validate_ohlcv_records

UNIVERSE_YAML = """
universe:
  name: test_universe
  asset_type: etf
  market: us_equities
  symbols:
    - SPY
    - QQQ
"""


class FakeBar:
    """Mimics the attribute shape of ``alpaca.data.models.bars.Bar``."""

    def __init__(self, *, symbol: str, timestamp: datetime, open: float, high: float, low: float, close: float, volume: float) -> None:  # noqa: A002
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


class FakeStockHistoricalDataClient:
    def __init__(self, bars_by_symbol: dict[str, list[FakeBar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.requests: list[Any] = []

    def get_stock_bars(self, request: Any) -> FakeBarSet:
        self.requests.append(request)
        return FakeBarSet(dict(self.bars_by_symbol))


def _write_universe(root: Path) -> Path:
    path = root / "universe.yml"
    path.write_text(UNIVERSE_YAML, encoding="utf-8")
    return path


class FetchDailyBarsTests(unittest.TestCase):
    def test_normalizes_sorts_dedupes_and_uppercases(self) -> None:
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(symbol="SPY", timestamp=datetime(2024, 1, 3), open=2, high=3, low=1, close=2, volume=10),
                    FakeBar(symbol="SPY", timestamp=datetime(2024, 1, 2), open=1, high=2, low=1, close=1, volume=5),
                ],
                "QQQ": [
                    FakeBar(symbol="QQQ", timestamp=datetime(2024, 1, 2), open=1, high=2, low=1, close=1, volume=5),
                ],
            }
        )

        records = fetch_daily_bars(
            symbols=["spy", "SPY", "qqq"],
            start="2024-01-01",
            end="2024-01-05",
            client=client,
        )

        self.assertEqual(len(client.requests), 1)
        self.assertEqual(
            records,
            [
                {"timestamp": "2024-01-02", "symbol": "QQQ", "open": 1.0, "high": 2.0, "low": 1.0, "close": 1.0, "volume": 5.0},
                {"timestamp": "2024-01-02", "symbol": "SPY", "open": 1.0, "high": 2.0, "low": 1.0, "close": 1.0, "volume": 5.0},
                {"timestamp": "2024-01-03", "symbol": "SPY", "open": 2.0, "high": 3.0, "low": 1.0, "close": 2.0, "volume": 10.0},
            ],
        )
        request = client.requests[0]
        self.assertEqual(list(request.symbol_or_symbols), ["QQQ", "SPY"])

    def test_empty_symbols_raises_value_error(self) -> None:
        client = FakeStockHistoricalDataClient({})

        with self.assertRaises(ValueError):
            fetch_daily_bars(symbols=[], start="2024-01-01", end="2024-01-05", client=client)

    def test_start_after_end_raises_value_error(self) -> None:
        client = FakeStockHistoricalDataClient({})

        with self.assertRaises(ValueError):
            fetch_daily_bars(symbols=["SPY"], start="2024-02-01", end="2024-01-01", client=client)

    def test_missing_credentials_and_no_client_raises_clear_error_without_secret_values(self) -> None:
        with self.assertRaises(AlpacaMarketDataError) as ctx:
            fetch_daily_bars(symbols=["SPY"], start="2024-01-01", end="2024-01-05", client=None, env={})

        message = str(ctx.exception)
        self.assertIn("ALPACA_PAPER_API_KEY", message)
        self.assertIn("ALPACA_PAPER_SECRET_KEY", message)
        # Only variable names may appear; no key-shaped value token is present.
        self.assertIsNone(re.search(r"[A-Za-z0-9]{24,}", message))


class RunMarketDataFetchTests(unittest.TestCase):
    def test_happy_path_writes_valid_csv_and_sidecar(self) -> None:
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(symbol="SPY", timestamp=datetime(2024, 1, 2), open=10, high=11, low=9, close=10, volume=100),
                ],
                "QQQ": [
                    FakeBar(symbol="QQQ", timestamp=datetime(2024, 1, 2), open=20, high=21, low=19, close=20, volume=200),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
                generated_at="2026-07-07T00:00:00Z",
            )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(output_path.exists())

            records = read_records(output_path)
            validation = validate_ohlcv_records(records, allowed_symbols=("SPY", "QQQ"))
            self.assertTrue(validation.valid, validation.errors)

            sidecar_path = root / "fresh_source.csv.fetch.json"
            self.assertTrue(sidecar_path.exists())
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["provider"], "alpaca_market_data")
            self.assertEqual(payload["feed"], "iex")
            self.assertEqual(payload["row_count"], 2)
            self.assertEqual(payload["per_symbol_row_counts"], {"SPY": 1, "QQQ": 1})
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

    def test_symbol_missing_bars_warns_and_writes_remaining_csv(self) -> None:
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(symbol="SPY", timestamp=datetime(2024, 1, 2), open=10, high=11, low=9, close=10, volume=100),
                ],
                "QQQ": [],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "WARN")
            self.assertEqual(result.exit_code, 0)
            self.assertIn("symbol_missing_bars:QQQ", result.payload["blockers"])
            self.assertTrue(output_path.exists())

            records = read_records(output_path)
            self.assertEqual([row["symbol"] for row in records], ["SPY"])

    def test_zero_rows_blocks_without_writing_csv(self) -> None:
        client = FakeStockHistoricalDataClient({"SPY": [], "QQQ": []})

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(output_path.exists())
            self.assertEqual(result.payload["row_count"], 0)

    def test_invalid_data_blocks_without_writing_csv(self) -> None:
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(symbol="SPY", timestamp=datetime(2024, 1, 2), open=10, high=5, low=8, close=9, volume=100),
                ],
                "QQQ": [
                    FakeBar(symbol="QQQ", timestamp=datetime(2024, 1, 2), open=20, high=21, low=19, close=20, volume=200),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(output_path.exists())
            self.assertTrue(any("high below low" in blocker for blocker in result.payload["blockers"]))


class FetchMarketDataCliParserTests(unittest.TestCase):
    def test_parser_accepts_required_flags_and_defaults(self) -> None:
        args = build_parser().parse_args(
            ["fetch-market-data", "--from", "2024-01-01", "--to", "2024-01-05"]
        )

        self.assertEqual(args.config, "configs/universe.yml")
        self.assertEqual(args.start, "2024-01-01")
        self.assertEqual(args.end, "2024-01-05")
        self.assertEqual(args.output, "data/incoming/fresh_source.csv")

    def test_parser_has_no_confirm_or_submit_flags(self) -> None:
        args = build_parser().parse_args(
            ["fetch-market-data", "--from", "2024-01-01", "--to", "2024-01-05"]
        )

        keys = vars(args).keys()
        self.assertFalse(any("confirm" in key for key in keys))
        self.assertFalse(any("submit" in key for key in keys))

    def test_parser_requires_from_and_to(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["fetch-market-data"])


if __name__ == "__main__":
    unittest.main()
