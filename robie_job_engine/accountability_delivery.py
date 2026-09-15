"""Verified delivery of accountability artifacts to explicit destinations."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping

from .chat_app_post import _chat_app_client, post_as_chat_app


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"


def _delegated_gmail_sender(service_account_email: str, sender: str) -> Any:
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    signer = iam.Signer(Request(), source, service_account_email)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE],
        subject=sender,
    )
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def deliver_report(
    report_path: Path,
    *,
    mode: str,
    delivery: Mapping[str, Any],
    environment: Mapping[str, str],
    subject: str | None = None,
) -> list[dict[str, Any]]:
    text = report_path.read_text(encoding="utf-8")
    receipts: list[dict[str, Any]] = []
    for space in delivery.get("chat_spaces", []) or []:
        for sequence, chunk in enumerate(_chat_chunks(text), 1):
            result = post_as_chat_app(str(space), chunk)
            receipts.append({
                "kind": "google_chat",
                "destination": str(space),
                "message_name": result.get("name"),
                "sequence": sequence,
            })

    recipients = [str(item).strip() for item in (delivery.get("email_recipients", {}) or {}).get(mode, []) if "@" in str(item)]
    if recipients:
        sender = str(delivery.get("email_sender") or "").strip()
        service_account = environment.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
        if not sender or not service_account:
            raise ValueError("email delivery requires email_sender and delegated service account")
        message = EmailMessage()
        message["To"] = ", ".join(recipients)
        message["From"] = sender
        message["Subject"] = subject or f"StreetSmart {mode.title()} Accountability Report"
        message.set_content(text)
        gmail = _delegated_gmail_sender(service_account, sender)
        sent = gmail.users().messages().send(
            userId="me",
            body={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")},
        ).execute()
        message_id = str(sent.get("id") or "").strip()
        receipts.append({
            "kind": "gmail",
            "destination": recipients,
            "message_id": message_id,
            "sender": sender,
            "service_account": service_account,
            "delivered": bool(message_id),
        })
    if not receipts:
        raise ValueError(f"delivery is enabled but no destination is configured for {mode}")
    return receipts


def _chat_chunks(text: str, limit: int = 3500) -> list[str]:
    """Split reports at line boundaries below Google Chat's message limit."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current or not chunks:
        chunks.append(current)
    return chunks


def verify_delivery_receipts(
    receipts: list[dict[str, Any]],
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Fresh destination read-back; a create response alone is not verification."""
    import os

    env = dict(environment or {})
    observed: list[dict[str, Any]] = []
    for receipt in receipts:
        if receipt.get("kind") == "google_chat":
            name = str(receipt.get("message_name") or "")
            item = _chat_app_client().spaces().messages().get(name=name).execute() if name else {}
            ok = str(item.get("name") or "") == name
            observed.append({"kind": "google_chat", "name": name, "exists": ok})
        elif receipt.get("kind") == "gmail":
            sender = str(receipt.get("sender") or "")
            message_id = str(receipt.get("message_id") or "")
            account = str(
                receipt.get("service_account")
                or env.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT")
                or os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT")
                or ""
            ).strip()
            # format=minimal works with gmail.send + gmail.metadata. format=full
            # needs gmail.readonly and is what made Production report
            # delivered=false after a successful gmail.send.
            item: dict[str, Any] = {}
            if account and sender and message_id:
                try:
                    item = _delegated_gmail_sender(account, sender).users().messages().get(
                        userId="me", id=message_id, format="minimal"
                    ).execute()
                except Exception:
                    item = {}
            ok = str(item.get("id") or "") == message_id
            observed.append({
                "kind": "gmail",
                "id": message_id,
                "exists_in_sent_mailbox": ok,
                "delivered": ok,
            })
        else:
            observed.append({"kind": receipt.get("kind"), "exists": False})
    return all(item.get("exists") or item.get("exists_in_sent_mailbox") for item in observed), observed
