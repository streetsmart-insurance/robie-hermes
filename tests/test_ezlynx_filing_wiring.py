"""Wiring tests: the filing gate runs BEFORE any EZLynx write."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from robie_job_engine.ezlynx_api_only_writes import add_note_to_discussion
from robie_job_engine.ezlynx_filing_guard import FilingTargetMismatch


class FakeDiscussionClient:
    def __init__(self, by_applicant):
        self.by_applicant = by_applicant
        self.append_calls = []

    def get_discussions(self, applicant_id):
        return self.by_applicant.get(str(applicant_id), [])

    def append_note(self, discussion_id, body, note_type="Note"):
        self.append_calls.append((discussion_id, body))
        return {"result": 999}


MISFIRE_IDENTITY = {
    "insured_name": "RIVERA, ANTONIO",
    "policy_number": "29 1152014106 05",
    "source_insured_name": "Laura Oloughlin",
    "source_policy_number": "29 1152005977 05",
}


def test_bad_identity_raises_before_any_write():
    """The gate must fire before the note is appended: no write happens."""
    client = FakeDiscussionClient(
        {
            "83644603": [
                {
                    "discussionId": "323617433",
                    "title": "Laura Oloughlin Flood Policy",
                    "noteCount": 2,
                }
            ]
        }
    )
    with pytest.raises(FilingTargetMismatch):
        add_note_to_discussion(
            "83644603",
            "Amended flood declaration received.",
            title_hint="Laura Oloughlin Flood Policy",
            discussion_client=client,
            filing_identity=MISFIRE_IDENTITY,
        )
    assert client.append_calls == [], "no EZLynx write may happen after a failed check"


def test_no_identity_keeps_legacy_behavior():
    """Callers without email context are unaffected by the gate."""
    client = FakeDiscussionClient(
        {
            "83644603": [
                {
                    "discussionId": "323617433",
                    "title": "Laura Oloughlin Flood Policy",
                    "noteCount": 2,
                }
            ]
        }
    )
    # Without filing_identity the legacy path runs (no gate, no verification
    # key in the result). We only assert it does not raise FilingTargetMismatch.
    try:
        add_note_to_discussion(
            "83644603",
            "System note without email context.",
            title_hint="Laura Oloughlin Flood Policy",
            discussion_client=client,
            dry_run=True,
        )
    except FilingTargetMismatch:
        pytest.fail("gate must not run when filing_identity is absent")
