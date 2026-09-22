"""Tests for confirmation_notify.py (Chat + email fan-out, idempotent)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import confirmation_notify as notify
from robie_job_engine import confirmations
from robie_job_engine.store import JobStore


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "jobs.db")


def _request(store, **kw):
    args = dict(
        loop_job_id="loop-1",
        job_type="policy_change",
        draft_summary="Raise written premium",
        changes_json={"policy_number": "HO-1", "changes": {"writtenPremium": "2450"}},
        requested_by="robie",
    )
    args.update(kw)
    return confirmations.request_confirmation(store=store, **args)


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


def test_notify_sends_chat_and_email(db):
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    assert len(result["notified"]) == 2
    assert result["errors"] == []
    assert len(chat.posts) == 1
    assert "needs your approval" in chat.posts[0]
    assert "Confirmations" in chat.posts[0]
    assert len(gmail.sent) == 1
    to, subject, body = gmail.sent[0]
    assert to == "carlo@streetsmart.insurance"
    assert "[ROBIE] Approval needed" in subject
    assert "Nothing moves until you decide" in body


def test_notify_is_idempotent(db):
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    assert result["notified"] == []
    assert result["errors"] == []
    assert len(chat.posts) == 1
    assert len(gmail.sent) == 1


def test_notify_skips_non_pending(db):
    store = JobStore(db)
    cid = _request(store)
    confirmations.approve(cid, "Carlo Ferrara", store=store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    assert result["skipped"] == "not PENDING"
    assert chat.posts == [] and gmail.sent == []


def test_one_channel_failure_does_not_block_the_other(db):
    store = JobStore(db)
    cid = _request(store)

    def bad_chat(text):
        raise RuntimeError("chat down")

    gmail = FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=bad_chat, gmail_sender=gmail)
    assert len(result["errors"]) == 1
    assert result["errors"][0]["channel"] == "google_chat"
    assert len(gmail.sent) == 1
    # Failed channel retries next time; the good one does not resend.
    chat2 = FakeChat()
    result2 = notify.notify_requested(store, cid, chat_poster=chat2, gmail_sender=gmail)
    assert len(chat2.posts) == 1
    assert len(gmail.sent) == 1
    assert result2["errors"] == []


def test_unknown_confirmation_raises(db):
    store = JobStore(db)
    with pytest.raises(ValueError):
        notify.notify_requested(store, "nope", chat_poster=FakeChat(), gmail_sender=FakeGmail())


class FakeZap:
    def __init__(self):
        self.payloads: list[dict] = []

    def __call__(self, payload):
        self.payloads.append(dict(payload))
        return {"zap_output": "ok"}


def test_assign_requester_task_fires_zap_with_login_username(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Carlo Ferrara")
    zap = FakeZap()
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["assignee"] == "Carlo1"
    assert len(zap.payloads) == 1
    payload = zap.payloads[0]
    assert payload["applicant_id"] == "220250093"
    assert payload["assignee"] == "Carlo1"
    assert payload["due_date"]  # Carlo's rule: always a due date
    assert "Confirmations tab" in payload["task_description"]


def test_assign_requester_task_is_idempotent(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Karla Brown")
    zap = FakeZap()
    notify.assign_requester_task(store, cid, applicant_id="220250093", zap_trigger=zap)
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["skipped"] == "already assigned"
    assert len(zap.payloads) == 1


def test_assign_requester_task_fails_closed_on_unknown_requester(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Some Stranger")
    with pytest.raises(ValueError, match="no known EZLynx login username"):
        notify.assign_requester_task(
            store, cid, applicant_id="220250093", zap_trigger=FakeZap()
        )


def test_assign_requester_task_env_override_adds_a_login(db, monkeypatch):
    store = JobStore(db)
    cid = _request(store, requested_by="Jake Ferrara")
    monkeypatch.setenv("ROBIE_EZLYNX_LOGIN_JAKE_FERRARA", "JakeSS")
    zap = FakeZap()
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["assignee"] == "JakeSS"


def test_assign_requester_task_requires_applicant_id(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Carlo Ferrara")
    with pytest.raises(ValueError, match="applicant_id is required"):
        notify.assign_requester_task(store, cid, applicant_id="", zap_trigger=FakeZap())


def test_assign_requester_task_skips_decided(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Carlo Ferrara")
    confirmations.approve(cid, "Carlo Ferrara", store=store)
    zap = FakeZap()
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["skipped"] == "not PENDING"
    assert zap.payloads == []
