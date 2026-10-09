"""Tests for robie_job_engine/utica_login.py (Utica First Okta + email MFA).

Replaces Ralph's tests/test_utica_login.py (commit e7c0e439), which was lost
with the corrupted fix-utica-auto-login bundle. Fixture-level only: no
portal, no Gmail, no Secret Manager.
"""
import base64
import os
import unittest
from unittest.mock import patch

from robie_job_engine import utica_login
from robie_job_engine.intake_core import IntakeHold


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(msg_id: str, received_ms: int, body: str, *, mime="text/plain", nested=False) -> dict:
    part = {"mimeType": mime, "body": {"data": _b64(body)}}
    payload = {"mimeType": "multipart/alternative", "parts": [part]}
    if nested:
        payload = {"mimeType": "multipart/mixed", "parts": [payload]}
    return {"id": msg_id, "internalDate": str(received_ms), "payload": payload}


class _Exec:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def execute(self):
        if self.error:
            raise self.error
        return self.value


class FakeGmail:
    """users().messages().list/get shaped like googleapiclient."""

    def __init__(self, messages, *, list_error=None):
        self._messages = {m["id"]: m for m in messages}
        self._order = [m["id"] for m in messages]
        self.list_error = list_error
        self.list_calls = 0
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        self.list_calls += 1
        self.queries.append(q)
        if self.list_error:
            return _Exec(error=self.list_error)
        return _Exec({"messages": [{"id": i} for i in self._order[:maxResults]]})

    def get(self, userId, id, format):
        return _Exec(self._messages[id])


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class CodeExtractionTests(unittest.TestCase):
    def test_extract_six_digit_code(self):
        self.assertEqual(utica_login.extract_code("Your code is 245764. It expires soon."), "245764")

    def test_no_code_returns_empty(self):
        self.assertEqual(utica_login.extract_code("No digits here, only 12345 and 1234567"), "")

    def test_redacted_code_holds(self):
        with self.assertRaises(IntakeHold) as ctx:
            utica_login.extract_code("Your code is [credential:2f6c-11aa] ")
        self.assertIn("redacted", str(ctx.exception))

    def test_message_text_walks_nested_html(self):
        msg = _message("m1", 0, "<p>Code: <b>245764</b></p>", mime="text/html", nested=True)
        text = utica_login.message_text(msg["payload"])
        self.assertIn("245764", text)
        self.assertNotIn("<b>", text)


class VerificationCodeTests(unittest.TestCase):
    def test_returns_newest_code_sent_after_click(self):
        clock = FakeClock()
        sent_ms = int(clock.now * 1000)
        gmail = FakeGmail([
            _message("old", sent_ms - 10 * 60 * 1000, "Code 111111"),
            _message("new", sent_ms + 5_000, "Code 245764"),
        ])
        code = utica_login.get_verification_code(
            30, not_before=clock.now, service=gmail, sleep=clock.sleep, clock=clock
        )
        self.assertEqual(code, "245764")
        self.assertIn('from:DoNotReply@uticafirst.com', gmail.queries[0])

    def test_ignores_codes_from_before_the_send_click(self):
        clock = FakeClock()
        sent_ms = int(clock.now * 1000)
        gmail = FakeGmail([_message("old", sent_ms - 10 * 60 * 1000, "Code 111111")])
        with self.assertRaises(IntakeHold) as ctx:
            utica_login.get_verification_code(
                12, not_before=clock.now, service=gmail, sleep=clock.sleep, clock=clock
            )
        self.assertIn("within timeout", str(ctx.exception))
        self.assertGreaterEqual(gmail.list_calls, 2)

    def test_redacted_body_holds_immediately_without_polling(self):
        clock = FakeClock()
        gmail = FakeGmail([_message("r", int(clock.now * 1000), "Code [credential:abc]")])
        with self.assertRaises(IntakeHold) as ctx:
            utica_login.get_verification_code(
                120, not_before=clock.now, service=gmail, sleep=clock.sleep, clock=clock
            )
        self.assertIn("redacted", str(ctx.exception))
        self.assertEqual(clock.sleeps, [])

    def test_transient_gmail_error_keeps_polling_then_times_out(self):
        clock = FakeClock()
        gmail = FakeGmail([], list_error=RuntimeError("503"))
        with self.assertRaises(IntakeHold):
            utica_login.get_verification_code(
                10, not_before=clock.now, service=gmail, sleep=clock.sleep, clock=clock
            )
        self.assertGreaterEqual(gmail.list_calls, 2)

    def test_unconfigured_gmail_holds_with_clear_message(self):
        with patch.dict(os.environ, {"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": ""}):
            with self.assertRaises(IntakeHold) as ctx:
                utica_login.get_verification_code(1)
        self.assertIn("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT not set", str(ctx.exception))

    def test_delegated_service_is_read_only_on_the_mfa_mailbox(self):
        calls = []

        def fake_build(sa, user, *, scopes):
            calls.append((sa, user, tuple(scopes)))
            return "svc"

        env = {"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "sa@example.test", "UTICA_MFA_MAILBOX": ""}
        with patch.dict(os.environ, env), \
                patch("robie_job_engine.gmail_accountability.build_keyless_delegated_service", fake_build):
            self.assertEqual(utica_login._get_gmail_service(), "svc")
        self.assertEqual(
            calls,
            [("sa@example.test", "carlo@streetsmart.insurance", ("https://www.googleapis.com/auth/gmail.readonly",))],
        )


# --- login flow -------------------------------------------------------------


class _Loc:
    def __init__(self, page, key, present=True):
        self.page, self.key, self.present = page, key, present

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.present else 0

    def is_visible(self):
        return self.present

    def fill(self, value):
        self.page.filled.append((self.key, value))

    def click(self):
        self.page.clicked.append(self.key)
        self.page.on_click(self.key)

    def wait_for(self, state=None, timeout=None):
        if not self.present:
            raise TimeoutError(self.key)

    def inner_text(self):
        return self.page.body


class _Keyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.keys.append(key)


class FakeOktaPage:
    """Okta -> (optional email MFA) -> UFirst Now, driven by clicks."""

    def __init__(self, *, mfa: bool, final_ok: bool = True):
        self.mfa = mfa
        self.final_ok = final_ok
        self.url = "https://www.uticafirst.com/"
        self.body = "Utica First Insurance"
        self.stage = "home"
        self.filled, self.clicked, self.keys, self.visited = [], [], [], []
        self.keyboard = _Keyboard(self)

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        return ""

    def get_by_text(self, text, exact=False):
        key = getattr(text, "pattern", text)
        return _Loc(self, f"text:{key}")

    def locator(self, selector):
        if selector == "body":
            return _Loc(self, "body")
        if "password" in selector and self.stage == "okta":
            return _Loc(self, "password")
        if "identifier" in selector:
            return _Loc(self, "username", present=self.stage == "okta")
        if selector == "input[name='credentials.passcode']":
            return _Loc(self, "passcode", present=self.stage == "code")
        if "submit" in selector:
            return _Loc(self, "submit")
        return _Loc(self, selector, present=False)

    def on_click(self, key):
        if key.startswith("text:UFirst Now"):
            self.url = "https://login.uticafirst.com/signin"
            self.stage = "okta"
            self.body = "Sign in"
        elif key == "submit" and self.stage == "okta" and any(k == "password" for k, _ in self.filled):
            if self.mfa:
                self.stage = "verify"
                self.body = "Verify with your email"
            else:
                self._land()
        elif key.startswith("text:enter a verification code instead"):
            self.stage = "code"
        elif key == "submit" and self.stage == "code":
            self._land()

    def _land(self):
        if self.final_ok:
            self.url = "https://ufirstnow.uticafirst.com/oneshield/sso?osst=T"
            self.body = "Welcome, Carlo Ferrara"
            self.stage = "portal"


def _secrets(name):
    return {"uticafirst_username": "agent-user", "uticafirst_password": "agent-pass"}[name]


class LoginFlowTests(unittest.TestCase):
    def test_trusted_session_logs_in_without_reading_gmail(self):
        page = FakeOktaPage(mfa=False)

        def reader(**kwargs):
            raise AssertionError("Gmail must not be read without an MFA prompt")

        with patch.object(utica_login, "_get_secret", _secrets):
            utica_login.login_utica(page, code_reader=reader)
        self.assertTrue(utica_login.is_logged_in(page))
        self.assertIn(("password", "agent-pass"), page.filled)

    def test_email_mfa_reads_code_sent_after_click_and_submits_it(self):
        page = FakeOktaPage(mfa=True)
        seen = {}

        def reader(*, max_wait, not_before):
            seen["not_before"] = not_before
            return "245764"

        with patch.object(utica_login, "_get_secret", _secrets):
            utica_login.login_utica(page, code_reader=reader, clock=lambda: 4242.0)
        self.assertEqual(seen["not_before"], 4242.0)
        self.assertIn(("passcode", "245764"), page.filled)
        self.assertTrue(utica_login.is_logged_in(page))

    def test_mfa_hold_propagates_and_nothing_is_typed_as_code(self):
        page = FakeOktaPage(mfa=True)

        def reader(**kwargs):
            raise IntakeHold(utica_login.MFA_UNCONFIGURED)

        with patch.object(utica_login, "_get_secret", _secrets):
            with self.assertRaises(IntakeHold):
                utica_login.login_utica(page, code_reader=reader)
        self.assertFalse(any(key == "passcode" for key, _ in page.filled))

    def test_not_landing_on_portal_holds(self):
        page = FakeOktaPage(mfa=False, final_ok=False)
        with patch.object(utica_login, "_get_secret", _secrets):
            with self.assertRaises(IntakeHold) as ctx:
                utica_login.login_utica(page, code_reader=lambda **k: "000000")
        self.assertIn("not on UFirst Now", str(ctx.exception))

    def test_missing_credentials_hold_before_navigation(self):
        page = FakeOktaPage(mfa=False)
        with patch.object(utica_login, "_get_secret", lambda name: ""):
            with self.assertRaises(IntakeHold):
                utica_login.login_utica(page)
        self.assertEqual(page.visited, [])

    def test_is_logged_in_rejects_other_hosts_and_login_paths(self):
        page = FakeOktaPage(mfa=False)
        page.body = "Welcome"
        page.url = "https://login.uticafirst.com/app"
        self.assertFalse(utica_login.is_logged_in(page))
        page.url = "https://ufirstnow.uticafirst.com/sso/login"
        self.assertFalse(utica_login.is_logged_in(page))
        page.url = "https://ufirstnow.uticafirst.com/oneshield/sso?osst=T"
        self.assertTrue(utica_login.is_logged_in(page))


if __name__ == "__main__":
    unittest.main()
