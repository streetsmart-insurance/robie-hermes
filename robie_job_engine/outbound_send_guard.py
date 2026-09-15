"""Outbound send guard: check Sent before sending, so the engine can never double-send.

2026-09-14: the no-blind-resend rule existed only as a chat instruction, and the
Julio's Tree Service double-send proved a chat rule is not enforcement. Every
engine email send must call should_skip_send() first. Kept dependency-free
(no robie_job_engine imports) so it stays unit-testable and import-safe.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SUBJECT_PREFIX_RE = re.compile(r"^(re|fwd?|aw|sv)\s*:\s*", re.IGNORECASE)


def normalize_subject(subject: str | None) -> str:
    """Strip reply/forward prefixes and normalize case/whitespace for comparison."""
    text = subject or ""
    while True:
        cleaned = SUBJECT_PREFIX_RE.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r"\s+", " ", text).casefold()


def _message_headers(msg: dict) -> tuple[str, str]:
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in msg.get("payload", {}).get("headers", [])
    }
    return headers.get("to", ""), headers.get("subject", "")


def find_recent_sent(
    service,
    to_address: str,
    subject: str,
    window_hours: int = 24,
    max_results: int = 10,
) -> list[dict]:
    """Return Sent messages to the same recipient with a matching subject in the window.

    Each match is {"id": ..., "date": iso8601-or-"unknown", "subject": ...}.
    Fails open: on any API error returns [] (a missed duplicate is annoying;
    a blocked legitimate send breaks the worker loop).
    """
    if service is None:
        return []
    want = normalize_subject(subject)
    addr = (to_address or "").strip().casefold()
    if not addr:
        return []
    try:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=f"in:sent to:{to_address} newer_than:2d",
                maxResults=max_results,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - guard must never break the send path
        logger.warning("Sent-folder check failed (%s); failing open", exc)
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    matches: list[dict] = []
    for meta in resp.get("messages", []) or []:
        try:
            full = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=meta["id"],
                    format="metadata",
                    metadataHeaders=["To", "Subject", "Date"],
                )
                .execute()
            )
        except Exception:  # noqa: BLE001 - skip one bad message, keep checking
            continue
        to_value, subject_value = _message_headers(full)
        if addr not in to_value.casefold():
            continue
        if normalize_subject(subject_value) != want:
            continue
        try:
            sent_at = datetime.fromtimestamp(
                int(full.get("internalDate", "0")) / 1000, timezone.utc
            )
        except (TypeError, ValueError):
            sent_at = None
        if sent_at is not None and sent_at < cutoff:
            continue
        matches.append(
            {
                "id": meta["id"],
                "date": sent_at.isoformat() if sent_at else "unknown",
                "subject": subject_value,
            }
        )
    return matches


def should_skip_send(
    service,
    to_address: str,
    subject: str,
    window_hours: int = 24,
) -> tuple[bool, str]:
    """Return (skip, reason). True when the same send already exists in Sent."""
    matches = find_recent_sent(service, to_address, subject, window_hours)
    if matches:
        first = matches[0]
        return True, (
            f"already sent to {to_address} "
            f"(sent id {first['id']} at {first['date']}); skipping duplicate"
        )
    return False, "no matching sent message"
