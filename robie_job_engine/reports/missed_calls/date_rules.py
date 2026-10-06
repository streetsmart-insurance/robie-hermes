"""Date rules: target dates, the Monday rule, tab names, day bounds.

Process-doc rules encoded here (M3 LOCKED 2026-10-05, Sandeep):
  * Target dates = every date since the last business day the agency was
    open. Walk back from yesterday: while the day is a weekend or an agency
    holiday, include it; stop at the first open business day and include it.
  * Monday -> Fri, Sat, Sun (each its own dated tab, M/D). Never merge.
  * Tuesday after a Monday holiday (e.g. 2026-10-12) -> Fri, Sat, Sun, Mon.
  * Any other weekday -> yesterday only.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

AGENCY_TZ = ZoneInfo("America/New_York")  # M1/X2 LOCKED 2026-10-05: ET.

# US federal holidays the agency observes (M3). Extend per-run with
# --holidays; clear with --ignore-default-holidays. If the agency works a
# listed day, the extra date is harmless (its calls are still real); if it
# closes on an unlisted day, pass it via --holidays.
DEFAULT_CLOSED_DATES = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),  # observed
        date(2026, 9, 7),
        date(2026, 10, 12),  # Columbus Day — the M3 motivating case
        date(2026, 11, 11),
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),  # observed
        date(2027, 9, 6),
        date(2027, 10, 11),
        date(2027, 11, 11),
        date(2027, 11, 25),
        date(2027, 12, 24),  # observed
    }
)

_TAB_RE = re.compile(r"^(1[0-2]|[1-9])/(3[01]|[12][0-9]|[1-9])$")


def agency_closed(
    d: date,
    extra_holidays: frozenset[date] = frozenset(),
    use_default_holidays: bool = True,
) -> bool:
    """Weekends and agency holidays (M3)."""
    if d.weekday() >= 5:
        return True
    if use_default_holidays and d in DEFAULT_CLOSED_DATES:
        return True
    return d in extra_holidays


def target_dates(
    run_date: date,
    extra_holidays: frozenset[date] = frozenset(),
    use_default_holidays: bool = True,
) -> list[date]:
    """Dates to process for a run executed on ``run_date`` (agency tz).

    Every date since the last business day the agency was open (M3, locked):
    walk back from yesterday while days are closed, then include the first
    open business day. Returned in chronological order.
    """
    dates: list[date] = []
    d = run_date - timedelta(days=1)
    while agency_closed(d, extra_holidays, use_default_holidays):
        dates.append(d)
        d -= timedelta(days=1)
    dates.append(d)  # the last open business day
    return sorted(dates)


def tab_name(target: date) -> str:
    """Dated tab name in M/D format, e.g. 10/4."""
    return f"{target.month}/{target.day}"


def day_bounds_utc(target: date, tz: ZoneInfo = AGENCY_TZ) -> tuple[datetime, datetime]:
    """[ET midnight, next ET midnight) for the target date, as UTC datetimes."""
    start_et = datetime(target.year, target.month, target.day, tzinfo=tz)
    end_et = start_et + timedelta(days=1)
    return start_et.astimezone(ZoneInfo("UTC")), end_et.astimezone(ZoneInfo("UTC"))


def parse_tab_name(name: str, year_hint: int) -> Optional[date]:
    """Parse an M/D tab title back to a date. None when not a date tab."""
    m = _TAB_RE.match(name.strip())
    if not m:
        return None
    try:
        return date(year_hint, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
