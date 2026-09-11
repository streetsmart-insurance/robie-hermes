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
import os
import sys

ENGINE_CANDIDATES = [
    "/opt/streetsmart-hermes/robie-job-engine",
    "/opt/streetsmart-hermes/current/robie-job-engine",
]


def _find_engine() -> tuple[str, dict]:
    """Locate the engine package; return (path, diagnostics)."""
    diag: dict = {"candidates": {}}
    for cand in ENGINE_CANDIDATES:
        info: dict = {"exists": os.path.isdir(cand)}
        pkg = os.path.join(cand, "robie_job_engine")
        info["package_exists"] = os.path.isdir(pkg)
        if info["package_exists"]:
            try:
                files = sorted(os.listdir(pkg))
            except OSError as exc:
                files = [f"listdir failed: {exc}"]
            info["has_ezlynx_session"] = "ezlynx_session.py" in files
            info["has_report_fetcher"] = "report_fetcher.py" in files
            info["has_report_registry"] = "report_registry.py" in files
            info["file_count"] = len(files)
        diag["candidates"][cand] = info
        if info.get("has_ezlynx_session") and info.get("has_report_fetcher"):
            return cand, diag
    # Fall back to the first existing candidate so the import error is informative.
    for cand in ENGINE_CANDIDATES:
        if diag["candidates"][cand]["exists"]:
            return cand, diag
    print(json.dumps({"engine_diagnostics": diag}, indent=2))
    raise SystemExit("fail closed: no engine candidate with ezlynx_session found")


_ENGINE_PATH, _DIAG = _find_engine()
sys.path.insert(0, _ENGINE_PATH)

try:
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
except ModuleNotFoundError as exc:
    print(json.dumps({"engine_diagnostics": _DIAG, "import_error": str(exc)}, indent=2))
    raise SystemExit(f"fail closed: {exc}")


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
