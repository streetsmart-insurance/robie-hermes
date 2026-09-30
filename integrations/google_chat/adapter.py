"""
Google Chat platform adapter.

Uses authenticated HTTP callbacks or Google Cloud Pub/Sub for inbound
events and the Google Chat REST API for outbound messages. Pub/Sub remains
available for no-public-URL deployments.

Concurrency model
-----------------
The Pub/Sub SubscriberClient invokes its message callback in a background
thread (managed by the client's internal executor). The adapter's
``handle_message`` coroutine must run on the asyncio event loop, so the
callback uses ``asyncio.run_coroutine_threadsafe`` with
``add_done_callback`` (never ``.result()`` — that would block the callback
thread and saturate the Pub/Sub executor under load).

All outbound Chat REST calls go through ``asyncio.to_thread`` because the
googleapiclient is synchronous. This keeps the event loop responsive.

Pub/Sub delivery diagram::

    Pub/Sub stream   ->  callback thread        ->  asyncio loop
    (streaming_pull)     (_on_pubsub_message)       (handle_message)
         |                       |                        |
         |   at-least-once       |  parse + dedup         |  agent work
         |   delivery            |  _submit_on_loop       |  send() response
         |                       |  message.ack()         |
         v                       v                        v

Event type routing
------------------
Inbound envelope carries ``type`` in [MESSAGE, ADDED_TO_SPACE, REMOVED_FROM_SPACE,
CARD_CLICKED]. Only MESSAGE dispatches to the agent. ADDED_TO_SPACE caches the
bot's resource name (belt-and-suspenders on top of eager resolution in connect()).
CARD_CLICKED is ACK'd only in v1 (follow-up PR implements interactivity).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Heavy google-cloud + googleapiclient imports are deferred to first
# adapter use. Importing them eagerly here added ~110ms wall and ~33MB
# RSS to *every* CLI invocation (the plugin loader imports this module at
# ``model_tools`` import time, so ``hermes status``, ``hermes chat``, etc.
# all paid the cost even though they never instantiate the adapter).
#
# All names below are module globals that ``_load_google_modules()``
# rebinds on first call. The ``HttpError = Exception`` placeholder is
# important: ``except HttpError as exc:`` clauses elsewhere in this
# module bind the *current* module-global at try/except evaluation time,
# so as long as ``_load_google_modules()`` runs before any such
# ``try`` block executes (which it does — ``__init__`` calls it), the
# rebound real ``googleapiclient.errors.HttpError`` is what actually
# matches at runtime.
GOOGLE_CHAT_AVAILABLE: bool = False
httplib2: Any = None  # type: ignore
pubsub_v1: Any = None  # type: ignore
gax_exceptions: Any = None  # type: ignore
service_account: Any = None  # type: ignore
AuthorizedHttp: Any = None  # type: ignore
build_service: Any = None  # type: ignore
HttpError: Any = Exception  # type: ignore
MediaFileUpload: Any = None  # type: ignore

_google_modules_loaded: bool = False
_GOOGLE_ID_TOKEN_CERTS_TTL_SECONDS = 300
_google_id_token_request: Any = None
_google_id_token_request_lock = threading.Lock()


class _CachedGoogleAuthRequest:
    def __init__(self, request: Any, ttl_seconds: int = _GOOGLE_ID_TOKEN_CERTS_TTL_SECONDS) -> None:
        self._request = request
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}

    def __call__(self, url: str, method: str = "GET", **kwargs: Any) -> Any:
        cache_key = (method.upper(), url)
        if cache_key[0] != "GET":
            return self._request(url=url, method=method, **kwargs)

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

        response = self._request(url=url, method=method, **kwargs)
        if getattr(response, "status", None) == 200:
            with self._lock:
                self._cache[cache_key] = (now + self._ttl_seconds, response)
        return response


def _get_google_id_token_request() -> Any:
    global _google_id_token_request
    with _google_id_token_request_lock:
        if _google_id_token_request is None:
            try:
                from google.auth.transport import requests as google_requests
            except ImportError as exc:
                raise RuntimeError("google-auth is required for Google Chat HTTP callbacks") from exc
            _google_id_token_request = _CachedGoogleAuthRequest(google_requests.Request())
        return _google_id_token_request


def _verify_google_id_token(token: str, audience: str) -> Dict[str, Any]:
    try:
        from google.oauth2 import id_token
    except ImportError as exc:
        raise RuntimeError("google-auth is required for Google Chat HTTP callbacks") from exc

    return id_token.verify_oauth2_token(
        token,
        _get_google_id_token_request(),
        audience,
    )


def _load_google_modules() -> bool:
    """Lazily import the heavy google-cloud + googleapiclient stack.

    Idempotent. Returns True if the optional deps are installed and
    were successfully imported, False otherwise. On success, mutates
    the module globals so existing code using ``pubsub_v1``,
    ``service_account``, ``HttpError``, etc. transparently uses the
    real classes.

    Why deferred: the import chain pulls in google.cloud.pubsub_v1,
    googleapiclient, grpc, and friends — about 33MB RSS and 110ms wall
    on a fresh interpreter. Plugin discovery imports this module on
    every CLI invocation, even ones that never touch a gateway.
    """
    global GOOGLE_CHAT_AVAILABLE, _google_modules_loaded
    global httplib2, pubsub_v1, gax_exceptions, service_account
    global AuthorizedHttp, build_service, HttpError, MediaFileUpload
    if _google_modules_loaded:
        return GOOGLE_CHAT_AVAILABLE
    _google_modules_loaded = True
    try:
        import httplib2 as _httplib2
        from google.cloud import pubsub_v1 as _pubsub_v1
        from google.api_core import exceptions as _gax_exceptions
        from google.oauth2 import service_account as _service_account
        from google_auth_httplib2 import AuthorizedHttp as _AuthorizedHttp
        from googleapiclient.discovery import build as _build_service
        from googleapiclient.errors import HttpError as _HttpError
        from googleapiclient.http import MediaFileUpload as _MediaFileUpload
    except ImportError:
        GOOGLE_CHAT_AVAILABLE = False
        return False
    httplib2 = _httplib2
    pubsub_v1 = _pubsub_v1
    gax_exceptions = _gax_exceptions
    service_account = _service_account
    AuthorizedHttp = _AuthorizedHttp
    build_service = _build_service
    HttpError = _HttpError
    MediaFileUpload = _MediaFileUpload
    GOOGLE_CHAT_AVAILABLE = True
    return True

from gateway.config import Platform, PlatformConfig

# Trigger registration of the dynamic ``google_chat`` enum member at module
# import time.  ``_missing_()`` caches the pseudo-member in
# ``_value2member_map_`` *and* ``_member_map_``, so after this call
# ``Platform.GOOGLE_CHAT`` resolves via attribute access too.  Without this
# line, any code (including tests) that references ``Platform.GOOGLE_CHAT``
# before an adapter instance is constructed would hit ``AttributeError``.
# Built-ins avoid this because they have explicit enum members; plugin
# platforms earn the attribute by asking for it once.
Platform("google_chat")
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
)


# Pin the logger name to the legacy module path so operator log filters,
# grep aliases, and the gateway's bundled log views keep matching after
# the in-tree → plugin migration. ``__name__`` resolves to
# ``hermes_plugins.platforms__google_chat.adapter`` once the plugin
# loader namespaces this module, which would silently break every
# downstream log-monitor that greps for ``gateway.platforms.google_chat``.
_configured_job_engine_root = os.environ.get("ROBIE_CANONICAL_JOB_ENGINE_ROOT")
if _configured_job_engine_root:
    # Production supplies the versioned release root. Never insert the old
    # flattened working directory: it contains ``secrets.py`` and can shadow
    # Python's standard-library ``secrets`` module.
    _canonical_import_root = str(_Path(_configured_job_engine_root).resolve())
    if _canonical_import_root not in sys.path:
        sys.path.insert(0, _canonical_import_root)
from robie_job_engine.attachments import (
    CallableDrivePort,
    drive_chip_download_failed_message,
    is_attachment_ingestion_error,
    proven_drive_share_identity,
    unmatched_drive_chip_refs,
)
from robie_job_engine.action_gate import (
    format_action_gate_chat_note,
    is_action_gate_refusal,
)
from robie_job_engine.runs import MessageMaintenanceDeferred
from robie_job_engine.chat_guard import (
    build_chat_execution_text,
    chat_hermes_should_run,
    chat_message_is_related_only,
    guard_chat_response,
    open_chat_job,
    retry_refusal_reply,
    retry_without_job_reply,
    start_generic_chat_job_heartbeat,
)
from robie_job_engine.chat_thread import (
    bind_job_chat_thread,
    inbound_thread_to_bind,
    outbound_thread_spec,
    read_job_chat_thread,
    remember_created_thread,
)
from robie_job_engine.engine import is_retry_text
from robie_job_engine.chat_queue import (
    DurableChatEventQueue,
    is_stale_human_input_bind_error,
)
from robie_job_engine.chat_admin import handle_admin_command
from robie_job_engine.decisions import resolve_bound_text_decision, text_decision_response
from robie_job_engine.models import TERMINAL_STATUSES, WAITING_STATUSES, JobStatus
from robie_job_engine.hitl import (
    classify_human_reply,
    human_reply_value,
    interaction_for_blocker,
)
from robie_job_engine.pubsub_ack import PubSubAckCoordinator
from robie_job_engine.runtime_env import chat_path_is_sandbox
from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS, classify_request
from robie_job_engine.secrets import redact_text
from robie_job_engine.test_runtime import dispatch_operational_chat, maybe_run_bounded_job
from robie_job_engine.store import JobStore

if _configured_job_engine_root:
    _EXPECTED_JOB_ENGINE_ROOT = _Path(_configured_job_engine_root).resolve()
    _LOADED_JOB_ENGINE_MODULE = _Path(
        sys.modules[open_chat_job.__module__].__file__ or ""
    ).resolve()
    if os.path.commonpath(
        (str(_EXPECTED_JOB_ENGINE_ROOT), str(_LOADED_JOB_ENGINE_MODULE))
    ) != str(_EXPECTED_JOB_ENGINE_ROOT):
        raise RuntimeError(
            "Google Chat gateway loaded ROBIE Job Engine from a non-canonical path: "
            f"{_LOADED_JOB_ENGINE_MODULE}"
        )
ROBIE_JOB_DB = os.environ.get(
    "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
)

logger = logging.getLogger("gateway.platforms.google_chat")


# Regex validating Pub/Sub subscription path format.
_SUBSCRIPTION_PATH_RE = re.compile(
    r"^projects/(?P<project>[^/]+)/subscriptions/(?P<sub>[^/]+)$"
)

# SA scopes — chat.bot covers messaging, memberships, and media.download
# for paperclip uploads. Drive picker chips (driveDataRef.driveFileId)
# have no Chat media resource; they need drive.readonly so the same app
# identity can fetch a file that has been shared with it. The bot still
# CANNOT call media.upload — Google requires user OAuth for that endpoint.
#
# Native attachment delivery (bot → user) is handled via a separate user-
# OAuth flow in ``oauth.py`` (this plugin's helper module): the user grants the bot
# the chat.messages.create scope ONCE via an in-chat consent flow; the
# bot then calls media.upload on the user's behalf when sending files.
# See https://developers.google.com/chat/api/guides/auth/users
_CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.bot",
    "https://www.googleapis.com/auth/pubsub",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Google Chat text-message size limit is 4096; leave margin.
_MAX_TEXT_LENGTH = 4000

# Per-space rate-limit hit counter threshold; warn if exceeded.
_RATE_LIMIT_WARN_THRESHOLD = 5

# Outbound retry parameters. Google's Chat REST API returns transient 5xx
# and 429 occasionally — without a retry wrapper, single hiccups drop
# user-visible messages. Backoff stays bounded so a true outage is still
# surfaced quickly. Pattern lifted from PR #14965.
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 8.0
_RETRY_JITTER = 0.3
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_CARD_WIDGET_TYPES = frozenset({
    "text",
    "text_paragraph",
    "decorated_text",
    "buttons",
    "button_list",
    "selection",
    "selection_input",
    "text_input",
    "image",
    "divider",
})


def _is_retryable_error(exc: BaseException) -> bool:
    """Classify outbound API errors as transient (retryable) vs permanent.

    Retries are applied to:
      - HTTP 429 (rate-limited)
      - HTTP 5xx (server errors)
      - Network/transport failures (timeout, connection reset, DNS)

    Authentication errors (401/403), client errors (4xx other than 429),
    and well-formed non-retryable failures are NOT retried — those
    indicate a misconfiguration or revoked token, not a hiccup.
    """
    # googleapiclient.errors.HttpError carries resp.status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status in _RETRYABLE_HTTP_STATUSES
    # Fallback heuristics for SSL/socket errors that don't carry an
    # HTTP status: text matches against common transport-layer wording.
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return True
    if "connection" in text and ("reset" in text or "refused" in text or "aborted" in text):
        return True
    if "broken pipe" in text or "remote disconnected" in text:
        return True
    return False

# Sentinel kept in ``_typing_messages`` after ``send()`` patches the typing
# marker into the agent's real response. Two purposes:
#   * ``send_typing`` checks for any value before posting — sentinel keeps
#     ``_keep_typing`` (running on the base-class timer) from creating a
#     fresh "Hermes is thinking…" card during the small window between
#     ``send()`` finishing and the base-class cancelling its typing_task.
#   * ``stop_typing`` checks for the sentinel and skips the API delete —
#     otherwise the safety-net cleanup at base.py:_process_message_background
#     would delete the response we just patched and leave a tombstone.
_TYPING_CONSUMED_SENTINEL = "<consumed>"


def check_google_chat_requirements() -> bool:
    """Check if Google Chat optional dependencies are installed.

    Triggers the lazy import of the google-cloud + googleapiclient stack
    on first call. Subsequent calls hit the cached result. This is the
    canonical "are the deps available" probe used by the plugin registry
    and the adapter's own startup gate.
    """
    return _load_google_modules()


# Hostnames we trust to host Google Chat attachment download URIs. Anything
# else gets rejected by _is_google_owned_host to block SSRF scenarios where
# a crafted event points downloadUri at a non-Google endpoint (e.g. the
# GCE/GKE metadata service at 169.254.169.254) and the bot's Service Account
# bearer token would be attached to the outbound request.
_TRUSTED_ATTACHMENT_HOSTS = (
    "googleapis.com",
    "chat.google.com",
    "drive.google.com",
    "docs.google.com",
    "lh3.googleusercontent.com",
    "lh4.googleusercontent.com",
    "lh5.googleusercontent.com",
    "lh6.googleusercontent.com",
)

_MAX_INBOUND_ATTACHMENT_BYTES = 25 * 1024 * 1024
_DRIVE_EXPORT_TYPES: Dict[str, Tuple[str, str]] = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


def _fetch_drive_bytes(
    creds: Any, drive_file_id: str, *, mime: str = "", filename: str = "attachment"
) -> Tuple[bytes, str, str]:
    """Download or export one Drive file. Raises on API / size / type errors."""
    import io
    from googleapiclient.http import MediaIoBaseDownload

    drive = build_service("drive", "v3", credentials=creds, cache_discovery=False)
    meta = drive.files().get(
        fileId=drive_file_id,
        fields="id,name,mimeType,size",
        supportsAllDrives=True,
    ).execute()
    source_mime = str(meta.get("mimeType") or mime or "")
    resolved_name = str(meta.get("name") or filename)
    if int(meta.get("size") or 0) > _MAX_INBOUND_ATTACHMENT_BYTES:
        raise ValueError("Drive attachment exceeds the 25 MB limit")
    export = _DRIVE_EXPORT_TYPES.get(source_mime)
    if source_mime.startswith("application/vnd.google-apps."):
        if export is None:
            raise ValueError(f"unsupported native Google Drive type: {source_mime}")
        resolved_mime, suffix = export
        if not resolved_name.casefold().endswith(suffix):
            resolved_name += suffix
        req = drive.files().export_media(fileId=drive_file_id, mimeType=resolved_mime)
    else:
        resolved_mime = source_mime or "application/octet-stream"
        req = drive.files().get_media(fileId=drive_file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
        if buf.tell() > _MAX_INBOUND_ATTACHMENT_BYTES:
            raise ValueError("Drive attachment exceeds the 25 MB limit")
    return buf.getvalue(), resolved_mime, resolved_name
_GOOGLE_WORKSPACE_URL_RE = re.compile(
    r"https://(?:docs\.google\.com/document/d/|drive\.google\.com/file/d/)"
    r"(?P<id>[A-Za-z0-9_-]{10,})[^\s<>()]*"
)


def _is_google_owned_host(url: str) -> bool:
    """Return True iff *url* is https and targets a Google-owned domain."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _TRUSTED_ATTACHMENT_HOSTS)


def _redact_sensitive(text: str) -> str:
    """Sanitize subscription paths and email-like tokens from an error string.

    Covers project IDs leaking via Pub/Sub exception messages, plus SA-ish
    email addresses. agent/redact.py handles log-level redaction elsewhere;
    this helper is for user-facing error messages.
    """
    if not text:
        return text
    text = re.sub(
        r"projects/[^/\s]+/subscriptions/[^/\s]+",
        "projects/<redacted>/subscriptions/<redacted>",
        text,
    )
    text = re.sub(
        r"projects/[^/\s]+/topics/[^/\s]+",
        "projects/<redacted>/topics/<redacted>",
        text,
    )
    text = redact_text(text)
    text = re.sub(
        r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.iam\.gserviceaccount\.com",
        "<sa>@<project>.iam.gserviceaccount.com",
        text,
    )
    return text


def _mime_for_message_type(mime: str) -> MessageType:
    """Map a MIME string to a hermes MessageType.

    Anything not image/audio/video falls through to DOCUMENT so the agent
    still receives the file.
    """
    if not mime:
        return MessageType.DOCUMENT
    if mime.startswith("image/"):
        return MessageType.PHOTO
    if mime.startswith("audio/"):
        return MessageType.AUDIO
    if mime.startswith("video/"):
        return MessageType.VIDEO
    return MessageType.DOCUMENT


def _required_str(mapping: Dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if value is None:
        raise ValueError(f"{context}.{key} is required")
    value = str(value).strip()
    if not value:
        raise ValueError(f"{context}.{key} is required")
    return value


def _button_to_chat(button: Dict[str, Any]) -> Dict[str, Any]:
    text = _required_str(button, "text", "button")
    action = _required_str(button, "action", "button")
    raw_params = button.get("parameters") or {}
    if not isinstance(raw_params, dict):
        raise ValueError("button.parameters must be an object")
    parameters = [
        {"key": str(key), "value": str(value)}
        for key, value in sorted(raw_params.items())
    ]
    if not any(item["key"] == "robie_env" for item in parameters):
        from robie_job_engine.runtime_env import chat_routing_env

        env = chat_routing_env()
        if env:
            parameters.append({"key": "robie_env", "value": env})
    action_base = os.getenv(
        "GOOGLE_CHAT_CARD_ACTION_BASE_URL",
        "https://robie-chat-http-bridge-751771086524.us-east1.run.app",
    ).strip().rstrip("/")
    function = f"{action_base}/actions/{action}"
    return {
        "text": text,
        "onClick": {"action": {"function": function, "parameters": parameters}},
    }


def _card_event_payload(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize legacy HTTP and Workspace Events card-click envelopes."""
    if not isinstance(envelope, dict):
        return None
    chat = envelope.get("chat") or {}
    candidates = [
        chat.get("buttonClickedPayload") if isinstance(chat, dict) else None,
        chat.get("cardClickedPayload") if isinstance(chat, dict) else None,
        chat.get("widgetUpdatedPayload") if isinstance(chat, dict) else None,
        envelope,
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        common = (
            candidate.get("common")
            or envelope.get("common")
            or envelope.get("commonEventObject")
            or {}
        )
        action = candidate.get("action") or {}
        invoked = (
            common.get("invokedFunction")
            or (common.get("parameters") or {}).get("__action_method_name__")
            or action.get("actionMethodName")
            or candidate.get("actionMethodName")
        )
        if not invoked:
            continue
        result = dict(candidate)
        for key in ("user", "space", "message", "common"):
            if not result.get(key) and envelope.get(key):
                result[key] = envelope[key]
        if not result.get("common") and common:
            result["common"] = common
        # Workspace Add-ons card clicks carry the true clicking user at
        # chat.user. commonEventObject has no user for this app, and the
        # envelope's top-level user is the message sender (the bot that
        # posted the card). Surface the human clicker as the payload's
        # user so actor extraction authorizes the right person.
        chat_user = chat.get("user") if isinstance(chat, dict) else None
        if isinstance(chat_user, dict) and chat_user.get("type") == "HUMAN":
            result["user"] = chat_user
        # Workspace Add-ons nest the clicked message (and its space) under
        # chat.message / chat.space -- the envelope has no top-level
        # "message" or "space" keys. Surface them so the async gateway path
        # can patch the answered card in place via messages.patch instead of
        # posting a separate acknowledgement message.
        if isinstance(chat, dict):
            if not result.get("message"):
                chat_message = chat.get("message")
                if isinstance(chat_message, dict) and chat_message.get("name"):
                    result["message"] = chat_message
            if not result.get("space"):
                chat_space = chat.get("space")
                if isinstance(chat_space, dict) and chat_space.get("name"):
                    result["space"] = chat_space
                else:
                    msg = result.get("message")
                    if isinstance(msg, dict):
                        msg_space = msg.get("space")
                        if isinstance(msg_space, dict) and msg_space.get("name"):
                            result["space"] = msg_space
        return result
    return None


def _card_parameters(payload: Dict[str, Any]) -> Dict[str, str]:
    common = payload.get("common") or {}
    direct = common.get("parameters") or payload.get("parameters")
    if isinstance(direct, dict):
        return {str(key): str(value) for key, value in direct.items()}
    action = payload.get("action") or {}
    result: Dict[str, str] = {}
    for item in action.get("parameters") or []:
        if isinstance(item, dict) and item.get("key") is not None:
            result[str(item["key"])] = str(item.get("value") or "")
    return result


def _gateway_job_db_path(adapter: Any) -> str:
    """This gateway's job database. An instance path overrides the env."""
    override = getattr(adapter, "_job_db_path", None)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return os.getenv(
        "ROBIE_JOB_DB",
        "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db",
    )


def _confirmation_owned_by_gateway(
    adapter: Any, parameters: Dict[str, str]
) -> Tuple[str, bool]:
    """Whether this gateway's own DB contains the clicked confirmation.

    The id is read from the token without verifying the HMAC. A different
    environment's signing key must not turn a foreign click into a reply.
    A lookup failure is treated as not owned: ack, do not patch.
    """
    from robie_job_engine.confirmations import has_confirmation, peek_confirmation_id

    confirmation_id = peek_confirmation_id(parameters.get("decision_token", ""))
    if not confirmation_id:
        return "", False
    try:
        owned = has_confirmation(_gateway_job_db_path(adapter), confirmation_id)
    except Exception:
        logger.info(
            "[GoogleChat] confirmation ownership check failed ref=%s",
            confirmation_id[:8],
        )
        return confirmation_id, False
    return confirmation_id, owned


def _click_routed_to_this_gateway(parameters: Dict[str, str]) -> bool:
    """Whether this click belongs on this gateway, by ``robie_env``.

    Same rule as the subscription filters: ``test`` is Test, ``prod`` is
    Prod, and a click with no ``robie_env`` is Prod's. A value that names
    the other environment is not ours.
    """
    from robie_job_engine.runtime_env import (
        PRODUCTION_ENV_NAMES,
        chat_routing_env,
        current_robie_env,
    )

    stamped = str(parameters.get("robie_env") or "").strip()
    if not stamped:
        return current_robie_env() in PRODUCTION_ENV_NAMES
    return stamped == (chat_routing_env() or "")


def _decision_owned_by_gateway(adapter: Any, decision_id: str) -> bool:
    """Whether this gateway's own DB already has the decision id.

    Read-only. Missing file or missing ``decisions`` table is not owned,
    so a foreign click cannot create schema or patch the card.
    """
    from robie_job_engine.decisions import has_decision

    decision_id = str(decision_id or "").strip()
    if not decision_id:
        return False
    try:
        return has_decision(_gateway_job_db_path(adapter), decision_id)
    except Exception:
        logger.info(
            "[GoogleChat] decision ownership check failed ref=%s",
            decision_id[:8],
        )
        return False


def _card_form_text(payload: Dict[str, Any], name: str) -> Optional[str]:
    common = payload.get("common") or {}
    inputs = common.get("formInputs") or payload.get("formInputs") or {}
    item = inputs.get(name) if isinstance(inputs, dict) else None
    if not isinstance(item, dict):
        return None
    string_inputs = item.get("stringInputs") or item.get("string_inputs") or {}
    values = string_inputs.get("value") if isinstance(string_inputs, dict) else None
    if isinstance(values, list) and values:
        return str(values[0]).strip() or None
    if isinstance(values, str):
        return values.strip() or None
    return None


def _widget_to_chat(widget: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(widget, dict):
        raise ValueError("card widgets must be objects")
    widget_type = str(widget.get("type") or "").strip()
    if widget_type not in _CARD_WIDGET_TYPES:
        raise ValueError(f"unsupported widget type: {widget_type or '<missing>'}")

    if widget_type in {"text", "text_paragraph"}:
        return {
            "textParagraph": {
                "text": GoogleChatAdapter.format_message(
                    _required_str(widget, "text", "widget")
                )
            }
        }
    if widget_type == "decorated_text":
        decorated: Dict[str, Any] = {
            "text": GoogleChatAdapter.format_message(
                _required_str(widget, "text", "widget")
            ),
            "wrapText": bool(widget.get("wrap_text", True)),
        }
        if widget.get("top_label"):
            decorated["topLabel"] = str(widget["top_label"])
        if widget.get("bottom_label"):
            decorated["bottomLabel"] = str(widget["bottom_label"])
        if widget.get("button"):
            decorated["button"] = _button_to_chat(widget["button"])
        return {"decoratedText": decorated}
    if widget_type == "divider":
        return {"divider": {}}
    if widget_type == "image":
        image = {"imageUrl": _required_str(widget, "image_url", "widget")}
        if widget.get("alt_text"):
            image["altText"] = str(widget["alt_text"])
        return {"image": image}
    if widget_type in {"buttons", "button_list"}:
        raw_buttons = widget.get("buttons") or []
        if not isinstance(raw_buttons, list) or not raw_buttons:
            raise ValueError("button widgets require at least one button")
        return {"buttonList": {"buttons": [_button_to_chat(btn) for btn in raw_buttons]}}
    if widget_type in {"selection", "selection_input"}:
        name = _required_str(widget, "name", "widget")
        raw_items = widget.get("items") or []
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("selection widgets require at least one item")
        items: List[Dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise ValueError("selection items must be objects")
            items.append({
                "text": _required_str(item, "text", "selection item"),
                "value": _required_str(item, "value", "selection item"),
                "selected": bool(item.get("selected", False)),
            })
        return {
            "selectionInput": {
                "name": name,
                "label": str(widget.get("label") or name),
                "type": str(widget.get("selection_type") or "CHECK_BOX"),
                "items": items,
            }
        }
    if widget_type == "text_input":
        name = _required_str(widget, "name", "widget")
        rendered_input: Dict[str, Any] = {
            "name": name,
            "label": str(widget.get("label") or name),
            "type": "MULTIPLE_LINE" if widget.get("multiline") else "SINGLE_LINE",
        }
        if widget.get("hint"):
            rendered_input["hintText"] = str(widget["hint"])
        if widget.get("value") is not None:
            rendered_input["value"] = str(widget["value"])
        return {"textInput": rendered_input}
    raise ValueError(f"unsupported widget type: {widget_type}")


def card_spec_to_cards_v2(card_spec: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(card_spec, dict):
        raise ValueError("card must be an object")

    raw_sections = card_spec.get("sections") or []
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValueError("card.sections must contain at least one section")

    sections: List[Dict[str, Any]] = []
    for section in raw_sections:
        if not isinstance(section, dict):
            raise ValueError("card sections must be objects")
        widgets = section.get("widgets") or []
        if not isinstance(widgets, list) or not widgets:
            raise ValueError("card section widgets must contain at least one widget")
        rendered: Dict[str, Any] = {"widgets": [_widget_to_chat(w) for w in widgets]}
        if section.get("header"):
            rendered["header"] = str(section["header"])
        sections.append(rendered)

    card: Dict[str, Any] = {"sections": sections}
    header = card_spec.get("header")
    if header:
        if not isinstance(header, dict):
            raise ValueError("card.header must be an object")
        rendered_header: Dict[str, Any] = {
            "title": _required_str(header, "title", "card.header")
        }
        if header.get("subtitle"):
            rendered_header["subtitle"] = str(header["subtitle"])
        if header.get("image_url"):
            rendered_header["imageUrl"] = str(header["image_url"])
            rendered_header["imageType"] = str(header.get("image_type") or "SQUARE")
        if header.get("image_alt_text"):
            rendered_header["imageAltText"] = str(header["image_alt_text"])
        card["header"] = rendered_header

    return {"cardId": str(card_spec.get("card_id") or "hermes-card"), "card": card}


class _ThreadCountStore:
    """Per-(chat_id, thread_name) inbound message counter, persisted to disk.

    Drives the DM main-flow vs side-thread heuristic:

    - prev_count == 0 (first time we see this thread) → "main flow":
      Google Chat just auto-created a fresh thread for the user's
      top-level message. Treat it as part of the shared DM session;
      bot replies at top-level (no thread.name on outbound).
    - prev_count >= 1 (we've already seen this thread) → "side thread":
      user explicitly engaged a thread that's been around. Isolate
      session by thread, route bot reply into the same thread.

    Persistence is essential: without it, every gateway restart wipes
    counts and active side-threads silently demote to "main flow",
    which leaks main-flow context into the user's isolated thread
    (the bug Ramón reported across 4 iterations of the in-memory
    version).

    File format (JSON):
        {"<chat_id>": {"<thread_name>": <int_count>, ...}, ...}

    Failure modes are non-fatal: a missing or corrupt file resets to
    empty (logged as warning) so the adapter never crashes on disk
    issues. The next ``incr`` will write a fresh file.

    Save strategy: write-through after every ``incr``. The file is
    tiny (a few KB even for very active bots), so the simplicity of
    write-through outweighs the cost of debouncing for now.
    """

    def __init__(self, path: _Path):
        self._path = path
        self._counts: Dict[str, Dict[str, int]] = {}
        self._loaded = False

    def load(self) -> None:
        """Load counts from disk. Safe to call multiple times.

        Missing file → empty store. Corrupt JSON → empty store + warn.
        """
        self._loaded = True
        if not self._path.exists():
            self._counts = {}
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            logger.warning(
                "[GoogleChat] thread-count store at %s is corrupt; "
                "starting fresh: %s",
                self._path, exc,
            )
            self._counts = {}
            return
        except OSError as exc:
            logger.warning(
                "[GoogleChat] could not read thread-count store at %s: %s",
                self._path, exc,
            )
            self._counts = {}
            return
        # Validate shape — anything off-schema gets dropped silently.
        clean: Dict[str, Dict[str, int]] = {}
        if isinstance(data, dict):
            for chat_id, threads in data.items():
                if not isinstance(chat_id, str) or not isinstance(threads, dict):
                    continue
                clean_threads: Dict[str, int] = {}
                for thread_name, count in threads.items():
                    if isinstance(thread_name, str) and isinstance(count, int):
                        clean_threads[thread_name] = count
                if clean_threads:
                    clean[chat_id] = clean_threads
        self._counts = clean

    def get(self, chat_id: str, thread_name: str) -> int:
        """Return the current count for (chat_id, thread_name), or 0."""
        return self._counts.get(chat_id, {}).get(thread_name, 0)

    def incr(self, chat_id: str, thread_name: str) -> int:
        """Increment count and write through to disk. Returns the
        PRE-increment value (the heuristic input — "have we seen this
        thread before this message?")."""
        chat_counts = self._counts.setdefault(chat_id, {})
        prev = chat_counts.get(thread_name, 0)
        chat_counts[thread_name] = prev + 1
        self._save()
        return prev

    def _save(self) -> None:
        """Atomic write of the counts dict to disk.

        Failure is non-fatal — log warning and continue. The in-memory
        counts stay consistent within the running process; only restart
        recovery is affected.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._counts, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning(
                "[GoogleChat] could not persist thread-count store to %s: %s",
                self._path, exc,
            )


class GoogleChatAdapter(BasePlatformAdapter):
    """
    Google Chat bot adapter using Pub/Sub pull + Chat REST API.

    Required environment (see gateway/config.py Google Chat block):
      GOOGLE_CHAT_PROJECT_ID           (or GOOGLE_CLOUD_PROJECT fallback)
      GOOGLE_CHAT_SUBSCRIPTION_NAME    (or GOOGLE_CHAT_SUBSCRIPTION fallback)
      GOOGLE_CHAT_SERVICE_ACCOUNT_JSON (or GOOGLE_APPLICATION_CREDENTIALS)

    Optional:
      GOOGLE_CHAT_ALLOWED_USERS, GOOGLE_CHAT_ALLOW_ALL_USERS
      GOOGLE_CHAT_HOME_CHANNEL
      GOOGLE_CHAT_MAX_MESSAGES (FlowControl, default 1)
      GOOGLE_CHAT_MAX_BYTES    (FlowControl, default 16_777_216 = 16 MiB)
    """

    MAX_MESSAGE_LENGTH = _MAX_TEXT_LENGTH
    # Pub/Sub supervisor configuration.
    _MAX_RECONNECT_ATTEMPTS = 10
    _RECONNECT_BASE_DELAY = 2.0
    _RECONNECT_MAX_DELAY = 120.0

    def __init__(self, config: PlatformConfig):
        # ``Platform("google_chat")`` resolves via ``_missing_()`` → pseudo-member
        # cached in ``_value2member_map_``.  We deliberately do NOT add an enum
        # attribute to ``gateway.config.Platform`` — bundled platform plugins
        # are looked up by value, not attribute (matches Teams, IRC).
        super().__init__(config, Platform("google_chat"))
        # Trigger the deferred google-cloud + googleapiclient import here so
        # that any code path which constructs the adapter and then calls
        # methods directly (notably the test suite, which builds an adapter
        # and invokes ``_send_file`` / ``_create_message`` / etc. without
        # going through ``connect()``) sees real classes for ``MediaFileUpload``,
        # ``service_account``, ``HttpError``, and friends. The module-level
        # globals were previously eager-imported; making this lazy saved
        # ~110ms / ~33MB on every CLI invocation. Idempotent — pays the cost
        # exactly once per process.
        _load_google_modules()
        self._subscriber: Optional[Any] = None
        self._chat_api: Optional[Any] = None
        # User-authed Chat API client built lazily from the OAuth refresh
        # token persisted by the plugin's ``oauth.py`` helper. Required for
        # native ``media.upload`` (bot identity is rejected by that
        # endpoint).
        #
        # Multi-user mode: each user runs ``/setup-files`` ONCE in their
        # own DM and the resulting refresh token is stored under their
        # email. ``_send_file`` looks up the requesting user's email via
        # ``_last_sender_by_chat`` and uses THAT user's token, so when
        # User B asks for a file in B's DM the bot uploads as B (not as
        # whoever first set up files long ago).
        #
        # ``_user_credentials`` / ``_user_chat_api`` keep their old names
        # but now hold the LEGACY single-user token (if any) — used as a
        # last-ditch fallback when the requesting user has no per-user
        # token yet. Pre-multi-user installs continue to work unchanged.
        self._user_chat_api: Optional[Any] = None
        self._user_credentials: Optional[Any] = None
        # Per-email caches. Populated lazily by ``_get_user_chat_for_chat``.
        self._user_creds_by_email: Dict[str, Any] = {}
        self._user_chat_api_by_email: Dict[str, Any] = {}
        # chat_id → most-recent inbound sender's email. Populated in
        # ``_build_message_event`` whenever the inbound event carries a
        # non-empty ``sender.email``. Drives the per-user token lookup
        # in ``_send_file`` so the bot uploads as the user who triggered
        # the request, not as some other authorized user.
        self._last_sender_by_chat: Dict[str, str] = {}
        self._credentials: Optional[Any] = None
        self._project_id: Optional[str] = None
        self._subscription_path: Optional[str] = None
        self._streaming_pull_future: Optional[Any] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        self._chat_queue: Optional[DurableChatEventQueue] = None
        self._chat_queue_drain_task: Optional[asyncio.Task] = None
        self._chat_queue_wakeup: Optional[asyncio.Event] = None
        self._chat_queue_worker_id = DurableChatEventQueue.worker_id()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._bot_user_id: Optional[str] = None  # users/{id}
        self._dedup = MessageDeduplicator()
        self._pubsub_ack = PubSubAckCoordinator(self._dedup)
        self._typing_messages: Dict[str, str] = {}
        self._clarify_state: Dict[str, str] = {}
        self._shutting_down = False
        self._rate_limit_hits: Dict[str, int] = {}
        # In-flight Chat turns, keyed by (chat_id, thread_id). /stop cancels
        # the task and fails the linked job. It does not open a new job.
        self._gateway_turns: Dict[tuple, Dict[str, Any]] = {}
        # Messages that arrived while this session was busy. Drained after
        # the session guard is free. They do not interrupt the running job.
        self._robie_deferred: Dict[str, list] = {}
        self._robie_deferred_drains: Dict[str, asyncio.Task] = {}
        self._robie_deferred_release_ids: set = set()
        # Last-seen inbound thread name per chat_id (space). Google Chat
        # DMs create a NEW thread per top-level user message but the user
        # views them as one logical conversation. We:
        #   (a) drop thread_id from the source for DMs (so session_key
        #       stays stable across top-level messages — see
        #       gateway/session.py:build_session_key).
        #   (b) cache the most recent inbound thread name here so outbound
        #       replies still land in the right visual thread without
        #       re-coupling sessions to threads.
        self._last_inbound_thread: Dict[str, str] = {}
        # Job currently handling this space, so thinking/clarify/status
        # sends can find the job when the gateway omits robie_job_id.
        self._active_chat_job: Dict[str, str] = {}
        # Inbound message name → thread.name when the user replied inside
        # a thread that already had messages (not a brand-new top-level).
        self._reply_in_existing_thread: Dict[str, str] = {}
        # Inbound message count per (chat_id, thread_name). Drives the
        # DM main-flow vs side-thread heuristic in _build_message_event
        # and the outbound thread routing in _resolve_thread_id.
        # Persisted to ${HERMES_HOME}/google_chat_thread_counts.json so
        # active side-threads survive gateway restarts (the bug that
        # made the in-memory version of this heuristic flaky for
        # multi-restart sessions).
        try:
            from hermes_constants import get_hermes_home as _get_hermes_home
            _hermes_home = _get_hermes_home()
        except (ModuleNotFoundError, ImportError):
            _hermes_home = _Path.home() / ".hermes"
        self._thread_count_store = _ThreadCountStore(
            _hermes_home / "google_chat_thread_counts.json"
        )
        # In-flight typing-card creates per chat_id. send_typing() reserves
        # an Event here BEFORE starting the API call so concurrent calls
        # from base.py's _keep_typing wait instead of duplicating cards.
        # Cleared in the create_and_record finally.
        self._typing_card_inflight: Dict[str, asyncio.Event] = {}
        # Orphaned typing cards (created by background tasks that lost a
        # race with send() / another concurrent create). Cleaned up at
        # end-of-turn by on_processing_complete via patch-to-empty so
        # they don't sit in the chat forever as "Hermes is thinking…".
        self._orphan_typing_messages: Dict[str, List[str]] = {}
        # FlowControl knobs (env-configurable).
        try:
            self._max_messages = int(os.getenv("GOOGLE_CHAT_MAX_MESSAGES", "1"))
        except (ValueError, TypeError):
            self._max_messages = 1
        try:
            self._max_bytes = int(os.getenv("GOOGLE_CHAT_MAX_BYTES", str(16 * 1024 * 1024)))
        except (ValueError, TypeError):
            self._max_bytes = 16 * 1024 * 1024
        self._http_events_url = (
            self.config.extra.get("http_events_url")
            or os.getenv("GOOGLE_CHAT_HTTP_EVENTS_URL", "")
            or ""
        ).strip()
        self._http_events_audience = (
            self.config.extra.get("http_events_audience")
            or os.getenv("GOOGLE_CHAT_HTTP_EVENTS_AUDIENCE", "")
            or self._http_events_url
        ).strip()
        self._http_events_service_account_email = (
            self.config.extra.get("http_events_service_account_email")
            or os.getenv("GOOGLE_CHAT_HTTP_EVENTS_SERVICE_ACCOUNT_EMAIL", "")
            or ""
        ).strip().lower()

    # ------------------------------------------------------------------
    # Configuration loading and validation
    # ------------------------------------------------------------------
    def _load_sa_credentials(self) -> Any:
        """Load Service Account credentials from env or config.extra,
        falling back to Application Default Credentials.

        Priority:
          1. Explicit ``extra['service_account_json']`` (path or inline JSON)
          2. ``GOOGLE_APPLICATION_CREDENTIALS`` env var (path)
          3. Application Default Credentials via ``google.auth.default()``
             — works on Cloud Run / GCE / GKE with a workload identity
             attached, or locally via ``gcloud auth application-default
             login``. Lets operators run the gateway in GCP without
             managing SA key files. Pattern lifted from PR #14965.
        """
        sa_path = (
            self.config.extra.get("service_account_json")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )
        if sa_path:
            # Inline JSON (rare, but supported).
            if sa_path.lstrip().startswith("{"):
                try:
                    info = json.loads(sa_path)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Inline SA JSON is not valid JSON: {exc}"
                    ) from exc
                return service_account.Credentials.from_service_account_info(
                    info, scopes=_CHAT_SCOPES
                )
            if not os.path.exists(sa_path):
                raise FileNotFoundError(
                    "Service Account JSON file not found at configured path."
                )
            # Validate file parses before handing to google-auth for nicer error.
            try:
                with open(sa_path, "r", encoding="utf-8") as fh:
                    info = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Service Account JSON file is not valid JSON: {exc}"
                ) from exc
            return service_account.Credentials.from_service_account_info(
                info, scopes=_CHAT_SCOPES
            )

        # No explicit SA configured — try ADC. This is the Cloud Run / GCE
        # path; google-auth picks up the workload identity automatically.
        try:
            import google.auth as google_auth
        except ImportError:
            google_auth = None  # type: ignore[assignment]
        if google_auth is None:
            raise ValueError(
                "No Service Account credentials configured. Set "
                "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS, "
                "or install google-auth to use Application Default Credentials."
            )
        try:
            credentials, _project = google_auth.default(scopes=_CHAT_SCOPES)
        except Exception as exc:
            raise ValueError(
                "No Service Account credentials configured and Application "
                "Default Credentials are unavailable. Set "
                "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON or run "
                "``gcloud auth application-default login``. "
                f"ADC error: {exc}"
            ) from exc
        logger.info(
            "[GoogleChat] No SA JSON configured; using Application "
            "Default Credentials"
        )
        return credentials

    def _validate_config(self) -> Tuple[str, Optional[str]]:
        """Return (project_id, subscription_path) after validation.

        ``subscription_path`` is ``None`` for HTTP-inbound deployments. Raises
        ValueError with a sanitized message on any config problem.
        """
        project_id = (self.config.extra.get("project_id") or "").strip()
        subscription = (self.config.extra.get("subscription_name") or "").strip()
        http_events_url = (self.config.extra.get("http_events_url") or "").strip()

        if subscription:
            match = _SUBSCRIPTION_PATH_RE.match(subscription)
            if not match:
                raise ValueError(
                    "GOOGLE_CHAT_SUBSCRIPTION_NAME must match "
                    "'projects/<project>/subscriptions/<sub>'."
                )
            subscription_project = match.group("project")
            if project_id and subscription_project != project_id:
                raise ValueError(
                    "project_id in GOOGLE_CHAT_PROJECT_ID does not match the "
                    "project embedded in GOOGLE_CHAT_SUBSCRIPTION_NAME."
                )
            return project_id or subscription_project, subscription

        if http_events_url:
            return project_id, None

        if not project_id:
            raise ValueError(
                "GOOGLE_CHAT_PROJECT_ID (or GOOGLE_CLOUD_PROJECT) is not set."
            )
        raise ValueError(
            "GOOGLE_CHAT_SUBSCRIPTION_NAME (or GOOGLE_CHAT_SUBSCRIPTION) is not set. "
            "Set GOOGLE_CHAT_HTTP_EVENTS_URL for HTTP callback mode."
        )

    # ------------------------------------------------------------------
    # Loop bridge helpers (thread -> asyncio loop)
    # ------------------------------------------------------------------
    @staticmethod
    def _log_background_failure(future: Any) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("[GoogleChat] Background inbound processing failed")

    @staticmethod
    def _loop_accepts_callbacks(loop: Optional[asyncio.AbstractEventLoop]) -> bool:
        return loop is not None and not bool(getattr(loop, "is_closed", lambda: False)())

    def _submit_on_loop(self, coro: Any) -> Any:
        """Schedule a coroutine on the adapter loop from a Pub/Sub callback thread."""
        loop = self._loop
        if not self._loop_accepts_callbacks(loop):
            if asyncio.iscoroutine(coro):
                coro.close()
            logger.warning("[GoogleChat] Loop not accepting callbacks; retaining event for retry")
            return None
        try:
            from agent.async_utils import safe_schedule_threadsafe
            return safe_schedule_threadsafe(
                coro, loop,
                logger=logger,
                log_message="[GoogleChat] Failed to schedule background callback",
                log_level=logging.WARNING,
            )
        except RuntimeError:
            if asyncio.iscoroutine(coro):
                coro.close()
            logger.warning("[GoogleChat] Loop closed between check and submit")
            return None

    def _schedule_pubsub_processing(
        self, coro: Any, message: Any, msg_name: str = ""
    ) -> None:
        def report_failure(exc: BaseException) -> None:
            try:
                logger.error(
                    "[GoogleChat] Pub/Sub handoff or settlement failed: %s",
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            except Exception:
                # Logging must never prevent nack. journald backpressure
                # after a HITL RuntimeError storm already silenced
                # hermes-poc-01 while the process stayed active.
                pass

        try:
            self._pubsub_ack.schedule(
                coro=coro,
                message=message,
                message_id=msg_name,
                submit=self._submit_on_loop,
                on_error=report_failure,
            )
        except Exception:
            logger.exception("[GoogleChat] Pub/Sub schedule failed")
            try:
                message.nack()
            except Exception:
                pass

    def _durable_chat_queue(self) -> DurableChatEventQueue:
        if self._chat_queue is None:
            self._chat_queue = DurableChatEventQueue(ROBIE_JOB_DB)
        return self._chat_queue

    def _ensure_chat_queue_drain(self) -> None:
        """Keep one restart-safe executable-work consumer on the event loop."""
        if self._shutting_down:
            return
        if self._chat_queue_wakeup is None:
            self._chat_queue_wakeup = asyncio.Event()
        if self._chat_queue_drain_task is None or self._chat_queue_drain_task.done():
            self._chat_queue_drain_task = asyncio.create_task(
                self._drain_chat_queue(), name="robie-google-chat-job-queue"
            )

    async def _enqueue_bounded_chat_job(
        self,
        event: MessageEvent,
        job_id: str,
        *,
        related_only: bool,
    ) -> bool:
        """Commit bounded work before allowing the inbound message to ACK."""
        job = await asyncio.to_thread(JobStore(ROBIE_JOB_DB).get_job, job_id)
        if job["action_type"] not in BOUNDED_ENGINE_ACTIONS:
            return False
        source = event.source
        conversation_id = getattr(source, "chat_id", None) or "google-chat:unknown"
        message_id = event.message_id or f"job:{job_id}"
        thread_id = getattr(source, "thread_id", None)
        payload = {
            "job_id": job_id,
            "action_type": job["action_type"],
            "request_sha256": hashlib.sha256(
                str(event.text or "").encode("utf-8")
            ).hexdigest(),
            "conversation_id": conversation_id,
            "message_id": message_id,
            "thread_id": thread_id,
        }
        queue = await asyncio.to_thread(self._durable_chat_queue)
        queued = await asyncio.to_thread(
            queue.enqueue,
            event_id=message_id,
            conversation_id=conversation_id,
            message_id=message_id,
            payload=payload,
            job_id=job_id,
        )
        await asyncio.to_thread(
            queue.link_conversation_job,
            conversation_id=conversation_id,
            job_id=job_id,
            message_id=message_id,
            event_id=message_id,
            relation="CORRECTION" if related_only else "CREATED",
        )
        logger.info(
            "[GoogleChat] durable executable handoff event=%s job=%s duplicate=%s",
            message_id,
            job_id,
            queued["duplicate"],
        )
        self._ensure_chat_queue_drain()
        if self._chat_queue_wakeup is not None:
            self._chat_queue_wakeup.set()
        return True

    def _fail_queued_job(self, job_id: str, error: str) -> None:
        store = JobStore(ROBIE_JOB_DB)
        job = store.get_job(job_id)
        status = JobStatus(job["status"])
        if status in TERMINAL_STATUSES or status in WAITING_STATUSES:
            return
        store.transition(
            job_id,
            JobStatus.FAILED,
            expected={JobStatus.PENDING, JobStatus.RUNNING, JobStatus.VERIFYING},
            error=f"durable Chat worker failed: {redact_text(error)}",
            release_lease=True,
        )

    async def _maintain_chat_queue_lease(
        self,
        queue: DurableChatEventQueue,
        event_id: str,
        lease_seconds: int,
    ) -> None:
        """Heartbeat long Playwright work so another worker cannot overlap it."""
        interval = max(5.0, min(float(lease_seconds) / 3.0, 30.0))
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(
                queue.renew_lease,
                event_id,
                self._chat_queue_worker_id,
                lease_seconds=lease_seconds,
            )

    async def _maintain_generic_chat_job_heartbeat(self, job_id: str) -> None:
        """Keep a live generic Chat Job from being orphan-failed at 300s.

        The write goes through Job Engine so it always uses the same jobs.db
        as ``fail_orphaned_chat_jobs``. The first commit is synchronous; a
        process-local thread continues on a 30s cadence. This is a backup
        for the ``open_chat_job`` / ``build_chat_execution_text`` hooks —
        those run even when hermes-gateway loads a stale plugin adapter.
        """
        await asyncio.to_thread(start_generic_chat_job_heartbeat, ROBIE_JOB_DB, job_id)

    async def _resume_direct_generic_chat_job(
        self,
        event: MessageEvent,
        job_id: str,
        message_id: str,
        text: str,
    ) -> None:
        """Re-open a HITL-resumed generic Chat Job and start the worker."""
        attachment_kwargs = self._chat_job_attachment_kwargs(event)
        reopened = await asyncio.to_thread(
            open_chat_job,
            ROBIE_JOB_DB,
            message_id,
            text,
            attachments=attachment_kwargs["attachments"],
            requested_by=(
                getattr(event.source, "user_name", None)
                or getattr(event.source, "user_id", None)
                or "Google Chat user"
            ),
            conversation_id=getattr(event.source, "chat_id", None),
            expected_attachment_count=attachment_kwargs["expected_attachment_count"],
            attachment_refs=attachment_kwargs["attachment_refs"],
            drive_port=attachment_kwargs["drive_port"],
        )
        job_id = reopened or job_id
        queue = await asyncio.to_thread(self._durable_chat_queue)
        await asyncio.to_thread(
            queue.link_conversation_job,
            conversation_id=getattr(event.source, "chat_id", None)
            or "google-chat:unknown",
            job_id=job_id,
            message_id=message_id,
            event_id=message_id,
            relation="CONTINUATION",
        )
        if await self._halt_failed_drive_ingestion(
            event, job_id, attachment_kwargs["attachment_refs"]
        ):
            return
        if await self._halt_action_gate_refuse(event, job_id):
            return
        if await self._halt_retry_refusal(event, job_id, text):
            return
        store = JobStore(ROBIE_JOB_DB)
        job = await asyncio.to_thread(store.get_job, job_id)
        original = str((job.get("payload") or {}).get("text") or text)
        execution_text = build_chat_execution_text(ROBIE_JOB_DB, job_id, original)
        try:
            event.text = execution_text
        except Exception:
            from dataclasses import replace

            event = replace(event, text=execution_text)
        await self._run_generic_chat_job(job_id, event)

    async def _run_generic_chat_job(
        self, job_id: str | None, event: MessageEvent
    ) -> None:
        """Run Hermes Chat work while heartbeating the unleased Job ledger row."""
        from robie_job_engine.chat_guard import require_message_execution_available

        # A retryable exception leaves the durable event available after restart.
        await asyncio.to_thread(require_message_execution_available, ROBIE_JOB_DB)
        if job_id:
            await self._bind_inbound_job_thread(event, job_id)
        if job_id:
            from robie_job_engine.chat_hitl import run_chat_hitl_coverage_resume
            from robie_job_engine.policy_setup_dispatch import is_coverage_fill_miss

            hitl_text = await asyncio.to_thread(
                run_chat_hitl_coverage_resume, ROBIE_JOB_DB, job_id
            )
            if hitl_text is not None:
                # Missing-letter HITL is already posted. Success still needs
                # a Chat reply; send() runs guard_chat_response.
                if (
                    event.source is not None
                    and not is_coverage_fill_miss(hitl_text)
                ):
                    await self.send(
                        event.source.chat_id,
                        hitl_text,
                        reply_to=event.message_id,
                        metadata={
                            "thread_id": getattr(event.source, "thread_id", None)
                        },
                    )
                return
        if job_id and not chat_hermes_should_run(ROBIE_JOB_DB, job_id):
            # Fail-closed policy setup already parked HITL. Do not start a
            # google_chat_task worker that would sit in RUNNING / still working.
            await self._send_clarification_if_needed(job_id, event)
            return
        if not job_id:
            await self._begin_fresh_chat_turn(event)
            await self.handle_message(event)
            return
        try:
            from robie_job_engine.playwright_observability import bind_current_playwright_job

            bind_current_playwright_job(ROBIE_JOB_DB, job_id)
        except Exception:
            pass
        await self._maintain_generic_chat_job_heartbeat(job_id)
        from robie_job_engine.write_verification_loop import prepare_chat_write_plan

        # A write states its plan before Hermes acts. Questions skip this.
        await asyncio.to_thread(prepare_chat_write_plan, ROBIE_JOB_DB, job_id)
        await self._run_gateway_turn_with_ceiling(job_id, event)

    async def _send_clarification_if_needed(self, job_id: str, event: MessageEvent) -> None:
        """One plain-English question. Does not start the agent."""
        store = JobStore(ROBIE_JOB_DB)
        job = await asyncio.to_thread(store.get_job, job_id)
        if JobStatus(job["status"]) != JobStatus.NEEDS_CLARIFICATION:
            return
        note = await asyncio.to_thread(store.get_checkpoint, job_id, "clarification")
        if not note:
            return
        from robie_job_engine.answer_only import CLARIFICATION_QUESTION

        if event.source is None:
            return
        await self.send(
            event.source.chat_id,
            f"{CLARIFICATION_QUESTION}\n\nRef: job {job_id}",
            reply_to=event.message_id,
            metadata={"thread_id": getattr(event.source, "thread_id", None)},
        )

    async def _fail_jobs_abandoned_by_restart(self) -> None:
        """The previous process is gone. A leftover heartbeat is not a live job."""
        def _fail() -> None:
            from robie_job_engine.store import JobStore

            active = {
                str(item.get("job_id"))
                for item in self._gateway_turns.values()
                if item.get("job_id")
            }
            JobStore(ROBIE_JOB_DB).fail_gateway_restart_orphans(exclude=active)

        try:
            await asyncio.to_thread(_fail)
        except Exception:
            logger.exception("[GoogleChat] could not fail jobs abandoned by restart")

    async def _terminate_running_agent(
        self,
        event: MessageEvent,
        job_id: str | None,
        *,
        reason: str,
    ) -> None:
        """Cancel the background agent task and kill its browser processes."""
        from robie_job_engine.chat_turn_control import terminate_gateway_agent

        await terminate_gateway_agent(
            self,
            event,
            job_id,
            reason=reason,
            store=JobStore(ROBIE_JOB_DB),
        )

    async def _run_gateway_turn_with_ceiling(self, job_id: str, event: MessageEvent) -> None:
        """Start one Chat turn and return so the next message can be read.

        ``handle_message`` starts the agent and returns. The 10 minute ceiling
        watches that agent from a background task. Waiting here would hold the
        gateway's single Chat slot, so /stop could not arrive until the job
        ended. This does not raise GOOGLE_CHAT_MAX_MESSAGES.
        """
        from robie_job_engine.chat_turn_control import (
            _abandon_timed_out_gateway_turn,
            gateway_max_turn_seconds,
            running_agent_task,
            turn_key,
            watch_turn_ceiling,
        )

        source = event.source
        key = turn_key(
            getattr(source, "chat_id", None),
            getattr(source, "thread_id", None),
        )
        before = set(getattr(self, "_background_tasks", set()) or ())
        self._gateway_turns[key] = {
            "task": None,
            "job_id": job_id,
            "watchdog": None,
        }
        limit = gateway_max_turn_seconds()
        try:
            from robie_job_engine.chat_turn_control import chat_turn_keeps_context

            if not chat_turn_keeps_context(ROBIE_JOB_DB, job_id):
                await self._begin_fresh_chat_turn(event)
            await self.handle_message(event)
        except Exception:
            current = self._gateway_turns.get(key)
            if current and current.get("job_id") == job_id:
                self._gateway_turns.pop(key, None)
            raise
        agent = running_agent_task(self, event, before=before)
        current = self._gateway_turns.get(key)
        if not current or current.get("job_id") != job_id:
            return

        def stop_requested() -> bool:
            record = self._gateway_turns.get(key)
            return not record or record.get("job_id") != job_id

        async def on_timeout() -> None:
            if stop_requested():
                return
            self._gateway_turns.pop(key, None)
            await self._terminate_running_agent(
                event, job_id, reason="gateway_max_turn_seconds"
            )
            reply = await asyncio.to_thread(
                _abandon_timed_out_gateway_turn,
                JobStore(ROBIE_JOB_DB),
                job_id,
                seconds=limit,
            )
            if source is not None:
                from robie_job_engine.chat_thread import read_job_chat_thread

                stored_thread = await asyncio.to_thread(
                    read_job_chat_thread, JobStore(ROBIE_JOB_DB), job_id
                )
                await self.send(
                    source.chat_id,
                    reply,
                    reply_to=event.message_id,
                    metadata={
                        "thread_id": stored_thread or getattr(source, "thread_id", None),
                        "robie_stop_notice": True,
                        "robie_delivery_kind": "ceiling",
                        "robie_job_id": job_id,
                    },
                )

        current["task"] = agent
        watchdog = asyncio.create_task(
            watch_turn_ceiling(
                agent,
                limit=limit,
                on_timeout=on_timeout,
                stop_requested=stop_requested,
            ),
            name=f"robie-turn-ceiling:{job_id}",
        )
        current["watchdog"] = watchdog

    async def _begin_fresh_chat_turn(self, event: MessageEvent) -> None:
        """Drop prior Q&A so this message is answered on its own."""
        from robie_job_engine.chat_turn_control import fresh_turn_history

        try:
            fresh_turn_history(self, event)
        except Exception:
            logger.exception("[GoogleChat] could not close session history")

    async def _apply_chat_stop(self, event: MessageEvent) -> None:
        """Skip job creation, stop the running agent, fail the linked job."""
        from robie_job_engine.chat_turn_control import (
            NOTHING_RUNNING_REPLY,
            fail_cancelled_chat_job,
            resolve_stop_target,
            session_is_busy,
            turn_key,
        )

        source = event.source
        if source is None:
            return
        key = turn_key(source.chat_id, getattr(source, "thread_id", None))
        record = self._gateway_turns.pop(key, None)
        if record is None:
            record = self._gateway_turns.pop((source.chat_id, ""), None)
        job_id, idle_reply = resolve_stop_target(
            record, session_busy=session_is_busy(self, event)
        )
        from robie_job_engine.chat_job_controls import waiting_jobs_for_requester
        from robie_job_engine.chat_thread import read_job_chat_thread

        requester = (
            getattr(source, "user_name", None)
            or getattr(source, "user_id", None)
            or ""
        )
        thread_id = getattr(source, "thread_id", None)
        waiting_ids = await asyncio.to_thread(
            waiting_jobs_for_requester, ROBIE_JOB_DB, source.chat_id, requester
        )
        store = JobStore(ROBIE_JOB_DB)
        in_this_thread = []
        for waiting_id in waiting_ids:
            stored = await asyncio.to_thread(read_job_chat_thread, store, waiting_id)
            if thread_id and stored == thread_id:
                in_this_thread.append(waiting_id)
        # Top-level /stop cancels every waiting job for this person in the
        # space. A /stop inside one thread cancels that thread's job.
        cancel_ids = in_this_thread if thread_id and in_this_thread else waiting_ids
        if not thread_id:
            cancel_ids = waiting_ids
        for waiting_id in cancel_ids:
            await asyncio.to_thread(fail_cancelled_chat_job, store, waiting_id)
        if cancel_ids:
            job_id = job_id or cancel_ids[0]
            idle_reply = None
        if idle_reply:
            await self.send(
                source.chat_id,
                idle_reply,
                reply_to=event.message_id,
                metadata={
                    "thread_id": getattr(source, "thread_id", None),
                    "robie_stop_notice": True,
                    "robie_delivery_kind": "idle_stop",
                },
            )
            return
        await self._terminate_running_agent(event, job_id, reason="/stop")
        task = (record or {}).get("task")
        if task is not None and task is not asyncio.current_task():
            cancel_task = getattr(task, "cancel", None)
            if callable(cancel_task) and not getattr(task, "done", lambda: False)():
                cancel_task()
        watchdog = (record or {}).get("watchdog")
        if (
            watchdog is not None
            and watchdog is not asyncio.current_task()
            and not getattr(watchdog, "done", lambda: True)()
        ):
            watchdog.cancel()
        if job_id and job_id not in cancel_ids:
            reply = await asyncio.to_thread(
                fail_cancelled_chat_job, JobStore(ROBIE_JOB_DB), job_id
            )
        elif cancel_ids:
            from robie_job_engine.chat_turn_control import stop_reply_line

            reply = stop_reply_line(cancel_ids[0])
        else:
            reply = NOTHING_RUNNING_REPLY
        stop_thread = getattr(source, "thread_id", None)
        if cancel_ids:
            stop_thread = (
                await asyncio.to_thread(read_job_chat_thread, store, cancel_ids[0])
                or stop_thread
            )
        await self.send(
            source.chat_id,
            reply,
            reply_to=event.message_id,
            metadata={
                "thread_id": stop_thread,
                "robie_stop_notice": True,
                "robie_delivery_kind": "stop",
                "robie_job_id": job_id,
            },
        )

    @staticmethod
    async def _stop_chat_queue_heartbeat(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _drain_chat_queue(self) -> None:
        """Execute committed bounded Jobs outside the Pub/Sub ACK coroutine."""
        from robie_job_engine.chat_guard import require_message_execution_available
        queue = await asyncio.to_thread(self._durable_chat_queue)
        lease_seconds = max(60, int(os.getenv("ROBIE_CHAT_QUEUE_LEASE_SECONDS", "1800")))
        while not self._shutting_down:
            item = await asyncio.to_thread(
                queue.claim_next,
                self._chat_queue_worker_id,
                lease_seconds=lease_seconds,
            )
            if item is None:
                # Sleep until an in-process enqueue arrives, with a bounded
                # periodic recovery check for rows committed before restart.
                if self._chat_queue_wakeup is None:
                    self._chat_queue_wakeup = asyncio.Event()
                self._chat_queue_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._chat_queue_wakeup.wait(), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            event_id = item["event_id"]
            payload = item["payload"]
            job_id = item.get("job_id") or payload.get("job_id")
            heartbeat = asyncio.create_task(
                self._maintain_chat_queue_lease(queue, event_id, lease_seconds),
                name=f"robie-chat-lease:{event_id}",
            )
            try:
                if not job_id:
                    raise RuntimeError("queued Chat event has no bound Job")
                await asyncio.to_thread(require_message_execution_available, ROBIE_JOB_DB)
                store = JobStore(ROBIE_JOB_DB)
                await asyncio.to_thread(store.wake_due)
                handled = await asyncio.to_thread(
                    maybe_run_bounded_job, ROBIE_JOB_DB, job_id
                )
                if not handled:
                    raise RuntimeError("queued Job is not a bounded executable action")
                job = await asyncio.to_thread(store.get_job, job_id)
                status = JobStatus(job["status"])
                if status == JobStatus.RETRY_WAIT:
                    await self._stop_chat_queue_heartbeat(heartbeat)
                    await asyncio.to_thread(
                        queue.defer,
                        event_id,
                        self._chat_queue_worker_id,
                        available_at=job["next_wakeup_at"],
                        error=job.get("last_error") or "bounded Job scheduled a retry",
                    )
                    continue
                if status in {
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.VERIFYING,
                }:
                    retry_at = job.get("lease_expires_at") or (
                        datetime.now(timezone.utc) + timedelta(seconds=5)
                    ).isoformat()
                    await self._stop_chat_queue_heartbeat(heartbeat)
                    await asyncio.to_thread(
                        queue.defer_until_available,
                        event_id,
                        self._chat_queue_worker_id,
                        available_at=retry_at,
                        error="durable Job is still owned by an active worker",
                    )
                    continue
                if status == JobStatus.AWAITING_HUMAN_INPUT:
                    job_payload = dict(job.get("payload") or {})
                    interaction = interaction_for_blocker(
                        job.get("last_error") or "PLAYWRIGHT_BLOCKED",
                        action_type=job["action_type"],
                        requester_name=(
                            job_payload.get("requested_by")
                            or payload.get("requested_by")
                            or payload.get("sender_name")
                        ),
                        job_id=job_id,
                        subject_name=(
                            job_payload.get("company_name")
                            or job_payload.get("client_name")
                            or job_payload.get("account_name")
                        ),
                    )
                    await self._stop_chat_queue_heartbeat(heartbeat)
                    await asyncio.to_thread(
                        queue.await_human_input,
                        event_id,
                        self._chat_queue_worker_id,
                        interaction_state=interaction,
                        error=job.get("last_error") or "PLAYWRIGHT_BLOCKED",
                    )
                    sent = await self.send(
                        payload["conversation_id"],
                        interaction["prompt"],
                        reply_to=payload.get("message_id"),
                        metadata={"thread_id": payload.get("thread_id")},
                    )
                    if not sent.success:
                        raise RuntimeError(
                            "human-input prompt could not be posted: "
                            f"{sent.error or 'unknown Chat API error'}"
                        )
                    continue
                reply_to = payload.get("message_id")
                terminal_detail = "The durable background worker finished."
                if job["action_type"] == "drive.skill_sync":
                    action = await asyncio.to_thread(
                        store.get_checkpoint, job_id, "action"
                    )
                    detail = dict((action or {}).get("detail") or {})
                    terminal_detail = (
                        "Drive Skill Sync finished. "
                        f"Synced {detail.get('file_count', 0)} approved file(s) "
                        "from 01_Core_Rules and 03_Active_Skills."
                    )
                terminal_message = await asyncio.to_thread(
                    guard_chat_response,
                    ROBIE_JOB_DB,
                    job_id,
                    terminal_detail,
                )
                sent = await self.send(
                    payload["conversation_id"],
                    terminal_message,
                    reply_to=reply_to,
                    metadata={"thread_id": payload.get("thread_id")},
                )
                if not sent.success:
                    raise RuntimeError(
                        "verified terminal status could not be posted: "
                        f"{sent.error or 'unknown Chat API error'}"
                    )
                # Stop renewals before settling the lease so the heartbeat can
                # never race a successful COMPLETE transition.
                await self._stop_chat_queue_heartbeat(heartbeat)
                await asyncio.to_thread(
                    queue.complete, event_id, self._chat_queue_worker_id
                )
            except MessageMaintenanceDeferred as exc:
                await self._stop_chat_queue_heartbeat(heartbeat)
                await asyncio.to_thread(
                    queue.defer_until_available, event_id, self._chat_queue_worker_id,
                    available_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                    error=str(exc),
                )
            except asyncio.CancelledError:
                # Do not release immediately: cancelling asyncio.to_thread does
                # not stop the underlying worker thread.  Let the heartbeat
                # stop and the short lease expire so recovery cannot overlap
                # work that is still winding down.
                raise
            except Exception as exc:
                safe_error = redact_text(str(exc))
                logger.exception(
                    "[GoogleChat] durable executable worker failed event=%s job=%s",
                    event_id,
                    job_id,
                )
                try:
                    await self._stop_chat_queue_heartbeat(heartbeat)
                except Exception:
                    logger.exception(
                        "[GoogleChat] queue heartbeat failed event=%s", event_id
                    )
                if item["attempt_count"] < item.get("max_attempts", 3):
                    retry_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=min(60, 2 ** item["attempt_count"]))
                    ).isoformat()
                    try:
                        await asyncio.to_thread(
                            queue.defer,
                            event_id,
                            self._chat_queue_worker_id,
                            available_at=retry_at,
                            error=safe_error,
                        )
                        continue
                    except Exception:
                        logger.exception(
                            "[GoogleChat] could not defer queue event=%s", event_id
                        )
                if job_id:
                    try:
                        await asyncio.to_thread(
                            self._fail_queued_job, job_id, safe_error
                        )
                    except Exception:
                        logger.exception(
                            "[GoogleChat] could not fail closed queued job=%s", job_id
                        )
                try:
                    await asyncio.to_thread(
                        queue.fail,
                        event_id,
                        self._chat_queue_worker_id,
                        safe_error,
                    )
                except Exception:
                    logger.exception(
                        "[GoogleChat] could not settle failed queue event=%s", event_id
                    )
            finally:
                try:
                    await self._stop_chat_queue_heartbeat(heartbeat)
                except Exception:
                    logger.exception(
                        "[GoogleChat] queue heartbeat shutdown failed event=%s",
                        event_id,
                    )

    # ------------------------------------------------------------------
    # Bot identity resolution
    # ------------------------------------------------------------------
    def _bot_id_cache_path(self) -> _Path:
        """Location where the resolved bot user_id is cached across restarts."""
        base = os.getenv("HERMES_HOME", str(_Path.home() / ".hermes"))
        return _Path(base) / "google_chat_bot_id.json"

    def _load_cached_bot_id(self) -> Optional[str]:
        path = self._bot_id_cache_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("bot_user_id") or None
        except (OSError, json.JSONDecodeError):
            return None

    def _save_cached_bot_id(self, bot_user_id: str) -> None:
        try:
            path = self._bot_id_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"bot_user_id": bot_user_id}),
                encoding="utf-8",
            )
        except OSError:
            logger.debug("[GoogleChat] Could not persist bot_user_id cache", exc_info=True)

    async def _resolve_bot_user_id(self) -> Optional[str]:
        """Resolve ``users/{id}`` via Chat API members.list on a known space.

        Tries the home channel first, then any space from the allowlist.
        If no space is known, returns None and self-filter falls back to
        filtering ``sender.type == 'BOT'`` (which is still safe but less
        precise — own messages and other bots look alike).
        """
        candidate_spaces: List[str] = []
        if self.config.home_channel and self.config.home_channel.chat_id:
            candidate_spaces.append(self.config.home_channel.chat_id)
        # Env-configured allowed spaces (comma-separated). Optional.
        extra_spaces = os.getenv("GOOGLE_CHAT_BOOTSTRAP_SPACES", "").strip()
        if extra_spaces:
            candidate_spaces.extend(
                s.strip() for s in extra_spaces.split(",") if s.strip()
            )
        for space in candidate_spaces:
            try:
                members = await asyncio.to_thread(
                    lambda s=space: self._chat_api.spaces()
                    .members()
                    .list(parent=s, pageSize=50)
                    .execute(http=self._new_authed_http())
                )
            except HttpError as exc:
                logger.debug(
                    "[GoogleChat] members.list failed on %s: %s",
                    space,
                    _redact_sensitive(str(exc)),
                )
                continue
            for member in members.get("memberships", []):
                if member.get("member", {}).get("type") == "BOT":
                    name = member.get("member", {}).get("name")
                    if name:
                        return name
        return None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Validate config, authenticate, start Pub/Sub pull, resolve bot id."""
        self._shutting_down = False
        # First call into the heavy google-cloud stack — trigger the lazy
        # import. ``_load_google_modules()`` is idempotent and rebinds the
        # module globals (``pubsub_v1``, ``service_account``, ``HttpError``,
        # …) used throughout this file. Anything that runs *before* this
        # call would see the placeholders, so connect() is the natural
        # gate.
        if not _load_google_modules():
            self._set_fatal_error(
                code="missing_deps",
                message="google-cloud-pubsub / google-api-python-client not installed",
                retryable=False,
            )
            return False

        self._loop = asyncio.get_running_loop()
        if not is_reconnect:
            await self._fail_jobs_abandoned_by_restart()
        try:
            project_id, subscription_path = self._validate_config()
            credentials = self._load_sa_credentials()
        except (ValueError, FileNotFoundError) as exc:
            msg = _redact_sensitive(str(exc))
            logger.error("[GoogleChat] Config validation failed: %s", msg)
            self._set_fatal_error(code="config_invalid", message=msg, retryable=False)
            return False

        self._project_id = project_id
        self._subscription_path = subscription_path
        self._credentials = credentials

        # Build Chat REST client (sync; wrap calls in asyncio.to_thread).
        try:
            self._chat_api = await asyncio.to_thread(
                lambda: build_service(
                    "chat",
                    "v1",
                    credentials=credentials,
                    cache_discovery=False,
                )
            )
        except Exception as exc:
            msg = _redact_sensitive(str(exc))
            logger.error("[GoogleChat] Failed to build Chat API client: %s", msg)
            self._set_fatal_error(code="chat_api_init", message=msg, retryable=False)
            return False

        # Attempt to load LEGACY single-user OAuth credentials at startup.
        # In multi-user mode each user's token is loaded lazily by
        # ``_load_per_user_chat_api`` on first send. The legacy slot is
        # kept as a last-ditch fallback for pre-multi-user installs and
        # for groups where the asker has no per-user token yet. Failure
        # here is NON-fatal: text messaging continues to work; only
        # attachments degrade to a setup-instructions text notice.
        try:
            from .oauth import (
                load_user_credentials as _load_user_creds,
                build_user_chat_service as _build_user_chat,
                list_authorized_emails as _list_emails,
            )
            user_creds = await asyncio.to_thread(_load_user_creds)
            if user_creds is not None:
                self._user_credentials = user_creds
                self._user_chat_api = await asyncio.to_thread(
                    lambda: _build_user_chat(user_creds)
                )
                logger.info(
                    "[GoogleChat] Legacy user OAuth loaded — fallback "
                    "attachment delivery enabled"
                )
            authorized = await asyncio.to_thread(_list_emails)
            if authorized:
                logger.info(
                    "[GoogleChat] %d per-user OAuth tokens on disk: %s",
                    len(authorized), ", ".join(authorized),
                )
            elif user_creds is None:
                logger.info(
                    "[GoogleChat] No user OAuth tokens at setup — file "
                    "attachments will degrade to text-only fallback. "
                    "Each user runs /setup-files once in their own DM "
                    "to enable native attachments."
                )
        except Exception as exc:
            logger.warning(
                "[GoogleChat] User OAuth load failed (attachments will "
                "degrade to text-only fallback): %s",
                _redact_sensitive(str(exc)),
            )
            self._user_credentials = None
            self._user_chat_api = None

        # Load the persistent thread-count store so the side-thread
        # heuristic in _build_message_event survives gateway restarts.
        try:
            await asyncio.to_thread(self._thread_count_store.load)
        except Exception:
            logger.warning(
                "[GoogleChat] thread-count store load failed (treating "
                "all threads as fresh)", exc_info=True,
            )

        if subscription_path is not None:
            # Sanity check: subscription exists / SA has access.
            self._subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
            try:
                await asyncio.to_thread(
                    lambda: self._subscriber.get_subscription(
                        request={"subscription": subscription_path}
                    )
                )
            except gax_exceptions.NotFound:
                self._set_fatal_error(
                    code="subscription_not_found",
                    message="Pub/Sub subscription not found at configured path",
                    retryable=False,
                )
                return False
            except gax_exceptions.PermissionDenied:
                self._set_fatal_error(
                    code="subscription_permission",
                    message=(
                        "Service Account lacks roles/pubsub.subscriber on the "
                        "subscription"
                    ),
                    retryable=False,
                )
                return False
            except Exception as exc:
                msg = _redact_sensitive(str(exc))
                logger.error("[GoogleChat] subscription.get failed: %s", msg)
                self._set_fatal_error(code="subscription_check", message=msg, retryable=True)
                return False

        # Resolve bot user_id (eager): cache first, then members.list.
        self._bot_user_id = self._load_cached_bot_id()
        if not self._bot_user_id:
            self._bot_user_id = await self._resolve_bot_user_id()
            if self._bot_user_id:
                self._save_cached_bot_id(self._bot_user_id)
            else:
                logger.info(
                    "[GoogleChat] bot_user_id not yet resolved; "
                    "will resolve on first addedToSpace or member lookup"
                )

        if subscription_path is not None:
            # Start the supervisor task that runs the Pub/Sub pull with exponential
            # backoff + jitter on transient errors, bails out after N retries.
            self._supervisor_task = asyncio.create_task(self._run_supervisor())
            inbound = "pubsub"
        else:
            self._supervisor_task = None
            inbound = "http"

        # Recover executable Chat work that was committed before a previous
        # gateway or worker restart. This task does not delay connection.
        self._ensure_chat_queue_drain()

        self._mark_connected()
        logger.info(
            "[GoogleChat] Connected; project=%s, inbound=%s, subscription=%s, "
            "bot_user_id=%s, flow_control(msgs=%s, bytes=%s)",
            project_id or "<unset>",
            inbound,
            "<redacted>" if subscription_path else "<none>",
            self._bot_user_id or "<unresolved>",
            self._max_messages,
            self._max_bytes,
        )
        return True

    async def disconnect(self) -> None:
        """Clean shutdown: stop accepting new messages, wait in-flight, close clients."""
        self._shutting_down = True
        if self._chat_queue_drain_task and not self._chat_queue_drain_task.done():
            self._chat_queue_drain_task.cancel()
            try:
                await self._chat_queue_drain_task
            except asyncio.CancelledError:
                pass
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await asyncio.wait_for(self._supervisor_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if self._streaming_pull_future is not None:
            try:
                self._streaming_pull_future.cancel()
                await asyncio.to_thread(self._streaming_pull_future.result, 10.0)
            except Exception:
                pass
            self._streaming_pull_future = None
        if self._subscriber is not None:
            try:
                await asyncio.to_thread(self._subscriber.close)
            except Exception:
                pass
            self._subscriber = None
        self._mark_disconnected()
        logger.info("[GoogleChat] Disconnected")

    # ------------------------------------------------------------------
    # Pub/Sub supervisor (reconnect loop)
    # ------------------------------------------------------------------
    async def _run_supervisor(self) -> None:
        """Run the streaming_pull with exponential backoff; fatal after 10 attempts.

        ``subscribe()`` returns a concurrent.futures.Future that resolves when
        the stream dies. We await ``future.result()`` in a worker thread and
        react to exceptions.
        """
        attempt = 0
        while not self._shutting_down:
            flow = pubsub_v1.types.FlowControl(
                max_messages=self._max_messages,
                max_bytes=self._max_bytes,
            )
            try:
                future = self._subscriber.subscribe(
                    self._subscription_path,
                    callback=self._on_pubsub_message,
                    flow_control=flow,
                )
                self._streaming_pull_future = future
                if attempt > 0:
                    logger.info("[GoogleChat] Pub/Sub stream reconnected after %d attempts", attempt)
                attempt = 0
                # Blocks until stream dies or cancel().
                await asyncio.to_thread(future.result)
                # Normal completion = disconnect requested.
                if self._shutting_down:
                    return
            except asyncio.CancelledError:
                return
            except gax_exceptions.Unauthenticated:
                self._set_fatal_error(
                    code="pubsub_auth",
                    message="Pub/Sub authentication failed (SA key invalid/revoked)",
                    retryable=False,
                )
                return
            except gax_exceptions.PermissionDenied:
                self._set_fatal_error(
                    code="pubsub_permission",
                    message="SA lacks pubsub.subscriber on the subscription",
                    retryable=False,
                )
                return
            except Exception as exc:
                attempt += 1
                msg = _redact_sensitive(str(exc))
                logger.warning(
                    "[GoogleChat] Pub/Sub stream died (attempt %d/%d): %s",
                    attempt,
                    self._MAX_RECONNECT_ATTEMPTS,
                    msg,
                )
                if attempt >= self._MAX_RECONNECT_ATTEMPTS:
                    self._set_fatal_error(
                        code="pubsub_reconnect_exhausted",
                        message=f"Pub/Sub reconnect failed {attempt} times; giving up",
                        retryable=False,
                    )
                    return
                delay = min(
                    self._RECONNECT_MAX_DELAY,
                    self._RECONNECT_BASE_DELAY * (2 ** (attempt - 1)),
                )
                # Full jitter: pick uniformly in [0, delay].
                sleep_for = random.uniform(0, delay)
                try:
                    await asyncio.sleep(sleep_for)
                except asyncio.CancelledError:
                    return

    # ------------------------------------------------------------------
    # Inbound event handling (Pub/Sub callback runs in a thread)
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_message_payload(
        envelope: Dict[str, Any], ce_type: str = ""
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], str]]:
        """Detect Pub/Sub envelope format and return ``(message, space, format_name)``.

        Three known formats are accepted. Returns ``None`` when the envelope
        is unrecognized, is a non-MESSAGE event, or otherwise should be
        silently dropped.

        Format 1 — Workspace Add-ons (canonical, ce-type-driven)::

            {"chat": {"messagePayload": {"message": {...}, "space": {...}}}}

        Format 2 — Native Chat API Pub/Sub (alternative configuration where
        the Chat app publishes events directly without the Workspace
        Add-ons wrapper)::

            {"type": "MESSAGE", "message": {...}, "space": {...}}

        Format 3 — Relay / flat (a custom Cloud Run relay that flattens the
        Chat event into top-level fields)::

            {"event_type": "MESSAGE", "sender_email": "...", "text": "...",
             "space_name": "spaces/X", "thread_name": "spaces/X/threads/Y",
             "message_name": "spaces/X/messages/M.M"}

        For format 3 the helper synthesizes a Chat-API-shaped ``message``
        dict so downstream code (``_dispatch_message`` →
        ``_build_message_event``) can consume it without branching.
        """
        # Format 1: Workspace Add-ons. The chat block carries one of
        # messagePayload / membershipPayload / cardClickedPayload depending
        # on the ce-type. ``_on_pubsub_message`` handles the membership and
        # card branches before reaching this helper, so here we only accept
        # message payloads.
        chat_block = envelope.get("chat") or {}
        msg_payload_wrapper = chat_block.get("messagePayload") if chat_block else None
        if msg_payload_wrapper:
            msg = msg_payload_wrapper.get("message") or {}
            space = msg_payload_wrapper.get("space") or msg.get("space") or {}
            return msg, space, "workspace_addons"

        # Format 2: Native Chat API Pub/Sub. Detected by a top-level
        # ``message`` object plus a ``type`` field; only MESSAGE events
        # flow through here.
        if isinstance(envelope.get("message"), dict):
            if envelope.get("type", "") != "MESSAGE":
                return None
            msg = envelope["message"]
            space = envelope.get("space") or msg.get("space") or {}
            return msg, space, "native_chat_api"

        # Format 3: Relay / flat. A custom Cloud Run relay typically
        # forwards Chat events with this shape so the bot can run without
        # direct GCP credentials.
        if "event_type" in envelope or "sender_email" in envelope:
            if envelope.get("event_type", "MESSAGE") != "MESSAGE":
                return None
            sender_email = (envelope.get("sender_email") or "").strip()
            sender_display = (
                envelope.get("sender_display_name")
                or sender_email
                or "Unknown"
            )
            # The Chat resource name is unknown for relay events; synthesize
            # a stable surrogate from the sender email so dedup keys and
            # session IDs stay deterministic across redelivery.
            sender_name_surrogate = (
                "users/relay-"
                + (sender_email or "unknown").replace("@", "_at_").replace(".", "_")
            )
            text = envelope.get("text", "") or ""
            # Honor the relay's declared sender_type when present so the
            # downstream BOT self-filter (sender_type == "BOT") fires for
            # bot-originated messages forwarded by the relay. Hardcoding
            # "HUMAN" here meant the bot would re-process its own replies
            # if the relay forwarded them, and allowed a relay envelope to
            # impersonate any allowlisted user without ever being marked
            # as a bot. Default to "HUMAN" for backward compatibility when
            # the relay does not provide the field.
            #
            # Operator contract: the relay MUST forward sender.type from
            # the upstream Chat event as ``sender_type``. Relays that
            # forward bot replies as HUMAN (or omit the field) cannot be
            # distinguished from genuine humans here.
            sender_type_raw = (envelope.get("sender_type") or "HUMAN")
            sender_type = str(sender_type_raw).strip().upper() or "HUMAN"
            if sender_type not in {"HUMAN", "BOT"}:
                sender_type = "HUMAN"
            msg: Dict[str, Any] = {
                "name": envelope.get("message_name", "") or "",
                "sender": {
                    "name": sender_name_surrogate,
                    "email": sender_email,
                    "displayName": sender_display,
                    "type": sender_type,
                },
                "text": text,
                "argumentText": text,
            }
            thread_name = envelope.get("thread_name") or ""
            if thread_name:
                msg["thread"] = {"name": thread_name}
            space = {
                "name": envelope.get("space_name", "") or "",
                "spaceType": envelope.get("space_type", "SPACE"),
            }
            return msg, space, "relay_flat"

        return None

    def _on_pubsub_message(self, message: Any) -> None:
        """Pub/Sub callback — parse envelope and dispatch to asyncio loop.

        Runs in a Pub/Sub SubscriberClient worker thread, NOT the event loop.
        Never block this function; never raise out of it (that triggers
        Pub/Sub nack + infinite redelivery).

        Google Chat Events API uses CloudEvents-style Pub/Sub messages. The
        event type is carried in Pub/Sub message attributes (``ce-type``),
        not in the JSON body. The body is wrapped in a ``chat`` object whose
        keys depend on the event type:

          - google.workspace.chat.message.v1.created
              -> envelope["chat"]["messagePayload"] = {space, message}
          - google.workspace.chat.membership.v1.created
              -> envelope["chat"]["membershipPayload"] = {space, membership}
          - google.workspace.chat.membership.v1.deleted
              -> envelope["chat"]["membershipPayload"] = {space, membership}
        """
        if self._shutting_down:
            message.nack()
            return
        try:
            envelope = json.loads(message.data.decode("utf-8"))
        except Exception:
            logger.exception("[GoogleChat] Could not parse Pub/Sub envelope")
            message.ack()
            return

        attrs = dict(getattr(message, "attributes", {}) or {})
        ce_type = attrs.get("ce-type") or ""
        logger.debug(
            "[GoogleChat] Envelope keys=%s, ce-type=%s",
            list(envelope.keys()),
            ce_type,
        )
        if os.getenv("GOOGLE_CHAT_DEBUG_RAW"):
            # Dangerous flag: contains message text and sender email. Route
            # through the global redaction filter and gate at DEBUG level so
            # default log configurations never surface it. Operators must
            # enable DEBUG logging AND set this env var to see the dump.
            try:
                from agent.redact import redact_sensitive_text

                dump = redact_sensitive_text(json.dumps(envelope))
            except Exception:
                dump = "<redact filter unavailable>"
            logger.debug("[GoogleChat] RAW envelope (redacted): %s", dump[:2000])

        try:
            chat_block = envelope.get("chat") or {}

            # --- Membership events ---
            if "membership" in ce_type or "MEMBERSHIP" in ce_type:
                mpl = chat_block.get("membershipPayload") or {}
                space = mpl.get("space") or {}
                membership = mpl.get("membership") or {}
                if "created" in ce_type:
                    # ADDED_TO_SPACE for this bot — resolve self user_id.
                    member = membership.get("member") or {}
                    if member.get("type") == "BOT" and not self._bot_user_id:
                        name = member.get("name")
                        if name:
                            self._bot_user_id = name
                            self._save_cached_bot_id(name)
                    logger.info(
                        "[GoogleChat] ADDED_TO_SPACE %s", space.get("name", "?")
                    )
                else:
                    logger.info(
                        "[GoogleChat] REMOVED_FROM_SPACE %s", space.get("name", "?")
                    )
                message.ack()
                return

            # --- Card-click events ---
            if _card_event_payload(envelope) is not None or "widget" in ce_type or "card" in ce_type.lower():
                self._schedule_pubsub_processing(
                    self._handle_card_event(envelope, notify=True), message
                )
                return

            # --- Message events ---
            extracted = self._extract_message_payload(envelope, ce_type)
            if extracted is None:
                logger.debug(
                    "[GoogleChat] Envelope did not match a known message format; "
                    "ce-type=%s, keys=%s", ce_type, list(envelope.keys())
                )
                message.ack()
                return

            msg, space, _fmt = extracted
            sender = msg.get("sender") or {}
            sender_type = sender.get("type") or ""

            # Self-filter: drop bot-sourced messages (own replies and other bots).
            if sender_type == "BOT":
                message.ack()
                return

            # Completed-message deduplication and concurrent redelivery
            # coordination are handled by PubSubAckCoordinator. An in-flight
            # duplicate must never ACK before the original handoff settles.
            msg_name = msg.get("name") or ""

            # Wrap msg with parent-level space so _build_message_event can find it.
            msg_with_space = dict(msg)
            if "space" not in msg_with_space and space:
                msg_with_space["space"] = space

            # Enrich envelope with a synthetic top-level "space" field so the
            # dispatch side has a consistent shape regardless of format.
            enriched_env = dict(envelope)
            if "space" not in enriched_env and space:
                enriched_env["space"] = space

            self._schedule_pubsub_processing(
                self._dispatch_message(msg_with_space, enriched_env),
                message,
                msg_name,
            )
        except Exception:
            logger.exception("[GoogleChat] Error in _on_pubsub_message")
            try:
                message.nack()
            except Exception:
                pass

    async def _handle_card_event(
        self, envelope: Dict[str, Any], *, notify: bool
    ) -> Optional[str]:
        """Resolve a trusted Google Chat card action without starting an agent turn.

        Returns None when this gateway must not patch or reply: the click's
        ``robie_env`` routes it to the other environment, whose own
        subscription receives it and patches the card. The caller still
        acks: a normal return settles the Pub/Sub delivery.

        A click routed to this gateway always gets a terminal patch. The
        bridge has already replaced the buttons with a processing card, so
        an unknown action, an unreadable token, or a confirmation id missing
        from this gateway's database is patched to "This card is no longer active."
        rather than left on "Processing". A clarify or decision click routed
        here still updates the card, including the expired-question reply
        after in-memory clarify state is gone.
        """
        payload = _card_event_payload(envelope)
        if payload is None:
            return "That action could not be read. Please ask ROBIE to show it again."

        common = payload.get("common") or {}
        action_obj = payload.get("action") or {}
        raw_action = str(
            common.get("invokedFunction")
            or action_obj.get("actionMethodName")
            or payload.get("actionMethodName")
            or ""
        ).strip()
        # Confirmation cards posted before the short-name fix stored a
        # bridge URL in action.function. Chat echoes that URL as
        # invokedFunction; fold it back to the bare action so the click
        # reaches the confirmation branch.
        from robie_job_engine.confirmation_cards import canonical_card_action
        action = canonical_card_action(raw_action)
        parameters = _card_parameters(payload)

        # Foreign clicks must not reach the patch below: Prod's old
        # else-branch told the user the action was unsupported and stripped
        # the other environment's buttons. A click routed here that this
        # gateway cannot act on is marked inactive instead of acked silently,
        # because the bridge already replaced its buttons with "Processing".
        inactive = False
        if action == "hermes_clarify":
            # In-memory clarify state expires, so a late click on our own
            # card must still reach the "expired" reply. Ownership is the
            # button's robie_env, not whether the id is still remembered.
            if not _click_routed_to_this_gateway(parameters):
                clarify_id = parameters.get("clarify_id", "").strip()
                logger.info(
                    "[GoogleChat] clarify click not owned here action=%s ref=%s",
                    action,
                    (clarify_id or "-")[:8],
                )
                return None
        elif action == "robie_decision":
            decision_id = parameters.get("decision_id", "").strip()
            # A row in this database is ours. If the row is gone, robie_env
            # still decides: our environment falls through to the resolver,
            # the other environment is acked with no patch.
            if (
                not _decision_owned_by_gateway(self, decision_id)
                and not _click_routed_to_this_gateway(parameters)
            ):
                logger.info(
                    "[GoogleChat] decision click not owned here action=%s ref=%s",
                    action,
                    (decision_id or "-")[:8],
                )
                return None
        elif action == "robie_confirmation_decision":
            confirmation_id, owned = _confirmation_owned_by_gateway(self, parameters)
            if not owned:
                if not _click_routed_to_this_gateway(parameters):
                    logger.info(
                        "[GoogleChat] confirmation click not owned here action=%s ref=%s",
                        action,
                        (confirmation_id or "-")[:8],
                    )
                    return None
                logger.info(
                    "[GoogleChat] confirmation click routed here but not found; marking card inactive action=%s ref=%s",
                    action,
                    (confirmation_id or "-")[:8],
                )
                inactive = True
        else:
            if not _click_routed_to_this_gateway(parameters):
                logger.info(
                    "[GoogleChat] unknown card action ignored action=%s",
                    (action or "-")[:80],
                )
                return None
            logger.info(
                "[GoogleChat] unknown card action routed here; marking card inactive action=%s",
                (action or "-")[:80],
            )
            inactive = True

        response = "That action is no longer available."

        try:
            if inactive:
                # Never call a resolver for a click this gateway does not
                # own: a missing id must not create schema or rows here.
                response = "This card is no longer active."
            elif action == "hermes_clarify":
                clarify_id = parameters.get("clarify_id", "").strip()
                choice = parameters.get("choice", "").strip()
                if not clarify_id or not choice or clarify_id not in self._clarify_state:
                    response = "That question has expired. Please ask ROBIE again."
                else:
                    from tools.clarify_gateway import (
                        mark_awaiting_text,
                        resolve_gateway_clarify,
                    )
                    custom_text = _card_form_text(payload, "custom_text")
                    if choice == "__other__" and not custom_text:
                        waiting = mark_awaiting_text(clarify_id)
                        response = (
                            "Type your answer in a new message."
                            if waiting
                            else "That question has expired. Please ask ROBIE again."
                        )
                    else:
                        answer = custom_text if choice == "__other__" else choice
                        resolved = bool(answer) and resolve_gateway_clarify(clarify_id, str(answer))
                        if resolved:
                            self._clarify_state.pop(clarify_id, None)
                            response = f"Choice recorded: {answer}"
                        else:
                            response = "That question was already answered or has expired."
            elif action == "robie_decision":
                from robie_job_engine.decisions import resolve_google_chat_interaction
                result = resolve_google_chat_interaction(
                    _gateway_job_db_path(self), payload
                )
                response = result.message
                if result.status == "RESOLVED" and result.choice:
                    response = f"Choice recorded: {result.choice}. ROBIE will continue from its checkpoint."
            elif action == "robie_confirmation_decision":
                # Chat-native plan-confirmation Approve/Reject. Ownership
                # was already checked against this gateway's DB. The button
                # carries the HMAC-signed decision token; the click is
                # verified (signature, expiry, principal) and applied
                # idempotently. dispatch_http_event wraps a real reply in
                # UPDATE_MESSAGE so the answered card is replaced.
                from robie_job_engine.confirmation_cards import (
                    resolve_confirmation_click,
                )
                from robie_job_engine.store import JobStore
                click = resolve_confirmation_click(
                    JobStore(_gateway_job_db_path(self)), payload
                )
                response = click.message
        except Exception:
            logger.exception("[GoogleChat] Card action failed (%s)", action or "unknown")
            response = "ROBIE could not record that choice safely. The Job remains paused."

        if notify:
            # Normalize the envelope first: Workspace Add-on card clicks carry
            # the message/space under chat.message / chat.space (no top-level
            # keys). Without this, message_name is empty, _patch_message is
            # skipped, and the user gets a separate acknowledgement message
            # while the answered card keeps its live Approve/Reject buttons.
            normalized = _card_event_payload(payload) or {}
            space = normalized.get("space") or payload.get("space") or {}
            event_message = normalized.get("message") or payload.get("message") or {}
            if not space and isinstance(event_message, dict):
                space = event_message.get("space") or {}
            chat_id = str(space.get("name") or "") if isinstance(space, dict) else ""
            message_name = str(event_message.get("name") or "") if isinstance(event_message, dict) else ""

            patched = False
            if message_name and hasattr(self, "_patch_message"):
                try:
                    await self._patch_message(
                        message_name,
                        {"text": f"✓ {response}", "cardsV2": []},
                    )
                    patched = True
                except Exception:
                    logger.debug("[GoogleChat] Could not patch card message in-place", exc_info=True)

            if not patched and chat_id:
                body: Dict[str, Any] = {"text": response}
                thread = event_message.get("thread") or {} if isinstance(event_message, dict) else {}
                if isinstance(thread, dict) and thread.get("name"):
                    body["thread"] = {"name": thread["name"]}
                try:
                    await self._create_message(chat_id, body)
                except Exception:
                    logger.exception("[GoogleChat] Could not send card-action acknowledgement")
        return response

    async def dispatch_http_event(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        if _card_event_payload(envelope) is not None:
            response = await self._handle_card_event(envelope, notify=False)
            if response is None:
                return {}
            return {
                "actionResponse": {
                    "type": "UPDATE_MESSAGE",
                },
                "text": f"✓ {response}",
                "cardsV2": [],
            }

        extracted = self._extract_message_payload(envelope)
        if extracted is None:
            return {}

        msg, space, _fmt = extracted
        sender = msg.get("sender") or {}
        if sender.get("type") == "BOT":
            return {}

        msg_name = msg.get("name") or ""
        if msg_name and self._dedup.is_duplicate(msg_name):
            return {}

        msg_with_space = dict(msg)
        if "space" not in msg_with_space and space:
            msg_with_space["space"] = space

        enriched_env = dict(envelope)
        if "space" not in enriched_env and space:
            enriched_env["space"] = space

        await self._dispatch_message(msg_with_space, enriched_env)
        return {}

    def verify_http_event_request(self, auth_header: str) -> Tuple[bool, str]:
        if not self._http_events_audience or not self._http_events_service_account_email:
            return False, "google_chat_http_events_not_configured"

        if not auth_header.startswith("Bearer "):
            return False, "missing_google_bearer"

        token = auth_header[7:].strip()
        if not token:
            return False, "missing_google_bearer"

        try:
            claims = _verify_google_id_token(token, self._http_events_audience)
        except Exception as exc:
            logger.warning(
                "[GoogleChat] HTTP event bearer verification failed: %s",
                _redact_sensitive(str(exc)),
            )
            return False, "invalid_google_bearer"

        expected = {
            item.strip().lower()
            for item in self._http_events_service_account_email.split(",")
            if item.strip()
        }
        claim_email = str(claims.get("email") or "").strip().lower()
        if not claim_email or claim_email not in expected:
            return False, "unexpected_google_bearer_identity"

        return True, ""

    async def _dispatch_message(self, msg: Dict[str, Any], envelope: Dict[str, Any]) -> None:
        """Translate a Chat message payload to a MessageEvent and hand off.

        Intercepts the ``/setup-files`` admin command BEFORE the agent
        sees it — that's a bot-local OAuth setup flow, not a prompt.
        Everything else flows to ``handle_message`` as normal.
        """
        try:
            event = await self._build_message_event(msg, envelope)
            if event is None:
                return

            # Short-circuit /setup-files before the agent dispatch.
            text = (event.text or "").strip()
            queue = None
            context = None
            interaction = {}
            source_space = envelope.get("space") or msg.get("space") or {}
            source_space_type = str(
                source_space.get("type") or source_space.get("spaceType") or ""
            ).upper()
            if (
                event.source is not None
                and source_space_type in {"DIRECT_MESSAGE", "DM"}
                and not text.casefold().startswith(("/approve", "/deny"))
            ):
                queue = await asyncio.to_thread(self._durable_chat_queue)
                context = await asyncio.to_thread(
                    queue.active_conversation_job, event.source.chat_id
                )
                interaction = dict((context or {}).get("interaction_state") or {})
                if (
                    interaction.get("awaiting") == "human_input"
                    and classify_human_reply(text, interaction) == "NEW_INTENT"
                ):
                    # Preserve the old Job and queue row as diagnostic history,
                    # but remove the active DM correlation before routing the
                    # new request. It can no longer consume this message as a
                    # missing-field reply.
                    await asyncio.to_thread(
                        queue.deactivate_conversation, event.source.chat_id
                    )
                    context = None
                    interaction = {}
            admin_response = await asyncio.to_thread(
                handle_admin_command,
                ROBIE_JOB_DB,
                text,
                actor=(
                    getattr(event.source, "user_id", None)
                    or getattr(event.source, "user_name", None)
                    or "Google Chat administrator"
                ),
            )
            if admin_response is not None and event.source is not None:
                await self.send(
                    event.source.chat_id,
                    admin_response,
                    reply_to=event.message_id,
                    metadata={"thread_id": getattr(event.source, "thread_id", None)},
                )
                return
            if text.startswith("/setup-files") and event.source is not None:
            # The sender email (user_id) is the per-user OAuth key.
            # The bot stores this user token at
                # ${HERMES_HOME}/google_chat_user_tokens/<sanitized>.json
                # so when User B asks for a file later in B's DM, B's
                # token gets used (not the first person who set up files).
                sender_email = (
                    event.source.user_id
                    if event.source and event.source.user_id
                    else None
                )
                handled = await self._handle_setup_files_command(
                    chat_id=event.source.chat_id,
                    thread_id=event.source.thread_id,
                    raw_text=text,
                    sender_email=sender_email,
                )
                if handled:
                    return

            if text.casefold() in {"/reset", "/new"} and event.source is not None:
                conversation_id = event.source.chat_id
                queue = await asyncio.to_thread(self._durable_chat_queue)
                await asyncio.to_thread(queue.deactivate_conversation, conversation_id)
                await self.send(
                    conversation_id,
                    "The active ROBIE job context was cleared. Your next executable request will start a new job.",
                    reply_to=event.message_id,
                    metadata={"thread_id": getattr(event.source, "thread_id", None)},
                )
                return

            if event.source is not None and not text.startswith("/"):
                queue = queue or await asyncio.to_thread(self._durable_chat_queue)
                if context is None:
                    context = await asyncio.to_thread(
                        queue.active_conversation_job, event.source.chat_id
                    )
                    interaction = dict((context or {}).get("interaction_state") or {})
                if interaction.get("awaiting") == "human_input":
                    # A FAILED/UNVERIFIED/COMPLETE Job can leave
                    # conversation_job_links.active=1 with stale
                    # interaction_state.awaiting=human_input. classify_human_reply
                    # then treats a new @robie as a HITL answer (accepts_value),
                    # resume_human_input raises, and Pub/Sub retries forever.
                    released = await asyncio.to_thread(
                        queue.release_stale_human_input_bind,
                        event.source.chat_id,
                    )
                    if released:
                        logger.warning(
                            "[GoogleChat] released stale HITL bind conversation=%s "
                            "job=%s status=%s reason=%s; opening a new Chat job",
                            event.source.chat_id,
                            released.get("job_id"),
                            released.get("job_status"),
                            released.get("released_reason"),
                        )
                        context = None
                        interaction = {}
                if interaction.get("awaiting") == "human_input":
                    await self._bind_inbound_job_thread(
                        event, (context or {}).get("job_id")
                    )
                    message_id = event.message_id or f"human:{id(event)}"
                    value = human_reply_value(text)
                    accepts_value = interaction.get("accepts_value", True)
                    reply_kind = classify_human_reply(value, interaction)
                    if reply_kind == "INVALID":
                        if accepts_value:
                            field_label = interaction.get("field_label") or interaction.get("field_name") or "requested value"
                            prompt = (
                                f"That does not look like a valid {field_label}. "
                                "Please reply with only the requested value, or send a new question to start fresh."
                            )
                        else:
                            prompt = (
                                "For security, do not send that sensitive value in Chat. "
                                "Enter it directly in EZLynx, then reply RETRY."
                            )
                        await self.send(
                            event.source.chat_id,
                            prompt,
                            reply_to=message_id,
                            metadata={
                                "thread_id": getattr(event.source, "thread_id", None)
                            },
                        )
                        return
                    try:
                        resumed = await asyncio.to_thread(
                            queue.resume_human_input,
                            conversation_id=event.source.chat_id,
                            job_id=context["job_id"],
                            reply_message_id=message_id,
                            field_name=(
                                interaction.get("field_name")
                                if accepts_value
                                else "operator_response"
                            ) or "operator_response",
                            value=value,
                        )
                    except RuntimeError as exc:
                        if not is_stale_human_input_bind_error(exc):
                            raise
                        # Belt-and-suspenders: a race or non-terminal dead bind
                        # (RUNNING leftover HITL state) must not wedge Pub/Sub.
                        logger.warning(
                            "[GoogleChat] resume_human_input rejected dead bind "
                            "conversation=%s job=%s error=%s; opening a new Chat job",
                            event.source.chat_id,
                            (context or {}).get("job_id"),
                            exc,
                        )
                        await asyncio.to_thread(
                            queue.deactivate_conversation, event.source.chat_id
                        )
                        context = None
                        interaction = {}
                    else:
                        await self.send(
                            event.source.chat_id,
                            f"Input received for ROBIE Job {context['job_id']}. Resuming from the saved checkpoint.",
                            reply_to=message_id,
                            metadata={
                                "thread_id": getattr(event.source, "thread_id", None)
                            },
                        )
                        if resumed.get("state") != "DIRECT_RESUME":
                            self._ensure_chat_queue_drain()
                            if self._chat_queue_wakeup is not None:
                                self._chat_queue_wakeup.set()
                            return
                        # The Chat ack is not a claim. Direct Hermes work has no
                        # bounded queue event to wake, so re-open / re-lease the
                        # same generic Job and start the worker without a new
                        # @robie. Fall-through is not enough: a stale adapter
                        # would stop here and the 300s orphan watcher would
                        # fail the unleased row (6cf6f6ae, da53765b).
                        await self._resume_direct_generic_chat_job(
                            event,
                            context["job_id"],
                            message_id,
                            text,
                        )
                        return

            if text.casefold().startswith(("/approve", "/deny")) and event.source is not None:
                queue = await asyncio.to_thread(self._durable_chat_queue)
                context = await asyncio.to_thread(
                    queue.active_conversation_job, event.source.chat_id
                )
                result = resolve_bound_text_decision(
                    ROBIE_JOB_DB,
                    text,
                    actor=(
                        getattr(event.source, "user_id", None)
                        or getattr(event.source, "user_name", None)
                        or ""
                    ),
                    active_job_id=context.get("job_id") if context else None,
                    active_decision_id=(
                        context.get("pending_decision_id") if context else None
                    ),
                )
                await self.send(
                    event.source.chat_id,
                    text_decision_response(result),
                    reply_to=event.message_id,
                    metadata={"thread_id": getattr(event.source, "thread_id", None)},
                )
                return

            expanded_text = await self._expand_workspace_text_links(
                text,
                sender_email=(
                    getattr(event.source, "user_id", None)
                    if event.source is not None
                    else ""
                ) or "",
            )
            if expanded_text != text:
                text = expanded_text
                try:
                    event.text = text
                except Exception:
                    from dataclasses import replace

                    event = replace(event, text=text)

            from robie_job_engine.playground_service import handle_playground_chat

            if event.source is not None:
                playground_replies = await asyncio.to_thread(
                    handle_playground_chat,
                    ROBIE_JOB_DB,
                    text,
                    conversation_id=event.source.chat_id,
                    thread_id=getattr(event.source, "thread_id", None),
                    message_id=event.message_id,
                    requested_by=(
                        getattr(event.source, "user_name", None)
                        or getattr(event.source, "user_id", None)
                        or "Google Chat user"
                    ),
                    requester_user_id=str(
                        getattr(event.source, "user_id", None) or ""
                    ).strip(),
                )
                if playground_replies is not None:
                    for reply_text in playground_replies:
                        await self.send(
                            event.source.chat_id,
                            reply_text,
                            reply_to=event.message_id,
                            metadata={
                                "thread_id": getattr(event.source, "thread_id", None)
                            },
                        )
                    return

            from robie_job_engine.chat_turn_control import is_stop_command

            if is_stop_command(text) and event.source is not None:
                await self._apply_chat_stop(event)
                return

            from robie_job_engine.chat_turn_control import (
                BUSY_SESSION_REPLY,
                busy_session_should_defer,
                running_chat_job_id,
                session_key_from_adapter,
            )

            if (
                id(event) not in self._robie_deferred_release_ids
                and event.source is not None
                and busy_session_should_defer(self, event, db_path=ROBIE_JOB_DB)
            ):
                busy_job_id = running_chat_job_id(self, event)
                logger.info(
                    "[GoogleChat] busy-session reply chat=%s job=%s",
                    event.source.chat_id,
                    busy_job_id or "",
                )
                await self.send(
                    event.source.chat_id,
                    BUSY_SESSION_REPLY,
                    reply_to=event.message_id,
                    metadata={
                        "thread_id": getattr(event.source, "thread_id", None),
                        "robie_job_id": busy_job_id,
                        "robie_delivery_kind": "busy",
                    },
                )
                key = session_key_from_adapter(self, event)
                self._robie_deferred.setdefault(key, []).append(event)
                self._ensure_deferred_chat_drain(key)
                return

            await self._open_and_run_chat_job(event, text)
        except Exception:
            logger.exception("[GoogleChat] _dispatch_message failed")
            # Pub/Sub may ACK only after the durable handoff succeeds. Let the
            # coordinator NACK failures so Google can redeliver the same
            # idempotent event instead of silently losing executable work.
            raise

    def _ensure_deferred_chat_drain(self, key: str) -> None:
        """Start one drainer for this session. A second message only queues."""
        existing = self._robie_deferred_drains.get(key)
        if existing is not None and not existing.done():
            return
        self._robie_deferred_drains[key] = asyncio.create_task(
            self._drain_deferred_chat(key),
            name=f"robie-deferred-chat:{key}",
        )

    async def _drain_deferred_chat(self, key: str) -> None:
        """Run queued messages only after this session's guard is free."""
        from robie_job_engine.chat_turn_control import busy_session_should_defer

        try:
            while self._robie_deferred.get(key):
                event = self._robie_deferred[key][0]
                if busy_session_should_defer(self, event, db_path=ROBIE_JOB_DB):
                    await asyncio.sleep(0.25)
                    continue
                self._robie_deferred[key].pop(0)
                self._robie_deferred_release_ids.add(id(event))
                try:
                    await self._open_and_run_chat_job(
                        event, str(getattr(event, "text", "") or "")
                    )
                except Exception:
                    logger.exception(
                        "[GoogleChat] deferred Chat message failed session=%s",
                        key,
                    )
            self._robie_deferred.pop(key, None)
        finally:
            current = self._robie_deferred_drains.get(key)
            if current is asyncio.current_task():
                self._robie_deferred_drains.pop(key, None)

    async def _open_and_run_chat_job(self, event: MessageEvent, text: str) -> None:
        """Open one job and run it. Caller has already decided this turn may start."""
        message_id = event.message_id or f"unidentified:{id(event)}"
        text = redact_text(text)
        await self._announce_expired_questions(event)
        attachment_kwargs = self._chat_job_attachment_kwargs(event)
        attachment_count = attachment_kwargs["expected_attachment_count"]
        job_id = await asyncio.to_thread(
                open_chat_job,
                ROBIE_JOB_DB,
                message_id,
                text,
                attachments=attachment_kwargs["attachments"],
                requested_by=(
                    getattr(event.source, "user_name", None)
                    or getattr(event.source, "user_id", None)
                    or "Google Chat user"
                ),
                conversation_id=getattr(event.source, "chat_id", None),
                expected_attachment_count=attachment_count,
                attachment_refs=attachment_kwargs["attachment_refs"],
                drive_port=attachment_kwargs["drive_port"],
                inbound_thread_id=(
                    getattr(event.source, "thread_id", None) if event.source else None
                ),
        )
        await self._bind_inbound_job_thread(event, job_id)
        related_only = chat_message_is_related_only(
            text,
            expected_attachment_count=attachment_count,
        )
        correction = classify_request(text, attachment_count=attachment_count)
        if job_id:
            queue = await asyncio.to_thread(self._durable_chat_queue)
            relation = (
                "CORRECTION"
                if related_only and correction.action_type in BOUNDED_ENGINE_ACTIONS
                else "CONTINUATION" if related_only else "CREATED"
            )
            try:
                await asyncio.to_thread(
                    queue.link_conversation_job,
                    conversation_id=getattr(event.source, "chat_id", None)
                    or "google-chat:unknown",
                    job_id=job_id,
                    message_id=message_id,
                    event_id=message_id,
                    relation=relation,
                )
            except Exception as exc:
                logger.warning(
                    "[GoogleChat] Could not link conversation job for %s: %s",
                    message_id,
                    exc,
                )
        if job_id and await self._halt_note_left_as_is(event, job_id):
            return
        if job_id and await self._halt_failed_drive_ingestion(
            event, job_id, attachment_kwargs["attachment_refs"]
        ):
            return
        if job_id and await self._halt_action_gate_refuse(event, job_id):
            return
        if job_id and await self._halt_hard_block_refuse(event, job_id):
            return
        if await self._halt_retry_refusal(event, job_id, text):
            return
        if related_only:
            from robie_job_engine.chat_turn_control import chat_turn_keeps_context

            # A clarification reply stays on this job. Do not wipe its thread.
            if not (job_id and chat_turn_keeps_context(ROBIE_JOB_DB, job_id)):
                # A corrective reply may safely retarget the exact active
                # zero-attempt Job to a bounded destination action. Execute
                # that same Job ID instead of dispatching Hermes or creating
                # a duplicate. Ordinary status/questions remain conversational.
                if (
                    job_id
                    and correction.action_type in BOUNDED_ENGINE_ACTIONS
                    and await self._enqueue_bounded_chat_job(
                        event, job_id, related_only=True
                    )
                ):
                    return
                await self._begin_fresh_chat_turn(event)
                await self.handle_message(event)
                return
        # Test AND Production: operational bounded work goes through
        # maybe_run_bounded_job → JobEngine.run → IsolatedRunStore +
        # DurableWorkLedger. Ledger/path failures fail closed. Hermes
        # is only for non-operational or explicit sandbox chat.
        if job_id and await self._enqueue_bounded_chat_job(
            event, job_id, related_only=False
        ):
            return
        if dispatch_operational_chat(
            ROBIE_JOB_DB,
            job_id,
            sandbox=chat_path_is_sandbox(
                conversation_id=getattr(event.source, "chat_id", None)
                if getattr(event, "source", None)
                else None
            ),
        ):
            return
        execution_text = build_chat_execution_text(ROBIE_JOB_DB, job_id, text)
        try:
            event.text = execution_text
        except Exception:
            from dataclasses import replace
            event = replace(event, text=execution_text)
        await self._run_generic_chat_job(job_id, event)

    async def _handle_setup_files_command(
        self,
        chat_id: str,
        thread_id: Optional[str],
        raw_text: str,
        sender_email: Optional[str] = None,
    ) -> bool:
        """Run the in-chat OAuth setup flow for native attachment delivery.

        Returns ``True`` if the message was consumed (no agent dispatch),
        ``False`` if it should fall through.

        Multi-user mode: ``sender_email`` is the asker's identity, which
        is also the per-user OAuth key. ``status`` / ``start`` / ``revoke``
        / code-exchange all operate on THIS user's token slot. When
        ``sender_email`` is ``None`` (e.g. tests, or older inbound events
        without a populated email field) the handler falls back to the
        legacy single-user path so pre-multi-user installs keep working.

        Subcommands:
          /setup-files                  → show status + next step
          /setup-files start            → print OAuth URL
          /setup-files revoke           → revoke and delete stored token
          /setup-files <CODE_OR_URL>    → exchange auth code for token

        Pre-requisite: client_secret.json must already be on the host
        (one-time terminal step). The status reply tells the user how to
        do that if it's missing.
        """
        from . import oauth as oauth_helper

        # Normalize the email: lowercase + strip. The on-disk token path
        # is sanitized further inside the helper, but having the same
        # normalization at both ends keeps cache lookups consistent.
        sender_key = sender_email.strip().lower() if sender_email else None

        parts = raw_text.split(maxsplit=1)
        # parts[0] is "/setup-files"; parts[1..] is the optional argument
        arg = parts[1].strip() if len(parts) > 1 else ""

        async def _reply(text: str) -> None:
            body: Dict[str, Any] = {"text": text}
            if thread_id:
                body["thread"] = {"name": thread_id}
            try:
                await self._create_message(chat_id, body)
            except Exception:
                logger.debug(
                    "[GoogleChat] /setup-files reply send failed",
                    exc_info=True,
                )

        # Status / no-arg: show what's set up and what to do next.
        if not arg:
            client_secret_present = (
                oauth_helper._client_secret_path().exists()
            )
            token_path = oauth_helper._token_path(sender_key)
            token_present = token_path.exists()
            creds = (
                oauth_helper.load_user_credentials(sender_key)
                if token_present else None
            )
            if creds is not None:
                who = sender_key or "shared (legacy)"
                await _reply(
                    "✅ Native attachment delivery is **active** for "
                    f"`{who}`.\n"
                    f"Token: `{token_path}`\n"
                    "Send `/setup-files revoke` to disable."
                )
                return True
            if not client_secret_present:
                await _reply(
                    "🔧 Native attachment delivery is **not configured**.\n"
                    "**Step 1 (one-time, on the host):** create OAuth client "
                    "credentials at "
                    "https://console.cloud.google.com/apis/credentials → "
                    "*Create credentials* → *OAuth client ID* → *Desktop app*. "
                    "Download the JSON. Then on the host run:\n"
                    "```\n"
                    "python -m plugins.platforms.google_chat.oauth "
                    "--client-secret /path/to/client_secret.json\n"
                    "```\n"
                    "**Step 2:** come back here and send `/setup-files start`."
                )
                return True
            await _reply(
                "🔧 Client credentials are stored but you haven't "
                "authorized yet. Send `/setup-files start` to begin."
            )
            return True

        if arg == "start":
            if not oauth_helper._client_secret_path().exists():
                await _reply(
                    "⚠️ No client credentials stored for this profile. Send "
                    "`/setup-files` (no args) for setup instructions."
                )
                return True
            try:
                # Reuse the helper logic but capture stdout via a sync
                # thread so we don't print to the gateway terminal.
                import io
                import contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    await asyncio.to_thread(
                        oauth_helper.get_auth_url, sender_key,
                    )
                auth_url = buf.getvalue().strip().splitlines()[-1]
            except SystemExit:
                await _reply(
                    "❌ Couldn't generate the OAuth URL. Check the gateway "
                    "logs and verify the client_secret.json is valid."
                )
                return True
            except Exception as exc:
                logger.warning(
                    "[GoogleChat] /setup-files start failed: %s", exc,
                )
                await _reply(f"❌ Error: {exc}")
                return True
            await _reply(
                "1. Open this URL in your browser and authorize:\n"
                f"{auth_url}\n\n"
                "2. After clicking *Allow*, your browser will fail to load "
                "`http://localhost:1/?...&code=...`. That's expected.\n\n"
                "3. Copy the entire failed URL from the browser's URL bar "
                "and paste it back here as: `/setup-files <PASTE_URL>` "
                "(or just the `code=...` value).\n\n"
                "Tip: the URL contains your access grant — keep it private."
            )
            return True

        if arg == "revoke":
            try:
                import io
                import contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    await asyncio.to_thread(oauth_helper.revoke, sender_key)
                output = buf.getvalue().strip() or "Revoked."
            except SystemExit:
                output = "Revoke completed (some steps may have been skipped)."
            except Exception as exc:
                logger.warning(
                    "[GoogleChat] /setup-files revoke failed: %s", exc,
                )
                await _reply(f"❌ Error revoking: {exc}")
                return True
            # Wipe in-memory creds so subsequent uploads fall through to
            # the setup-instructions text notice immediately. Scope the
            # eviction to the sender's slot — Bob revoking shouldn't
            # break Alice's per-user token nor wipe the shared legacy
            # fallback that other users may still depend on.
            if sender_key:
                self._user_creds_by_email.pop(sender_key, None)
                self._user_chat_api_by_email.pop(sender_key, None)
            else:
                self._user_credentials = None
                self._user_chat_api = None
            await _reply(f"✅ Done.\n```\n{output}\n```")
            return True

        # Anything else is treated as the auth code or the failed-redirect
        # URL the user pasted.
        try:
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                await asyncio.to_thread(
                    oauth_helper.exchange_auth_code, arg, sender_key,
                )
            output = buf.getvalue().strip()
        except SystemExit:
            await _reply(
                "❌ Token exchange failed. The code may have expired or "
                "the URL is malformed. Send `/setup-files start` to get "
                "a fresh OAuth URL."
            )
            return True
        except Exception as exc:
            logger.warning(
                "[GoogleChat] /setup-files exchange failed: %s", exc,
            )
            await _reply(f"❌ Error: {exc}")
            return True

        # Re-load credentials into the adapter so the next file send uses
        # them WITHOUT a gateway restart.
        try:
            new_creds = await asyncio.to_thread(
                oauth_helper.load_user_credentials, sender_key,
            )
            if new_creds is not None:
                new_api = await asyncio.to_thread(
                    lambda: oauth_helper.build_user_chat_service(new_creds)
                )
                if sender_key:
                    self._user_creds_by_email[sender_key] = new_creds
                    self._user_chat_api_by_email[sender_key] = new_api
                else:
                    self._user_credentials = new_creds
                    self._user_chat_api = new_api
                await _reply(
                    "✅ Authorized! Native attachment delivery is now "
                    "active. Try asking me to send you a PDF."
                )
                return True
        except Exception as exc:
            logger.warning(
                "[GoogleChat] post-exchange creds load failed: %s", exc,
            )

        await _reply(
            "⚠️ Token exchanged but the gateway couldn't load the new "
            "credentials in-memory. Restart the gateway and the token "
            f"at `{oauth_helper._token_path(sender_key)}` will be picked "
            f"up.\nHelper output:\n```\n{output}\n```"
        )
        return True

    async def _build_message_event(
        self, msg: Dict[str, Any], envelope: Dict[str, Any]
    ) -> Optional[MessageEvent]:
        """Parse a Chat API message into a hermes MessageEvent."""
        space = envelope.get("space") or msg.get("space") or {}
        space_name = space.get("name") or ""  # "spaces/XXX"
        space_type = (space.get("type") or space.get("spaceType") or "").upper()
        thread = msg.get("thread") or {}
        thread_name = thread.get("name") or None
        sender = msg.get("sender") or {}
        sender_name = sender.get("name") or ""
        sender_display = sender.get("displayName") or sender.get("email") or sender_name
        sender_email = sender.get("email") or ""

        # Cache the asker's email per chat_id so _send_file can pick the
        # right per-user OAuth token when the agent later wants to send
        # an attachment in this conversation. Lower-cased so cache hits
        # match the sanitized token-file lookup.
        if sender_email and space_name:
            self._last_sender_by_chat[space_name] = sender_email.strip().lower()

        chat_type = "dm" if space_type in {"DIRECT_MESSAGE", "DM"} else "group"
        text = msg.get("argumentText") or msg.get("text") or ""
        text = text.strip()

        # Slash command: emit MessageType.COMMAND with normalized text.
        slash = msg.get("slashCommand") or {}
        is_slash = bool(slash)
        if is_slash:
            command_id = str(slash.get("commandId") or "")
            if command_id and not text.startswith("/"):
                text = f"/cmd_{command_id} {text}".strip()

        # Attachments: download and cache.
        media_urls: List[str] = []
        media_types: List[str] = []
        message_type = MessageType.TEXT
        attachments = msg.get("attachment") or []
        for att in attachments:
            try:
                local_path, mime = await self._download_attachment(
                    att, sender_email=sender_email,
                )
            except Exception:
                logger.exception("[GoogleChat] attachment download failed")
                continue
            if not local_path:
                continue
            media_urls.append(local_path)
            media_types.append(mime or "application/octet-stream")
            # Prefer the first-seen type for MessageType if no text present.
            if message_type == MessageType.TEXT and not text:
                message_type = _mime_for_message_type(mime or "")

        if is_slash:
            message_type = MessageType.COMMAND

        # Increment the persistent inbound count for this thread.
        # The PRE-increment value (==0 for the very first time we see
        # this thread, persisted across gateway restarts) drives the
        # main-flow-vs-side-thread heuristic below.
        prev_thread_count = 0
        if thread_name and space_name:
            prev_thread_count = self._thread_count_store.incr(
                space_name, thread_name
            )

        # Session-thread + outbound-thread routing for DMs:
        # - prev_count == 0  → first message in this thread. Google Chat
        #   creates a fresh thread per top-level message in the DM input
        #   box; treat as "main flow" so all top-level messages share
        #   one DM session and the user keeps continuity. The bot's
        #   reply ALSO must NOT thread with the user message — if we
        #   pass thread.name on outbound, Chat displays the pair as an
        #   expandable thread under the user's message instead of two
        #   adjacent top-level cards.
        # - prev_count >= 1  → user explicitly engaged a thread that
        #   already had messages (clicked "Reply in thread" on a prior
        #   message). Isolate session by chat_id+thread_id, AND keep
        #   the bot's reply inside that thread.
        #
        # For groups, threads ARE meaningful conversational containers
        # (Telegram forum / Discord thread parity); always isolate AND
        # always reply in-thread.
        if chat_type == "dm":
            is_side_thread = prev_thread_count > 0
            session_thread_id = thread_name if is_side_thread else None
            # Outbound thread cache: populated only when side-thread, so
            # _resolve_thread_id falls through to "no thread" on main
            # flow and the bot reply lands as a top-level sibling.
            if thread_name and space_name and is_side_thread:
                self._last_inbound_thread[space_name] = thread_name
            elif space_name:
                self._last_inbound_thread.pop(space_name, None)
        else:
            session_thread_id = thread_name
            # Groups always reply in-thread.
            if thread_name and space_name:
                self._last_inbound_thread[space_name] = thread_name

        # A reply inside a thread that already had messages is bound to
        # the job. A brand-new top-level (prev count 0) is not: the first
        # outbound starts the job's own thread.
        inbound_name = str(msg.get("name") or "")
        if thread_name and inbound_name and prev_thread_count > 0:
            self._reply_in_existing_thread[inbound_name] = thread_name

        source = self.build_source(
            chat_id=space_name,
            chat_name=space.get("displayName") or space.get("name") or "",
            chat_type=chat_type,
            # ``user_id`` is the canonical identity used by allowlists,
            # session keys, and audit. Operators configure
            # ``GOOGLE_CHAT_ALLOWED_USERS`` with email addresses (the
            # value Google Chat surfaces in its UI), so the email is
            # the natural canonical id. The Chat resource name
            # ``users/{id}`` moves to ``user_id_alt`` for traceability
            # and Chat-API operations that need it. Falls back to the
            # resource name when sender has no email (rare — bot-to-bot
            # or system events). Pattern lifted from PR #14965.
            user_id=(sender_email or sender_name),
            user_name=sender_display,
            thread_id=session_thread_id,
            user_id_alt=(sender_name or None),
        )
        return MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=msg,
            message_id=msg.get("name") or None,
            media_urls=media_urls,
            media_types=media_types,
        )

    def _drive_scoped_credentials(self, creds: Any) -> Any:
        """Request drive.readonly on a copy of *creds* when the client supports it."""
        if creds is None:
            return None
        try:
            if hasattr(creds, "with_scopes"):
                return creds.with_scopes(list(_CHAT_SCOPES))
        except Exception:
            logger.debug("[GoogleChat] with_scopes(drive.readonly) failed", exc_info=True)
        return creds

    def _fetch_drive_file_sync(
        self, drive_file_id: str, *, sender_email: str = ""
    ) -> Optional[Tuple[bytes, str, str]]:
        """Fetch a Drive picker chip. Returns ``(bytes, mime, filename)`` or None."""
        if not drive_file_id:
            return None
        candidates: List[Tuple[str, Any]] = []
        if self._credentials is not None:
            candidates.append(("app", self._drive_scoped_credentials(self._credentials)))
        if sender_email:
            try:
                from .oauth import load_user_credentials

                user_creds = load_user_credentials(sender_email.strip().lower())
                if user_creds is not None:
                    candidates.append(("user", user_creds))
            except Exception:
                logger.exception("[GoogleChat] Could not load user Drive credentials")

        for identity, creds in candidates:
            if creds is None:
                continue
            try:
                result = _fetch_drive_bytes(creds, drive_file_id)
                logger.info(
                    "[GoogleChat] Downloaded Drive attachment with %s identity",
                    identity,
                )
                return result
            except Exception as exc:
                logger.warning(
                    "[GoogleChat] Drive download with %s identity failed: %s",
                    identity,
                    _redact_sensitive(str(exc)),
                )
        return None

    def _drive_file_port(self, sender_email: str = "") -> CallableDrivePort:
        """Job-engine Drive port bound to this adapter's fetch identities."""

        def _fetch(drive_file_id: str) -> Tuple[bytes, str, str]:
            result = self._fetch_drive_file_sync(
                drive_file_id, sender_email=sender_email
            )
            if result is None:
                raise RuntimeError("Drive file did not download")
            return result

        return CallableDrivePort(_fetch)

    def _chat_job_attachment_kwargs(
        self, event: MessageEvent
    ) -> Dict[str, Any]:
        """Build open_chat_job attachment arguments without double-counting."""
        raw_attachments = list(
            ((getattr(event, "raw_message", None) or {}).get("attachment") or [])
        )
        staged_files = list(zip(event.media_urls or [], event.media_types or []))
        drive_refs = unmatched_drive_chip_refs(raw_attachments, len(staged_files))
        sender_email = ""
        if event.source is not None:
            sender_email = str(getattr(event.source, "user_id", None) or "")
        return {
            "attachments": staged_files,
            "expected_attachment_count": len(raw_attachments),
            "attachment_refs": drive_refs,
            "drive_port": self._drive_file_port(sender_email) if drive_refs else None,
        }

    async def _announce_expired_questions(self, event: MessageEvent) -> None:
        """One line when a clarify job passed the 10-minute limit."""
        source = event.source
        if source is None:
            return
        from robie_job_engine.chat_job_controls import expire_stale_waiting_jobs

        try:
            expired = await asyncio.to_thread(
                expire_stale_waiting_jobs, JobStore(ROBIE_JOB_DB)
            )
        except Exception:
            logger.exception("[GoogleChat] could not expire stale questions")
            return
        for item in expired:
            chat_id = str(item.get("conversation_id") or "")
            if not chat_id.startswith("spaces/"):
                chat_id = source.chat_id
            await self.send(
                chat_id,
                str(item.get("reply") or ""),
                reply_to=None,
                metadata={
                    "thread_id": item.get("thread_id") or None,
                    "robie_delivery_kind": "notice",
                },
            )

    async def _halt_note_left_as_is(
        self, event: MessageEvent, job_id: Optional[str]
    ) -> bool:
        """The user said no to a repeat note. One line, then stop."""
        if not job_id:
            return False
        note = await asyncio.to_thread(
            JobStore(ROBIE_JOB_DB).get_checkpoint, job_id, "note_left_as_is"
        )
        reply = str((note or {}).get("reply") or "").strip()
        if not reply:
            return False
        chat_id = getattr(event.source, "chat_id", None) if event.source else None
        if not chat_id:
            return True
        from robie_job_engine.chat_thread import read_job_chat_thread
        from robie_job_engine.chat_turn_control import release_chat_lock

        stored = await asyncio.to_thread(
            read_job_chat_thread, JobStore(ROBIE_JOB_DB), job_id
        )
        await self.send(
            chat_id,
            reply,
            reply_to=event.message_id,
            metadata={
                "thread_id": stored or getattr(event.source, "thread_id", None),
                "robie_job_id": job_id,
                "robie_delivery_kind": "notice",
            },
        )
        release_chat_lock(self, chat_id, job_id)
        return True

    async def _halt_hard_block_refuse(
        self, event: MessageEvent, job_id: Optional[str]
    ) -> bool:
        """Send the one-line cancel refusal and do not start Hermes."""
        if not job_id:
            return False
        note = await asyncio.to_thread(
            JobStore(ROBIE_JOB_DB).get_checkpoint, job_id, "hard_block"
        )
        reply = str((note or {}).get("reply") or "").strip()
        if not reply:
            return False
        chat_id = getattr(event.source, "chat_id", None) if event.source else None
        if not chat_id:
            return True
        await self.send(
            chat_id,
            reply,
            reply_to=None,
            metadata={
                "thread_id": getattr(event.source, "thread_id", None),
                "robie_delivery_kind": "hard_block",
                "robie_job_id": job_id,
            },
        )
        return True

    async def _halt_action_gate_refuse(
        self, event: MessageEvent, job_id: Optional[str]
    ) -> bool:
        """Post a dry refuse note and stop Hermes before any Ascend click."""
        if not job_id:
            return False
        job = await asyncio.to_thread(JobStore(ROBIE_JOB_DB).get_job, job_id)
        if not is_action_gate_refusal(job):
            return False
        chat_id = getattr(event.source, "chat_id", None) if event.source else None
        if not chat_id:
            return True
        await self.send(
            chat_id,
            format_action_gate_chat_note(job),
            reply_to=None,
            metadata={"thread_id": getattr(event.source, "thread_id", None)},
        )
        return True

    async def _halt_retry_refusal(
        self, event: MessageEvent, job_id: Optional[str], text: str
    ) -> bool:
        """Reply in plain English when retry is refused, then stop.

        A retry that is allowed returns False so the job can run. ``reply_to``
        stays unset so send() does not rewrite this note into the generic
        terminal template. The thread still comes from metadata.
        """
        if not is_retry_text(text):
            return False
        chat_id = getattr(event.source, "chat_id", None) if event.source else None
        thread_id = getattr(event.source, "thread_id", None) if event.source else None
        if job_id:
            note = await asyncio.to_thread(
                retry_refusal_reply, JobStore(ROBIE_JOB_DB), job_id
            )
            if not note:
                return False
        else:
            note = retry_without_job_reply()
        if not chat_id:
            return True
        await self.send(
            chat_id,
            note,
            reply_to=None,
            metadata={"thread_id": thread_id},
        )
        return True

    async def _halt_failed_drive_ingestion(
        self, event: MessageEvent, job_id: str, drive_refs: List[Any]
    ) -> bool:
        """Post one honest Chat note and stop the job when a Drive chip failed."""
        if not job_id or not drive_refs:
            return False
        job = await asyncio.to_thread(JobStore(ROBIE_JOB_DB).get_job, job_id)
        if not is_attachment_ingestion_error(job.get("last_error")):
            return False
        if job.get("status") != JobStatus.FAILED.value:
            return False
        identity = proven_drive_share_identity(self._credentials)
        chat_id = getattr(event.source, "chat_id", None) if event.source else None
        if not chat_id:
            return True
        # No reply_to: send() would resolve the FAILED job and rewrite this
        # into the generic terminal template. Thread via metadata only.
        await self.send(
            chat_id,
            drive_chip_download_failed_message(identity),
            reply_to=None,
            metadata={"thread_id": getattr(event.source, "thread_id", None)},
        )
        return True

    async def _download_attachment(
        self, attachment: Dict[str, Any], *, sender_email: str = ""
    ) -> Tuple[Optional[str], Optional[str]]:
        """Download an inbound attachment to the local cache; return (path, mime).

        Priority for bot Service Accounts:

          1. ``attachmentDataRef.resourceName`` via ``chat.media.download`` —
             the supported bot path. The Service Account bearer token has
             ``chat.bot`` scope which the Chat API authorises against the
             space membership.
          2. Drive picker chips (``driveDataRef.driveFileId``) via Drive
             API using the app identity (now includes ``drive.readonly``)
             then the sender's ``/setup-files`` user OAuth token.
          3. Direct HTTP fetch of ``downloadUri`` only as a last resort —
             that URL is meant for user OAuth tokens (chat.google.com
             returns 401 for SA bearer tokens) and is unlikely to work,
             but we keep the path for forward-compat with Google changes.
        """
        mime = attachment.get("contentType") or ""
        source = attachment.get("source") or ""
        name = attachment.get("name") or ""
        attachment_data_ref = attachment.get("attachmentDataRef") or {}
        resource_name = attachment_data_ref.get("resourceName") or ""
        drive_file_id = (attachment.get("driveDataRef") or {}).get("driveFileId") or ""
        download_uri = attachment.get("downloadUri") or ""

        # NOTE on ``source == "DRIVE_FILE"``: Google Chat tags BOTH
        # drag-and-drop chat uploads AND Drive-picker shares with this
        # source string, but the two have different access models.
        # Drag-and-drop uploads come with an ``attachmentDataRef.resourceName``
        # that bot SA tokens CAN download via ``media.download_media``.
        # Drive-picker shares use a structured Drive file reference.
        if source == "DRIVE_FILE" and not resource_name and not drive_file_id:
            logger.warning("[GoogleChat] Drive attachment has no supported data reference")
            return None, mime

        data: Optional[bytes] = None
        filename = attachment.get("contentName") or (
            name.split("/")[-1] if name else "attachment"
        )

        # Path 1: media.download with attachmentDataRef.resourceName (bot-path).
        if resource_name:
            def _fetch_media() -> bytes:
                req = self._chat_api.media().download_media(
                    resourceName=resource_name,
                )
                from googleapiclient.http import MediaIoBaseDownload
                import io

                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, req)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
                return buf.getvalue()

            try:
                data = await asyncio.to_thread(_fetch_media)
            except HttpError as exc:
                logger.warning(
                    "[GoogleChat] media.download_media failed: %s",
                    _redact_sensitive(str(exc)),
                )
                data = None


        # Path 2: Drive picker. Accept IDs only from Google's structured event.
        if data is None and drive_file_id:
            fetched = await asyncio.to_thread(
                self._fetch_drive_file_sync,
                drive_file_id,
                sender_email=sender_email,
            )
            if fetched is not None:
                data, mime, filename = fetched

        # Path 3: downloadUri fallback (rarely works with SA tokens, but try).
        if data is None and download_uri:
            if not _is_google_owned_host(download_uri):
                logger.warning(
                    "[GoogleChat] Rejecting attachment fetch: non-Google host"
                )
                return None, mime

            def _fetch_uri() -> bytes:
                import google.auth.transport.requests as gar

                authed_session = gar.AuthorizedSession(self._credentials)
                resp = authed_session.get(download_uri, timeout=30)
                resp.raise_for_status()
                return resp.content

            try:
                data = await asyncio.to_thread(_fetch_uri)
            except Exception as exc:
                logger.warning(
                    "[GoogleChat] downloadUri fetch failed (SA tokens often "
                    "lack access here; this is expected for user-uploaded "
                    "content): %s",
                    _redact_sensitive(str(exc)),
                )
                return None, mime

        if data is None:
            return None, mime

        # Cache based on MIME. Upstream's cache_* helpers expect `ext` for
        # media (image/audio/video) and a positional `filename` for docs.
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
        else:
            ext = ""
        if mime.startswith("image/"):
            local = cache_image_from_bytes(data, ext=ext or ".jpg")
        elif mime.startswith("audio/"):
            local = cache_audio_from_bytes(data, ext=ext or ".ogg")
        elif mime.startswith("video/"):
            local = cache_video_from_bytes(data, ext=ext or ".mp4")
        else:
            local = cache_document_from_bytes(data, filename)
        return local, mime

    async def _expand_workspace_text_links(
        self, text: str, *, sender_email: str = ""
    ) -> str:
        """Parse linked Google Docs/Markdown as text without local downloads."""
        matches = list(_GOOGLE_WORKSPACE_URL_RE.finditer(text or ""))
        if not matches:
            return text
        candidates: List[Tuple[str, Any]] = []
        if sender_email:
            try:
                from .oauth import load_user_credentials

                user_creds = await asyncio.to_thread(
                    load_user_credentials, sender_email.strip().lower()
                )
                if user_creds is not None:
                    candidates.append(("user", user_creds))
            except Exception:
                logger.exception("[GoogleChat] Could not load Workspace parser credentials")
        if self._credentials is not None:
            candidates.append(("app", self._credentials))

        blocks: list[str] = []
        seen: set[str] = set()
        for match in matches:
            file_id = match.group("id")
            if file_id in seen:
                continue
            seen.add(file_id)
            parsed: tuple[str, str] | None = None
            for identity, creds in candidates:
                def _fetch_workspace_text() -> tuple[str, str]:
                    from robie_job_engine.skill_sync import (
                        GOOGLE_DOC_MIME,
                        GoogleDriveSkillSource,
                    )

                    drive = build_service(
                        "drive", "v3", credentials=creds, cache_discovery=False
                    )
                    meta = drive.files().get(
                        fileId=file_id,
                        fields="id,name,mimeType,modifiedTime,webViewLink,size",
                        supportsAllDrives=True,
                    ).execute()
                    name = str(meta.get("name") or "Google Workspace document")
                    mime = str(meta.get("mimeType") or "")
                    if mime != GOOGLE_DOC_MIME and not name.casefold().endswith(".md"):
                        raise ValueError(
                            "linked Drive file is not a Google Doc or Markdown file"
                        )
                    if int(meta.get("size") or 0) > _MAX_INBOUND_ATTACHMENT_BYTES:
                        raise ValueError("linked Workspace document exceeds the size limit")
                    data = GoogleDriveSkillSource(drive).read_file(meta)
                    if len(data) > _MAX_INBOUND_ATTACHMENT_BYTES:
                        raise ValueError("linked Workspace document exceeds the size limit")
                    return name, data.decode("utf-8")

                try:
                    parsed = await asyncio.to_thread(_fetch_workspace_text)
                    logger.info(
                        "[GoogleChat] Parsed Workspace text link with %s identity",
                        identity,
                    )
                    break
                except Exception as exc:
                    logger.warning(
                        "[GoogleChat] Workspace link parse with %s identity failed: %s",
                        identity,
                        _redact_sensitive(str(exc)),
                    )
            if parsed:
                name, content = parsed
                blocks.append(
                    f"SOURCE: {name} ({match.group(0)})\n{content[:100_000]}"
                )
            else:
                blocks.append(
                    f"SOURCE: {match.group(0)}\n"
                    "Workspace text parser could not read this link with ROBIE's approved identity."
                )
        if not blocks:
            return text
        return (
            text
            + "\n\n[GOOGLE WORKSPACE TEXT PARSER]\n"
            + "\n\n".join(blocks)
            + "\n[END GOOGLE WORKSPACE TEXT PARSER]"
        )

    # ------------------------------------------------------------------
    # Outbound send paths
    # ------------------------------------------------------------------
    def _remember_active_chat_job(self, chat_id: str | None, job_id: str | None) -> None:
        if chat_id and job_id:
            self._active_chat_job[str(chat_id)] = str(job_id)

    def _live_chat_job_id(self, chat_id: str | None) -> str | None:
        """The job this Chat turn is running, when the gateway omits it.

        Releasing the chat lock drops the active map. The reply job stays
        so a later send in the same turn cannot post the model's text.
        """
        if not chat_id:
            return None
        mapped = str((getattr(self, "_active_chat_job", None) or {}).get(chat_id) or "").strip()
        if mapped:
            return mapped
        turns = getattr(self, "_gateway_turns", None)
        if isinstance(turns, dict):
            for key, record in turns.items():
                if not isinstance(key, tuple) or str(key[0]) != str(chat_id):
                    continue
                if not isinstance(record, dict):
                    continue
                found = str(record.get("job_id") or "").strip()
                if found:
                    return found
        sticky = getattr(self, "_reply_job_by_chat", None)
        if isinstance(sticky, dict):
            found = str(sticky.get(chat_id) or "").strip()
            if found:
                return found
        return None

    def _remember_reply_job(self, chat_id: str | None, job_id: str | None) -> None:
        if not chat_id or not job_id:
            return
        sticky = getattr(self, "_reply_job_by_chat", None)
        if not isinstance(sticky, dict):
            sticky = {}
            self._reply_job_by_chat = sticky
        sticky[str(chat_id)] = str(job_id)

    def _sole_reply_already_sent(self, job_id: str | None) -> bool:
        sent = getattr(self, "_sole_outbound_sent", None)
        return bool(job_id and isinstance(sent, set) and str(job_id) in sent)

    def _mark_sole_reply_sent(self, job_id: str | None) -> None:
        if not job_id:
            return
        sent = getattr(self, "_sole_outbound_sent", None)
        if not isinstance(sent, set):
            sent = set()
            self._sole_outbound_sent = sent
        sent.add(str(job_id))

    def _agent_reply_is_replaced(self, job_id: str | None) -> bool:
        if not job_id:
            return False
        try:
            from robie_job_engine.chat_guard import agent_reply_is_replaced
            from robie_job_engine.store import JobStore

            return agent_reply_is_replaced(JobStore(ROBIE_JOB_DB), job_id)
        except Exception:
            logger.debug("[GoogleChat] could not tell if the model text is replaced", exc_info=True)
            return False

    async def _bind_inbound_job_thread(self, event: MessageEvent, job_id: str | None) -> None:
        """When the user replied inside a thread, keep that thread on the job."""
        if not job_id or event is None:
            return
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", None) if source else None
        self._remember_active_chat_job(chat_id, job_id)
        message_id = getattr(event, "message_id", None)
        raw_message = getattr(event, "raw_message", None) or {}
        raw_thread = ""
        if isinstance(raw_message, dict):
            thread = raw_message.get("thread") or {}
            if isinstance(thread, dict):
                raw_thread = str(thread.get("name") or "")
        session_thread = getattr(source, "thread_id", None) if source else None
        marked = self._reply_in_existing_thread.get(str(message_id or ""))

        def _bind() -> None:
            store = JobStore(ROBIE_JOB_DB)
            try:
                job = store.get_job(job_id)
            except KeyError:
                return
            name = inbound_thread_to_bind(
                job=job,
                message_id=message_id,
                session_thread_id=session_thread,
                raw_thread_name=raw_thread or marked,
                reply_in_existing_thread=bool(marked),
            )
            if name:
                bind_job_chat_thread(store, job_id, name)

        try:
            await asyncio.to_thread(_bind)
        except Exception:
            logger.exception("[GoogleChat] could not bind job thread job=%s", job_id)

    def _thread_spec_for_outbound(
        self,
        chat_id: str,
        metadata: Optional[Dict[str, Any]],
        *,
        job_id: str | None,
        job_owns_thread: bool,
        reply_to: str | None = None,
    ) -> Dict[str, str]:
        """thread.name for a bound job, or threadKey for the job's first message."""
        explicit = None
        if not job_owns_thread:
            explicit = self._resolve_thread_id(reply_to, metadata, chat_id=chat_id)
        stored = None
        if job_id:
            try:
                stored = read_job_chat_thread(JobStore(ROBIE_JOB_DB), job_id)
            except Exception:
                logger.debug(
                    "[GoogleChat] job thread lookup failed job=%s", job_id, exc_info=True
                )
        return outbound_thread_spec(
            job_id=job_id,
            stored_thread_name=stored,
            explicit_thread_name=explicit,
            prefer_job_thread_key=job_owns_thread,
        )

    def _media_thread_spec(
        self,
        chat_id: str,
        metadata: Optional[Dict[str, Any]],
        reply_to: str | None = None,
    ) -> Tuple[str | None, Dict[str, str]]:
        meta = metadata if isinstance(metadata, dict) else None
        job_id = str((meta or {}).get("robie_job_id") or "").strip() or None
        owns = bool(job_id)
        if not job_id:
            mapped = self._active_chat_job.get(chat_id)
            if mapped:
                job_id = mapped
                owns = True
            else:
                cron_job = str((meta or {}).get("job_id") or "").strip()
                if cron_job:
                    job_id = cron_job
                    owns = False
        return job_id, self._thread_spec_for_outbound(
            chat_id,
            meta,
            job_id=job_id,
            job_owns_thread=owns,
            reply_to=reply_to,
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message.

        Signature matches ``BasePlatformAdapter.send``: ``content`` is the
        message body, ``reply_to`` is an optional message_id (the inbound
        message to thread under), and ``metadata`` may carry ``thread_id``
        (the resolved Google Chat ``spaces/X/threads/Y`` resource name).

        If a typing card is tracked for this chat, transform it in-place via
        ``messages.patch`` — NO delete+create. Google Chat shows a tombstone
        ("Message deleted by its author") on delete, which is visual noise.
        Patch rewrites the text of the existing message seamlessly.

        Also pauses the base class's ``_keep_typing`` loop for this chat so
        it can't post a racing typing card between the patch and the reply.

        If ``content`` exceeds MAX_MESSAGE_LENGTH, the first chunk patches
        the typing card (if any), subsequent chunks are new messages.
        """
        delivery_kind = str((metadata or {}).get("robie_delivery_kind") or "")
        job_id = str((metadata or {}).get("robie_job_id") or "").strip() or None
        if delivery_kind == "idle_stop":
            # Nothing is running. Do not attach this reply to a finished job.
            job_id = None
        elif reply_to and not job_id:
            try:
                queue = await asyncio.to_thread(self._durable_chat_queue)
                link = await asyncio.to_thread(
                    queue.conversation_job_for_event, reply_to
                )
                job_id = link.get("job_id") if link else None
            except Exception:
                # Fail closed when this send cannot be tied to a job. A live
                # turn still has a job, and that job's guard replaces the text.
                logger.exception(
                    "[GoogleChat] durable reply-to-Job lookup failed reply=%s",
                    reply_to,
                )
                if not self._live_chat_job_id(chat_id):
                    return SendResult(
                        success=False,
                        error="durable reply-to-Job lookup failed",
                    )
                job_id = None
        # The agent's final text often arrives with no robie_job_id. This
        # turn's job is still the one whose reply this is. Busy, stop, and
        # notice sends keep the id they were given.
        agent_reply = delivery_kind not in {
            "idle_stop",
            "busy",
            "stop",
            "ceiling",
            "hard_block",
            "notice",
        } and not (metadata or {}).get("robie_stop_notice")
        if agent_reply and not job_id:
            job_id = self._live_chat_job_id(chat_id)
        if agent_reply and job_id:
            self._remember_reply_job(chat_id, job_id)
        if agent_reply and job_id and self._sole_reply_already_sent(job_id):
            return SendResult(success=True, message_id=None)
        # Thread routing uses the same job. A stored thread wins over the
        # inbound message's thread field, including a top-level auto thread.
        thread_job_id = job_id
        job_owns_thread = bool(job_id) and delivery_kind != "idle_stop"
        if delivery_kind != "idle_stop" and not thread_job_id:
            mapped = self._active_chat_job.get(chat_id)
            if mapped:
                thread_job_id = mapped
                job_owns_thread = True
            else:
                cron_job = str((metadata or {}).get("job_id") or "").strip()
                if cron_job:
                    thread_job_id = cron_job
                    job_owns_thread = False
        if not (metadata or {}).get("robie_stop_notice") and job_id:
            from robie_job_engine.chat_turn_control import agent_output_blocked

            blocked = agent_output_blocked(job_id, JobStore(ROBIE_JOB_DB))
            if blocked:
                logger.info("[GoogleChat] refusing send after stop job=%s", job_id)
                return SendResult(success=False, error=blocked)
        outbound_raw = str(content or "")
        content = redact_text(content)
        if delivery_kind == "idle_stop":
            from robie_job_engine.chat_turn_control import NOTHING_RUNNING_REPLY

            content = NOTHING_RUNNING_REPLY
        elif delivery_kind == "busy":
            # The running job is still open. Do not run the post-job guard,
            # which would rewrite this ack and replace the job's real reply.
            content = str(content or "").strip()
        elif (metadata or {}).get("robie_stop_notice") and delivery_kind == "stop":
            from robie_job_engine.chat_guard import guard_chat_notice
            from robie_job_engine.chat_turn_control import (
                NOTHING_RUNNING_REPLY,
                stop_reply_line,
            )

            # One fixed line. The post-job audit, including any tool-mismatch
            # note, stays in the ledger and is not sent. A job that already
            # finished keeps Nothing is running right now.
            if job_id and str(content or "").strip() != NOTHING_RUNNING_REPLY:
                content = stop_reply_line(job_id)
            content = guard_chat_notice(ROBIE_JOB_DB, job_id, content)
        elif (metadata or {}).get("robie_stop_notice") and delivery_kind == "ceiling":
            from robie_job_engine.chat_guard import guard_chat_notice

            content = guard_chat_notice(ROBIE_JOB_DB, job_id, content)
        elif delivery_kind in {"hard_block", "notice"}:
            content = str(content or "")
        else:
            content = guard_chat_response(ROBIE_JOB_DB, job_id, content)
        from robie_job_engine.user_reply import format_user_reply

        content = format_user_reply(content)
        thread_spec = self._thread_spec_for_outbound(
            chat_id,
            metadata,
            job_id=thread_job_id,
            job_owns_thread=job_owns_thread,
            reply_to=reply_to,
        )
        self.pause_typing_for_chat(chat_id)
        try:
            # Convert standard Markdown emitted by the LLM to Chat's dialect
            # and strip invisible Unicode that renders as tofu (□). Runs
            # BEFORE chunking so the size limit applies to the rendered
            # form, not the source markdown.
            chunks = self._chunk_text(self.format_message(content))
            if not chunks:
                return SendResult(success=False, error="empty message")

            last_result: Optional[SendResult] = None
            typing_msg_name = self._typing_messages.pop(chat_id, None)
            # Treat any earlier sentinel as "no real card to patch" — defensive.
            if typing_msg_name == _TYPING_CONSUMED_SENTINEL:
                typing_msg_name = None
            # Patching the typing card leaves the result in that card's
            # thread. The final result has to use the job's stored thread.
            create_on_job_thread = bool(thread_spec) and delivery_kind != "busy"
            if create_on_job_thread:
                typing_msg_name = None
            patched_typing = False

            for idx, chunk in enumerate(chunks):
                body: Dict[str, Any] = {"text": chunk}
                # Only set thread on new-message create path. Patch inherits
                # the thread the thinking card was created in.
                creating_new = idx > 0 or not typing_msg_name
                if thread_spec and creating_new:
                    body["thread"] = dict(thread_spec)
                try:
                    if idx == 0 and typing_msg_name:
                        result = await self._patch_message(typing_msg_name, body)
                        patched_typing = True
                    else:
                        result = await self._create_message(
                            chat_id, body, job_id=thread_job_id
                        )
                    last_result = result
                except HttpError as exc:
                    status = getattr(getattr(exc, "resp", None), "status", None)
                    if status == 403:
                        self._set_fatal_error(
                            code="chat_forbidden",
                            message="Bot lacks access (removed from space or perms revoked)",
                            retryable=False,
                        )
                        return SendResult(success=False, error=str(exc))
                    if status == 404:
                        # Typing card was deleted out from under us, or space
                        # is gone. Fall through to creating a new message on
                        # the first-chunk patch failure.
                        if idx == 0 and typing_msg_name:
                            logger.info(
                                "[GoogleChat] Typing card disappeared; creating new message"
                            )
                            typing_msg_name = None
                            if thread_spec and "thread" not in body:
                                body["thread"] = dict(thread_spec)
                            result = await self._create_message(
                                chat_id, body, job_id=thread_job_id
                            )
                            last_result = result
                            continue
                        logger.info("[GoogleChat] send target 404; skipping")
                        return SendResult(success=False, error="target not found")
                    if status == 429:
                        self._rate_limit_hits[chat_id] = (
                            self._rate_limit_hits.get(chat_id, 0) + 1
                        )
                        if self._rate_limit_hits[chat_id] >= _RATE_LIMIT_WARN_THRESHOLD:
                            logger.warning(
                                "[GoogleChat] Rate limit hit %d times on chat; throttling",
                                self._rate_limit_hits[chat_id],
                            )
                        raise
                    raise
            if last_result is None:
                return SendResult(success=False, error="empty message")
            # Mark the chat's typing slot as "consumed" so the base class's
            # _keep_typing loop (which may iterate one more time before
            # typing_task.cancel() lands) does not post a fresh marker that
            # the safety-net stop_typing would then delete and tombstone.
            # Cleared in on_processing_complete.
            if patched_typing:
                self._typing_messages[chat_id] = _TYPING_CONSUMED_SENTINEL
            delivery_kind = str((metadata or {}).get("robie_delivery_kind") or "")
            if (
                job_id
                and last_result is not None
                and getattr(last_result, "success", False)
                and (
                    (metadata or {}).get("robie_stop_notice")
                    or delivery_kind == "busy"
                )
            ):
                try:
                    from robie_job_engine.chat_turn_control import (
                        record_busy_reply,
                        record_chat_delivery,
                        sent_message_id,
                    )

                    posted_id = sent_message_id(last_result)
                    kind = str((metadata or {}).get("robie_delivery_kind") or "stop")
                    if kind == "busy":
                        await asyncio.to_thread(
                            record_busy_reply,
                            JobStore(ROBIE_JOB_DB),
                            job_id,
                            posted_id,
                            content,
                        )
                    else:
                        await asyncio.to_thread(
                            record_chat_delivery,
                            JobStore(ROBIE_JOB_DB),
                            job_id,
                            posted_id,
                            content,
                            kind,
                        )
                except Exception:
                    logger.exception(
                        "[GoogleChat] could not record stop delivery job=%s",
                        job_id,
                    )
            if getattr(last_result, "success", False):
                if agent_reply and job_id and self._agent_reply_is_replaced(job_id):
                    self._mark_sole_reply_sent(job_id)
                await self._finish_sent_reply(
                    chat_id,
                    job_id,
                    thread_job_id,
                    job_owns_thread,
                    delivery_kind,
                    outbound_raw,
                    content,
                    metadata,
                )
            return last_result
        finally:
            self.resume_typing_for_chat(chat_id)

    async def send_card(
        self,
        chat_id: str,
        card: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        body: Dict[str, Any] = {"cardsV2": [card]}
        meta = metadata if isinstance(metadata, dict) else {}
        job_id = str(meta.get("robie_job_id") or "").strip() or None
        job_owns_thread = bool(job_id)
        if not job_id:
            mapped = self._active_chat_job.get(chat_id)
            if mapped:
                job_id = mapped
                job_owns_thread = True
            else:
                cron_job = str(meta.get("job_id") or "").strip()
                if cron_job:
                    job_id = cron_job
                    job_owns_thread = False
        thread_spec = self._thread_spec_for_outbound(
            chat_id,
            meta or None,
            job_id=job_id,
            job_owns_thread=job_owns_thread,
        )
        if thread_spec:
            body["thread"] = dict(thread_spec)
        try:
            result = await self._create_message(chat_id, body, job_id=job_id)
            result.raw_response = result.raw_response or {"cardsV2": body["cardsV2"]}
            return result
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            return SendResult(
                success=False,
                error=_redact_sensitive(str(exc)),
                retryable=status in _RETRYABLE_HTTP_STATUSES,
            )
        except Exception as exc:
            logger.debug("[GoogleChat] send_card failed", exc_info=True)
            return SendResult(
                success=False,
                error=_redact_sensitive(str(exc)),
                retryable=_is_retryable_error(exc),
            )

    def _mark_clarify_waiting(self, chat_id: str, question: str) -> None:
        """An outbound question parks the job so the answer is not the busy reply."""
        job_id = self._active_chat_job.get(chat_id)
        if not job_id:
            return
        try:
            from robie_job_engine.chat_job_controls import (
                mark_job_waiting_for_user,
                stop_recordings_for_jobs,
            )

            if mark_job_waiting_for_user(JobStore(ROBIE_JOB_DB), job_id, question):
                status = str(
                    JobStore(ROBIE_JOB_DB).get_job(job_id).get("status") or ""
                )
                stop_recordings_for_jobs(ROBIE_JOB_DB, [job_id], status)
        except Exception:
            logger.exception("[GoogleChat] could not mark job waiting job=%s", job_id)

    async def _finish_sent_reply(
        self,
        chat_id: str,
        job_id: Optional[str],
        thread_job_id: Optional[str],
        job_owns_thread: bool,
        delivery_kind: str,
        outbound_raw: str,
        content: str,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        """Park a clarify, or close a running job once its reply is out."""
        if delivery_kind in {"idle_stop", "busy", "stop", "ceiling", "hard_block", "notice"}:
            return
        if (metadata or {}).get("robie_stop_notice"):
            return
        from robie_job_engine.chat_job_controls import (
            outbound_is_clarify,
            settle_job_when_reply_sent,
        )

        clarify = outbound_is_clarify(outbound_raw, content)
        target = job_id
        if not target and job_owns_thread and (clarify or len(str(content or "")) > 280):
            target = thread_job_id
        if not target:
            return
        if clarify:
            self._mark_clarify_waiting(chat_id, content)
        settled = False
        try:
            settled = bool(
                await asyncio.to_thread(
                    settle_job_when_reply_sent, ROBIE_JOB_DB, target, content
                )
            )
        except Exception:
            logger.exception("[GoogleChat] could not finish job after reply job=%s", target)
        if settled:
            from robie_job_engine.chat_turn_control import release_chat_lock

            release_chat_lock(self, chat_id, target)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        from robie_job_engine.user_reply import format_user_reply

        question = format_user_reply(question)
        self._mark_clarify_waiting(chat_id, question)
        if not choices:
            return await super().send_clarify(
                chat_id, question, choices, clarify_id, session_key, metadata
            )

        choice_widgets: List[Dict[str, Any]] = []
        short_buttons: List[Dict[str, Any]] = []
        has_long_choice = any(len(str(c).strip()) > 24 for c in choices)

        for choice in choices:
            choice_text = str(choice).strip()
            if not choice_text:
                continue
            # One free-text path is rendered below. Do not duplicate it when
            # the model includes an equivalent choice itself.
            if choice_text.casefold().rstrip(" .:!?/") in {
                "other",
                "other / type answer",
                "something else",
                "something else (type it out)",
            }:
                continue
            if has_long_choice:
                choice_widgets.append(
                    {
                        "type": "decorated_text",
                        "text": f"<b>{choice_text}</b>",
                        "wrap_text": True,
                        "button": {
                            "text": "Select",
                            "action": "hermes_clarify",
                            "parameters": {
                                "clarify_id": clarify_id,
                                "choice": choice_text,
                            },
                        },
                    }
                )
            else:
                label = choice_text if len(choice_text) <= 80 else choice_text[:77] + "..."
                short_buttons.append(
                    {
                        "text": label,
                        "action": "hermes_clarify",
                        "parameters": {
                            "clarify_id": clarify_id,
                            "choice": choice_text,
                        },
                    }
                )

        submit_btn = {
            "text": "Submit",
            "action": "hermes_clarify",
            "parameters": {
                "clarify_id": clarify_id,
                "choice": "__other__",
            },
        }

        if not choice_widgets and not short_buttons:
            return await super().send_clarify(
                chat_id, question, choices, clarify_id, session_key, metadata
            )

        widgets: List[Dict[str, Any]] = [
            {"type": "text", "text": f"❓ {question}"},
        ]
        if choice_widgets:
            widgets.extend(choice_widgets)
        elif short_buttons:
            widgets.append({"type": "buttons", "buttons": short_buttons})

        widgets.append(
            {
                "type": "text_input",
                "name": "custom_text",
                "label": "Something else",
                "hint": "Tell ROBIE what to do instead.",
                "multiline": True,
            }
        )
        widgets.append({"type": "buttons", "buttons": [submit_btn]})

        card = card_spec_to_cards_v2(
            {
                "card_id": f"clarify-{clarify_id}",
                "header": {"title": "Question"},
                "sections": [
                    {
                        "widgets": widgets,
                    }
                ],
            }
        )
        result = await self.send_card(chat_id, card, metadata=metadata)
        if result.success:
            self._clarify_state[clarify_id] = session_key
            return result
        return await super().send_clarify(
            chat_id, question, choices, clarify_id, session_key, metadata
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent message via ``messages.patch``.

        Required for the gateway tool-progress + token-streaming pipeline:
        ``GatewayStreamConsumer`` and ``send_progress_messages`` both gate
        on this method being overridden (see gateway/run.py:10199 and
        gateway/stream_consumer.py). Without it, Google Chat shows no
        tool activity (no "🔍 web_search…", no progressive token edits).

        ``message_id`` is the Google Chat resource name
        ``spaces/X/messages/Y``. ``finalize`` is unused here — Google
        Chat's patch API has no streaming lifecycle state, so the same
        patch closes the stream and any prior edit.

        404 (message gone) and 403 (perms revoked) are reported as
        non-success; the gateway falls back to ``send()`` for the next
        edit cycle.
        """
        if not message_id:
            return SendResult(success=False, error="missing message_id")
        live_job = self._live_chat_job_id(chat_id)
        if live_job and self._agent_reply_is_replaced(live_job):
            # Streaming the model's final text would post it in whatever
            # thread the bubble was opened in. The one allowed line goes
            # out through send(), on the job's stored thread.
            if self._sole_reply_already_sent(live_job):
                return SendResult(success=True, message_id=message_id)
            return await self.send(chat_id, content)
        from robie_job_engine.user_reply import format_user_reply

        content = format_user_reply(content)
        # Google Chat caps message text at 4096; we use 4000 elsewhere.
        if len(content) > _MAX_TEXT_LENGTH:
            content = content[: _MAX_TEXT_LENGTH - 1] + "…"
        try:
            return await self._patch_message(message_id, {"text": content})
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 429:
                self._rate_limit_hits[chat_id] = (
                    self._rate_limit_hits.get(chat_id, 0) + 1
                )
            return SendResult(
                success=False, error=_redact_sensitive(str(exc))
            )
        except Exception as exc:
            logger.debug("[GoogleChat] edit_message failed", exc_info=True)
            return SendResult(success=False, error=str(exc))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message — used sparingly (deletion creates a tombstone).

        The base contract returns False on unsupported. We do support it,
        but most internal code should prefer ``edit_message`` to avoid the
        "Message deleted by its author" tombstone. Provided so the
        gateway's stream-consumer fallback paths (e.g. removing an aborted
        partial preview) work correctly when explicit deletion is the
        right call.
        """
        if not message_id:
            return False

        def _do_delete() -> None:
            (
                self._chat_api.spaces()
                .messages()
                .delete(name=message_id)
                .execute(http=self._new_authed_http())
            )

        try:
            await asyncio.to_thread(_do_delete)
            return True
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in {403, 404}:
                return False
            logger.debug(
                "[GoogleChat] delete_message failed: %s",
                _redact_sensitive(str(exc)),
            )
            return False
        except Exception:
            logger.debug("[GoogleChat] delete_message failed", exc_info=True)
            return False

    async def _patch_message(
        self, message_name: str, body: Dict[str, Any]
    ) -> SendResult:
        """Update a message's text (and optionally cards) in-place."""
        from robie_job_engine.user_reply import format_user_reply

        if isinstance(body.get("text"), str):
            body = dict(body)
            body["text"] = format_user_reply(body["text"], collapse=False)
        update_mask_fields = []
        if "text" in body:
            update_mask_fields.append("text")
        if "cardsV2" in body:
            update_mask_fields.append("cardsV2")
        update_mask = ",".join(update_mask_fields) or "text"

        # Patch body cannot carry thread (immutable).
        patch_body = {k: v for k, v in body.items() if k not in {"thread",}}

        def _do_patch() -> Dict[str, Any]:
            return (
                self._chat_api.spaces()
                .messages()
                .patch(name=message_name, updateMask=update_mask, body=patch_body)
                .execute(http=self._new_authed_http())
            )

        resp = await asyncio.to_thread(_do_patch)
        return SendResult(success=True, message_id=resp.get("name", message_name))

    def _chunk_text(self, text: str) -> List[str]:
        if not text:
            return []
        if len(text) <= _MAX_TEXT_LENGTH:
            return [text]
        chunks: List[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= _MAX_TEXT_LENGTH:
                chunks.append(remaining)
                break
            # Try to split on a newline near the cutoff.
            cut = remaining.rfind("\n", 0, _MAX_TEXT_LENGTH)
            if cut < _MAX_TEXT_LENGTH // 2:
                cut = _MAX_TEXT_LENGTH
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip()
        return chunks

    # ------------------------------------------------------------------
    # Outbound formatting
    # ------------------------------------------------------------------
    # Invisible Unicode codepoints that render as tofu (□) in Google
    # Chat's restricted font stack. ZWJ/ZWNJ/ZWS are the glue inside
    # composite emoji and bidirectional text; Variation Selectors
    # control text-vs-emoji presentation but Chat ignores them and
    # often shows a blank box. Pattern lifted from PR #14965.
    _INVISIBLE_RE = re.compile(
        "["
        "​"          # Zero-Width Space
        "‌"          # Zero-Width Non-Joiner
        "‍"          # Zero-Width Joiner (ZWJ)
        "‎‏"    # LTR / RTL marks
        "⁠"          # Word Joiner
        "﻿"          # BOM / Zero-Width No-Break Space
        "︀-️"   # Variation Selectors 1-16 (VS1–VS16)
        "\U000e0100-\U000e01ef"  # Variation Selectors 17-256
        "]"
    )

    @classmethod
    def format_message(cls, content: str) -> str:
        """Convert standard Markdown to Google Chat's formatting dialect.

        Google Chat renders a small subset: ``*bold*``, ``_italic_``,
        ``~strikethrough~``, fenced/inline code. Standard Markdown
        constructs (``**bold**``, ``# headers``, ``[text](url)``) do
        not render and need conversion before they reach Chat.

        Code blocks (fenced AND inline) are protected from transformation
        via placeholder substitution so backticks-wrapped content with
        literal asterisks or brackets stays intact. Invisible Unicode
        codepoints that render as tofu in Chat's restricted font stack
        are stripped at the end. Empty/None input passes through.

        Pattern lifted from PR #14965.
        """
        if not content:
            return content

        text = content
        placeholders: Dict[str, str] = {}
        counter = [0]

        def _ph(value: str) -> str:
            key = f"\x00GC{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key

        # Protect fenced and inline code blocks from transformation.
        # Fenced blocks first (``` ... ```), then inline code (`...`).
        text = re.sub(
            r"(```(?:[^\n]*\n)?[\s\S]*?```)",
            lambda m: _ph(m.group(0)),
            text,
        )
        text = re.sub(r"(`[^`]+`)", lambda m: _ph(m.group(0)), text)

        # Headers (## Title) → *Title* (Chat has no header support).
        text = re.sub(
            r"^#{1,6}\s+(.+)$",
            lambda m: _ph(f"*{m.group(1).strip()}*"),
            text,
            flags=re.MULTILINE,
        )

        # Bold+italic: ***text*** → *_text_*
        text = re.sub(
            r"\*\*\*(.+?)\*\*\*",
            lambda m: _ph(f"*_{m.group(1)}_*"),
            text,
        )

        # Bold: **text** → *text* (Chat uses single asterisks).
        text = re.sub(
            r"\*\*(.+?)\*\*",
            lambda m: _ph(f"*{m.group(1)}*"),
            text,
        )

        # Markdown links [text](url) → <url|text> (Slack-style angle-bracket).
        text = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: _ph(f"<{m.group(2)}|{m.group(1)}>"),
            text,
        )

        # Strip invisible Unicode that renders as tofu.
        text = cls._INVISIBLE_RE.sub("", text)

        # Collapse double spaces left over from stripped chars.
        text = re.sub(r"  +", " ", text)

        # Restore protected regions.
        for key, value in placeholders.items():
            text = text.replace(key, value)

        return text

    def _resolve_thread_id(
        self,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
        chat_id: Optional[str] = None,
    ) -> Optional[str]:
        """Return the Google Chat thread resource name to reply under, or None.

        Priority:
          1. ``metadata['thread_id']`` — populated by the gateway's session
             plumbing from ``SessionSource.thread_id`` (the inbound
             ``thread.name``). Canonical path for groups.
          2. ``metadata['thread_name']`` / ``metadata['thread_ts']`` — Slack
             precedent aliases that the broader codebase sometimes passes.
          3. ``reply_to`` if it already looks like a thread resource name
             (``spaces/X/threads/Y``). Message names ``spaces/X/messages/Y``
             cannot be converted to threads without an extra API call.
          4. ``self._last_inbound_thread[chat_id]`` — Google Chat DMs spawn
             a new thread per top-level user message, and the adapter
             intentionally drops thread_id from the source so the session
             key stays stable. Without this fallback, DM replies would
             land at top-level (a fresh thread separate from the user's),
             visually disconnected from the user's question.
        """
        if metadata:
            for key in ("thread_id", "thread_name", "thread_ts"):
                value = metadata.get(key)
                if value:
                    return str(value)
        if reply_to and "/threads/" in reply_to and "/messages/" not in reply_to:
            return reply_to
        # Cron deliveries (job_id present in metadata) must post as a new
        # top-level message unless an explicit thread was requested above. The
        # _last_inbound_thread fallback below exists for interactive DMs, where
        # Google Chat spawns a fresh thread per top-level user message and the
        # adapter drops thread_id to keep the session key stable. Replaying
        # that fallback for a cron output would reply inside a stale inbound
        # thread instead of starting a new one, burying the delivery.
        if metadata and metadata.get("job_id"):
            return None
        if chat_id:
            cached = self._last_inbound_thread.get(chat_id)
            if cached:
                return cached
        return None

    def _new_authed_http(self) -> Any:
        """Return a fresh AuthorizedHttp.

        googleapiclient's discovery client is NOT thread-safe because httplib2
        shares SSL state between calls. Passing a fresh http= to each
        ``execute()`` avoids record-layer failures when calls run in
        ``asyncio.to_thread`` workers. Cheap (~no network).
        """
        return AuthorizedHttp(self._credentials, http=httplib2.Http(timeout=30))

    async def _call_with_retry(
        self,
        sync_fn: Callable[[], Any],
        *,
        op_name: str = "chat-api-call",
    ) -> Any:
        """Run ``sync_fn`` in a thread with bounded retry + jittered backoff.

        Wraps a sync Chat API call (typically a ``.execute()``) so transient
        429/5xx/timeout failures don't drop user-visible messages. Permanent
        failures (auth, client errors, validation) bubble up on the first
        attempt — see :func:`_is_retryable_error`. Cancellation propagates
        immediately, no extra retries after a CancelledError.

        Pattern lifted from PR #14965.
        """
        delay = _RETRY_BASE_DELAY
        last_exc: Optional[BaseException] = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(sync_fn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                retryable = _is_retryable_error(exc)
                if not retryable or attempt >= _RETRY_MAX_ATTEMPTS:
                    raise
                jitter = delay * _RETRY_JITTER * random.random()
                wait = min(delay + jitter, _RETRY_MAX_DELAY + _RETRY_JITTER)
                logger.warning(
                    "[GoogleChat] %s attempt %d/%d failed (%s); "
                    "retrying in %.2fs",
                    op_name, attempt, _RETRY_MAX_ATTEMPTS,
                    _redact_sensitive(str(exc)), wait,
                )
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    raise
                delay = min(delay * 2, _RETRY_MAX_DELAY)
        # Defensive — the loop above always either returns or re-raises.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"{op_name}: retry loop exited without result")

    async def _create_message(
        self, chat_id: str, body: Dict[str, Any], job_id: str | None = None,
    ) -> SendResult:
        """POST spaces/{space}/messages via REST, returning SendResult.

        When ``body`` carries ``thread.name``, we MUST pass
        ``messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`` —
        otherwise Google Chat silently ignores ``thread.name`` and
        creates a new thread anyway. From the official docs:

            "Default. Starts a new thread. Using this option ignores
             any thread ID or threadKey that's included."

        See https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create
        """
        from robie_job_engine.user_reply import format_user_reply

        if isinstance(body.get("text"), str):
            body = dict(body)
            body["text"] = format_user_reply(body["text"], collapse=False)
        kwargs: Dict[str, Any] = {"parent": chat_id, "body": body}
        thread_meta = body.get("thread") or {}
        if thread_meta.get("name") or thread_meta.get("threadKey"):
            # FALLBACK_TO_NEW_THREAD: try the requested thread; if Chat
            # can't route there (e.g. thread no longer exists), create a
            # new one rather than erroring. Safer than REPLY_MESSAGE_OR_FAIL
            # for a chat-bot context where stale thread names are rare
            # but possible. Required for both thread.name and threadKey —
            # the default option ignores both.
            kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

        def _do_create() -> Dict[str, Any]:
            return (
                self._chat_api.spaces()
                .messages()
                .create(**kwargs)
                .execute(http=self._new_authed_http())
            )

        resp = await self._call_with_retry(_do_create, op_name="messages.create")
        # Track outbound destination thread in the persistent count store
        # so a future user "Reply in thread" on the bot's message resolves
        # to a known thread (prev_count >= 1 → side thread). Without
        # this, threads created by the bot's own outbound look fresh
        # the first time the user engages them, and the heuristic
        # incorrectly classifies the engagement as main-flow → bot
        # replies at top-level instead of in the thread.
        resp_thread = (resp.get("thread") or {}).get("name") or ""
        if chat_id and resp_thread:
            try:
                self._thread_count_store.incr(chat_id, resp_thread)
            except Exception:
                logger.debug(
                    "[GoogleChat] outbound thread-count incr failed",
                    exc_info=True,
                )
        if job_id and resp_thread:
            try:
                remember_created_thread(JobStore(ROBIE_JOB_DB), job_id, resp)
            except Exception:
                logger.debug(
                    "[GoogleChat] could not store created job thread",
                    exc_info=True,
                )
        return SendResult(success=True, message_id=resp.get("name"))

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        """Post a visible 'Hermes is thinking…' marker message.

        NOT ephemeral (Google Chat has no ephemeral text messages outside
        slash command responses). ``send()`` PATCHes this marker in-place
        with the real response (no deletion tombstone). The typing card is
        either patched by ``send()`` (success) or by
        ``on_processing_complete`` (failure / cancellation).

        IMPORTANT — must place the typing card in the user's thread:
        ``messages.patch`` cannot change a message's ``thread`` (it's
        immutable on update). If we create the typing card at top-level
        and the user is replying inside thread T, send() will patch the
        top-level card in place — leaving the bot's whole response
        stranded outside the user's thread. We resolve the thread the
        same way send() does.

        IMPORTANT — cancellation safety:
        ``base.py``'s ``_keep_typing`` calls this through
        ``asyncio.wait_for(send_typing, timeout=1.5)``. When the
        create-API call takes longer than 1.5s, ``wait_for`` cancels
        ``send_typing`` mid-flight — but the underlying ``asyncio.to_thread``
        keeps running and creates a card in Chat that we have NO way to
        track (the storage line never runs). Next ``_keep_typing`` tick
        sees an empty slot and creates a SECOND card. Result: one orphan
        "Hermes is thinking…" stuck in chat forever, plus one card that
        gets patched into the reply.

        Fix: reserve the slot with an in-flight ``Event``, run the
        create in a background task, and ``await asyncio.shield`` it.
        Cancellation of THIS coroutine no longer cancels the create —
        the task runs to completion and the msg_id lands in the slot
        regardless.
        """
        # Already have a card (real msg_id, sentinel, or in-flight) — bail.
        if chat_id in self._typing_messages:
            return
        if chat_id in self._typing_card_inflight:
            # Another create is already running for this chat. Wait for
            # it to finish so we honor the contract "if called, the card
            # is up by the time we return". Bounded wait — if the
            # background task is stuck, _keep_typing will retry.
            try:
                await asyncio.wait_for(
                    self._typing_card_inflight[chat_id].wait(),
                    timeout=5.0,
                )
            except (asyncio.TimeoutError, KeyError):
                pass
            return

        meta = metadata if isinstance(metadata, dict) else None
        job_id = str((meta or {}).get("robie_job_id") or "").strip() or None
        job_owns_thread = bool(job_id)
        if not job_id:
            mapped = self._active_chat_job.get(chat_id)
            if mapped:
                job_id = mapped
                job_owns_thread = True
            else:
                cron_job = str((meta or {}).get("job_id") or "").strip()
                if cron_job:
                    job_id = cron_job
                    job_owns_thread = False
        thread_spec = self._thread_spec_for_outbound(
            chat_id,
            meta,
            job_id=job_id,
            job_owns_thread=job_owns_thread,
        )
        typing_choices = [
            "Robie is thinking…",
            "Robie is working on it…",
            "Robie is on it…",
            "Robie is pulling up the files…",
            "Robie is crunching the details…",
            "Robie is handling this…",
        ]
        status_text = getattr(self.config, "typing_status_text", None)
        if not status_text or status_text == "Robie is thinking…" or status_text == "Hermes is thinking…":
            status_text = random.choice(typing_choices)
        body: Dict[str, Any] = {
            "text": status_text
        }
        if thread_spec:
            body["thread"] = dict(thread_spec)

        completed = asyncio.Event()
        self._typing_card_inflight[chat_id] = completed

        async def _create_and_record() -> None:
            try:
                result = await self._create_message(chat_id, body, job_id=job_id)
                if result.success and result.message_id:
                    # Only overwrite the slot if nothing else has claimed it
                    # in the meantime (e.g. send() racing ahead of us).
                    if chat_id not in self._typing_messages:
                        self._typing_messages[chat_id] = result.message_id
                    else:
                        # Slot already populated — likely send() patched
                        # something or another create completed first.
                        # Our card is ORPHANED here, but at least it's a
                        # known orphan we can clean up at end of turn.
                        # Track for cleanup by on_processing_complete.
                        self._orphan_typing_messages.setdefault(
                            chat_id, []
                        ).append(result.message_id)
            except Exception:
                logger.debug(
                    "[GoogleChat] send_typing background create failed",
                    exc_info=True,
                )
            finally:
                self._typing_card_inflight.pop(chat_id, None)
                completed.set()

        task = asyncio.create_task(_create_and_record())
        # Shield the task from cancellation of our awaiter. If
        # _keep_typing's wait_for times out, our coroutine is cancelled
        # but the task continues in the background — so the msg_id
        # eventually lands in the slot even when the API call is slow.
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The shielded task keeps running. Re-raise so the caller's
            # cancellation semantics are preserved.
            raise

    async def stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator — NO-OP when a live card is tracked.

        Google Chat has no separate typing API: the "Hermes is thinking…"
        marker is a real message that ``send()`` patches in-place with the
        agent's reply. Deleting the marker creates a "Message deleted by
        its author" tombstone, which is visual noise.

        Upstream code (gateway/run.py and gateway/platforms/base.py) calls
        ``stop_typing`` at three moments per turn — typically BEFORE
        ``send()`` runs (so deleting the slot would leave ``send()``
        nothing to patch, forcing it to create a fresh message and leaving
        the original card as a tombstone). To fix this without modifying
        upstream contracts, ``stop_typing`` here is intentionally a NO-OP
        when the slot holds a real ``message_name``: the card is left in
        place so ``send()`` can patch it.

        Three cases:
          * Slot empty → nothing to do.
          * Slot holds SENTINEL → ``send()`` already patched the card;
            pop the sentinel so the next turn starts clean.
          * Slot holds a real ``message_name`` → leave it for ``send()``
            to consume. NO-OP.

        Stranded cards on error / cancellation paths (where ``send()``
        never runs) are reaped by ``on_processing_complete`` — see that
        hook for the patch-to-final-state cleanup.
        """
        current = self._typing_messages.get(chat_id)
        if not current:
            return
        if current == _TYPING_CONSUMED_SENTINEL:
            self._typing_messages.pop(chat_id, None)
            return
        # Real message_name — leave it for send() to patch. Deliberate no-op.
        return

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        """Reap typing card(s) after the message-handling cycle ends.

        SUCCESS: ``send()`` set the SENTINEL after patching. Pop it.

        FAILURE / CANCELLED: ``send()`` may not have run, leaving a real
        ``message_name`` in the slot. Patching the card to a final state
        (``"(interrupted)"``) avoids the tombstone that ``messages.delete``
        would create. If ``send()`` did run (e.g. base.py error-send branch
        patched it), the slot holds the SENTINEL — pop and exit.

        Orphan cards: when a background ``send_typing`` task creates a
        card AFTER ``send()`` already populated the slot (race window
        when the API call takes longer than _keep_typing's wait_for
        timeout), the orphan id is stashed in ``self._orphan_typing_messages``.
        Patch each orphan with an empty-ish marker so the user doesn't
        see "Hermes is thinking…" stuck forever.
        """
        if event.source is None:
            return
        chat_id = event.source.chat_id
        try:
            current = self._typing_messages.pop(chat_id, None)
            if current and current != _TYPING_CONSUMED_SENTINEL:
                # Real message_name still in slot — send() never ran. Patch
                # with a benign final state instead of deleting (no tombstone).
                label = (
                    "(interrupted)" if outcome == ProcessingOutcome.CANCELLED
                    else "(no reply)"
                )
                try:
                    await self._patch_message(current, {"text": label})
                except Exception:
                    logger.debug(
                        "[GoogleChat] on_processing_complete patch fallback failed",
                        exc_info=True,
                    )
            # Reap orphan typing cards (background creates that lost a
            # race with send()). Patch them to a single dot so they
            # gracefully retire — the user already saw the real reply
            # in another card, this one is just visual noise to clear.
            orphans = self._orphan_typing_messages.pop(chat_id, [])
            for orphan_id in orphans:
                try:
                    await self._patch_message(orphan_id, {"text": "·"})
                except Exception:
                    logger.debug(
                        "[GoogleChat] orphan typing-card patch failed: %s",
                        orphan_id, exc_info=True,
                    )
        except Exception:
            logger.debug(
                "[GoogleChat] cleanup in on_processing_complete failed", exc_info=True
            )

    # ------------------------------------------------------------------
    # Attachment send paths
    # ------------------------------------------------------------------
    async def _consume_typing_card_with_text(
        self, chat_id: str, text: str
    ) -> Optional[SendResult]:
        """Patch the tracked typing card with ``text`` (no tombstone).

        Returns ``None`` if there's no real typing card to patch (caller
        should create a new message). Returns the patch result if the
        card was successfully patched. Raises on transient HttpErrors so
        the caller can decide whether to fall back to ``_create_message``.

        Leaves the SENTINEL in place when present: a previous ``send()``
        already consumed the typing card, and the SENTINEL must stay in
        the slot to keep the base class's ``_keep_typing`` loop from
        creating a fresh "Hermes is thinking…" card during any subsequent
        attachment send (which would later be reaped as "(no reply)").
        """
        current = self._typing_messages.get(chat_id)
        if not current or current == _TYPING_CONSUMED_SENTINEL:
            return None
        # Real msg_id — pop and patch.
        self._typing_messages.pop(chat_id, None)
        try:
            result = await self._patch_message(current, {"text": text})
            self._typing_messages[chat_id] = _TYPING_CONSUMED_SENTINEL
            return result
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                # Card disappeared — caller should create a new message.
                return None
            raise

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an inline image via attachment URL (no upload).

        If a typing card is tracked for this chat, patch it in-place with
        the image (caption + URL) — same anti-tombstone pattern used by
        ``send()``. Otherwise create a new message.
        """
        job_id, thread_spec = self._media_thread_spec(chat_id, metadata, reply_to)
        text_parts: List[str] = []
        if caption:
            text_parts.append(caption)
        text_parts.append(image_url)
        text = "\n".join(text_parts)

        try:
            patched = await self._consume_typing_card_with_text(chat_id, text)
            if patched is not None:
                return patched
            body: Dict[str, Any] = {"text": text}
            if thread_spec:
                body["thread"] = dict(thread_spec)
            return await self._create_message(chat_id, body, job_id=job_id)
        except HttpError as exc:
            return SendResult(success=False, error=_redact_sensitive(str(exc)))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        _job_id, thread_spec = self._media_thread_spec(
            chat_id, kwargs.get("metadata"), reply_to
        )
        return await self._send_file(
            chat_id, image_path, caption,
            mime_hint="image/*",
            thread_id=thread_spec.get("name"),
            thread_key=thread_spec.get("threadKey"),
            job_id=_job_id,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        _job_id, thread_spec = self._media_thread_spec(
            chat_id, kwargs.get("metadata"), reply_to
        )
        return await self._send_file(
            chat_id, file_path, caption,
            mime_hint=None,
            thread_id=thread_spec.get("name"),
            thread_key=thread_spec.get("threadKey"),
            job_id=_job_id,
            override_filename=file_name,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        _job_id, thread_spec = self._media_thread_spec(
            chat_id, kwargs.get("metadata"), reply_to
        )
        return await self._send_file(
            chat_id, audio_path, caption,
            mime_hint="audio/ogg",
            thread_id=thread_spec.get("name"),
            thread_key=thread_spec.get("threadKey"),
            job_id=_job_id,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        _job_id, thread_spec = self._media_thread_spec(
            chat_id, kwargs.get("metadata"), reply_to
        )
        return await self._send_file(
            chat_id, video_path, caption,
            mime_hint="video/mp4",
            thread_id=thread_spec.get("name"),
            thread_key=thread_spec.get("threadKey"),
            job_id=_job_id,
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Google Chat has no native animation type; fall back to send_image."""
        return await self.send_image(
            chat_id, animation_url, caption=caption,
            reply_to=reply_to, metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Native attachment delivery via user OAuth
    #
    # Google Chat's media.upload endpoint hard-rejects SA authentication
    # ("This method doesn't support app authentication with a service
    # account"). The bot itself cannot upload files. Instead the user
    # grants the bot the chat.messages.create scope ONCE via an in-chat
    # OAuth consent flow (``/setup-files``); the resulting refresh token
    # lets the bot call media.upload AS the user, producing native Chat
    # attachments (file widget, inline preview, click-to-download).
    #
    # See https://developers.google.com/chat/api/guides/auth/users for
    # the upstream limitation that makes user OAuth necessary, and
    # ``plugins/platforms/google_chat/oauth.py`` for the helper
    # script + library functions backing this path.
    # ------------------------------------------------------------------
    @staticmethod
    def _is_app_auth_attachment_error(exc: HttpError) -> bool:
        """Detect Google Chat's media.upload bot-auth rejection.

        Returns True for the canonical ``"doesn't support app
        authentication"`` wording (and the legacy
        ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` variant some older clients
        still see). Used to flag a misuse — calling ``media.upload``
        through the SA-authed Chat API client instead of the user-authed
        one. With correct routing this error should never fire in the
        adapter; it remains as a defensive check.
        """
        text = str(exc) or ""
        return (
            "doesn't support app authentication" in text
            or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text
        )

    _LEGACY_USER_IDENTITY = "__legacy__"

    async def _load_per_user_chat_api(self, email: str) -> Optional[Any]:
        """Get (or build + cache) a user-authed Chat client for ``email``.

        Hits ``self._user_chat_api_by_email`` first; on miss, loads the
        per-user token from disk, refreshes if needed, builds an API
        client, and caches both. Refresh failures evict the slot so the
        next request goes back through the disk path (and ultimately the
        text-notice fallback if the user has revoked).
        """
        from .oauth import (
            load_user_credentials as _load,
            build_user_chat_service as _build,
            refresh_or_none as _refresh,
        )

        cached_api = self._user_chat_api_by_email.get(email)
        cached_creds = self._user_creds_by_email.get(email)
        if cached_api is not None and cached_creds is not None:
            try:
                refreshed = await asyncio.to_thread(_refresh, cached_creds, email)
            except Exception:
                logger.debug(
                    "[GoogleChat] cached per-user refresh raised", exc_info=True,
                )
                refreshed = None
            if refreshed is None:
                self._user_chat_api_by_email.pop(email, None)
                self._user_creds_by_email.pop(email, None)
                return None
            self._user_creds_by_email[email] = refreshed
            return cached_api

        try:
            creds = await asyncio.to_thread(_load, email)
            if creds is None:
                return None
            api = await asyncio.to_thread(lambda: _build(creds))
        except Exception:
            logger.debug(
                "[GoogleChat] per-user creds load/build failed for %s",
                email, exc_info=True,
            )
            return None

        self._user_creds_by_email[email] = creds
        self._user_chat_api_by_email[email] = api
        return api

    async def _acquire_user_chat_api(
        self, sender_email: Optional[str]
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Resolve the user-authed Chat client for an outbound attachment.

        Lookup order:
          1. Per-user token for ``sender_email`` — the asker's identity.
          2. Legacy single-user fallback (``self._user_chat_api``) for
             pre-multi-user installs.
          3. None — caller posts the setup-instructions text notice.

        Returns ``(client, identity_label)`` where ``identity_label`` is
        the sanitized email or the literal ``"__legacy__"`` sentinel.
        ``_invalidate_user_creds`` uses the label to evict the right slot
        on auth failure.
        """
        if sender_email:
            api = await self._load_per_user_chat_api(sender_email)
            if api is not None:
                return api, sender_email

        if self._user_chat_api is not None:
            try:
                from .oauth import (
                    refresh_or_none as _refresh,
                )
                refreshed = await asyncio.to_thread(
                    _refresh, self._user_credentials, None,
                )
            except Exception:
                logger.debug(
                    "[GoogleChat] legacy creds refresh raised", exc_info=True,
                )
                refreshed = None
            if refreshed is None:
                logger.warning(
                    "[GoogleChat] legacy user-OAuth refresh returned None — "
                    "evicting fallback creds"
                )
                self._user_credentials = None
                self._user_chat_api = None
                return None, None
            self._user_credentials = refreshed
            return self._user_chat_api, self._LEGACY_USER_IDENTITY

        return None, None

    def _invalidate_user_creds(self, identity: Optional[str]) -> None:
        """Drop creds for ``identity`` after an auth failure.

        ``identity`` comes from ``_acquire_user_chat_api`` — either the
        sender email (per-user slot) or ``__legacy__`` for the fallback
        slot. None is a no-op.
        """
        if not identity:
            return
        if identity == self._LEGACY_USER_IDENTITY:
            self._user_credentials = None
            self._user_chat_api = None
            return
        self._user_creds_by_email.pop(identity, None)
        self._user_chat_api_by_email.pop(identity, None)

    async def _send_file(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str],
        mime_hint: Optional[str],
        thread_id: Optional[str] = None,
        override_filename: Optional[str] = None,
        thread_key: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> SendResult:
        """Native Chat attachment via user-OAuth media.upload.

        Two-step on the wire: ``media.upload`` then
        ``spaces.messages.create`` with the returned ``attachmentDataRef``.
        BOTH calls go through a user-authed Chat API client — the
        SA-authed client is rejected by ``media.upload`` regardless of
        scopes.

        Multi-user routing: the bot looks up the most recent inbound
        sender for this ``chat_id`` and uses THAT user's stored OAuth
        token. Falls back to a legacy single-user token when present
        (for pre-multi-user installs), and to a setup-instructions text
        notice when neither is available.

        Google Chat ``messages.patch`` cannot add an attachment to an
        existing message, so we cannot transform the typing card directly
        into the file message. Instead we patch the typing card with the
        caption (or a single space when none) so it retires without a
        tombstone, then create the attachment message.
        """
        if not os.path.exists(path):
            return SendResult(success=False, error=f"file not found: {path}")

        filename = override_filename or os.path.basename(path) or "upload.bin"
        mime = mime_hint or "application/octet-stream"

        sender_email = self._last_sender_by_chat.get(chat_id)
        chat_api, identity = await self._acquire_user_chat_api(sender_email)

        # No user OAuth → can't upload natively. Surface clear setup
        # instructions in chat instead of silently failing.
        if chat_api is None:
            return await self._post_attachment_fallback(
                chat_id=chat_id,
                path=path,
                filename=filename,
                caption=caption,
                thread_id=thread_id,
                thread_key=thread_key,
                job_id=job_id,
            )

        # Pre-patch the typing card with the caption (or single space) so
        # it retires without a tombstone before the attachment message is
        # posted.
        try:
            await self._consume_typing_card_with_text(chat_id, caption or " ")
        except Exception:
            logger.debug(
                "[GoogleChat] _send_file pre-patch typing-card failed",
                exc_info=True,
            )

        def _upload() -> Dict[str, Any]:
            media = MediaFileUpload(path, mimetype=mime, resumable=False)
            return (
                chat_api.media()
                .upload(
                    parent=chat_id,
                    body={"filename": filename},
                    media_body=media,
                )
                .execute()
            )

        try:
            upload_resp = await asyncio.to_thread(_upload)
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in {401, 403}:
                logger.warning(
                    "[GoogleChat] media.upload auth failure for identity=%s "
                    "(token revoked or scope missing) — falling back to "
                    "text notice. Status=%s", identity, status,
                )
                self._invalidate_user_creds(identity)
                return await self._post_attachment_fallback(
                    chat_id=chat_id,
                    path=path,
                    filename=filename,
                    caption=caption,
                    thread_id=thread_id,
                    thread_key=thread_key,
                    job_id=job_id,
                )
            return SendResult(
                success=False, error=_redact_sensitive(str(exc))
            )

        attachment_ref = upload_resp.get("attachmentDataRef")
        if not attachment_ref:
            return SendResult(
                success=False,
                error="upload returned no attachmentDataRef",
            )

        body: Dict[str, Any] = {
            "attachment": [{"attachmentDataRef": attachment_ref}],
        }
        if caption:
            body["text"] = caption
        if thread_id:
            body["thread"] = {"name": thread_id}
        elif thread_key:
            body["thread"] = {"threadKey": thread_key}

        # The accompanying messages.create that references the attachment
        # also needs user auth (the attachmentDataRef is bound to the
        # uploading principal). messageReplyOption is required for the
        # thread.name or threadKey in body to actually be honored — see
        # _create_message docstring for the API quirk.
        create_kwargs: Dict[str, Any] = {"parent": chat_id, "body": body}
        if thread_id or thread_key:
            create_kwargs["messageReplyOption"] = (
                "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
            )

        def _create_with_attachment() -> Dict[str, Any]:
            return (
                chat_api.spaces()
                .messages()
                .create(**create_kwargs)
                .execute()
            )

        try:
            resp = await asyncio.to_thread(_create_with_attachment)
            # Track outbound destination thread (see _create_message
            # comment for why — same reasoning applies to the
            # user-OAuth attachment path).
            resp_thread = (resp.get("thread") or {}).get("name") or ""
            if chat_id and resp_thread:
                try:
                    self._thread_count_store.incr(chat_id, resp_thread)
                except Exception:
                    logger.debug(
                        "[GoogleChat] outbound thread-count incr failed",
                        exc_info=True,
                    )
            if job_id and resp_thread:
                try:
                    remember_created_thread(JobStore(ROBIE_JOB_DB), job_id, resp)
                except Exception:
                    logger.debug(
                        "[GoogleChat] could not store attachment job thread",
                        exc_info=True,
                    )
            return SendResult(
                success=True, message_id=resp.get("name"),
            )
        except HttpError as exc:
            return SendResult(
                success=False, error=_redact_sensitive(str(exc))
            )

    async def _post_attachment_fallback(
        self,
        chat_id: str,
        path: str,
        filename: str,
        caption: Optional[str],
        thread_id: Optional[str],
        thread_key: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> SendResult:
        """Post a text notice when native attachment delivery is unavailable.

        Tells the user that file delivery requires a one-time consent
        flow (``/setup-files``) and reports the local-host path so the
        file isn't lost. Returns ``success=False`` so callers know the
        attachment did not land.
        """
        lines = []
        if caption:
            lines.append(caption)
        lines.extend([
            f"⚠️ No he podido adjuntar **{filename}**.",
            "Google Chat sólo permite adjuntar archivos cuando el bot tiene "
            "permiso explícito tuyo (OAuth de usuario). Es un consentimiento "
            "único que se hace desde este chat.",
            "**Para activarlo:** envía `/setup-files` y sigue las instrucciones.",
            f"Mientras tanto el archivo está en el host: `{path}`",
        ])
        body: Dict[str, Any] = {"text": "\n".join(lines)}
        if thread_id:
            body["thread"] = {"name": thread_id}
        elif thread_key:
            body["thread"] = {"threadKey": thread_key}
        try:
            await self._create_message(chat_id, body, job_id=job_id)
        except Exception:
            logger.debug(
                "[GoogleChat] attachment fallback notice send failed",
                exc_info=True,
            )
        return SendResult(
            success=False,
            error="google_chat: native attachment requires user OAuth — "
            "run /setup-files in chat",
        )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return {name, type, chat_id} for a space."""
        try:
            info = await asyncio.to_thread(
                lambda: self._chat_api.spaces()
                .get(name=chat_id)
                .execute(http=self._new_authed_http())
            )
        except HttpError as exc:
            logger.debug(
                "[GoogleChat] get_chat_info failed: %s", _redact_sensitive(str(exc))
            )
            return {"name": chat_id, "type": "group", "chat_id": chat_id}
        space_type = (info.get("spaceType") or info.get("type") or "").upper()
        display = info.get("displayName") or chat_id
        return {
            "name": display,
            "type": "dm" if space_type in {"DIRECT_MESSAGE", "DM"} else "group",
            "chat_id": chat_id,
        }


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def _validate_config(config: PlatformConfig) -> bool:
    """Plugin-side config gate for HTTP callback or Pub/Sub inbound modes."""
    extra = getattr(config, "extra", {}) or {}
    return bool(
        extra.get("http_events_url")
        or (extra.get("project_id") and extra.get("subscription_name"))
    )


def _check_for_registry() -> bool:
    """``check_fn`` for the platform registry pass — stricter than the
    deps-only ``check_google_chat_requirements``.

    The registry pass at ``gateway/config.py:_apply_env_overrides`` adds
    the platform to ``cfg.platforms`` whenever ``check_fn`` returns True.
    For backward compat with the pre-plugin behavior, we ALSO require
    the minimum Pub/Sub env vars so an unconfigured user doesn't
    accidentally see ``google_chat`` enabled. This matches the legacy
    ``if gc_project and gc_subscription`` gate.
    """
    if not check_google_chat_requirements():
        return False
    project = (
        os.getenv("GOOGLE_CHAT_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    subscription = (
        os.getenv("GOOGLE_CHAT_SUBSCRIPTION_NAME")
        or os.getenv("GOOGLE_CHAT_SUBSCRIPTION")
    )
    http_events_url = os.getenv("GOOGLE_CHAT_HTTP_EVENTS_URL")
    return bool(http_events_url or (project and subscription))


def _is_connected(config: PlatformConfig) -> bool:
    """``GatewayConfig.get_connected_platforms()`` polls this."""
    return bool(getattr(config, "enabled", False)) and _validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed ``PlatformConfig.extra`` from env vars during
    ``_apply_env_overrides``.

    The registry's env-enablement hook is called BEFORE the adapter is
    constructed, so ``gateway status`` and ``get_connected_platforms()``
    reflect env-only configuration without instantiating the Pub/Sub client.
    Returns ``None`` when the required Pub/Sub project/subscription aren't
    set; the caller then skips auto-enabling the platform.

    The special ``home_channel`` key in the returned dict is handled by the
    core hook — it becomes a proper ``HomeChannel`` dataclass on the
    ``PlatformConfig`` rather than being merged into ``extra``.
    """
    project = (
        os.getenv("GOOGLE_CHAT_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    subscription = (
        os.getenv("GOOGLE_CHAT_SUBSCRIPTION_NAME")
        or os.getenv("GOOGLE_CHAT_SUBSCRIPTION")
    )
    http_events_url = os.getenv("GOOGLE_CHAT_HTTP_EVENTS_URL")
    if not (http_events_url or (project and subscription)):
        return None
    seed: Dict[str, Any] = {}
    if project:
        seed["project_id"] = project
    if subscription:
        seed["subscription_name"] = subscription
    if http_events_url:
        seed["http_events_url"] = http_events_url
    http_events_audience = os.getenv("GOOGLE_CHAT_HTTP_EVENTS_AUDIENCE")
    if http_events_audience:
        seed["http_events_audience"] = http_events_audience
    http_events_sa_email = os.getenv("GOOGLE_CHAT_HTTP_EVENTS_SERVICE_ACCOUNT_EMAIL")
    if http_events_sa_email:
        seed["http_events_service_account_email"] = http_events_sa_email
    sa_json = (
        os.getenv("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )
    if sa_json:
        seed["service_account_json"] = sa_json
    home = os.getenv("GOOGLE_CHAT_HOME_CHANNEL")
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("GOOGLE_CHAT_HOME_CHANNEL_NAME", "Home"),
        }
    return seed


def interactive_setup() -> None:
    """Walk the user through Google Chat configuration via ``hermes setup``.

    The setup wizard at ``hermes_cli/gateway.py`` calls this for plugin
    platforms instead of using the in-tree ``_PLATFORMS`` data block. The
    flow mirrors the in-tree built-ins: print the GCP setup instructions,
    prompt for env vars, persist them to ``~/.hermes/.env`` so the next
    gateway restart picks them up.
    """
    from hermes_cli.cli_output import (
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
    )
    from hermes_cli.config import get_env_value, save_env_value

    existing_sub = get_env_value("GOOGLE_CHAT_SUBSCRIPTION_NAME")
    if existing_sub:
        print_info(f"Google Chat: already configured (subscription: {existing_sub})")
        if not prompt_yes_no("Reconfigure Google Chat?", False):
            return

    print_info("Google Chat needs a GCP project, a Pub/Sub topic + subscription,")
    print_info("and a Service Account with Pub/Sub Subscriber on the subscription.")
    print_info("Walkthrough:")
    print_info("  1. Create or select a GCP project; enable Google Chat API + Cloud Pub/Sub API.")
    print_info("  2. Create a Service Account (no project-level IAM role needed).")
    print_info("  3. Create a Pub/Sub topic (e.g. hermes-chat-events) and a Pull subscription.")
    print_info("  4. On the TOPIC: add chat-api-push@system.gserviceaccount.com as Pub/Sub Publisher.")
    print_info("  5. On the SUBSCRIPTION: grant your Service Account Pub/Sub Subscriber.")
    print_info("  6. Download the Service Account JSON key.")
    print_info("  7. Google Chat API console → Configuration: connection = Cloud Pub/Sub,")
    print_info("     point at the topic, enable 1:1 + group, restrict visibility.")
    print_info("  8. Install the bot in a space (fires ADDED_TO_SPACE and resolves its user_id).")
    print_info("")
    print_info("Full guide: website/docs/user-guide/messaging/google_chat.md")
    print_info("")

    project = prompt(
        "GCP project ID (e.g. my-project)",
        default=get_env_value("GOOGLE_CHAT_PROJECT_ID") or "",
    )
    if not project:
        print_warning("Project ID is required — skipping Google Chat setup")
        return
    save_env_value("GOOGLE_CHAT_PROJECT_ID", project.strip())

    subscription = prompt(
        "Pub/Sub subscription (projects/<proj>/subscriptions/<sub>)",
        default=get_env_value("GOOGLE_CHAT_SUBSCRIPTION_NAME") or "",
    )
    if not subscription:
        print_warning("Subscription is required — skipping Google Chat setup")
        return
    save_env_value("GOOGLE_CHAT_SUBSCRIPTION_NAME", subscription.strip())

    sa_path = prompt(
        "Path to Service Account JSON (or inline JSON)",
        default=get_env_value("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON") or "",
        password=True,
    )
    if sa_path:
        save_env_value("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", sa_path.strip())

    if prompt_yes_no("Restrict access to specific users? (recommended)", True):
        allowed = prompt(
            "Allowed user emails (comma-separated)",
            default=get_env_value("GOOGLE_CHAT_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("GOOGLE_CHAT_ALLOWED_USERS", allowed.replace(" ", ""))
            print_success("Allowlist configured")
        else:
            save_env_value("GOOGLE_CHAT_ALLOWED_USERS", "")
    else:
        save_env_value("GOOGLE_CHAT_ALLOW_ALL_USERS", "true")
        print_warning("⚠️  Open access — anyone who can DM the bot can command it.")

    home = prompt(
        "Home space for cron/notification delivery (e.g. spaces/AAAA, or empty)",
        default=get_env_value("GOOGLE_CHAT_HOME_CHANNEL") or "",
    )
    if home:
        save_env_value("GOOGLE_CHAT_HOME_CHANNEL", home.strip())

    print()
    print_success("Google Chat configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway: hermes gateway restart")


# Strict resource-name pattern.  ``spaces/<id>`` and ``users/<id>`` must
# only contain Google Chat's documented character set; anything else
# means a tampered chat_id trying to break out of the REST URL path
# (path traversal, ``?`` query injection, ``#`` fragment truncation).
_GCHAT_CHAT_ID_RE = re.compile(r"^(?:spaces|users)/[A-Za-z0-9_-]+$")


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """POST a single Google Chat message via the REST API without the SDK.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (e.g. ``hermes cron`` running as a
    separate process from ``hermes gateway``).  Without this hook,
    ``deliver=google_chat`` cron jobs fail with ``No live adapter for
    platform``.

    Configuration: requires service-account credentials via
    ``GOOGLE_CHAT_SERVICE_ACCOUNT_JSON``, ``GOOGLE_APPLICATION_CREDENTIALS``,
    or Application Default Credentials, and a space resource name as
    ``chat_id`` (e.g. ``spaces/AAAA-BBBB`` or ``users/<id>``).

    Security: ``chat_id`` is validated against the documented Google Chat
    resource-name character set before substitution into the REST URL so
    a tampered value cannot path-traverse or query-inject.

    ``media_files`` and ``force_document`` are accepted for signature
    parity but are not implemented for the standalone path; messages with
    attachments send as text-only.  The live adapter handles attachments.
    """
    if not chat_id:
        return {"error": "Google Chat standalone send: chat_id (space resource) is required"}
    if not _GCHAT_CHAT_ID_RE.match(chat_id):
        return {"error": (
            f"Google Chat standalone send: chat_id {chat_id!r} must match "
            f"'spaces/<id>' or 'users/<id>' with only [A-Za-z0-9_-] in the id"
        )}
    if thread_id is not None and not re.match(r"^spaces/[A-Za-z0-9_-]+/threads/[A-Za-z0-9_-]+$", thread_id):
        return {"error": (
            f"Google Chat standalone send: thread_id {thread_id!r} must match "
            f"'spaces/<id>/threads/<id>'"
        )}

    extra = getattr(pconfig, "extra", {}) or {}
    sa_value = (
        extra.get("service_account_json")
        or os.getenv("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )

    if service_account is None:
        return {"error": "Google Chat standalone send: google-auth not installed"}

    try:
        from google.auth.transport.requests import Request as _GoogleAuthRequest
    except Exception as e:
        return {"error": f"Google Chat standalone send: google-auth import failed: {e}"}

    try:
        if sa_value:
            stripped = sa_value.lstrip()
            if stripped.startswith("{"):
                try:
                    info = json.loads(sa_value)
                except json.JSONDecodeError as exc:
                    return {"error": f"Google Chat standalone send: inline SA JSON is invalid: {exc}"}
                creds = service_account.Credentials.from_service_account_info(info, scopes=_CHAT_SCOPES)
            else:
                if not os.path.exists(sa_value):
                    return {"error": f"Google Chat standalone send: SA JSON file not found at {sa_value}"}
                try:
                    with open(sa_value, "r", encoding="utf-8") as fh:
                        info = json.load(fh)
                except json.JSONDecodeError as exc:
                    return {"error": f"Google Chat standalone send: SA JSON file is invalid: {exc}"}
                creds = service_account.Credentials.from_service_account_info(info, scopes=_CHAT_SCOPES)
        else:
            try:
                import google.auth as _google_auth
            except ImportError:
                return {"error": (
                    "Google Chat standalone send: no SA credentials configured "
                    "and google-auth is not installed for ADC fallback"
                )}
            try:
                creds, _project = _google_auth.default(scopes=_CHAT_SCOPES)
            except Exception as exc:
                return {"error": (
                    f"Google Chat standalone send: no SA credentials configured "
                    f"and Application Default Credentials are unavailable: {exc}"
                )}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"error": f"Google Chat standalone send: credential load failed: {e}"}

    # Bound the synchronous urllib3-backed token refresh so a hung Google
    # STS endpoint cannot stall the cron scheduler indefinitely.
    try:
        await asyncio.wait_for(
            asyncio.to_thread(creds.refresh, _GoogleAuthRequest()),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        return {"error": "Google Chat standalone send: token refresh timed out"}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"error": f"Google Chat standalone send: token refresh failed: {e}"}

    token = getattr(creds, "token", None)
    if not token:
        return {"error": "Google Chat standalone send: refreshed credentials have no token"}

    body: Dict[str, Any] = {"text": message}
    if thread_id:
        body["thread"] = {"name": thread_id}

    url = f"https://chat.googleapis.com/v1/{chat_id}/messages"
    try:
        import aiohttp as _aiohttp
    except ImportError:
        return {"error": "Google Chat standalone send: aiohttp not installed"}

    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=30.0), trust_env=True) as session:
            async with session.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    return {"error": (
                        f"Google Chat standalone send: API returned "
                        f"{resp.status}: {text[:300]}"
                    )}
                payload = await resp.json()
        return {
            "success": True,
            "message_id": payload.get("name"),
        }
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("Google Chat standalone send raised", exc_info=True)
        return {"error": f"Google Chat standalone send failed: {e}"}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup.

    Registers the Google Chat adapter under the ``google_chat`` name.
    The gateway's ``_create_adapter`` consults the platform registry
    BEFORE its built-in if/elif chain, so this registration is what
    drives adapter creation at runtime.
    """
    ctx.register_platform(
        name="google_chat",
        label="Google Chat",
        adapter_factory=lambda cfg: GoogleChatAdapter(cfg),
        check_fn=_check_for_registry,
        validate_config=_validate_config,
        is_connected=_is_connected,
        required_env=[
            "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",
        ],
        install_hint="Run `hermes setup` to install Google Chat support.",
        setup_fn=interactive_setup,
        # Env-driven auto-configuration — the core env-populator hook calls
        # this during ``_apply_env_overrides`` and seeds
        # ``PlatformConfig.extra`` + home_channel from env vars.  Without this
        # the adapter would still work on explicit config.yaml entries, but
        # env-only setup (GOOGLE_CHAT_PROJECT_ID/_SUBSCRIPTION_NAME/...) would
        # not flow through to ``gateway status`` or ``get_connected_platforms``.
        env_enablement_fn=_env_enablement,
        # Cron home-channel delivery support.  Lets ``deliver=google_chat``
        # cron jobs route to the configured home space without editing
        # cron/scheduler.py's hardcoded sets.
        cron_deliver_env_var="GOOGLE_CHAT_HOME_CHANNEL",
        # Out-of-process cron delivery via the Chat REST API.  Without this
        # hook, deliver=google_chat cron jobs fail with "No live adapter"
        # when cron runs separately from the gateway.
        standalone_sender_fn=_standalone_send,
        # Auth env vars for _is_user_authorized() integration.
        allowed_users_env="GOOGLE_CHAT_ALLOWED_USERS",
        allow_all_env="GOOGLE_CHAT_ALLOW_ALL_USERS",
        # Chat caps text messages at 4096 chars; we leave margin to fit
        # the "Hermes is thinking..." marker patches and edit overhead.
        max_message_length=4000,
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are on Google Chat. Limited markdown subset is rendered: "
            "*bold*, _italic_, ~strike~, `code`. No headings or lists. "
            "Message size limit: 4000 characters; longer responses are split "
            "across multiple messages. You are in a space (DM or group). "
            "Images render inline; audio, video, and document attachments "
            "render as download cards (no native voice/video UI). To send "
            "files, include MEDIA:/absolute/path/to/file in your response. "
            "Native file attachments require the user to run /setup-files "
            "once in their own DM — until they do, file requests fall back "
            "to a text notice with the host path. Interactive Card v2 decision "
            "prompts are supported through the clarify tool. Use a card when "
            "the user must approve, choose a route, pause, deny, or supply a "
            "short custom answer; wait for the card response before continuing. "
            "While you "
            "are generating a response, a 'Hermes is thinking…' marker message "
            "appears in the space and is deleted once your response is ready. "
            "You do NOT have access to Google Chat-specific APIs — you cannot "
            "search space history, list space members, or manage spaces. Do "
            "not promise to perform these actions; explain that you can only "
            "read messages sent directly to you and respond in the same "
            "space/thread."
        ),
    )
