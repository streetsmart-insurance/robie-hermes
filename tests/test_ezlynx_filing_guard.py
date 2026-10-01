"""Tests for the EZLynx filing triple-verification gate.

Replays the 2026-09-25 Rivera misfire: Rivera's amended flood dec
(policy 29 1152014106 05) was filed to Laura Oloughlin's applicant
(83644603) because the worker matched Laura's queue row and never
compared the email's identity with the matched record's identity.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from robie_job_engine.ezlynx_filing_guard import (
    FilingIdentity,
    FilingTargetMismatch,
    verify_filing_target,
)


class FakeDiscussionClient:
    def __init__(self, by_applicant):
        self.by_applicant = by_applicant

    def get_discussions(self, applicant_id):
        return self.by_applicant.get(str(applicant_id), [])


class FakePolicyClient:
    def __init__(self, rows):
        self.rows = rows

    def search_policy_by_number(self, policy_number):
        return {"status": "success", "data": {"results": self.rows, "totalSize": len(self.rows)}}


def discussions(*items):
    return [
        {"discussionId": did, "title": title, "noteCount": n}
        for did, title, n in items
    ]


RIVERA_IDENTITY = {
    "insured_name": "RIVERA, ANTONIO",
    "policy_number": "29 1152014106 05",
    "source_insured_name": "Antonio Rivera",
    "source_policy_number": "29 1152014106 05",
}

# The wrong match the morning worker actually made.
MISFIRE_IDENTITY = {
    "insured_name": "RIVERA, ANTONIO",
    "policy_number": "29 1152014106 05",
    "source_insured_name": "Laura Oloughlin",
    "source_policy_number": "29 1152005977 05",
}

RIVERA_DISCUSSIONS = {
    "84421772": discussions(
        ("845337115", "Flood | 29 1152014106 05", 1),
    ),
}
OLOUGHLIN_DISCUSSIONS = {
    "83644603": discussions(
        ("323617433", "Laura Oloughlin Flood Policy", 2),
    ),
}


def test_misfire_blocked_at_source_consistency():
    """The exact 2026-09-25 misfire must not verify."""
    client = FakeDiscussionClient(OLOUGHLIN_DISCUSSIONS)
    with pytest.raises(FilingTargetMismatch) as exc:
        verify_filing_target(
            applicant_id="83644603",
            filing_identity=MISFIRE_IDENTITY,
            discussion_id="323617433",
            discussion_client=client,
        )
    assert "does not match" in str(exc.value)


def test_correct_filing_verifies():
    """Rivera's email filed to Rivera's applicant and discussion verifies."""
    client = FakeDiscussionClient(RIVERA_DISCUSSIONS)
    result = verify_filing_target(
        applicant_id="84421772",
        filing_identity=RIVERA_IDENTITY,
        discussion_id="845337115",
        discussion_client=client,
    )
    assert result["applicant_id"] == "84421772"
    assert result["discussion_id"] == "845337115"
    assert result["policy_via"] == "discussion-title"
    assert result["checks"] == [
        "source-consistency",
        "policy-anchored",
        "discussion-on-applicant",
    ]


def test_policy_on_wrong_applicant_is_hard_fail():
    """PolicyApi tying the policy to a different applicant fails loudly,
    even when the source record was consistent."""
    client = FakeDiscussionClient(
        {"999": discussions(("1", "Some other discussion", 1))}
    )
    policy = FakePolicyClient(
        [{"policyNumber": "13SBABN8TSF", "accountId": 60900886}]
    )
    with pytest.raises(FilingTargetMismatch) as exc:
        verify_filing_target(
            applicant_id="999",
            filing_identity={
                "insured_name": "Quality Food Products",
                "policy_number": "13SBABN8TSF",
                "source_insured_name": "Quality Food Products",
                "source_policy_number": "13SBABN8TSF",
            },
            discussion_id="1",
            discussion_client=client,
            policy_client=policy,
        )
    assert "belongs to applicant 60900886, not 999" in str(exc.value)


def test_policy_api_authoritative_pass():
    """A policy only in PolicyApi (no digits in the title) verifies when
    PolicyApi ties it to the applicant."""
    client = FakeDiscussionClient(
        {"60900886": discussions(("7", "General service discussion", 3))}
    )
    policy = FakePolicyClient(
        [{"policyNumber": "13SBABN8TSF", "accountId": 60900886}]
    )
    result = verify_filing_target(
        applicant_id="60900886",
        filing_identity={
            "insured_name": "Quality Food Products",
            "policy_number": "13SBABN8TSF",
            "source_insured_name": "Quality Food Products",
            "source_policy_number": "13SBABN8TSF",
        },
        discussion_id="7",
        discussion_client=client,
        policy_client=policy,
    )
    assert result["policy_via"] == "policy-api"


def test_discussion_not_on_applicant_fails():
    client = FakeDiscussionClient(RIVERA_DISCUSSIONS)
    with pytest.raises(FilingTargetMismatch) as exc:
        verify_filing_target(
            applicant_id="84421772",
            filing_identity=RIVERA_IDENTITY,
            discussion_id="323617433",  # Laura's discussion
            discussion_client=client,
        )
    assert "is not on applicant 84421772" in str(exc.value)


def test_missing_source_identity_fails_closed():
    """A worker that cannot state what it matched must not file."""
    client = FakeDiscussionClient(RIVERA_DISCUSSIONS)
    with pytest.raises(FilingTargetMismatch):
        verify_filing_target(
            applicant_id="84421772",
            filing_identity={
                "insured_name": "RIVERA, ANTONIO",
                "policy_number": "29 1152014106 05",
            },
            discussion_id="845337115",
            discussion_client=client,
        )


def test_document_filing_verifies_without_discussion():
    """Document uploads verify against the applicant's discussion titles."""
    client = FakeDiscussionClient(RIVERA_DISCUSSIONS)
    result = verify_filing_target(
        applicant_id="84421772",
        filing_identity=RIVERA_IDENTITY,
        discussion_client=client,
    )
    assert result["discussion_id"] is None
    assert result["policy_via"] == "discussion-title"
    assert "applicant-live" in result["checks"]


def test_document_filing_to_wrong_applicant_fails():
    client = FakeDiscussionClient(OLOUGHLIN_DISCUSSIONS)
    with pytest.raises(FilingTargetMismatch):
        verify_filing_target(
            applicant_id="83644603",
            filing_identity=RIVERA_IDENTITY,
            discussion_client=client,
        )


def test_compact_policy_digit_format_matches():
    """Titles carrying the compact digit format still anchor the policy."""
    client = FakeDiscussionClient(
        {"84421772": discussions(("845337115", "Flood | 29115201410605", 1))}
    )
    result = verify_filing_target(
        applicant_id="84421772",
        filing_identity=RIVERA_IDENTITY,
        discussion_id="845337115",
        discussion_client=client,
    )
    assert result["policy_via"] == "discussion-title"
