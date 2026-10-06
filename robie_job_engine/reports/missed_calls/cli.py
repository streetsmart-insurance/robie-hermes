"""CLI for the Missed Call Report automation.

Examples:
  # Dry run (default, safe): pull yesterday's log, match, print what WOULD happen.
  python -m robie_job_engine.reports.missed_calls.cli

  # Date rule is automatic (every date since the last business day the agency
  # was open, M3); explicit dates override it.
  python -m robie_job_engine.reports.missed_calls.cli --dates 2026-10-02,2026-10-03

  # Real write (canonical workbook confirmed 2026-10-05, X1; --dry-run is
  # still the default, so nothing writes until you pass --no-dry-run):
  python -m robie_job_engine.reports.missed_calls.cli --no-dry-run

Exit codes: 0 ok (dry-run included), 2 fail-closed (aborted before/while
writing), 1 unexpected error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import date_rules
from .models import RunSummary
from .phone_index import PhoneIndex
from .pipeline import PipelineError, run
from .sheet_io import CANONICAL_SPREADSHEET_ID, SPREADSHEET_TITLE, SheetIO

DEFAULT_INDEX_PATH = (
    "/opt/streetsmart-phone-watchdog/data/phone_index_fullbook.json"
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Missed Call Report: RingCentral pull -> dedupe -> "
        "offline phone match -> dated sheet tabs (append-only)."
    )
    p.add_argument(
        "--date",
        default=None,
        help="Run date YYYY-MM-DD in America/New_York (default: today). "
        "Target dates derive from this via the Monday rule.",
    )
    p.add_argument(
        "--dates",
        default=None,
        help="Explicit comma-separated target dates YYYY-MM-DD (overrides the "
        "automatic rule below).",
    )
    p.add_argument(
        "--holidays",
        default=None,
        help="Comma-separated extra agency-closed dates YYYY-MM-DD, added to "
        "the built-in federal-holiday list (M3).",
    )
    p.add_argument(
        "--ignore-default-holidays",
        action="store_true",
        help="Do not treat built-in federal holidays as agency-closed.",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Default. Pull + match + report, write NOTHING to the sheet.",
    )
    p.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Actually append rows to the sheet (canonical workbook confirmed "
        "2026-10-05, X1; the write path itself is still untested — first "
        "live run must be eyeballed).",
    )
    p.add_argument(
        "--spreadsheet-id",
        default=CANONICAL_SPREADSHEET_ID,
        help="Workbook id. Defaults to the confirmed canonical workbook "
        f"{CANONICAL_SPREADSHEET_ID} (X1, locked 2026-10-05); override to "
        "target a different workbook.",
    )
    p.add_argument(
        "--spreadsheet-title",
        default=SPREADSHEET_TITLE,
        help="Workbook title for Drive lookup.",
    )
    p.add_argument(
        "--index-path",
        default=os.environ.get("MISSED_CALL_PHONE_INDEX", DEFAULT_INDEX_PATH),
        help="Phone index JSON path.",
    )
    p.add_argument(
        "--index-max-age-days",
        type=int,
        default=7,
        help="Index older than this fails closed (default 7).",
    )
    p.add_argument(
        "--extension-map",
        default=None,
        help="Optional JSON file: {extension_or_number: employee_name} passed "
        "to the RingCentral client for department attribution (M4).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit run summaries as JSON on stdout.",
    )
    return p


def _parse_dates(args: argparse.Namespace) -> list[date]:
    if args.dates:
        out = []
        for piece in args.dates.split(","):
            piece = piece.strip()
            if piece:
                out.append(date.fromisoformat(piece))
        if not out:
            raise PipelineError("--dates produced no valid dates")
        return out
    if args.date:
        run_date = date.fromisoformat(args.date)
    else:
        run_date = datetime.now(date_rules.AGENCY_TZ).date()
    extra = frozenset(
        date.fromisoformat(p.strip())
        for p in (args.holidays or "").split(",")
        if p.strip()
    )
    return date_rules.target_dates(
        run_date,
        extra_holidays=extra,
        use_default_holidays=not args.ignore_default_holidays,
    )


def _client_factory(extension_map_path: str | None):
    def factory():
        # Lazy import: the module stays importable without the full engine.
        from robie_job_engine.ringcentral_client import RingCentralClient

        mapping: dict[str, str] = {}
        if extension_map_path:
            mapping = json.loads(Path(extension_map_path).read_text(encoding="utf-8"))
        # Credentials: RINGCENTRAL_CLIENT_ID / _SECRET / _JWT / _SERVER_URL
        # (GCP Secret Manager key names: ringcentral-accountability-*).
        client = RingCentralClient.from_env()
        if not client.is_configured():
            raise PipelineError(
                "RingCentral client is not configured: set RINGCENTRAL_CLIENT_ID, "
                "RINGCENTRAL_CLIENT_SECRET, RINGCENTRAL_JWT, "
                "RINGCENTRAL_SERVER_URL (or run on the box job identity)."
            )
        client.employee_mapping = mapping
        return client

    return factory


def _print_summary(summary: RunSummary) -> None:
    states = ", ".join(f"{k}={v}" for k, v in sorted(summary.lookup_states.items()))
    print(f"--- {summary.target_date} (tab {summary.tab_name}) ---")
    print(f"  inbound seen: {summary.inbound_calls_seen}")
    print(f"  missed/voicemail: {summary.missed_calls_seen}")
    print(f"  unique numbers: {summary.unique_numbers} "
          f"(duplicates removed: {summary.duplicates_removed}, "
          f"cross-date: {summary.cross_date_duplicates_removed})")
    print(f"  lookup states: {states or 'n/a'}")
    if summary.unmapped_departments:
        print(f"  unmapped departments (blank cells; forward to Sandeep): "
              f"{', '.join(summary.unmapped_departments)}")
    print(f"  existing rows skipped: {summary.existing_rows_skipped}")
    print(f"  rows appended: {summary.rows_appended} "
          f"({'dry-run' if summary.dry_run else 'LIVE'})")
    for note in summary.notes:
        print(f"  note: {note}")
    for err in summary.errors:
        print(f"  ERROR: {err}")
    if summary.fail_closed:
        print("  FAIL-CLOSED: aborted; no partial sheet writes for this date")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        dates = _parse_dates(args)
    except (ValueError, PipelineError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    index = PhoneIndex(
        args.index_path, max_age_days=args.index_max_age_days
    )
    sheet = SheetIO(
        spreadsheet_id=args.spreadsheet_id,
        spreadsheet_title=args.spreadsheet_title,
        dry_run=args.dry_run,
    )

    summaries = run(_client_factory(args.extension_map), index, sheet, dates)

    if args.json:
        import dataclasses

        print(json.dumps([dataclasses.asdict(s) for s in summaries], indent=2))
    else:
        print(f"index: {json.dumps(index.stats())}")
        print(f"sheet: title={sheet.spreadsheet_title!r} "
              f"id={sheet.spreadsheet_id or '(Drive lookup at write time)'} "
              f"dry_run={args.dry_run}")
        for summary in summaries:
            _print_summary(summary)

    if any(s.fail_closed for s in summaries):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
