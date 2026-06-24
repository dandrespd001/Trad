"""Self-contained NYSE trading calendar (pure Python, no third-party deps).

The core package depends only on PyYAML, so this implements the NYSE holiday
schedule directly rather than pulling in ``pandas_market_calendars``/``holidays``.
It covers the regular full-day market closures (it does not model early-close
half days, which do not affect daily-bar continuity):

- New Year's Day (Jan 1; Sunday observed Monday, but a Saturday Jan 1 is NOT
  observed on the preceding Friday — an NYSE-specific exception)
- Martin Luther King Jr. Day (3rd Monday of January, from 1998)
- Washington's Birthday / Presidents' Day (3rd Monday of February)
- Good Friday (Friday before Easter Sunday)
- Memorial Day (last Monday of May)
- Juneteenth National Independence Day (Jun 19, weekend-observed, from 2021)
- Independence Day (Jul 4, weekend-observed)
- Labor Day (1st Monday of September)
- Thanksgiving Day (4th Thursday of November)
- Christmas Day (Dec 25, weekend-observed)

Weekend-observance rule (NYSE): a fixed-date holiday falling on Saturday is
observed the preceding Friday; on Sunday it is observed the following Monday.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

MLK_FIRST_YEAR = 1998
JUNETEENTH_FIRST_YEAR = 2021


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the ``n``-th ``weekday`` (Mon=0) of ``month`` in ``year`` (1-indexed)."""

    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last ``weekday`` (Mon=0) of ``month`` in ``year``."""

    last = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(holiday: date) -> date:
    """Apply the NYSE weekend-observance rule to a fixed-date holiday."""

    if holiday.weekday() == 5:  # Saturday -> observed Friday
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday -> observed Monday
        return holiday + timedelta(days=1)
    return holiday


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday using the anonymous Gregorian (Meeus/Jones/Butcher) algorithm."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    """Return the set of full-day NYSE market closures for the given calendar year."""

    holidays: set[date] = {
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
        _observed(date(year, 12, 25)),  # Christmas Day
    }
    # New Year's Day: Sunday is observed the following Monday, but a Saturday Jan 1 is
    # NOT observed on the preceding Friday (NYSE exception), so the market stays open.
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        holidays.add(new_year + timedelta(days=1))
    elif new_year.weekday() != 5:
        holidays.add(new_year)
    if year >= MLK_FIRST_YEAR:
        holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK Jr. Day
    if year >= JUNETEENTH_FIRST_YEAR:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return holidays


def is_trading_day(day: date) -> bool:
    """True when ``day`` is a regular NYSE session (not a weekend or full-day holiday)."""

    if day.weekday() >= 5:  # Saturday/Sunday
        return False
    return day not in nyse_holidays(day.year)


def trading_days(start: date, end: date) -> list[date]:
    """All NYSE sessions in the inclusive ``[start, end]`` range, in ascending order."""

    if end < start:
        return []
    sessions: list[date] = []
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            sessions.append(cursor)
        cursor += timedelta(days=1)
    return sessions


def missing_sessions(observed: Iterable[date], *, start: date, end: date) -> list[date]:
    """Expected NYSE sessions in ``[start, end]`` that are absent from ``observed``."""

    seen = {value for value in observed if isinstance(value, date)}
    return [session for session in trading_days(start, end) if session not in seen]
