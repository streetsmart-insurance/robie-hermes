"""Per-day phone dedupe (process doc Phase 2, Step 1).

One row per unique phone number per day, even when a number had multiple
separate missed calls that day (voicemail + hang-up included). The earliest
call of the day is kept as the row's representative; the rest are counted in
the summary.

Scope rule from the doc: dedupe applies only to rows this run is adding. It
is NEVER applied retroactively to older tabs -- and because sheet writes are
append-only, this module only ever dedupes the freshly pulled calls, never
sheet rows.
"""

from __future__ import annotations

from .models import MissedCall


def dedupe_by_day(calls: list[MissedCall]) -> tuple[list[MissedCall], int]:
    """Return (unique_calls, duplicates_removed)."""
    by_number: dict[str, MissedCall] = {}
    removed = 0
    for call in sorted(calls, key=lambda c: c.start_time):
        existing = by_number.get(call.from_digits)
        if existing is None:
            call.duplicate_count = 1
            by_number[call.from_digits] = call
        else:
            existing.duplicate_count += 1
            removed += 1
    return list(by_number.values()), removed
