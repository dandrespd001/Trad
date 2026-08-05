import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trading_ai.data.market_calendar import (
    XNYS_CALENDAR_CONTRACT_VERSION,
    XNYS_CALENDAR_SHA256,
    XNYS_CALENDAR_SNAPSHOT_END,
    XnysCalendarContractError,
    is_trading_day,
    latest_closed_xnys_session,
    missing_sessions,
    nyse_holidays,
    trading_days,
    verified_xnys_trading_days,
    xnys_calendar_implementation_sha256,
    xnys_calendar_sha256,
    xnys_calendar_snapshot_payload,
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

    def test_2025_national_day_of_mourning_is_full_day_closure(self) -> None:
        closure = date(2025, 1, 9)

        self.assertIn(closure, nyse_holidays(2025))
        self.assertFalse(is_trading_day(closure))
        self.assertEqual(
            trading_days(date(2025, 1, 8), date(2025, 1, 10)),
            [date(2025, 1, 8), date(2025, 1, 10)],
        )

    def test_2028_saturday_new_year_has_no_observed_monday_closure(self) -> None:
        self.assertNotIn(date(2028, 1, 3), nyse_holidays(2028))
        self.assertTrue(is_trading_day(date(2028, 1, 3)))


class XnysCalendarSnapshotTests(unittest.TestCase):
    def test_snapshot_hash_and_payload_are_deterministic(self) -> None:
        self.assertEqual(
            xnys_calendar_sha256(),
            "886eed762ac68ac5e90520d79a3b67b0c09a93c8aa2f4a69bedf434a7bf7ceb0",
        )
        self.assertEqual(XNYS_CALENDAR_SHA256, xnys_calendar_sha256())
        self.assertRegex(xnys_calendar_implementation_sha256(), r"^[0-9a-f]{64}$")

        payload = xnys_calendar_snapshot_payload()
        self.assertEqual(payload["contract_version"], XNYS_CALENDAR_CONTRACT_VERSION)
        self.assertEqual(payload["snapshot_start"], "2024-01-01")
        self.assertEqual(payload["snapshot_end"], "2028-12-31")
        self.assertEqual(payload["watermark_regular_close"], "16:00:00")
        payload["contract_version"] = "mutated"
        self.assertEqual(
            xnys_calendar_snapshot_payload()["contract_version"],
            XNYS_CALENDAR_CONTRACT_VERSION,
        )

    def test_verified_sessions_require_exact_version_hash_and_range(self) -> None:
        sessions = verified_xnys_trading_days(
            date(2025, 1, 8),
            date(2025, 1, 10),
            calendar_contract_version=XNYS_CALENDAR_CONTRACT_VERSION,
            calendar_sha256=XNYS_CALENDAR_SHA256,
        )
        self.assertEqual(sessions, [date(2025, 1, 8), date(2025, 1, 10)])

        invalid_cases = (
            {
                "calendar_contract_version": "xnys-unpinned",
                "calendar_sha256": XNYS_CALENDAR_SHA256,
                "start": date(2025, 1, 8),
                "end": date(2025, 1, 10),
            },
            {
                "calendar_contract_version": XNYS_CALENDAR_CONTRACT_VERSION,
                "calendar_sha256": "0" * 64,
                "start": date(2025, 1, 8),
                "end": date(2025, 1, 10),
            },
            {
                "calendar_contract_version": XNYS_CALENDAR_CONTRACT_VERSION,
                "calendar_sha256": XNYS_CALENDAR_SHA256,
                "start": date(2023, 12, 29),
                "end": date(2024, 1, 2),
            },
            {
                "calendar_contract_version": XNYS_CALENDAR_CONTRACT_VERSION,
                "calendar_sha256": XNYS_CALENDAR_SHA256,
                "start": XNYS_CALENDAR_SNAPSHOT_END,
                "end": date(2029, 1, 2),
            },
            {
                "calendar_contract_version": XNYS_CALENDAR_CONTRACT_VERSION,
                "calendar_sha256": XNYS_CALENDAR_SHA256,
                "start": date(2029, 1, 2),
                "end": XNYS_CALENDAR_SNAPSHOT_END,
            },
        )
        for case in invalid_cases:
            with self.subTest(case=case), self.assertRaises(XnysCalendarContractError):
                verified_xnys_trading_days(**case)

    def test_closed_session_watermark_is_conservative_and_timezone_aware(self) -> None:
        new_york = ZoneInfo("America/New_York")
        self.assertEqual(
            latest_closed_xnys_session(datetime(2026, 7, 29, 15, 59, tzinfo=new_york)),
            date(2026, 7, 28),
        )
        self.assertEqual(
            latest_closed_xnys_session(datetime(2026, 7, 29, 16, 0, tzinfo=new_york)),
            date(2026, 7, 29),
        )
        self.assertEqual(
            latest_closed_xnys_session(datetime(2026, 7, 5, 18, 0, tzinfo=new_york)),
            date(2026, 7, 2),
        )
        with self.assertRaisesRegex(XnysCalendarContractError, "timezone-aware"):
            latest_closed_xnys_session(datetime(2026, 7, 29, 16, 0))


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
