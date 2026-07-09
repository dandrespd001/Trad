"""Tests for the signal-order reference price lookup (paper price-sanity gate)."""

from __future__ import annotations

import unittest

from trading_ai.cli import _signal_reference_price


class SignalReferencePriceTests(unittest.TestCase):
    ROWS = [
        {"timestamp": "2026-07-06", "symbol": "SPY", "close": "600.0"},
        {"timestamp": "2026-07-07", "symbol": "SPY", "close": "601.5"},
        {"timestamp": "2026-07-08", "symbol": "SPY", "close": "602.25"},
        {"timestamp": "2026-07-08", "symbol": "XLV", "close": "162.18"},
    ]

    def test_exact_timestamp_match(self) -> None:
        self.assertEqual(
            _signal_reference_price(self.ROWS, symbol="SPY", timestamp="2026-07-07"), 601.5
        )

    def test_case_insensitive_symbol(self) -> None:
        self.assertEqual(
            _signal_reference_price(self.ROWS, symbol="xlv", timestamp="2026-07-08"), 162.18
        )

    def test_falls_back_to_latest_close_when_timestamp_absent(self) -> None:
        # No 2026-07-09 row for SPY -> latest available close (2026-07-08).
        self.assertEqual(
            _signal_reference_price(self.ROWS, symbol="SPY", timestamp="2026-07-09"), 602.25
        )

    def test_unknown_symbol_returns_none(self) -> None:
        self.assertIsNone(
            _signal_reference_price(self.ROWS, symbol="ZZZ", timestamp="2026-07-08")
        )

    def test_non_numeric_close_skipped(self) -> None:
        rows = [
            {"timestamp": "2026-07-08", "symbol": "SPY", "close": ""},
            {"timestamp": "2026-07-08", "symbol": "SPY", "close": "not_a_number"},
        ]
        self.assertIsNone(_signal_reference_price(rows, symbol="SPY", timestamp="2026-07-08"))


if __name__ == "__main__":
    unittest.main()
