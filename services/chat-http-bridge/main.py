from __future__ import annotations

import copy
import logging
import os
import re
from typing import Any

from flask import Flask, jsonify, request
from google.cloud import pubsub_v1


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("robie-chat-http-bridge")

PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT"]
TOPIC_ID = os.environ.get("ROBIE_CHAT_TOPIC", "hermes-chat-topic")
TOPIC_PATH = f"projects/{PROJECT_ID}/topics/{TOPIC_ID}"
ACTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
publisher = pubsub_v1.PublisherClient()


def _normalize(payload: dict[str, Any], action_name: str | None) -> dict[str, Any]:
    """Preserve Google's event and add the legacy fields ROBIE consumes."""
    event = copy.deepcopy(payload)
    chat = event.get("chat") if isinstance(event.get("chat"), dict) else {}
    message_payload = (
        chat.get("messagePayload")
        if isinstance(chat.get("messagePayload"), dict)
        else {}
    )
    clicked = {}
    for key in ("buttonClickedPayload", "cardClickedPayload", "widgetUpdatedPayload"):
        candidate = chat.get(key)
        if isinstance(candidate, dict):
            clicked = candidate
            break

    common = event.get("common")
    if not isinstance(common, dict):
        common = event.get("commonEventObject")
    common = dict(common) if isinstance(common, dict) else {}
    if action_name:
        common["invokedFunction"] = action_name
    if common:
        event["common"] = common

    # Registered Google Chat slash commands arrive through the Workspace
    # Add-ons envelope under chat.messagePayload.  The deployed Hermes
    # adapter consumes the legacy top-level message/space/user fields, so
    # expose those fields without discarding or mutating Google's envelope.
    # Card actions use the sibling clicked payload handled by the same rule.
    source = message_payload or clicked
    for key in ("space", "message", "user"):
        if not event.get(key) and source.get(key):
            event[key] = source[key]

    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if not event.get("user") and isinstance(message.get("sender"), dict):
        event["user"] = message["sender"]

    if message_payload and not event.get("type"):
        event["type"] = "MESSAGE"
    if action_name and not event.get("type"):
        event["type"] = "CARD_CLICKED"
    return event


def _publish(event: dict[str, Any], event_type: str) -> None:
    body = app.json.dumps(event, separators=(",", ":")).encode("utf-8")
    future = publisher.publish(TOPIC_PATH, body, **{"ce-type": event_type})
    future.result(timeout=8)


def _chat_message(text: str) -> dict[str, Any]:
    """Return the Message object expected by a standard Google Chat app."""
    return {"text": text}


def _event_style(payload: dict[str, Any]) -> str:
    """Classify the envelope without logging customer message contents."""
    if isinstance(payload.get("chat"), dict):
        return "workspace_addon"
    if payload.get("type") or payload.get("eventType"):
        return "chat_api"
    return "unknown"


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "robie-chat-http-bridge"})


@app.post("/")
@app.post("/actions/<action_name>")
def receive(action_name: str | None = None):
    if action_name and not ACTION_RE.fullmatch(action_name):
        return jsonify(_chat_message("Unsupported action.")), 404
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(_chat_message("Invalid request.")), 400

    event = _normalize(payload, action_name)
    event_type = "google.workspace.chat.card.v1.clicked" if action_name else "google.workspace.chat.event.v1.received"
    try:
        _publish(event, event_type)
    except Exception:
        logger.exception("Could not forward Google Chat event")
        return jsonify(_chat_message("ROBIE could not record this safely. Please try again.")), 503

    logger.info(
        "Forwarded Google Chat event action=%s style=%s event_type=%s keys=%s",
        action_name or "message",
        _event_style(payload),
        payload.get("type") or payload.get("eventType") or "none",
        ",".join(sorted(payload.keys())),
    )

    # Hermes posts the durable status/card asynchronously. Google recommends an
    # empty synchronous response for this pattern; returning a Message or an
    # add-on action here can make Chat display "app not responding" when the
    # configured interaction format differs from the response envelope.
    return jsonify({})
