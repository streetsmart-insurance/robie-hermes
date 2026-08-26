from __future__ import annotations

import unittest
from unittest.mock import patch

from robie_job_engine.models import JobStatus
from robie_job_engine.submission_audit import (
    BoundedProcessError,
    DEFAULT_SCOPE,
    EzlynxSubmissionAuditWorker,
    PYTHON,
    _runner_command,
)


OBSERVED = {
    "read_only": True,
    "scope_time_frame": "All Submissions",
    "scope_assigned_producer": "Streetsmart Insurance",
    "scope_my_submissions": False,
    "mat_row_count": 100,
    "pager_total_present": True,
    "pager_total": 4751,
    "status_aria_sort": "ascending",
    "first_row_non_closed": True,
    "first_row_status": "Not Submitted",
    "first_closed_row_inspected": True,
    "first_closed_row_index": 55,
    "first_closed_row_status": "Closed - Not Sold",
    "rows_inspected_through_boundary": 56,
    "distinct_non_closed_statuses": ["Not Submitted", "Submitted"],
    "headers_present": True,
}


def _job():
    return {
        "payload": {
            "read_only": True,
            "scope": DEFAULT_SCOPE,
            "expected_postcondition": {
                "mat_row_count": 100,
                "pager_total_present": True,
                "status_aria_sort": "ascending",
                "first_row_non_closed": True,
                "first_closed_row_inspected": True,
            },
        }
    }


class SubmissionAuditRuntimeTests(unittest.TestCase):
    def test_playwright_runner_starts_as_package_module(self):
        self.assertEqual(
            _runner_command(fresh=False),
            [
                PYTHON,
                "-m",
                "robie_job_engine.submission_audit_runner",
                "--reuse",
            ],
        )

    @patch("robie_job_engine.submission_audit.run_submission_read", return_value=OBSERVED)
    @patch("robie_job_engine.submission_audit.ensure_ezlynx_login")
    @patch("robie_job_engine.submission_audit.exclusive_session")
    def test_worker_uses_allowlisted_login_and_returns_structured_read_only_action(
        self, session_lock, login, read
    ):
        result = EzlynxSubmissionAuditWorker().perform(_job(), idempotency_key="audit-1")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.action, "ezlynx.submission_audit")
        self.assertEqual(result.destination["expected_postcondition"], OBSERVED)
        login.assert_called_once_with()
        read.assert_called_once_with(fresh=False)

    @patch(
        "robie_job_engine.submission_audit.ensure_ezlynx_login",
        side_effect=BoundedProcessError("MAILBOX_IDENTITY_MISMATCH"),
    )
    @patch("robie_job_engine.submission_audit.exclusive_session")
    def test_wrong_mailbox_fails_closed_as_needs_auth(self, session_lock, login):
        result = EzlynxSubmissionAuditWorker().perform(_job(), idempotency_key="audit-2")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_AUTH)
        self.assertEqual(result.error, "MAILBOX_IDENTITY_MISMATCH")

    @patch("robie_job_engine.submission_audit.ensure_ezlynx_login")
    @patch(
        "robie_job_engine.submission_audit.run_submission_read",
        side_effect=BoundedProcessError("PLAYWRIGHT_TIMEOUT_RECOVERED"),
    )
    @patch("robie_job_engine.submission_audit.exclusive_session")
    def test_playwright_timeout_is_recoverable_and_never_claims_success(
        self, session_lock, read, login
    ):
        result = EzlynxSubmissionAuditWorker().perform(_job(), idempotency_key="audit-3")
        self.assertFalse(result.succeeded)
        self.assertTrue(result.retryable)
        self.assertEqual(result.error, "PLAYWRIGHT_TIMEOUT_RECOVERED")

    @patch("robie_job_engine.submission_audit.ensure_ezlynx_login")
    @patch(
        "robie_job_engine.submission_audit.run_submission_read",
        side_effect=BoundedProcessError(
            "PLAYWRIGHT_BLOCKED: required form field not found"
        ),
    )
    @patch("robie_job_engine.submission_audit.exclusive_session")
    def test_playwright_blocker_waits_for_bound_human_input(
        self, session_lock, read, login
    ):
        result = EzlynxSubmissionAuditWorker().perform(
            _job(), idempotency_key="audit-hitl"
        )
        self.assertFalse(result.succeeded)
        self.assertFalse(result.retryable)
        self.assertEqual(result.hold_status, JobStatus.AWAITING_HUMAN_INPUT)


if __name__ == "__main__":
    unittest.main()
