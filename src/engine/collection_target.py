"""Target dates for evening source collection versus the morning department Doc.

``production_main`` on streetsmart-accountability-prod (captured 2026-09-20,
and still the 2026-09-25 evening outcome) chooses the run date like this:

    target = parsed(--date) if --date else get_previous_business_day()

``--skip-if-prepared`` reuses a prepared snapshot only when that snapshot's
date is ``target``. It does not treat a ready prior day as today's snapshot.
The weekday 17:00 collector was invoking ``production_main --skip-if-prepared``
with no ``--date``, so it prepared the prior business day and Team Lead Chat
EOD at 17:05 found no same-day snapshot.

Evening collect must pass today's America/New_York calendar day. Morning
09:00 keeps omitting ``--date`` so the department Doc stays on the previous
weekday.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def eastern_calendar_day(moment: datetime) -> date:
    """Calendar date of ``moment`` in America/New_York."""
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    return moment.astimezone(EASTERN).date()


def previous_weekday(day: date) -> date:
    """Monday reports Friday. Saturday and Sunday walk back to Friday.

    Federal holidays are not skipped here. The morning wrapper already skips
    a run when *today* is a federal holiday; ``production_main`` still owns
    ``get_previous_business_day()`` when ``--date`` is omitted.
    """
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def evening_collection_target(moment: datetime) -> date:
    """Same Eastern calendar day the 17:05 Team Lead Chat EOD verifies."""
    return eastern_calendar_day(moment)


def morning_report_target(moment: datetime) -> date:
    """Previous weekday in America/New_York.

    The morning wrapper does not call this. It omits ``--date`` so
    ``production_main`` keeps its own previous-business-day default. Tests use
    this function as the explicit contrast with evening collection.
    """
    return previous_weekday(eastern_calendar_day(moment))


def skip_if_prepared_reuses(ready_dates: set[date], target: date) -> bool:
    """True only when the prepared snapshot for ``target`` itself is ready.

    A ready snapshot for another date, including the prior business day, does
    not satisfy ``--skip-if-prepared`` for ``target``.
    """
    return target in ready_dates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--evening",
        action="store_true",
        help="Print today's America/New_York calendar day for evening collect.",
    )
    group.add_argument(
        "--morning",
        action="store_true",
        help=(
            "Print the previous weekday in America/New_York. "
            "The 09:00 wrapper does not use this flag."
        ),
    )
    args = parser.parse_args(argv)
    now = datetime.now(EASTERN)
    target = evening_collection_target(now) if args.evening else morning_report_target(now)
    print(target.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
