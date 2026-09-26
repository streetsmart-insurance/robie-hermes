"""Shared helpers for the staff automation jobs (meeting synthesis, staff fun).

Conventions (mirror the existing job engine):
- Secrets are read at runtime from GCP Secret Manager via ADC. Only secret
  *names* live in env vars / code; values are never written anywhere.
- Google API access uses the ROBIE_GOOGLE_TOKEN_FILE OAuth token (already in
  the scheduler's environment), same as chat_app_post.py.
- Outbound email from robie@streetsmart.insurance uses the repo's delegated
  service-account Gmail pattern (accountability_delivery._delegated_gmail_sender),
  which is proven working on hermes-poc-01.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from email.message import EmailMessage
from typing import Any


PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "streetsmart-hermes-poc").strip() or "streetsmart-hermes-poc"

DEFAULT_GEMINI_KEY_SECRET = (
    f"projects/{PROJECT}/secrets/gemini-api-key/versions/latest"
)
DEFAULT_CHAT_WEBHOOK_SECRET = (
    f"projects/{PROJECT}/secrets/streetsmart-general-chat-webhook/versions/latest"
)
DEFAULT_GMAIL_DELEGATED_SA = "hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com"

GEMINI_MODEL = os.environ.get("ROBIE_GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"


def read_secret(resource_name: str) -> str:
    """Read a Secret Manager secret value at runtime (transient, never stored)."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    resp = client.access_secret_version(request={"name": resource_name})
    value = resp.payload.data.decode("utf-8")
    if not value:
        raise RuntimeError(f"Secret Manager returned an empty value for {resource_name}")
    return value


def gemini_generate(prompt: str, *, api_key: str | None = None, model: str = GEMINI_MODEL) -> str:
    """Call the Gemini generateContent REST API and return the text."""
    import requests

    key = api_key or read_secret(
        os.environ.get("ROBIE_GEMINI_API_KEY_SECRET", DEFAULT_GEMINI_KEY_SECRET).strip()
        or DEFAULT_GEMINI_KEY_SECRET
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    resp = requests.post(
        url,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Gemini returned no text: {json.dumps(data)[:300]}") from exc


def google_credentials():
    """OAuth user credentials from ROBIE_GOOGLE_TOKEN_FILE (scheduler env)."""
    from google.oauth2.credentials import Credentials

    token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
    if not token_file:
        raise RuntimeError("ROBIE_GOOGLE_TOKEN_FILE is not set")
    return Credentials.from_authorized_user_file(token_file)


def drive_client() -> Any:
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=google_credentials(), cache_discovery=False)


def docs_client() -> Any:
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=google_credentials(), cache_discovery=False)


def send_gmail(*, sender: str, to: list[str], subject: str, body: str) -> str:
    """Send email via the delegated service-account Gmail pattern.

    Returns the Gmail message ID. Raises on failure.
    """
    from .accountability_delivery import _delegated_gmail_sender

    service_account = (
        os.environ.get("ROBIE_GMAIL_DELEGATED_SA", "").strip() or DEFAULT_GMAIL_DELEGATED_SA
    )
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(body)
    gmail = _delegated_gmail_sender(service_account, sender)
    sent = gmail.users().messages().send(
        userId="me",
        body={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")},
    ).execute()
    message_id = str(sent.get("id") or "")
    if not message_id:
        raise RuntimeError("Gmail send returned no message ID")
    return message_id


def gmail_message_exists(message_id: str, *, sender: str) -> bool:
    """Read-back check: does the sent message exist with the right From?"""
    from .accountability_delivery import _delegated_gmail_sender

    service_account = (
        os.environ.get("ROBIE_GMAIL_DELEGATED_SA", "").strip() or DEFAULT_GMAIL_DELEGATED_SA
    )
    gmail = _delegated_gmail_sender(service_account, sender)
    try:
        meta = gmail.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
    except Exception:
        return False
    if meta.get("id") != message_id:
        return False
    headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
    return sender.lower() in headers.get("from", "").lower()


def post_chat_webhook(text: str, *, webhook_url: str | None = None) -> bool:
    """POST a message to a Google Chat incoming webhook. Returns True on 2xx."""
    url = webhook_url or read_secret(
        os.environ.get("ROBIE_GENERAL_CHAT_WEBHOOK_SECRET", DEFAULT_CHAT_WEBHOOK_SECRET).strip()
        or DEFAULT_CHAT_WEBHOOK_SECRET
    )
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return 200 <= resp.status < 300
