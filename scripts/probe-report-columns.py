#!/usr/bin/env python3
"""Read-only probe: EZLynx report column headers for human schema verification.

Usage: probe-report-columns.py [report_id]   (default: 4359)

Bypasses the report-registry schema gate ON PURPOSE — this is how we obtain
the actual columns for a human to verify. Does NOT open a registry run, does
NOT create a job, does NOT write to EZLynx. Exports the report CSV, prints
ONLY the header row as JSON, then deletes the file. No row data is printed.

Fails closed on auth failure, navigation failure, or missing filter scope.
"""
from __future__ import annotations

import csv
import json
import sys

sys.path.insert(0, "/opt/streetsmart-hermes/robie-job-engine")

from robie_job_engine.ezlynx_session import (  # noqa: E402
    PlaywrightEzlynxSession,
    ensure_ezlynx_session,
)
from robie_job_engine.report_fetcher import (  # noqa: E402
    REPORT_DOWNLOAD_DIR,
    _cdp_url,
    _export_looker_report_csv,
    _page_of,
)
from robie_job_engine.report_registry import get_report_spec  # noqa: E402


def main() -> None:
    report_id = (sys.argv[1] if len(sys.argv) > 1 else "4359").strip()
    spec = get_report_spec(report_id)  # registry lookup only; no run opened
    run_id = f"probe-{report_id}-columns"
    dest = REPORT_DOWNLOAD_DIR / f"robie-report-{report_id}-{run_id}.csv"

    browser = PlaywrightEzlynxSession(_cdp_url())
    export_error: str | None = None
    try:
        ensure_ezlynx_session(browser)
        page = _page_of(browser)
        try:
            _export_looker_report_csv(
                page,
                spec=spec,
                run={"run_id": run_id},
                download_dir=REPORT_DOWNLOAD_DIR,
                fields=None,
            )
        except RuntimeError as exc:
            # The CSV was still saved; keep the error and read headers anyway.
            export_error = f"{type(exc).__name__}: {exc}"
        with open(dest, newline="", encoding="utf-8-sig") as fh:
            columns = next(csv.reader(fh), [])
        print(
            json.dumps(
                {
                    "report_id": report_id,
                    "report_name": spec.name,
                    "filter_name": spec.filter_name,
                    "identity_fields": list(spec.identity_fields),
                    "columns": columns,
                    "column_count": len(columns),
                    "export_error": export_error,
                },
                indent=2,
            )
        )
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
