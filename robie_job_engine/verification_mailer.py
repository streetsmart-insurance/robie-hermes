"""Outbound email for the verification workers, sent from robie@streetsmart.insurance.

Modeled on ``accountability_delivery._delegated_gmail_sender``: keyless auth
via Workload-Identity ``iam.Signer`` + Google Workspace domain-wide delegation.
No OAuth token files, no stored passwords.

Fail closed: missing sender or delegated service-account configuration raises
``ValueError`` before any send is attempted. Logs carry only recipient counts
and a subject hash — never recipients, subjects, or bodies.
"""

from __future__ import annotations

import hashlib
import base64
import logging
import os
from email.message import EmailMessage
from typing import Any

from . import accountability_delivery

logger = logging.getLogger("robie.verification_mailer")

DEFAULT_SENDER = "robie@streetsmart.insurance"
"""Outbound identity for all four verification workers."""


def _sender() -> str:
    return (
        os.environ.get("ROBIE_VERIFICATION_MAIL_SENDER") or DEFAULT_SENDER
    ).strip()


def _delegated_service_account() -> str:
    return (
        os.environ.get("ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT")
        or os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT")
        or ""
    ).strip()


def _redacted_log_context(to: list[str], cc: list[str], subject: str) -> dict[str, Any]:
    return {
        "recipient_count": len(to),
        "cc_count": len(cc),
        "subject_sha256": hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16],
    }


def send_verification_email(
    *,
    to: list[str],
    cc: list[str],
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> dict[str, Any]:
    """Send one verification email from robie@streetsmart.insurance.

    Returns a receipt dict ``{"kind": "gmail", "message_id", "sender",
    "recipient_count", "cc_count"}`` — no addresses, subjects, or bodies are
    ever logged or returned. Raises (fail closed) on missing configuration,
    invalid recipients, or send failure.
    """
    recipients = [str(item).strip() for item in (to or []) if str(item).strip()]
    cc_list = [str(item).strip() for item in (cc or []) if str(item).strip()]
    subject_text = str(subject or "").strip()
    body_text = str(text_body or "")
    if not recipients or not all("@" in item for item in recipients):
        raise ValueError("verification email requires at least one valid recipient")
    if not all("@" in item for item in cc_list):
        raise ValueError("verification email CC contains an invalid address")
    if not subject_text:
        raise ValueError("verification email requires a subject")
    if not body_text.strip():
        raise ValueError("verification email requires a text body")

    sender = _sender()
    service_account = _delegated_service_account()
    if not sender or not service_account:
        raise ValueError(
            "verification email delivery requires the sender mailbox and the "
            "delegated Gmail service account "
            "(ROBIE_VERIFICATION_MAIL_SENDER / "
            "ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT)"
        )

    message = EmailMessage()
    message["To"] = ", ".join(recipients)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    message["From"] = sender
    message["Subject"] = subject_text
    message.set_content(body_text)
    if html_body:
        message.add_alternative(str(html_body), subtype="html")

    context = _redacted_log_context(recipients, cc_list, subject_text)
    try:
        gmail = accountability_delivery._delegated_gmail_sender(service_account, sender)
        sent = (
            gmail.users()
            .messages()
            .send(
                userId="me",
                body={
                    "raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
                },
            )
            .execute()
        )
    except Exception as exc:
        logger.error(
            "verification email send failed: %s %s",
            type(exc).__name__,
            context,
        )
        raise
    message_id = str((sent or {}).get("id") or "")
    if not message_id:
        raise RuntimeError("verification email send returned no message id")
    receipt = {
        "kind": "gmail",
        "message_id": message_id,
        "sender": sender,
        "recipient_count": len(recipients),
        "cc_count": len(cc_list),
    }
    logger.info("verification email sent: %s", {**context, "message_id": message_id})
    return receipt
