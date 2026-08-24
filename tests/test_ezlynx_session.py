from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from robie_job_engine.ezlynx_session import (
    InteractiveAuthenticationRequired,
    SessionState,
    ensure_ezlynx_session,
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

    def test_secret_repr_is_redacted(self):
        credentials = EzlynxCredentials("visible-user", "visible-password")
        rendered = repr(credentials)
        self.assertNotIn("visible-user", rendered)
        self.assertNotIn("visible-password", rendered)

    def test_missing_secret_references_fail_before_access(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            load_ezlynx_credentials(FakeAccessor())


if __name__ == "__main__":
    unittest.main()
