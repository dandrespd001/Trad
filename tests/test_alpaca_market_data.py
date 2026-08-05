import hashlib
import json
import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.cli import build_parser
from trading_ai.data.alpaca_market_data import (
    AlpacaMarketDataError,
    _bar_date,
    fetch_daily_bars,
    run_market_data_fetch,
)
from trading_ai.data.io import read_records
from trading_ai.data.market_calendar import (
    XNYS_CALENDAR_CONTRACT_VERSION,
    XNYS_CALENDAR_SHA256,
)
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

    def __init__(
        self,
        *,
        symbol: str,
        timestamp: datetime,
        open: float,  # noqa: A002
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
                {
                    "timestamp": "2024-01-02",
                    "symbol": "QQQ",
                    "open": 1.0,
                    "high": 2.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 5.0,
                },
                {
                    "timestamp": "2024-01-02",
                    "symbol": "SPY",
                    "open": 1.0,
                    "high": 2.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 5.0,
                },
                {
                    "timestamp": "2024-01-03",
                    "symbol": "SPY",
                    "open": 2.0,
                    "high": 3.0,
                    "low": 1.0,
                    "close": 2.0,
                    "volume": 10.0,
                },
            ],
        )
        request = client.requests[0]
        self.assertEqual(list(request.symbol_or_symbols), ["QQQ", "SPY"])
        self.assertEqual(
            getattr(request.adjustment, "value", request.adjustment),
            "all",
        )
        self.assertEqual(getattr(request.feed, "value", request.feed), "iex")

    def test_empty_symbols_raises_value_error(self) -> None:
        client = FakeStockHistoricalDataClient({})

        with self.assertRaises(ValueError):
            fetch_daily_bars(symbols=[], start="2024-01-01", end="2024-01-05", client=client)

    def test_start_after_end_raises_value_error(self) -> None:
        client = FakeStockHistoricalDataClient({})

        with self.assertRaises(ValueError):
            fetch_daily_bars(symbols=["SPY"], start="2024-02-01", end="2024-01-01", client=client)

    def test_governed_timestamp_contract_rejects_naive_non_utc_and_malformed(self) -> None:
        invalid_values = (
            datetime(2024, 1, 2),
            datetime(2024, 1, 2, tzinfo=timezone(timedelta(hours=-5))),
            "not-a-timestamp",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(AlpacaMarketDataError):
                _bar_date(value, require_utc=True)

        self.assertEqual(
            _bar_date(datetime(2024, 1, 2, tzinfo=UTC), require_utc=True),
            "2024-01-02",
        )

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
            universe_path = _write_universe(root)
            output_path = root / "fresh_source.csv"

            result = run_market_data_fetch(
                config=universe_path,
                start="2024-01-05",
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
            self.assertEqual(payload["schema_version"], "1.1")
            self.assertEqual(payload["provenance_contract_version"], "iex-1.0")
            self.assertEqual(payload["frequency"], "1d")
            self.assertEqual(payload["adjustment_policy"], "all")
            self.assertEqual(
                payload["corporate_action_policy"],
                "alpaca_adjustment_all",
            )
            self.assertEqual(payload["request_timezone"], "UTC")
            self.assertEqual(payload["bar_timestamp_timezone"], "UTC")
            self.assertEqual(
                payload["timestamp_semantics"],
                "XNYS_exchange_session_date",
            )
            self.assertEqual(payload["exchange_calendar"], "XNYS")
            self.assertEqual(
                payload["calendar_contract_version"],
                XNYS_CALENDAR_CONTRACT_VERSION,
            )
            self.assertEqual(
                payload["calendar_sha256"],
                XNYS_CALENDAR_SHA256,
            )
            self.assertRegex(
                payload["calendar_implementation_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertEqual(payload["closed_session_watermark"], "2026-07-06")
            self.assertEqual(payload["sdk_package"], "alpaca-py")
            self.assertTrue(payload["sdk_version"])
            self.assertFalse(payload["attestation_eligible"])
            self.assertTrue(payload["client_injected"])
            self.assertTrue(payload["clock_injected"])
            self.assertRegex(payload["universe_config_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(payload["normalization_code_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(payload["row_count"], 2)
            self.assertEqual(payload["per_symbol_row_counts"], {"SPY": 1, "QQQ": 1})
            self.assertEqual(payload["per_symbol_latest_dates"], {"SPY": "2024-01-05", "QQQ": "2024-01-05"})
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

    def test_unverified_or_open_session_window_fails_before_client_use(self) -> None:
        cases = (
            (
                "outside_snapshot",
                "2029-01-02",
                "2029-01-02",
                "2029-01-03T00:00:00Z",
                "outside the governed XNYS calendar snapshot",
            ),
            (
                "session_not_closed",
                "2026-07-29",
                "2026-07-29",
                "2026-07-29T19:59:00Z",
                "exceeds closed XNYS session watermark 2026-07-28",
            ),
            (
                "naive_generated_at",
                "2024-01-05",
                "2024-01-05",
                "2026-07-07T00:00:00",
                "generated_at must include a timezone offset",
            ),
        )

        for name, start, end, generated_at, expected_error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                universe_path = _write_universe(root)
                output_path = root / "fresh_source.csv"
                client = FakeStockHistoricalDataClient({})

                with self.assertRaisesRegex(
                    AlpacaMarketDataError,
                    expected_error,
                ):
                    run_market_data_fetch(
                        config=universe_path,
                        start=start,
                        end=end,
                        output=output_path,
                        client=client,
                        generated_at=generated_at,
                    )

                self.assertEqual(client.requests, [])
                self.assertFalse(output_path.exists())
                self.assertFalse(Path(f"{output_path}.fetch.json").exists())

    def test_injected_clock_is_not_eligible_even_without_injected_client(self) -> None:
        records = [
            {
                "timestamp": "2024-01-05",
                "symbol": symbol,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.0,
                "volume": 100.0,
            }
            for symbol in ("SPY", "QQQ")
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                mock.patch(
                    "trading_ai.data.alpaca_market_data.fetch_daily_bars",
                    return_value=records,
                ),
                mock.patch(
                    "trading_ai.data.alpaca_market_data._alpaca_sdk_version",
                    return_value="0.43.4",
                ),
            ):
                result = run_market_data_fetch(
                    config=_write_universe(root),
                    start="2024-01-05",
                    end="2024-01-05",
                    output=root / "fresh_source.csv",
                    generated_at="2024-01-05T21:00:00Z",
                )

        self.assertEqual(result.status, "OK")
        self.assertFalse(result.payload["client_injected"])
        self.assertTrue(result.payload["clock_injected"])
        self.assertFalse(result.payload["attestation_eligible"])

    def test_symbol_missing_bars_blocks_and_does_not_publish_partial_csv(self) -> None:
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

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("symbol_missing_bars:QQQ", result.payload["blockers"])
            self.assertFalse(output_path.exists())
            self.assertFalse(result.payload["published"])
            self.assertIsNone(result.payload["source_sha256"])

            sidecar = json.loads(
                (root / "fresh_source.csv.fetch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sidecar["status"], "BLOCKED")

    def test_partial_fetch_preserves_previous_good_csv(self) -> None:
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
                "QQQ": [],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_path = _write_universe(root)
            output_path = root / "fresh_source.csv"
            previous = b"timestamp,symbol,open,high,low,close,volume\n2024-01-04,SPY,9,10,8,9,50\n"
            output_path.write_bytes(previous)

            result = run_market_data_fetch(
                config=universe_path,
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
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(
                        symbol="SPY",
                        timestamp=datetime(2024, 1, 4),
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
            output_path = root / "fresh_source.csv"
            result = run_market_data_fetch(
                config=_write_universe(root),
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
                {"SPY": "2024-01-04", "QQQ": "2024-01-05"},
            )
            self.assertIn(
                "symbol_missing_expected_latest_bar:SPY:expected=2024-01-05:actual=2024-01-04",
                result.payload["blockers"],
            )

    def test_interior_session_gap_blocks_even_when_latest_bar_is_present(self) -> None:
        bars = {
            symbol: [
                FakeBar(
                    symbol=symbol,
                    timestamp=datetime(2024, 1, day),
                    open=10,
                    high=11,
                    low=9,
                    close=10,
                    volume=100,
                )
                for day in (2, 5)
            ]
            for symbol in ("SPY", "QQQ")
        }
        client = FakeStockHistoricalDataClient(bars)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = run_market_data_fetch(
                config=_write_universe(root),
                start="2024-01-02",
                end="2024-01-05",
                output=root / "fresh_source.csv",
                client=client,
            )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn(
            "symbol_session_set_mismatch:SPY:missing=2:unexpected=0",
            result.payload["blockers"],
        )

    def test_bar_outside_requested_range_blocks(self) -> None:
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(
                        symbol="SPY",
                        timestamp=datetime(2023, 12, 29),
                        open=9,
                        high=10,
                        low=8,
                        close=9,
                        volume=50,
                    ),
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
            output_path = root / "fresh_source.csv"
            result = run_market_data_fetch(
                config=_write_universe(root),
                start="2024-01-01",
                end="2024-01-05",
                output=output_path,
                client=client,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(output_path.exists())
            self.assertIn(
                "bar_outside_requested_range:SPY:2023-12-29", result.payload["blockers"]
            )

    def test_weekend_end_uses_previous_nyse_session(self) -> None:
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
            result = run_market_data_fetch(
                config=_write_universe(root),
                start="2024-01-05",
                end="2024-01-07",
                output=root / "fresh_source.csv",
                client=client,
                generated_at="2024-01-07T23:00:00Z",
            )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.payload["expected_latest_bar_date"], "2024-01-05")
            self.assertEqual(result.payload["closed_session_watermark"], "2024-01-05")

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
                    FakeBar(
                        symbol="QQQ",
                        timestamp=datetime(2024, 1, 2),
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


class FetchRequestWindowTests(unittest.TestCase):
    def test_request_end_covers_the_full_end_day(self) -> None:
        """Regression: a midnight end excluded the end day's bar entirely
        (campaign day 1 blocked with dataset_stale despite a same-day fetch)."""
        client = FakeStockHistoricalDataClient(
            {
                "SPY": [
                    FakeBar(
                        symbol="SPY",
                        timestamp=datetime(2026, 7, 7),
                        open=1,
                        high=2,
                        low=1,
                        close=2,
                        volume=5,
                    )
                ]
            }
        )
        fetch_daily_bars(symbols=["SPY"], start="2026-07-01", end="2026-07-07", client=client)

        request = client.requests[0]
        self.assertEqual((request.end.hour, request.end.minute, request.end.second), (23, 59, 59))
        self.assertEqual(request.end.date().isoformat(), "2026-07-07")


if __name__ == "__main__":
    unittest.main()
