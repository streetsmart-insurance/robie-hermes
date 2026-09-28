"""Reliability battery: cross-mailbox read-only Gmail search for phase 2.

Carlo authorized domain-wide delegation so the worker finds carrier
replies / endorsement documents in the inboxes of the people involved in
each change (robie@ + assigned CSR + assigned producer).

Every test proves a hard boundary:
- an endorsement attachment in the CSR's inbox advances the change to
  endorsement_found (never confirmed — phase 3 owns that);
- a carrier reply in the producer's inbox is logged and clears the
  manual-action queue;
- unrelated mail (wrong policy number) is ignored;
- nobody uninvolved in the change is ever impersonated;
- NO code path can send from an impersonated account (send-from is
  always robie@streetsmart.insurance);
- dry-run mutates nothing;
- a delegation failure on one mailbox never kills the run.

Fake Gmail only: no network, no secrets, no real sends.
"""

import base64
import inspect
import json
from datetime import date, timedelta
from email import message_from_bytes
from pathlib import Path

import pytest

from robie_job_engine.overdue_policy_change_reports import (
    notification_key,
    parse_4359_csv,
)
from robie_job_engine.policy_change_carrier_contact import (
    PolicyChangeCarrierContactWorker,
    default_carrier_mailer,
)
from robie_job_engine.policy_change_mailbox_search import (
    APPROVED_DOMAIN,
    SENDER,
    PolicyChangeMailboxSearcher,
    build_default_mailbox_searcher,
    classify_message,
    default_search_service_factory,
    extract_message_fields,
    resolve_change_mailboxes,
)

TODAY = date(2026, 9, 27)

HEADER = (
    "Account Name,Applicant ID,Policy Number,Line Of Business,Effective Date,"
    "Master Company,Request Status,Created By,Written Premium,Premium - Annualized,"
    "Branch,Department,Service Team,Assigned Producer,CSR,Preferred Language,"
    "Applicant Labels,Policy Labels,Change Request Created Date"
)


def row(account="SAPP Construction Corp", applicant="41055091", policy="S 2391821",
        created="2026-08-17", csr="Eimy Ramos", status="Open",
        carrier="Merchants Insurance Group", lob="Commercial Pkg",
        producer="Sandy Santana") -> str:
    return (
        f"{account},{applicant},{policy},{lob},2025-12-10,{carrier},{status},"
        f"\"Quezada, Zeus\",$28936.00,$28936.00,Streetsmart Insurance,"
        f"Commercial Lines,,{producer},{csr},English,,,{created}"
    )


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def fake_table():
    return {
        "carriers": [
            {"record_id": "120", "name": "Merchants Insurance Group",
             "route_status": "ok",
             "routes": [{"label": "Policy Changes", "type": "email",
                         "email": "MidlanticOffice@Merchantsgroup.com",
                         "cc_emails": [], "phone": None, "url": None, "notes": ""}]},
            {"record_id": "147", "name": "Personal Umbrella",
             "route_status": "manual",
             "routes": [{"label": "Policy Changes", "type": "phone",
                         "email": None, "cc_emails": [],
                         "phone": "(510) 903-3313", "url": None,
                         "notes": "phone-only"}]},
            {"record_id": "188", "name": "Tuscano Agency",
             "route_status": "missing", "routes": []},
        ]
    }


def fake_roster():
    return {"directory": {
        "eimy ramos": "eimy@streetsmart.insurance",
        "sandy santana": "sandy@streetsmart.insurance",
        "jake ferrara": "jake@streetsmart.insurance",  # uninvolved
    }}


class FakeFollowupStore:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.events = []
        self.set_status_calls = []

    def get(self, key):
        return {"status": self.statuses.get(key), "history": []}

    def record_event(self, key, event, detail, today):
        self.events.append((key, event, detail, today))

    def set_status(self, key, status, today, detail=""):
        self.set_status_calls.append((key, status))


# -- fake Gmail -----------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def fake_message(msg_id, *, subject, sender, date_str, snippet="",
                 body="", filenames=()):
    parts = []
    if body:
        parts.append({"mimeType": "text/plain", "filename": "",
                      "body": {"data": _b64(body)}})
    for filename in filenames:
        parts.append({"mimeType": "application/pdf", "filename": filename,
                      "body": {"attachmentId": "att-" + filename}})
    return {
        "id": msg_id,
        "snippet": snippet,
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date_str},
            ],
            "parts": parts,
        },
    }


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _SendRaisesMessages:
    """messages() resource whose send() explodes: proves no send path."""

    def __init__(self, parent):
        self._parent = parent

    def list(self, **kwargs):
        return _FakeRequest({"messages": [{"id": m["id"]}
                                          for m in self._parent.inbox]})

    def get(self, userId, id, format):  # noqa: A002
        for message in self._parent.inbox:
            if message["id"] == id:
                return _FakeRequest(message)
        raise AssertionError(f"unknown message id {id}")

    def send(self, **kwargs):  # noqa: A002
        raise AssertionError(
            "mailbox search must never call messages().send()")


class FakeDelegatedService:
    def __init__(self, mailbox, inbox):
        self.mailbox = mailbox
        self.inbox = inbox

    def users(self):
        return self

    def messages(self):
        return _SendRaisesMessages(self)

    def getProfile(self, userId):  # noqa: N802
        return _FakeRequest({"emailAddress": self.mailbox})


def make_fake_factory(mailboxes, fail_on=()):
    """mailboxes: {email: [fake messages]}. fail_on: mailboxes that raise.

    Records every impersonated mailbox on factory.searched.
    """
    searched = []

    def factory(user):
        searched.append(user)
        if user in fail_on:
            raise RuntimeError("delegation denied for " + user)
        return FakeDelegatedService(user, mailboxes.get(user, []))

    factory.searched = searched
    return factory


def make_worker(tmp_path, *, table=None, mailboxes=None, fail_on=(),
                roster=None, rows=None, nag_days_ago=10):
    sent = {}
    mails = []
    parsed = parse_4359_csv(csv_bytes(*(rows or [row()])))
    for r in parsed:
        key = notification_key(r["CSR"], r["Policy Number"],
                               r["Change Request Created Date"])
        sent[key] = (TODAY - timedelta(days=nag_days_ago)).isoformat()
    sent_path = tmp_path / "sent.json"
    sent_path.write_text(json.dumps(sent))
    factory = make_fake_factory(mailboxes or {}, fail_on)
    searcher = PolicyChangeMailboxSearcher(service_factory=factory)

    def mailer(*, to, cc, subject, text_body, html_body):
        mails.append({"to": to, "cc": cc, "subject": subject})
        return {"message_id": "fake"}

    worker = PolicyChangeCarrierContactWorker(
        queue_reader=lambda payload: parsed,
        document_search=lambda applicant_id: [],
        mailer=mailer,
        routing_table=table or fake_table(),
        mailbox_searcher=searcher,
        roster_maps=fake_roster() if roster is None else roster,
    )
    payload = {
        "sent_store_path": str(sent_path),
        "carrier_store_path": str(tmp_path / "carrier_contact.json"),
        "followup_store": FakeFollowupStore(),
    }
    worker._test_factory = factory
    return worker, payload, mails, tmp_path


def run_worker(worker, payload, dry_run=False):
    job = {"action_type": "contact_carriers", "payload": payload}
    return worker.perform(job, dry_run=dry_run, today=TODAY)


# -- mailbox resolution -----------------------------------------------------------

def test_resolve_change_mailboxes_involved_only():
    item = {"CSR": "Eimy Ramos", "Assigned Producer": "Sandy Santana"}
    mailboxes = resolve_change_mailboxes(item, fake_roster())
    assert mailboxes == [
        "robie@streetsmart.insurance",
        "eimy@streetsmart.insurance",
        "sandy@streetsmart.insurance",
    ]
    # Jake is in the roster but uninvolved in this change: never included.
    assert "jake@streetsmart.insurance" not in mailboxes


def test_resolve_change_mailboxes_roster_unavailable_is_robie_only():
    item = {"CSR": "Eimy Ramos", "Assigned Producer": "Sandy Santana"}
    assert resolve_change_mailboxes(item, None) == ["robie@streetsmart.insurance"]
    assert resolve_change_mailboxes(item, {}) == ["robie@streetsmart.insurance"]


def test_resolve_change_mailboxes_rejects_non_agency_email():
    roster = {"directory": {"eimy ramos": "eimy@gmail.com"}}
    item = {"CSR": "Eimy Ramos", "Assigned Producer": ""}
    assert resolve_change_mailboxes(item, roster) == ["robie@streetsmart.insurance"]


def test_resolve_change_mailboxes_unresolved_person_skipped():
    item = {"CSR": "Ghost Agent", "Assigned Producer": "Sandy Santana"}
    mailboxes = resolve_change_mailboxes(item, fake_roster())
    assert mailboxes == ["robie@streetsmart.insurance",
                         "sandy@streetsmart.insurance"]


# -- message classification ---------------------------------------------------------

def test_classify_endorsement_attachment():
    fields = extract_message_fields(fake_message(
        "m1", subject="FW: endorsement docs",
        sender="eimy@streetsmart.insurance",
        date_str="Sat, 26 Sep 2026 10:00:00 -0400",
        body="SAPP Construction Corp — see attached",
        filenames=["Endorsement S2391821.pdf"]))
    hit = classify_message(fields, policy_digits="2391821",
                           insured_tokens=["sapp", "construction"])
    assert hit and hit["kind"] == "endorsement"
    assert hit["filename"] == "Endorsement S2391821.pdf"


def test_classify_carrier_reply():
    fields = extract_message_fields(fake_message(
        "m2", subject="Re: policy change S 2391821",
        sender="Underwriting <uw@merchantsgroup.com>",
        date_str="Fri, 25 Sep 2026 09:00:00 -0400",
        body="The endorsement for SAPP Construction Corp S 2391821 "
             "has been issued."))
    hit = classify_message(fields, policy_digits="2391821",
                           insured_tokens=["sapp", "construction"])
    assert hit and hit["kind"] == "carrier_reply"


def test_classify_wrong_policy_number_ignored():
    fields = extract_message_fields(fake_message(
        "m3", subject="Re: policy change S 9999999",
        sender="uw@merchantsgroup.com",
        date_str="Fri, 25 Sep 2026 09:00:00 -0400",
        body="Endorsement for S 9999999 attached.",
        filenames=["Endorsement S9999999.pdf"]))
    assert classify_message(fields, policy_digits="2391821",
                            insured_tokens=["sapp", "construction"]) is None


def test_classify_agency_internal_mail_is_not_carrier_reply():
    fields = extract_message_fields(fake_message(
        "m4", subject="S 2391821 SAPP follow up",
        sender="Eimy Ramos <eimy@streetsmart.insurance>",
        date_str="Fri, 25 Sep 2026 09:00:00 -0400",
        body="Chasing the carrier on S 2391821."))
    assert classify_message(fields, policy_digits="2391821",
                            insured_tokens=["sapp", "construction"]) is None


def test_classify_wrong_insured_ignored():
    fields = extract_message_fields(fake_message(
        "m5", subject="Re: policy change S 2391821",
        sender="uw@othermutual.com",
        date_str="Fri, 25 Sep 2026 09:00:00 -0400",
        body="About S 2391821 for Acme Widgets LLC."))
    assert classify_message(fields, policy_digits="2391821",
                            insured_tokens=["sapp", "construction"]) is None


# -- worker behavior ------------------------------------------------------------------

def test_endorsement_in_csr_inbox_advances_manual_change(tmp_path):
    rows = [row(carrier="Personal Umbrella")]  # phone-only -> manual queue
    inbox = {"eimy@streetsmart.insurance": [fake_message(
        "m1", subject="endorsement received",
        sender="uw@personalumbrella.example",
        date_str="Sat, 26 Sep 2026 10:00:00 -0400",
        body="SAPP Construction Corp S 2391821",
        filenames=["Endorsement S2391821.pdf"])]}
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows,
                                            mailboxes=inbox)
    evidence = run_worker(worker, payload)
    summary = evidence["summary"]
    assert summary["endorsement_found"] == 1
    assert summary["endorsement_found_via_mailbox"] == 1
    assert summary["manual_action"] == []  # never queued: found first
    assert summary["emailed"] == 0 and mails == []
    searches = evidence["mailbox_search"]["searches"]
    assert any(s["mailbox"] == "eimy@streetsmart.insurance" and
               s["status"] == "hit" for s in searches[0]["mailboxes"])
    # State advanced (live mode here), never to "confirmed".
    followup = payload["followup_store"]
    assert any(e[1] == "endorsement_found" for e in followup.events)
    assert followup.set_status_calls == []


def test_carrier_reply_in_producer_inbox_clears_manual_queue(tmp_path):
    rows = [row(carrier="Personal Umbrella")]  # phone-only -> manual queue
    inbox = {"sandy@streetsmart.insurance": [fake_message(
        "m2", subject="Re: policy change request S 2391821",
        sender="Service <service@personalumbrella.example>",
        date_str="Mon, 21 Sep 2026 09:00:00 -0400",  # 6 days ago: old reply
        body="Working on the endorsement for SAPP Construction Corp "
             "policy S 2391821. Will advise.")]}
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows,
                                            mailboxes=inbox)
    evidence = run_worker(worker, payload)
    summary = evidence["summary"]
    assert summary["manual_action"] == []  # cleared
    assert summary["manual_queue_cleared"] == 1
    assert summary["emailed"] == 0 and mails == []
    followup = payload["followup_store"]
    assert any(e[1] == "carrier_reply_found" for e in followup.events)
    assert followup.set_status_calls == []


def test_recent_carrier_reply_suppresses_duplicate_email(tmp_path):
    inbox = {"eimy@streetsmart.insurance": [fake_message(
        "m2", subject="Re: change S 2391821",
        sender="uw@merchantsgroup.com",
        date_str="Sat, 26 Sep 2026 10:00:00 -0400",  # yesterday: recent
        body="SAPP Construction Corp S 2391821 is being processed.")]}
    worker, payload, mails, _ = make_worker(tmp_path, mailboxes=inbox)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert evidence["summary"]["carrier_reply_recent"] == 1
    assert mails == []


def test_unrelated_mail_does_not_suppress_email(tmp_path):
    inbox = {"eimy@streetsmart.insurance": [fake_message(
        "m3", subject="Re: policy change S 9999999",
        sender="uw@merchantsgroup.com",
        date_str="Sat, 26 Sep 2026 10:00:00 -0400",
        body="Endorsement for S 9999999 attached.",
        filenames=["Endorsement S9999999.pdf"])]}
    worker, payload, mails, _ = make_worker(tmp_path, mailboxes=inbox)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 1
    assert len(mails) == 1
    searches = evidence["mailbox_search"]["searches"][0]["mailboxes"]
    assert all(s["status"] == "miss" for s in searches)


def test_uninvolved_mailbox_never_searched(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path, mailboxes={})
    run_worker(worker, payload)
    searched = worker._test_factory.searched
    assert "jake@streetsmart.insurance" not in searched
    assert sorted(searched) == [
        "eimy@streetsmart.insurance",
        "robie@streetsmart.insurance",
        "sandy@streetsmart.insurance",
    ]


def test_delegation_failure_on_one_mailbox_does_not_kill_run(tmp_path):
    inbox = {"sandy@streetsmart.insurance": [fake_message(
        "m1", subject="endorsement received",
        sender="uw@merchantsgroup.com",
        date_str="Sat, 26 Sep 2026 10:00:00 -0400",
        body="SAPP Construction Corp S 2391821",
        filenames=["Endorsement S2391821.pdf"])]}
    worker, payload, mails, _ = make_worker(
        tmp_path, mailboxes=inbox, fail_on=("eimy@streetsmart.insurance",))
    evidence = run_worker(worker, payload)
    assert evidence["succeeded"]
    assert evidence["summary"]["endorsement_found"] == 1
    mailboxes = {s["mailbox"]: s
                 for s in evidence["mailbox_search"]["searches"][0]["mailboxes"]}
    assert mailboxes["eimy@streetsmart.insurance"]["status"] == "error"
    assert "delegation" in mailboxes["eimy@streetsmart.insurance"]["error"].casefold()
    assert mailboxes["sandy@streetsmart.insurance"]["status"] == "hit"


def test_every_mailbox_search_logged_in_evidence(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path, mailboxes={})
    evidence = run_worker(worker, payload)
    mb = evidence["mailbox_search"]
    assert mb["enabled"] is True
    assert mb["reason"] is None
    for search in mb["searches"]:
        for entry in search["mailboxes"]:
            assert entry["mailbox"]
            assert entry["query"]  # the exact Gmail query, logged
            assert entry["status"] in ("hit", "miss", "error", "skipped")


def test_dry_run_mailbox_search_mutates_nothing(tmp_path):
    inbox = {"eimy@streetsmart.insurance": [fake_message(
        "m1", subject="endorsement received",
        sender="uw@merchantsgroup.com",
        date_str="Sat, 26 Sep 2026 10:00:00 -0400",
        body="SAPP Construction Corp S 2391821",
        filenames=["Endorsement S2391821.pdf"])]}
    worker, payload, mails, _ = make_worker(tmp_path, mailboxes=inbox)
    evidence = run_worker(worker, payload, dry_run=True)
    assert evidence["succeeded"]
    assert mails == []
    assert not Path(payload["carrier_store_path"]).exists()
    assert payload["followup_store"].events == []
    # ... but the read-only search still ran and found the evidence.
    assert evidence["summary"]["endorsement_found"] == 1
    assert evidence["mailbox_search"]["enabled"] is True


def test_searcher_disabled_without_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT",
                       raising=False)
    assert build_default_mailbox_searcher() is None
    worker, payload, mails, _ = make_worker(tmp_path)
    worker.mailbox_searcher = None  # resolve from env -> unconfigured
    evidence = run_worker(worker, payload)
    assert evidence["succeeded"]
    assert evidence["mailbox_search"]["enabled"] is False
    assert evidence["summary"]["emailed"] == 1  # run continues


# -- send-from pin ----------------------------------------------------------------------

def test_searcher_never_calls_send(tmp_path):
    """The fake service's send() raises; a full run must never trigger it."""
    inbox = {"eimy@streetsmart.insurance": [fake_message(
        "m1", subject="Re: S 2391821 SAPP",
        sender="uw@merchantsgroup.com",
        date_str="Sat, 26 Sep 2026 10:00:00 -0400",
        body="Processing SAPP Construction Corp S 2391821.")]}
    worker, payload, mails, _ = make_worker(tmp_path, mailboxes=inbox)
    evidence = run_worker(worker, payload)  # would raise if send were called
    assert evidence["succeeded"]


def test_mailbox_search_module_has_no_send_path():
    module_path = Path(__file__).parent.parent / "robie_job_engine" / \
        "policy_change_mailbox_search.py"
    text = module_path.read_text(encoding="utf-8")
    assert ".send(" not in text
    assert "gmail.send" not in text


def test_default_search_factory_requests_readonly_scope_only(monkeypatch):
    import robie_job_engine.gmail_accountability as ga

    captured = {}

    def fake_build(service_account_email, user, scopes=None):
        captured["scopes"] = tuple(scopes or ())
        captured["user"] = user
        return object()

    def fake_verify(service, expected_mailbox):
        captured["verified"] = expected_mailbox

    monkeypatch.setattr(ga, "build_keyless_delegated_service", fake_build)
    monkeypatch.setattr(ga, "verify_delegated_mailbox", fake_verify)
    factory = default_search_service_factory("sa@example.iam.gserviceaccount.com")
    factory("eimy@streetsmart.insurance")
    assert captured["user"] == "eimy@streetsmart.insurance"
    assert captured["verified"] == "eimy@streetsmart.insurance"
    assert captured["scopes"] == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_default_carrier_mailer_always_sends_from_robie(monkeypatch):
    """End-to-end pin: the only send path hardcodes From: robie@.

    googleapiclient is stubbed (not installed in this sandbox; it is a
    declared dependency in CI) — the assertion target is the message the
    mailer builds, which is where the sender is pinned.
    """
    import sys
    import types

    import google.auth
    import google.auth.iam
    import google.oauth2.service_account

    monkeypatch.setenv("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT",
                       "sa@example.iam.gserviceaccount.com")
    monkeypatch.setattr(google.auth, "default",
                        lambda scopes=None: (object(), None))
    monkeypatch.setattr(google.auth.iam, "Signer",
                        lambda request, source, email: object())
    monkeypatch.setattr(google.oauth2.service_account, "Credentials",
                        lambda **kwargs: object())

    captured = {}

    class FakeSend:
        def send(self, userId, body):  # noqa: N803
            captured["raw"] = body["raw"]
            return self

        def execute(self):
            return {"id": "sent-1"}

    class FakeMessages:
        def messages(self):
            return FakeSend()

    class FakeGmail:
        def users(self):
            return FakeMessages()

    discovery_stub = types.ModuleType("googleapiclient.discovery")
    discovery_stub.build = lambda *a, **k: FakeGmail()
    pkg_stub = types.ModuleType("googleapiclient")
    pkg_stub.discovery = discovery_stub
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg_stub)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery",
                        discovery_stub)

    # The mailer signature offers no from/sender parameter: no caller can
    # redirect the sender.
    params = inspect.signature(default_carrier_mailer).parameters
    assert "from" not in {p.casefold() for p in params}
    assert "sender" not in {p.casefold() for p in params}

    result = default_carrier_mailer(
        to=["MidlanticOffice@Merchantsgroup.com"], cc=[],
        subject="test", text_body="hello", html_body="<p>hello</p>")
    raw = base64.urlsafe_b64decode(captured["raw"].encode("ascii"))
    parsed = message_from_bytes(raw)
    assert parsed["From"] == "robie@streetsmart.insurance"
    assert result["sender"] == "robie@streetsmart.insurance"
    assert APPROVED_DOMAIN in parsed["From"]
