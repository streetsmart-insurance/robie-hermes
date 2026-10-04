"""Task Check-In timestamps are America/Chicago.

Looker writes Created Date one hour behind Eastern. A task created at
10:11 ET is stored as 9:11. Every calling-window, same-day, and age
check converts that naive timestamp to America/New_York first.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
NEW_YORK = ZoneInfo("America/New_York")

CALL_WINDOW_START = time(9, 0)
CALL_WINDOW_END = time(18, 0)  # 6:00 PM exclusive

_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def report_created_et(raw: str) -> datetime | None:
    """Parse a report Created Date and return it in America/New_York.

    Naive values are America/Chicago. Values that already carry an
    offset are converted, not reinterpreted. Empty or unreadable text
    returns None so the caller fails closed instead of guessing.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = _parse_formats(text)
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHICAGO)
    return parsed.astimezone(NEW_YORK)


def _parse_formats(text: str) -> datetime | None:
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def as_eastern_clock(moment: datetime) -> datetime:
    """Operator clock to Eastern. An aware value is converted.

    A naive value is the operator clock, already Eastern. Report
    Created Date strings go through report_created_et instead, because
    those naive values are Central.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=NEW_YORK)
    return moment.astimezone(NEW_YORK)


def in_calling_window(moment: datetime) -> bool:
    """Weekdays 9:00 inclusive through 6:00 PM exclusive, Eastern.

    ``moment`` must already be the instant to test. Pass the result of
    report_created_et when the instant came from the report.
    """
    current = moment.astimezone(NEW_YORK) if moment.tzinfo else moment.replace(tzinfo=NEW_YORK)
    if current.weekday() >= 5:
        return False
    start = current.replace(
        hour=CALL_WINDOW_START.hour, minute=0, second=0, microsecond=0,
    )
    end = current.replace(
        hour=CALL_WINDOW_END.hour, minute=0, second=0, microsecond=0,
    )
    return start <= current < end


def in_business_hours(moment: datetime) -> bool:
    """Health-probe hours: weekdays 9:00–6:00 PM ET. Holidays still count."""
    return in_calling_window(moment)


def eastern_day(moment: datetime) -> str:
    """America/New_York calendar day for same-day dedupe."""
    current = moment.astimezone(NEW_YORK) if moment.tzinfo else moment.replace(tzinfo=NEW_YORK)
    return current.date().isoformat()


def age_minutes(created_et: datetime, now: datetime) -> float:
    """Minutes from a converted Created Date to an Eastern clock."""
    return (as_eastern_clock(now) - created_et.astimezone(NEW_YORK)).total_seconds() / 60


def previous_business_day(day: date) -> date:
    """The business day before ``day``.

    Weekends and US federal holidays are skipped. Monday's previous
    business day is Friday, unless that Friday is a holiday.
    """
    from .business_calendar import previous_business_day as calendar_previous

    return calendar_previous(day, holiday_calendar="US-FEDERAL")


def within_default_call_age(created_et: datetime, now: datetime) -> bool:
    """True when Created Date is at or after 00:00 ET on the previous business day.

    A task created Saturday, Sunday, or on a federal holiday is dialable
    on the next business day, inside the calling window. A task from
    before that midnight is too old. A future Created Date is not eligible.
    """
    created = created_et.astimezone(NEW_YORK)
    current = as_eastern_clock(now)
    if created > current:
        return False
    start = datetime.combine(
        previous_business_day(current.date()), time.min, tzinfo=NEW_YORK,
    )
    return created >= start


def within_task_age(
    created_et: datetime, now: datetime, *, max_hours: int | None,
) -> bool:
    """Age gate for a dial. Unset hours means the business-day rule."""
    if max_hours is None:
        return within_default_call_age(created_et, now)
    minutes = age_minutes(created_et, now)
    return 0 <= minutes <= max_hours * 60
