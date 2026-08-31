import os
import unittest
from unittest.mock import patch

import ezlynx_login_bootstrap as bootstrap


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Users:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def getProfile(self, **kwargs):
        return _Execute({"emailAddress": self.mailbox})


class _Gmail:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def users(self):
        return _Users(self.mailbox)


class KeylessMailboxTests(unittest.TestCase):
    def test_gmail_service_uses_keyless_delegation_when_configured(self):
        calls = []
        service = _Gmail(bootstrap.EXPECTED_MAILBOX)
        with (
            patch.dict(
                os.environ,
                {
                    "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT":
                    "robie-test@example.iam.gserviceaccount.com"
                },
                clear=False,
            ),
            patch.object(
                bootstrap,
                "build_keyless_mailbox_service",
                side_effect=lambda account: calls.append(account) or service,
            ),
            patch.object(
                bootstrap,
                "build_legacy_mailbox_service",
                side_effect=AssertionError("legacy OAuth token must not be read"),
            ),
        ):
            self.assertIs(bootstrap.gmail_service(), service)

        self.assertEqual(calls, ["robie-test@example.iam.gserviceaccount.com"])

    def test_gmail_service_rejects_wrong_delegated_mailbox(self):
        with (
            patch.dict(
                os.environ,
                {
                    "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT":
                    "robie-test@example.iam.gserviceaccount.com"
                },
                clear=False,
            ),
            patch.object(
                bootstrap,
                "build_keyless_mailbox_service",
                return_value=_Gmail("someone-else@streetsmart.insurance"),
            ),
        ):
            with self.assertRaises(bootstrap.MailboxIdentityError):
                bootstrap.gmail_service()


if __name__ == "__main__":
    unittest.main()
