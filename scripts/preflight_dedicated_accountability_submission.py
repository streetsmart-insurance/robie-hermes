#!/usr/bin/env python3
"""Run a privacy-safe, read-only Production Submission Center preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


EXPECTED_ROOT = Path("/opt/streetsmart-daily-accountability")
CLOSED = {"Closed - Not Sold", "Closed - Bound"}
EXPECTED_RED = "rgb(211, 47, 47)"
REQUIRED_EXACT = {
    "read_only": True,
    "source_status": "available",
    "scope_time_frame": "All Submissions",
    "scope_assigned_producer": "Streetsmart Insurance",
    "scope_my_submissions": False,
    "mat_row_count": 100,
    "pager_total_present": True,
    "status_aria_sort": "ascending",
    "first_row_non_closed": True,
    "day_31_qualifies": True,
    "headers_present": True,
}


class SubmissionPreflightError(RuntimeError):
    """The live Submission Center evidence did not satisfy the release gate."""


SAFE_FAILURE_CODES = (
    ("Persistent Robie Chrome is unavailable", "chrome_unavailable"),
    ("Persistent Robie Chrome has no browser context", "browser_context_missing"),
    ("NEEDS_AUTH", "session_not_authenticated"),
    ("agency options not found", "agency_options_missing"),
    ("agency field not found", "agency_field_missing"),
    ("agency scope did not apply", "agency_scope_not_applied"),
    ("My Submissions remained selected", "my_submissions_still_selected"),
    ("paginator not found", "paginator_missing"),
    ("page-size control not found", "page_size_control_missing"),
    ("100 page-size option not found", "page_size_option_missing"),
    ("100-row selection rendered", "page_size_rows_mismatch"),
    ("page-size control did not retain 100", "page_size_not_retained"),
    ("paginator did not confirm rows 1-100", "page_size_range_mismatch"),
    ("Status header not found", "status_header_missing"),
    ("Status cell missing", "status_cell_missing"),
    ("ascending sort showed a closed first row", "status_boundary_wrong"),
    ("Status sort did not reach ascending", "status_sort_failed"),
    ("live pager total not found", "pager_total_missing"),
    ("live pager range not found", "pager_range_missing"),
    ("header not found", "required_header_missing"),
    ("row is missing required cells", "required_cell_missing"),
    ("next-page control not found", "next_page_control_missing"),
    ("non-closed group exceeded", "pagination_boundary_missing"),
    ("next Submission Center page did not load", "next_page_failed"),
    ("page rendered no statuses", "page_statuses_missing"),
    ("no visible non-closed first row", "non_closed_boundary_missing"),
)


def _safe_failure_code(exc: Exception) -> str:
    message = str(exc).splitlines()[0]
    for fragment, code in SAFE_FAILURE_CODES:
        if fragment in message:
            return code
    return type(exc).__name__


def _require_int(observed: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = observed.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SubmissionPreflightError(f"{key} was not an integer >= {minimum}")
    return value


def validate_observation(observed: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(observed, Mapping):
        raise SubmissionPreflightError("collector result was not a mapping")

    mismatches = [
        key for key, expected in REQUIRED_EXACT.items()
        if observed.get(key) != expected
    ]
    if mismatches:
        raise SubmissionPreflightError(
            "required evidence mismatch: " + ", ".join(sorted(mismatches))
        )

    pager_total = _require_int(observed, "pager_total", minimum=1)
    pages_reviewed = _require_int(observed, "pages_reviewed", minimum=1)
    rows_inspected = _require_int(
        observed, "rows_inspected_through_boundary", minimum=1
    )
    non_closed = _require_int(
        observed, "non_closed_rows_inspected", minimum=1
    )
    open_count = _require_int(observed, "open_over_30_count", minimum=0)
    closed_boundary = observed.get("first_closed_row_inspected") is True
    exhausted_boundary = observed.get("full_dataset_exhausted") is True
    if closed_boundary == exhausted_boundary:
        raise SubmissionPreflightError(
            "exactly one Submission Center terminal boundary was required"
        )
    if closed_boundary:
        first_closed_page = _require_int(
            observed, "first_closed_row_page", minimum=1
        )
        _require_int(observed, "first_closed_row_index", minimum=0)
        if first_closed_page > pages_reviewed:
            raise SubmissionPreflightError("first closed row was outside reviewed pages")
        if rows_inspected != non_closed + 1:
            raise SubmissionPreflightError(
                "rows inspected did not reconcile through the first closed row"
            )
        if str(observed.get("first_closed_row_status") or "") not in CLOSED:
            raise SubmissionPreflightError("first closed-row status was not recognized")
        boundary_kind = "first_closed_row"
    else:
        if rows_inspected != pager_total or non_closed != pager_total:
            raise SubmissionPreflightError(
                "fully exhausted pager rows did not reconcile"
            )
        if observed.get("first_closed_row_page") is not None:
            raise SubmissionPreflightError("exhausted pager reported a closed-row page")
        if observed.get("first_closed_row_index") is not None:
            raise SubmissionPreflightError("exhausted pager reported a closed-row index")
        if str(observed.get("first_closed_row_status") or ""):
            raise SubmissionPreflightError("exhausted pager reported a closed-row status")
        boundary_kind = "pager_exhausted"
    if rows_inspected > pager_total:
        raise SubmissionPreflightError("rows inspected exceeded the live pager total")

    records = observed.get("qualifying_records")
    if not isinstance(records, list) or len(records) != open_count:
        raise SubmissionPreflightError("qualifying record count did not reconcile")

    urls: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise SubmissionPreflightError("qualifying record was not a mapping")
        url = str(record.get("submission_url") or "").strip()
        if not url.startswith("https://app.ezlynx.com/") or url in urls:
            raise SubmissionPreflightError(
                "qualifying submission links were missing, foreign, or duplicated"
            )
        urls.add(url)
        if str(record.get("status") or "") in CLOSED:
            raise SubmissionPreflightError("closed record appeared in qualifying results")
        age_days = record.get("age_days")
        if isinstance(age_days, bool) or not isinstance(age_days, int) or age_days <= 30:
            raise SubmissionPreflightError("day-31 threshold was not enforced")
        red = record.get("red_state_evidence")
        if not isinstance(red, Mapping):
            raise SubmissionPreflightError("red-state evidence was missing")
        if red.get("overdue_class") is not True:
            raise SubmissionPreflightError("live overdue class was not present")
        if str(red.get("computed_color") or "") != EXPECTED_RED:
            raise SubmissionPreflightError("live overdue color did not match")
        source_page = record.get("source_page")
        if (
            isinstance(source_page, bool)
            or not isinstance(source_page, int)
            or source_page < 1
            or source_page > pages_reviewed
        ):
            raise SubmissionPreflightError("record source page was outside reviewed pages")

    for key in ("counts_by_producer", "counts_by_status"):
        counts = observed.get(key)
        if not isinstance(counts, Mapping):
            raise SubmissionPreflightError(f"{key} was missing")
        values = list(counts.values())
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise SubmissionPreflightError(f"{key} contained an invalid count")
        if sum(values) != open_count:
            raise SubmissionPreflightError(f"{key} did not reconcile")

    canonical = json.dumps(observed, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "verified": True,
        "read_only": True,
        "agency_scope_verified": True,
        "all_submissions_verified": True,
        "page_size_verified": True,
        "pagination_boundary_verified": True,
        "boundary_kind": boundary_kind,
        "red_overdue_verified": True,
        "day_31_verified": True,
        "pages_reviewed": pages_reviewed,
        "rows_inspected_through_boundary": rows_inspected,
        "pager_total": pager_total,
        "open_over_30_count": open_count,
        "evidence_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def run_preflight(app_root: Path, reliability_attempts: int) -> dict[str, Any]:
    root = app_root.expanduser().resolve()
    if root != EXPECTED_ROOT or not (root / "src" / "production_main.py").is_file():
        raise SubmissionPreflightError("unexpected or incomplete accountability app root")
    if reliability_attempts < 2:
        raise SubmissionPreflightError("at least two reliability attempts are required")

    os.chdir(root)
    sys.path.insert(0, str(root))
    from src.extractors.ezlynx_login_bootstrap import ensure_ezlynx_authenticated
    from src.extractors.ezlynx_submission_browser import audit

    summaries: list[dict[str, Any]] = []
    for attempt in range(1, reliability_attempts + 1):
        try:
            ensure_ezlynx_authenticated()
        except Exception as exc:
            raise SubmissionPreflightError(
                f"attempt_{attempt}_authentication:{_safe_failure_code(exc)}"
            ) from exc
        try:
            observed = audit(fresh=True)
        except Exception as exc:
            raise SubmissionPreflightError(
                f"attempt_{attempt}_audit:{_safe_failure_code(exc)}"
            ) from exc
        try:
            summaries.append(validate_observation(observed))
        except SubmissionPreflightError as exc:
            raise SubmissionPreflightError(
                f"attempt_{attempt}_validation:{_safe_failure_code(exc)}"
            ) from exc

    stable_keys = (
        "pager_total",
        "pages_reviewed",
        "rows_inspected_through_boundary",
        "open_over_30_count",
        "evidence_sha256",
        "boundary_kind",
    )
    baseline = tuple(summaries[0][key] for key in stable_keys)
    if any(tuple(item[key] for key in stable_keys) != baseline for item in summaries[1:]):
        raise SubmissionPreflightError(
            "live Submission Center evidence changed between reliability attempts"
        )

    return {
        "ready": True,
        "authenticated": True,
        "read_only": True,
        "delivery_attempted": False,
        "records_exposed": False,
        "reliability_attempts_passed": len(summaries),
        "evidence": summaries[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", type=Path, default=EXPECTED_ROOT)
    parser.add_argument("--reliability-attempts", type=int, default=2)
    args = parser.parse_args()
    try:
        result = run_preflight(args.app_root, args.reliability_attempts)
    except Exception as exc:
        safe = str(exc).splitlines()[0]
        stage, separator, code = safe.partition(":")
        print(json.dumps({
            "ready": False,
            "authenticated": "authentication" not in stage,
            "read_only": True,
            "delivery_attempted": False,
            "records_exposed": False,
            "failure_stage": stage if separator else "setup_or_stability",
            "failure_code": code if separator else _safe_failure_code(exc),
        }, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
