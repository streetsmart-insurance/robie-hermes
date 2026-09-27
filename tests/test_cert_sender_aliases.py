"""Tests for the human-verified sender-alias table (2026-09-26/27 research).

Senders the report's email column doesn't know but a person resolved to a
client (e.g. mela@seciinc.com -> Seci Construction Inc). Strong aliases
resolve directly; medium aliases resolve but flag alias_confidence so
review tooling can surface them. Vendor/internal senders are never
aliased, even if the data file grew such an entry.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine import cert_applicant_index as cai  # noqa: E402
from robie_job_engine.cert_applicant_index import (  # noqa: E402
    AMBIGUOUS,
    MATCHED,
    NO_MATCH,
    ApplicantIndex,
    build_index,
    clear_sender_alias_cache,
    load_sender_aliases,
    match_applicant,
)
from robie_job_engine.cert_intake import (  # noqa: E402
    RequestFacts,
    extract_subject_insured,
)
from robie_job_engine.cert_verification import (  # noqa: E402
    VERIFIED,
    verify_record,
)


def make_index():
    rows = [
        {"account_name": "Seci Construction Inc", "applicant_id": 78540038,
         "email_primary": "sami@seciinc.com", "phones": []},
        {"account_name": "Fonseca General Contractor LLC",
         "applicant_id": 116349171, "email_primary": "office@fonsecagc.com",
         "phones": []},
        {"account_name": "Amour Business Group LLC DBA Abg Transportation",
         "applicant_id": 147197937, "email_primary": "",
         "phones": []},
        # Vendor-system sender filed on a record — must never be a match key.
        {"account_name": "Some Client LLC", "applicant_id": 999,
         "email_primary": "rmis@registrymonitoring.com", "phones": []},
    ]
    return build_index(rows, source_path="/tmp/fake.xlsx")


@pytest.fixture()
def alias_file(tmp_path, monkeypatch):
    """Point the loader at a synthetic alias file for one test."""
    def _write(entries):
        path = tmp_path / "aliases.json"
        path.write_text(json.dumps({"aliases": entries}))
        monkeypatch.setenv("CERT_SENDER_ALIASES_PATH", str(path))
        clear_sender_alias_cache()
        return str(path)
    yield _write
    clear_sender_alias_cache()
    monkeypatch.delenv("CERT_SENDER_ALIASES_PATH", raising=False)


def entry(sender, applicant_id, confidence, account_name="Test Client LLC"):
    return {"sender": sender, "applicant_id": applicant_id,
            "account_name": account_name, "confidence": confidence,
            "evidence": "test"}


def test_strong_alias_resolves_directly(alias_file):
    alias_file([entry("mela@seciinc.com", 78540038, "strong",
                      "Seci Construction Inc")])
    index = make_index()
    res = match_applicant(RequestFacts(requester_email="mela@seciinc.com"),
                          index)
    assert res.status == MATCHED
    assert res.applicant_id == 78540038
    assert res.alias_confidence is None
    assert "sender alias" in res.evidence


def test_medium_alias_resolves_with_flag(alias_file):
    alias_file([entry("botirconstruction@gmail.com", 114073986, "medium",
                      "Botir Construction LLC")])
    index = make_index()
    res = match_applicant(
        RequestFacts(requester_email="botirconstruction@gmail.com"), index)
    assert res.status == MATCHED
    assert res.applicant_id == 114073986
    assert res.alias_confidence == "medium"


def test_unknown_sender_still_no_match(alias_file):
    alias_file([entry("mela@seciinc.com", 78540038, "strong")])
    index = make_index()
    res = match_applicant(RequestFacts(requester_email="nobody@nowhere.com"),
                          index)
    assert res.status == NO_MATCH


def test_vendor_sender_never_aliased_even_if_listed(alias_file):
    # Defense in depth: a vendor-system address in the data file is dropped
    # at load, never a match key.
    alias_file([entry("rmis@registrymonitoring.com", 147197937, "strong")])
    assert load_sender_aliases() == {}
    index = make_index()
    res = match_applicant(
        RequestFacts(requester_email="rmis@registrymonitoring.com"), index)
    assert res.status == NO_MATCH


def test_internal_sender_never_aliased_even_if_listed(alias_file):
    alias_file([entry("sandy@streetsmart.insurance", 78540038, "strong")])
    assert load_sender_aliases() == {}
    index = make_index()
    res = match_applicant(
        RequestFacts(requester_email="sandy@streetsmart.insurance"), index)
    assert res.status == NO_MATCH


def test_insured_name_beats_alias(alias_file):
    # The certificate is FOR the insured: an exact insured-name match to a
    # different applicant wins over the sender alias.
    alias_file([entry("mela@seciinc.com", 78540038, "strong",
                      "Seci Construction Inc")])
    index = make_index()
    res = match_applicant(
        RequestFacts(insured_name="Fonseca General Contractor LLC",
                     requester_email="mela@seciinc.com"), index)
    assert res.status == MATCHED
    assert res.applicant_id == 116349171


def test_alias_conflicting_with_dba_fragment_holds(alias_file):
    # DBA-fragment "Abg Transportation" -> 147197937, but the aliased sender
    # points at 78540038: conflicting signals -> hold, never guess.
    alias_file([entry("mela@seciinc.com", 78540038, "strong",
                      "Seci Construction Inc")])
    index = make_index()
    res = match_applicant(
        RequestFacts(insured_name="Abg Transportation",
                     requester_email="mela@seciinc.com"), index)
    assert res.status == AMBIGUOUS


def test_trustlayer_sender_never_matches_email():
    index = make_index()
    res = match_applicant(
        RequestFacts(requester_email="requests@trustlayer.io"), index)
    assert res.status == NO_MATCH


def test_operfi_sender_never_matches_email():
    index = make_index()
    res = match_applicant(
        RequestFacts(requester_email="insurance@operfi.com"), index)
    assert res.status == NO_MATCH


def test_alias_email_match_case_insensitive(alias_file):
    alias_file([entry("mela@seciinc.com", 78540038, "strong",
                      "Seci Construction Inc")])
    index = make_index()
    res = match_applicant(RequestFacts(requester_email="Mela@SeciInc.com"),
                          index)
    assert res.status == MATCHED
    assert res.applicant_id == 78540038


# --- subject insured extraction: TrustLayer / Certificial shapes ---

def test_trustlayer_document_request_for_from():
    assert extract_subject_insured(
        "Document request for All Force Construction from The Fania "
        "Company, Inc.") == "All Force Construction"


def test_trustlayer_trailing_for():
    assert extract_subject_insured(
        "Compliance request from 2 Grand Central Tower - Sovereign "
        "Partners for MALAS BROTHERS PAINTING") == "MALAS BROTHERS PAINTING"


def test_certificial_policy_expired_possessive():
    assert extract_subject_insured(
        "Ekmg Logistics LLC's Policy has Expired") == "Ekmg Logistics LLC"


def test_request_for_coi_does_not_make_junk_insured():
    # The generic "for" tail must not turn bare request-words into an
    # insured ("COI" was a v4 false-verification insured).
    assert extract_subject_insured("Request for COI") is None


def test_existing_patterns_still_win():
    # The generic "for" tail is last: specific patterns take precedence.
    assert extract_subject_insured(
        "Certificate of Insurance LA Burger LLC to Anderson Market") == \
        "LA Burger LLC"


# --- verify_record propagation ---

def _match(status, applicant_id=None, alias_confidence=None):
    return SimpleNamespace(status=status, applicant_id=applicant_id,
                           candidates=[], alias_confidence=alias_confidence)


def _record(**kw):
    facts = SimpleNamespace(
        insured_name=kw.get("insured"), policy_numbers=[],
        requester_name=None, requester_email=kw.get("requester_email"),
        holder_names=[], pdf_texts=[], pdf_unreadable=False,
        requester_is_third_party=False)
    return SimpleNamespace(
        subject="Please issue a certificate of insurance", facts=facts,
        match=kw.get("match", _match("NO_MATCH")), attachments=[])


def test_verify_record_carries_medium_flag():
    rec = _record(match=_match("MATCHED", 78540038,
                               alias_confidence="medium"))
    res = verify_record(rec, index=None, verifier=None)
    assert res.status == VERIFIED
    assert res.applicant_id == 78540038
    assert res.alias_confidence == "medium"
    assert any("human review" in e for e in res.evidence)


def test_verify_record_no_flag_for_strong_alias():
    rec = _record(match=_match("MATCHED", 78540038))
    res = verify_record(rec, index=None, verifier=None)
    assert res.status == VERIFIED
    assert res.alias_confidence is None


# --- the shipped data file ---

def test_shipped_alias_file_loads():
    data_path = os.path.join(os.path.dirname(cai.__file__), "data",
                             "cert_sender_aliases.json")
    aliases = load_sender_aliases(data_path)
    assert len(aliases) == 42
    assert aliases["mela@seciinc.com"]["applicant_id"] == 78540038
    assert aliases["mela@seciinc.com"]["confidence"] == "medium"
    strong = [a for a in aliases.values() if a["confidence"] == "strong"]
    medium = [a for a in aliases.values() if a["confidence"] == "medium"]
    assert len(strong) == 19 and len(medium) == 23
    # No vendor/internal domain may be aliased, ever.
    for sender in aliases:
        domain = sender.split("@", 1)[1]
        assert not cai._sender_is_vendor_system(sender), sender
        assert not cai._sender_is_internal(sender), sender
