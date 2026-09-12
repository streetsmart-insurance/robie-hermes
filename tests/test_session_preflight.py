#!/usr/bin/env python3
"""Tests for session_preflight. stdlib unittest — no pytest on the Mac shell.

The payloads below are the real CDP /json/list output captured from
hermes-poc-01 on 2026-09-12, including the two `browser_ui` omnibox entries
that made "the browser has tabs" look true while it showed nothing.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from durable_temp import durable_temporary_directory
except ModuleNotFoundError:
    from tests.durable_temp import durable_temporary_directory

from robie_job_engine.engine import JobEngine
from robie_job_engine.models import (
    JobStatus,
    VerificationEvidence,
    VerificationResult,
    WorkerResult,
)
from robie_job_engine.session_preflight import (
    LOGGED_OUT,
    NO_EZLYNX_TAB,
    NO_TABS,
    SESSION_PRESENT,
    UNREACHABLE,
    check,
    classify,
)
from robie_job_engine.store import JobStore

LOGIN_TAB = {
    "id": "E7FD9BADD6E996313D607D2BD08D00DD",
    "title": "Login",
    "type": "page",
    "url": "https://app.ezlynx.com/auth/account/login?redirectURL=https%3A%2F%2Fapp.ezlynx.com%2Fweb%2F",
    "faviconUrl": "https://app.ezlynx.com/web/assets/ezlynx/favicon.svg",
}

OMNIBOX_A = {
    "id": "E141DAE7E061001EB0CD6E1037674242",
    "title": "Omnibox Popup",
    "type": "browser_ui",
    "url": "chrome://omnibox-popup.top-chrome/",
}

OMNIBOX_B = {
    "id": "BBF89891F30EFE927FB348284730702F",
    "title": "Omnibox Popup",
    "type": "browser_ui",
    "url": "chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html",
}

POLICIES_TAB = {
    "id": "AAAA",
    "title": "ROBIE Test LLC - Policies",
    "type": "page",
    "url": "https://app.ezlynx.com/web/account/220250093/policies",
}


class TestClassifyLoggedOut(unittest.TestCase):
    def test_real_production_payload_is_logged_out(self):
        result = classify([LOGIN_TAB, OMNIBOX_A, OMNIBOX_B])
        self.assertEqual(result["state"], LOGGED_OUT)
        self.assertTrue(result["blocking"])

    def test_blocker_reason_names_the_condition_not_a_symptom(self):
        result = classify([LOGIN_TAB])
        self.assertIn("SESSION_LOGGED_OUT", result["reason"])
        self.assertIn("Secret Manager", result["reason"])
        self.assertNotIn("DISPLAY", result["reason"])

    def test_evidence_carries_the_login_url(self):
        result = classify([LOGIN_TAB, OMNIBOX_A])
        self.assertEqual(len(result["login_urls"]), 1)
        self.assertIn("/auth/account/login", result["login_urls"][0])

    def test_login_detected_from_path_even_when_title_differs(self):
        tab = dict(LOGIN_TAB, title="EZLynx")
        self.assertEqual(classify([tab])["state"], LOGGED_OUT)

    def test_login_detected_from_title_when_host_matches(self):
        tab = {"type": "page", "title": "Login", "url": "https://app.ezlynx.com/web/"}
        self.assertEqual(classify([tab])["state"], LOGGED_OUT)

    def test_login_titled_page_on_another_host_is_not_our_login(self):
        tab = {"type": "page", "title": "Login", "url": "https://accounts.google.com/signin"}
        self.assertEqual(classify([tab])["state"], NO_EZLYNX_TAB)


class TestClassifyNonBlocking(unittest.TestCase):
    def test_authenticated_page_is_session_present(self):
        result = classify([POLICIES_TAB, OMNIBOX_A])
        self.assertEqual(result["state"], SESSION_PRESENT)
        self.assertFalse(result["blocking"])

    def test_session_present_does_not_claim_proof(self):
        result = classify([POLICIES_TAB])
        self.assertIn("not proof", result["reason"])

    def test_one_live_tab_beside_a_login_tab_does_not_block(self):
        # A second window mid-login should not kill a job that has a working tab.
        result = classify([LOGIN_TAB, POLICIES_TAB])
        self.assertEqual(result["state"], SESSION_PRESENT)
        self.assertFalse(result["blocking"])

    def test_browser_ui_entries_alone_count_as_no_tabs(self):
        result = classify([OMNIBOX_A, OMNIBOX_B])
        self.assertEqual(result["state"], NO_TABS)
        self.assertFalse(result["blocking"])

    def test_empty_list_is_no_tabs(self):
        self.assertEqual(classify([])["state"], NO_TABS)

    def test_non_ezlynx_page_is_not_blocking(self):
        tab = {"type": "page", "title": "New Tab", "url": "about:blank"}
        result = classify([tab])
        self.assertEqual(result["state"], NO_EZLYNX_TAB)
        self.assertFalse(result["blocking"])

    def test_malformed_entries_are_ignored_not_fatal(self):
        result = classify([None, "junk", 7, LOGIN_TAB])
        self.assertEqual(result["state"], LOGGED_OUT)


class TestCheckNeverRaises(unittest.TestCase):
    def test_unreachable_cdp_is_reported_not_raised(self):
        # Port 1 is reserved and never listening.
        result = check("http://127.0.0.1:1", timeout=0.25)
        self.assertEqual(result["state"], UNREACHABLE)

    def test_unreachable_cdp_does_not_block(self):
        # A dead CDP endpoint is a different failure with a different fix.
        # Calling it "logged out" sends the next person to the wrong place.
        result = check("http://127.0.0.1:1", timeout=0.25)
        self.assertFalse(result["blocking"])

    def test_unreachable_reason_names_the_endpoint(self):
        result = check("http://127.0.0.1:1", timeout=0.25)
        self.assertIn("127.0.0.1:1", result["reason"])


class DummyWorker:
    def __init__(self, result=None):
        self.called = False
        self.result = result or WorkerResult(True, "test", {})

    def perform(self, job, **kwargs):
        self.called = True
        return self.result


class DummyVerifier:
    def verify(self, job, action):
        return VerificationResult(
            True,
            VerificationEvidence(
                method="TEST",
                source="test",
                authoritative=True,
                expected={},
                observed={},
            ),
        )


class TestEngineWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.tmp.cleanup()

    @patch("robie_job_engine.session_recovery.attempt_session_recovery")
    @patch("robie_job_engine.session_preflight.check")
    def test_browser_required_job_fails_preflight_when_logged_out(
        self, mock_check, mock_recover
    ):
        mock_check.return_value = {
            "state": LOGGED_OUT,
            "blocking": True,
            "reason": "SESSION_LOGGED_OUT: browser is on /auth/account/login",
            "login_urls": ["https://app.ezlynx.com/auth/account/login"],
        }
        mock_recover.return_value = {
            "recovered": False,
            "reason": "INTERACTIVE_AUTH_REQUIRED: EZLynx requires MFA",
        }
        worker = DummyWorker()
        engine = JobEngine(
            self.store,
            {"hermes-cua": worker},
            {"ezlynx.reassign": DummyVerifier()},
        )
        job = self.store.create_job(
            "ezlynx.reassign",
            {"worker": "hermes-cua"},
            max_attempts=3,
        )

        final = engine.run(job["id"])

        self.assertTrue(mock_recover.called, "recovery must be attempted before failing")
        self.assertFalse(worker.called, "_perform must NEVER be called when preflight blocks")
        self.assertEqual(final["status"], JobStatus.FAILED.value)
        self.assertEqual(final["attempt_count"], 0, "attempt_count must remain 0")

        preflight_cp = self.store.get_checkpoint(job["id"], "session_preflight")
        self.assertIsNotNone(preflight_cp)
        self.assertEqual(preflight_cp["state"], LOGGED_OUT)

        recovery_cp = self.store.get_checkpoint(job["id"], "session_recovery")
        self.assertIsNotNone(recovery_cp)
        self.assertFalse(recovery_cp["recovered"])

        email_cp = self.store.get_checkpoint(job["id"], "email_response")
        self.assertIsNotNone(email_cp)
        self.assertIn("SESSION_LOGGED_OUT", email_cp["response_text"])
        self.assertIn("INTERACTIVE_AUTH_REQUIRED", email_cp["body"])
        self.assertEqual(email_cp["subject"], "ROBIE Blocker: EZLynx Session Logged Out")

    @patch("robie_job_engine.session_recovery.attempt_session_recovery")
    @patch("robie_job_engine.session_preflight.check")
    def test_browser_required_job_recovers_session_and_proceeds(
        self, mock_check, mock_recover
    ):
        logged_out = {
            "state": LOGGED_OUT,
            "blocking": True,
            "reason": "SESSION_LOGGED_OUT: browser is on /auth/account/login",
        }
        session_present = {
            "state": SESSION_PRESENT,
            "blocking": False,
            "reason": "An EZLynx tab is not on the login page.",
        }
        mock_check.side_effect = [logged_out, session_present]
        mock_recover.return_value = {
            "recovered": True,
            "state": "SIGNED_IN",
            "marker": "SESSION_RECOVERED",
        }
        worker = DummyWorker()
        engine = JobEngine(
            self.store,
            {"hermes-cua": worker},
            {"ezlynx.reassign": DummyVerifier()},
        )
        job = self.store.create_job(
            "ezlynx.reassign",
            {"worker": "hermes-cua"},
            max_attempts=3,
        )

        engine.run(job["id"])

        self.assertTrue(mock_recover.called, "recovery must be attempted when logged out")
        self.assertTrue(worker.called, "_perform must be called after recovery")
        self.assertIsNotNone(
            self.store.get_checkpoint(job["id"], "session_preflight_recheck")
        )
        recheck_cp = self.store.get_checkpoint(job["id"], "session_preflight_recheck")
        self.assertEqual(recheck_cp["state"], SESSION_PRESENT)
        self.assertIsNone(self.store.get_checkpoint(job["id"], "email_response"))

    @patch("robie_job_engine.session_preflight.check")
    def test_non_browser_job_bypasses_preflight_and_proceeds(self, mock_check):
        mock_check.return_value = {
            "state": LOGGED_OUT,
            "blocking": True,
            "reason": "SESSION_LOGGED_OUT: should not block non-browser job",
        }
        worker = DummyWorker(WorkerResult(True, "robie.official_install", {"installed": True}))
        engine = JobEngine(
            self.store,
            {"deploy-truth": worker},
            {"robie.official_install": DummyVerifier()},
        )
        job = self.store.create_job(
            "robie.official_install",
            {"worker": "deploy-truth"},
            max_attempts=3,
        )

        final = engine.run(job["id"])

        mock_check.assert_not_called()
        self.assertTrue(worker.called, "_perform must be called for non-browser job")
        self.assertNotEqual(final["status"], JobStatus.FAILED.value)
        self.assertIsNone(self.store.get_checkpoint(job["id"], "session_preflight"))


if __name__ == "__main__":
    unittest.main()
