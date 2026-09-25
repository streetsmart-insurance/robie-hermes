"""Stuck Processing cards on Chat Approve, and the decision race.

The bridge shows "Processing" before the gateway finishes. These tests
cover an owned Approve, a second click after the decision is recorded,
a foreign or unknown card, and one sweeper tick for a card that never
received a terminal update.

POLICY_CHANGE_ENABLED stays false. An approval here does not start
policy-change execution.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from robie_job_engine import confirmation_cards as cards
from robie_job_engine import confirmations
from robie_job_engine.policy_change_worker import POLICY_CHANGE_ENABLED
from robie_job_engine.stuck_processing_cards import (
    note_processing_card,
    run_stuck_processing_sweep,
    stuck_processing_actions,
    stuck_processing_minutes,
    sweep_stuck_processing_cards,
)
from robie_job_engine.store import JobStore
from test_chat_click_ownership import (
    MESSAGE,
    TEST_KEY,
    _bind,
    _click,
    _request,
    _Gateway,
    _principal,
)


INACTIVE = "This card is no longer active."
ALREADY_APPROVED = (
    "This request was already approved. The extra click changed nothing."
)
ALREADY_REJECTED = (
    "This request was already rejected. The extra click changed nothing."
)


def test_policy_change_kill_switch_stays_off():
    assert POLICY_CHANGE_ENABLED is False


def test_stuck_minutes_default_is_between_five_and_ten(monkeypatch):
    monkeypatch.delenv("ROBIE_STUCK_PROCESSING_CARD_MINUTES", raising=False)
    assert 5 <= stuck_processing_minutes() <= 10
    assert stuck_processing_minutes() == 8


def _request_for(store, loop_job_id: str) -> str:
    return confirmations.request_confirmation(
        store=store,
        loop_job_id=loop_job_id,
        job_type="policy_change",
        draft_summary="Raise written premium",
        changes_json={"policy_number": "HO-1"},
        requested_by="robie",
        origin_platform="chat",
        origin_ref={"space": "spaces/AAQA", "thread": "spaces/AAQA/threads/T"},
    )


def _open_rows(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT message_name FROM chat_processing_cards "
                "WHERE cleared_at IS NULL ORDER BY message_name"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def test_owned_approve_returns_terminal_update(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_DECISION_SIGNING_KEY", TEST_KEY)
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    db = str(tmp_path / "owner.db")
    store = JobStore(db)
    cid = _request(store)
    token = confirmations.mint_decision_token(cid, "APPROVE", _principal(), key=TEST_KEY)
    gateway = _bind(_Gateway(db))
    envelope = _click(token)

    http = asyncio.run(gateway.dispatch_http_event(envelope))
    assert http["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert http["cardsV2"] == []
    assert http["text"].startswith("✓ Approved.")
    assert "Processing" not in http["text"]
    assert confirmations.get(cid, store=store)["status"] == "APPROVED"
    assert _open_rows(db) == []
    assert POLICY_CHANGE_ENABLED is False

    # The Pub/Sub path patches the same terminal card in place.
    other = _request_for(store, "loop-2")
    other_token = confirmations.mint_decision_token(
        other, "APPROVE", _principal(), key=TEST_KEY
    )
    other_gateway = _bind(_Gateway(db))
    result = asyncio.run(
        other_gateway._handle_card_event(_click(other_token), notify=True)
    )
    assert result.startswith("Approved.")
    assert other_gateway.patches[0][0] == MESSAGE
    assert other_gateway.patches[0][1]["cardsV2"] == []
    assert other_gateway.patches[0][1]["text"].startswith("✓ Approved.")
    assert confirmations.get(other, store=store)["status"] == "APPROVED"
    assert _open_rows(db) == []


def test_already_decided_reclick_patches_terminal_card(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_DECISION_SIGNING_KEY", TEST_KEY)
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    db = str(tmp_path / "owner.db")
    store = JobStore(db)
    cid = _request(store)
    token = confirmations.mint_decision_token(cid, "APPROVE", _principal(), key=TEST_KEY)
    gateway = _bind(_Gateway(db))
    envelope = _click(token)
    first = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert first.startswith("Approved.")
    assert confirmations.get(cid, store=store)["status"] == "APPROVED"

    second = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    assert second == ALREADY_APPROVED
    assert http["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert http["cardsV2"] == []
    assert http["text"] == f"✓ {ALREADY_APPROVED}"
    assert "Processing" not in http["text"]
    assert gateway.patches[-1][1] == {"text": f"✓ {ALREADY_APPROVED}", "cardsV2": []}
    record = confirmations.get(cid, store=store)
    assert record["status"] == "APPROVED"
    assert record["decided_by"] == _principal()


def test_foreign_env_is_silent_and_unknown_routed_here_goes_inactive(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ROBIE_DECISION_SIGNING_KEY", TEST_KEY)
    owner_db = str(tmp_path / "owner.db")
    foreign_db = str(tmp_path / "foreign.db")
    owner_store = JobStore(owner_db)
    JobStore(foreign_db)
    cid = _request(owner_store)
    token = confirmations.mint_decision_token(cid, "APPROVE", _principal(), key=TEST_KEY)
    envelope = _click(token)

    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    foreign = _bind(_Gateway(foreign_db))
    assert asyncio.run(foreign._handle_card_event(envelope, notify=True)) is None
    assert asyncio.run(foreign.dispatch_http_event(envelope)) == {}
    assert foreign.patches == []
    assert confirmations.get(cid, store=owner_store)["status"] == "PENDING"
    assert _open_rows(foreign_db) == []

    monkeypatch.setenv("ROBIE_ENV", "TEST")
    unknown = _bind(_Gateway(str(tmp_path / "unknown.db")))
    unknown_click = _click("rbd1.unused", action="not_a_real_action")
    result = asyncio.run(unknown._handle_card_event(unknown_click, notify=True))
    http = asyncio.run(unknown.dispatch_http_event(unknown_click))
    assert result == INACTIVE
    assert unknown.patches[0][1] == {"text": f"✓ {INACTIVE}", "cardsV2": []}
    assert http["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert http["text"] == f"✓ {INACTIVE}"
    assert http["cardsV2"] == []


def test_lost_race_reread_patches_already_decided(tmp_path, monkeypatch):
    """In-memory result says Approved; the row was rejected first."""
    monkeypatch.setenv("ROBIE_DECISION_SIGNING_KEY", TEST_KEY)
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    db = str(tmp_path / "owner.db")
    store = JobStore(db)
    cid = _request(store)
    token = confirmations.mint_decision_token(cid, "APPROVE", _principal(), key=TEST_KEY)

    def lost_race(store_arg, payload, decision_key=None):
        parsed, _actor = cards.parse_confirmation_click(payload)
        verified = confirmations.verify_decision_token(parsed, key=TEST_KEY)
        confirmations.reject(
            verified["confirmation_id"],
            decided_by="other@example.com",
            reason="won the race",
            store=store_arg,
        )
        return cards.ConfirmationClickResult(
            status="APPROVED",
            decided=True,
            confirmation_id=verified["confirmation_id"],
            message=(
                "Approved. ROBIE recorded your decision and the request "
                "is no longer pending."
            ),
        )

    monkeypatch.setattr(cards, "resolve_confirmation_click", lost_race)
    gateway = _bind(_Gateway(db))
    envelope = _click(token)
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    assert http["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert http["cardsV2"] == []
    assert http["text"] == f"✓ {ALREADY_REJECTED}"
    assert "Approved. ROBIE recorded" not in http["text"]
    assert "Processing" not in http["text"]
    assert confirmations.get(cid, store=store)["status"] == "REJECTED"

    notify_gateway = _bind(_Gateway(db))
    # Row is already terminal, so a second delivery patches the same line.
    result = asyncio.run(notify_gateway._handle_card_event(envelope, notify=True))
    assert result == ALREADY_REJECTED
    assert notify_gateway.patches[0][1] == {
        "text": f"✓ {ALREADY_REJECTED}",
        "cardsV2": [],
    }


def test_stuck_sweeper_tick_clears_old_cards_and_retries(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBIE_STUCK_PROCESSING_CARD_MINUTES", raising=False)
    db = str(tmp_path / "jobs.db")
    store = JobStore(db)
    pending_id = _request(store)
    approved_id = _request_for(store, "loop-approved")
    confirmations.approve(approved_id, decided_by=_principal(), store=store)
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=9)
    fresh = now - timedelta(minutes=1)
    note_processing_card(
        db,
        message_name="spaces/AAQA/messages/old-pending",
        confirmation_id=pending_id,
        started_at=old,
    )
    note_processing_card(
        db,
        message_name="spaces/AAQA/messages/old-approved",
        confirmation_id=approved_id,
        started_at=old,
    )
    note_processing_card(
        db,
        message_name="spaces/AAQA/messages/fail-once",
        confirmation_id=pending_id,
        started_at=old,
    )
    note_processing_card(
        db,
        message_name="spaces/AAQA/messages/fresh",
        confirmation_id=pending_id,
        started_at=fresh,
    )
    assert POLICY_CHANGE_ENABLED is False

    patches: list[tuple[str, dict]] = []

    def flaky(message_name, body):
        if message_name.endswith("fail-once"):
            raise RuntimeError("chat down")
        patches.append((message_name, body))

    cleared = sweep_stuck_processing_cards(
        db, flaky, now=now, older_than_minutes=8
    )
    cleared_names = {item["message_name"] for item in cleared}
    assert cleared_names == {
        "spaces/AAQA/messages/old-pending",
        "spaces/AAQA/messages/old-approved",
    }
    by_name = {name: body for name, body in patches}
    assert by_name["spaces/AAQA/messages/old-pending"] == {
        "text": f"✓ {INACTIVE}",
        "cardsV2": [],
    }
    assert by_name["spaces/AAQA/messages/old-approved"]["text"] == (
        f"✓ {ALREADY_APPROVED}"
    )
    assert by_name["spaces/AAQA/messages/old-approved"]["cardsV2"] == []
    assert "Processing" not in by_name["spaces/AAQA/messages/old-pending"]["text"]
    # No terminal decision was invented for the card that was still waiting.
    assert confirmations.get(pending_id, store=store)["status"] == "PENDING"
    assert confirmations.get(approved_id, store=store)["status"] == "APPROVED"
    assert _open_rows(db) == [
        "spaces/AAQA/messages/fail-once",
        "spaces/AAQA/messages/fresh",
    ]

    # A card one minute old is not due at the default age, even via env.
    monkeypatch.setenv("ROBIE_STUCK_PROCESSING_CARD_MINUTES", "8")
    assert [item["message_name"] for item in stuck_processing_actions(db, now=now)] == [
        "spaces/AAQA/messages/fail-once",
    ]

    retry: list[tuple[str, dict]] = []
    again = sweep_stuck_processing_cards(
        db, lambda name, body: retry.append((name, body)), now=now
    )
    assert [item["message_name"] for item in again] == [
        "spaces/AAQA/messages/fail-once",
    ]
    assert retry[0][1]["text"] == f"✓ {INACTIVE}"
    assert retry[0][1]["cardsV2"] == []
    assert _open_rows(db) == ["spaces/AAQA/messages/fresh"]
    assert confirmations.get(pending_id, store=store)["status"] == "PENDING"


def test_scheduler_sweep_skips_chat_when_nothing_is_due(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    JobStore(db)

    def boom(*_args, **_kwargs):
        raise AssertionError("Chat should stay closed when no card is due")

    monkeypatch.setattr(
        "robie_job_engine.chat_app_post.patch_message_as_chat_app", boom
    )
    assert run_stuck_processing_sweep(db) == 0


def test_scheduler_sweep_patches_a_due_card(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    store = JobStore(db)
    cid = _request(store)
    note_processing_card(
        db,
        message_name="spaces/AAQA/messages/due",
        confirmation_id=cid,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=9),
    )
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "robie_job_engine.chat_app_post.patch_message_as_chat_app",
        lambda name, body: calls.append((name, body)),
    )
    assert run_stuck_processing_sweep(db) == 1
    assert calls[0][0] == "spaces/AAQA/messages/due"
    assert calls[0][1]["cardsV2"] == []
    assert calls[0][1]["text"] == f"✓ {INACTIVE}"
    assert confirmations.get(cid, store=store)["status"] == "PENDING"
    assert _open_rows(db) == []
