"""Pipeline orchestration: per target date, pull -> dedupe -> lookup -> rows -> sheet.

Fail-closed semantics (X3 assumption, pending Sandeep):
  * Any fail-closed precondition (RingCentral auth/fetch failure, phone index
    missing or stale) aborts BEFORE any sheet write for that date, and the run
    exits non-zero. Partial results are reported, never silently shipped.
  * Sheet appends are atomic per date (one append call). If the append fails,
    nothing is retried implicitly; the error is reported.
  * "Was addressed?" is never written by automation (human-only, finding 2).
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Callable, Optional

from . import date_rules
from .dedupe import dedupe_by_day
from .models import MissedCall, PhoneLookup, RunSummary, SheetRow
from .phone_index import PhoneIndex
from .ringcentral_pull import pull_inbound_missed
from .rows import build_rows, department_for
from .sheet_io import SheetIO

ClientFactory = Callable[[], Any]


class PipelineError(RuntimeError):
    """Fail-closed pipeline error."""


def run_for_date(
    client: Any,
    index: PhoneIndex,
    sheet: SheetIO,
    target: date,
    skip_numbers: frozenset[str] = frozenset(),
) -> RunSummary:
    """Run the full pipeline for one target date.

    ``skip_numbers``: normalized phones already emitted on an EARLIER date's
    tab this run (M5: a number appears on the report only once). Those calls
    are dropped here and counted in ``cross_date_duplicates_removed``.
    """
    summary = RunSummary(
        target_date=target.isoformat(),
        tab_name=date_rules.tab_name(target),
        dry_run=sheet.dry_run,
    )

    # 1. Pull.
    start_utc, end_utc = date_rules.day_bounds_utc(target)
    try:
        missed, inbound_seen = pull_inbound_missed(client, start_utc, end_utc)
    except Exception as exc:  # fail closed: no pull, no writes, non-zero exit
        summary.fail_closed = True
        summary.errors.append(f"RingCentral pull failed: {exc}")
        return summary
    summary.inbound_calls_seen = inbound_seen
    summary.missed_calls_seen = len(missed)

    # 2. Fail closed on the phone index BEFORE any sheet interaction.
    if not index.available:
        summary.fail_closed = True
        summary.errors.append(
            f"phone index unavailable ({index.load_error or 'stale'}); "
            "refusing to label numbers No Account"
        )
        return summary

    # 3. Dedupe (per day), then drop numbers already emitted on an
    # earlier date's tab this run (M5, locked 2026-10-05: report-wide "only
    # once"; the earliest date's occurrence wins).
    unique, removed = dedupe_by_day(missed)
    summary.duplicates_removed = removed
    fresh: list[MissedCall] = []
    for call in unique:
        if call.from_digits in skip_numbers:
            summary.cross_date_duplicates_removed += 1
        else:
            fresh.append(call)
    unique = fresh
    summary.unique_numbers = len(unique)
    summary.emitted_numbers = [c.from_digits for c in unique]

    # 4. Offline lookup for each unique number.
    lookups: dict[str, PhoneLookup] = {}
    state_counts: Counter[str] = Counter()
    for call in unique:
        lookup = index.lookup(call.from_digits)
        lookups[call.from_digits] = lookup
        state_counts[lookup.state] += 1
    summary.lookup_states = dict(state_counts)

    # 5. Build rows.
    rows: list[SheetRow] = build_rows(unique, lookups)

    # 5b. M4 (locked 2026-10-05): department attribution lives on the AppSheet
    # app (administration > employees); unmappable cells stay blank and
    # Sandeep gets an email in that case. The pipeline does not send the
    # email — it surfaces the unmapped extensions in the run output so the
    # operator can forward them.
    unmapped = sorted(
        {c.extension.strip() or "(no extension)" for c in unique if not department_for(c)}
    )
    summary.unmapped_departments = unmapped
    if unmapped:
        summary.notes.append(
            "unmapped departments (blank cells; forward to Sandeep): "
            + ", ".join(unmapped)
        )

    # 6. Sheet: in dry-run, report without touching Sheets at all.
    # Live: ensure tab, then
    #    append only genuinely new phones (append-only, M13/M14).
    new_rows: list[SheetRow] = rows
    summary.rows = list(new_rows)  # for the EOD digest (posted by cli.py)
    if sheet.dry_run:
        summary.existing_rows_skipped = 0
        summary.rows_appended = 0
        summary.notes.append(
            f"dry-run: {len(new_rows)} row(s) would be appended, 0 written; "
            "sheet not contacted"
        )
        return summary
    try:
        tab_status = sheet.ensure_tab(summary.tab_name, year_hint=target.year)
        summary.notes.append(f"tab {summary.tab_name!r}: {tab_status}")
        existing = sheet.existing_phones(summary.tab_name)
        new_rows = [r for r in rows if r.phone_digits not in existing]
        summary.existing_rows_skipped = len(rows) - len(new_rows)
        summary.rows_appended = sheet.append_rows(summary.tab_name, new_rows)
    except Exception as exc:
        summary.fail_closed = True
        summary.errors.append(f"sheet write failed: {exc}")

    return summary


def run(
    client_factory: ClientFactory,
    index: PhoneIndex,
    sheet: SheetIO,
    dates: list[date],
) -> list[RunSummary]:
    """Run the pipeline for each target date (Monday rule = 3 dates).

    The RingCentral client is created once and reused across dates. Dates are
    processed in chronological order and each date skips numbers already
    emitted on an earlier date's tab (M5: report-wide "only once").
    """
    try:
        client = client_factory()
    except Exception as exc:
        summary = RunSummary(target_date="", tab_name="", dry_run=sheet.dry_run)
        summary.fail_closed = True
        summary.errors.append(f"RingCentral client init failed: {exc}")
        return [summary]
    emitted: set[str] = set()
    summaries: list[RunSummary] = []
    for target in sorted(dates):
        summary = run_for_date(client, index, sheet, target, skip_numbers=frozenset(emitted))
        emitted.update(summary.emitted_numbers)
        summaries.append(summary)
    return summaries
