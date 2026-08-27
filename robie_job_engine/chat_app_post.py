"""Post a short message as the Robie Chat APP into an existing thread.

This is the Job Engine / scheduler fallback. Production Chat jobs already
post as the APP because ``guard_chat_response`` is included in adapter.send().
Never impersonate the person Robie AI. Never create a new space.
"""

from __future__ import annotations

import os
from typing import Any


CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"


def conversation_target(job: dict[str, Any]) -> tuple[str, str | None] | None:
    payload = dict(job.get("payload") or {})
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id.startswith("spaces/"):
        return None
    thread_id = str(payload.get("thread_id") or payload.get("thread_name") or "").strip() or None
    if thread_id and not thread_id.startswith("spaces/"):
        thread_id = f"{conversation_id}/threads/{thread_id}"
    return conversation_id, thread_id


def post_as_chat_app(
    space_name: str,
    text: str,
    *,
    thread_name: str | None = None,
) -> dict[str, Any]:
    """POST spaces.messages.create as the Chat APP. Fail-closed on auth errors."""
    if not space_name.startswith("spaces/"):
        raise ValueError("Chat APP posts stay in an existing space")
    import google.auth
    from googleapiclient.discovery import build

    token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
    if token_file:
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_file(token_file)
    else:
        credentials, _ = google.auth.default(scopes=[CHAT_BOT_SCOPE])
    chat = build("chat", "v1", credentials=credentials, cache_discovery=False)
    body: dict[str, Any] = {"text": text}
    kwargs: dict[str, Any] = {"parent": space_name, "body": body}
    if thread_name:
        body["thread"] = {"name": thread_name}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    result = chat.spaces().messages().create(**kwargs).execute()
    return {"name": result.get("name"), "thread": (result.get("thread") or {}).get("name")}


def maybe_post_audit_as_chat_app(job: dict[str, Any], message: str) -> dict[str, Any] | None:
    target = conversation_target(job)
    if target is None:
        return None
    space, thread = target
    return post_as_chat_app(space, message, thread_name=thread)
