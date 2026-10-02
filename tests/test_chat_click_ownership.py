"""Two gateways see one click; only the owner patches the card.

Prod and Test share hermes-chat-topic. A confirmation click must update
the card only on the gateway whose own database contains that id. An
click routed to a gateway that cannot act on it (unknown action,
unreadable token, confirmation id not in its database) is patched to
"This card is no longer active." because the bridge already replaced the
buttons with a processing card. A click stamped for the other environment
stays silent: that environment's subscription receives and patches it.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import sys
import types
import typing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import confirmation_notify as notify
from robie_job_engine import confirmations
from robie_job_engine.store import JobStore


def _load_adapter_card_handlers():
    """Load the card-click handlers without importing the gateway package.

    ``integrations.google_chat.adapter`` imports ``gateway`` at module
    level, which is not installed in this tree. These functions are pure
    enough to exec from the shipped source.
    """
    adapter_path = Path(__file__).resolve().parent.parent / "integrations/google_chat/adapter.py"
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    wanted = {
        "_gateway_job_db_path",
        "_confirmation_owned_by_gateway",
        "_click_routed_to_this_gateway",
        "_decision_owned_by_gateway",
        "_card_parameters",
        "_card_event_payload",
        "_card_form_text",
        "_handle_card_event",
        "dispatch_http_event",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "GoogleChatAdapter":
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name in wanted:
                    nodes.append(item)
    missing = wanted - {node.name for node in nodes}
    if missing:
        raise AssertionError(f"adapter card handlers not found: {sorted(missing)}")
    namespace = {
        "Any": typing.Any,
        "Dict": typing.Dict,
        "Optional": typing.Optional,
        "Tuple": typing.Tuple,
        "logger": logging.getLogger("gateway.platforms.google_chat"),
        "os": os,
    }
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(adapter_path), "exec"), namespace)
    return namespace


TEST_KEY = "test-decision-signing-key-0123456789abcdef"
MESSAGE = "spaces/AAQA/messages/clicked-card"


def _request(store):
    return confirmations.request_confirmation(
        store=store,
        loop_job_id="loop-1",
        job_type="policy_change",
        draft_summary="Raise written premium",
        changes_json={"policy_number": "HO-1"},
        requested_by="robie",
        origin_platform="chat",
        origin_ref={"space": "spaces/AAQA", "thread": "spaces/AAQA/threads/T"},
    )


def _principal():
    return notify.approver_principal()


class _Gateway:
    def __init__(self, db_path: str):
        self._job_db_path = db_path
        self._clarify_state: dict = {}
        self.patches: list = []
        self.creates: list = []

    async def _patch_message(self, name, body):
        self.patches.append((name, body))

    async def _create_message(self, chat_id, body):
        self.creates.append((chat_id, body))


def _bind(gateway: _Gateway):
    handlers = _load_adapter_card_handlers()
    gateway._handle_card_event = types.MethodType(handlers["_handle_card_event"], gateway)
    gateway.dispatch_http_event = types.MethodType(handlers["dispatch_http_event"], gateway)
    return gateway


def _click(token: str, action: str = "robie_confirmation_decision") -> dict:
    return {
        "chat": {
            "user": {"email": _principal(), "type": "HUMAN"},
            "message": {
                "name": MESSAGE,
                "space": {"name": "spaces/AAQA"},
            },
            "space": {"name": "spaces/AAQA"},
            "buttonClickedPayload": {
                "action": {"actionMethodName": action},
            },
        },
        "commonEventObject": {
            "invokedFunction": action,
            "parameters": {"decision_token": token, "robie_env": "test"},
        },
        "user": {"name": "users/bot", "type": "BOT"},
    }


def test_peek_confirmation_id_ignores_signature():
    cid = "cid-foreign-12345678"
    token = confirmations.mint_decision_token(
        cid, "REJECT", "approver@example.com", key=TEST_KEY
    )
    forged = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    assert confirmations.peek_confirmation_id(token) == cid
    assert confirmations.peek_confirmation_id(forged) == cid
    assert confirmations.peek_confirmation_id("not-a-token") == ""
    assert confirmations.peek_confirmation_id("") == ""


def test_only_owning_gateway_patches_the_shared_click(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ROBIE_DECISION_SIGNING_KEY", TEST_KEY)
    owner_db = str(tmp_path / "owner.db")
    foreign_db = str(tmp_path / "foreign.db")
    owner_store = JobStore(owner_db)
    JobStore(foreign_db)
    cid = _request(owner_store)
    token = confirmations.mint_decision_token(cid, "REJECT", _principal(), key=TEST_KEY)
    envelope = _click(token)

    owner = _bind(_Gateway(owner_db))
    foreign = _bind(_Gateway(foreign_db))

    # The click is stamped robie_env=test; the foreign gateway is Prod.
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    with caplog.at_level("INFO", logger="gateway.platforms.google_chat"):
        foreign_result = asyncio.run(
            foreign._handle_card_event(envelope, notify=True)
        )
    assert foreign_result is None
    assert foreign.patches == []
    assert foreign.creates == []
    foreign_conn = __import__("sqlite3").connect(foreign_db)
    try:
        assert foreign_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plan_confirmations'"
        ).fetchone() is None
    finally:
        foreign_conn.close()
    assert confirmations.get(cid, store=owner_store)["status"] == "PENDING"
    assert any(
        "confirmation click not owned here" in record.getMessage()
        and f"ref={cid[:8]}" in record.getMessage()
        for record in caplog.records
    )
    assert token not in "\n".join(record.getMessage() for record in caplog.records)

    monkeypatch.setenv("ROBIE_ENV", "TEST")
    owner_result = asyncio.run(owner._handle_card_event(envelope, notify=True))
    assert owner_result
    assert confirmations.get(cid, store=owner_store)["status"] == "REJECTED"
    assert len(owner.patches) == 1
    assert owner.patches[0][0] == MESSAGE
    assert owner.patches[0][1]["cardsV2"] == []
    assert owner.creates == []
    assert "not supported" not in owner.patches[0][1]["text"].lower()


def test_foreign_env_unknown_card_action_does_not_patch_or_reply(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    gateway = _bind(_Gateway(str(tmp_path / "jobs.db")))
    envelope = _click("rbd1.unused", action="not_a_real_action")
    with caplog.at_level("INFO", logger="gateway.platforms.google_chat"):
        result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
        http = asyncio.run(gateway.dispatch_http_event(envelope))
    assert result is None
    assert http == {}
    assert gateway.patches == []
    assert gateway.creates == []
    assert any(
        "unknown card action ignored" in record.getMessage()
        and "action=not_a_real_action" in record.getMessage()
        for record in caplog.records
    )


def _card(action, **parameters):
    return {
        "type": "CARD_CLICKED",
        "common": {"invokedFunction": action, "parameters": parameters},
        "user": {"email": "carlo@streetsmart.insurance", "type": "HUMAN"},
        "message": {"name": MESSAGE, "space": {"name": "spaces/AAQA"}},
        "space": {"name": "spaces/AAQA"},
    }


def test_known_clarify_action_still_patches(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    gateway = _bind(_Gateway(str(tmp_path / "jobs.db")))
    gateway._clarify_state["clarify-local-1"] = "session-1"
    envelope = _card("hermes_clarify", clarify_id="clarify-local-1", robie_env="test")
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert result == "That question has expired. Please ask ROBIE again."
    assert len(gateway.patches) == 1
    assert gateway.patches[0][0] == MESSAGE
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    assert http["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert http["cardsV2"] == []


def test_own_expired_clarify_replies_expired(tmp_path, monkeypatch):
    """Clarify state expires after 120s. Our own late click still replies."""
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    gateway = _bind(_Gateway(str(tmp_path / "jobs.db")))
    envelope = _card(
        "hermes_clarify",
        clarify_id="expired-clarify",
        choice="yes",
        robie_env="test",
    )
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert result == "That question has expired. Please ask ROBIE again."
    assert len(gateway.patches) == 1
    assert "expired" in gateway.patches[0][1]["text"].lower()


def test_foreign_env_clarify_and_decision_are_silent(tmp_path, monkeypatch, caplog):
    import sqlite3

    monkeypatch.setenv("ROBIE_ENV", "TEST")
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    before = bare.read_bytes()
    gateway = _bind(_Gateway(str(bare)))
    clarify = _card(
        "hermes_clarify",
        clarify_id="foreign-clarify",
        choice="yes",
        robie_env="prod",
    )
    decision = _card(
        "robie_decision",
        decision_id="foreign-decision",
        choice="approve",
        robie_env="prod",
    )
    with caplog.at_level("INFO", logger="gateway.platforms.google_chat"):
        clarify_result = asyncio.run(gateway._handle_card_event(clarify, notify=True))
        decision_result = asyncio.run(gateway._handle_card_event(decision, notify=True))
        clarify_http = asyncio.run(gateway.dispatch_http_event(clarify))
        decision_http = asyncio.run(gateway.dispatch_http_event(decision))
    assert clarify_result is None
    assert decision_result is None
    assert clarify_http == {}
    assert decision_http == {}
    assert gateway.patches == []
    assert gateway.creates == []
    assert bare.read_bytes() == before
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "clarify click not owned here" in messages
    assert "ref=foreign-" in messages
    assert "decision click not owned here" in messages


def test_missing_robie_env_on_prod_is_treated_as_own(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    gateway = _bind(_Gateway(str(tmp_path / "jobs.db")))
    envelope = _card("hermes_clarify", clarify_id="untagged-clarify", choice="yes")
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert result == "That question has expired. Please ask ROBIE again."
    assert gateway.patches

    monkeypatch.setenv("ROBIE_ENV", "TEST")
    test_gateway = _bind(_Gateway(str(tmp_path / "test.db")))
    silent = asyncio.run(test_gateway._handle_card_event(envelope, notify=True))
    assert silent is None
    assert test_gateway.patches == []


def test_matching_env_decision_without_a_row_still_replies(tmp_path, monkeypatch):
    """Id missing locally, but robie_env is ours: do not ack silently."""
    import sqlite3

    monkeypatch.setenv("ROBIE_ENV", "TEST")
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    gateway = _bind(_Gateway(str(db)))
    envelope = _card(
        "robie_decision",
        decision_id="missing-decision",
        choice="approve",
        robie_env="test",
    )
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert result
    assert result != "That action is not supported."
    assert gateway.patches
    assert "not supported" not in gateway.patches[0][1]["text"].lower()


def test_owned_decision_click_still_replies(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    import sqlite3

    db = tmp_path / "owner.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE decisions (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO decisions (id) VALUES ('owned-decision')")
    conn.commit()
    conn.close()
    gateway = _bind(_Gateway(str(db)))
    envelope = {
        "type": "CARD_CLICKED",
        "common": {
            "invokedFunction": "robie_decision",
            "parameters": {"decision_id": "owned-decision", "choice": "approve"},
        },
        "user": {"email": "carlo@streetsmart.insurance", "type": "HUMAN"},
        "message": {"name": MESSAGE, "space": {"name": "spaces/AAQA"}},
        "space": {"name": "spaces/AAQA"},
    }
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert result
    assert gateway.patches


def test_confirmation_lookup_does_not_create_a_missing_table(tmp_path):
    import sqlite3

    missing = tmp_path / "missing" / "jobs.db"
    assert confirmations.has_confirmation(str(missing), "cid-1") is False
    assert not missing.exists()

    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    before = bare.read_bytes()
    assert confirmations.has_confirmation(str(bare), "cid-1") is False
    assert bare.read_bytes() == before
    conn = sqlite3.connect(bare)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'plan_confirmations'"
        ).fetchone() is None
    finally:
        conn.close()

    owned = tmp_path / "owned.db"
    store = JobStore(str(owned))
    cid = _request(store)
    assert confirmations.has_confirmation(str(owned), cid) is True
    assert confirmations.has_confirmation(str(owned), "someone-elses-id") is False


def test_adapter_buttons_stamp_routing_env(monkeypatch):
    adapter_path = Path(__file__).resolve().parent.parent / "integrations/google_chat/adapter.py"
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    wanted = {"_required_str", "_button_to_chat"}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {"Any": typing.Any, "Dict": typing.Dict, "os": os}
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(adapter_path), "exec"), namespace)
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    button = namespace["_button_to_chat"](
        {"text": "Yes", "action": "hermes_clarify", "parameters": {"choice": "a"}}
    )
    params = {item["key"]: item["value"] for item in button["onClick"]["action"]["parameters"]}
    assert params["choice"] == "a"
    assert params["robie_env"] == "test"
    explicit = namespace["_button_to_chat"](
        {
            "text": "Yes",
            "action": "hermes_clarify",
            "parameters": {"choice": "a", "robie_env": "prod"},
        }
    )
    explicit_params = {
        item["key"]: item["value"] for item in explicit["onClick"]["action"]["parameters"]
    }
    assert explicit_params["robie_env"] == "prod"
    monkeypatch.delenv("ROBIE_ENV", raising=False)
    untagged = namespace["_button_to_chat"](
        {"text": "Yes", "action": "hermes_clarify", "parameters": {"choice": "a"}}
    )
    assert "robie_env" not in {
        item["key"] for item in untagged["onClick"]["action"]["parameters"]
    }


def test_foreign_env_unreadable_confirmation_token_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    gateway = _bind(_Gateway(str(tmp_path / "jobs.db")))
    envelope = _click("not-a-decision-token")
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert result is None
    assert gateway.patches == []
    assert gateway.creates == []


INACTIVE = "This card is no longer active."


def _assert_patched_inactive(gateway, result, http):
    assert result == INACTIVE
    assert len(gateway.patches) == 1
    assert gateway.patches[0][0] == MESSAGE
    assert gateway.patches[0][1] == {"text": f"✓ {INACTIVE}", "cardsV2": []}
    assert gateway.creates == []
    assert http["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert http["text"] == f"✓ {INACTIVE}"


def _no_confirmations_table(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plan_confirmations'"
        ).fetchone() is None
    finally:
        conn.close()


def test_routed_unknown_card_action_patches_card_inactive(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    gateway = _bind(_Gateway(str(tmp_path / "jobs.db")))
    envelope = _click("rbd1.unused", action="not_a_real_action")
    with caplog.at_level("INFO", logger="gateway.platforms.google_chat"):
        result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    _assert_patched_inactive(gateway, result, http)
    assert any(
        "unknown card action routed here" in record.getMessage()
        for record in caplog.records
    )


def test_unstamped_unknown_card_action_on_prod_patches_card_inactive(tmp_path, monkeypatch):
    # No robie_env on the button is Prod's click, matching the Prod filter.
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    gateway = _bind(_Gateway(str(tmp_path / "jobs.db")))
    envelope = _card("not_a_real_action")
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    _assert_patched_inactive(gateway, result, http)


def test_routed_confirmation_not_in_db_patches_inactive_without_schema(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ROBIE_DECISION_SIGNING_KEY", TEST_KEY)
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    db = str(tmp_path / "jobs.db")
    __import__("sqlite3").connect(db).close()
    token = confirmations.mint_decision_token(
        "cid-gone-12345678", "REJECT", _principal(), key=TEST_KEY
    )
    gateway = _bind(_Gateway(db))
    envelope = _click(token)
    with caplog.at_level("INFO", logger="gateway.platforms.google_chat"):
        result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    _assert_patched_inactive(gateway, result, http)
    # The resolver never ran: no schema was created for a foreign id.
    assert _no_confirmations_table(db)
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "routed here but not found" in log_text
    assert "ref=cid-gone" in log_text
    assert token not in log_text


def test_routed_unreadable_confirmation_token_patches_inactive(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    db = str(tmp_path / "jobs.db")
    gateway = _bind(_Gateway(db))
    envelope = _click("not-a-decision-token")
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    _assert_patched_inactive(gateway, result, http)


def test_shared_click_gets_exactly_one_terminal_patch(tmp_path, monkeypatch):
    """Both gateways see a Test click; the owner patches, Prod stays silent."""
    monkeypatch.setenv("ROBIE_DECISION_SIGNING_KEY", TEST_KEY)
    test_db = str(tmp_path / "test.db")
    prod_db = str(tmp_path / "prod.db")
    test_store = JobStore(test_db)
    JobStore(prod_db)
    cid = _request(test_store)
    token = confirmations.mint_decision_token(cid, "REJECT", _principal(), key=TEST_KEY)
    unknown = _click(token, action="not_a_real_action")
    known = _click(token)

    prod = _bind(_Gateway(prod_db))
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    assert asyncio.run(prod._handle_card_event(known, notify=True)) is None
    assert asyncio.run(prod._handle_card_event(unknown, notify=True)) is None
    assert prod.patches == [] and prod.creates == []

    test = _bind(_Gateway(test_db))
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    assert asyncio.run(test._handle_card_event(known, notify=True)) != INACTIVE
    assert confirmations.get(cid, store=test_store)["status"] == "REJECTED"
    assert asyncio.run(test._handle_card_event(unknown, notify=True)) == INACTIVE
    assert len(test.patches) == 2
