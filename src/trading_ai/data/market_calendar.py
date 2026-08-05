"""Self-contained, versioned NYSE/XNYS full-day trading calendar.

The core package depends only on PyYAML, so this implements the NYSE holiday
schedule directly rather than pulling in ``pandas_market_calendars``/``holidays``.
Governed IEX imports use an explicit 2024-2028 snapshot sourced from NYSE. The
recurring rules remain available outside that range for legacy diagnostics,
but only the snapshot is eligible for governed provenance.

The calendar covers regular full-day market closures (it does not model
early-close half days, which remain valid daily sessions):

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
- Explicit one-off full-day closures announced by the exchange

Weekend-observance rule (NYSE): a fixed-date holiday falling on Saturday is
observed the preceding Friday; on Sunday it is observed the following Monday.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MLK_FIRST_YEAR = 1998
JUNETEENTH_FIRST_YEAR = 2021
XNYS_CALENDAR_CONTRACT_VERSION = "xnys-full-day-2024-2028-v2"
XNYS_CALENDAR_TIMEZONE = "America/New_York"
XNYS_CALENDAR_SNAPSHOT_START = date(2024, 1, 1)
XNYS_CALENDAR_SNAPSHOT_END = date(2028, 12, 31)
XNYS_REGULAR_CLOSE = time(16, 0)

# Exact full-day closures for the governed snapshot. These dates are frozen
# rather than inferred at runtime so historical and forward folds can pin one
# deterministic calendar, including extraordinary closures.
XNYS_FULL_DAY_CLOSURES = (
    (
        2024,
        (
            "2024-01-01",
            "2024-01-15",
            "2024-02-19",
            "2024-03-29",
            "2024-05-27",
            "2024-06-19",
            "2024-07-04",
            "2024-09-02",
            "2024-11-28",
            "2024-12-25",
        ),
    ),
    (
        2025,
        (
            "2025-01-01",
            "2025-01-09",
            "2025-01-20",
            "2025-02-17",
            "2025-04-18",
            "2025-05-26",
            "2025-06-19",
            "2025-07-04",
            "2025-09-01",
            "2025-11-27",
            "2025-12-25",
        ),
    ),
    (
        2026,
        (
            "2026-01-01",
            "2026-01-19",
            "2026-02-16",
            "2026-04-03",
            "2026-05-25",
            "2026-06-19",
            "2026-07-03",
            "2026-09-07",
            "2026-11-26",
            "2026-12-25",
        ),
    ),
    (
        2027,
        (
            "2027-01-01",
            "2027-01-18",
            "2027-02-15",
            "2027-03-26",
            "2027-05-31",
            "2027-06-18",
            "2027-07-05",
            "2027-09-06",
            "2027-11-25",
            "2027-12-24",
        ),
    ),
    (
        2028,
        (
            "2028-01-17",
            "2028-02-21",
            "2028-04-14",
            "2028-05-29",
            "2028-06-19",
            "2028-07-04",
            "2028-09-04",
            "2028-11-23",
            "2028-12-25",
        ),
    ),
)
XNYS_CALENDAR_SOURCES = (
    "https://www.nyse.com/publicdocs/ICE_NYSE_2024_Yearly_Trading_Calendar.pdf",
    "https://www.nyse.com/publicdocs/ICE_NYSE_2025_Yearly_Trading_Calendar.pdf",
    "https://www.nyse.com/trade/hours-calendars",
    (
        "https://www.nyse.com/publicdocs/nyse/markets/american-options/"
        "rule-interpretations/2025/National_Day_of_Mourning_20250102.pdf"
    ),
)
# Updated deliberately whenever the canonical snapshot payload changes.
XNYS_CALENDAR_SHA256 = "886eed762ac68ac5e90520d79a3b67b0c09a93c8aa2f4a69bedf434a7bf7ceb0"


class XnysCalendarContractError(ValueError):
    """Raised when governed code cannot prove the frozen XNYS calendar contract."""

# One-off closures cannot be derived from the recurring holiday rules. Keep
# this list explicit and source-backed so a missing bar on one of these dates
# is not misclassified as an incomplete market-data response.
#
# 2025-01-09: National Day of Mourning for President Jimmy Carter.
# https://www.nyse.com/publicdocs/nyse/markets/american-options/rule-interpretations/2025/National_Day_of_Mourning_20250102.pdf
SPECIAL_FULL_DAY_CLOSURES = frozenset(
    {
        date(2025, 1, 9),
    }
)


def xnys_calendar_snapshot_payload() -> dict[str, object]:
    """Return a fresh JSON-compatible copy of the governed calendar snapshot."""

    return {
        "calendar": "XNYS",
        "contract_version": XNYS_CALENDAR_CONTRACT_VERSION,
        "timezone": XNYS_CALENDAR_TIMEZONE,
        "session_kind": "full_day_closures_only",
        "early_close_dates_are_sessions": True,
        "watermark_regular_close": XNYS_REGULAR_CLOSE.isoformat(),
        "watermark_policy": "regular_close_conservative_for_early_close_sessions",
        "snapshot_start": XNYS_CALENDAR_SNAPSHOT_START.isoformat(),
        "snapshot_end": XNYS_CALENDAR_SNAPSHOT_END.isoformat(),
        "full_day_closures": [
            {"year": year, "dates": list(closures)}
            for year, closures in XNYS_FULL_DAY_CLOSURES
        ],
        "sources": list(XNYS_CALENDAR_SOURCES),
    }


def _computed_xnys_calendar_sha256() -> str:
    canonical = json.dumps(
        xnys_calendar_snapshot_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def xnys_calendar_sha256() -> str:
    """Return the verified hash of the governed calendar snapshot."""

    actual = _computed_xnys_calendar_sha256()
    if actual != XNYS_CALENDAR_SHA256:
        raise XnysCalendarContractError(
            "XNYS calendar snapshot hash does not match its pinned SHA-256"
        )
    return actual


def xnys_calendar_implementation_sha256() -> str:
    """Hash the executable session/watermark policy used with the snapshot.

    The snapshot digest alone cannot detect changes to the code that interprets
    its closure dates. Producers attest this independent implementation digest;
    importers and consumers recompute it from their local trusted checkout.
    """

    digest = hashlib.sha256()
    with Path(__file__).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# This is a runtime pin to the trusted local implementation. It intentionally
# is not embedded as a literal in this same file, which would make the digest
# self-referential and impossible to update deterministically.
XNYS_CALENDAR_IMPLEMENTATION_SHA256 = xnys_calendar_implementation_sha256()


def _snapshot_closures(year: int) -> set[date] | None:
    if not XNYS_CALENDAR_SNAPSHOT_START.year <= year <= XNYS_CALENDAR_SNAPSHOT_END.year:
        return None
    xnys_calendar_sha256()
    for snapshot_year, closures in XNYS_FULL_DAY_CLOSURES:
        if snapshot_year == year:
            return {date.fromisoformat(value) for value in closures}
    raise XnysCalendarContractError(f"XNYS calendar snapshot is missing year {year}")


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

    snapshot = _snapshot_closures(year)
    if snapshot is not None:
        return snapshot

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
    holidays.update(day for day in SPECIAL_FULL_DAY_CLOSURES if day.year == year)
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


def verified_xnys_trading_days(
    start: date,
    end: date,
    *,
    calendar_contract_version: str,
    calendar_sha256: str,
) -> list[date]:
    """Return governed sessions only when the exact frozen contract is pinned."""

    if calendar_contract_version != XNYS_CALENDAR_CONTRACT_VERSION:
        raise XnysCalendarContractError(
            "XNYS calendar contract version does not match the governed snapshot"
        )
    actual_sha256 = xnys_calendar_sha256()
    if calendar_sha256 != actual_sha256:
        raise XnysCalendarContractError(
            "XNYS calendar SHA-256 does not match the governed snapshot"
        )
    if end < start:
        raise XnysCalendarContractError(
            "XNYS calendar range start must not be after end"
        )
    if not (
        XNYS_CALENDAR_SNAPSHOT_START <= start <= XNYS_CALENDAR_SNAPSHOT_END
        and XNYS_CALENDAR_SNAPSHOT_START <= end <= XNYS_CALENDAR_SNAPSHOT_END
    ):
        raise XnysCalendarContractError(
            "requested range is outside the governed XNYS calendar snapshot"
        )
    return trading_days(start, end)


def latest_closed_xnys_session(as_of: datetime) -> date:
    """Return the latest conservatively closed regular XNYS session.

    The watermark uses the regular 16:00 America/New_York close. Early-close
    sessions therefore become eligible only at 16:00, which is deliberately
    conservative for daily-bar publication.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise XnysCalendarContractError("as_of must be timezone-aware")
    local = as_of.astimezone(ZoneInfo(XNYS_CALENDAR_TIMEZONE))
    candidate = local.date()
    if candidate < XNYS_CALENDAR_SNAPSHOT_START or candidate > XNYS_CALENDAR_SNAPSHOT_END:
        raise XnysCalendarContractError(
            "as_of is outside the governed XNYS calendar snapshot"
        )
    after_regular_close = (
        local.hour,
        local.minute,
        local.second,
        local.microsecond,
    ) >= (
        XNYS_REGULAR_CLOSE.hour,
        XNYS_REGULAR_CLOSE.minute,
        0,
        0,
    )
    if not (is_trading_day(candidate) and after_regular_close):
        candidate -= timedelta(days=1)
        while candidate >= XNYS_CALENDAR_SNAPSHOT_START and not is_trading_day(candidate):
            candidate -= timedelta(days=1)
    if candidate < XNYS_CALENDAR_SNAPSHOT_START:
        raise XnysCalendarContractError(
            "no closed XNYS session exists inside the governed snapshot"
        )
    return candidate


def missing_sessions(observed: Iterable[date], *, start: date, end: date) -> list[date]:
    """Expected NYSE sessions in ``[start, end]`` that are absent from ``observed``."""

    seen = {value for value in observed if isinstance(value, date)}
    return [session for session in trading_days(start, end) if session not in seen]
