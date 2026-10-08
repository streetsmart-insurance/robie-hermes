"""Tests for v8 discussion parsing + the 10 wired deterministic scripts."""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock

from ezlynx_client import EZLynxClient
from prompts import load_scripts, deterministic_campaigns, build_deterministic_task

# Exact 13-field v8 shape from the 2026-10-03 live probe.
V8_DISCUSSIONS = [
    {
        "applicantId": "25486692",
        "created": "2026-10-02T20:48:00",
        "createdById": "u1",
        "deleted": False,
        "discussionId": "disc-1",
        "lastModified": "2026-10-02T21:06:00",
        "lastModifiedById": "u1",
        "mostRecentNoteId": "n9",
        "noteCount": 3,
        "opportunityId": "o1",
        "organizationId": "114423",
        "title": "Call about renewal",
        "watcherUserIds": ["u1"],
    },
    {
        "applicantId": "25486692",
        "created": "2026-10-01T10:00:00",
        "createdById": "u1",
        "deleted": False,
        "discussionId": "disc-2",
        "lastModified": "2026-10-01T10:00:00",
        "lastModifiedById": "u1",
        "mostRecentNoteId": "n3",
        "noteCount": 1,
        "opportunityId": None,
        "organizationId": "114423",
        "title": "Untitled",
        "watcherUserIds": [],
    },
]


def _client_with_discussions(payload):
    """EZLynxClient with OAuth + GET mocked to return `payload`.

    Returns (client, patcher); caller must patcher.stop() when done.
    """
    client = EZLynxClient()
    client._oauth_token = "tok"
    client._oauth_expires_at = 9999999999
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = payload
    patcher = patch("ezlynx_client.requests.get", return_value=fake_resp)
    patcher.start()
    return client, patcher


def _stop(patcher):
    patcher.stop()


def test_v8_list_shape_parsed():
    client, p = _client_with_discussions(V8_DISCUSSIONS)
    try:
        discs = client.get_discussions("25486692")
        assert len(discs) == 2
        assert discs[0]["discussionId"] == "disc-1"
        assert discs[0]["title"] == "Call about renewal"
        # All 13 v8 fields present, zero label fields.
        assert len(discs[0].keys()) == 13
        assert not any("label" in k.lower() for k in discs[0].keys())
    finally:
        _stop(p)


def test_v8_wrapped_shape_parsed():
    client, p = _client_with_discussions({"discussions": V8_DISCUSSIONS})
    try:
        discs = client.get_discussions("25486692")
        assert len(discs) == 2
    finally:
        _stop(p)


def test_v8_no_label_fields_anywhere():
    client, p = _client_with_discussions(V8_DISCUSSIONS)
    try:
        for d in client.get_discussions("25486692"):
            for k in d.keys():
                assert "label" not in k.lower(), f"unexpected label field: {k}"
    finally:
        _stop(p)


def test_get_discussion_by_id():
    client, p = _client_with_discussions(V8_DISCUSSIONS)
    try:
        d = client.get_discussion_by_id("25486692", "disc-2")
        assert d["title"] == "Untitled"
        assert client.get_discussion_by_id("25486692", "missing") is None
        assert client.get_discussion_by_id("25486692", "") is None
    finally:
        _stop(p)


def test_most_recent_discussion_uses_last_modified():
    client, p = _client_with_discussions(V8_DISCUSSIONS)
    try:
        d = client.most_recent_discussion("25486692")
        assert d["discussionId"] == "disc-1"  # later lastModified
    finally:
        _stop(p)


def test_discussion_id_of_normalizes():
    assert EZLynxClient.discussion_id_of({"discussionId": "abc"}) == "abc"
    assert EZLynxClient.discussion_id_of({"id": "xyz"}) == "xyz"
    assert EZLynxClient.discussion_id_of({}) == ""


# ----------------------------------------------------------------------
# The 10 wired deterministic scripts (Carlo approved 2026-10-03)
# ----------------------------------------------------------------------

EXPECTED_CAMPAIGNS = {
    "robie-lead-followup",
    "robie-client-outreach",
    "robie-cancellation",
    "robie-audit",
    "robie-returned-mail",
    "robie-esign",
    "robie-additional-info",
    "robie-recommendations",
    "robie-unresponsive",
    "robie-renewal-reachout",
}


def test_all_ten_scripts_wired():
    scripts = load_scripts()
    assert set(scripts.keys()) == EXPECTED_CAMPAIGNS
    assert set(deterministic_campaigns()) == EXPECTED_CAMPAIGNS


def test_no_pending_scripts_remain():
    scripts = load_scripts()
    for cid, s in scripts.items():
        assert "PENDING" not in s.get("reason_sentence", ""), cid
        assert "PENDING" not in s.get("script", ""), cid
        assert len(s["reason_sentence"]) > 10, cid
        assert len(s["script"]) > 20, cid


def test_each_script_has_jake_identity():
    # The full `script` is Eva's opener and carries the Jake/StreetSmart
    # identity; `reason_sentence` is just the one-sentence reason spoken
    # right after the AI disclosure (built by first_sentence()).
    scripts = load_scripts()
    for cid, s in scripts.items():
        assert "Jake" in s["script"] and "StreetSmart" in s["script"], cid
        assert len(s["reason_sentence"]) > 10, cid


def test_deterministic_task_uses_script():
    scripts = load_scripts()
    task = build_deterministic_task(
        "Jane Doe", "Jane", "robie-renewal-reachout",
        scripts["robie-renewal-reachout"],
    )
    assert "Eva" in task
    assert "renewal" in task.lower()
    assert "AI assistant" in task
