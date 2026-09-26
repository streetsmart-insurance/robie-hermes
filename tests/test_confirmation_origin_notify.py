"""Tests for the origin-medium notify policy (Carlo 2026-09-22).

Origin wins: the approval ask goes out on the medium where it started --
never blasted to Chat + EZLynx + email at once. EZLynx only when identity
is known; a Chat-origin ask never writes EZLynx; an EZLynx-origin ask stays
in EZLynx until the quiet window, then one Chat nudge.

Chat-origin backup email is deferred (Carlo 2026-09-22): a successful Chat
card does not email. The tokenless backup goes out only when the Chat post
fails, or after the quiet window if the ask is still unanswered.
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


class BoomCardPoster:
    def __call__(self, space, card_v2, thread=None):
        raise RuntimeError("chat down")


def _body_has_decision_token(body: str) -> bool:
    return any(
        line.strip().startswith(confirmations.DECISION_TOKEN_PREFIX)
        for line in body.splitlines()
    )


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
    assert channels == [notify.CHANNEL_ORIGIN_CHAT_CARD]
    # The card went to the originating thread.
    assert len(card_poster.posts) == 1
    assert card_poster.posts[0]["space"] == "spaces/AAA"
    assert card_poster.posts[0]["thread"] == "spaces/AAA/threads/TTT"
    # A successful Chat post does not email at the same time.
    assert gmail.sent == []
    assert not notify.was_notified(store, cid, notify.CHANNEL_BACKUP_EMAIL)
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
    zap, gmail = FakeZap(), FakeGmail()
    result = _route(store, cid, approval_card_poster=FakeCardPoster(),
                    gmail_sender=gmail, zap_trigger=zap)
    assert result["errors"] == []
    assert zap.fired == []
    assert gmail.sent == []
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
    widgets = card_poster.posts[0]["card"]["card"]["sections"][0]["widgets"]
    button_lists = [w["buttonList"]["buttons"] for w in widgets if "buttonList" in w]
    assert len(button_lists) == 1
    buttons = button_lists[0]
    for button, decision in zip(buttons, ("APPROVE", "REJECT")):
        token = next(
            p["value"] for p in button["onClick"]["action"]["parameters"]
            if p["key"] == "decision_token"
        )
        verified = confirmations.verify_decision_token(token, key=TEST_KEY)
        assert verified["confirmation_id"] == cid
        assert verified["decision"] == decision


def test_chat_origin_without_space_fails_closed_and_emails(db):
    """A Chat post that cannot land emails immediately. Still no EZLynx write."""
    store = JobStore(db)
    cid = _request(store, origin_platform="chat", origin_ref={"thread": "t"})
    gmail, zap = FakeGmail(), FakeZap()
    result = _route(store, cid, approval_card_poster=FakeCardPoster(),
                    gmail_sender=gmail, zap_trigger=zap)
    assert all(
        n["channel"] != notify.CHANNEL_ORIGIN_CHAT_CARD for n in result["notified"]
    )
    assert any(e["channel"] == notify.CHANNEL_ORIGIN_CHAT_CARD for e in result["errors"])
    assert [n["channel"] for n in result["notified"]] == [notify.CHANNEL_BACKUP_EMAIL]
    assert len(gmail.sent) == 1
    _, subject, body = gmail.sent[0]
    assert "waiting in Chat" in subject
    assert not _body_has_decision_token(body)
    assert zap.fired == []
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_chat_post_failure_sends_backup_email_immediately(db):
    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    gmail, zap = FakeGmail(), FakeZap()
    result = _route(store, cid, approval_card_poster=BoomCardPoster(),
                    gmail_sender=gmail, zap_trigger=zap)
    assert any(e["channel"] == notify.CHANNEL_ORIGIN_CHAT_CARD for e in result["errors"])
    assert [n["channel"] for n in result["notified"]] == [notify.CHANNEL_BACKUP_EMAIL]
    assert len(gmail.sent) == 1
    _, _, body = gmail.sent[0]
    assert not _body_has_decision_token(body)
    assert "Confirmations tab" in body
    assert zap.fired == []
    assert not notify.was_notified(store, cid, notify.CHANNEL_ORIGIN_CHAT_CARD)
    # The failure email is once: a second failed post does not send another.
    again = _route(store, cid, approval_card_poster=BoomCardPoster(),
                   gmail_sender=gmail, zap_trigger=zap)
    assert again["notified"] == []
    assert len(gmail.sent) == 1
    # The quiet-window path must not send a second copy after the failure email.
    quiet = notify.maybe_quiet_backup_email(
        store, cid, gmail_sender=gmail, quiet_hours=0.001,
    )
    assert quiet["notified"] == []
    assert quiet["skipped"] == "already emailed"
    assert len(gmail.sent) == 1


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
    assert [n["channel"] for n in first["notified"]] == [notify.CHANNEL_ORIGIN_CHAT_CARD]
    assert second["notified"] == []
    assert second["errors"] == []
    assert len(card_poster.posts) == 1
    assert gmail.sent == []


# ---------------------------------------------------------------------------
# quiet nudge
# ---------------------------------------------------------------------------

def _backdate_channel(db, cid, channel, hours_ago):
    moment = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE confirmation_notifications SET notified_at=? "
        "WHERE confirmation_id=? AND channel=?",
        (moment, cid, channel),
    )
    conn.commit()
    conn.close()


def _backdate_ezlynx_leg(db, cid, hours_ago):
    _backdate_channel(db, cid, "ezlynx_task", hours_ago)


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


# ---------------------------------------------------------------------------
# chat-origin deferred backup email
# ---------------------------------------------------------------------------

def _chat_pending(db, store):
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    gmail = FakeGmail()
    result = _route(
        store, cid, approval_card_poster=FakeCardPoster(),
        gmail_sender=gmail, zap_trigger=FakeZap(),
    )
    assert [n["channel"] for n in result["notified"]] == [notify.CHANNEL_ORIGIN_CHAT_CARD]
    assert gmail.sent == []
    return cid, gmail


def test_quiet_window_unanswered_chat_sends_backup_email_once(db):
    store = JobStore(db)
    cid, gmail = _chat_pending(db, store)
    _backdate_channel(db, cid, notify.CHANNEL_ORIGIN_CHAT_CARD, hours_ago=30)
    zap = FakeZap()
    first = notify.maybe_quiet_backup_email(
        store, cid, gmail_sender=gmail, quiet_hours=24,
    )
    assert [n["channel"] for n in first["notified"]] == [notify.CHANNEL_BACKUP_EMAIL]
    assert len(gmail.sent) == 1
    _, subject, body = gmail.sent[0]
    assert "waiting in Chat" in subject
    assert not _body_has_decision_token(body)
    assert zap.fired == []
    second = notify.maybe_quiet_backup_email(
        store, cid, gmail_sender=gmail, quiet_hours=24,
    )
    assert second["notified"] == []
    assert second["skipped"] == "already emailed"
    assert len(gmail.sent) == 1
    # A later origin route must not send a second copy alongside the card.
    again = _route(
        store, cid, approval_card_poster=FakeCardPoster(),
        gmail_sender=gmail, zap_trigger=zap,
    )
    assert again["notified"] == []
    assert len(gmail.sent) == 1
    assert zap.fired == []


def test_quiet_backup_email_waits_out_the_window(db):
    store = JobStore(db)
    cid, gmail = _chat_pending(db, store)
    _backdate_channel(db, cid, notify.CHANNEL_ORIGIN_CHAT_CARD, hours_ago=2)
    result = notify.maybe_quiet_backup_email(
        store, cid, gmail_sender=gmail, quiet_hours=24,
    )
    assert result["notified"] == []
    assert "quiet window" in result["skipped"]
    assert gmail.sent == []


def test_quiet_backup_email_skips_decided(db):
    store = JobStore(db)
    cid, gmail = _chat_pending(db, store)
    _backdate_channel(db, cid, notify.CHANNEL_ORIGIN_CHAT_CARD, hours_ago=30)
    confirmations.approve(cid, decided_by="carlo@streetsmart.insurance", store=store)
    result = notify.maybe_quiet_backup_email(
        store, cid, gmail_sender=gmail, quiet_hours=24,
    )
    assert result["skipped"] == "not PENDING"
    assert gmail.sent == []


def test_quiet_backup_email_skips_non_chat_origin(db):
    store = JobStore(db)
    cid = _ezlynx_pending(db, store)
    _backdate_ezlynx_leg(db, cid, hours_ago=30)
    gmail = FakeGmail()
    result = notify.maybe_quiet_backup_email(
        store, cid, gmail_sender=gmail, quiet_hours=0.001,
    )
    assert result["skipped"] == "not chat origin"
    assert gmail.sent == []


def test_board_notify_defers_chat_email_until_quiet_window(db):
    """The Confirmations sync is what actually runs the quiet check."""
    from robie_job_engine import confirmation_board

    store = JobStore(db)
    cid = _request(
        store,
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    gmail, card, zap = FakeGmail(), FakeCardPoster(), FakeZap()
    record = confirmations.get(cid, store=store)
    first = confirmation_board._notify_pending(
        store, [record], "sheet-id",
        notify=True,
        chat_poster=FakeChat(),
        gmail_sender=gmail,
        approval_card_poster=card,
        zap_trigger=zap,
        decision_key=TEST_KEY,
        quiet_hours=24,
    )
    assert gmail.sent == []
    assert len(card.posts) == 1
    assert zap.fired == []
    assert all(
        notify.CHANNEL_BACKUP_EMAIL not in [n["channel"] for n in item.get("notified", [])]
        for item in first
    )
    _backdate_channel(db, cid, notify.CHANNEL_ORIGIN_CHAT_CARD, hours_ago=30)
    record = confirmations.get(cid, store=store)
    second = confirmation_board._notify_pending(
        store, [record], "sheet-id",
        notify=True,
        chat_poster=FakeChat(),
        gmail_sender=gmail,
        approval_card_poster=card,
        zap_trigger=zap,
        decision_key=TEST_KEY,
        quiet_hours=24,
    )
    assert len(gmail.sent) == 1
    assert not _body_has_decision_token(gmail.sent[0][2])
    assert len(card.posts) == 1
    assert zap.fired == []
    assert any(
        notify.CHANNEL_BACKUP_EMAIL in [n["channel"] for n in item.get("notified", [])]
        for item in second
    )
