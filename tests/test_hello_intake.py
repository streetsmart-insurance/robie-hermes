"""Tests for the hello@ intake classifier, identity extraction, and
unmatched queue. Mirrors the certificate intake's test philosophy: noise
guards first, genuine categories, vendor-sender protection, and
learn-on-resolve.
"""

import json
import os

import pytest

from robie_job_engine.hello_classifier import (
    ACK,
    AUTO_REPLY,
    GENUINE,
    INTERNAL,
    NOISE,
    UNKNOWN,
    ROUTE_FOR_REQUEST_TYPE,
    classify_hello,
    extract_hello_identity,
)
from robie_job_engine.hello_unmatched_queue import (
    QueueError,
    enqueue_unmatched,
    load_alias_store,
    open_entries,
    render_markdown,
    resolve_entry,
    sender_is_vendor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def qpath(tmp_path):
    return str(tmp_path / "hello_queue.jsonl")


@pytest.fixture()
def apath(tmp_path):
    return str(tmp_path / "hello_aliases.json")


def enqueue(qpath, **kw):
    base = dict(sender_email="client@example.com", subject="test",
                hold_reason="no match")
    base.update(kw)
    return enqueue_unmatched(queue_path=qpath, **base)


# ---------------------------------------------------------------------------
# Classifier: noise guards (checked first — they quote request language)
# ---------------------------------------------------------------------------

def test_autoreply_subject_beats_request_tail():
    action, rtype, _ = classify_hello(
        "Automatic reply: Renewal Quote for Jersey Strong Properties LLC",
        "I am out of the office.", "someone@example.com")
    assert action == AUTO_REPLY
    assert rtype is None


def test_bounce_subject_is_autoreply():
    action, _, _ = classify_hello(
        "Undeliverable: Renewal Quote for Jersey Strong Properties LLC",
        "Delivery has failed.", "mailer-daemon@example.com")
    assert action == AUTO_REPLY


def test_mailer_daemon_sender_is_autoreply():
    action, _, _ = classify_hello("Re: question", "see below",
                                  "mailer-daemon@google.com")
    assert action == AUTO_REPLY


def test_newsletter_is_noise():
    action, _, _ = classify_hello(
        "Fwd: Help Is at Hand: Severe Weather Resources",
        "Storm prep tips. Unsubscribe here.",
        "certificates@streetsmart.insurance")
    assert action == NOISE


def test_carrier_marketing_is_noise():
    action, _, _ = classify_hello("SWYFFT Get Together Request!",
                                  "Join us for lunch.", "x@swyfft.com")
    assert action == NOISE


def test_text_message_notification_is_noise():
    action, _, _ = classify_hello(
        "Re: Do Not Reply - Mununga Kipata has sent you a text message",
        "You have a new text.", "noreply@example.com")
    assert action == NOISE


def test_call_analysis_digest_is_noise():
    action, _, _ = classify_hello(
        "Re: Sonant Call Analysis - Phone: Scheduled Callback",
        "Call analysis summary.", "noreply@example.com")
    assert action == NOISE


# ---------------------------------------------------------------------------
# Classifier: internal + acknowledgement
# ---------------------------------------------------------------------------

def test_internal_sender():
    action, _, _ = classify_hello(
        "Fwd: Renewal Quote for Jersey Strong Properties LLC - AETVA",
        "Forwarding the carrier quote.", "carlo@streetsmart.insurance")
    assert action == INTERNAL


def test_internal_sender_ssinj():
    action, _, _ = classify_hello("Fwd: Notice of Cancellation",
                                  "FYI.", "carlo@ssinj.com")
    assert action == INTERNAL


def test_polite_thank_you_is_ack():
    action, _, _ = classify_hello("Re: Renewal Quote", "Thank you!",
                                  "client@gmail.com")
    assert action == ACK


def test_receipt_language_is_ack():
    action, _, _ = classify_hello("Re: quote", "Received the quote, all set.",
                                  "client@gmail.com")
    assert action == ACK


def test_request_language_beats_politeness():
    action, rtype, _ = classify_hello(
        "quote please", "Please provide a quote for my new shop. Thank you!",
        "client@gmail.com")
    assert action == GENUINE
    assert rtype == "new_business"


# ---------------------------------------------------------------------------
# Classifier: genuine categories
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,body,rtype", [
    ("Insurance for a new Little Andy's Setup within a market.", "",
     "new_business"),
    ("Renewal Quote for Jersey Strong Properties LLC - AETVA", "",
     "renewal"),
    ("Re: Trailer to add to my policy", "", "midterm"),
    ("Policy change - READY 2 ROLL MOVING LLC - 812601-884344-75", "",
     "midterm"),
    ("Issues with our policies that need Resolved Immediately.", "",
     "client_issue"),
    ("Fwd: Important: Policy Rescission Notice 1-HNY-NJ-01-014332", "",
     "carrier_notice"),
    ("Return premium received for Policy 3AB025200", "", "billing"),
    ("SETTLEMENT OFFER / Case 13256517 / Capital Premium Financing", "",
     "billing"),
    ("Fwd: Additional Interest added - Business Owners BP00109727", "",
     "endorsement"),
    ("Re: my license", "Attached is my driver's license as requested.",
     "document"),
    ("Quick question", "Can you confirm my premium for next term?",
     "general_question"),
])
def test_genuine_categories(subject, body, rtype):
    action, got, _ = classify_hello(subject, body, "client@gmail.com")
    assert action == GENUINE, subject
    assert got == rtype, subject


def test_unknown_for_gibberish():
    action, rtype, _ = classify_hello("asdf qwer", "zxcv 1234",
                                      "nobody@example.com")
    assert action == UNKNOWN
    assert rtype is None


def test_routes_cover_all_types():
    _, rtype, _ = classify_hello("x", "need a quote", "c@g.com")
    assert rtype in ROUTE_FOR_REQUEST_TYPE
    assert ROUTE_FOR_REQUEST_TYPE["new_business"] == "originating_producer"
    assert ROUTE_FOR_REQUEST_TYPE["renewal"] == "applicable_csr"
    assert ROUTE_FOR_REQUEST_TYPE["midterm"] == "applicable_csr"


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------

def test_company_from_renewal_subject():
    ident = extract_hello_identity(
        "Fwd: Renewal Quote for Jersey Strong Properties LLC - AETVA",
        "", "carlo@streetsmart.insurance")
    assert ident["company_name"] == "Jersey Strong Properties LLC"


def test_company_and_policy_from_midterm():
    ident = extract_hello_identity(
        "Policy change - READY 2 ROLL MOVING LLC - 812601-884344-75",
        "", "jake@streetsmart.insurance")
    assert ident["company_name"] == "READY 2 ROLL MOVING LLC"
    assert "812601-884344-75" in ident["policy_numbers"]


def test_company_and_policy_from_carrier_notice():
    ident = extract_hello_identity(
        "Hearts For Home Healthcare, LLC, Policy: PHPK2734817-000 has been",
        "", "PhlyProducerNotices@phly.com")
    assert ident["company_name"] == "Hearts For Home Healthcare, LLC"
    assert "PHPK2734817-000" in ident["policy_numbers"]


def test_sender_name_and_email_parsed():
    ident = extract_hello_identity("hi", "",
                                   "Jake Ferrara <jake@streetsmart.insurance>")
    assert ident["sender_email"] == "jake@streetsmart.insurance"
    assert ident["sender_name"] == "Jake Ferrara"


def test_mc_number_extracted():
    ident = extract_hello_identity("MC 478132 question", "", "c@g.com")
    assert "478132" in ident["mc_numbers"]


def test_no_anchors_is_not_a_guess():
    ident = extract_hello_identity("hello", "just saying hi", "c@g.com")
    assert ident["company_name"] is None
    assert ident["policy_numbers"] == []
    assert ident["mc_numbers"] == []


# ---------------------------------------------------------------------------
# Vendor guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sender", [
    "accounting@useascend.com",
    "PhlyProducerNotices@phly.com",
    "cheryl.ashley@brownandjoseph.net",
    "admin@britecore.com",
    "x@swyfft.com",
    "rmis@registrymonitoring.com",
    "carlo@streetsmart.insurance",
    "ops@vc.realpage.com",
])
def test_vendor_senders_never_aliased(sender):
    assert sender_is_vendor(sender)


def test_client_sender_not_vendor():
    assert not sender_is_vendor("client@gmail.com")


# ---------------------------------------------------------------------------
# Queue: enqueue / dedupe / report
# ---------------------------------------------------------------------------

def test_enqueue_ids_and_dedupe(qpath):
    e1 = enqueue(qpath, gmail_id="g1", request_type="renewal",
                 route="applicable_csr")
    assert e1["entry_id"] == "hu-000001"
    assert e1["request_type"] == "renewal"
    assert e1["route"] == "applicable_csr"
    e2 = enqueue(qpath, gmail_id="g1", request_type="renewal",
                 route="applicable_csr")
    assert e2["entry_id"] == "hu-000001"  # no double queue
    assert len(open_entries(qpath)) == 1


def test_enqueue_requires_sender_and_reason(qpath):
    with pytest.raises(ValueError):
        enqueue_unmatched(sender_email="", subject="x", hold_reason="y",
                          queue_path=qpath)
    with pytest.raises(ValueError):
        enqueue_unmatched(sender_email="a@b.c", subject="x", hold_reason="",
                          queue_path=qpath)


def test_report_shows_type_and_route(qpath):
    enqueue(qpath, sender_email="c@g.com", subject="Renewal Quote for X LLC",
            company_name="X LLC", request_type="renewal",
            route="applicable_csr", hold_reason="no report hit",
            strategies_tried=["report_email", "report_name"],
            policy_numbers=["BP00109727"])
    md = render_markdown(queue_path=qpath)
    assert "hu-000001" in md
    assert "renewal" in md
    assert "applicable_csr" in md
    assert "BP00109727" in md


def test_empty_queue_report(qpath):
    assert "Queue is clear" in render_markdown(queue_path=qpath)


# ---------------------------------------------------------------------------
# Queue: resolve + learn-on-resolve
# ---------------------------------------------------------------------------

def test_resolve_writes_strong_alias(qpath, apath):
    enqueue(qpath, sender_email="Client@Example.com", subject="need a quote",
            company_name="Example Shop LLC", hold_reason="no match")
    entry = resolve_entry("hu-000001", applicant_id=78540038,
                          account_name="Example Shop LLC",
                          request_type="new_business",
                          resolved_by="Carlo", queue_path=qpath,
                          alias_store_path=apath)
    assert entry["status"] == "resolved"
    assert entry["alias_written"] is True
    assert entry["resolution"]["request_type"] == "new_business"
    store = load_alias_store(apath)
    assert store["aliases"][0]["sender"] == "client@example.com"
    assert store["aliases"][0]["applicant_id"] == 78540038
    assert store["aliases"][0]["confidence"] == "strong"
    assert open_entries(qpath) == []


def test_resolve_vendor_sender_writes_no_alias(qpath, apath):
    enqueue(qpath, sender_email="accounting@useascend.com",
            subject="Return premium received for Policy 3AB025200",
            hold_reason="carrier notice, no client match")
    entry = resolve_entry("hu-000001", applicant_id=123,
                          resolved_by="Carlo", queue_path=qpath,
                          alias_store_path=apath)
    assert entry["alias_written"] is False
    assert "no fixed alias" in entry["notes"][0]
    assert load_alias_store(apath)["aliases"] == []


def test_resolve_not_our_client(qpath, apath):
    enqueue(qpath, hold_reason="no match")
    entry = resolve_entry("hu-000001", not_our_client=True,
                          resolved_by="Carlo", queue_path=qpath,
                          alias_store_path=apath)
    assert entry["resolution"] == {"not_our_client": True}
    assert entry["alias_written"] is False


def test_resolve_rejects_bad_applicant(qpath):
    enqueue(qpath, hold_reason="no match")
    with pytest.raises(QueueError):
        resolve_entry("hu-000001", applicant_id="abc", resolved_by="Carlo",
                      queue_path=qpath)
    with pytest.raises(QueueError):
        resolve_entry("hu-000001", applicant_id=-5, resolved_by="Carlo",
                      queue_path=qpath)


def test_resolve_rejects_double_resolve_and_missing_by(qpath):
    enqueue(qpath, hold_reason="no match")
    resolve_entry("hu-000001", not_our_client=True, resolved_by="Carlo",
                  queue_path=qpath)
    with pytest.raises(QueueError):
        resolve_entry("hu-000001", not_our_client=True, resolved_by="Carlo",
                      queue_path=qpath)
    enqueue(qpath, hold_reason="no match", gmail_id="g9")
    with pytest.raises(QueueError):
        resolve_entry("hu-000002", not_our_client=True, resolved_by="",
                      queue_path=qpath)


def test_alias_upsert_on_repeat_sender(qpath, apath):
    enqueue(qpath, hold_reason="no match", gmail_id="g1")
    resolve_entry("hu-000001", applicant_id=111, resolved_by="Carlo",
                  queue_path=qpath, alias_store_path=apath)
    enqueue(qpath, hold_reason="no match", gmail_id="g2")
    resolve_entry("hu-000002", applicant_id=222, resolved_by="Carlo",
                  queue_path=qpath, alias_store_path=apath)
    store = load_alias_store(apath)
    assert len(store["aliases"]) == 1
    assert store["aliases"][0]["applicant_id"] == 222


def test_corrupt_alias_store_refuses_write(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"aliases": "not-a-list"}))
    with pytest.raises(ValueError):
        load_alias_store(str(bad))


def test_env_override_paths(qpath, apath, monkeypatch):
    monkeypatch.setenv("HELLO_UNMATCHED_QUEUE_PATH", qpath)
    monkeypatch.setenv("HELLO_SENDER_ALIASES_PATH", apath)
    from robie_job_engine.hello_unmatched_queue import (
        default_alias_store_path, default_queue_path)
    assert default_queue_path() == qpath
    assert default_alias_store_path() == apath
