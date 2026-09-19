from copy import deepcopy
from pathlib import Path

import pytest

from scripts.preflight_dedicated_accountability_submission import (
    EXPECTED_RED,
    SubmissionPreflightError,
    _safe_failure_code,
    validate_observation,
)


def _observation():
    record = {
        "applicant": "Sanitized Applicant",
        "assigned_producer": "Sanitized Producer",
        "status": "Quoting",
        "quote_due_date": "2026-07-01",
        "effective_date": "2026-07-15",
        "age_days": 80,
        "submission_url": "https://app.ezlynx.com/web/submission-center/submissions/sanitized-1",
        "red_state_evidence": {
            "overdue_class": True,
            "computed_color": EXPECTED_RED,
            "expected_color": EXPECTED_RED,
        },
        "source_page": 1,
        "source_row": 1,
    }
    return {
        "read_only": True,
        "source_status": "available",
        "scope_time_frame": "All Submissions",
        "scope_assigned_producer": "Streetsmart Insurance",
        "scope_my_submissions": False,
        "run_date": "2026-09-19",
        "qualifying_due_date_on_or_before": "2026-08-19",
        "day_31_qualifies": True,
        "mat_row_count": 100,
        "pager_total_present": True,
        "pager_total": 240,
        "status_aria_sort": "ascending",
        "first_row_non_closed": True,
        "first_row_status": "Quoting",
        "first_closed_row_inspected": True,
        "first_closed_row_index": 1,
        "first_closed_row_page": 1,
        "first_closed_row_status": "Closed - Bound",
        "pages_reviewed": 1,
        "rows_inspected_through_boundary": 2,
        "non_closed_rows_inspected": 1,
        "distinct_non_closed_statuses": ["Quoting"],
        "headers_present": True,
        "open_over_30_count": 1,
        "qualifying_records": [record],
        "counts_by_producer": {"Sanitized Producer": 1},
        "counts_by_status": {"Quoting": 1},
        "candidate_dispositions": [],
        "emails_sent": 0,
        "email_delivery_enabled": False,
    }


def test_valid_live_observation_returns_only_redacted_summary():
    summary = validate_observation(_observation())
    assert summary["verified"] is True
    assert summary["open_over_30_count"] == 1
    assert len(summary["evidence_sha256"]) == 64
    assert "Sanitized Applicant" not in str(summary)
    assert "Sanitized Producer" not in str(summary)
    assert "submission_url" not in str(summary)


def test_proven_zero_is_allowed_only_with_complete_boundary_evidence():
    observed = _observation()
    observed["qualifying_records"] = []
    observed["open_over_30_count"] = 0
    observed["counts_by_producer"] = {}
    observed["counts_by_status"] = {}
    summary = validate_observation(observed)
    assert summary["open_over_30_count"] == 0
    assert summary["pagination_boundary_verified"] is True


def test_fully_exhausted_pager_is_a_complete_terminal_boundary():
    observed = _observation()
    observed.update({
        "first_closed_row_inspected": False,
        "full_dataset_exhausted": True,
        "first_closed_row_index": None,
        "first_closed_row_page": None,
        "first_closed_row_status": "",
        "pager_total": 1,
        "pages_reviewed": 1,
        "rows_inspected_through_boundary": 1,
        "non_closed_rows_inspected": 1,
    })
    summary = validate_observation(observed)
    assert summary["pagination_boundary_verified"] is True
    assert summary["boundary_kind"] == "pager_exhausted"


def test_exhausted_pager_must_reconcile_every_row():
    observed = _observation()
    observed.update({
        "first_closed_row_inspected": False,
        "full_dataset_exhausted": True,
        "first_closed_row_index": None,
        "first_closed_row_page": None,
        "first_closed_row_status": "",
        "pager_total": 2,
        "rows_inspected_through_boundary": 1,
        "non_closed_rows_inspected": 1,
    })
    with pytest.raises(SubmissionPreflightError, match="did not reconcile"):
        validate_observation(observed)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("scope_assigned_producer",), "My Submissions"),
        (("mat_row_count",), 10),
        (("first_closed_row_inspected",), False),
        (("day_31_qualifies",), False),
        (("qualifying_records", 0, "age_days"), 30),
        (("qualifying_records", 0, "red_state_evidence", "overdue_class"), False),
        (("qualifying_records", 0, "red_state_evidence", "computed_color"), "rgb(0, 0, 0)"),
    ],
)
def test_rejects_incomplete_or_incorrect_evidence(path, value):
    observed = _observation()
    target = observed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(SubmissionPreflightError):
        validate_observation(observed)


def test_rejects_duplicate_submission_links():
    observed = _observation()
    duplicate = deepcopy(observed["qualifying_records"][0])
    observed["qualifying_records"].append(duplicate)
    observed["open_over_30_count"] = 2
    observed["counts_by_producer"] = {"Sanitized Producer": 2}
    observed["counts_by_status"] = {"Quoting": 2}
    with pytest.raises(SubmissionPreflightError, match="duplicated"):
        validate_observation(observed)


def test_workflows_run_two_attempt_submission_preflight_before_delivery():
    root = Path(__file__).resolve().parents[1]
    diagnostic = (
        root / ".github" / "workflows" / "diagnose-accountability-dedicated.yml"
    ).read_text(encoding="utf-8")
    manual = (
        root / ".github" / "workflows" / "run-accountability-now.yml"
    ).read_text(encoding="utf-8")
    script = "preflight_dedicated_accountability_submission.py"
    assert script in diagnostic
    assert script in manual
    assert "timeout-minutes: 30" in diagnostic
    assert "--reliability-attempts 2" in diagnostic
    assert "--reliability-attempts 2" in manual
    diagnostic_step = diagnostic.index(
        "Verify reusable EZLynx Submission Center session without delivery"
    )
    diagnostic_upload = diagnostic.index("Upload read-only diagnostic evidence")
    assert diagnostic_step < diagnostic_upload
    manual_step = manual.index(script)
    service_step = manual.index("Run the dedicated accountability service")
    assert manual_step < service_step


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("PLAYWRIGHT_BLOCKED: agency scope did not apply", "agency_scope_not_applied"),
        ("PLAYWRIGHT_BLOCKED: 100-row selection rendered 10 mat-row elements", "page_size_rows_mismatch"),
        ("PLAYWRIGHT_BLOCKED: next Submission Center page did not load", "next_page_failed"),
        ("NEEDS_AUTH", "session_not_authenticated"),
    ],
)
def test_failure_codes_are_bounded_and_privacy_safe(message, expected):
    assert _safe_failure_code(RuntimeError(message)) == expected


def test_unknown_failure_does_not_echo_exception_message():
    secret_message = "applicant and credential must never be emitted"
    assert _safe_failure_code(RuntimeError(secret_message)) == "RuntimeError"
