"""Tests for the origin-medium notify policy (Carlo 2026-09-22).

Origin wins: the approval ask goes out on the medium where it started --
never blasted to Chat + EZLynx + email at once. EZLynx only when identity
is known; a Chat-origin ask never writes EZLynx; an EZLynx-origin ask stays
in EZLynx until the quiet window, then one Chat nudge.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import confirmation_notify as notify
from robie_job_engine import confirmations
from robie_job_engine.store import JobStore


TEST_KEY = "test-decision-signing-key-0123456789abcdef"


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "jobs.db")


def _request(store, **kw):
    args = dict(
        loop_job_id="loop-1",
        job_type="policy_change",
        draft_summary="Raise written premium",
        changes_json={"policy_number": "HO-1", "changes": {"writtenPremium": "2450"}},
        requested_by="carlo",
    )
    args.update(kw)
    return confirmations.request_confirmation(store=store, **args)


class FakeCardPoster:
    def __init__(self):
        self.posts: list[dict] = []

    def __call__(self, space, card_v2, thread=None):
        self.posts.append({"space": space, "card": card_v2, "thread": thread})
        return {"space": space, "message_name": "spaces/AAA/messages/9"}


class FakeChat:
    def __init__(self):
        self.posts: list[str] = []

    def __call__(self, text):
        self.posts.append(text)
        return {"space": "spaces/DM", "message_name": "spaces/DM/messages/1"}


class FakeGmail:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, to, subject, body):
        self.sent.append((to, subject, body))
        return {"message_id": "msg-1", "to": to}


class FakeZap:
    def __init__(self):
        self.fired: list[dict] = []

    def __call__(self, payload):
        self.fired.append(dict(payload))
        return {"ok": True}


def _route(store, cid, **kw):
    kw.setdefault("decision_key", TEST_KEY)
    return notify.notify_approval_on_origin(store, cid, **kw)


# ---------------------------------------------------------------------------
# chat origin
# ---------------------------------------------------------------------------

def test_chat_origin_posts_card_to_origin_thread_only(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    card_poster, gmail, zap = FakeCardPoster(), FakeGmail(), FakeZap()
    result = _route(store, cid, approval_card_poster=card_poster,
                    gmail_sender=gmail, zap_trigger=zap)
    assert result["errors"] == []
    channels = [n["channel"] for n in result["notified"]]
    assert notify.CHANNEL_ORIGIN_CHAT_CARD in channels
    assert notify.CHANNEL_BACKUP_EMAIL in channels
    # The card went to the originating thread.
    assert len(card_poster.posts) == 1
    assert card_poster.posts[0]["space"] == "spaces/AAA"
    assert card_poster.posts[0]["thread"] == "spaces/AAA/threads/TTT"
    # Backup email is tokenless: not a second decision thread.
    assert len(gmail.sent) == 1
    _, _, body = gmail.sent[0]
    assert not any(
        line.strip().startswith(confirmations.DECISION_TOKEN_PREFIX)
        for line in body.splitlines()
    )
    # EZLynx never touched.
    assert zap.fired == []
    assert not notify.was_notified(store, cid, "ezlynx_task")


def test_chat_origin_with_applicant_still_never_writes_ezlynx(db):
    """Even when an applicant id is on the job, a Chat-origin ask never
    invents an EZLynx note/task."""
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={
            "space": "spaces/AAA",
            "thread": "spaces/AAA/threads/TTT",
            "applicant_id": "12345",
        },
    )
    zap = FakeZap()
    result = _route(store, cid, approval_card_poster=FakeCardPoster(),
                    gmail_sender=FakeGmail(), zap_trigger=zap)
    assert result["errors"] == []
    assert zap.fired == []
    assert not notify.was_notified(store, cid, "ezlynx_task")


def test_chat_origin_card_buttons_carry_valid_tokens(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    card_poster = FakeCardPoster()
    _route(store, cid, approval_card_poster=card_poster,
           gmail_sender=FakeGmail(), zap_trigger=FakeZap())
    buttons = card_poster.posts[0]["card"]["card"]["sections"][0]["widgets"][2][
        "buttonList"]["buttons"]
    for button, decision in zip(buttons, ("APPROVE", "REJECT")):
        token = next(
            p["value"] for p in button["onClick"]["action"]["parameters"]
            if p["key"] == "decision_token"
        )
        verified = confirmations.verify_decision_token(token, key=TEST_KEY)
        assert verified["confirmation_id"] == cid
        assert verified["decision"] == decision


def test_chat_origin_without_space_fails_closed(db):
    store = JobStore(db)
    cid = _request(store, origin_platform="chat", origin_ref={"thread": "t"})
    result = _route(store, cid, approval_card_poster=FakeCardPoster(),
                    gmail_sender=FakeGmail(), zap_trigger=FakeZap())
    assert result["notified"] == [] or all(
        n["channel"] != notify.CHANNEL_ORIGIN_CHAT_CARD for n in result["notified"]
    )
    assert any(e["channel"] == notify.CHANNEL_ORIGIN_CHAT_CARD for e in result["errors"])
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


# ---------------------------------------------------------------------------
# email origin
# ---------------------------------------------------------------------------

def test_email_origin_sends_token_email_only(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="email",
        origin_ref={"to": "requester@example.com"},
    )
    card_poster, gmail, zap = FakeCardPoster(), FakeGmail(), FakeZap()
    result = _route(store, cid, approval_card_poster=card_poster,
                    gmail_sender=gmail, zap_trigger=zap)
    assert result["errors"] == []
    assert [n["channel"] for n in result["notified"]] == [notify.CHANNEL_ORIGIN_EMAIL]
    assert card_poster.posts == []
    assert zap.fired == []
    to, subject, body = gmail.sent[0]
    assert to == "requester@example.com"
    tokens = [line.strip() for line in body.splitlines()
              if line.strip().startswith(confirmations.DECISION_TOKEN_PREFIX)]
    assert len(tokens) == 2


def test_email_origin_without_address_fails_closed(db):
    store = JobStore(db)
    cid = _request(store, origin_platform="email", origin_ref={})
    result = _route(store, cid, approval_card_poster=FakeCardPoster(),
                    gmail_sender=FakeGmail(), zap_trigger=FakeZap())
    assert result["notified"] == []
    assert any(e["channel"] == notify.CHANNEL_ORIGIN_EMAIL for e in result["errors"])
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


# ---------------------------------------------------------------------------
# ezlynx origin
# ---------------------------------------------------------------------------

def test_ezlynx_origin_assigns_task_only(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="ezlynx",
        origin_ref={"applicant_id": "12345"},
    )
    card_poster, gmail, zap = FakeCardPoster(), FakeGmail(), FakeZap()
    result = _route(store, cid, approval_card_poster=card_poster,
                    gmail_sender=gmail, zap_trigger=zap)
    assert result["errors"] == []
    assert [n["channel"] for n in result["notified"]] == ["ezlynx_task"]
    assert len(zap.fired) == 1
    assert zap.fired[0]["applicant_id"] == "12345"
    assert zap.fired[0]["confirmation_id"] == cid
    # No Chat thread opened up front, no email ask.
    assert card_poster.posts == []
    assert gmail.sent == []


def test_ezlynx_origin_without_applicant_fails_closed(db):
    """No applicant id: no EZLynx write is invented, the ask stays PENDING."""
    store = JobStore(db)
    cid = _request(store, origin_platform="ezlynx", origin_ref={})
    zap = FakeZap()
    result = _route(store, cid, approval_card_poster=FakeCardPoster(),
                    gmail_sender=FakeGmail(), zap_trigger=zap)
    assert result["notified"] == []
    assert any(e["channel"] == "ezlynx_task" for e in result["errors"])
    assert zap.fired == []
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


# ---------------------------------------------------------------------------
# missing / unknown origin
# ---------------------------------------------------------------------------

def test_missing_origin_falls_back_to_legacy_fanout(db):
    store = JobStore(db)
    cid = _request(store)  # no origin_platform
    chat, gmail = FakeChat(), FakeGmail()
    result = _route(store, cid, chat_poster=chat, gmail_sender=gmail,
                    zap_trigger=FakeZap())
    assert result["errors"] == []
    channels = [n["channel"] for n in result["notified"]]
    assert "google_chat" in channels
    assert "email" in channels


def test_unknown_origin_platform_is_not_routed(db):
    # request_confirmation rejects unknown platforms at creation: an ask
    # can never enter the ledger with an unroutable origin.
    store = JobStore(db)
    with pytest.raises(ValueError, match="origin_platform must be chat, email, or ezlynx"):
        _request(store, origin_platform="carrier_pigeon")


def test_router_is_idempotent(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    card_poster, gmail = FakeCardPoster(), FakeGmail()
    first = _route(store, cid, approval_card_poster=card_poster,
                   gmail_sender=gmail, zap_trigger=FakeZap())
    second = _route(store, cid, approval_card_poster=card_poster,
                    gmail_sender=gmail, zap_trigger=FakeZap())
    assert len(first["notified"]) == 2
    assert second["notified"] == []
    assert second["errors"] == []
    assert len(card_poster.posts) == 1
    assert len(gmail.sent) == 1


# ---------------------------------------------------------------------------
# quiet nudge
# ---------------------------------------------------------------------------

def _backdate_ezlynx_leg(db, cid, hours_ago):
    moment = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE confirmation_notifications SET notified_at=? "
        "WHERE confirmation_id=? AND channel='ezlynx_task'",
        (moment, cid),
    )
    conn.commit()
    conn.close()


def _ezlynx_pending(db, store):
    cid = _request(
        store,
        origin_platform="ezlynx",
        origin_ref={"applicant_id": "12345"},
    )
    zap = FakeZap()
    notify.notify_approval_on_origin(store, cid, zap_trigger=zap, decision_key=TEST_KEY)
    assert len(zap.fired) == 1
    return cid


def test_quiet_nudge_fires_once_after_window(db):
    store = JobStore(db)
    cid = _ezlynx_pending(db, store)
    _backdate_ezlynx_leg(db, cid, hours_ago=30)
    chat = FakeChat()
    first = notify.maybe_quiet_nudge(store, cid, chat_poster=chat, quiet_hours=24)
    assert [n["channel"] for n in first["notified"]] == [notify.CHANNEL_CHAT_NUDGE]
    assert len(chat.posts) == 1
    assert "one nudge" in chat.posts[0]
    assert "EZLynx" in chat.posts[0]
    second = notify.maybe_quiet_nudge(store, cid, chat_poster=chat, quiet_hours=24)
    assert second["notified"] == []
    assert second["skipped"] == "already nudged"
    assert len(chat.posts) == 1


def test_quiet_nudge_waits_out_the_window(db):
    store = JobStore(db)
    cid = _ezlynx_pending(db, store)
    _backdate_ezlynx_leg(db, cid, hours_ago=2)
    chat = FakeChat()
    result = notify.maybe_quiet_nudge(store, cid, chat_poster=chat, quiet_hours=24)
    assert result["notified"] == []
    assert "quiet window" in result["skipped"]
    assert chat.posts == []


def test_quiet_nudge_skips_non_ezlynx_origin(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    chat = FakeChat()
    result = notify.maybe_quiet_nudge(store, cid, chat_poster=chat, quiet_hours=0.001)
    assert result["skipped"] == "not ezlynx origin"
    assert chat.posts == []


def test_quiet_nudge_skips_when_ezlynx_leg_never_sent(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="ezlynx",
        origin_ref={"applicant_id": "12345"},
    )
    chat = FakeChat()
    result = notify.maybe_quiet_nudge(store, cid, chat_poster=chat, quiet_hours=0.001)
    assert result["skipped"] == "ezlynx leg not sent yet"
    assert chat.posts == []


def test_quiet_nudge_skips_decided(db):
    store = JobStore(db)
    cid = _ezlynx_pending(db, store)
    _backdate_ezlynx_leg(db, cid, hours_ago=30)
    confirmations.approve(cid, decided_by="carlo@streetsmart.insurance", store=store)
    chat = FakeChat()
    result = notify.maybe_quiet_nudge(store, cid, chat_poster=chat, quiet_hours=24)
    assert result["skipped"] == "not PENDING"
    assert chat.posts == []
