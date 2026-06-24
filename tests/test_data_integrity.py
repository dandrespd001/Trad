import unittest

from trading_ai.data.validation import (
    detect_calendar_gaps,
    detect_missing_sessions,
    timezone_consistency_issues,
)


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


class MissingSessionWarningTests(unittest.TestCase):
    def test_holiday_week_is_clean(self) -> None:
        # Thanksgiving week (Thu Nov 26 2026 closed) with the holiday legitimately absent.
        rows = [_row("SPY", d) for d in ("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27")]
        self.assertEqual(detect_missing_sessions(rows), [])

    def test_long_holiday_weekend_is_clean(self) -> None:
        # Fri before MLK Monday (Jan 19 2026 closed) -> next session Tue Jan 20.
        rows = [_row("SPY", d) for d in ("2026-01-16", "2026-01-20", "2026-01-21")]
        self.assertEqual(detect_missing_sessions(rows), [])

    def test_skipped_trading_day_is_flagged(self) -> None:
        rows = [_row("SPY", d) for d in ("2026-01-05", "2026-01-06", "2026-01-08", "2026-01-09")]
        warnings = detect_missing_sessions(rows)
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("SPY"))
        self.assertIn("2026-01-07", warnings[0])

    def test_missing_sessions_are_per_symbol(self) -> None:
        rows = [
            _row("SPY", "2026-01-05"),
            _row("SPY", "2026-01-06"),
            _row("QQQ", "2026-01-05"),
            _row("QQQ", "2026-01-08"),  # QQQ skips Jan 6-7
        ]
        warnings = detect_missing_sessions(rows)
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("QQQ"))

    def test_single_observation_is_ignored(self) -> None:
        self.assertEqual(detect_missing_sessions([_row("SPY", "2026-01-05")]), [])


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
