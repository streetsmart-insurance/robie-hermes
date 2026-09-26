"""Tests for the certificates intake thin slice (all offline, fake clients)."""

from __future__ import annotations

import base64

import pytest

from robie_job_engine.cert_intake import (
    CertAttachment,
    CertEmail,
    MemoryDedupeStore,
    RequestFacts,
    dedupe_keys,
    discover_messages,
    extract_request_facts,
    is_duplicate,
    mark_processed,
    summarize_for_note,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _gmail_payload(
    *,
    gid="g1",
    message_id="<m1@example.com>",
    frm="Town Clerk <clerk@exampletown.gov>",
    subject="Certificate request",
    body="Named Insured: Lawn Buddies LLC\nPolicy #: SHOH0178125030000\nCertificate Holder: Example Town",
    attachments=(),
):
    parts = [
        {
            "mimeType": "text/plain",
            "filename": "",
            "body": {"data": _b64(body)},
        }
    ]
    for filename, data, aid in attachments:
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": filename,
                "body": {"attachmentId": aid},
            }
        )
    return {
        "id": gid,
        "threadId": "t1",
        "payload": {
            "headers": [
                {"name": "From", "value": frm},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": message_id},
                {"name": "Date", "value": "Fri, 25 Sep 2026 10:00:00 -0400"},
            ],
            "parts": parts,
        },
    }


class FakeGmail:
    def __init__(self, pages, payloads, blobs):
        self.pages = pages
        self.payloads = payloads
        self.blobs = blobs

    def list_message_ids(self, query, page_token, page_size=50):
        page = self.pages[int(page_token or 0)]
        nxt = str(int(page_token or 0) + 1) if int(page_token or 0) + 1 < len(
            self.pages
        ) else None
        return page, nxt

    def get_message(self, gmail_id):
        return self.payloads[gmail_id]

    def get_attachment(self, gmail_id, attachment_id):
        return self.blobs[attachment_id]


# ---------------------------------------------------------------------------
# Intake: parsing + discovery
# ---------------------------------------------------------------------------


def test_from_gmail_api_parses_body_and_attachments():
    blobs = {"a1": b"%PDF-1.4 fake"}
    g = FakeGmail([], {}, blobs)
    payload = _gmail_payload(attachments=[("coi.pdf", None, "a1")])
    email = CertEmail.from_gmail_api(payload, g.get_attachment)
    assert email.gmail_id == "g1"
    assert email.rfc_message_id == "<m1@example.com>"
    assert "Lawn Buddies" in email.body_text
    assert len(email.attachments) == 1
    assert email.attachments[0].sha256
    assert email.attachments[0].content == b"%PDF-1.4 fake"


def test_discover_messages_pages_to_end():
    g = FakeGmail([["a", "b"], ["c"]], {}, {})
    assert discover_messages(g, "q") == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def _email(gid="g1", mid="<m1@example.com>", body=None, atts=()):
    blobs = {aid: data for _, data, aid in atts}
    g = FakeGmail([], {}, blobs)
    return CertEmail.from_gmail_api(
        _gmail_payload(
            gid=gid,
            message_id=mid,
            body=body
            or "Named Insured: Lawn Buddies LLC\nPolicy #: SHOH0178125030000\nCertificate Holder: Example Town",
            attachments=atts,
        ),
        g.get_attachment,
    )


def test_duplicate_same_provider_id():
    store = MemoryDedupeStore()
    e = _email()
    assert is_duplicate(e, store) == (False, "")
    mark_processed(e, store)
    dup, reason = is_duplicate(_email(), store)
    assert dup and "Gmail message id" in reason


def test_duplicate_same_rfc_new_provider_id():
    store = MemoryDedupeStore()
    mark_processed(_email(gid="g1"), store)
    dup, reason = is_duplicate(_email(gid="g2"), store)
    assert dup and "RFC Message-ID" in reason


def test_duplicate_forward_identical_body_new_ids():
    store = MemoryDedupeStore()
def test_duplicate_forward_identical_body_new_ids():
    # Distinct messages can legitimately share identical body bytes (thread
    # replies quoting prior content). Only identity keys gate dedupe.
    store = MemoryDedupeStore()
    mark_processed(_email(gid="g1", mid="<m1@example.com>"), store)
    dup, _ = is_duplicate(_email(gid="g9", mid="<m9@other.com>"), store)
    assert not dup


def test_renamed_attachment_same_bytes_is_not_duplicate():
    # Same bytes under a different name with a distinct message id is a
    # distinct delivery, not a duplicate. Hashes are forensic only.
    store = MemoryDedupeStore()
    mark_processed(_email(atts=[("coi.pdf", b"BYTES", "a1")]), store)
    dup, _ = is_duplicate(
        _email(gid="g2", mid="<m2@x>", atts=[("renamed.pdf", b"BYTES", "a2")]), store
    )
    assert not dup


def test_changed_attachment_bytes_is_not_duplicate():
    store = MemoryDedupeStore()
    mark_processed(_email(atts=[("coi.pdf", b"V1", "a1")]), store)
    dup, _ = is_duplicate(
        _email(gid="g2", mid="<m2@x>", atts=[("coi.pdf", b"V2-NEW", "a2")]), store
    )
    assert not dup  # new bytes may be a new version


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------


def test_extract_facts_names_insured_policy_holder():
    e = _email()
    facts = extract_request_facts(e)
    assert facts.insured_name == "Lawn Buddies LLC"
    assert "SHOH0178125030000" in facts.policy_numbers
    assert facts.holder_names == ["Example Town"]
    assert facts.requested_action == "certificate_request"
    assert facts.requester_email == "clerk@exampletown.gov"
    assert facts.requester_is_third_party  # town clerk is not the insured


def test_extract_facts_insured_sending_is_not_third_party():
    payload = _gmail_payload(
        frm="Taylor <taylor@lawnbuddies.example>",
        body="Named Insured: Lawn Buddies LLC\nPolicy #: SHOH0178125030000",
    )
    e = CertEmail.from_gmail_api(payload)
    facts = extract_request_facts(e)
    assert not facts.requester_is_third_party


def test_extract_facts_no_policy_means_none_not_guess():
    payload = _gmail_payload(body="Hi, please send a certificate. Thanks!")
    e = CertEmail.from_gmail_api(payload)
    facts = extract_request_facts(e)
    assert facts.policy_numbers == []
    assert facts.insured_name is None


def test_pdf_text_extractor_fills_missing_insured():
    blobs = {"a1": b"%PDF"}
    g = FakeGmail([], {}, blobs)
    payload = _gmail_payload(
        body="Please see attached.",
        attachments=[("acord.pdf", None, "a1")],
    )
    e = CertEmail.from_gmail_api(payload, g.get_attachment)
    facts = extract_request_facts(
        e, pdf_text_extractor=lambda b: "Named Insured: Harbor View LLC\nPolicy # GL-998877"
    )
    assert facts.insured_name == "Harbor View LLC"
    assert "GL-998877" in facts.policy_numbers
    assert not facts.pdf_unreadable


def test_unreadable_pdf_sets_flag():
    blobs = {"a1": b"%PDF"}
    g = FakeGmail([], {}, blobs)
    payload = _gmail_payload(
        body="Named Insured: X\nPolicy #: P1-23456",
        attachments=[("scan.pdf", None, "a1")],
    )
    e = CertEmail.from_gmail_api(payload, g.get_attachment)
    facts = extract_request_facts(e, pdf_text_extractor=lambda b: None)
    assert facts.pdf_unreadable


def test_summarize_for_note_real_content_third_party():
    e = _email()
    e.date = "Fri, 25 Sep 2026 10:00:00 -0400"
    facts = extract_request_facts(e)
    note = summarize_for_note(e, facts, ["doc1"])
    assert "Lawn Buddies LLC" in note
    # Policy digits are masked so the call automation never dials them;
    # the full number is used for matching and filed with the email.
    assert "SHOH" in note
    assert "0178125030" not in note
    assert "third party" in note
    assert "Steffany" in note
    assert "note_text" not in note and "note.txt" not in note
    # The note must survive the phone-number guard.
    from robie_job_engine.ezlynx_discussions import reject_phone_numbers

    reject_phone_numbers(note)


def test_render_email_source_has_headers_and_manifest():
    from robie_job_engine.cert_intake import render_email_source

    blobs = {"a1": b"BYTES"}
    g = FakeGmail([], {}, blobs)
    payload = _gmail_payload(attachments=[("coi.pdf", None, "a1")])
    email = CertEmail.from_gmail_api(payload, g.get_attachment)
    src = render_email_source(email)
    assert "From: Town Clerk" in src
    assert "Subject: Certificate request" in src
    assert "Gmail-ID: g1" in src
    assert "coi.pdf" in src and "sha256" in src

