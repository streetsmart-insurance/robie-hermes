"""Post a short message as the Robie Chat APP into an existing thread.

This is the Job Engine / scheduler fallback. Production Chat jobs already
post as the APP because ``guard_chat_response`` is included in adapter.send().
Never impersonate the person Robie AI. Never create a new space.

SINGLE-IDENTITY RULE (M4 hardening): Chat posts go out as the dedicated Chat
app's own service account, and ONLY as that identity.

Credential handling for the Chat API client (fail closed):
1. ``ROBIE_CHAT_SA_KEY_FILE`` — path to the Chat app's service-account key
   JSON. This is the ONLY accepted credential source.
2. The user-token fallback (``ROBIE_GOOGLE_TOKEN_FILE``) and the ADC fallback
   (``google.auth.default``) were REMOVED. When the SA key is not configured,
   Chat posting raises ``ChatAppIdentityError`` instead of silently posting as
   a human or the wrong service account.

Required configuration:
- ``ROBIE_CHAT_SA_KEY_FILE`` must point to a protected secret mount: the
  file must be a regular file with NO group/other permission bits (0600 or
  0400 style) and must be owned by the service user running the engine
  (``st_uid == os.geteuid()``). Inline key JSON via an env var is never
  accepted — only ``service_account.Credentials.from_service_account_file``.
- ``ROBIE_CHAT_APP_CLIENT_EMAIL`` must hold the expected ``client_email`` of
  the Chat app's service account. After loading the key, the client email is
  asserted against this value and the client is refused on mismatch.

Failure handling: missing/unreadable/unparseable key, unsafe permissions, or
principal mismatch raise ``ChatAppIdentityError``. ``post_hitl_to_originating_thread``
converts that into an operator alert on the email/ops channel (the existing
``fail_notify_emails()`` recipients — Carlo and Jake) via
``alert_chat_app_identity_failure``. Reporting that Chat posting is broken via
Chat itself would be circular, so the alert NEVER goes through Chat.

There is no outbound email API on hermes-poc-01 besides the engine's Gmail
sender; the fail-notify path uses that (``verification_mailer``) when
available, and always logs at CRITICAL. ``find_direct_message_space`` only
resolves an already-existing DM (``spaces.findDirectMessage``). It never
creates a space and does not @mention anyone.
"""

from __future__ import annotations

import logging
import os
import stat
from typing import Any, Callable


logger = logging.getLogger("robie.chat_app_post")


class ChatAppIdentityError(RuntimeError):
    """The dedicated Chat-app identity could not be established.

    Raised fail-closed when ``ROBIE_CHAT_SA_KEY_FILE`` is unset, points at a
    missing/unreadable/unparseable file, fails the protected-mount permission
    check, or when the loaded key's ``client_email`` does not match
    ``ROBIE_CHAT_APP_CLIENT_EMAIL``. There is intentionally no fallback to a
    user token file or Application Default Credentials.
    """


CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
# Production Robie home space (CURRENT_STATE / regression_battery). Email
# jobs have no originating Chat thread; HITL still belongs here, not a
# new space and not signBlob email.
DEFAULT_ROBIE_HOME_SPACE = "spaces/AAQAZbLJO78"
DEFAULT_FAIL_NOTIFY_EMAILS = (
    "carlo@streetsmart.insurance",
    "jake@streetsmart.insurance",
)


def robie_home_space() -> str | None:
    """Existing Robie Chat space. Never creates a space."""
    raw = str(os.environ.get("ROBIE_CHAT_HOME_SPACE") or "").strip()
    name = raw or DEFAULT_ROBIE_HOME_SPACE
    if name.startswith("spaces/"):
        return name
    return None


def conversation_target(job: dict[str, Any]) -> tuple[str, str | None] | None:
    payload = dict(job.get("payload") or {})
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if conversation_id.startswith("spaces/"):
        thread_id = str(payload.get("thread_id") or payload.get("thread_name") or "").strip() or None
        if thread_id and not thread_id.startswith("spaces/"):
            thread_id = f"{conversation_id}/threads/{thread_id}"
        return conversation_id, thread_id
    # Email jobs have no @robie thread. Post HITL to the existing Robie
    # home space (job c75aab5c: hitl_posted=false / signBlob 403).
    if str(job.get("action_type") or "").strip() == "hermes.email_task":
        home = robie_home_space()
        if home:
            return home, None
    return None


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


def _expected_client_email() -> str:
    """The configured Chat-app identity. Required — no default is safe."""
    expected = str(os.environ.get("ROBIE_CHAT_APP_CLIENT_EMAIL") or "").strip()
    if not expected:
        raise ChatAppIdentityError(
            "ROBIE_CHAT_APP_CLIENT_EMAIL is not set; cannot validate the Chat "
            "app's service-account identity, refusing to post."
        )
    return expected


def _check_key_file_permissions(path: str) -> None:
    """Fail closed unless the key file is a protected secret mount.

    Required: a regular file, no group/other permission bits set
    (e.g. 0600 or 0400), and owned by the service user running this process
    (``st_uid == os.geteuid()``). A world/group-readable key, or a key owned
    by another user, is refused — it must live on a protected secret mount.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ChatAppIdentityError(
            f"ROBIE_CHAT_SA_KEY_FILE is unreadable ({path}): {exc}"
        ) from exc
    if not stat.S_ISREG(st.st_mode):
        raise ChatAppIdentityError(
            f"ROBIE_CHAT_SA_KEY_FILE must be a regular file, not {path}"
        )
    if st.st_mode & 0o077:
        raise ChatAppIdentityError(
            f"ROBIE_CHAT_SA_KEY_FILE ({path}) has group/other permission bits "
            f"set ({oct(st.st_mode & 0o777)}); key files must be 0600-style "
            "(no group/other access) on a protected secret mount."
        )
    if st.st_uid != os.geteuid():
        raise ChatAppIdentityError(
            f"ROBIE_CHAT_SA_KEY_FILE ({path}) is not owned by the service user "
            "running this process; key files must be owned by the service user."
        )


def _chat_app_client() -> Any:
    from googleapiclient.discovery import build

    sa_key_file = os.environ.get("ROBIE_CHAT_SA_KEY_FILE", "").strip()
    if sa_key_file.lstrip().startswith("{"):
        # Never accept inline key JSON via env var — the key must live on a
        # protected secret mount and be loaded with from_service_account_file.
        raise ChatAppIdentityError(
            "ROBIE_CHAT_SA_KEY_FILE looks like inline key JSON; inline key "
            "material is never accepted. Point it at a protected key file."
        )
    if not sa_key_file:
        token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
        if token_file:
            raise ChatAppIdentityError(
                "ROBIE_CHAT_SA_KEY_FILE is not set and ROBIE_GOOGLE_TOKEN_FILE "
                "is set; the user-token fallback was removed (M4 hardening) — "
                "Chat posts go out as the dedicated Chat app identity only. "
                "Set ROBIE_CHAT_SA_KEY_FILE to the Chat app's key file."
            )
        raise ChatAppIdentityError(
            "ROBIE_CHAT_SA_KEY_FILE is not set; refusing to build a Chat API "
            "client. The user-token and ADC fallbacks were removed — Chat "
            "posts require the dedicated Chat app service-account key."
        )
    if os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip():
        logger.warning(
            "ROBIE_GOOGLE_TOKEN_FILE is set but is ignored for Chat posting; "
            "only ROBIE_CHAT_SA_KEY_FILE is honored."
        )
    _check_key_file_permissions(sa_key_file)
    expected_email = _expected_client_email()

    from google.oauth2 import service_account

    try:
        credentials = service_account.Credentials.from_service_account_file(
            sa_key_file, scopes=[CHAT_BOT_SCOPE]
        )
    except Exception as exc:
        raise ChatAppIdentityError(
            f"ROBIE_CHAT_SA_KEY_FILE ({sa_key_file}) is unparseable or could "
            f"not be loaded: {exc}"
        ) from exc
    actual_email = str(getattr(credentials, "service_account_email", "") or "").strip()
    if actual_email != expected_email:
        raise ChatAppIdentityError(
            "Chat app principal mismatch: key file "
            f"{sa_key_file} identifies as {actual_email or '<unknown>'} but "
            f"ROBIE_CHAT_APP_CLIENT_EMAIL expects {expected_email}; refusing "
            "to post as the wrong identity."
        )
    return build("chat", "v1", credentials=credentials, cache_discovery=False)


# Injectable alert sender for tests: (recipients, subject, body) -> None.
# Production default below goes through the engine's Gmail sender.
_identity_alert_sender: Callable[[list[str], str, str], None] | None = None


def _send_alert_email(recipients: list[str], subject: str, body: str) -> None:
    """Default alert sender: the engine's Gmail sender (never Chat)."""
    try:
        from .verification_mailer import send_verification_email
    except Exception as exc:
        logger.error(
            "chat-app identity alert: email sender unavailable: %s", exc
        )
        raise
    send_verification_email(
        to=list(recipients),
        cc=[],
        subject=subject,
        text_body=body,
        plain_only=True,
    )


def alert_chat_app_identity_failure(reason: str) -> bool:
    """Fire the operator alert for a Chat-app identity failure.

    Uses the email/ops channel (``fail_notify_emails()`` recipients — Carlo
    and Jake), NEVER Chat: reporting that Chat posting is broken via Chat
    itself is circular. Always logs at CRITICAL. Returns True when an alert
    send was attempted, False when even the alert path failed.
    """
    logger.critical("CHAT APP IDENTITY FAILURE: %s", reason)
    recipients = fail_notify_emails()
    subject = "[ROBIE] Chat app identity failure — operator action required"
    body = (
        "The Robie Chat app could not establish its dedicated posting identity, "
        "so Chat posting is DISABLED until this is fixed.\n\n"
        f"Reason: {reason}\n\n"
        "Check on the engine host:\n"
        "  1. ROBIE_CHAT_SA_KEY_FILE points at the Chat app's service-account "
        "key JSON on a protected secret mount (0600-style perms, owned by the "
        "service user).\n"
        "  2. ROBIE_CHAT_APP_CLIENT_EMAIL matches the key's client_email.\n"
        "  3. The ROBIE_GOOGLE_TOKEN_FILE / ADC fallbacks were removed — a user "
        "token or default credentials will no longer be used.\n"
    )
    sender = _identity_alert_sender or _send_alert_email
    try:
        sender(recipients, subject, body)
        return True
    except Exception as exc:
        logger.error("chat-app identity alert sender failed: %s", exc)
        return False


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


def _scrub_card_text(cards: list[dict[str, Any]], scrub) -> list[dict[str, Any]]:
    """Run the outbound sanitizer over text fields in a card payload."""

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return scrub(value)
        if isinstance(value, list):
            return [_walk(item) for item in value]
        if isinstance(value, dict):
            return {key: _walk(item) for key, item in value.items()}
        return value

    return _walk(cards)


def post_as_chat_app(
    space_name: str,
    text: str,
    *,
    thread_name: str | None = None,
    thread_key: str | None = None,
    chat: Any | None = None,
) -> dict[str, Any]:
    """POST spaces.messages.create as the Chat APP. Fail-closed on auth errors."""
    if not space_name.startswith("spaces/"):
        raise ValueError("Chat APP posts stay in an existing space")
    from .hitl import sanitize_hitl_chat_text
    from .user_reply import format_user_reply

    text = format_user_reply(sanitize_hitl_chat_text(text))
    client = chat if chat is not None else _chat_app_client()
    body: dict[str, Any] = {"text": text}
    kwargs: dict[str, Any] = {"parent": space_name, "body": body}
    if thread_name:
        body["thread"] = {"name": thread_name}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    elif thread_key:
        body["thread"] = {"threadKey": thread_key}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    result = client.spaces().messages().create(**kwargs).execute()
    return {"name": result.get("name"), "thread": (result.get("thread") or {}).get("name")}


def post_card_as_chat_app(
    space_name: str,
    cards_v2: list[dict[str, Any]],
    *,
    thread_name: str | None = None,
    thread_key: str | None = None,
    chat: Any | None = None,
) -> dict[str, Any]:
    """POST spaces.messages.create with cardsV2 as the Chat APP.

    Same fail-closed identity as ``post_as_chat_app``: the dedicated Chat
    app service account only (``ROBIE_CHAT_SA_KEY_FILE`` /
    ``ROBIE_CHAT_APP_CLIENT_EMAIL``), no user-token or ADC fallback.
    Used for interactive approval cards (Approve/Reject buttons).
    """
    if not space_name.startswith("spaces/"):
        raise ValueError("Chat APP posts stay in an existing space")
    if not isinstance(cards_v2, list) or not cards_v2:
        raise ValueError("cardsV2 must be a non-empty list")
    from .user_reply import format_user_reply

    cards_v2 = _scrub_card_text(
        cards_v2, lambda value: format_user_reply(value, collapse=False)
    )
    client = chat if chat is not None else _chat_app_client()
    body: dict[str, Any] = {"cardsV2": list(cards_v2)}
    kwargs: dict[str, Any] = {"parent": space_name, "body": body}
    if thread_name:
        body["thread"] = {"name": thread_name}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    elif thread_key:
        body["thread"] = {"threadKey": thread_key}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    result = client.spaces().messages().create(**kwargs).execute()
    return {"name": result.get("name"), "thread": (result.get("thread") or {}).get("name")}


def maybe_post_audit_as_chat_app(job: dict[str, Any], message: str) -> dict[str, Any] | None:
    target = conversation_target(job)
    if target is None:
        return None
    space, thread = target
    return post_as_chat_app(space, message, thread_name=thread)


def post_hitl_to_originating_thread(
    message: str,
    *,
    job_id: str | None = None,
    store: Any | None = None,
    poster: Any | None = None,
    db_path: str | None = None,
) -> bool:
    """Post HITL into the same Chat thread as the @robie. Not a second channel.

    Fail closed when the job has no conversation_id/thread. Never uses a
    webhook. RETRY lives in that originating thread.

    A Chat-app identity failure (missing/invalid key, principal mismatch)
    fires the operator alert via ``alert_chat_app_identity_failure`` (email/
    ops channel, never Chat) and returns False. Other post errors still
    return False without an operator alert.
    """
    job_key = str(
        job_id or os.environ.get("ROBIE_JOB_ID") or os.environ.get("JOB_ID") or ""
    ).strip()
    path = str(db_path or os.environ.get("ROBIE_JOB_DB") or "").strip()
    if not job_key:
        return False
    if store is None:
        if not path:
            return False
        from .store import JobStore

        store = JobStore(path)
    try:
        job = store.get_job(job_key)
    except Exception:
        return False
    target = conversation_target(job)
    if target is None:
        return False
    space, thread = target
    send = poster if poster is not None else post_as_chat_app
    try:
        try:
            if thread:
                send(space, message, thread_name=thread)
            else:
                from .chat_thread import job_thread_key

                send(space, message, thread_key=job_thread_key(job_key))
        except TypeError:
            try:
                send(space, message, thread_name=thread)
            except TypeError:
                send(space, message)
        return True
    except ChatAppIdentityError as exc:
        # The Chat identity is broken: page the operator on the email/ops
        # channel (Chat itself is the thing that's down), then fail closed.
        alert_chat_app_identity_failure(str(exc))
        return False
    except Exception:
        return False
