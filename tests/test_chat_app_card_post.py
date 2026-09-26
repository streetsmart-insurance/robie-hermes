"""Tests for chat_app_post.post_card_as_chat_app (Chat-native cardsV2 post).

The module does not import googleapiclient at module level, so these run
without the Google client libraries by injecting a fake client.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import chat_app_post


class FakeMessages:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class Exec:
            def execute(self_inner):
                return {
                    "name": "spaces/AAA/messages/9",
                    "thread": {"name": "spaces/AAA/threads/TTT"},
                }

        return Exec()


class FakeSpaces:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeClient:
    def __init__(self, messages):
        self._spaces = FakeSpaces(messages)

    def spaces(self):
        return self._spaces


def _card():
    return [{"cardId": "robie-confirmation-cid-1", "card": {"header": {"title": "t"}}}]


def test_post_card_uses_cardsv2_and_originating_thread():
    messages = FakeMessages()
    receipt = chat_app_post.post_card_as_chat_app(
        "spaces/AAA",
        _card(),
        thread_name="spaces/AAA/threads/TTT",
        chat=FakeClient(messages),
    )
    assert receipt == {
        "name": "spaces/AAA/messages/9",
        "thread": "spaces/AAA/threads/TTT",
    }
    (call,) = messages.calls
    assert call["parent"] == "spaces/AAA"
    assert call["body"]["cardsV2"] == _card()
    assert call["body"]["thread"] == {"name": "spaces/AAA/threads/TTT"}
    assert call["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def test_post_card_without_thread_omits_reply_option():
    messages = FakeMessages()
    chat_app_post.post_card_as_chat_app("spaces/AAA", _card(), chat=FakeClient(messages))
    (call,) = messages.calls
    assert "thread" not in call["body"]
    assert "messageReplyOption" not in call


def test_post_card_rejects_non_space_parent():
    with pytest.raises(ValueError, match="existing space"):
        chat_app_post.post_card_as_chat_app(
            "threads/TTT", _card(), chat=FakeClient(FakeMessages())
        )


def test_post_card_rejects_empty_cards():
    with pytest.raises(ValueError, match="cardsV2 must be a non-empty list"):
        chat_app_post.post_card_as_chat_app(
            "spaces/AAA", [], chat=FakeClient(FakeMessages())
        )


def test_post_card_without_key_file_fails_closed(monkeypatch):
    # _chat_app_client imports googleapiclient at call time; stub the module
    # so we exercise the key-file check, not the local import. On deployed
    # boxes the library is installed and this path raises ChatAppIdentityError.
    import types

    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *a, **k: None
    pkg = types.ModuleType("googleapiclient")
    pkg.discovery = discovery
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.delenv("ROBIE_CHAT_SA_KEY_FILE", raising=False)
    monkeypatch.delenv("ROBIE_CHAT_APP_CLIENT_EMAIL", raising=False)
    with pytest.raises(chat_app_post.ChatAppIdentityError):
        chat_app_post.post_card_as_chat_app("spaces/AAA", _card())
