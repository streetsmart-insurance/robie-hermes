"""Email HITL for the email watcher. Carlo only. Never a client."""

from __future__ import annotations

import logging

logger = logging.getLogger("robie.hitl_email")

CARLO_EMAIL = "carlo@streetsmart.insurance"


def carlo_hitl_email_sender():
    """Send a help request to Carlo. Refuse every other address."""

    def send(*, to: str, subject: str, body: str) -> None:
        recipient = str(to or "").strip().lower()
        if recipient != CARLO_EMAIL:
            raise RuntimeError(
                "refusing to email anyone but carlo@streetsmart.insurance"
            )
        from .hitl_copy import sanitize_plain_text
        from .verification_mailer import send_verification_email

        send_verification_email(
            to=[CARLO_EMAIL],
            cc=[],
            subject=sanitize_plain_text(subject).replace("\n", " "),
            text_body=sanitize_plain_text(body),
            html_body=None,
            plain_only=True,
        )

    return send
