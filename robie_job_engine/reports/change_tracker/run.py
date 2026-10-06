"""Weekly Change Request Tracker — run the full pipeline.

Exact run command (see README.md):

    python -m robie_job_engine.reports.change_tracker.run \\
        --csv /tmp/4659-export.csv --total-open 12 --total-requests 15 \\
        --week-tab 9/28-10/4 --dry-run

Live-fetch variant (needs the box EZLynx session + sheet token):

    python -m robie_job_engine.reports.change_tracker.run \\
        --live --db-path /opt/streetsmart-hermes/jobs.db --dry-run

--dry-run never writes to the sheet (no service is even built). Without
--dry-run the script fails closed unless the canonical spreadsheet id is
configured (X1 pending) and the week tab is empty (or --force is given).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import sheets as sheets_mod
from .config import DEFAULT_CONFIG, ChangeTrackerConfig
from .fetch_4659 import fetch_4659_export
from .transform import (
    ChangeTrackerError,
    CountMismatchError,
    WeekStats,
    build_week_matrix,
    week_tab_name,
)


# P10 (LOCKED, Sandeep 2026-10-05): on a count mismatch against the "Total
# Open Change Requests" tile, retry the download up to 3 attempts total,
# then fail closed (exit 2). Only the --live path can retry (a --csv export
# is a fixed file; re-reading it would change nothing).
MAX_DOWNLOAD_ATTEMPTS = 3


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weekly Policy Change Request Tracker pipeline"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--csv",
        help="Path to a 4659 CSV export (fixture or manual download). "
        "Same code path as the live fetch, minus the browser.",
    )
    src.add_argument(
        "--live",
        action="store_true",
        help="Drive the 4659 Looker UI over CDP and export the CSV. "
        "Requires an EZLynx session on the box.",
    )
    parser.add_argument("--total-open", help="Total Open Change Requests tile value")
    parser.add_argument("--total-requests", help="Total Policy Change Requests tile value")
    parser.add_argument(
        "--week-tab",
        default="",
        help="Weekly tab name (default: week just closed per config, e.g. 9/28-10/4)",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Job DB path for the report-run registry (required with --live)",
    )
    parser.add_argument("--dry-run", action="store_true", help="No sheet writes")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild a week tab that already holds data (idempotency override)",
    )
    parser.add_argument(
        "--spreadsheet-id",
        default="",
        help="Override the canonical spreadsheet id (default: config/env)",
    )
    parser.add_argument(
        "--token-file", default="", help="Google OAuth token file override"
    )
    parser.add_argument(
        "--stats-json", default="", help="Write the run stats JSON to this path"
    )
    return parser


def _fetch_and_build_live(
    db_path: str, config: ChangeTrackerConfig
) -> tuple[str, list[list[str]], WeekStats, int]:
    """Download the 4659 export, dedupe, and run the P10 tile count check,
    retrying the download on a count mismatch (P10: up to 3 attempts total).

    Returns (csv_text, matrix, stats, attempts). Raises CountMismatchError
    when all attempts still mismatch; any other ChangeTrackerError is not
    retryable and propagates immediately.
    """
    last_mismatch: CountMismatchError | None = None
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        csv_text, total_requests, total_open = fetch_4659_export(db_path=db_path)
        print(
            f"tiles: total_requests={total_requests} total_open={total_open} "
            f"(download attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS})"
        )
        try:
            matrix, stats = build_week_matrix(
                csv_text,
                total_requests=total_requests,
                total_open=total_open,
                config=config,
            )
        except CountMismatchError as exc:
            last_mismatch = exc
            print(
                f"attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS}: count mismatch "
                f"({exc}); {'retrying download' if attempt < MAX_DOWNLOAD_ATTEMPTS else 'no attempts left'}",
                file=sys.stderr,
            )
            continue
        print(f"count cross-check passed on attempt {attempt}")
        return csv_text, matrix, stats, attempt
    assert last_mismatch is not None
    raise last_mismatch


def _run(argv: list[str] | None, config: ChangeTrackerConfig) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.spreadsheet_id.strip():
        import dataclasses
        config = dataclasses.replace(
            config, spreadsheet_id=args.spreadsheet_id.strip()
        )

    week_tab = args.week_tab.strip() or week_tab_name(config=config)
    print(f"week_tab={week_tab}")
    print(f"mode={'dry-run' if args.dry_run else 'LIVE'}")

    download_attempts = 1
    if args.csv:
        csv_text = Path(args.csv).read_text(encoding="utf-8-sig")
        total_requests = args.total_requests
        total_open = args.total_open
        if total_requests is None or total_open is None:
            print(
                "fail closed: --csv mode requires --total-requests and --total-open "
                "(the two summary-tile values for the count cross-check, P10)",
                file=sys.stderr,
            )
            return 2
        # --csv mode: a fixed file, so no download retry; the count
        # cross-check inside build_week_matrix fails closed immediately (P10).
        matrix, stats = build_week_matrix(
            csv_text,
            total_requests=total_requests,
            total_open=total_open,
            config=config,
        )
    else:
        if not args.db_path.strip():
            print("fail closed: --live requires --db-path", file=sys.stderr)
            return 2
        csv_text, matrix, stats, download_attempts = _fetch_and_build_live(
            db_path=args.db_path.strip(), config=config
        )
    print(
        f"built: rows_raw={stats.rows_raw} rows_after_dedupe={stats.rows_after_dedupe} "
        f"teams={stats.teams} stale_rows={stats.stale_rows} "
        f"duplicates_dropped={stats.duplicates_dropped}"
    )
    print(f"matrix_rows={len(matrix)} (incl. header + team header rows)")

    if args.stats_json.strip():
        Path(args.stats_json).write_text(
            json.dumps(
                {
                    "week_tab": week_tab,
                    "dry_run": bool(args.dry_run),
                    "download_attempts": download_attempts,
                    "rows_raw": stats.rows_raw,
                    "rows_after_dedupe": stats.rows_after_dedupe,
                    "rows_open": stats.rows_open,
                    "teams": stats.teams,
                    "stale_rows": stats.stale_rows,
                    "duplicates_dropped": stats.duplicates_dropped,
                    "dropped_not_open": stats.dropped_not_open,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    if args.dry_run:
        # Print the matrix so a human can eyeball it; no service is built, no writes.
        for row in matrix:
            print(" | ".join(row))
        write_result = sheets_mod.write_week_tab(
            _DryRunService(),  # placeholder; dry-run path returns before touching it
            "<spreadsheet-id-unresolved>",
            week_tab,
            matrix,
            dry_run=True,
            force=args.force,
            config=config,
        )
        print(f"DRY_RUN_OK wrote={write_result['wrote']}")
        return 0

    spreadsheet_id = sheets_mod.resolve_spreadsheet_id(config)
    service, auth_via = sheets_mod.build_service(args.token_file.strip() or None)
    print(f"auth_via={auth_via}")
    write_result = sheets_mod.write_week_tab(
        service, spreadsheet_id, week_tab, matrix,
        dry_run=False, force=args.force, config=config,
    )
    print(json.dumps(write_result, indent=2))
    print("WRITE_OK")
    return 0


def main(argv: list[str] | None = None, config: ChangeTrackerConfig = DEFAULT_CONFIG) -> int:
    """CLI entry: fail closed with exit 2 and a clean message (no traceback)."""
    try:
        return _run(argv, config)
    except ChangeTrackerError as exc:
        print(f"FAIL_CLOSED: {exc}", file=sys.stderr)
        return 2


class _DryRunService:
    """Sentinel: any attribute access means dry-run touched the service."""

    def __getattr__(self, name):
        raise AssertionError(f"dry-run must not touch the sheets service: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
