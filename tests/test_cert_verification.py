"""Tests for cert_verification (chunk 2). All offline with fakes."""

import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_verification import (  # noqa: E402
    ACTION_ACK,
    ACTION_NEW_REQUEST,
    ACTION_UNKNOWN,
    HOLD,
    VERIFIED,
    EzlynxReadClient,
    FilingTargetMismatch,
    VerificationResult,
    classify_requested_action,
    detect_insured_conflict,
    extract_subject_insured,
    normalize_insured_name,
    verify_filing_target,
    verify_record,
)


def make_match(status, applicant_id=None, candidates=None):
    return SimpleNamespace(status=status, applicant_id=applicant_id,
                           candidates=candidates or [])


def make_record(subject="Certificate request", insured=None,
                policy_numbers=None, match=None, requester_name=None,
                requester_email=None, pdf_texts=None, pdf_unreadable=False):
    facts = SimpleNamespace(
        insured_name=insured,
        policy_numbers=list(policy_numbers or []),
        requester_name=requester_name,
        requester_email=requester_email,
        holder_names=[],
        pdf_texts=list(pdf_texts or []),
        pdf_unreadable=pdf_unreadable,
        requester_is_third_party=False,
    )
    return SimpleNamespace(subject=subject, facts=facts,
                           match=match or make_match("NO_MATCH"),
                           attachments=[])


class FakeVerifier:
    """Stands in for EzlynxReadClient."""

    def __init__(self, policies=None, discussions=None):
        # policies: policy_number -> list of row dicts
        self.policies = policies or {}
        self.discussions = discussions or {}

    def search_policies(self, policy_number):
        return self.policies.get(policy_number, [])

    def get_discussions(self, applicant_id):
        return self.discussions.get(applicant_id, [])


# --- subject extraction ----------------------------------------------------

def test_subject_insured_patterns():
    assert extract_subject_insured(
        "Certificate of Insurance for Homegrown Moving Company"
    ) == "Homegrown Moving Company"
    assert extract_subject_insured(
        "Re: Certificate of Insurance LA Burger LLC to Anderson Marketing"
    ) == "LA Burger LLC"
    assert extract_subject_insured(
        "Renewal Certificate Request- Abg Transportation MC112184"
    ) == "Abg Transportation"
    assert extract_subject_insured(
        "Fwd: Haris Uddin 008265/15/00") == "Haris Uddin"
    assert extract_subject_insured(
        "Request for COI for Ameritesting LLC Covering SilverLining"
    ) == "Ameritesting LLC"


def test_subject_insured_none_when_absent():
    assert extract_subject_insured(
        "Action Required: Daily Summary of Open & Renewal Insurance") is None
    assert extract_subject_insured("") is None


# --- conflict detection ----------------------------------------------------

def test_conflict_when_sources_disagree():
    conflict = detect_insured_conflict({
        "email_body": "Fonseca General Contractor LLC",
        "subject": "Diamond T Express LLC",
        "pdf": None,
    })
    assert conflict is not None
    assert "conflicting insured names" in conflict


def test_no_conflict_when_suffix_differs():
    assert detect_insured_conflict({
        "email_body": "Fonseca General Contractor LLC",
        "subject": "Fonseca General Contractor",
        "pdf": None,
    }) is None


def test_no_conflict_when_only_one_source():
    assert detect_insured_conflict({
        "email_body": None, "subject": "Abg Transportation", "pdf": None,
    }) is None


# --- action classification -------------------------------------------------

def test_action_classification():
    assert classify_requested_action(
        "Re: certificate", "I received the certificate, thank you!") == ACTION_ACK
    assert classify_requested_action(
        "Certificate request", "Please issue a COI for the holder") == ACTION_NEW_REQUEST
    assert classify_requested_action(
        "Daily summary", "Here is your daily summary") == ACTION_UNKNOWN


# --- verify_record ----------------------------------------------------------

def test_verified_report_match_without_policy_numbers():
    rec = make_record(insured="Fonseca General Contractor LLC",
                      match=make_match("MATCHED", 116349171))
    res = verify_record(rec, index=None, verifier=FakeVerifier())
    assert res.status == VERIFIED
    assert res.applicant_id == 116349171
    assert res.source == "report"


def test_matched_but_policy_anchors_elsewhere_holds():
    rec = make_record(insured="Fonseca General Contractor LLC",
                      policy_numbers=["47VBR02289201"],
                      match=make_match("MATCHED", 116349171))
    verifier = FakeVerifier(policies={
        "47VBR02289201": [{"policyNumber": "47VBR02289201",
                           "applicantId": 999999999}]})
    res = verify_record(rec, index=None, verifier=verifier)
    assert res.status == HOLD
    assert any("wrong-applicant" in r for r in res.hold_reasons)


def test_no_match_ezlynx_fallback_verifies():
    rec = make_record(subject="Certificate of Insurance for Homegrown Moving",
                      policy_numbers=["WC123456"],
                      match=make_match("NO_MATCH"))
    verifier = FakeVerifier(policies={
        "WC123456": [{"policyNumber": "WC123456", "applicantId": 555}]})
    res = verify_record(rec, index=None, verifier=verifier)
    assert res.status == VERIFIED
    assert res.applicant_id == 555
    assert res.source == "ezlynx"


def test_no_match_multiple_applicants_holds():
    rec = make_record(policy_numbers=["P1", "P2"],
                      match=make_match("NO_MATCH"))
    verifier = FakeVerifier(policies={
        "P1": [{"applicantId": 1}], "P2": [{"applicantId": 2}]})
    res = verify_record(rec, index=None, verifier=verifier)
    assert res.status == HOLD
    assert any("multiple applicants" in r for r in res.hold_reasons)


def test_ambiguous_holds():
    rec = make_record(insured="Smith LLC",
                      match=make_match("AMBIGUOUS", candidates=[1, 2]))
    res = verify_record(rec, index=None, verifier=FakeVerifier())
    assert res.status == HOLD


def test_unreadable_pdf_holds_without_ocr(monkeypatch):
    import robie_job_engine.cert_verification as cv
    monkeypatch.setattr(cv.shutil, "which", lambda *_: None)
    rec = make_record(pdf_unreadable=True,
                      match=make_match("MATCHED", 116349171))
    res = verify_record(rec, index=None, verifier=FakeVerifier())
    assert res.status == HOLD
    assert any("OCR" in r for r in res.hold_reasons)


def test_acknowledgement_flagged_not_new_request():
    rec = make_record(subject="Re: certificate",
                      insured="Fonseca General Contractor LLC",
                      match=make_match("MATCHED", 116349171))
    rec.facts.pdf_texts = ["Thanks, I received the certificate."]
    res = verify_record(rec, index=None, verifier=FakeVerifier())
    assert res.requested_action == ACTION_ACK


def test_no_applicant_creation_path():
    import robie_job_engine.cert_verification as cv
    names = [n for n in dir(cv) if "creat" in n.lower() or "merge" in n.lower()]
    assert names == []


# --- triple filing guard -----------------------------------------------------

def test_triple_guard_passes():
    verified = VerificationResult(
        status=VERIFIED, applicant_id=116349171,
        insured_name="Fonseca General Contractor LLC",
        policy_numbers=["47VBR02289201"],
        evidence=["report match"])
    verifier = FakeVerifier(
        policies={"47VBR02289201": [{"policyNumber": "47VBR02289201",
                                     "applicantId": 116349171}]},
        discussions={116349171: [{"id": 777, "title": "COI"}]})
    assert verify_filing_target(verified, 777, verifier) is True


def test_triple_guard_rejects_wrong_discussion():
    verified = VerificationResult(
        status=VERIFIED, applicant_id=116349171,
        insured_name="Fonseca General Contractor LLC",
        policy_numbers=["47VBR02289201"],
        evidence=["report match"])
    verifier = FakeVerifier(
        policies={"47VBR02289201": [{"policyNumber": "47VBR02289201",
                                     "applicantId": 116349171}]},
        discussions={116349171: [{"id": 777}]})
    try:
        verify_filing_target(verified, 999, verifier)
    except FilingTargetMismatch as e:
        assert "does not belong to applicant" in str(e)
    else:
        raise AssertionError("expected FilingTargetMismatch")


def test_triple_guard_rejects_unverified():
    verified = VerificationResult(status=HOLD, applicant_id=None)
    try:
        verify_filing_target(verified, 777, FakeVerifier())
    except FilingTargetMismatch:
        pass
    else:
        raise AssertionError("expected FilingTargetMismatch")
