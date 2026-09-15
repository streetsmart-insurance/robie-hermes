"""Regression tests: the four workers' shared send path must refuse self-
addressed mail and must not repeat a send already sitting in Sent.

2026-09-14: PRs #423 and #424 fixed the inbox agent's reply path (self-send
exclusion, Sent-folder duplicate check), but the four verification workers
send through send_verification_email(), which had neither guard. These tests
pin the same two rules on the worker path: a worker addressing robie@ fails
closed, and a worker repeating an identical send fails closed instead of
double-sending. Fakes only — no sends.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _sibling_fakes import ensure_real_module

from robie_job_engine import accountability_delivery

# Sibling worker test modules install a fake robie_job_engine.verification_mailer
# in sys.modules at THEIR import time; evict it so `vm` binds the REAL
# implementation under test (same pattern as test_verification_mailer.py).
vm = ensure_real_module("robie_job_engine.verification_mailer")


class _Executable:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeGmail:
    """Fake Gmail service: supports Sent list/get for the duplicate check
    plus send. `sent_messages` are full message dicts as .get() returns them."""

    def __init__(self, sent_messages=(), message_id="msg-123", list_error=None):
        self.sent_messages = list(sent_messages)
        self.message_id = message_id
        self.list_error = list_error
        self.captured = {}

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId=None, q=None, maxResults=None):
        if self.list_error is not None:
            raise self.list_error
        return _Executable({"messages": [{"id": m["id"]} for m in self.sent_messages]})

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        for m in self.sent_messages:
            if m["id"] == id:
                return _Executable(m)
        return _Executable({})

    def send(self, userId=None, body=None):
        self.captured["userId"] = userId
        self.captured["body"] = body
        return _Executable({"id": self.message_id})


def _sent_message(msg_id, to, subject):
    return {
        "id": msg_id,
        "internalDate": str(int(time.time() * 1000)),
        "payload": {
            "headers": [
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ]
        },
    }


def _env(**overrides):
    base = {
        "ROBIE_VERIFICATION_MAIL_SENDER": "robie@streetsmart.insurance",
        "ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT": "robie-mailer@test.iam.gserviceaccount.com",
    }
    base.update(overrides)
    return base


class WorkerMailerGuardTests(unittest.TestCase):
    def setUp(self):
        self._orig = accountability_delivery._delegated_gmail_sender
        self.fake = _FakeGmail()
        accountability_delivery._delegated_gmail_sender = lambda *args: self.fake

    def tearDown(self):
        accountability_delivery._delegated_gmail_sender = self._orig

    def _send(self, **kwargs):
        args = dict(
            to=["carlo@streetsmart.insurance"],
            cc=[],
            subject="Verification digest",
            text_body="plain body",
        )
        args.update(kwargs)
        with unittest.mock.patch.dict(os.environ, _env(), clear=True):
            return vm.send_verification_email(**args)

    def test_self_address_in_to_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._send(to=["robie@streetsmart.insurance"])
        self.assertIn("robie@", str(ctx.exception))
        # no address/subject/body leaks through the error either
        self.assertNotIn("robie@streetsmart.insurance", str(ctx.exception))

    def test_self_address_case_variant_in_cc_is_rejected(self):
        with self.assertRaises(ValueError):
            self._send(cc=[" Robie@StreetSmart.Insurance "])

    def test_self_send_never_builds_gmail_service(self):
        calls = []
        accountability_delivery._delegated_gmail_sender = lambda *a: calls.append(a)
        with self.assertRaises(ValueError):
            self._send(to=["robie@streetsmart.insurance"])
        self.assertEqual(calls, [])

    def test_duplicate_in_sent_is_rejected(self):
        self.fake = _FakeGmail(
            sent_messages=[
                _sent_message(
                    "sent-1",
                    "carlo@streetsmart.insurance",
                    "Re: Verification digest",
                )
            ]
        )
        accountability_delivery._delegated_gmail_sender = lambda *args: self.fake
        with self.assertRaises(ValueError) as ctx:
            self._send()
        self.assertIn("already in Sent", str(ctx.exception))
        # the duplicate was never re-sent
        self.assertEqual(self.fake.captured, {})

    def test_different_subject_is_not_a_duplicate(self):
        self.fake = _FakeGmail(
            sent_messages=[
                _sent_message("sent-1", "carlo@streetsmart.insurance", "Other subject")
            ]
        )
        accountability_delivery._delegated_gmail_sender = lambda *args: self.fake
        receipt = self._send()
        self.assertEqual(receipt["kind"], "gmail")
        self.assertEqual(receipt["message_id"], "msg-123")

    def test_fresh_send_goes_through(self):
        receipt = self._send()
        self.assertEqual(receipt["kind"], "gmail")
        self.assertEqual(receipt["message_id"], "msg-123")
        self.assertIn("body", self.fake.captured)

    def test_sent_check_error_fails_open(self):
        # A Gmail lookup hiccup must never block legitimate worker mail.
        self.fake = _FakeGmail(list_error=RuntimeError("gmail down"))
        accountability_delivery._delegated_gmail_sender = lambda *args: self.fake
        receipt = self._send()
        self.assertEqual(receipt["kind"], "gmail")

    def test_error_messages_never_carry_addresses(self):
        with self.assertRaises(ValueError) as ctx:
            self._send(to=["robie@streetsmart.insurance"])
        self.assertNotIn("robie@streetsmart.insurance", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
