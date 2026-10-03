"""Regressions from the live Test round on head 0f36700.

A Chat model turn must not POST a discussion note before go. The quoted
title has to match the inbound text Google Chat actually delivers, including
a mention, the [[robie-test]] prefix, and curly quotes. An unconfirmed
ledger row is not reported as added. A finished turn must not show
"(no reply)".
"""
from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_write_go import (
    bind_chat_write_go,
    permit_chat_http_write,
)
from robie_job_engine.ezlynx_discussions import select_discussion_for_note
from robie_job_engine.store import JobStore
from robie_job_engine.turn_finalization import (
    begin_model_generation,
    isolated_model_generation_context,
)
from robie_job_engine.write_verification_loop import PLAN_CHECKPOINT
from test_chat_reconciliation import running
from test_discussion_note_readback import LiveShapeClient
from test_round10_reply_lifecycle import SPACE, THREAD, _adapter_module, _chat

NOTE_1 = (
    '[[robie-test]] Add a note to Buster Brown 26356199 on the discussion '
    '"Message Received by Robie": Round 725 0f36700 note 1 of 2, please ignore'
)
NOTE_1_LIVE = (
    "@Robie [[robie-test]] Add a note to Buster Brown 26356199 on the discussion "
    "\u201cMessage Received by Robie\u201d: Round 725 0f36700 note 1 of 2, please ignore"
)
NOTE_1_USER = (
    "<users/123456> [[robie-test]] Add a note to Buster Brown 26356199 on the "
    'discussion \u201cMessage Received by Robie\u201d: Round 725 0f36700 note 1 of 2, '
    "please ignore"
)
NOTE_2_LIVE = (
    "@Robie [[robie-test]] Add a note to Buster Brown 26356199 on the discussion "
    "\u201cPolicy Change Request Checkup - Mailing Address update\u201d: "
    "Round 725 0f36700 note 2 of 2, please ignore"
)
TITLE_1 = "Message Received by Robie"
TITLE_2 = "Policy Change Request Checkup - Mailing Address update"
ID_1 = "848110016"
ID_2 = "229078800"


@pytest.fixture
def tmp_path():
    from durable_temp import durable_temporary_directory

    with durable_temporary_directory() as tmp:
        yield Path(tmp)


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    for key in ("ROBIE_JOB_ID", "ROBIE_CURRENT_JOB_ID", "ROBIE_JOB_DB", "JOB_ID"):
        monkeypatch.delenv(key, raising=False)
    with isolated_model_generation_context():
        yield


def _rows():
    return [
        {"discussionId": ID_1, "title": TITLE_1},
        {"discussionId": ID_2, "title": TITLE_2},
        {"discussionId": "other", "title": "follw up 1"},
    ]


@pytest.mark.parametrize(
    "request_text,hint",
    [
        (NOTE_1, TITLE_1),
        (NOTE_1_LIVE, "\u201cMessage Received by Robie\u201d"),
        (NOTE_1_USER, '"Message Received by Robie"'),
        (NOTE_2_LIVE, "\u201c" + TITLE_2 + "\u201d"),
    ],
)
def test_live_inbound_text_selects_the_quoted_discussion(tmp_path, request_text, hint):
    store = JobStore(str(tmp_path / "jobs.db"))
    owner = running(store, request_text, applicant_id="26356199")
    begin_model_generation(owner, store=store)
    chosen = select_discussion_for_note(_rows(), title_hint=hint)
    expected = ID_2 if TITLE_2 in request_text else ID_1
    assert chosen["discussionId"] == expected


class _NoteClient(LiveShapeClient):
    def __init__(self):
        super().__init__(post_body={"noteId": "701"}, discussion_id=ID_1)
        self.title = TITLE_1
        self.after_title = TITLE_1
        self.body = ""

    def get_discussions(self, applicant_id):
        return [
            {"discussionId": ID_1, "title": TITLE_1, "applicantId": applicant_id},
            {"discussionId": ID_2, "title": TITLE_2, "applicantId": applicant_id},
        ]

    def get_discussion(self, discussion_id):
        self.title = TITLE_1 if discussion_id == ID_1 else TITLE_2
        self.after_title = self.title
        return {**super().get_discussion(discussion_id), "applicantId": "26356199"}

    def append_note(self, discussion_id, text, note_type="Note"):
        self.body = text
        self.discussion_id = discussion_id
        return super().append_note(discussion_id, text, note_type)

    def list_notes(self, discussion_id):
        return [{"noteId": "701", "noteText": self.body}] if self.posted else []


def _run_tool(tmp_path, monkeypatch, *, go: str | None, thread_id: str | None = None):
    from robie_job_engine import ezlynx_api_only_writes as writes
    from robie_job_engine import ezlynx_write_scope
    from test_tonight_fix_bundle import _load_hermes_tool, _restore_modules

    monkeypatch.setattr(
        ezlynx_write_scope,
        "ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({"26356199"}),
    )
    store = JobStore(str(tmp_path / "jobs.db"))
    owner = running(store, NOTE_1_LIVE, applicant_id="26356199")
    begin_model_generation(owner, store=store)
    store.checkpoint(owner, PLAN_CHECKPOINT, {"locked": True})
    if go:
        bound = bind_chat_write_go(
            store,
            owner,
            go,
            thread_id=thread_id or "",
            message_id="m-go",
        )
        if not thread_id:
            assert bound
    client = _NoteClient()
    ledger = tmp_path / "ledger.json"
    actual = partial(
        writes.add_note_to_discussion,
        discussion_client=client,
        ledger_path=ledger,
    )
    posted: list[str] = []
    note, previous, created = _load_hermes_tool(
        "round_0f36700_note_tool", "ezlynx_note_tool.py"
    )
    try:
        with patch.object(writes, "add_note_to_discussion", side_effect=actual):
            response = note.ezlynx_discussion_note_handler(
                {
                    "applicant_id": "26356199",
                    "note_text": "Round 725 0f36700 note 1 of 2, please ignore",
                    "title_hint": "\u201cMessage Received by Robie\u201d",
                },
                job_id=owner,
                db_path=str(store.path),
                outcome_poster=lambda _space, text, _thread, _job: posted.append(text),
            )
    finally:
        _restore_modules(previous, created)
    return response, client, ledger, posted, store, owner


def test_selection_reads_the_text_field_when_request_text_is_empty(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    owner = running(store, "placeholder", applicant_id="26356199")
    job = store.get_job(owner)
    payload = dict(job.get("payload") or {})
    payload["text"] = NOTE_1_LIVE
    payload["request_text"] = ""
    payload["original_text"] = ""
    store.update_payload(owner, payload)
    begin_model_generation(owner, store=store)
    chosen = select_discussion_for_note(
        _rows(), title_hint="\u201cMessage Received by Robie\u201d"
    )
    assert chosen["discussionId"] == ID_1


def test_model_tool_without_go_posts_nothing(tmp_path, monkeypatch):
    response, client, ledger, posted, _store, _owner = _run_tool(
        tmp_path, monkeypatch, go=None
    )
    assert client.posts == 0, response
    result = response["result"]
    assert result["status"] == "awaiting_go"
    assert result["discussion_id"] == ID_1
    assert result["discussion_title"] == TITLE_1
    assert "Say go" in str(result["reason"])
    assert "already added" not in str(result["reason"]).casefold()
    assert not ledger.exists() or not json.loads(ledger.read_text()).get("notes")
    assert posted
    assert "Say go" in posted[0]
    assert "(no reply)" not in " ".join(posted)


def test_model_tool_posts_once_after_go_in_that_thread(tmp_path, monkeypatch):
    response, client, _ledger, _posted, _store, _owner = _run_tool(
        tmp_path, monkeypatch, go="go"
    )
    assert client.posts == 1, response
    assert response["result"]["status"] == "filed"
    assert response["result"]["discussion_id"] == ID_1


def test_go_in_another_thread_does_not_post(tmp_path, monkeypatch):
    response, client, _ledger, _posted, _store, _owner = _run_tool(
        tmp_path,
        monkeypatch,
        go="go",
        thread_id="spaces/other/threads/not-this-job",
    )
    assert client.posts == 0, response
    assert response["result"]["status"] == "awaiting_go"


def test_http_write_refuses_without_a_go_token(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    owner = running(
        store, "Add a note to applicant 26356199", applicant_id="26356199"
    )
    begin_model_generation(owner, store=store)
    with pytest.raises(RuntimeError, match="say go"):
        permit_chat_http_write()
    assert bind_chat_write_go(store, owner, "go ahead", message_id="m-go")
    permit_chat_http_write()
    with pytest.raises(RuntimeError, match="say go"):
        permit_chat_http_write()


def test_unconfirmed_repeat_is_not_described_as_added(tmp_path):
    from robie_job_engine.discussion_note_ledger import record_posted_note
    from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion

    ledger = tmp_path / "ledger.json"
    record_posted_note(
        "220250093",
        ID_1,
        note_text="Round 725 0f36700 note 1 of 2, please ignore\n\nRobie was here",
        ledger_path=ledger,
        confirmation="sent, unconfirmed",
    )

    class One(LiveShapeClient):
        def __init__(self):
            super().__init__(discussion_id=ID_1)
            self.title = TITLE_1
            self.after_title = TITLE_1

        def get_discussions(self, applicant_id):
            return [{"discussionId": ID_1, "title": TITLE_1, "applicantId": applicant_id}]

    client = One()
    again = file_note_to_existing_discussion(
        client,
        "220250093",
        "Round 725 0f36700 note 1 of 2, please ignore\n\nRobie was here",
        title_hint=TITLE_1,
        ledger_path=ledger,
    )
    assert client.posts == 0
    assert again["status"] == "already_posted"
    assert "couldn't confirm" in again["reason"].casefold()
    assert "already added" not in again["reason"].casefold()
    assert "Want me to add it again?" not in again["reason"]


def test_confirmed_repeat_still_asks_before_adding_again(tmp_path):
    from robie_job_engine.discussion_note_ledger import record_posted_note
    from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion

    ledger = tmp_path / "ledger.json"
    record_posted_note(
        "220250093",
        ID_1,
        note_text="Hello there. Robie was here",
        note_id="701",
        ledger_path=ledger,
        confirmation="confirmed",
    )

    class One(LiveShapeClient):
        def __init__(self):
            super().__init__(discussion_id=ID_1)
            self.title = TITLE_1
            self.after_title = TITLE_1

        def get_discussions(self, applicant_id):
            return [{"discussionId": ID_1, "title": TITLE_1, "applicantId": applicant_id}]

    client = One()
    again = file_note_to_existing_discussion(
        client,
        "220250093",
        "Hello there. Robie was here",
        title_hint=TITLE_1,
        ledger_path=ledger,
    )
    assert client.posts == 0
    assert "Want me to add it again?" in again["reason"]
    assert "already added" in again["reason"].casefold()


def test_turn_end_never_patches_no_reply(tmp_path):
    chat = _chat(str(tmp_path / "jobs.db"))
    space = "spaces/room"
    chat._typing_messages[space] = "spaces/room/messages/thinking"
    patched: list[tuple[str, str]] = []

    async def patch(name, body):
        patched.append((name, body.get("text")))
        return SimpleNamespace(success=True, message_id=name)

    chat._patch_message = patch
    outcome = _adapter_module().ProcessingOutcome
    outcome.CANCELLED = "cancelled"
    event = SimpleNamespace(source=SimpleNamespace(chat_id=space))
    asyncio.run(chat.on_processing_complete(event, "success"))
    assert patched == [("spaces/room/messages/thinking", "·")]
    assert "(no reply)" not in " ".join(text or "" for _, text in patched)

    chat._typing_messages[space] = "spaces/room/messages/thinking-2"
    asyncio.run(chat.on_processing_complete(event, outcome.CANCELLED))
    assert patched[-1] == ("spaces/room/messages/thinking-2", "(interrupted)")
    assert "(no reply)" not in " ".join(text or "" for _, text in patched)


def test_outcome_question_retires_the_thinking_card(tmp_path):
    chat = _chat(str(tmp_path / "jobs.db"))
    store = JobStore(str(tmp_path / "jobs.db"))
    owner = running(store, NOTE_1, applicant_id="26356199")
    bind_job_chat_thread(store, owner, THREAD)
    card = "spaces/ROBY/messages/thinking"
    chat._typing_messages[SPACE] = card
    patched: list[dict] = []

    class _Exec:
        def __init__(self, body):
            self.body = body

        def execute(self, http=None):
            del http
            return {"name": "spaces/ROBY/messages/out", "thread": {"name": THREAD}}

    class _Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return _Exec(kwargs.get("body") or {})

        def patch(self, **kwargs):
            patched.append(kwargs)
            return _Exec(kwargs.get("body") or {})

    class _Spaces:
        def __init__(self, messages):
            self._messages = messages

        def messages(self):
            return self._messages

    messages = _Messages()
    chat._chat_api.spaces = lambda: _Spaces(messages)
    chat.post_outcome_sync(SPACE, "Which discussion should I use: one, or two?", THREAD, owner)
    assert patched
    assert patched[0]["body"]["text"] == "·"
    assert chat._typing_messages.get(SPACE) in (None, "<consumed>")

    async def patch_later(name, body):
        patched.append({"name": name, "body": body})
        return SimpleNamespace(success=True, message_id=name)

    chat._patch_message = patch_later
    event = SimpleNamespace(source=SimpleNamespace(chat_id=SPACE))
    asyncio.run(chat.on_processing_complete(event, "success"))
    assert "(no reply)" not in json.dumps(patched)
