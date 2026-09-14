"""Tests for robie_job_engine.ezlynx_write_verify (triple verification)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["EZLYNX_WRITE_APPLICANT_IDS"] = "220250093, 999000111"

from robie_job_engine.ezlynx_write_verify import (  # noqa: E402
    EzlynxWriteVerifyError,
    names_match,
    normalize_name,
    verify_write_target,
)

APPLICANT = "999000111"
POLICY = "PAC00001215485"
NAME = "Green Lion Lawn Care LLC DBA Lawn Buddies"


def _search_ok(policy_number):
    assert policy_number == POLICY
    return {"policy_number": POLICY, "applicant_id": APPLICANT}


def _fetch_ok(applicant_id):
    assert applicant_id == APPLICANT
    return {"applicant_id": APPLICANT, "name": "Green Lion Lawn Care LLC"}


def test_all_three_checks_pass_with_dba_suffix():
    evidence = verify_write_target(
        APPLICANT,
        expected_policy_number=POLICY,
        expected_name=NAME,
        policy_search_fn=_search_ok,
        applicant_fetch_fn=_fetch_ok,
    )
    assert evidence["verified"] is True
    assert all(c["passed"] for c in evidence["checks"].values())


def test_exact_name_match_passes():
    evidence = verify_write_target(
        APPLICANT,
        expected_policy_number=POLICY,
        expected_name="Green Lion Lawn Care LLC",
        policy_search_fn=_search_ok,
        applicant_fetch_fn=_fetch_ok,
    )
    assert evidence["verified"] is True


def test_allowlist_failure_refuses_first():
    with pytest.raises(EzlynxWriteVerifyError, match="check 1"):
        verify_write_target(
            "000000000",
            expected_policy_number=POLICY,
            expected_name=NAME,
            policy_search_fn=_search_ok,
            applicant_fetch_fn=_fetch_ok,
        )


def test_policy_owned_by_other_applicant_refuses():
    def search_other(policy_number):
        return {"policy_number": policy_number, "applicant_id": "220250093"}

    with pytest.raises(EzlynxWriteVerifyError, match="check 2"):
        verify_write_target(
            APPLICANT,
            expected_policy_number=POLICY,
            expected_name=NAME,
            policy_search_fn=search_other,
            applicant_fetch_fn=_fetch_ok,
        )


def test_unknown_policy_number_refuses():
    with pytest.raises(EzlynxWriteVerifyError, match="check 2"):
        verify_write_target(
            APPLICANT,
            expected_policy_number=POLICY,
            expected_name=NAME,
            policy_search_fn=lambda pn: None,
            applicant_fetch_fn=_fetch_ok,
        )


def test_name_mismatch_refuses():
    def fetch_wrong(applicant_id):
        return {"applicant_id": applicant_id, "name": "Some Other Company LLC"}

    with pytest.raises(EzlynxWriteVerifyError, match="check 3"):
        verify_write_target(
            APPLICANT,
            expected_policy_number=POLICY,
            expected_name=NAME,
            policy_search_fn=_search_ok,
            applicant_fetch_fn=fetch_wrong,
        )


def test_missing_policy_number_skips_check_2():
    evidence = verify_write_target(
        APPLICANT,
        expected_name="Green Lion Lawn Care LLC",
        policy_search_fn=None,
        applicant_fetch_fn=_fetch_ok,
    )
    assert evidence["verified"] is True
    check2 = evidence["checks"]["policy_cross_reference"]
    assert check2["passed"] is True and check2["skipped"] is True


def test_missing_fetch_fn_refuses_loudly():
    with pytest.raises(EzlynxWriteVerifyError, match="no applicant_fetch_fn"):
        verify_write_target(
            APPLICANT,
            expected_policy_number=POLICY,
            expected_name=NAME,
            policy_search_fn=_search_ok,
            applicant_fetch_fn=None,
        )


@pytest.mark.parametrize(
    "expected,actual,want",
    [
        ("Green Lion Lawn Care LLC", "green lion lawn care llc", True),
        ("Green Lion, Lawn Care LLC!", "Green Lion Lawn Care LLC", True),
        (NAME, "Green Lion Lawn Care LLC", True),  # trailing DBA stripped
        ("Green Lion Lawn Care LLC", "Green Lion Lawn Care LLC DBA Lawn Buddies", False),
        ("Acme Inc", "Acme Inc 2", False),
        ("", "Green Lion Lawn Care LLC", False),
        (None, "Green Lion Lawn Care LLC", False),
    ],
)
def test_names_match_rule(expected, actual, want):
    assert names_match(expected, actual) is want


def test_normalize_name():
    assert normalize_name("  Green\tLion, LLC! ") == "green lion llc"


# Check 4 -- document corroboration (COI / hello-inbox path: no policy number).


def _docs_fetch_match(applicant_id):
    return [
        {"title": "Certificate of Insurance", "insured_name": "Green Lion Lawn Care LLC"},
        {"title": "Dec page", "policy_number": "OTHER123"},
    ]


def _docs_fetch_disagree(applicant_id):
    return [{"title": "Certificate of Insurance", "insured_name": "Some Other Company LLC"}]


def test_coi_no_policy_docs_corroborate_pass():
    evidence = verify_write_target(
        APPLICANT,
        expected_name="Green Lion Lawn Care LLC",
        policy_search_fn=None,
        applicant_fetch_fn=_fetch_ok,
        documents_fetch_fn=_docs_fetch_match,
    )
    assert evidence["verified"] is True
    check4 = evidence["checks"]["document_corroboration"]
    assert check4["passed"] is True
    assert check4["documents_reviewed"] == 2
    assert check4["corroborating_document_found"] is True


def test_coi_no_policy_docs_disagree_refuse():
    with pytest.raises(EzlynxWriteVerifyError, match="check 4"):
        verify_write_target(
            APPLICANT,
            expected_name="Green Lion Lawn Care LLC",
            policy_search_fn=None,
            applicant_fetch_fn=_fetch_ok,
            documents_fetch_fn=_docs_fetch_disagree,
        )


def test_coi_no_policy_no_docs_pass_with_note():
    evidence = verify_write_target(
        APPLICANT,
        expected_name="Green Lion Lawn Care LLC",
        policy_search_fn=None,
        applicant_fetch_fn=_fetch_ok,
        documents_fetch_fn=lambda aid: [],
    )
    assert evidence["verified"] is True
    check4 = evidence["checks"]["document_corroboration"]
    assert check4["passed"] is True and check4["skipped"] is True


def test_policy_known_docs_disagree_is_evidence_only():
    evidence = verify_write_target(
        APPLICANT,
        expected_policy_number=POLICY,
        expected_name="Green Lion Lawn Care LLC",
        policy_search_fn=_search_ok,
        applicant_fetch_fn=_fetch_ok,
        documents_fetch_fn=_docs_fetch_disagree,
    )
    assert evidence["verified"] is True
    check4 = evidence["checks"]["document_corroboration"]
    assert check4["passed"] is True and check4["evidence_only"] is True


def test_check4_skipped_without_fn():
    evidence = verify_write_target(
        APPLICANT,
        expected_policy_number=POLICY,
        expected_name="Green Lion Lawn Care LLC",
        policy_search_fn=_search_ok,
        applicant_fetch_fn=_fetch_ok,
    )
    check4 = evidence["checks"]["document_corroboration"]
    assert check4["passed"] is True and check4["skipped"] is True


def test_doc_policy_number_match_corroborates():
    evidence = verify_write_target(
        APPLICANT,
        expected_policy_number=POLICY,
        expected_name="Green Lion Lawn Care LLC",
        policy_search_fn=_search_ok,
        applicant_fetch_fn=_fetch_ok,
        documents_fetch_fn=lambda aid: [{"file_name": "cert.pdf", "policy_number": POLICY}],
    )
    assert evidence["verified"] is True
    assert evidence["checks"]["document_corroboration"]["corroborating_document_found"] is True
