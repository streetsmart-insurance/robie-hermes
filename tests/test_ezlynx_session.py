from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from robie_job_engine.ezlynx_session import (
    NAVIGATION_SETTLE_DELAY_SECONDS,
    InteractiveAuthenticationRequired,
    PlaywrightEzlynxSession,
    SessionVerificationFailed,
    SessionState,
    authenticated_app_evidence,
    classify_page_snapshot,
    ensure_ezlynx_session,
    wait_for_post_login_state,
    wait_for_settled_session,
)
from robie_job_engine.secret_manager import EzlynxCredentials, load_ezlynx_credentials


class FakeAccessor:
    def __init__(self):
        self.values = {
            "projects/test/secrets/user/versions/latest": "agent@example.com",
            "projects/test/secrets/password/versions/latest": "correct horse battery staple",
        }
        self.calls = []

    def access(self, resource_name):
        self.calls.append(resource_name)
        return self.values[resource_name]


class FakeBrowser:
    def __init__(self, initial, after_login=SessionState.SIGNED_IN):
        self.initial = initial
        self.after_login = after_login
        self.login_calls = []

    def state(self):
        return self.initial

    def login(self, username, password):
        self.login_calls.append((username, password))
        return self.after_login


class EzlynxSessionTests(unittest.TestCase):
    def refs(self):
        return patch.dict(os.environ, {
            "ROBIE_EZLYNX_USERNAME_SECRET": "projects/test/secrets/user/versions/latest",
            "ROBIE_EZLYNX_PASSWORD_SECRET": "projects/test/secrets/password/versions/latest",
        }, clear=False)

    def test_signed_in_session_never_reads_secrets(self):
        browser = FakeBrowser(SessionState.SIGNED_IN)
        accessor = FakeAccessor()
        with self.refs():
            result = ensure_ezlynx_session(browser, accessor=accessor)
        self.assertEqual(result, SessionState.SIGNED_IN)
        self.assertEqual(accessor.calls, [])
        self.assertEqual(browser.login_calls, [])

    def test_logged_out_session_reads_both_secrets_and_logs_in(self):
        browser = FakeBrowser(SessionState.LOGIN_REQUIRED)
        accessor = FakeAccessor()
        with self.refs():
            result = ensure_ezlynx_session(browser, accessor=accessor)
        self.assertEqual(result, SessionState.SIGNED_IN)
        self.assertEqual(len(accessor.calls), 2)
        self.assertEqual(len(browser.login_calls), 1)

    def test_interactive_auth_does_not_read_secrets(self):
        browser = FakeBrowser(SessionState.INTERACTIVE_AUTH_REQUIRED)
        accessor = FakeAccessor()
        with self.refs(), self.assertRaises(InteractiveAuthenticationRequired):
            ensure_ezlynx_session(browser, accessor=accessor)
        self.assertEqual(accessor.calls, [])

    def test_unrecognized_ezlynx_page_fails_closed_without_reading_secrets(self):
        browser = FakeBrowser(SessionState.UNVERIFIED)
        accessor = FakeAccessor()
        with self.refs(), self.assertRaises(SessionVerificationFailed):
            ensure_ezlynx_session(browser, accessor=accessor)
        self.assertEqual(accessor.calls, [])

    def test_arbitrary_ezlynx_url_is_not_authenticated_evidence(self):
        self.assertFalse(authenticated_app_evidence(
            "https://app.ezlynx.com/error",
            internal_web_links=0,
            login_controls=0,
        ))

    def test_authenticated_web_shell_requires_fresh_internal_navigation(self):
        self.assertTrue(authenticated_app_evidence(
            "https://app.ezlynx.com/web/submission-center/overview/submissions",
            internal_web_links=16,
            login_controls=0,
        ))
        self.assertFalse(authenticated_app_evidence(
            "https://app.ezlynx.com/web/submission-center/overview/submissions",
            internal_web_links=0,
            login_controls=0,
        ))

    def test_secret_repr_is_redacted(self):
        credentials = EzlynxCredentials("visible-user", "visible-password")
        rendered = repr(credentials)
        self.assertNotIn("visible-user", rendered)
        self.assertNotIn("visible-password", rendered)

    def test_post_login_state_retries_while_the_login_url_lingers(self):
        seen = iter([
            SessionState.LOGIN_REQUIRED,
            SessionState.LOGIN_REQUIRED,
            SessionState.SIGNED_IN,
        ])
        delays = []
        state = wait_for_post_login_state(
            lambda: next(seen),
            attempts=6,
            delay_seconds=3,
            sleeper=delays.append,
        )
        self.assertEqual(state, SessionState.SIGNED_IN)
        self.assertEqual(delays, [3, 3])

    def test_post_login_state_returns_mfa_without_waiting(self):
        delays = []
        state = wait_for_post_login_state(
            lambda: SessionState.INTERACTIVE_AUTH_REQUIRED,
            attempts=6,
            delay_seconds=3,
            sleeper=delays.append,
        )
        self.assertEqual(state, SessionState.INTERACTIVE_AUTH_REQUIRED)
        self.assertEqual(delays, [])

    def test_unsettled_app_url_is_not_a_decision(self):
        self.assertIsNone(classify_page_snapshot(
            "https://app.ezlynx.com/web/dashboard",
            "",
            internal_web_links=0,
            login_controls=0,
        ))

    def test_settled_wait_returns_logged_out_after_spa_redirect(self):
        snapshots = iter([
            {
                "url": "https://app.ezlynx.com/web/dashboard",
                "body": "",
                "internal_web_links": 0,
                "login_controls": 0,
            },
            {
                "url": "https://app.ezlynx.com/auth/account/login",
                "body": "Login",
                "internal_web_links": 0,
                "login_controls": 2,
            },
        ])
        delays = []
        state = wait_for_settled_session(
            lambda: next(snapshots),
            attempts=4,
            delay_seconds=1,
            sleeper=delays.append,
        )
        self.assertEqual(state, SessionState.LOGIN_REQUIRED)
        self.assertEqual(delays, [1])

    def test_settled_wait_returns_dashboard_without_calling_it_unverified(self):
        snapshots = iter([
            {
                "url": "https://app.ezlynx.com/web/",
                "body": "loading",
                "internal_web_links": 0,
                "login_controls": 0,
            },
            {
                "url": "https://app.ezlynx.com/web/dashboard",
                "body": "Dashboard",
                "internal_web_links": 4,
                "login_controls": 0,
            },
        ])
        delays = []
        state = wait_for_settled_session(
            lambda: next(snapshots),
            attempts=4,
            delay_seconds=1,
            sleeper=delays.append,
        )
        self.assertEqual(state, SessionState.SIGNED_IN)
        self.assertEqual(delays, [1])

    def test_settled_wait_gives_up_as_unverified(self):
        delays = []
        state = wait_for_settled_session(
            lambda: {
                "url": "https://app.ezlynx.com/web/dashboard",
                "body": "",
                "internal_web_links": 0,
                "login_controls": 0,
            },
            attempts=3,
            delay_seconds=0.5,
            sleeper=delays.append,
        )
        self.assertEqual(state, SessionState.UNVERIFIED)
        self.assertEqual(delays, [0.5, 0.5])

    def test_state_waits_for_login_redirect_instead_of_unverified(self):
        session = PlaywrightEzlynxSession.__new__(PlaywrightEzlynxSession)
        session._page = type("Page", (), {"goto": lambda *args, **kwargs: None})()
        snapshots = iter([
            {
                "url": "https://app.ezlynx.com/web/dashboard",
                "body": "",
                "internal_web_links": 0,
                "login_controls": 0,
            },
            {
                "url": "https://app.ezlynx.com/auth/account/login",
                "body": "Login",
                "internal_web_links": 0,
                "login_controls": 3,
            },
        ])
        delays = []
        with patch(
            "robie_job_engine.ezlynx_session.read_playwright_snapshot",
            side_effect=lambda _page: next(snapshots),
        ), patch(
            "robie_job_engine.ezlynx_session.time.sleep",
            side_effect=delays.append,
        ):
            state = session.state()
        self.assertEqual(state, SessionState.LOGIN_REQUIRED)
        self.assertEqual(delays, [NAVIGATION_SETTLE_DELAY_SECONDS])

    def test_state_navigation_failure_is_unverified_without_waiting(self):
        session = PlaywrightEzlynxSession.__new__(PlaywrightEzlynxSession)

        class _Page:
            def goto(self, *args, **kwargs):
                raise TimeoutError("navigation")

        session._page = _Page()
        with patch(
            "robie_job_engine.ezlynx_session.read_playwright_snapshot",
            side_effect=AssertionError("read"),
        ), patch(
            "robie_job_engine.ezlynx_session.time.sleep",
            side_effect=AssertionError("sleep"),
        ):
            state = session.state()
        self.assertEqual(state, SessionState.UNVERIFIED)

    def test_missing_secret_references_fail_before_access(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            load_ezlynx_credentials(FakeAccessor())


if __name__ == "__main__":
    unittest.main()
