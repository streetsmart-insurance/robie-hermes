"""Post a short message as the Robie Chat APP into an existing thread.

This is the Job Engine / scheduler fallback. Production Chat jobs already
post as the APP because ``guard_chat_response`` is included in adapter.send().
Never impersonate the person Robie AI. Never create a new space.

There is no outbound email API on hermes-poc-01. Operator fail-notify uses
this same Chat APP poster. ``find_direct_message_space`` only resolves an
already-existing DM (``spaces.findDirectMessage``). It never creates a
space and does not @mention anyone.
"""

from __future__ import annotations

import os
from typing import Any, Callable


CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
DEFAULT_CHAT_SPACE = "spaces/AAQAZbLJO78"
DEFAULT_FAIL_NOTIFY_EMAILS = (
    "carlo@streetsmart.insurance",
    "jake@streetsmart.insurance",
)
# Same cascade Production pre-flight already honors. Callers may set a
# more specific key (ROBIE_PREFLIGHT_CHAT_SPACE) without creating a space.
OPS_CHAT_SPACE_ENV_KEYS = (
    "ROBIE_PREFLIGHT_CHAT_SPACE",
    "ROBIE_OPS_CHAT_SPACE",
    "GOOGLE_CHAT_HOME_CHANNEL",
)


def conversation_target(job: dict[str, Any]) -> tuple[str, str | None] | None:
    payload = dict(job.get("payload") or {})
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id.startswith("spaces/"):
        return None
    thread_id = str(payload.get("thread_id") or payload.get("thread_name") or "").strip() or None
    if thread_id and not thread_id.startswith("spaces/"):
        thread_id = f"{conversation_id}/threads/{thread_id}"
    return conversation_id, thread_id


def fail_notify_emails() -> list[str]:
    """Carlo and Jake. Override with ROBIE_PREFLIGHT_FAIL_NOTIFY (comma emails)."""
    raw = os.environ.get("ROBIE_PREFLIGHT_FAIL_NOTIFY", "").strip()
    if raw:
        emails = [
            part.strip().casefold()
            for part in raw.split(",")
            if "@" in part.strip()
        ]
        if emails:
            return emails
    return list(DEFAULT_FAIL_NOTIFY_EMAILS)


def ops_chat_space() -> str:
    """Existing Robie Chat space. Never creates a space. Never @robie."""
    for key in OPS_CHAT_SPACE_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value.startswith("spaces/"):
            return value
    return DEFAULT_CHAT_SPACE


def notify_operators(
    text: str,
    *,
    poster: Callable[..., Any] | None = None,
    dm_finder: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Post the Robie space message, then the same text to Carlo/Jake DMs.

    Shared by Production pre-flight and the scheduler OnFailure oneshot.
    There is no outbound email API on hermes-poc-01; operator notify reuses
    ``post_as_chat_app`` against an existing DM from
    ``find_direct_message_space``. Never @robie. Never creates a space.
    """
    if "@robie" in text.casefold():
        raise ValueError("Chat APP notify must not @robie")
    send = poster if poster is not None else post_as_chat_app
    finder = dm_finder if dm_finder is not None else find_direct_message_space
    targets: list[str] = []
    dm_errors: list[str] = []
    space = ops_chat_space()
    space_posted = False
    if space.startswith("spaces/"):
        send(space, text)
        targets.append(space)
        space_posted = True
    for email in fail_notify_emails():
        try:
            dm_space = finder(email)
        except Exception as exc:
            dm_errors.append(f"{email}: {type(exc).__name__}")
            continue
        if not str(dm_space or "").startswith("spaces/"):
            dm_errors.append(f"{email}: no existing DM space")
            continue
        if dm_space in targets:
            continue
        try:
            send(dm_space, text)
            targets.append(dm_space)
        except Exception as exc:
            dm_errors.append(f"{email}: {type(exc).__name__}")
    return {
        "space_posted": space_posted,
        "targets": targets,
        "dm_errors": dm_errors,
    }


def _chat_app_client() -> Any:
    import google.auth
    from googleapiclient.discovery import build

    token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
    if token_file:
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_file(token_file)
    else:
        credentials, _ = google.auth.default(scopes=[CHAT_BOT_SCOPE])
    return build("chat", "v1", credentials=credentials, cache_discovery=False)


def find_direct_message_space(
    user: str,
    *,
    chat: Any | None = None,
) -> str:
    """Resolve an existing Chat APP DM. Never creates a space. Never @mentions."""
    name = str(user or "").strip()
    if not name:
        raise ValueError("DM user is required")
    if name.startswith("spaces/"):
        return name
    if not name.startswith("users/"):
        name = f"users/{name}"
    client = chat if chat is not None else _chat_app_client()
    result = client.spaces().findDirectMessage(name=name).execute()
    space = str((result or {}).get("name") or "").strip()
    if not space.startswith("spaces/"):
        raise ValueError("findDirectMessage did not return an existing DM space")
    return space


def post_as_chat_app(
    space_name: str,
    text: str,
    *,
    thread_name: str | None = None,
    chat: Any | None = None,
) -> dict[str, Any]:
    """POST spaces.messages.create as the Chat APP. Fail-closed on auth errors."""
    if not space_name.startswith("spaces/"):
        raise ValueError("Chat APP posts stay in an existing space")
    from .hitl import sanitize_hitl_chat_text

    text = sanitize_hitl_chat_text(text)
    client = chat if chat is not None else _chat_app_client()
    body: dict[str, Any] = {"text": text}
    kwargs: dict[str, Any] = {"parent": space_name, "body": body}
    if thread_name:
        body["thread"] = {"name": thread_name}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    result = client.spaces().messages().create(**kwargs).execute()
    return {"name": result.get("name"), "thread": (result.get("thread") or {}).get("name")}


def maybe_post_audit_as_chat_app(job: dict[str, Any], message: str) -> dict[str, Any] | None:
    target = conversation_target(job)
    if target is None:
        return None
    space, thread = target
    return post_as_chat_app(space, message, thread_name=thread)
