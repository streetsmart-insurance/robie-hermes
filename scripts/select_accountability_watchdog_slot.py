#!/usr/bin/env python3
"""Select the single DST-correct GitHub schedule for the accountability watchdog."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
WATCHDOG_LOCAL_TIME = time(10, 15)


def expected_schedule(schedule_date: date) -> str:
    local_run = datetime.combine(schedule_date, WATCHDOG_LOCAL_TIME, tzinfo=NEW_YORK)
    utc_run = local_run.astimezone(timezone.utc)
    return f"{utc_run.minute} {utc_run.hour} * * 1-5"


def select_slot(
    *,
    event_name: str,
    event_schedule: str,
    schedule_date: date,
) -> tuple[bool, bool, str]:
    if event_name == "workflow_dispatch":
        return True, False, "manual verification"

    expected = expected_schedule(schedule_date)
    if event_name != "schedule":
        return False, False, f"unsupported event: {event_name}"
    if event_schedule == expected:
        return True, True, f"canonical schedule: {expected}"
    return False, False, f"duplicate UTC slot: {event_schedule}; canonical schedule: {expected}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-schedule", default="")
    parser.add_argument("--schedule-date", required=True)
    parser.add_argument("--github-output", required=True)
    args = parser.parse_args()

    schedule_date = date.fromisoformat(args.schedule_date)
    should_run, qualifying, reason = select_slot(
        event_name=args.event_name,
        event_schedule=args.event_schedule,
        schedule_date=schedule_date,
    )
    with Path(args.github_output).open("a", encoding="utf-8") as handle:
        handle.write(f"should_run={'true' if should_run else 'false'}\n")
        handle.write(f"qualifying_scheduled_proof={'true' if qualifying else 'false'}\n")
    print(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
