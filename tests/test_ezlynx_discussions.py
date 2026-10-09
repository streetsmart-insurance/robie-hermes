"""Tests for robie_job_engine.ezlynx_discussions (Discussion API v8).

All HTTP is faked: no test may touch the network or Secret Manager.
"""

import json
import os
from unittest.mock import patch
from urllib import parse

import pytest

from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine.ezlynx_write_scope import EzlynxWriteScopeError

ALLOWED_APPLICANT = "220250093"
TOKEN_URL = "https://identity.example.com/connect/token"
API_BASE = "https://app.uatezlynx.com/DiscussionApi/"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class FakeUrlopen:
    """Route by URL substring; record every call for assertions."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, url, *, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": dict(headers or {})})
        for needle, payload in self.routes:
            if needle in url:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected Discussion API URL: {url}")

    def posts_to(self, needle):
        return [c for c in self.calls if needle in c["url"] and c["data"]]


def make_client(routes, **overrides):
    config = disc.DiscussionApiConfig(
        discussion_base_url=API_BASE,
        token_endpoint=TOKEN_URL,
        client_id="street_smart_api",
        client_secret="secret",
        username="SSRobie",
        integration_group_id="159",
    )
    return disc.DiscussionApiClient(config, urlopen=FakeUrlopen(routes), **overrides)


def token_routes(extra=()):
    return [("connect/token", {"access_token": "tok123", "expires_in": 3600})] + list(extra)


# ---------------------------------------------------------------------------
# authentication


def test_token_uses_vendor_data_access_grant_and_caches():
    client = make_client(token_routes())
    first = client.get_token()
    second = client.get_token()
    assert first == second == "tok123"
    token_calls = client._urlopen.posts_to("connect/token")
    assert len(token_calls) == 1  # second call served from cache
    form = parse.parse_qs(token_calls[0]["data"].decode("utf-8"))
    assert form["grant_type"] == ["vendor_data_access"]
    assert form["username"] == ["SSRobie"]
    assert "Authorization" not in token_calls[0]["headers"]


def test_token_missing_access_token_raises():
    client = make_client([("connect/token", {"expires_in": 3600})])
    with pytest.raises(disc.DiscussionApiError):
        client.get_token()


def test_api_calls_carry_bearer_token():
    routes = token_routes([("by-applicant", [{"discussionId": "d1", "title": "PCR"}])])
    client = make_client(routes)
    client.get_discussions(ALLOWED_APPLICANT)
    api_calls = [c for c in client._urlopen.calls if "by-applicant" in c["url"]]
    assert api_calls
    assert api_calls[0]["headers"]["Authorization"] == "Bearer tok123"
    assert "applicantId=220250093" in api_calls[0]["url"]


# ---------------------------------------------------------------------------
# lookup


def test_get_discussion_ids():
    routes = token_routes([("ids-by-applicant", ["d1", "d2"])])
    client = make_client(routes)
    assert client.get_discussion_ids(ALLOWED_APPLICANT) == ["d1", "d2"]


def test_get_discussions_parses_list():
    rows = [{"discussionId": "d1", "title": "PCR"}, {"id": "d2", "Title": "COI"}]
    client = make_client(token_routes([("by-applicant", rows)]))
    assert client.get_discussions(ALLOWED_APPLICANT) == rows


def test_get_discussion_by_id():
    client = make_client(token_routes([("v8/discussions/d9", {"discussionId": "d9"})]))
    record = client.get_discussion("d9")
    assert record["discussionId"] == "d9"


def test_get_discussion_with_notes_uses_the_with_notes_path():
    client = make_client(token_routes([("with-notes", {"discussionId": "d9", "notes": []})]))
    record = client.get_discussion_with_notes("d9")
    assert record["discussionId"] == "d9"
    urls = [call["url"] for call in client._urlopen.calls if "with-notes" in call["url"]]
    assert urls and urls[0].endswith("/v8/discussions/d9/with-notes")


def test_with_notes_read_is_complete_only_for_the_whole_list():
    proven = {"note_count": 1, "most_recent_note_id": "n1"}
    whole = {"discussionId": "d1", "notes": [{"noteId": "n1", "body": "hello"}]}
    assert disc.with_notes_read_is_complete(whole, proven)
    assert not disc.with_notes_read_is_complete(whole)
    assert not disc.with_notes_read_is_complete(
        {"discussionId": "d1", "noteCount": 1, "mostRecentNoteId": "n1"}
    )
    assert not disc.with_notes_read_is_complete(
        {"discussionId": "d1", "noteCount": 0, "notes": []},
        {"note_count": 0, "most_recent_note_id": ""},
    )
    assert not disc.with_notes_read_is_complete(
        {"discussionId": "d1", "notes": [{"noteId": "n1"}]},
        proven,
    )
    # A first page, a nested count, totalCount/pageSize, and a bare list
    # are not proof, even when they look locally consistent.
    page = [{"noteId": "n-page", "body": "other"}]
    plain = {"note_count": 4, "most_recent_note_id": "n-latest"}
    assert not disc.with_notes_read_is_complete({"notes": page}, plain)
    assert not disc.with_notes_read_is_complete(
        {"discussion": {"noteCount": 1, "mostRecentNoteId": "n-page"}, "notes": page},
        plain,
    )
    assert not disc.with_notes_read_is_complete(
        {"totalCount": 4, "pageSize": 1, "notes": page},
        plain,
    )
    assert not disc.with_notes_read_is_complete(page, plain)


# ---------------------------------------------------------------------------
# append


def test_append_note_posts_to_notes_endpoint():
    routes = token_routes([("/notes", {"noteId": "n42"})])
    client = make_client(routes)
    created = client.append_note("d1", "Renewal update — (phone on file)")
    assert created == {"noteId": "n42"}
    posts = client._urlopen.posts_to("/v8/discussions/d1/notes")
    assert len(posts) == 1
    body = json.loads(posts[0]["data"].decode("utf-8"))
    assert body == {"type": "Note", "body": "Renewal update — (phone on file)"}


def test_append_note_enforces_write_scope_when_the_applicant_is_named(monkeypatch):
    routes = token_routes([("/notes", {"noteId": "n42"})])
    client = make_client(routes)
    monkeypatch.delenv("ROBIE_EZLYNX_WRITE_SCOPE", raising=False)
    monkeypatch.delenv("ROBIE_PLAYGROUND", raising=False)
    with pytest.raises(EzlynxWriteScopeError, match="EZLYNX_WRITE_SCOPE_REFUSED"):
        client.append_note("d1", "Held for a person.", applicant_id="80026158")
    assert client._urlopen.posts_to("/notes") == []

    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    monkeypatch.setenv("ROBIE_PLAYGROUND", "1")
    created = client.append_note("d1", "Held for a person.", applicant_id="80026158")
    assert created == {"noteId": "n42"}
    assert len(client._urlopen.posts_to("/v8/discussions/d1/notes")) == 1


def test_append_note_refuses_phone_numbers():
    client = make_client(token_routes())
    with pytest.raises(disc.DiscussionApiError, match="phone-number-like"):
        client.append_note("d1", "Call Tracy Paquette at 603.769.3995")
    assert client._urlopen.posts_to("/notes") == []


@pytest.mark.parametrize("number", ["603-769-3995", "(603) 769-3995", "+1 603 769 3995"])
def test_reject_phone_numbers_variants(number):
    with pytest.raises(disc.DiscussionApiError):
        disc.reject_phone_numbers(f"reach them at {number} tomorrow")


@pytest.mark.parametrize("number", ["732-995-3409", "(732) 995-3409", "+1 732 995 3409"])
def test_reject_phone_numbers_still_refuses_real_numbers_after_policy_fix(number):
    for text in (number, f"Call {number}.", f"reach them at {number} tomorrow"):
        with pytest.raises(disc.DiscussionApiError, match="phone-number-like"):
            disc.reject_phone_numbers(text)


@pytest.mark.parametrize(
    "text",
    [
        "CT1278263263-2",
        "Late-payment notice for policy CT1278263263-2 from Ascend.",
        "policy AB12345678901 is past due",
    ],
)
def test_reject_phone_numbers_allows_policy_numbers_glued_to_letters(text):
    assert disc.reject_phone_numbers(text) == text


def test_phone_like_does_not_match_inside_a_longer_digit_run():
    assert disc._PHONE_LIKE.search("12345678901234") is None
    assert disc._PHONE_LIKE.search("00 7329953409") is not None


def test_reject_phone_numbers_allows_plain_text():
    assert disc.reject_phone_numbers("No dialable digits here.") == "No dialable digits here."


# ---------------------------------------------------------------------------
# fail-closed selection


def test_select_single_discussion_wins():
    row = {"discussionId": "d1", "title": "PCR"}
    assert disc.select_discussion_for_note([row]) == row


def test_select_no_discussions_raises():
    with pytest.raises(disc.DiscussionSelectionError) as excinfo:
        disc.select_discussion_for_note([])
    assert excinfo.value.code == disc.NO_DISCUSSIONS


def test_select_ambiguous_without_hint_raises():
    rows = [{"id": "d1", "title": "PCR"}, {"id": "d2", "title": "COI"}]
    with pytest.raises(disc.DiscussionSelectionError) as excinfo:
        disc.select_discussion_for_note(rows)
    assert excinfo.value.code == disc.AMBIGUOUS_DISCUSSIONS


def test_select_hint_matching_one_wins():
    rows = [{"id": "d1", "title": "PCR"}, {"id": "d2", "title": "COI Request"}]
    assert disc.select_discussion_for_note(rows, title_hint="coi")["id"] == "d2"


def test_select_hint_matching_two_raises():
    rows = [{"id": "d1", "title": "COI Request"}, {"id": "d2", "title": "COI Renewal"}]
    with pytest.raises(disc.DiscussionSelectionError) as excinfo:
        disc.select_discussion_for_note(rows, title_hint="coi")
    assert excinfo.value.code == disc.AMBIGUOUS_DISCUSSIONS


def test_select_hint_matching_none_raises():
    rows = [{"id": "d1", "title": "PCR"}, {"id": "d2", "title": "COI"}]
    with pytest.raises(disc.DiscussionSelectionError) as excinfo:
        disc.select_discussion_for_note(rows, title_hint="billing")
    assert excinfo.value.code == disc.AMBIGUOUS_DISCUSSIONS


def test_select_untitled_only_is_refused():
    rows = [{"id": "d1", "title": "Untitled"}, {"id": "d2", "Title": ""}]
    with pytest.raises(disc.DiscussionSelectionError) as excinfo:
        disc.select_discussion_for_note(rows)
    assert excinfo.value.code == disc.UNTITLED_FORBIDDEN


def test_select_filters_untitled_and_keeps_titled():
    rows = [
        {"id": "d0", "title": "Untitled"},
        {"id": "d1", "title": "Cancellation"},
    ]
    assert disc.select_discussion_for_note(rows)["id"] == "d1"


def test_is_untitled_discussion():
    assert disc.is_untitled_discussion({"title": "Untitled"}) is True
    assert disc.is_untitled_discussion({"title": "untitled"}) is True
    assert disc.is_untitled_discussion({"title": ""}) is True
    assert disc.is_untitled_discussion({"title": "Cancellation"}) is False


# ---------------------------------------------------------------------------
# orchestrator: file_note_to_existing_discussion


def _file_routes(discussions):
    return token_routes(
        [
            ("by-applicant", discussions),
            ("/notes", {"noteId": "n7"}),
            (
                "v8/discussions/",
                {"discussionId": "d1", "notes": [{"noteId": "n7", "body": "seed"}]},
            ),
        ]
    )


def test_file_note_happy_path():
    client = make_client(_file_routes([{"discussionId": "d1", "title": "PCR"}]))
    result = disc.file_note_to_existing_discussion(client, ALLOWED_APPLICANT, "Filed note")
    assert result["status"] == "filed"
    assert result["discussion_id"] == "d1"
    assert result["note_id"] == "n7"
    assert result["read_back"] is True
    assert len(client._urlopen.posts_to("/notes")) == 1


def test_file_note_rejects_non_allowlisted_applicant_before_any_http():
    client = make_client(_file_routes([{"discussionId": "d1"}]))
    with patch(
        "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({ALLOWED_APPLICANT}),
    ):
        with pytest.raises(EzlynxWriteScopeError):
            disc.file_note_to_existing_discussion(client, "999999999", "Filed note")
    assert client._urlopen.calls == []


def test_file_note_untitled_only_is_pending_and_writes_nothing():
    client = make_client(_file_routes([{"discussionId": "d1", "title": "Untitled"}]))
    result = disc.file_note_to_existing_discussion(client, ALLOWED_APPLICANT, "Filed note")
    assert result["status"] == "pending"
    assert result["reason_code"] == disc.UNTITLED_FORBIDDEN
    assert result["discussion_id"] is None
    assert client._urlopen.posts_to("/notes") == []


def test_file_note_no_discussions_is_pending_and_writes_nothing():
    client = make_client(_file_routes([]))
    result = disc.file_note_to_existing_discussion(client, ALLOWED_APPLICANT, "Filed note")
    assert result["status"] == "pending"
    assert result["reason_code"] == disc.NO_DISCUSSIONS
    assert result["discussion_id"] is None
    assert client._urlopen.posts_to("/notes") == []


def test_file_note_ambiguous_is_pending_and_writes_nothing():
    rows = [{"id": "d1", "title": "PCR"}, {"id": "d2", "title": "COI"}]
    client = make_client(_file_routes(rows))
    result = disc.file_note_to_existing_discussion(client, ALLOWED_APPLICANT, "Filed note")
    assert result["status"] == "pending"
    assert result["reason_code"] == disc.AMBIGUOUS_DISCUSSIONS
    assert client._urlopen.posts_to("/notes") == []


def test_file_note_dry_run_writes_nothing():
    client = make_client(_file_routes([{"discussionId": "d1", "title": "PCR"}]))
    result = disc.file_note_to_existing_discussion(
        client, ALLOWED_APPLICANT, "Filed note", dry_run=True
    )
    assert result["status"] == "dry_run"
    assert result["discussion_id"] == "d1"
    assert client._urlopen.posts_to("/notes") == []


def test_file_note_phone_number_refused():
    client = make_client(_file_routes([{"discussionId": "d1"}]))
    with pytest.raises(disc.DiscussionApiError, match="phone-number-like"):
        disc.file_note_to_existing_discussion(client, ALLOWED_APPLICANT, "Call 603-769-3995")
    assert client._urlopen.posts_to("/notes") == []


# ---------------------------------------------------------------------------
# the delete endpoints must not exist here


def test_no_delete_endpoints_implemented():
    source = open(os.path.join(os.path.dirname(disc.__file__), "ezlynx_discussions.py")).read()
    # No HTTP DELETE is ever issued (the docstring mentions the documented
    # delete endpoints only to state they are NOT implemented).
    assert '"DELETE"' not in source and "'DELETE'" not in source
    assert "method=\"DELETE\"" not in source and 'method="DELETE"' not in source
    for name in dir(disc):
        assert "delete" not in name.lower(), f"unexpected delete API: {name}"
    for name in dir(disc.DiscussionApiClient):
        assert "delete" not in name.lower(), f"unexpected delete method: {name}"
