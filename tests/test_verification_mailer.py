"""Unit tests for robie_job_engine.verification_mailer. Fakes only — no sends."""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.modules.pop("robie_job_engine.verification_common", None)

from _sibling_fakes import ensure_real_module

from robie_job_engine import accountability_delivery

# Sibling worker test modules install a fake robie_job_engine.verification_mailer
# in sys.modules at THEIR import time, and pytest imports every test module
# before running any test. Evict any such fake so `vm` binds the REAL
# implementation under test. (The faking modules already bound their fakes
# into their worker namespaces at their own import time, so this does not
# disturb them; their teardown becomes a safe no-op for this entry.)
vm = ensure_real_module("robie_job_engine.verification_mailer")


class _FakeSend:
    def __init__(self, message_id="msg-123"):
        self.message_id = message_id
        self.captured = {}

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId=None, body=None):
        self.captured["userId"] = userId
        self.captured["body"] = body
        return self

    def execute(self):
        return {"id": self.message_id}


def _env(**overrides):
    base = {
        "ROBIE_VERIFICATION_MAIL_SENDER": "robie@streetsmart.insurance",
        "ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT": "robie-mailer@test.iam.gserviceaccount.com",
    }
    base.update(overrides)
    return base


class MailerFailClosedTests(unittest.TestCase):
    def test_missing_service_account_config_fails_closed(self):
        env = {
            key: value
            for key, value in _env().items()
            if key
            not in (
                "ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT",
                "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT",
            )
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                vm.send_verification_email(
                    to=["carlo@streetsmart.insurance"],
                    cc=[],
                    subject="s",
                    text_body="b",
                )

    def test_invalid_recipients_rejected(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=True):
            with self.assertRaises(ValueError):
                vm.send_verification_email(
                    to=["not-an-address"], cc=[], subject="s", text_body="b"
                )
            with self.assertRaises(ValueError):
                vm.send_verification_email(to=[], cc=[], subject="s", text_body="b")

    def test_empty_subject_and_body_rejected(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=True):
            with self.assertRaises(ValueError):
                vm.send_verification_email(
                    to=["a@b.test"], cc=[], subject=" ", text_body="b"
                )
            with self.assertRaises(ValueError):
                vm.send_verification_email(
                    to=["a@b.test"], cc=[], subject="s", text_body="  "
                )


class MailerSendTests(unittest.TestCase):
    def setUp(self):
        self._orig = accountability_delivery._delegated_gmail_sender
        self.fake = _FakeSend()
        accountability_delivery._delegated_gmail_sender = lambda *args: self.fake

    def tearDown(self):
        accountability_delivery._delegated_gmail_sender = self._orig

    def test_send_builds_message_and_returns_receipt(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=True):
            receipt = vm.send_verification_email(
                to=["carlo@streetsmart.insurance"],
                cc=["jake@streetsmart.insurance"],
                subject="Verification digest",
                text_body="plain body",
                html_body="<p>html body</p>",
            )
        self.assertEqual(receipt["kind"], "gmail")
        self.assertEqual(receipt["message_id"], "msg-123")
        self.assertEqual(receipt["sender"], "robie@streetsmart.insurance")
        self.assertEqual(receipt["recipient_count"], 1)
        self.assertEqual(receipt["cc_count"], 1)
        # no addresses/subjects/bodies leak via the receipt
        self.assertNotIn("carlo@", str(receipt))

        import base64
        from email.parser import BytesParser
        from email.policy import default

        raw = base64.urlsafe_b64decode(self.fake.captured["body"]["raw"])
        parsed = BytesParser(policy=default).parsebytes(raw)
        self.assertEqual(parsed["From"], "robie@streetsmart.insurance")
        self.assertEqual(parsed["To"], "carlo@streetsmart.insurance")
        self.assertEqual(parsed["Cc"], "jake@streetsmart.insurance")
        self.assertEqual(parsed["Subject"], "Verification digest")
        self.assertIn("plain body", parsed.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("<p>html body</p>", parsed.get_body(preferencelist=("html",)).get_content())

    def test_logs_never_carry_addresses_subjects_or_bodies(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=True):
            with self.assertLogs("robie.verification_mailer", level="INFO") as logs:
                vm.send_verification_email(
                    to=["carlo@streetsmart.insurance"],
                    cc=[],
                    subject="Super secret subject line",
                    text_body="Super secret body content",
                )
        combined = "\n".join(logs.output)
        self.assertNotIn("carlo@", combined)
        self.assertNotIn("Super secret subject line", combined)
        self.assertNotIn("Super secret body content", combined)
        self.assertIn("recipient_count", combined)

    def test_send_failure_raises(self):
        class _Boom:
            def users(self):
                raise RuntimeError("gmail down")

        accountability_delivery._delegated_gmail_sender = lambda *args: _Boom()
        with unittest.mock.patch.dict(os.environ, _env(), clear=True):
            with self.assertRaises(RuntimeError):
                vm.send_verification_email(
                    to=["a@b.test"], cc=[], subject="s", text_body="b"
                )


class MailerIsolationTests(unittest.TestCase):
    """Regression: sibling worker tests install a fake
    ``robie_job_engine.verification_mailer`` in ``sys.modules`` at their
    import time. The mailer tests must still exercise the REAL implementation
    (fail-closed validation, gmail receipts), never the fake."""

    def test_sibling_fake_does_not_capture_mailer_binding(self):
        import types

        from _sibling_fakes import install_fake, teardown_fakes

        registry: dict = {}
        fake = types.ModuleType("robie_job_engine.verification_mailer")
        fake.send_verification_email = lambda **kwargs: {"sent": True}
        # Simulate a sibling worker test module installing its fake.
        install_fake(registry, "robie_job_engine.verification_mailer", fake)
        try:
            real = ensure_real_module("robie_job_engine.verification_mailer")
            # The resolved module is the real implementation, not the fake.
            self.assertIsNotNone(getattr(real, "__file__", None))
            self.assertIsNot(real, fake)
            # And it behaves like the real mailer: fail-closed validation.
            with self.assertRaises(ValueError):
                real.send_verification_email(
                    to=["not-an-address"], cc=[], subject="s", text_body="b"
                )
        finally:
            teardown_fakes(registry)

    def test_module_binding_is_real_implementation(self):
        # `vm` (bound at this module's import) must be the real mailer even
        # when the full suite runs with sibling fakes installed.
        self.assertIsNotNone(getattr(vm, "__file__", None))
        with self.assertRaises(ValueError):
            vm.send_verification_email(
                to=["not-an-address"], cc=[], subject="s", text_body="b"
            )


if __name__ == "__main__":
    unittest.main()
