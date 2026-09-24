"""Tests for confirmation_cards.py: Chat-native Approve/Reject buttons.

Acceptance: Carlo can Approve/Reject from Chat with no sheet paste; a
bare click with no valid token fails closed; token/auth stays fail-closed;
double-submit is safe; the answered card is replaced.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import confirmation_cards as cards
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
        requested_by="robie",
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/TTT"},
    )
    args.update(kw)
    return confirmations.request_confirmation(store=store, **args)


def _principal():
    return notify.approver_principal()


def _tokens(cid):
    return {
        "APPROVE": confirmations.mint_decision_token(cid, "APPROVE", _principal(), key=TEST_KEY),
        "REJECT": confirmations.mint_decision_token(cid, "REJECT", _principal(), key=TEST_KEY),
    }


def _click(token, actor=None, action="robie_confirmation_decision"):
    # Bridge-normalized envelope shape (common.invokedFunction from the
    # /actions/<name> URL, common.parameters as a dict).
    return {
        "common": {
            "invokedFunction": action,
            "parameters": {"decision_token": token} if token is not None else {},
        },
        "user": {"email": actor if actor is not None else _principal()},
    }


def test_card_buttons_carry_signed_tokens(db):
    store = JobStore(db)
    cid = _request(store)
    record = confirmations.get(cid, store=store)
    tokens = _tokens(cid)
    card = cards.approval_card_v2(record, tokens)
    assert card["cardId"] == f"robie-confirmation-{cid}"
    buttons = card["card"]["sections"][0]["widgets"][2]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["Approve", "Reject"]
    seen = set()
    for button, decision in zip(buttons, ("APPROVE", "REJECT")):
        params = button["onClick"]["action"]["parameters"]
        # Chat apps on an HTTP endpoint require the FULL action URL in
        # action.function. A bare function name is treated by Google as an
        # add-on deployment function and the click never reaches the bridge
        # (every real Approve/Reject click on 2026-09-23 failed this way).
        function = button["onClick"]["action"]["function"]
        assert function == cards._click_function()
        assert function.startswith("https://")
        assert function.endswith("/actions/" + cards.CARD_ACTION)
        assert "://" in function and function.count("/") > 2
        token = next(p["value"] for p in params if p["key"] == "decision_token")
        verified = confirmations.verify_decision_token(token, key=TEST_KEY)
        assert verified["confirmation_id"] == cid
        assert verified["decision"] == decision
        assert verified["principal"] == _principal()
        seen.add(decision)
    assert seen == {"APPROVE", "REJECT"}


def test_card_without_key_is_display_only(db):
    store = JobStore(db)
    cid = _request(store)
    record = confirmations.get(cid, store=store)
    card = cards.approval_card_v2(record, None)
    widgets = card["card"]["sections"][0]["widgets"]
    assert not any("buttonList" in w for w in widgets)
    assert any("display-only" in str(w.get("textParagraph", {}).get("text", "")) for w in widgets)


def test_approve_click_decides_without_sheet(db):
    store = JobStore(db)
    cid = _request(store)
    result = cards.resolve_confirmation_click(
        store, _click(_tokens(cid)["APPROVE"]), decision_key=TEST_KEY
    )
    assert result.status == "APPROVED"
    assert result.decided is True
    record = confirmations.get(cid, store=store)
    assert record["status"] == "APPROVED"
    assert record["decided_by"] == _principal()


def test_click_logs_short_ref_action_outcome_and_actor_domain(db, caplog):
    store = JobStore(db)
    cid = _request(store)
    principal = _principal()
    domain = principal.split("@", 1)[1]
    with caplog.at_level("INFO", logger="robie_job_engine.confirmation_cards"):
        result = cards.resolve_confirmation_click(
            store, _click(_tokens(cid)["APPROVE"]), decision_key=TEST_KEY
        )
    assert result.status == "APPROVED"
    line = next(r.getMessage() for r in caplog.records if "confirmation_cards.click" in r.getMessage())
    assert f"ref={cid[:8]}" in line
    assert cid not in line
    assert "action=APPROVE" in line
    assert "click.status=APPROVED" in line
    assert f"actor_domain={domain}" in line
    assert principal not in line
    assert "decision_token" not in line


def test_reject_click_decides_without_sheet(db):
    store = JobStore(db)
    cid = _request(store)
    result = cards.resolve_confirmation_click(
        store, _click(_tokens(cid)["REJECT"]), decision_key=TEST_KEY
    )
    assert result.status == "REJECTED"
    assert result.decided is True
    assert confirmations.get(cid, store=store)["status"] == "REJECTED"


def test_bare_click_with_no_token_fails_closed(db, caplog):
    store = JobStore(db)
    cid = _request(store)
    with caplog.at_level("INFO", logger="robie_job_engine.confirmation_cards"):
        result = cards.resolve_confirmation_click(store, _click(None), decision_key=TEST_KEY)
    assert result.status == "INVALID"
    assert result.decided is False
    assert confirmations.get(cid, store=store)["status"] == "PENDING"
    assert any("click.status=INVALID" in r.getMessage() for r in caplog.records)


def test_forged_token_fails_closed(db):
    store = JobStore(db)
    cid = _request(store)
    good = _tokens(cid)["APPROVE"]
    forged = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    result = cards.resolve_confirmation_click(store, _click(forged), decision_key=TEST_KEY)
    assert result.status == "INVALID_TOKEN"
    assert result.decided is False
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_expired_token_fails_closed(db):
    store = JobStore(db)
    cid = _request(store)
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    token = confirmations.mint_decision_token(
        cid, "APPROVE", _principal(), key=TEST_KEY, now=past, ttl_seconds=3600
    )
    result = cards.resolve_confirmation_click(store, _click(token), decision_key=TEST_KEY)
    assert result.status == "EXPIRED_TOKEN"
    assert result.decided is False
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_token_for_other_confirmation_fails_closed(db):
    store = JobStore(db)
    cid = _request(store)
    other = _request(store, loop_job_id="loop-2")
    other_token = confirmations.mint_decision_token(
        other, "APPROVE", _principal(), key=TEST_KEY
    )
    result = cards.resolve_confirmation_click(store, _click(other_token), decision_key=TEST_KEY)
    # Token is valid but bound to a different confirmation: it decides the
    # OTHER confirmation, never this one.
    assert result.status == "APPROVED"
    assert result.confirmation_id == other
    assert confirmations.get(cid, store=store)["status"] == "PENDING"
    assert confirmations.get(other, store=store)["status"] == "APPROVED"


def test_click_by_wrong_actor_fails_closed(db):
    store = JobStore(db)
    cid = _request(store)
    result = cards.resolve_confirmation_click(
        store,
        _click(_tokens(cid)["APPROVE"], actor="mallory@example.com"),
        decision_key=TEST_KEY,
    )
    assert result.status == "UNAUTHORIZED"
    assert result.decided is False
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_double_click_is_safe(db):
    store = JobStore(db)
    cid = _request(store)
    payload = _click(_tokens(cid)["APPROVE"])
    first = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    second = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert first.status == "APPROVED"
    assert second.status == "ALREADY_DECIDED"
    assert second.decided is False
    record = confirmations.get(cid, store=store)
    assert record["status"] == "APPROVED"
    assert record["decided_by"] == _principal()


def test_click_after_reject_is_safe(db):
    store = JobStore(db)
    cid = _request(store)
    tokens = _tokens(cid)
    first = cards.resolve_confirmation_click(
        store, _click(tokens["REJECT"]), decision_key=TEST_KEY
    )
    # Approve click arriving after the reject: must not flip the decision.
    second = cards.resolve_confirmation_click(
        store, _click(tokens["APPROVE"]), decision_key=TEST_KEY
    )
    assert first.status == "REJECTED"
    assert second.status == "ALREADY_DECIDED"
    assert confirmations.get(cid, store=store)["status"] == "REJECTED"


def test_no_signing_key_fails_closed(db, monkeypatch):
    monkeypatch.delenv("ROBIE_DECISION_SIGNING_KEY", raising=False)
    store = JobStore(db)
    cid = _request(store)
    token = confirmations.mint_decision_token(cid, "APPROVE", _principal(), key=TEST_KEY)
    result = cards.resolve_confirmation_click(store, _click(token), decision_key=None)
    assert result.status == "NO_KEY"
    assert result.decided is False
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_unknown_confirmation_fails_closed(db):
    store = JobStore(db)
    token = confirmations.mint_decision_token(
        "no-such-confirmation", "APPROVE", _principal(), key=TEST_KEY
    )
    result = cards.resolve_confirmation_click(store, _click(token), decision_key=TEST_KEY)
    assert result.status == "INVALID"
    assert result.decided is False


def test_wrong_action_name_rejected(db):
    store = JobStore(db)
    cid = _request(store)
    payload = _click(_tokens(cid)["APPROVE"], action="robie_decision")
    result = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert result.status == "INVALID"
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_raw_chat_api_parameter_shape_parses(db):
    """The un-normalized Chat API envelope (action.parameters list) works."""
    store = JobStore(db)
    cid = _request(store)
    token = _tokens(cid)["APPROVE"]
    payload = {
        "action": {
            "actionMethodName": "robie_confirmation_decision",
            "parameters": [{"key": "decision_token", "value": token}],
        },
        "user": {"email": _principal()},
    }
    result = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert result.status == "APPROVED"
    assert confirmations.get(cid, store=store)["status"] == "APPROVED"


_LIVE_BRIDGE_URL = (
    "https://robie-chat-http-bridge-751771086524.us-east1.run.app"
    "/actions/robie_confirmation_decision"
)


def test_click_function_is_full_bridge_action_url():
    # Regression: the bare action name is treated by Google as an add-on
    # deployment function and the click never reaches the bridge. The
    # rendered card must carry the full HTTPS action URL.
    function = cards._click_function()
    assert function.startswith("https://")
    assert function.endswith("/actions/" + cards.CARD_ACTION)
    # The click still resolves: the bridge echoes the bare name as
    # common.invokedFunction and the envelope carries the full URL.
    assert cards.canonical_card_action(function) == cards.CARD_ACTION


def test_canonical_action_accepts_short_name_and_bridge_url():
    assert cards.canonical_card_action(cards.CARD_ACTION) == cards.CARD_ACTION
    assert cards.canonical_card_action(_LIVE_BRIDGE_URL) == cards.CARD_ACTION
    assert cards.canonical_card_action(_LIVE_BRIDGE_URL + "?x=1") == cards.CARD_ACTION
    assert cards.canonical_card_action("/actions/robie_confirmation_decision") == cards.CARD_ACTION
    # Unrelated actions, including a lookalike suffix, stay untouched.
    assert cards.canonical_card_action("robie_decision") == "robie_decision"
    assert cards.canonical_card_action(
        "https://example.test/actions/robie_decision"
    ) == "https://example.test/actions/robie_decision"
    assert cards.canonical_card_action("not_robie_confirmation_decision") == (
        "not_robie_confirmation_decision"
    )


def test_google_card_clicked_short_function_approves(db):
    """Shape Google sends when action.function is the short Chat-app name."""
    store = JobStore(db)
    cid = _request(store)
    token = _tokens(cid)["APPROVE"]
    payload = {
        "type": "CARD_CLICKED",
        "space": {"name": "spaces/AAQAZbLJO78"},
        "message": {"name": "spaces/AAQAZbLJO78/messages/1"},
        "user": {"email": _principal(), "type": "HUMAN"},
        "action": {
            "actionMethodName": cards.CARD_ACTION,
            "parameters": [{"key": "decision_token", "value": token}],
        },
        "common": {
            "hostApp": "CHAT",
            "invokedFunction": cards.CARD_ACTION,
            "parameters": {"decision_token": token},
        },
    }
    result = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert result.status == "APPROVED"
    assert confirmations.get(cid, store=store)["status"] == "APPROVED"


def test_google_card_clicked_bridge_url_function_approves(db):
    """The live card stored a full URL in action.function. Chat echoes it."""
    store = JobStore(db)
    cid = _request(store)
    token = _tokens(cid)["REJECT"]
    payload = {
        "type": "CARD_CLICKED",
        "user": {"email": _principal(), "type": "HUMAN"},
        "action": {
            "actionMethodName": _LIVE_BRIDGE_URL,
            "parameters": [{"key": "decision_token", "value": token}],
        },
        "common": {
            "invokedFunction": _LIVE_BRIDGE_URL,
            "parameters": {"decision_token": token},
        },
    }
    result = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert result.status == "REJECTED"
    assert confirmations.get(cid, store=store)["status"] == "REJECTED"


def test_unrelated_url_function_is_not_a_confirmation_click(db):
    store = JobStore(db)
    cid = _request(store)
    payload = _click(
        _tokens(cid)["APPROVE"],
        action="https://example.test/actions/robie_decision",
    )
    result = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert result.status == "INVALID"
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_adapter_folds_bridge_url_before_unsupported_branch():
    """Live failure: the adapter compared the raw URL and returned
    "That action is not supported." before the parser could accept it.
    The hermes ``gateway`` package is not in this tree, so lock the
    dispatch order from the adapter source.
    """
    adapter_path = Path(__file__).resolve().parent.parent / "integrations/google_chat/adapter.py"
    handle = adapter_path.read_text(encoding="utf-8").split(
        "async def _handle_card_event", 1
    )[1].split("async def dispatch_http_event", 1)[0]
    canon_at = handle.index("canonical_card_action")
    branch_at = handle.index('action == "robie_confirmation_decision"')
    unsupported_at = handle.index("That action is not supported.")
    assert canon_at < branch_at < unsupported_at
    update = adapter_path.read_text(encoding="utf-8").split(
        "async def dispatch_http_event", 1
    )[1].split("async def ", 1)[0]
    assert '"type": "UPDATE_MESSAGE"' in update
    assert '"cardsV2": []' in update


def test_click_response_replaces_card():
    result = cards.ConfirmationClickResult(
        status="APPROVED", decided=True, confirmation_id="cid-1", message="Approved."
    )
    response = cards.confirmation_click_response(result)
    assert response["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert response["text"] == "Approved."
    assert response["cardsV2"] == []


def test_addon_clicker_not_shadowed_by_message_sender(db):
    """Regression (live 2026-09-23): a Workspace Add-ons card click arrives
    with the clicking user at ``chat.user``. ``commonEventObject`` carries
    no ``user`` key for this app, and the bridge's top-level ``user`` is the
    message sender -- the bot that posted the card. The old actor extraction
    read the top-level sender and refused the approver's Approve as
    UNAUTHORIZED ("bound to a different approver"). The click must resolve
    against the true clicker.
    """
    store = JobStore(db)
    cid = _request(store)
    token = _tokens(cid)["APPROVE"]
    payload = {
        # Raw bridge shape: clicker at chat.user, no commonEventObject.user.
        "chat": {
            "user": {
                "email": _principal(),
                "displayName": "Carlo Ferrara",
                "type": "HUMAN",
            },
        },
        "common": {
            "invokedFunction": "robie_confirmation_decision",
            "parameters": {"decision_token": token},
        },
        # Bridge message-sender fallback: the bot that posted the card.
        "user": {"name": "users/112266247562475398062", "type": "BOT"},
    }
    result = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert result.status == "APPROVED"
    assert result.decided is True
    record = confirmations.get(cid, store=store)
    assert record["status"] == "APPROVED"
    assert record["decided_by"] == _principal()


def test_adapter_surfaced_clicker_resolves(db):
    """The adapter-normalized shape: the human clicker surfaced at the
    top-level ``user`` (from ``chat.user``), with an empty ``common.user``.
    Must still approve.
    """
    store = JobStore(db)
    cid = _request(store)
    token = _tokens(cid)["REJECT"]
    payload = {
        "common": {
            "invokedFunction": "robie_confirmation_decision",
            "parameters": {"decision_token": token},
            "user": {},
        },
        "user": {"email": _principal(), "type": "HUMAN"},
    }
    result = cards.resolve_confirmation_click(store, payload, decision_key=TEST_KEY)
    assert result.status == "REJECTED"
    assert result.decided is True
    assert confirmations.get(cid, store=store)["status"] == "REJECTED"


def _load_card_event_payload():
    """Load the adapter's real ``_card_event_payload`` in isolation.

    The adapter module has heavy gateway imports; this function is pure
    (typing only), so exec its actual source from the repo tree. This
    tests the shipped code, not a copy.
    """
    import ast
    from pathlib import Path

    adapter_path = Path(__file__).resolve().parent.parent / "integrations/google_chat/adapter.py"
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_card_event_payload":
            ns = {"Dict": dict, "Any": object, "Optional": __import__("typing").Optional}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(adapter_path), "exec"), ns)
            return ns["_card_event_payload"]
    raise AssertionError("_card_event_payload not found in adapter")


def test_card_event_payload_surfaces_chat_user_as_clicker():
    """Direct adapter regression (live 2026-09-23): a Workspace Add-ons
    CARD_CLICKED envelope carries the true clicking human at ``chat.user``.
    ``commonEventObject`` has no user for this app, and the envelope's
    top-level ``user`` is the message sender (the bot that posted the card).
    ``_card_event_payload()`` must surface the human clicker as the
    normalized payload's ``user`` so actor extraction authorizes the right
    person instead of the bot.
    """
    _card_event_payload = _load_card_event_payload()
    envelope = {
        # Workspace Add-ons style: clicker lives under chat.user
        "chat": {
            "user": {
                "displayName": "Carlo Ferrara",
                "email": "carlo@streetsmart.insurance",
                "type": "HUMAN",
            },
            "buttonClickedPayload": {
                "action": {"actionMethodName": "robie_confirmation_decision"},
            },
        },
        # No user here for this app (live shape)
        "commonEventObject": {
            "invokedFunction": "robie_confirmation_decision",
            "parameters": {"decision_token": "rbd1.test"},
        },
        "common": {},
        # Message sender: the bot, NOT the clicker
        "user": {"name": "users/112266247562475398062", "type": "BOT"},
    }
    payload = _card_event_payload(envelope)
    assert payload is not None
    assert payload["user"]["email"] == "carlo@streetsmart.insurance"
    assert payload["user"]["type"] == "HUMAN"
    # The bot sender must not shadow the human clicker.
    assert payload["user"]["type"] != "BOT"


def test_card_event_payload_ignores_non_human_chat_user():
    """A non-HUMAN chat.user (e.g. BOT) must not replace the payload user."""
    _card_event_payload = _load_card_event_payload()
    envelope = {
        "chat": {
            "user": {"type": "BOT", "name": "users/112266247562475398062"},
            "buttonClickedPayload": {
                "action": {"actionMethodName": "robie_confirmation_decision"},
            },
        },
        "commonEventObject": {
            "invokedFunction": "robie_confirmation_decision",
            "parameters": {},
        },
        "common": {},
        "user": {"email": "carlo@streetsmart.insurance", "type": "HUMAN"},
    }
    payload = _card_event_payload(envelope)
    assert payload is not None
    assert payload["user"]["email"] == "carlo@streetsmart.insurance"


def test_card_event_payload_surfaces_chat_message_for_inplace_patch():
    """Direct adapter regression (live 2026-09-23): a Workspace Add-ons
    CARD_CLICKED envelope has NO top-level "message" key -- the clicked
    message lives at ``chat.message`` (keys are authorizationEventObject,
    chat, commonEventObject). ``_card_event_payload()`` must surface it as
    the normalized payload's ``message`` so the async gateway path can
    patch the answered card in place via messages.patch. Without this,
    message_name is empty, _patch_message is skipped, and the user gets a
    separate acknowledgement while the card keeps live Approve/Reject buttons.
    """
    _card_event_payload = _load_card_event_payload()
    envelope = {
        "chat": {
            "user": {
                "displayName": "Carlo Ferrara",
                "email": "carlo@streetsmart.insurance",
                "type": "HUMAN",
            },
            "message": {
                "name": "spaces/AAQAZbLJO78/messages/gHMvZiUpF5A.gHMvZiUpF5A",
                "space": {"name": "spaces/AAQAZbLJO78"},
            },
            "space": {"name": "spaces/AAQAZbLJO78"},
            "buttonClickedPayload": {
                "action": {"actionMethodName": "robie_confirmation_decision"},
            },
        },
        "commonEventObject": {
            "invokedFunction": "robie_confirmation_decision",
            "parameters": {},
        },
        "authorizationEventObject": {},
        # No top-level "message", "space", or "user" keys (live shape).
    }
    payload = _card_event_payload(envelope)
    assert payload is not None
    # Message identity must be surfaced for the in-place patch.
    assert payload["message"]["name"] == (
        "spaces/AAQAZbLJO78/messages/gHMvZiUpF5A.gHMvZiUpF5A"
    )
    # Space must be surfaced for the fallback separate-message path.
    assert payload["space"]["name"] == "spaces/AAQAZbLJO78"
    # The human clicker must still be surfaced (PR #555 behavior preserved).
    assert payload["user"]["email"] == "carlo@streetsmart.insurance"
    assert payload["user"]["type"] == "HUMAN"


def test_card_event_payload_prefers_top_level_message():
    """A top-level "message" (legacy Chat-app shape) wins over chat.message."""
    _card_event_payload = _load_card_event_payload()
    envelope = {
        "chat": {
            "message": {"name": "spaces/AAA/messages/chat-nested"},
            "buttonClickedPayload": {
                "action": {"actionMethodName": "robie_confirmation_decision"},
            },
        },
        "message": {"name": "spaces/AAA/messages/top-level"},
        "commonEventObject": {"invokedFunction": "robie_confirmation_decision"},
    }
    payload = _card_event_payload(envelope)
    assert payload is not None
    assert payload["message"]["name"] == "spaces/AAA/messages/top-level"
