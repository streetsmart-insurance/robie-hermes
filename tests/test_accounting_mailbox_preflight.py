import json
import unittest

from robie_job_engine.accounting_mailbox_preflight import main, probe_mailboxes
from robie_job_engine.gmail_accountability import GMAIL_READONLY_SCOPE


ACCOUNT = "synthetic@test-project.iam.gserviceaccount.com"
MAILBOX = "desk@example.com"


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self, *, num_retries):
        assert num_retries == 0
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeService:
    def __init__(self, profile=MAILBOX, listing=None, message=None):
        self.profile = profile
        self.listing = listing if listing is not None else {"messages": [{"id": "synthetic-id"}], "nextPageToken": "more"}
        self.message = message if message is not None else {"id": "synthetic-id", "payload": {"body": {"data": "PRIVATE_CONTENT"}}, "snippet": "PRIVATE_SNIPPET"}
        self.calls = []

    def users(self):
        return self

    def getProfile(self, **kwargs):
        self.calls.append(("profile", kwargs))
        return Request({"emailAddress": self.profile})

    def messages(self):
        return self

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return Request(self.listing)

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return Request(self.message)

    def send(self, **kwargs):
        raise AssertionError("no sends permitted")

    modify = send
    delete = send


class PreflightTests(unittest.TestCase):
    def probe(self, service, mailboxes=(MAILBOX,)):
        def factory(account, mailbox, *, scopes):
            self.assertEqual(account, ACCOUNT)
            self.assertEqual(scopes, (GMAIL_READONLY_SCOPE,))
            return service
        return probe_mailboxes(ACCOUNT, mailboxes, approved_domain="example.com", service_factory=factory)

    def test_success_is_bounded_readonly_and_redacted(self):
        service = FakeService()
        report = self.probe(service)
        self.assertEqual(report["status"], "VERIFIED")
        self.assertFalse(report["complete_source_inventory"])
        self.assertFalse(report["send_access_checked"])
        self.assertEqual(service.calls[1][1], {"userId": "me", "q": "in:anywhere", "maxResults": 1})
        self.assertEqual(service.calls[2][1]["format"], "full")
        for private in (MAILBOX, ACCOUNT, "synthetic-id", "PRIVATE_CONTENT", "PRIVATE_SNIPPET", "more"):
            self.assertNotIn(private, json.dumps(report))

    def test_wrong_identity_stops_before_messages(self):
        service = FakeService(profile="wrong@example.com")
        report = self.probe(service)
        self.assertEqual(report["mailboxes"][0]["reason"], "MAILBOX_IDENTITY_MISMATCH")
        self.assertEqual(len(service.calls), 1)

    def test_empty_mailbox_does_not_prove_full_message_read(self):
        report = self.probe(FakeService(listing={}))
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertEqual(report["mailboxes"][0]["reason"], "NO_MESSAGE_TO_VERIFY")

    def test_failed_read_does_not_leak_provider_error(self):
        report = self.probe(FakeService(message=RuntimeError("TOKEN_AND_PRIVATE_CONTENT")))
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertNotIn("TOKEN", json.dumps(report))

    def test_malformed_and_mismatched_readbacks_are_unverified(self):
        for message in ({}, {"id": "wrong", "payload": {}}, {"id": "synthetic-id"}, "invalid"):
            with self.subTest(message=message):
                self.assertEqual(self.probe(FakeService(message=message))["status"], "UNVERIFIED")

    def test_invalid_search_results_are_unverified(self):
        for listing in ("invalid", {"messages": "invalid"}, {"messages": [{}]}, {"messages": [{"id": "one"}, {"id": "two"}]}):
            with self.subTest(listing=listing):
                self.assertEqual(self.probe(FakeService(listing=listing))["status"], "UNVERIFIED")

    def test_scope_population_validated_before_any_access(self):
        for mailboxes in ((), ("desk@outside.com",), ("*@example.com",), ("desk@example.com\nother@example.com",)):
            service = FakeService()
            with self.subTest(mailboxes=mailboxes), self.assertRaises(ValueError):
                self.probe(service, mailboxes)
            self.assertEqual(service.calls, [])

    def test_invalid_service_reference_rejected(self):
        with self.assertRaises(ValueError):
            probe_mailboxes("credential-value", (MAILBOX,), approved_domain="example.com")

    def test_duplicates_do_not_repeat_reads(self):
        service = FakeService()
        report = self.probe(service, (MAILBOX, " DESK@EXAMPLE.COM "))
        self.assertEqual(len(report["mailboxes"]), 1)
        self.assertEqual(len(service.calls), 3)

    def test_failed_mailbox_keeps_overall_unverified_and_checks_remaining(self):
        services = iter([FakeService(profile="wrong@example.com"), FakeService(profile="other@example.com")])
        report = probe_mailboxes(ACCOUNT, (MAILBOX, "other@example.com"), approved_domain="example.com", service_factory=lambda *a, **kw: next(services))
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertEqual([row["status"] for row in report["mailboxes"]], ["UNVERIFIED", "VERIFIED"])

    def test_live_read_requires_explicit_opt_in(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--service-account", ACCOUNT, "--approved-domain", "example.com", "--mailbox", MAILBOX])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
