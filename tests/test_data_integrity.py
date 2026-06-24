import unittest

from trading_ai.data.validation import detect_calendar_gaps, timezone_consistency_issues


def _row(symbol: str, timestamp: str) -> dict[str, object]:
    return {"symbol": symbol, "timestamp": timestamp}


class CalendarGapTests(unittest.TestCase):
    def test_contiguous_daily_data_has_no_gaps(self) -> None:
        rows = [_row("SPY", f"2026-01-{d:02d}") for d in (5, 6, 7, 8, 9)]  # Mon-Fri
        self.assertEqual(detect_calendar_gaps(rows, max_gap_days=5), [])

    def test_weekend_within_tolerance(self) -> None:
        rows = [_row("SPY", "2026-01-09"), _row("SPY", "2026-01-12")]  # Fri -> Mon (3 days)
        self.assertEqual(detect_calendar_gaps(rows, max_gap_days=5), [])

    def test_multi_week_hole_is_flagged(self) -> None:
        rows = [_row("SPY", "2026-01-05"), _row("SPY", "2026-01-26")]  # 21-day gap
        gaps = detect_calendar_gaps(rows, max_gap_days=5)
        self.assertEqual(len(gaps), 1)
        self.assertIn("SPY", gaps[0])
        self.assertIn("21-day gap", gaps[0])

    def test_gaps_are_per_symbol(self) -> None:
        rows = [
            _row("SPY", "2026-01-05"),
            _row("SPY", "2026-01-06"),
            _row("QQQ", "2026-01-05"),
            _row("QQQ", "2026-02-05"),  # big hole for QQQ only
        ]
        gaps = detect_calendar_gaps(rows, max_gap_days=5)
        self.assertEqual(len(gaps), 1)
        self.assertTrue(gaps[0].startswith("QQQ"))

    def test_unparseable_timestamps_ignored(self) -> None:
        rows = [_row("SPY", "not-a-date"), _row("SPY", "2026-01-05")]
        self.assertEqual(detect_calendar_gaps(rows, max_gap_days=5), [])


class TimezoneConsistencyTests(unittest.TestCase):
    def test_all_naive_is_consistent(self) -> None:
        rows = [_row("SPY", "2026-01-05T09:30:00"), _row("SPY", "2026-01-06T09:30:00")]
        self.assertEqual(timezone_consistency_issues(rows), [])

    def test_all_dates_is_consistent(self) -> None:
        rows = [_row("SPY", "2026-01-05"), _row("SPY", "2026-01-06")]
        self.assertEqual(timezone_consistency_issues(rows), [])

    def test_mixed_aware_and_naive_is_flagged(self) -> None:
        rows = [_row("SPY", "2026-01-05T09:30:00+00:00"), _row("SPY", "2026-01-06T09:30:00")]
        issues = timezone_consistency_issues(rows)
        self.assertEqual(len(issues), 1)
        self.assertIn("timezone", issues[0])


if __name__ == "__main__":
    unittest.main()
