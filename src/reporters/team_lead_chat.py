"""Team Lead Chat webhook used by streetsmart-accountability-prod.

Morning publish and the 17:05 end-of-day wrap-up both post through this
module. It does not send leadership email.

Secret Manager secret id ``accountability-team-lead-chat-webhook``
(space ``spaces/AAQAHYP7Ezg``). Optional env
``TEAM_LEAD_CHAT_WEBHOOK_SECRET`` overrides that secret id or a full
``projects/.../secrets/...`` resource name. It is never a webhook URL.

The 2026-09-20 read-only runtime diagnostic did not contain this file.
Morning delivery on that capture was leadership email, not this webhook.
This module is the git source of truth for the Chat webhook so end-of-day
does not grow a second poster. If the VM already has a morning formatter
in this path, diff before replacing it and keep that formatter calling
``post_team_lead_chat``.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

SECRET_PROJECT = "streetsmart-hermes-poc"
DEFAULT_SECRET_ID = "accountability-team-lead-chat-webhook"
TEAM_LEAD_CHAT_SPACE = "spaces/AAQAHYP7Ezg"
ENV_SECRET = "TEAM_LEAD_CHAT_WEBHOOK_SECRET"


class TeamLeadChatWebhookMissing(RuntimeError):
    """The Team Lead Chat webhook secret is absent or is not that space."""


class TeamLeadChatPostError(RuntimeError):
    """Google Chat did not accept the message. The webhook URL is not included."""


SecretReader = Callable[[str], str]


def secret_resource_name(environ: Mapping[str, str] | None = None) -> str:
    """Return the Secret Manager resource for the Team Lead Chat webhook."""
    source = os.environ if environ is None else environ
    project = str(source.get("GOOGLE_CLOUD_PROJECT") or SECRET_PROJECT).strip() or SECRET_PROJECT
    raw = str(source.get(ENV_SECRET) or DEFAULT_SECRET_ID).strip() or DEFAULT_SECRET_ID
    if raw.lower().startswith("https://") or raw.lower().startswith("http://"):
        raise TeamLeadChatWebhookMissing(
            f"{ENV_SECRET} must be a Secret Manager id, not a webhook URL"
        )
    if " " in raw or "\n" in raw:
        raise TeamLeadChatWebhookMissing(f"{ENV_SECRET} is not a Secret Manager id")
    if raw.startswith("projects/"):
        if "/versions/" not in raw:
            return f"{raw}/versions/latest"
        return raw
    return f"projects/{project}/secrets/{raw}/versions/latest"


def validate_team_lead_webhook_url(value: str) -> str:
    """Accept only the incoming webhook for the Team Lead Chat space."""
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    expected_prefix = f"/v1/{TEAM_LEAD_CHAT_SPACE}/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chat.googleapis.com"
        or not parsed.path.startswith(expected_prefix)
        or not query.get("key")
        or not query.get("token")
    ):
        raise TeamLeadChatWebhookMissing(
            "Secret does not contain the Team Lead Chat webhook"
        )
    return url


def read_secret_manager_secret(resource_name: str) -> str:
    """Read one Secret Manager payload with application default credentials."""
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    service = build(
        "secretmanager", "v1", credentials=credentials, cache_discovery=False
    )
    response = service.projects().secrets().versions().access(
        name=resource_name
    ).execute()
    encoded = (response.get("payload") or {}).get("data")
    if not encoded:
        raise TeamLeadChatWebhookMissing(
            "Team Lead Chat webhook secret is empty"
        )
    try:
        decoded = base64.b64decode(encoded).decode("utf-8").strip()
    except (ValueError, UnicodeError) as exc:
        raise TeamLeadChatWebhookMissing(
            "Team Lead Chat webhook secret could not be read"
        ) from exc
    if not decoded:
        raise TeamLeadChatWebhookMissing(
            "Team Lead Chat webhook secret is empty"
        )
    return decoded


def resolve_team_lead_chat_webhook(
    *,
    secret_reader: SecretReader | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Load and validate the Team Lead Chat webhook. Never log the URL."""
    resource = secret_resource_name(environ)
    reader = secret_reader or read_secret_manager_secret
    try:
        payload = reader(resource)
    except TeamLeadChatWebhookMissing:
        raise
    except Exception as exc:
        raise TeamLeadChatWebhookMissing(
            "Team Lead Chat webhook secret is unavailable"
        ) from exc
    return validate_team_lead_webhook_url(payload)


def post_team_lead_chat(
    text: str,
    *,
    webhook_url: str,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """POST ``{"text": ...}`` to the Team Lead Chat webhook.

    Returns ``{"delivered": True, "message_name": ...}`` when Google Chat
    returns HTTP 200 and a message resource name. Does not send email.
    """
    body = str(text or "").strip()
    if not body:
        raise TeamLeadChatPostError("refusing to post an empty Team Lead Chat message")
    url = validate_team_lead_webhook_url(webhook_url)
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": body}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            status = int(getattr(response, "status", 0) or 0)
            parsed = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise TeamLeadChatPostError(
            f"Google Chat delivery failed with HTTP {exc.code}"
        ) from None
    except urllib.error.URLError:
        raise TeamLeadChatPostError("Google Chat delivery failed") from None
    except json.JSONDecodeError as exc:
        raise TeamLeadChatPostError(
            "Google Chat did not return a message receipt"
        ) from exc
    message_name = str(parsed.get("name") or "") if isinstance(parsed, dict) else ""
    if status != 200 or not message_name.startswith(f"{TEAM_LEAD_CHAT_SPACE}/messages/"):
        raise TeamLeadChatPostError(
            "Google Chat did not return a verified message receipt"
        )
    return {"delivered": True, "message_name": message_name}
