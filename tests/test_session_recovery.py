#!/usr/bin/env python3
"""Tests for session_recovery. stdlib unittest — no browser, no network, no box.

The recovery path is what stands between "EZLynx is logged out" and a dead
job: it must re-authenticate through Secret Manager when it can, and fail
closed with a named marker — never an exception, never a credential leak —
when it cannot.
"""
from __future__ import annotations

import fcntl
import os
import unittest
from pathlib import Path

try:
    from durable_temp import durable_temporary_directory
except ModuleNotFoundError:
    from tests.durable_temp import durable_temporary_directory

from robie_job_engine.ezlynx_session import (
    InteractiveAuthenticationRequired,
    SessionState,
    SessionVerificationFailed,
)
from robie_job_engine.session_recovery import (
    INTERACTIVE_AUTH_REQUIRED,
    RECOVERED,
    RECOVERY_ERROR,
    RECOVERY_FAILED,
    attempt_session_recovery,
    recovery_summary,
)


class FakeBrowser:
    def __init__(self, url: str):
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


def signed_in_ensurer(browser):
    return SessionState.SIGNED_IN


class TestAttemptSessionRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.lock_path = str(Path(self.tmp.name) / "ezlynx-session.lock")
        self._old_lock_env = os.environ.get("ROBIE_EZLYNX_SESSION_LOCK")
        os.environ["ROBIE_EZLYNX_SESSION_LOCK"] = self.lock_path

    def tearDown(self):
        if self._old_lock_env is None:
            os.environ.pop("ROBIE_EZLYNX_SESSION_LOCK", None)
        else:
            os.environ["ROBIE_EZLYNX_SESSION_LOCK"] = self._old_lock_env
        self.tmp.cleanup()

    def test_successful_recovery_reports_signed_in(self):
        result = attempt_session_recovery(
            browser_factory=FakeBrowser,
            session_ensurer=signed_in_ensurer,
        )
        self.assertTrue(result["recovered"])
        self.assertEqual(result["state"], SessionState.SIGNED_IN.value)
        self.assertEqual(result["marker"], RECOVERED)

    def test_interactive_auth_is_reported_not_raised(self):
        def need_mfa(browser):
            raise InteractiveAuthenticationRequired("EZLynx requires MFA")

        result = attempt_session_recovery(
            browser_factory=FakeBrowser,
            session_ensurer=need_mfa,
        )
        self.assertFalse(result["recovered"])
        self.assertTrue(
            result["reason"].startswith(INTERACTIVE_AUTH_REQUIRED),
            result["reason"],
        )

    def test_failed_verification_is_reported_not_raised(self):
        def bad_state(browser):
            raise SessionVerificationFailed("not an authenticated shell")

        result = attempt_session_recovery(
            browser_factory=FakeBrowser,
            session_ensurer=bad_state,
        )
        self.assertFalse(result["recovered"])
        self.assertTrue(result["reason"].startswith(RECOVERY_FAILED), result["reason"])

    def test_unexpected_error_is_reported_not_raised(self):
        def boom(browser):
            raise RuntimeError("cdp went away")

        result = attempt_session_recovery(
            browser_factory=FakeBrowser,
            session_ensurer=boom,
        )
        self.assertFalse(result["recovered"])
        self.assertTrue(result["reason"].startswith(RECOVERY_ERROR), result["reason"])
        self.assertIn("RuntimeError", result["reason"])

    def test_browser_factory_error_is_reported_not_raised(self):
        def no_browser(url):
            raise ImportError("playwright is required")

        result = attempt_session_recovery(browser_factory=no_browser)
        self.assertFalse(result["recovered"])
        self.assertTrue(result["reason"].startswith(RECOVERY_ERROR), result["reason"])

    def test_lock_timeout_is_reported_not_raised(self):
        # Hold the session lock so recovery cannot take it.
        with open(self.lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                result = attempt_session_recovery(
                    browser_factory=FakeBrowser,
                    session_ensurer=signed_in_ensurer,
                    lock_timeout_seconds=0.2,
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertFalse(result["recovered"])
        self.assertIn("EZLYNX_SESSION_LOCK_TIMEOUT", result["reason"])

    def test_browser_is_closed_after_attempt(self):
        created = []

        def factory(url):
            browser = FakeBrowser(url)
            created.append(browser)
            return browser

        attempt_session_recovery(
            browser_factory=factory,
            session_ensurer=signed_in_ensurer,
        )
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


class TestRecoverySummary(unittest.TestCase):
    def test_summary_for_success(self):
        self.assertIn(
            "recovered automatically",
            recovery_summary({"recovered": True, "state": "SIGNED_IN"}),
        )

    def test_summary_for_failure(self):
        self.assertIn(
            "INTERACTIVE_AUTH_REQUIRED",
            recovery_summary(
                {"recovered": False, "reason": "INTERACTIVE_AUTH_REQUIRED: MFA"}
            ),
        )

    def test_summary_for_none(self):
        self.assertIn("failed", recovery_summary(None))


if __name__ == "__main__":
    unittest.main()
