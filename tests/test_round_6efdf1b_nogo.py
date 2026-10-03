"""Regressions from the live Test round on head 6efdf1b.

Quoted discussion titles must select. The note plan is only the text
after the colon. A suppressed model reply must not leave "(no reply)".
A clarification stays open for 30 minutes, and an expired answer is not
a new job. A bare listed title, including one prefixed by @Robie, binds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from robie_job_engine.ezlynx_discussions import (
    DiscussionSelectionError,
    ambiguous_discussion_question,
    recent_discussion_titles,
    select_discussion_for_note,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import discussion_note_body
from robie_job_engine.store import JobStore
from robie_job_engine.turn_finalization import (
    begin_model_generation,
    isolated_model_generation_context,
)
from robie_job_engine.user_reply import format_outbound_reply
from robie_job_engine.write_verification_loop import (
    _reply_text,
    compare_plan_to_api,
    lock_stated_plan,
)
from test_round10_reply_lifecycle import _chat


@pytest.fixture
def tmp_path():
    from pathlib import Path

    from durable_temp import durable_temporary_directory

    with durable_temporary_directory() as tmp:
        yield Path(tmp)

REQUEST_A = (
    '[[robie-test]] Add a note to Buster Brown 26356199 on the discussion '
    '"Message Received by Robie": Round 725 6efdf1b note 2 of 2, please ignore'
)
BODY_A = "Round 725 6efdf1b note 2 of 2, please ignore"
TITLE_A = "Message Received by Robie"
REQUEST_B = (
    '[[robie-test]] Add a note to Buster Brown 26356199 on the discussion '
    '"Policy Change Request Checkup - Mailing Address update": '
    "Round 725 6efdf1b note 1 of 2, please ignore"
)
BODY_B = "Round 725 6efdf1b note 1 of 2, please ignore"
TITLE_B = "Policy Change Request Checkup - Mailing Address update"
QUOTED_IN_BODY = (
    '[[robie-test]] Add a note to Buster Brown 26356199 on the discussion '
    '"Message Received by Robie": the other title is '
    '"Policy Change Request Checkup - Mailing Address update", please ignore'
)


def _rows():
    dated = [
        ("1", "Additional Information - CHANGE ME", "2026-10-03T00:06:00Z"),
        ("2", "follw up 1", "2026-10-03T00:05:00Z"),
        ("3", TITLE_A, "2026-10-03T00:04:00Z"),
        ("4", "Renewal Manual Policy | Buster Brown", "2026-10-03T00:03:00Z"),
        ("5", "Robie test1", "2026-10-03T00:02:00Z"),
        ("6", TITLE_B, "2026-10-03T00:01:00Z"),
    ]
    return [
        {"discussionId": ident, "title": title, "updatedAt": stamp}
        for ident, title, stamp in dated
    ]


def _bound(store, text):
    job = store.create_job(
        "ezlynx.discussion_note",
        {
            "text": text,
            "request_text": text,
            "original_text": text,
        },
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    begin_model_generation(job["id"], store=store)
    return job["id"]


def test_live_requests_select_the_quoted_discussion_and_the_body(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    with isolated_model_generation_context():
        _bound(store, REQUEST_A)
        chosen = select_discussion_for_note(_rows(), title_hint=TITLE_A)
        assert chosen["discussionId"] == "3"
        assert discussion_note_body(REQUEST_A) == BODY_A
        _bound(store, REQUEST_B)
        chosen = select_discussion_for_note(_rows(), title_hint=TITLE_B)
        assert chosen["discussionId"] == "6"
        assert discussion_note_body(REQUEST_B) == BODY_B


def test_quoted_title_inside_the_note_body_does_not_select(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    with isolated_model_generation_context():
        _bound(store, QUOTED_IN_BODY)
        chosen = select_discussion_for_note(_rows(), title_hint=TITLE_A)
        assert chosen["discussionId"] == "3"
        with pytest.raises(DiscussionSelectionError):
            select_discussion_for_note(_rows(), title_hint=TITLE_B)


def test_exact_title_outside_the_recent_five_is_listed_first():
    titles = recent_discussion_titles(_rows(), preferred=TITLE_B)
    assert titles[0] == TITLE_B
    assert TITLE_B in titles
    assert len(titles) == 5
    question = ambiguous_discussion_question(titles, hint=TITLE_B)
    assert question.startswith("Which discussion should I use:")
    assert not question.startswith(f"Which {TITLE_B}")
    assert TITLE_B in question


def test_mention_prefixed_answer_binds_the_listed_discussion(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    with isolated_model_generation_context():
        owner = _bound(store, REQUEST_A)
        store.checkpoint(
            owner,
            "clarification_reply",
            {"text": "@Robie Message Received by Robie"},
        )
        chosen = select_discussion_for_note(_rows(), title_hint="follw up 1")
        assert chosen["discussionId"] == "3"
        assert chosen["title"] == TITLE_A


def test_saved_plan_and_readback_are_only_the_note_body(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    job = store.create_job(
        "ezlynx.discussion_note",
        {"text": REQUEST_A, "request_text": REQUEST_A, "original_text": REQUEST_A},
    )
    plan = lock_stated_plan(
        store,
        job,
        {
            "write": "discussion note",
            "target": {"discussion": TITLE_A, "applicant_id": "26356199"},
            "values": {"note_text": REQUEST_A},
        },
    )
    assert plan["values"]["note_text"] == BODY_A
    assert plan["values"]["note_text"] == discussion_note_body(REQUEST_A)
    readback = compare_plan_to_api(plan, {"note_text": BODY_A})
    assert readback["items"][0]["expected"] == BODY_A
    decision = SimpleNamespace(display_verdict="correct", confidence=90, reason="ok")
    shown = _reply_text(job["id"], readback, decision)
    assert BODY_A in shown
    assert "Add a note to Buster" not in shown


def test_suppressed_reply_retires_the_thinking_card(tmp_path):
    chat = _chat(str(tmp_path / "jobs.db"))
    space = "spaces/room"
    chat._typing_messages[space] = "spaces/room/messages/thinking"
    patched = []

    async def patch(name, body):
        patched.append((name, body.get("text")))
        return SimpleNamespace(success=True, message_id=name)

    chat._patch_message = patch
    asyncio.run(chat._retire_suppressed_typing_card(space))
    from test_round10_reply_lifecycle import _adapter_module

    outcome_type = _adapter_module().ProcessingOutcome
    outcome_type.CANCELLED = "cancelled"
    event = SimpleNamespace(source=SimpleNamespace(chat_id=space))
    asyncio.run(chat.on_processing_complete(event, "success"))
    assert patched == [("spaces/room/messages/thinking", "·")]
    assert "(no reply)" not in " ".join(text or "" for _, text in patched)
    assert chat._typing_messages.get(space) in (None, "<consumed>")


def test_expired_notice_keeps_the_original_ask():
    from robie_job_engine.chat_job_controls import WAITING_EXPIRED_NOTE, expired_question_reply

    job = {
        "id": "job",
        "payload": {"original_text": REQUEST_A, "text": REQUEST_A},
    }
    reply = expired_question_reply(None, job)
    assert reply.startswith(WAITING_EXPIRED_NOTE)
    assert REQUEST_A in reply
    shown = format_outbound_reply(reply, None)
    assert "expired" in shown.casefold()
    assert "Round 725 6efdf1b note 2 of 2" in shown
