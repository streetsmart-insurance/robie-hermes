"""Cloud Run ingress for Google Chat and Workspace Add-on events.

Every POST must present a Google-signed ``Authorization: Bearer`` JWT.
Verification follows the Workspace Add-on HTTP check (audience is the
configured HTTP endpoint URL; ``email`` is the add-on service account)
and the Chat HTTP check (``chat@system.gserviceaccount.com``, either an
ID token for the endpoint URL or a project-number JWT signed with that
issuer's certs). Tokens are never logged. Rejections log a stable reason
code plus the unverified aud, iss, and email. The raw token is never logged.

Required environment variables (fail closed when either is unset):

- ``ROBIE_CHAT_BRIDGE_AUDIENCE`` — comma-separated accepted ``aud`` values.
  Use each HTTPS origin of this service (no path) for Add-ons and for Chat
  apps whose authentication audience is "HTTP endpoint URL". On
  ``/actions/<name>`` the bridge also accepts that origin plus the request
  path: Workspace Add-on button clicks set ``aud`` to the full action URL
  Google called (live Test, 2026-09-24). Entries already in this list are
  accepted as written, including a Cloud project number for Chat
  project-number tokens. No other audience is accepted.
- ``ROBIE_CHAT_BRIDGE_SERVICE_ACCOUNT_EMAILS`` — comma-separated allowed
  token identities. Include the add-on service account shown on the Chat
  API configuration page and, when standard Chat events can also hit this
  service, ``chat@system.gserviceaccount.com``.

Redeploy of this service is a separate human GO. This module does not
deploy Cloud Run, Test, or Production.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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

# Google OIDC ID tokens (Add-on system tokens and Chat "HTTP endpoint URL"
# tokens) use these issuers. Chat "Project Number" tokens use the Chat
# system service account as both issuer and cert source.
_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
_CHAT_ISSUER = "chat@system.gserviceaccount.com"
_CHAT_CERTS_URL = (
    "https://www.googleapis.com/service_accounts/v1/metadata/x509/" + _CHAT_ISSUER
)


class _BearerAuthError(Exception):
    """Fail-closed auth result. ``reason`` is a stable code, never a token.

    ``detail`` is an optional non-secret diagnostic (for example ``exp=``).
    ``aud``, ``iss``, and ``email`` are the unverified claims for the log
    line. None of these fields is returned to the caller, and the raw token
    is never stored here.
    """

    def __init__(
        self,
        reason: str,
        detail: str = "",
        *,
        aud: str = "",
        iss: str = "",
        email: str = "",
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.aud = aud
        self.iss = iss
        self.email = email
        super().__init__(reason)


_ACTION_PATH_RE = re.compile(r"^/actions/[A-Za-z0-9_-]{1,80}$")


def _log_claim(value: Any) -> str:
    """One log field. Newlines stripped so a claim cannot forge extra lines."""
    if isinstance(value, list):
        text = ",".join(str(item) for item in value)
    else:
        text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text[:300] or "-"


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _expected_audiences() -> list[str]:
    return _csv_env("ROBIE_CHAT_BRIDGE_AUDIENCE")


def _expected_service_accounts() -> set[str]:
    return {item.lower() for item in _csv_env("ROBIE_CHAT_BRIDGE_SERVICE_ACCOUNT_EMAILS")}


def _configured_origin(value: str) -> str | None:
    """Return ``scheme://host[:port]`` when ``value`` is an origin only.

    A value that already carries a path, query, userinfo, or fragment stays
    an explicit audience and is not used as a base to append ``/actions``.
    """
    parts = urlsplit(value.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.path not in {"", "/"}:
        return None
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _audiences_for_request(request_path: str) -> list[str]:
    """Configured audiences, plus origin + path for ``/actions/<name>``.

    The explicit env list is always accepted. For an action route, each
    configured origin also accepts that origin joined with the request path
    and nothing else — not a different path, not a longer host, not a query.
    """
    configured = _expected_audiences()
    allowed: list[str] = []
    seen: set[str] = set()
    for item in configured:
        if item not in seen:
            allowed.append(item)
            seen.add(item)
    if not _ACTION_PATH_RE.fullmatch(request_path or ""):
        return allowed
    for item in configured:
        origin = _configured_origin(item)
        if origin is None:
            continue
        candidate = origin + request_path
        if candidate not in seen:
            allowed.append(candidate)
            seen.add(candidate)
    return allowed


def _unverified_identity(token: str) -> tuple[str, str, str]:
    """Read aud, iss, and email without checking the signature.

    Used only for rejection logs. The token itself is never returned.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3 or not parts[1]:
            return ("", "", "")
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return ("", "", "")
    if not isinstance(claims, dict):
        return ("", "", "")
    aud = claims.get("aud")
    if isinstance(aud, list):
        aud_text = ",".join(str(item) for item in aud)
    else:
        aud_text = str(aud or "")
    return (aud_text, str(claims.get("iss") or ""), str(claims.get("email") or ""))


def _verify_google_id_token(
    token: str,
    audiences: list[str],
    *,
    certs_url: str | None = None,
) -> dict[str, Any]:
    """Validate a Google-signed JWT against Google certs and ``audiences``.

    Mirrors ``integrations/google_chat/adapter.py`` ``_verify_google_id_token``
    without importing the gateway adapter. ``certs_url`` selects the Chat
    project-number certs; the default path is ``verify_oauth2_token``.
    The raw token is never logged.
    """
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:
        raise _BearerAuthError("auth_dependency_unavailable") from exc

    google_request = google_requests.Request()
    if certs_url:
        claims = id_token.verify_token(
            token,
            google_request,
            audience=audiences,
            certs_url=certs_url,
        )
    else:
        claims = id_token.verify_oauth2_token(token, google_request, audiences)
    if not isinstance(claims, dict):
        raise ValueError("Google token verification returned no claims")
    return claims


def _try_verify(
    token: str,
    audiences: list[str],
    *,
    certs_url: str | None = None,
) -> dict[str, Any] | None:
    try:
        return _verify_google_id_token(token, audiences, certs_url=certs_url)
    except _BearerAuthError:
        raise
    except Exception:
        return None


# Leeway for the expiry diagnostic only. Verification itself uses
# google-auth's clock handling; this keeps borderline clock-skew cases out
# of the "expired" bucket so the reason stays trustworthy.
_EXPIRY_LEEWAY_SECONDS = 300


def _unverified_claims(token: str) -> dict[str, Any] | None:
    """Decode the JWT payload without verifying the signature.

    Used ONLY to build non-secret rejection diagnostics (aud/iss/exp).
    The result is never trusted for authentication. Returns None when the
    token is not a decodable JWT.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def _expand_audiences(audiences: list[str]) -> list[str]:
    """Accept trailing-slash variants of configured URL audiences.

    Google sometimes sends the audience with a trailing slash even when the
    configured value has none (or vice versa). Both forms identify the same
    endpoint, so both are accepted. Non-URL audiences (project numbers) are
    left exactly as configured. Order-preserving, de-duplicated.
    """
    expanded: list[str] = []
    for aud in audiences:
        if aud not in expanded:
            expanded.append(aud)
        if aud.startswith(("http://", "https://")):
            noslash = aud.rstrip("/")
            for variant in (noslash, noslash + "/"):
                if variant != aud and variant not in expanded:
                    expanded.append(variant)
    return expanded


def _audience_matches(claims: dict[str, Any], audiences: list[str]) -> bool:
    aud = claims.get("aud")
    values = aud if isinstance(aud, list) else [aud]
    allowed = set(audiences)
    return any(isinstance(item, str) and item in allowed for item in values)


def _identity_matches(claims: dict[str, Any], expected_emails: set[str]) -> bool:
    issuer = str(claims.get("iss") or "").strip()
    email = str(claims.get("email") or "").strip().lower()
    if issuer in _GOOGLE_ISSUERS:
        if claims.get("email_verified") not in (True, "true"):
            return False
        return bool(email) and email in expected_emails
    if issuer == _CHAT_ISSUER and _CHAT_ISSUER in expected_emails:
        # Project-number Chat JWTs identify the issuer. An email claim, when
        # present, still has to be on the allowlist.
        return not email or email in expected_emails
    return False


def _diagnose_rejection(token: str, audiences: list[str]) -> _BearerAuthError:
    """Build a specific, token-safe rejection for a failed verification.

    Inspects the UNVERIFIED claims (aud/iss/exp are not secrets) to tell
    operators exactly why Google's token was rejected. The raw token is
    never included.
    """
    claims = _unverified_claims(token)
    if claims is None:
        return _BearerAuthError("token_malformed")
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time() - _EXPIRY_LEEWAY_SECONDS:
        return _BearerAuthError("token_expired", f"exp={int(exp)}")
    aud = claims.get("aud")
    aud_values = aud if isinstance(aud, list) else [aud]
    aud_str = ",".join(str(item) for item in aud_values if isinstance(item, str))
    allowed = set(_expand_audiences(audiences))
    if not any(isinstance(item, str) and item in allowed for item in aud_values):
        return _BearerAuthError("audience_mismatch", f"aud={aud_str or 'missing'}")
    issuer = str(claims.get("iss") or "").strip()
    if issuer not in _GOOGLE_ISSUERS and issuer != _CHAT_ISSUER:
        return _BearerAuthError("issuer_unexpected", f"iss={issuer or 'missing'}")
    return _BearerAuthError(
        "signature_verification_failed",
        f"aud={aud_str or 'missing'} iss={issuer or 'missing'}",
    )


def _extract_bearer(header: str | None) -> str:
    if not isinstance(header, str):
        raise _BearerAuthError("missing_bearer")
    scheme, separator, token = header.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise _BearerAuthError("missing_bearer")
    return token.strip()


def _claims_from_google(
    token: str,
    audiences: list[str],
    expected_emails: set[str],
) -> dict[str, Any]:
    expanded = _expand_audiences(audiences)
    oidc_claims = _try_verify(token, expanded)
    if oidc_claims is not None and str(oidc_claims.get("iss") or "").strip() in _GOOGLE_ISSUERS:
        return oidc_claims
    if _CHAT_ISSUER in expected_emails:
        chat_claims = _try_verify(token, expanded, certs_url=_CHAT_CERTS_URL)
        issuer = str((chat_claims or {}).get("iss") or "").strip()
        if chat_claims is not None and issuer == _CHAT_ISSUER:
            return chat_claims
    raise _diagnose_rejection(token, audiences)


def _check_bearer(token: str, request_path: str) -> None:
    audiences = _audiences_for_request(request_path)
    expected_emails = _expected_service_accounts()
    if not audiences or not expected_emails:
        raise _BearerAuthError("auth_not_configured")
    claims = _claims_from_google(token, audiences, expected_emails)
    if not _audience_matches(claims, _expand_audiences(audiences)) or not _identity_matches(claims, expected_emails):
        raise _BearerAuthError("unexpected_bearer_identity")


def _authenticate_bearer(header: str | None, *, request_path: str) -> None:
    """Reject the POST unless the Bearer JWT is signed for this bridge.

    Unset audience or service-account allowlist fails closed. Any
    ``run.invoker`` principal that is not an expected Chat or Add-on
    identity is rejected the same way. Rejection carries the unverified
    aud, iss, and email for the log line; the token is not retained.
    """
    token = _extract_bearer(header)
    aud, iss, email = _unverified_identity(token)
    try:
        _check_bearer(token, request_path)
    except _BearerAuthError as exc:
        raise _BearerAuthError(
            exc.reason,
            exc.detail,
            aud=aud,
            iss=iss,
            email=email,
        ) from None


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


def _addon_processing_response() -> dict[str, Any]:
    """Return a Workspace Add-on update-message action with a processing card.

    Google Workspace Add-ons reject the empty ``{}`` synchronous response that
    works for standard Chat apps. They also reject the legacy Chat-app
    ``{"actionResponse": {"type": "UPDATE_MESSAGE"}, "cardsV2": [...]}``
    envelope. The Add-on response is
    ``hostAppDataAction.chatDataAction.updateMessageAction.message``.
    Hermes then replaces that message asynchronously via the Chat API.
    """
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "updateMessageAction": {
                    "message": {
                        "cardsV2": [
                            {
                                "cardId": "robie-processing",
                                "card": {
                                    "sections": [
                                        {
                                            "widgets": [
                                                {
                                                    "textParagraph": {
                                                        "text": "⏳ Processing your decision…"
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }


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
    try:
        _authenticate_bearer(
            request.headers.get("Authorization"),
            request_path=request.path,
        )
    except _BearerAuthError as exc:
        # ``detail`` repeats aud/iss for some reasons. Keep it only when it
        # adds something those three fields do not, such as exp=.
        extra = f" {exc.detail}" if exc.detail.startswith("exp=") else ""
        logger.info(
            "Rejected Google Chat POST reason=%s aud=%s iss=%s email=%s%s",
            exc.reason,
            _log_claim(exc.aud),
            _log_claim(exc.iss),
            _log_claim(exc.email),
            extra,
        )
        return jsonify(_chat_message("Unauthorized.")), 401

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

    # Workspace Add-on CARD_CLICKED / action routes need a synchronous
    # updateMessageAction. The empty {} that works for standard Chat apps
    # makes Chat display "<App> is unable to process your request."
    # Hermes replaces the processing card asynchronously via the Chat API.
    # Standard Chat API events keep the empty async-ack response.
    if action_name and _event_style(payload) == "workspace_addon":
        return jsonify(_addon_processing_response())

    # Hermes posts the durable status/card asynchronously. Google recommends an
    # empty synchronous response for this pattern; returning a Message or an
    # add-on action here can make Chat display "app not responding" when the
    # configured interaction format differs from the response envelope.
    return jsonify({})
