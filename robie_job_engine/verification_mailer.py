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
from email.policy import SMTP
from typing import Any

from . import accountability_delivery
from .email_sender_policy import is_self_sender
from .hitl_copy import sanitize_plain_text
from .outbound_send_guard import should_skip_send

logger = logging.getLogger("robie.verification_mailer")

DEFAULT_SENDER = "robie@streetsmart.insurance"
"""Outbound identity for all four verification workers."""


def _sender() -> str:
    return (
        os.environ.get("ROBIE_VERIFICATION_MAIL_SENDER") or DEFAULT_SENDER
    ).strip()


def _read_sa_from_accountability_env() -> str:
    """Fallback: read the delegated SA from the accountability env file.

    The verification worker systemd units load robie-recording.env and
    robie-evidence-loop.env but not robie-accountability.env, so the
    ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT env var is never set
    for them. The service user can read that file (group-readable), so
    fall back to parsing it directly rather than failing closed.
    The value is never logged.
    """
    path = os.environ.get(
        "ROBIE_ACCOUNTABILITY_ENV_PATH",
        "/etc/streetsmart-hermes-test/robie-accountability.env",
    )
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _delegated_service_account() -> str:
    sa = (
        os.environ.get("ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT")
        or os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT")
        or ""
    ).strip()
    if not sa:
        sa = _read_sa_from_accountability_env()
    return sa


def _redacted_log_context(to: list[str], cc: list[str], subject: str) -> dict[str, Any]:
    return {
        "recipient_count": len(to),
        "cc_count": len(cc),
        "subject_sha256": hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16],
    }


def build_plain_email_message(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    text_body: str,
    html_body: str | None = None,
    plain_only: bool = False,
) -> EmailMessage:
    """text/plain 8bit. No HTML unless explicitly asked. No QP mid-word wraps."""
    body_text = sanitize_plain_text(text_body)
    subject_text = sanitize_plain_text(subject).replace("\n", " ")
    # Unlimited lines only for plain HITL. HTML alternatives still need a
    # real max_line_length or quoprimime raises (maxlinelen must be >= 4).
    policy = SMTP.clone(max_line_length=0) if plain_only else SMTP
    message = EmailMessage(policy=policy)
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["From"] = sender
    message["Subject"] = subject_text
    message.set_content(body_text, subtype="plain", charset="utf-8", cte="8bit")
    if html_body and not plain_only:
        html = str(html_body)
        if "letter-spacing" in html.casefold() or "zwsp" in html.casefold():
            html = ""
        if html.strip():
            message.add_alternative(html, subtype="html")
    return message


def send_verification_email(
    *,
    to: list[str],
    cc: list[str],
    subject: str,
    text_body: str,
    html_body: str | None = None,
    plain_only: bool = False,
) -> dict[str, Any]:
    """Send one verification email from robie@streetsmart.insurance.

    Returns a receipt dict ``{"kind": "gmail", "message_id", "sender",
    "recipient_count", "cc_count"}`` — no addresses, subjects, or bodies are
    ever logged or returned. Raises (fail closed) on missing configuration,
    invalid recipients, or send failure.
    """
    recipients = [str(item).strip() for item in (to or []) if str(item).strip()]
    cc_list = [str(item).strip() for item in (cc or []) if str(item).strip()]
    subject_text = sanitize_plain_text(subject).replace("\n", " ")
    body_text = sanitize_plain_text(text_body)
    if not recipients or not all("@" in item for item in recipients):
        raise ValueError("verification email requires at least one valid recipient")
    if not all("@" in item for item in cc_list):
        raise ValueError("verification email CC contains an invalid address")
    if not subject_text:
        raise ValueError("verification email requires a subject")
    if not body_text.strip():
        raise ValueError("verification email requires a text body")

    # 2026-09-14: the four verification workers share this send path. A worker
    # must never address our own mailbox (that is loop fuel for the inbox
    # agent) and must never repeat a send already sitting in Sent
    # (no-blind-resend is now code, not a chat rule). Both fail closed.
    for addr in recipients + cc_list:
        if is_self_sender(addr):
            raise ValueError(
                "verification email must not be addressed to the robie@ mailbox"
            )

    sender = _sender()
    service_account = _delegated_service_account()
    if not sender or not service_account:
        raise ValueError(
            "verification email delivery requires the sender mailbox and the "
            "delegated Gmail service account "
            "(ROBIE_VERIFICATION_MAIL_SENDER / "
            "ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT)"
        )

    message = build_plain_email_message(
        sender=sender,
        to=recipients,
        cc=cc_list,
        subject=subject_text,
        text_body=body_text,
        html_body=html_body,
        plain_only=plain_only,
    )

    context = _redacted_log_context(recipients, cc_list, subject_text)
    try:
        gmail = accountability_delivery._delegated_gmail_sender(service_account, sender)
    except Exception as exc:
        logger.error(
            "verification email send failed: %s %s",
            type(exc).__name__,
            context,
        )
        raise
    # Sent-folder duplicate check: the guard fails open on lookup errors, so
    # a Gmail hiccup never blocks legitimate worker mail — but an actual
    # duplicate in Sent refuses the send loudly instead of double-sending.
    for addr in recipients:
        skip, _reason = should_skip_send(gmail, addr, subject_text)
        if skip:
            logger.warning("duplicate verification email refused: %s", context)
            raise ValueError(
                "duplicate verification email refused: an identical message "
                "is already in Sent"
            )
    try:
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

