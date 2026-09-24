"""Unknown Google Chat card actions ack without a patch or a reply.

The adapter module imports gateway, which is not installed in this tree,
so the handlers are executed from the shipped source.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import types
import typing
from pathlib import Path


MESSAGE = "spaces/AAQA/messages/clicked-card"


def _handlers():
    adapter_path = Path(__file__).resolve().parent.parent / "integrations/google_chat/adapter.py"
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    wanted = {
        "_card_event_payload",
        "_card_parameters",
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
        raise AssertionError(f"adapter handlers not found: {sorted(missing)}")
    namespace = {
        "Any": typing.Any,
        "Dict": typing.Dict,
        "Optional": typing.Optional,
        "logger": logging.getLogger("gateway.platforms.google_chat"),
        "os": os,
    }
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(adapter_path), "exec"), namespace)
    return namespace


class _Gateway:
    def __init__(self):
        self._clarify_state = {}
        self.patches = []
        self.creates = []

    async def _patch_message(self, name, body):
        self.patches.append((name, body))

    async def _create_message(self, chat_id, body):
        self.creates.append((chat_id, body))


def _bind():
    handlers = _handlers()
    gateway = _Gateway()
    gateway._handle_card_event = types.MethodType(handlers["_handle_card_event"], gateway)
    gateway.dispatch_http_event = types.MethodType(handlers["dispatch_http_event"], gateway)
    return gateway


def _envelope(action, **parameters):
    return {
        "type": "CARD_CLICKED",
        "common": {"invokedFunction": action, "parameters": parameters},
        "message": {"name": MESSAGE, "space": {"name": "spaces/AAQA"}},
        "space": {"name": "spaces/AAQA"},
    }


def test_unknown_card_action_does_not_patch_or_reply(caplog):
    gateway = _bind()
    envelope = _envelope("not_a_real_action")
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


def test_known_clarify_action_still_patches():
    gateway = _bind()
    envelope = _envelope("hermes_clarify")
    result = asyncio.run(gateway._handle_card_event(envelope, notify=True))
    assert result == "That question has expired. Please ask ROBIE again."
    assert len(gateway.patches) == 1
    assert gateway.patches[0][0] == MESSAGE
    http = asyncio.run(gateway.dispatch_http_event(envelope))
    assert http["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    assert "not supported" not in http["text"].lower()


def test_unsupported_reply_string_is_gone():
    adapter_path = Path(__file__).resolve().parent.parent / "integrations/google_chat/adapter.py"
    handle = adapter_path.read_text(encoding="utf-8").split(
        "async def _handle_card_event", 1
    )[1].split("async def dispatch_http_event", 1)[0]
    assert "That action is not supported." not in handle
    assert "unknown card action ignored" in handle
    assert "return None" in handle
