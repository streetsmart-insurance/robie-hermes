"""Edge-case accuracy suite for the certificate worker.

Every test here is a real shape seen (or plausibly seen) in
certificates@streetsmart.insurance. The bar is 100%: each test asserts the
exact expected outcome — verified to the right applicant, or held with the
right reason. A single failure blocks activation.

Covers:
  - extraction: subject-only names, sentence runoff, internal periods,
    MC numbers vs policy numbers, holders vs insureds, unicode names
  - matching: DBA fragments, ambiguous names/DBAs, email cross-checks
  - classification: acks, canned replies, vendor digests, expiry alerts,
    cancel-replace language
  - end-to-end: intake -> match -> verify on the three genuine 2026-09-26
    requests plus adversarial shapes
"""
from __future__ import annotations

import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_intake import (  # noqa: E402
    CertAttachment,
    CertEmail,
    _clean_name,
    _truncate_sentence_runoff,
    dedupe_keys,
    extract_request_facts,
    extract_subject_insured,
    is_duplicate,
)
from robie_job_engine.cert_applicant_index import (  # noqa: E402
    ApplicantIndex,
    build_index,
    match_applicant,
)
from robie_job_engine.cert_verification import (  # noqa: E402
    ACTION_ACK,
    ACTION_AUTOREPLY,
    ACTION_NEW_REQUEST,
    ACTION_UNKNOWN,
    HOLD,
    VERIFIED,
    classify_requested_action,
    verify_record,
)

_GID = 0


def _email(*, frm, subject, body, attachments=()):
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
        attachments=[
            CertAttachment(filename=fn, mime_type="application/pdf",
                           content=data)
            for fn, data in attachments
        ],
    )


def _book():
    return build_index([
        {"account_name": "AMOUR BUSINESS GROUP LLC DBA ABG TRANSPORTATION",
         "applicant_id": 147197937, "email_primary": "", "phones": []},
        {"account_name": "LA Burger LLC",
         "applicant_id": 211722620,
         "email_primary": "mike@littleandysburgers.com", "phones": []},
        {"account_name": "Springfield Plumbing LLC",
         "applicant_id": 111, "email_primary": "", "phones": []},
        {"account_name": "Springfield Plumbing LLC",
         "applicant_id": 222, "email_primary": "", "phones": []},
        {"account_name": "EASTERN FOODS INC DBA TONYS PIZZA",
         "applicant_id": 333, "email_primary": "", "phones": []},
        {"account_name": "WESTERN FOODS INC DBA TONYS PIZZA",
         "applicant_id": 444, "email_primary": "", "phones": []},
        {"account_name": "Harbor Marine Services LLC",
         "applicant_id": 555, "email_primary": "harbor@example.com",
         "phones": []},
        {"account_name": "Jose Garcia Landscaping LLC",
         "applicant_id": 666, "email_primary": "", "phones": []},
    ])


def _run(email, index, pdf_texts=None):
    """Intake -> match -> verify, the way the runner does it (read-only)."""
    facts = extract_request_facts(
        email,
        pdf_text_extractor=(lambda b: pdf_texts) if pdf_texts else None)
    match = match_applicant(facts, index)
    record = SimpleNamespace(
        facts=facts, match=match, subject=email.subject,
        attachments=email.attachments)
    result = verify_record(record, index, verifier=None)
    return facts, match, result


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_subject_only_name_is_extracted():
    e = _email(
        frm="RMIS <donotreply@rmis.example.com>",
        subject="Renewal Certificate Request- Abg Transportation MC1121844",
        body="Please provide a certificate of insurance.\nThank you.",
    )
    facts = extract_request_facts(e)
    assert facts.insured_name == "Abg Transportation"
    # An MC/DOT number is a motor-carrier identifier, never a policy number.
    assert facts.policy_numbers == []


def test_sentence_runoff_truncated_from_body_capture():
    e = _email(
        frm="Michael Solomon <mike@littleandysburgers.com>",
        subject="Re: Certificate of Insurance",
        body=("Certificate of Insurance for LA Burger LLC. This certificate "
              "confirms that the listed insurance is in force.\n"),
    )
    facts = extract_request_facts(e)
    assert facts.insured_name == "LA Burger LLC"


def test_legitimate_internal_periods_preserved():
    assert _truncate_sentence_runoff(
        "St. Mary Hospital. Please send the certificate today") == \
        "St. Mary Hospital"
    assert _truncate_sentence_runoff(
        "J. P. Morgan. Please issue a certificate today now") == \
        "J. P. Morgan"
    assert _truncate_sentence_runoff("MAR Engineering, P.C.") == \
        "MAR Engineering, P.C."
    assert _truncate_sentence_runoff(
        "Main St. Pizza Shop. Please send the certificate today") == \
        "Main St. Pizza Shop"
    assert _truncate_sentence_runoff("John Smith Jr. Please send it today") == \
        "John Smith Jr"
    assert _truncate_sentence_runoff("LA Burger LLC") == "LA Burger LLC"


def test_clean_name_applies_runoff_truncation():
    assert _clean_name(
        "LA Burger LLC. This certificate confirms coverage") == "LA Burger LLC"


def test_unicode_name_extracted_and_matchable():
    # Accented name in the email still matches the ASCII book entry —
    # normalization strips accents on both sides.
    e = _email(
        frm="Jose Garcia <jose@example.com>",
        subject="COI - José García Landscaping LLC",
        body="Please send a COI for José García Landscaping LLC.\n",
    )
    facts = extract_request_facts(e)
    assert facts.insured_name == "José García Landscaping LLC"
    index = _book()
    match = match_applicant(facts, index)
    assert match.status == "MATCHED" and match.applicant_id == 666


def test_holder_is_recorded_not_matched():
    e = _email(
        frm="Michael Solomon <mike@littleandysburgers.com>",
        subject="Certificate of Insurance LA Burger LLC to Anderson Market",
        body=("Certificate Holder: Anderson Market\n"
              "Please issue the certificate.\n"),
    )
    facts = extract_request_facts(e)
    assert facts.insured_name == "LA Burger LLC"
    assert any("Anderson Market" in h for h in facts.holder_names)
    # The holder must never be used as a match key.
    index = _book()
    match = match_applicant(facts, index)
    assert match.applicant_id == 211722620


def test_policy_keyword_number_extracted():
    e = _email(
        frm="Town Clerk <clerk@exampletown.gov>",
        subject="Certificate request",
        body="Named Insured: Lawn Buddies LLC\nPolicy #: SHOH0178125030000\n",
    )
    facts = extract_request_facts(e)
    assert facts.policy_numbers == ["SHOH0178125030000"]


def test_multiple_policy_numbers_all_extracted():
    e = _email(
        frm="Broker <broker@example.com>",
        subject="Certificates needed",
        body=("Named Insured: Harbor Marine Services LLC\n"
              "Policy: GL-88213\nPolicy: WC-99100\n"),
    )
    facts = extract_request_facts(e)
    assert facts.policy_numbers == ["GL-88213", "WC-99100"]


def test_empty_body_subject_only_no_crash():
    e = _email(
        frm="RMIS <donotreply@rmis.example.com>",
        subject="Renewal Certificate Request- Abg Transportation MC1121844",
        body="",
    )
    facts = extract_request_facts(e)
    assert facts.insured_name == "Abg Transportation"


def test_attachment_pdf_text_fills_missing_insured():
    e = _email(
        frm="Agent <agent@example.com>",
        subject="See attached",
        body="Please see attached.\n",
        attachments=[("acord.pdf", b"%PDF-1.4 fake")],
    )
    facts = extract_request_facts(
        e, pdf_text_extractor=lambda b: "Named Insured: Harbor Marine Services LLC\n")
    assert facts.insured_name == "Harbor Marine Services LLC"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_dba_fragment_matches_legal_name():
    index = _book()
    facts = SimpleNamespace(insured_name="Abg Transportation", dba=None,
                            requester_email="rmis@registrymonitoring.com")
    match = match_applicant(facts, index)
    assert match.status == "MATCHED"
    assert match.applicant_id == 147197937


def test_dba_fragment_with_apostrophe_matches():
    # "Tony's Pizza" in the email matches the book's "TONYS PIZZA" DBA.
    index = build_index([
        {"account_name": "EASTERN FOODS INC DBA TONY'S PIZZA",
         "applicant_id": 333, "email_primary": "", "phones": []},
    ])
    facts = SimpleNamespace(insured_name="Tony's Pizza", dba=None,
                            requester_email="orders@example.com")
    match = match_applicant(facts, index)
    assert match.status == "MATCHED"
    assert match.applicant_id == 333


def test_dba_fragment_shared_by_two_applicants_holds():
    index = _book()
    facts = SimpleNamespace(insured_name="Tonys Pizza", dba=None,
                            requester_email="orders@example.com")
    match = match_applicant(facts, index)
    assert match.status == "AMBIGUOUS"
    assert set(match.candidates) == {333, 444}


def test_dba_fragment_vs_sender_email_conflict_holds():
    # DBA run says 147197937, but the sender's email is Harbor Marine's
    # address on file (555): fail closed, never pick one.
    index = _book()
    facts = SimpleNamespace(insured_name="Abg Transportation", dba=None,
                            requester_email="harbor@example.com")
    match = match_applicant(facts, index)
    assert match.status == "AMBIGUOUS"
    assert match.applicant_id is None


def test_exact_name_collision_holds():
    index = _book()
    facts = SimpleNamespace(insured_name="Springfield Plumbing LLC", dba=None,
                            requester_email="someone@example.com")
    match = match_applicant(facts, index)
    assert match.status == "AMBIGUOUS"


def test_sender_email_match_when_name_unknown():
    index = _book()
    facts = SimpleNamespace(insured_name="Some Unknown Entity", dba=None,
                            requester_email="harbor@example.com")
    match = match_applicant(facts, index)
    assert match.status == "MATCHED"
    assert match.applicant_id == 555


def test_unknown_name_no_email_is_no_match():
    index = _book()
    facts = SimpleNamespace(insured_name="Haris Uddin", dba=None,
                            requester_email="eimy@streetsmart.insurance")
    match = match_applicant(facts, index)
    assert match.status == "NO_MATCH"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_acknowledgement_is_not_a_new_request():
    assert classify_requested_action(
        "Re: your certificate",
        "Thanks! We received the certificate.") == ACTION_ACK
    assert classify_requested_action(
        "Certificate",
        "Confirming receipt of the COI, thank you.") == ACTION_ACK


def test_canned_autoreply_is_not_a_new_request():
    body = ("This is an automated response. Your message was received and "
            "a team member will review it shortly.")
    # The quoted original subject carries request language — the auto-reply
    # signal must win so no duplicate task is minted.
    assert classify_requested_action(
        "Re: Renewal Certificate Request- Abg Transportation",
        body) == ACTION_AUTOREPLY
    # Sender-based: the mailbox's own canned responder, thin body or not.
    assert classify_requested_action(
        "Re: Renewal Certificate Request- Abg Transportation",
        "Your message was received.",
        sender="certificates+canned.response@streetsmart.insurance"
    ) == ACTION_AUTOREPLY
    # But a genuine RMIS request from a donotreply sender is NOT an
    # auto-reply — sender detection stays narrow on purpose.
    assert classify_requested_action(
        "Renewal Certificate Request- Abg Transportation MC1121844",
        "Please provide a renewal certificate of insurance.",
        sender="donotreply@rmis.example.com") == ACTION_NEW_REQUEST


def test_vendor_digest_is_not_a_request():
    assert classify_requested_action(
        "Your daily certificate compliance summary",
        "Here is today's Certificial digest of certificate activity.") \
        == ACTION_UNKNOWN


def test_expiry_alert_is_not_a_request():
    assert classify_requested_action(
        "A Policy is Expiring in 13 Days",
        "This is a reminder that a policy tracked in myCOI is expiring.") \
        == ACTION_UNKNOWN


def test_cancel_replace_language_is_not_a_new_request():
    # "Please cancel that certificate request" must never mint a new task.
    assert classify_requested_action(
        "Re: certificate",
        "Please cancel that certificate request, we no longer need it.") \
        != ACTION_NEW_REQUEST


def test_genuine_ask_is_a_new_request():
    assert classify_requested_action(
        "Certificate of Insurance LA Burger LLC to Anderson Market",
        "Please issue a certificate of insurance.") == ACTION_NEW_REQUEST


# ---------------------------------------------------------------------------
# End to end: intake -> match -> verify
# ---------------------------------------------------------------------------

def test_e2e_abg_transportation_verifies():
    index = _book()
    e = _email(
        frm="RMIS <donotreply@rmis.example.com>",
        subject="Renewal Certificate Request- Abg Transportation MC1121844",
        body=("Please provide a renewal certificate of insurance for "
              "Abg Transportation.\n"),
    )
    facts, match, result = _run(e, index)
    assert facts.insured_name == "Abg Transportation"
    assert match.status == "MATCHED" and match.applicant_id == 147197937
    assert result.status == VERIFIED
    assert result.applicant_id == 147197937


def test_e2e_la_burger_verifies_via_sender_email():
    index = _book()
    e = _email(
        frm="Michael Solomon <mike@littleandysburgers.com>",
        subject="Re: Certificate of Insurance LA Burger LLC to Anderson Market",
        body=("Certificate of Insurance for LA Burger LLC. This certificate "
              "confirms that the listed insurance is in force.\n"
              "Certificate Holder: Anderson Market\n"),
    )
    facts, match, result = _run(e, index)
    assert facts.insured_name == "LA Burger LLC"
    assert result.status == VERIFIED
    assert result.applicant_id == 211722620


def test_e2e_haris_uddin_holds_for_human():
    index = _book()
    e = _email(
        frm="Eimy Ramos <eimy@streetsmart.insurance>",
        subject="Fwd: Haris Uddin 008265/15/00",
        body="Please see the forwarded request below.\n",
    )
    facts, match, result = _run(e, index)
    assert facts.insured_name == "Haris Uddin"
    assert match.status == "NO_MATCH"
    assert result.status == HOLD
    assert result.applicant_id is None


def test_genuine_request_with_donotreply_footer_is_new_request():
    # RMIS-style: real request language in the body plus a "do not reply"
    # footer. The footer must not turn a genuine request into an auto-reply.
    body = ("Please provide a renewal certificate of insurance for "
            "Abg Transportation MC1121844.\n\n"
            "This is an automated notification. Please do not reply to "
            "this email.")
    assert classify_requested_action(
        "Renewal Certificate Request- Abg Transportation MC1121844",
        body,
        sender="donotreply@rmis.example.com") == ACTION_NEW_REQUEST
    # But a pure auto-reply body with no request language still holds,
    # even from an unknown sender.
    assert classify_requested_action(
        "Re: Renewal Certificate Request- Abg Transportation",
        "This is an automated response. Your message was received.",
        sender="someone@example.com") == ACTION_AUTOREPLY


def test_e2e_canned_autoreply_holds_never_tasks():
    # The mailbox's own canned responder quoting a real request subject:
    # matched applicant or not, it must hold and never verify.
    index = _book()
    e = _email(
        frm="certificates+canned.response@streetsmart.insurance",
        subject="Re: Renewal Certificate Request- Abg Transportation MC1121844",
        body=("This is an automated response. Your message was received and "
              "a team member will review it shortly.\n"),
    )
    facts, match, result = _run(e, index)
    assert result.status == HOLD
    assert result.applicant_id is None
    assert result.requested_action == ACTION_AUTOREPLY
    # And the task layer must refuse it a task as well.
    from robie_job_engine.cert_task_registry import decide_task_action, HOLD as TASK_HOLD
    assert decide_task_action(result.requested_action, None) == TASK_HOLD


def test_e2e_conflicting_body_and_subject_hold():
    index = _book()
    e = _email(
        frm="Broker <broker@example.com>",
        subject="COI - Beta LLC",
        body="Named Insured: Acme Corp\nPlease issue the certificate.\n",
    )
    facts, match, result = _run(e, index)
    assert result.status == HOLD
    assert any("conflict" in r.lower() for r in result.hold_reasons)


def test_e2e_ambiguous_name_holds():
    index = _book()
    e = _email(
        frm="Plumber <joe@example.com>",
        subject="COI - Springfield Plumbing LLC",
        body="Please send a certificate for Springfield Plumbing LLC.\n",
    )
    facts, match, result = _run(e, index)
    assert match.status == "AMBIGUOUS"
    assert result.status == HOLD


def test_e2e_forwarded_request_still_matches_insured():
    # A staffer forwards a client request: the forwarder is not the
    # insured, the insured in the body still drives the match.
    index = _book()
    e = _email(
        frm="Eimy Ramos <eimy@streetsmart.insurance>",
        subject="Fwd: COI needed",
        body=("Forwarding the request below.\n\n"
              "Named Insured: Harbor Marine Services LLC\n"
              "Please issue a certificate.\n"),
    )
    facts, match, result = _run(e, index)
    assert facts.insured_name == "Harbor Marine Services LLC"
    assert result.status == VERIFIED
    assert result.applicant_id == 555


def test_duplicate_message_detected():
    from robie_job_engine.cert_intake import mark_processed
    seen = set()

    class Store:
        def seen(self, key):
            return key in seen

        def mark(self, key, meta=None):
            seen.add(key)

    store = Store()
    e = _email(
        frm="RMIS <donotreply@rmis.example.com>",
        subject="Renewal Certificate Request- Abg Transportation MC1121844",
        body="Please provide a certificate.\n",
    )
    dup, _ = is_duplicate(e, store)
    assert not dup
    mark_processed(e, store)
    dup2, reason = is_duplicate(e, store)
    assert dup2
    assert "already processed" in reason


def test_subject_extractor_shared_by_intake_and_verifier():
    # One implementation, used by both stages: they can never disagree.
    from robie_job_engine import cert_verification as cv
    assert cv.extract_subject_insured is extract_subject_insured
    assert (cv.extract_subject_insured(
        "Renewal Certificate Request- Abg Transportation MC1121844")
        == "Abg Transportation")
