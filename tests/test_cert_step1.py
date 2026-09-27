"""Tests for cert_gmail_adapter (fake session) and cert_intake_runner."""

import base64
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_gmail_adapter import CertGmailAdapter  # noqa: E402
from robie_job_engine.cert_applicant_index import build_index  # noqa: E402
from robie_job_engine.cert_intake import MemoryDedupeStore  # noqa: E402
from robie_job_engine.cert_intake_runner import run_intake_once  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def gmail_payload(gmail_id, sender, subject, body):
    def header(name, value):
        return {"name": name, "value": value}

    raw_body = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": gmail_id,
        "threadId": "thread-" + gmail_id,
        "payload": {
            "headers": [
                header("From", sender),
                header("Subject", subject),
                header("Date", "Sat, 26 Sep 2026 09:00:00 -0400"),
                header("Message-ID", f"<{gmail_id}@example.com>"),
            ],
            "body": {"data": raw_body},
            "parts": [],
        },
    }


class FakeSession:
    """Stands in for requests.Session; records calls."""

    def __init__(self, messages):
        self.messages = messages  # gmail_id -> payload
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        if url.endswith("/messages"):
            ids = list(self.messages.keys())
            return FakeResponse(200, {"messages": [{"id": i} for i in ids]})
        mid = url.split("/messages/")[1].split("/")[0]
        return FakeResponse(200, self.messages[mid])


def make_index():
    return build_index([
        {"account_name": "Fonseca General Contractor LLC",
         "applicant_id": 116349171, "email_primary": "office@fonsecagc.com",
         "phones": []},
    ])


def test_adapter_lists_and_fetches():
    session = FakeSession({
        "m1": gmail_payload("m1", "office@fonsecagc.com",
                           "Certificate request",
                           "Named insured: Fonseca General Contractor LLC"),
    })
    adapter = CertGmailAdapter(session)
    ids, token = adapter.list_message_ids("newer_than:1d", None)
    assert ids == ["m1"]
    assert token is None
    full = adapter.get_full_message("m1")
    assert full["id"] == "m1"
    assert any("messages" in c[0] for c in session.calls)


def test_runner_matches_and_dedupes():
    session = FakeSession({
        "m1": gmail_payload("m1", "office@fonsecagc.com",
                           "Certificate request",
                           "Named insured: Fonseca General Contractor LLC"),
        "m2": gmail_payload("m2", "gc@example.com",
                           "Need COI",
                           "Certificate for Some Unknown Company LLC"),
    })
    adapter = CertGmailAdapter(session)
    store = MemoryDedupeStore()
    index = make_index()

    out1 = run_intake_once(adapter, store, index, query="newer_than:1d")
    assert out1["stats"]["processed"] == 2
    assert out1["stats"]["matched"] == 1
    assert out1["stats"]["held"] == 1
    matched = [r for r in out1["records"] if not r.held][0]
    assert matched.match.applicant_id == 116349171
    held = [r for r in out1["records"] if r.held][0]
    assert "EZLynx lookup" in held.hold_reason

    # Second sweep: everything is a duplicate, nothing re-processed.
    out2 = run_intake_once(adapter, store, index, query="newer_than:1d")
    assert out2["stats"]["duplicates"] == 2
    assert out2["stats"]["processed"] == 0


def test_identical_bodies_are_not_duplicates():
    """Regression: two distinct messages with byte-identical bodies (e.g.
    thread replies quoting prior content) must both be processed. The live
    mailbox sweep of 2026-09-26 showed 94 real messages skipped on
    body_hash alone."""
    same_body = "Please issue a certificate. Thanks."
    session = FakeSession({
        "m1": gmail_payload("m1", "office@fonsecagc.com",
                           "Certificate request",
                           same_body),
        "m2": gmail_payload("m2", "office@fonsecagc.com",
                           "Re: Certificate request",
                           same_body),
    })
    adapter = CertGmailAdapter(session)
    store = MemoryDedupeStore()
    out = run_intake_once(adapter, store, make_index(), query="newer_than:1d")
    assert out["stats"]["processed"] == 2
    assert out["stats"]["duplicates"] == 0


def test_runner_checkpoint_meta_records_steps():
    session = FakeSession({
        "m1": gmail_payload("m1", "office@fonsecagc.com",
                           "Certificate request",
                           "Named insured: Fonseca General Contractor LLC"),
    })
    adapter = CertGmailAdapter(session)
    store = MemoryDedupeStore()
    out = run_intake_once(adapter, store, index := make_index(),
                          query="newer_than:1d")
    assert out["stats"]["matched"] == 1
    metas = list(store._seen.values())
    assert any(m.get("stage") == "intake_complete"
               and m.get("applicant_id") == 116349171 for m in metas)
