"""Regression tests for the 2026-09-27 certificate audit fixes (PR #609).

Covers the seven audit findings:
1. Vendor systems (RMIS, Highway, myCOI, Certificial, Next) name the insured
   in the message — extraction must use the named insured, never the vendor
   sender.
2. Follow-ups ("still waiting for my certificate", "send it to this email",
   "where is the certificate") classify as new requests.
3. Carrier change requests (Next "requested changes to certificate").
4. Subject-extracted holders: *...* instruction tails and truncation
   fragments are stripped; a thank-you reply (LA Burger) is an
   acknowledgement HOLD, never verified.
5. Note text: "(policy number not shown)" (no doubled "policy"); no
   trailing-period leakage in names.
6. ezlynx_oauth_token takes the secret resource name FIRST positionally.
7. Canned auto-replies land in the automated HOLD bucket — even when the
   insured sources disagree — never in conflict/no-applicant/unknown.

Everything here runs offline. No EZLynx, no Gmail, no Zapier.
"""

from __future__ import annotations

import inspect
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_intake import (  # noqa: E402
    CertEmail,
    _clean_name,
    extract_request_facts,
    extract_subject_insured,
    summarize_for_note,
)
from robie_job_engine.cert_applicant_index import (  # noqa: E402
    _sender_is_vendor_system,
    build_index,
    match_applicant,
)
from robie_job_engine.cert_verification import (  # noqa: E402
    ACTION_ACK,
    ACTION_AUTOREPLY,
    ACTION_NEW_REQUEST,
    ACTION_UNKNOWN,
    HOLD,
    _body_is_coi_request,
    classify_requested_action,
    ezlynx_oauth_token,
    verify_record,
)


def _facts(**kw):
    base = dict(insured_name=None, policy_numbers=[], requester_name=None,
                requester_email=None, holder_names=[], pdf_texts=[],
                pdf_unreadable=False, requester_is_third_party=False,
                body_text="")
    base.update(kw)
    return SimpleNamespace(**base)


def _record(subject, facts):
    return SimpleNamespace(subject=subject, facts=facts,
                           match=SimpleNamespace(status="NO_MATCH",
                                                 applicant_id=None,
                                                 candidates=[]),
                           attachments=[])


def _email(subject, body, frm):
    return CertEmail(gmail_id="test", thread_id="t", rfc_message_id="m",
                     from_header=frm, subject=subject, date="",
                     body_text=body, attachments=[])


# ---------------------------------------------------------------------------
# FIX 1 — vendor-system insured extraction
# ---------------------------------------------------------------------------

def test_rmis_company_name_not_captured_as_insured():
    body = ("Dear Producer:\n"
            "The certificate on file has coverage(s) that are due to expire "
            "for your insured:\nCompany Name:\nAbg Transportation\n"
            "Legal Name:\nAmour Business Group LLC\nAddress:\n123 Main St")
    facts = extract_request_facts(_email("Renewal Certificate Request- "
                                         "Abg Transportation MC1121844",
                                         body, "rmis@registrymonitoring.com"))
    assert facts.insured_name == "Abg Transportation", facts.insured_name


def test_rmis_empty_company_name_falls_through_to_legal_name():
    body = "Company Name:\n\nLegal Name:\nAmour Business Group LLC"
    facts = extract_request_facts(_email("Renewal Certificate Request", body,
                                         "rmis@registrymonitoring.com"))
    assert facts.insured_name == "Amour Business Group LLC", facts.insured_name


def test_highway_have_on_file_for_names_insured():
    body = ("The certificate of insurance (COI) we have on file for "
            "*ECMANAGEMENT GROUP INCORPORATED dba TRUTHH TRANSPORT LLC* "
            "has a policy expiring tomorrow.")
    facts = extract_request_facts(
        _email("Renewal COI Request: COI for ECMANAGEMENT GROUP INCORPORATED "
               "dba TRUTHH TRANSPORT LLC Expires Tomorrow",
               body, "no-reply@highway.com"))
    assert facts.insured_name == (
        "ECMANAGEMENT GROUP INCORPORATED dba TRUTHH TRANSPORT LLC"), \
        facts.insured_name


def test_next_one_of_your_customers_names_insured():
    body = ("One of your customers, Advanced Electric Design & Service LLC "
            "(Joe@advancedelectricdesign.net), received a new request for "
            "changes to their certificate")
    facts = extract_request_facts(
        _email("Progress Property LLC has requested changes to Advanced "
               "Electric Design & Service LLC's certificate",
               body, "hello@nextinsurance.com"))
    assert facts.insured_name == "Advanced Electric Design & Service LLC", \
        facts.insured_name


def test_mycoi_your_insured_names_insured():
    body = ("We need your help to show your insured, TMA Contracting LLC, "
            "is renewing their policies with you.")
    facts = extract_request_facts(
        _email("A Policy is Expiring in 13 Days", body,
               "noreply@mycoisolution.com"))
    assert facts.insured_name == "TMA Contracting LLC", facts.insured_name


def test_mycoi_expiring_policy_stays_unclassifiable():
    # The vendor compliance notice names the insured for routing but is
    # NOT a certificate request — extraction must not flip classification.
    body = ("We need your help to show your insured, TMA Contracting LLC, "
            "is renewing their policies with you.")
    assert classify_requested_action("A Policy is Expiring in 13 Days", body) \
        == ACTION_UNKNOWN


def test_highway_document_disclaimer_never_a_request():
    # "This is NOT a request for a COI" beats the word-boundary coi rule.
    body = ("This is NOT a request for a COI. Highway is requesting a copy "
            "of your insured's full Motor Truck Cargo Insurance Policy "
            "document. Thanks, The Highway Team")
    assert not _body_is_coi_request(body, "no-reply@highway.com")


def test_vendor_senders_never_match_on_email():
    for addr in ("rmis@registrymonitoring.com",
                 "no-reply@highway.com",
                 "noreply@mycoisolution.com",
                 "hello@nextinsurance.com",
                 "noreply@certificial.com"):
        assert _sender_is_vendor_system(addr), addr
    assert not _sender_is_vendor_system("client@example.com")
    assert not _sender_is_vendor_system(None)


def test_vendor_sender_email_does_not_verify():
    # Even if the vendor address sits on the applicant record, the email
    # match is skipped when no insured name matched first (sender !=
    # insured).
    row = {"applicant_id": 1, "account_name": "Some Client LLC",
           "email_primary": "no-reply@highway.com", "phones": []}
    index = build_index([row])
    facts = _facts(requester_email="no-reply@highway.com")
    assert match_applicant(index, facts).status == "NO_MATCH"


# ---------------------------------------------------------------------------
# FIX 2 — follow-ups classify as requests
# ---------------------------------------------------------------------------

def test_still_waiting_for_certificate_is_request():
    assert classify_requested_action(
        "Re:", "Good morning I'm still waiting for my certificate") \
        == ACTION_NEW_REQUEST


def test_send_it_to_this_email_is_request():
    assert classify_requested_action(
        "Re: certificate", "Please send it to this email") \
        == ACTION_NEW_REQUEST


def test_where_is_the_certificate_is_request():
    assert classify_requested_action(
        "Re:", "Hello, where is the certificate I asked for last week?") \
        == ACTION_NEW_REQUEST


def test_polite_signoff_does_not_demote_request():
    # "Please provide current COI to the following thank you" — the
    # "thank you" is a sign-off; the request language wins.
    assert classify_requested_action(
        "Please provide current COI to the following thank you",
        "Please provide current COI to the following. Thank you.") \
        == ACTION_NEW_REQUEST


def test_thank_you_for_certificate_is_ack():
    # Gratitude for a certificate already received is an acknowledgement.
    assert classify_requested_action(
        "Re:", "Thank you for the renewal certificate!") == ACTION_ACK


# ---------------------------------------------------------------------------
# FIX 3 — carrier change requests
# ---------------------------------------------------------------------------

def test_next_requested_changes_is_request():
    assert classify_requested_action(
        "Progress Property LLC has requested changes to Advanced Electric "
        "Design & Service LLC's certificate",
        "One of your customers, Advanced Electric Design & Service LLC, "
        "received a new request for changes to their certificate") \
        == ACTION_NEW_REQUEST


def test_next_subject_extracts_insured_not_holder():
    insured = extract_subject_insured(
        "Progress Property LLC has requested changes to Advanced Electric "
        "Design & Service LLC's certificate")
    assert insured == "Advanced Electric Design & Service LLC", insured


# ---------------------------------------------------------------------------
# FIX 4 — holder cleanup + thank-you stays held
# ---------------------------------------------------------------------------

def test_star_instruction_tail_stripped_from_holder():
    assert _clean_name("Anderson Market & Metrovation *WORDING LOCATED ON "
                       "PAGE 2*") == "Anderson Market & Metrovation"


def test_subject_insured_strips_instruction_tail():
    insured = extract_subject_insured(
        "Re: Certificate of Insurance LA Burger LLC to Anderson Market & "
        "Metrovation *WORDING LOCATED ON PAGE 2*")
    assert insured == "LA Burger LLC", insured


def test_trailing_period_leakage_removed():
    assert _clean_name("ALTI TRANSPORT LLC .") == "ALTI TRANSPORT LLC"


def test_la_burger_thank_you_is_ack_hold():
    # The real LA Burger reply: body "Thank you!", subject with the
    # instruction tail. After holder cleanup it must HOLD as an
    # acknowledgement — never verify.
    res = verify_record(
        _record("Re: Certificate of Insurance LA Burger LLC to Anderson "
                "Market & Metrovation *WORDING LOCATED ON PAGE 2*",
                _facts(body_text="Thank you!")),
        index=None, verifier=None)
    assert res.requested_action == ACTION_ACK
    assert res.status == HOLD


# ---------------------------------------------------------------------------
# FIX 5 — note text
# ---------------------------------------------------------------------------

def test_note_policy_bit_has_no_doubled_policy():
    email = _email("Certificate request", "Please issue a certificate.",
                   "client@example.com")
    facts = _facts(insured_name="Test LLC", policy_numbers=[])
    note = summarize_for_note(email, facts, [])
    assert "(policy policy number not shown)" not in note
    assert "(policy number not shown)" in note


# ---------------------------------------------------------------------------
# FIX 6 — oauth positional hazard
# ---------------------------------------------------------------------------

def test_oauth_secret_is_first_positional():
    params = list(inspect.signature(ezlynx_oauth_token).parameters)
    assert params[0] == "secret_resource", params


# ---------------------------------------------------------------------------
# FIX 7 — canned auto-replies land in the automated bucket
# ---------------------------------------------------------------------------

def test_conflict_shaped_canned_reply_is_automated_hold():
    # The real 2026-09-26 mis-bucket: a canned auto-reply quoting an MC/DOT
    # subject held as "conflicting insured names" instead of automated.
    facts = _facts(insured_name="mc1485645 dot3973073",
                   requester_email="certificates+canned.response@"
                                   "streetsmart.insurance")
    res = verify_record(
        _record("Re: Request for Certificate of Insurance for MC1485645 / "
                "DOT3973073", facts),
        index=None, verifier=None)
    assert res.requested_action == ACTION_AUTOREPLY
    assert res.status == HOLD
    assert "automated" in (res.hold_reasons[0] if res.hold_reasons else "")
