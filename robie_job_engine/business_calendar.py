"""Business dates for accountability's explicitly configured holiday calendar."""

from datetime import date, timedelta


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))


def us_federal_holidays(year: int) -> set[date]:
    """Observed federal holidays, including next year's New Year observance."""
    holidays = set()
    for holiday_year in (year - 1, year, year + 1):
        for month, day in ((1, 1), (7, 4), (11, 11), (12, 25)):
            holidays.add(_observed(date(holiday_year, month, day)))
        if holiday_year >= 2021:
            holidays.add(_observed(date(holiday_year, 6, 19)))
        holidays.update((
            _nth_weekday(holiday_year, 1, 0, 3),
            _nth_weekday(holiday_year, 2, 0, 3),
            date(holiday_year, 5, 31) - timedelta(days=date(holiday_year, 5, 31).weekday()),
            _nth_weekday(holiday_year, 9, 0, 1),
            _nth_weekday(holiday_year, 10, 0, 2),
            _nth_weekday(holiday_year, 11, 3, 4),
        ))
    return {day for day in holidays if day.year == year}


def previous_business_day(value: date, *, holiday_calendar: str | None = None) -> date:
    """Skip weekends and the selected calendar; never ignore an unknown name.

    An absent calendar preserves the existing weekday-only behavior. Agency
    closures must be configured explicitly rather than inferred from location.
    """
    if holiday_calendar not in (None, "", "US-FEDERAL"):
        raise ValueError(f"Unsupported accountability holiday calendar: {holiday_calendar}")
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5 or (
        holiday_calendar == "US-FEDERAL" and candidate in us_federal_holidays(candidate.year)
    ):
        candidate -= timedelta(days=1)
    return candidate
