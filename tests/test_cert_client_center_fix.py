"""FIX 1 (Client Center) + FIX 2 (terse "coi") regression tests.

FIX 1 — EZLynx Client Center notifications (Carlo's standing rule,
2026-09-26): a customer filling out the portal form triggers an email
from cplive@ezlynx.com, subject "EZLynx Client Center Notification",
and EZLynx AUTO-CREATES the task. The worker must recognize these and
NEVER create a duplicate task. The agency's own canned auto-reply
("Re: EZLynx Client Center Notification" from
certificates+canned.response@streetsmart.insurance) is NOT one — it
stays an auto-reply.

FIX 2 — terse "coi" subjects: a client email whose subject is just
"Coi" (plus holder details in the body) is a genuine certificate
request. Bounces and canned auto-replies still classify first, so they
stay held.

Everything here runs offline. No EZLynx, no Gmail, no Zapier.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_filing import (  # noqa: E402
    FilingDeps, file_record,
)
from robie_job_engine.cert_intake import (  # noqa: E402
    CertEmail,
    _html_to_text,
    extract_request_facts,
    is_client_center_sender,
    parse_client_center_notification,
)
from robie_job_engine.cert_applicant_index import (  # noqa: E402
    build_index, match_applicant,
)
from robie_job_engine.cert_task_registry import (  # noqa: E402
    ALREADY_EXISTS, TaskRegistry, decide_task_action,
)
from robie_job_engine.cert_verification import (  # noqa: E402
    ACTION_AUTOREPLY, ACTION_CLIENT_CENTER, ACTION_NEW_REQUEST,
    ACTION_UNKNOWN, HOLD, VERIFIED, classify_requested_action,
    verify_record,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Trimmed from the real 2026-09-25 Client Center notification (Gmail ID
# 1a0d9ed7ae3073e9): same field structure, HTML-only, no text/plain part.
_CC_HTML = """\
<html xmlns="http://www.w3.org/1999/xhtml"><head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>EZLynx Template</title></head>
<body>
<table width="900" border="0" cellspacing="0" cellpadding="0" align="left">
<tbody>
<tr><td colspan="2"><b>A change request has been submitted in EZLynx Client Center.</b></td></tr>
<tr><td><b>Name:</b></td><td><a href="http://ct.ezlynx.com/ls/click?upn=xxx">Toby Martinez</a></td></tr>
<tr><td><b>Email:</b></td><td>hgmdenver@gmail.com</td></tr>
<tr><td><b>Request Type:</b></td><td>requested for certificate:</td></tr>
<tr><td><b>Message to Agent:</b></td><td>Unico Properties LLC, FBRT 1660 Lincoln Owner, LLC, including their affiliates, officers, directors and employees, are named in additional insured. Policies shall include Waiver of Subrogation endorsements. Premises locations: 1660 Lincoln St. Denver, CO 80264</td></tr>
<tr><td><b>Date and Time:</b></td><td>Friday, September 25, 2026 @ 1:56 PM</td></tr>
</tbody></table>
</body></html>"""

_CC_FROM = '"cplive@ezlynx.com" <cplive@ezlynx.com>'
_CC_SUBJECT = "EZLynx Client Center Notification"
_CANNED_FROM = ("Certificates Inbox "
                "<certificates+canned.response@streetsmart.insurance>")


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _gmail_payload(*, frm: str, subject: str, html: str) -> dict:
    return {
        "id": "cc1",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": frm},
                {"name": "Subject", "value": subject},
                {"name": "Date",
                 "value": "Fri, 25 Sep 2026 18:56:53 +0000 (UTC)"},
                {"name": "Message-ID", "value": "<cc1@example>"},
            ],
            "body": {"data": _b64(html)},
        },
    }


_GID = 1000


def _email(*, frm, subject, body):
    global _GID
    _GID += 1
    return CertEmail(
        gmail_id=f"g{_GID}",
        thread_id=f"t{_GID}",
        rfc_message_id=f"<m{_GID}@example.com>",
        from_header=frm,
        subject=subject,
        date="Fri, 26 Sep 2026 10:00:00 -0400",
        body_text=body,
        attachments=[],
    )


def _book():
    return build_index([
        {"account_name": "HGM Denver Holdings LLC",
         "applicant_id": 205884934, "email_primary": "hgmdenver@gmail.com",
         "phones": []},
        {"account_name": "Malas Brothers Painting LLC",
         "applicant_id": 21588160,
         "email_primary": "malasbrotherspainting@gmail.com", "phones": []},
        {"account_name": "Carry Your Burden LLC",
         "applicant_id": 216803074,
         "email_primary": "carrymeyerburdenllc@gmail.com", "phones": []},
    ])


def _run(email, index):
    facts = extract_request_facts(email)
    match = match_applicant(facts, index)
    record = SimpleNamespace(
        facts=facts, match=match, subject=email.subject,
        attachments=email.attachments)
    result = verify_record(record, index, verifier=None)
    return facts, match, result


# ---------------------------------------------------------------------------
# FIX 1: Client Center detection
# ---------------------------------------------------------------------------

def test_html_to_text_extracts_visible_text():
    text = _html_to_text(_CC_HTML)
    assert "Toby Martinez" in text
    assert "hgmdenver@gmail.com" in text
    assert "requested for certificate" in text
    assert "Unico Properties LLC" in text
    assert "<td>" not in text and "<b>" not in text


def test_html_only_payload_yields_body_text():
    email = CertEmail.from_gmail_api(
        _gmail_payload(frm=_CC_FROM, subject=_CC_SUBJECT, html=_CC_HTML))
    assert email.body_text, "HTML-only notification must not parse to empty"
    assert "Toby Martinez" in email.body_text
    assert "hgmdenver@gmail.com" in email.body_text


def test_client_center_sender_exact():
    assert is_client_center_sender(_CC_FROM)
    assert is_client_center_sender("cplive@ezlynx.com")
    # The agency's canned responder quotes the same subject — never a match.
    assert not is_client_center_sender(_CANNED_FROM)
    assert not is_client_center_sender("donotreply@rmis.example.com")


def test_client_center_fields_parsed():
    text = _html_to_text(_CC_HTML)
    fields = parse_client_center_notification(_CC_FROM, _CC_SUBJECT, text)
    assert fields is not None
    assert fields["requester_name"] == "Toby Martinez"
    assert fields["requester_email"] == "hgmdenver@gmail.com"
    assert "certificate" in fields["request_type"].lower()
    assert "Unico Properties LLC" in fields["message"]


def test_client_center_parse_rejects_canned_reply():
    # Same subject shape, wrong sender -> not a Client Center notification.
    assert parse_client_center_notification(
        _CANNED_FROM, "Re: EZLynx Client Center Notification",
        "Your message was received.") is None


def test_client_center_facts_origin_and_requester():
    email = CertEmail.from_gmail_api(
        _gmail_payload(frm=_CC_FROM, subject=_CC_SUBJECT, html=_CC_HTML))
    facts = extract_request_facts(email)
    assert facts.origin == "client_center"
    assert facts.requested_action == "certificate_request"
    assert facts.requester_name == "Toby Martinez"
    assert facts.requester_email == "hgmdenver@gmail.com"


def test_client_center_non_certificate_type_not_origin():
    html = _CC_HTML.replace("requested for certificate:",
                            "requested for policy change:")
    email = CertEmail.from_gmail_api(
        _gmail_payload(frm=_CC_FROM, subject=_CC_SUBJECT, html=html))
    facts = extract_request_facts(email)
    assert facts.origin is None, \
        "a non-certificate portal request must not claim client_center origin"


def test_canned_autoreply_stays_autoreply():
    # The canned responder quoting the CC subject is an auto-reply, and the
    # sender filter runs before any Client Center logic.
    assert classify_requested_action(
        "Re: EZLynx Client Center Notification",
        "Your message was received and a team member will review it.",
        sender="certificates+canned.response@streetsmart.insurance",
    ) == ACTION_AUTOREPLY


def test_e2e_client_center_verifies_via_requester_email():
    index = _book()
    email = CertEmail.from_gmail_api(
        _gmail_payload(frm=_CC_FROM, subject=_CC_SUBJECT, html=_CC_HTML))
    facts, match, result = _run(email, index)
    assert facts.origin == "client_center"
    assert match.status == "MATCHED" and match.applicant_id == 205884934
    assert result.status == VERIFIED
    assert result.applicant_id == 205884934
    assert result.requested_action == ACTION_CLIENT_CENTER
    assert result.origin == "client_center"


def test_client_center_task_action_is_already_exists():
    assert decide_task_action(ACTION_CLIENT_CENTER, None) == ALREADY_EXISTS
    assert decide_task_action(
        ACTION_CLIENT_CENTER, SimpleNamespace(task_id="t1")) == ALREADY_EXISTS


class _FakeZapier:
    def __init__(self):
        self.created = []

    def get_task_state(self, task_id):
        return "unknown"

    def create_task(self, **kw):
        self.created.append(kw)
        raise AssertionError("Client Center must never fire the task Zap")


def test_e2e_client_center_never_fires_task_zap():
    # Even when discussion resolution holds the filing, the task decision
    # is already_exists — the Zap is never fired.
    tmp = tempfile.mkdtemp()
    index = _book()
    email = CertEmail.from_gmail_api(
        _gmail_payload(frm=_CC_FROM, subject=_CC_SUBJECT, html=_CC_HTML))
    facts, match, result = _run(email, index)
    assert result.status == VERIFIED
    record = SimpleNamespace(
        gmail_id=email.gmail_id, subject=email.subject, date=email.date,
        facts=facts, attachments=[])
    zapier = _FakeZapier()

    class _Disc:
        def get_discussions(self, applicant_id):
            return []

    class _NoteWriter:
        def __call__(self, applicant_id, note_text, **kw):
            return {"status": "dry_run", "discussion_id": None,
                    "note_id": None}

    deps = FilingDeps(
        discussions_client=_Disc(), verifier=None,
        note_writer=_NoteWriter(), doc_writer=None, zapier=zapier,
        registry=TaskRegistry(tmp + "/tasks.db"), store=None)
    fr = file_record(record, result, deps, dry_run=True)
    assert fr.task_action == ALREADY_EXISTS
    assert zapier.created == []


# ---------------------------------------------------------------------------
# FIX 2: terse "coi" recognition
# ---------------------------------------------------------------------------

def test_terse_coi_subject_is_new_request():
    assert classify_requested_action(
        "Coi",
        "Resident Name: Ira Rosenbaum, Apt #7F, For painting, 2 days, "
        "$3250, 205 West End Ave NYC") == ACTION_NEW_REQUEST
    assert classify_requested_action(
        "Re: Coi", "Please send over the certificate holder details."
    ) == ACTION_NEW_REQUEST


def test_terse_coi_sets_intake_requested_action():
    e = _email(frm="brett malas <malasbrotherspainting@gmail.com>",
               subject="Coi",
               body="Resident Name: Ira Rosenbaum, Apt #7F, For painting, "
                    "2 days, $3250, 205 West End Ave NYC")
    assert extract_request_facts(e).requested_action == "certificate_request"


def test_coi_word_boundary_only():
    # "coin", "recoil", "scoi" must not trigger — word boundary required.
    e = _email(frm="x@y.com", subject="bitcoin invoice",
               body="Please pay the bitcoin invoice.")
    assert extract_request_facts(e).requested_action is None
    assert classify_requested_action(
        "bitcoin invoice", "Please pay the bitcoin invoice.") == ACTION_UNKNOWN


def test_coi_statement_subject_is_not_a_request():
    # "COI received" is a statement about a COI, not a request for one.
    assert classify_requested_action(
        "COI received", "Attached is the signed COI for your file."
    ) != ACTION_NEW_REQUEST


def test_coi_bounce_stays_autoreply():
    # Bounce and sender filters run FIRST — a bounce quoting "Coi" stays held.
    assert classify_requested_action(
        "Undeliverable: Coi",
        "Delivery has failed to these recipients.",
        sender="postmaster@example.com") == ACTION_AUTOREPLY
    assert classify_requested_action(
        "Re: Coi", "Your message was received.",
        sender="certificates+canned.response@streetsmart.insurance",
    ) == ACTION_AUTOREPLY


def test_coi_ack_still_ack():
    assert classify_requested_action(
        "Coi", "Thanks! We got the certificate.") == "acknowledgement"


def test_e2e_terse_coi_verifies_via_sender_email():
    index = _book()
    e = _email(frm="brett malas <malasbrotherspainting@gmail.com>",
               subject="Coi",
               body="Resident Name: Ira Rosenbaum, Apt #7F, For painting, "
                    "2 days, $3250, 205 West End Ave NYC")
    facts, match, result = _run(e, index)
    assert facts.requested_action == "certificate_request"
    assert result.requested_action == ACTION_NEW_REQUEST
    assert match.status == "MATCHED" and match.applicant_id == 21588160
    assert result.status == VERIFIED
    assert result.applicant_id == 21588160


def test_e2e_vendor_doc_update_with_coi_still_holds():
    # A vendor documentation update mentioning certificates is not a
    # request, even with "coi" in the text.
    index = _book()
    e = _email(frm="Vendor System <noreply@vendor.example.com>",
               subject="Action Required: Updated Insurance & Vendor Documentation",
               body=("Please upload your updated certificate of insurance "
                     "(COI) and vendor documentation to the portal.\n"))
    facts, match, result = _run(e, index)
    assert result.requested_action == ACTION_UNKNOWN
    assert result.status == HOLD
    assert result.applicant_id is None
