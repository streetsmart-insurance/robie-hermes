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
    is_noise,
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
# Noise filtering
# ---------------------------------------------------------------------------


def _noise_email(subject, frm="sender@example.com"):
    """Build a CertEmail with specific subject/from for noise testing."""
    blobs = {}
    g = FakeGmail([], {}, blobs)
    return CertEmail.from_gmail_api(
        _gmail_payload(
            gid="g-noise",
            message_id="<noise@example.com>",
            frm=frm,
            subject=subject,
            body="Some body text",
        ),
        g.get_attachment,
    )


def test_noise_cert_task_callback():
    e = _noise_email("[cert-task-callback] certificate task for applicant 123")
    noise, reason = is_noise(e)
    assert noise
    assert "callback" in reason.lower()


def test_noise_delivery_status_notification():
    e = _noise_email("Delivery Status Notification (Failure)")
    noise, reason = is_noise(e)
    assert noise
    assert "bounce" in reason.lower()


def test_noise_undeliverable():
    e = _noise_email("Undeliverable: Certificate request")
    noise, reason = is_noise(e)
    assert noise
    assert "bounce" in reason.lower()


def test_noise_mailer_daemon():
    e = _noise_email("Some subject", frm="mailer-daemon@example.com")
    noise, reason = is_noise(e)
    assert noise
    assert "bounce" in reason.lower()


def test_noise_automatic_reply():
    e = _noise_email("Automatic reply: Out of office")
    noise, reason = is_noise(e)
    assert noise
    assert "auto-reply" in reason.lower()


def test_noise_out_of_office():
    e = _noise_email("Re: Certificate - Out of Office")
    noise, reason = is_noise(e)
    assert noise


def test_not_noise_real_request():
    e = _noise_email("Certificate request for ABC Construction")
    noise, _ = is_noise(e)
    assert not noise


def test_not_noise_fwd_request():
    e = _noise_email("Fwd: Insurance Certificate Request")
    noise, _ = is_noise(e)
    assert not noise


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


def test_extract_facts_coi_for_compliance_platform_shape():
    # Highway renewal shape (2026-09-29): the insured appears only as
    # "COI for <NAME>" in subject and body — no "Named Insured:" line.
    payload = _gmail_payload(
        frm="Highway <no-reply@highway.com>",
        subject="Renewal COI Request: COI for EPHE LLC Expires Tomorrow",
        body=(
            "Highway COI Request for EPHE LLC\n\nHi there,\n\n"
            "The certificate of insurance (COI) we have on file for *EPHE LLC* "
            "has a policy expiring tomorrow. Please provide a new COI for the "
            "upcoming policy period so we can update our records.\n\n"
            "Policy #: 9300216995\n"
        ),
    )
    e = CertEmail.from_gmail_api(payload)
    facts = extract_request_facts(e)
    assert facts.insured_name == "EPHE LLC"
    assert facts.policy_numbers == ["9300216995"]
    assert facts.requester_email == "no-reply@highway.com"


def test_extract_facts_coi_for_unknown_name_still_extracts():
    # Extraction must not depend on the name being a client: an unknown
    # name extracts cleanly so the matcher can hold it (never misroute).
    payload = _gmail_payload(
        frm="Highway <no-reply@highway.com>",
        subject="Renewal COI Request: COI for Nonexistent Company LLC Expires Tomorrow",
        body="Highway COI Request for Nonexistent Company LLC\nPolicy #: ZZZ999\n",
    )
    e = CertEmail.from_gmail_api(payload)
    facts = extract_request_facts(e)
    assert facts.insured_name == "Nonexistent Company LLC"


def test_extract_facts_coi_for_keeps_covering_tail_off_name():
    # "Request for COI for Ameritesting LLC Covering SilverLini" — the
    # holder tail ("Covering ...") must not glue onto the insured name.
    payload = _gmail_payload(
        subject="Request for COI for Ameritesting LLC Covering SilverLini",
        body="Please send the certificate.",
    )
    e = CertEmail.from_gmail_api(payload)
    facts = extract_request_facts(e)
    assert facts.insured_name == "Ameritesting LLC"


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
# Quoted-history hygiene
# ---------------------------------------------------------------------------
# On RE: threads the newest block is the request; quoted history below
# it (or our own canned auto-reply) must not feed extraction.

def test_quoted_history_names_stale_insured_ignored():
    body = (
        "Hi, please issue a certificate for Harbor View LLC\n"
        "\n"
        "On Fri, 25 Sep 2026 at 09:00, clerk@exampletown.gov wrote:\n"
        "> Named Insured: Lawn Buddies LLC\n"
        "> Policy #: SHOH0178125030000\n"
    )
    e = _email(body=body)
    facts = extract_request_facts(e)
    assert facts.insured_name == "Harbor View LLC"
    assert "Lawn Buddies LLC" not in (facts.insured_name or "")
    assert facts.additional_insured_names == []


def test_gt_quoted_lines_stripped():
    body = (
        "Certificate for Harbor View LLC\n"
        "> Certificate for Lawn Buddies LLC\n"
        "> Thanks!\n"
    )
    e = _email(body=body)
    facts = extract_request_facts(e)
    assert facts.insured_name == "Harbor View LLC"


def test_own_canned_reply_quoted_back_is_stripped():
    body = (
        "Certificate Holder: Example Town Hall\n"
        "\n"
        "Hello, We have received your request for a certificate of "
        "insurance. Please be sure to include the name, address, and a "
        "fax/email to send the certificate of insurance to.\n"
    )
    e = _email(body=body)
    facts = extract_request_facts(e)
    # Our canned words are never the insured; the holder still extracts.
    assert facts.insured_name is None
    assert "Example Town Hall" in facts.holder_names


# ---------------------------------------------------------------------------
# Multi-insured emails
# ---------------------------------------------------------------------------

def test_multi_insured_email_extracts_both_names():
    e = _email(
        body="Please issue a certificate of insurance for ABC LLC "
             "and XYZ Inc\nThanks!"
    )
    facts = extract_request_facts(e)
    assert facts.insured_name == "ABC LLC and XYZ Inc"
    assert facts.additional_insured_names == ["ABC LLC", "XYZ Inc"]


def test_single_name_with_and_inside_is_not_split_wrong():
    # "Smith and Sons LLC" still extracts as one blob; the sweep tries
    # the whole blob first, so the one client keeps matching.
    e = _email(body="Please issue a certificate for Smith and Sons LLC\n")
    facts = extract_request_facts(e)
    assert facts.insured_name == "Smith and Sons LLC"
    assert facts.additional_insured_names == ["Smith", "Sons LLC"]


# ---------------------------------------------------------------------------
# Phone extraction (ambiguous-name tie-break signal only)
# ---------------------------------------------------------------------------

def test_phones_extracted_for_tie_break():
    e = _email(
        body="Named Insured: Robert Lake\n"
             "Please call me at (212) 555-0101 about the certificate.\n"
    )
    facts = extract_request_facts(e)
    assert facts.phones == ["2125550101"]


def test_policy_number_not_mistaken_for_phone():
    e = _email(body="Named Insured: X\nPolicy #: 9300216995")
    facts = extract_request_facts(e)
    assert "9300216995" in facts.policy_numbers
    assert "9300216995" not in facts.phones


# ---------------------------------------------------------------------------
# PDF filenames as a weak insured-name source
# ---------------------------------------------------------------------------

def test_filename_suggests_insured_when_body_has_no_name():
    blobs = {"a1": b"%PDF"}
    g = FakeGmail([], {}, blobs)
    # The PDF has no readable text (extractor returns None), so the
    # filename is the only name signal — recorded as a weak candidate.
    e = CertEmail.from_gmail_api(
        _gmail_payload(
            body="Please see attached.",
            attachments=[("arias_con_certificate_of_liability_ins.pdf",
                          None, "a1")],
        ),
        g.get_attachment,
    )
    facts = extract_request_facts(e, pdf_text_extractor=lambda b: None)
    assert facts.insured_name == "arias con"
    assert facts.insured_name_source == "pdf_filename"


def test_filename_never_overrides_body_name():
    blobs = {"a1": b"%PDF"}
    g = FakeGmail([], {}, blobs)
    e = CertEmail.from_gmail_api(
        _gmail_payload(
            body="Named Insured: Harbor View LLC",
            attachments=[("arias_con_certificate_of_liability_ins.pdf",
                          None, "a1")],
        ),
        g.get_attachment,
    )
    facts = extract_request_facts(e, pdf_text_extractor=lambda b: None)
    assert facts.insured_name == "Harbor View LLC"
    assert facts.insured_name_source == "body"


# ---------------------------------------------------------------------------
# Filename candidate -> applicant matching (confirm-only)
# ---------------------------------------------------------------------------

def _filename_index():
    from robie_job_engine.cert_applicant_index import build_index
    return build_index(
        [{"account_name": "Fonseca General Contractor LLC",
          "applicant_id": 116349171, "email_primary": "office@fonsecagc.com",
          "phones": []}],
        source_path="/tmp/fake.xlsx",
    )


def _filename_facts(filename):
    from robie_job_engine.cert_applicant_index import match_applicant  # noqa
    blobs = {"a1": b"%PDF"}
    g = FakeGmail([], {}, blobs)
    e = CertEmail.from_gmail_api(
        _gmail_payload(
            body="Please see attached.",
            attachments=[(filename, None, "a1")],
        ),
        g.get_attachment,
    )
    return extract_request_facts(e, pdf_text_extractor=lambda b: None)


def test_filename_only_known_client_matches():
    from robie_job_engine.cert_applicant_index import (
        MATCHED, match_applicant)
    facts = _filename_facts("fonseca_general_contractor_llc_cert.pdf")
    assert facts.insured_name == "fonseca general contractor llc"
    assert facts.insured_name_source == "pdf_filename"
    # Confirm-only: the candidate still has to hit the index.
    res = match_applicant(facts, _filename_index())
    assert res.status == MATCHED
    assert res.applicant_id == 116349171


def test_filename_only_non_client_stays_held():
    from robie_job_engine.cert_applicant_index import (
        NO_MATCH, match_applicant)
    facts = _filename_facts("arias_con_certificate_of_liability_ins.pdf")
    assert facts.insured_name == "arias con"
    # No index hit -> held. A filename can never invent a client.
    res = match_applicant(facts, _filename_index())
    assert res.status == NO_MATCH
    assert res.applicant_id is None
