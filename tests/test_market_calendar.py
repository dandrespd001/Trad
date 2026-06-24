import unittest
from datetime import date

from trading_ai.data.market_calendar import (
    is_trading_day,
    missing_sessions,
    nyse_holidays,
    trading_days,
)


class NyseHolidayTests(unittest.TestCase):
    def test_full_2026_holiday_set(self) -> None:
        expected = {
            date(2026, 1, 1),  # New Year's Day (Thu)
            date(2026, 1, 19),  # MLK Jr. Day (3rd Mon)
            date(2026, 2, 16),  # Presidents' Day (3rd Mon)
            date(2026, 4, 3),  # Good Friday (Easter Apr 5)
            date(2026, 5, 25),  # Memorial Day (last Mon)
            date(2026, 6, 19),  # Juneteenth (Fri)
            date(2026, 7, 3),  # Independence Day observed (Jul 4 is Sat)
            date(2026, 9, 7),  # Labor Day (1st Mon)
            date(2026, 11, 26),  # Thanksgiving (4th Thu)
            date(2026, 12, 25),  # Christmas (Fri)
        }
        self.assertEqual(nyse_holidays(2026), expected)

    def test_saturday_new_year_is_not_observed(self) -> None:
        # Jan 1 2022 was a Saturday; NYSE did NOT close the preceding Friday.
        self.assertNotIn(date(2022, 1, 1), nyse_holidays(2022))
        self.assertNotIn(date(2021, 12, 31), nyse_holidays(2022))
        self.assertTrue(is_trading_day(date(2021, 12, 31)))

    def test_sunday_holiday_observed_following_monday(self) -> None:
        # Jul 4 2021 was a Sunday -> observed Monday Jul 5.
        self.assertIn(date(2021, 7, 5), nyse_holidays(2021))
        self.assertFalse(is_trading_day(date(2021, 7, 5)))

    def test_saturday_christmas_observed_preceding_friday(self) -> None:
        # Dec 25 2027 is a Saturday -> observed Friday Dec 24.
        self.assertIn(date(2027, 12, 24), nyse_holidays(2027))

    def test_good_friday_tracks_easter(self) -> None:
        # Easter 2027 is Mar 28 -> Good Friday Mar 26.
        self.assertIn(date(2027, 3, 26), nyse_holidays(2027))

    def test_juneteenth_only_from_2021(self) -> None:
        self.assertNotIn(date(2020, 6, 19), nyse_holidays(2020))
        self.assertIn(date(2021, 6, 18), nyse_holidays(2021))  # Jun 19 2021 Sat -> Fri 18

    def test_mlk_only_from_1998(self) -> None:
        self.assertFalse(any(d.month == 1 and d.day >= 15 for d in nyse_holidays(1997)))


class TradingDayTests(unittest.TestCase):
    def test_weekend_is_not_a_trading_day(self) -> None:
        self.assertFalse(is_trading_day(date(2026, 1, 3)))  # Saturday
        self.assertFalse(is_trading_day(date(2026, 1, 4)))  # Sunday

    def test_holiday_is_not_a_trading_day(self) -> None:
        self.assertFalse(is_trading_day(date(2026, 11, 26)))  # Thanksgiving

    def test_regular_weekday_is_a_trading_day(self) -> None:
        self.assertTrue(is_trading_day(date(2026, 1, 5)))  # Monday

    def test_trading_days_excludes_holiday_and_weekend(self) -> None:
        # Week containing Thanksgiving (Thu Nov 26 2026).
        sessions = trading_days(date(2026, 11, 23), date(2026, 11, 29))
        self.assertEqual(
            sessions,
            [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27)],
        )

    def test_trading_days_empty_when_end_before_start(self) -> None:
        self.assertEqual(trading_days(date(2026, 1, 9), date(2026, 1, 5)), [])


class MissingSessionTests(unittest.TestCase):
    def test_no_missing_when_all_sessions_present(self) -> None:
        observed = trading_days(date(2026, 1, 5), date(2026, 1, 9))
        self.assertEqual(missing_sessions(observed, start=date(2026, 1, 5), end=date(2026, 1, 9)), [])

    def test_skipped_weekday_is_missing(self) -> None:
        observed = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 8), date(2026, 1, 9)]
        self.assertEqual(
            missing_sessions(observed, start=date(2026, 1, 5), end=date(2026, 1, 9)),
            [date(2026, 1, 7)],
        )

    def test_absent_holiday_is_not_reported_missing(self) -> None:
        # Thanksgiving week with the holiday legitimately absent from the data.
        observed = [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27)]
        self.assertEqual(
            missing_sessions(observed, start=date(2026, 11, 23), end=date(2026, 11, 27)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
