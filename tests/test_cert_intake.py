"""Tests for the certificates intake thin slice (all offline, fake clients)."""

from __future__ import annotations

import base64

import pytest

from robie_job_engine.cert_filing import (
    FilingResult,
    FilingTargetMismatch,
    HoldForHuman,
    build_task_payload,
    file_certificate_request,
    resolve_discussion,
    verify_filing_target,
)
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
    mark_processed(_email(gid="g1", mid="<m1@example.com>"), store)
    dup, reason = is_duplicate(_email(gid="g9", mid="<m9@other.com>"), store)
    assert dup and "identical body" in reason


def test_renamed_attachment_same_bytes_is_duplicate():
    store = MemoryDedupeStore()
    mark_processed(_email(atts=[("coi.pdf", b"BYTES", "a1")]), store)
    dup, _ = is_duplicate(
        _email(gid="g2", mid="<m2@x>", atts=[("renamed.pdf", b"BYTES", "a2")]), store
    )
    assert dup  # hash-based, not filename-based


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


# ---------------------------------------------------------------------------
# Filing gate
# ---------------------------------------------------------------------------


def _applicant():
    return {
        "applicant_id": "12345",
        "insured_name": "Lawn Buddies LLC",
        "policy_numbers": ["SHOH0178125030000", "GL-111"],
    }


def _facts():
    return RequestFacts(
        insured_name="Lawn Buddies LLC", policy_numbers=["SHOH0178125030000"]
    )


def _discussion():
    return {"discussion_id": "d9", "title": "Certificate 2026", "applicant_id": "12345"}


def test_gate_passes_on_agreement():
    target = verify_filing_target(_facts(), _applicant(), _discussion())
    assert target["applicant_id"] == "12345"
    assert target["discussion_id"] == "d9"


def test_gate_rejects_insured_mismatch():
    facts = _facts()
    facts.insured_name = "Some Other LLC"
    with pytest.raises(FilingTargetMismatch, match="insured mismatch"):
        verify_filing_target(facts, _applicant(), _discussion())


def test_gate_rejects_unanchored_policy():
    facts = _facts()
    facts.policy_numbers = ["NOT-ON-FILE-1"]
    with pytest.raises(FilingTargetMismatch, match="not on applicant"):
        verify_filing_target(facts, _applicant(), _discussion())


def test_gate_rejects_email_with_no_policy():
    facts = _facts()
    facts.policy_numbers = []
    with pytest.raises(FilingTargetMismatch, match="no policy number"):
        verify_filing_target(facts, _applicant(), _discussion())


def test_gate_rejects_foreign_discussion():
    disc = _discussion()
    disc["applicant_id"] = "99999"
    with pytest.raises(FilingTargetMismatch, match="belongs to"):
        verify_filing_target(_facts(), _applicant(), disc)


# ---------------------------------------------------------------------------
# Discussion resolution
# ---------------------------------------------------------------------------


class FakeDiscussionClient:
    def __init__(self, discussions):
        self._discussions = {d["discussion_id"]: dict(d) for d in discussions}
        self.appended = []
        self._note_seq = 0

    def get_discussions(self, applicant_id):
        return [d for d in self._discussions.values()]

    def get_discussion(self, discussion_id):
        d = self._discussions[discussion_id]
        return {
            "discussionId": discussion_id,
            "title": d["title"],
            "noteCount": d["noteCount"],
            "mostRecentNoteId": d.get("mostRecentNoteId"),
            "lastModified": d.get("lastModified", ""),
        }

    def append_note(self, discussion_id, body, **kwargs):
        self.appended.append((discussion_id, body))
        d = self._discussions[discussion_id]
        d["noteCount"] += 1
        self._note_seq += 1
        d["mostRecentNoteId"] = f"n-{self._note_seq}"
        d["lastModified"] = "2026-09-26T12:00:00+00:00"
        return {"result": "ok"}


def test_resolve_discussion_explicit_id():
    client = FakeDiscussionClient(
        [{"discussion_id": "d1", "title": "Certificate 2026", "noteCount": 2}]
    )
    d = resolve_discussion(client, "12345", "d1")
    assert d["discussion_id"] == "d1"


def test_resolve_discussion_missing_id_raises():
    client = FakeDiscussionClient(
        [{"discussion_id": "d1", "title": "Certificate 2026", "noteCount": 2}]
    )
    with pytest.raises(FilingTargetMismatch, match="not found"):
        resolve_discussion(client, "12345", "nope")


def test_resolve_discussion_single_cert_title():
    client = FakeDiscussionClient(
        [
            {"discussion_id": "d1", "title": "Renewal chat", "noteCount": 1},
            {"discussion_id": "d2", "title": "Certificate 2026", "noteCount": 1},
        ]
    )
    assert resolve_discussion(client, "12345")["discussion_id"] == "d2"


def test_resolve_discussion_ambiguous_holds():
    client = FakeDiscussionClient(
        [
            {"discussion_id": "d1", "title": "Certificate 2025", "noteCount": 1},
            {"discussion_id": "d2", "title": "Certificate 2026", "noteCount": 1},
        ]
    )
    with pytest.raises(HoldForHuman, match="2 certificate discussions"):
        resolve_discussion(client, "12345")


def test_resolve_discussion_none_holds():
    client = FakeDiscussionClient(
        [{"discussion_id": "d1", "title": "Renewal chat", "noteCount": 1}]
    )
    with pytest.raises(HoldForHuman, match="no certificate discussion"):
        resolve_discussion(client, "12345")


# ---------------------------------------------------------------------------
# Full filing path (fakes)
# ---------------------------------------------------------------------------


class FakeApplicantLookup:
    def __init__(self, applicant=None):
        self.applicant = applicant
        self.queries = []

    def find_applicant(self, policy_number):
        self.queries.append(policy_number)
        return self.applicant


class FakeDocumentClient:
    def __init__(self):
        self.uploads = []
        self.docs = []

    def upload(self, applicant_id, document_name, file_bytes, filename=None):
        assert applicant_id and document_name and file_bytes
        doc_id = f"doc-{len(self.uploads) + 1}"
        self.uploads.append((applicant_id, document_name))
        self.docs.append({"document_id": doc_id, "name": document_name})
        return doc_id

    def search(self, applicant_id):
        return [d for d in self.docs]


def _filing_email():
    blobs = {"a1": b"%PDF bytes"}
    g = FakeGmail([], {}, blobs)
    payload = _gmail_payload(
        gid="g-file",
        body="Named Insured: Lawn Buddies LLC\nPolicy #: SHOH0178125030000",
        attachments=[("coi-request.pdf", None, "a1")],
    )
    return CertEmail.from_gmail_api(payload, g.get_attachment)


def test_file_certificate_request_happy_path():
    email = _filing_email()
    facts = extract_request_facts(email)
    dclient = FakeDiscussionClient(
        [{"discussion_id": "d9", "title": "Certificate 2026", "noteCount": 3}]
    )
    doc_client = FakeDocumentClient()
    result = file_certificate_request(
        email=email,
        facts=facts,
        applicant_lookup=FakeApplicantLookup(_applicant()),
        discussion_client=dclient,
        document_client=doc_client,
        note_text=summarize_for_note(email, facts, ["coi-request.pdf"]),
        assignee="steffany_login",
        due_date="2026-09-29",
    )
    assert isinstance(result, FilingResult)
    assert result.applicant_id == "12345"
    assert result.discussion_id == "d9"
    # The email itself is filed first, then its attachments.
    assert len(result.documents) == 2
    assert result.documents[0]["name"].endswith("-email.txt")
    assert result.documents[1]["document_id"] == "doc-2"
    assert all(not d["duplicate_skipped"] for d in result.documents)
    assert result.note["status"] == "filed"
    assert len(dclient.appended) == 1
    assert dclient.appended[0][0] == "d9"
    task = result.task_payload
    assert task["applicant_id"] == "12345"
    assert task["assignee"] == "steffany_login"
    assert task["due_date"] == "2026-09-29"
    assert "Lawn Buddies LLC" in task["note_text"]


def test_rerun_same_delivery_skips_duplicate_documents():
    from datetime import datetime, timezone

    email = _filing_email()
    facts = extract_request_facts(email)
    dclient = FakeDiscussionClient(
        [{"discussion_id": "d9", "title": "Certificate 2026", "noteCount": 3}]
    )
    doc_client = FakeDocumentClient()
    kwargs = dict(
        email=email,
        facts=facts,
        applicant_lookup=FakeApplicantLookup(_applicant()),
        discussion_client=dclient,
        document_client=doc_client,
        note_text=summarize_for_note(email, facts, ["coi-request.pdf"]),
        assignee="steffany_login",
        due_date="2026-09-29",
    )
    first = file_certificate_request(**kwargs)
    assert first.note["status"] == "filed"
    # Same Gmail delivery filed again (e.g. the dedupe store was empty):
    # no document is uploaded twice, and the note guard refuses a second
    # append because the discussion was modified after the first filing.
    cutoff = datetime(2026, 9, 26, 11, 59, tzinfo=timezone.utc)
    result = file_certificate_request(**kwargs, note_guard_cutoff=cutoff)
    assert all(d["duplicate_skipped"] for d in result.documents)
    assert len(doc_client.uploads) == 2  # only the first run's uploads
    assert result.note["status"] == "skipped_guard"
    assert len(dclient.appended) == 1  # the note went in exactly once


def test_gate_failure_writes_nothing():
    email = _filing_email()
    facts = extract_request_facts(email)
    facts.insured_name = "Impostor LLC"  # email disagrees with the record
    dclient = FakeDiscussionClient(
        [{"discussion_id": "d9", "title": "Certificate 2026", "noteCount": 3}]
    )
    doc_client = FakeDocumentClient()
    with pytest.raises(FilingTargetMismatch):
        file_certificate_request(
            email=email,
            facts=facts,
            applicant_lookup=FakeApplicantLookup(_applicant()),
            discussion_client=dclient,
            document_client=doc_client,
            note_text="note",
            assignee="steffany_login",
            due_date="2026-09-29",
        )
    assert doc_client.uploads == [] and dclient.appended == []


def test_unreadable_pdf_holds_before_any_write():
    email = _filing_email()
    facts = extract_request_facts(email)
    facts.pdf_unreadable = True
    dclient = FakeDiscussionClient(
        [{"discussion_id": "d9", "title": "Certificate 2026", "noteCount": 3}]
    )
    doc_client = FakeDocumentClient()
    with pytest.raises(HoldForHuman, match="could not be read"):
        file_certificate_request(
            email=email,
            facts=facts,
            applicant_lookup=FakeApplicantLookup(_applicant()),
            discussion_client=dclient,
            document_client=doc_client,
            note_text="note",
            assignee="steffany_login",
            due_date="2026-09-29",
        )
    assert doc_client.uploads == [] and dclient.appended == []


def test_no_applicant_match_holds():
    email = _filing_email()
    facts = extract_request_facts(email)
    with pytest.raises(HoldForHuman, match="matched no EZLynx applicant"):
        file_certificate_request(
            email=email,
            facts=facts,
            applicant_lookup=FakeApplicantLookup(None),
            discussion_client=FakeDiscussionClient([]),
            document_client=FakeDocumentClient(),
            note_text="note",
            assignee="steffany_login",
            due_date="2026-09-29",
        )


# ---------------------------------------------------------------------------
# Task payload
# ---------------------------------------------------------------------------


def test_build_task_payload_requires_assignee_due_date_note():
    email = _email()
    facts = _facts()
    with pytest.raises(HoldForHuman, match="login username"):
        build_task_payload(
            applicant_id="1", email=email, facts=facts, note="n",
            assignee="", due_date="2026-09-29",
        )
    with pytest.raises(HoldForHuman, match="due_date"):
        build_task_payload(
            applicant_id="1", email=email, facts=facts, note="n",
            assignee="steffany_login", due_date="tomorrow",
        )
    with pytest.raises(HoldForHuman, match="placeholder"):
        build_task_payload(
            applicant_id="1", email=email, facts=facts, note="  ",
            assignee="steffany_login", due_date="2026-09-29",
        )
