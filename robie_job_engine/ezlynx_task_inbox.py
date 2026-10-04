#!/usr/bin/env python3
"""Gmail inbox for the "Robie AI - Task Check-In" report.

The scheduled Looker Look emails a CSV to robie@streetsmart.insurance
every 30 minutes. This module fetches the latest delivery with a
fail-closed envelope:

- From must be exactly Applied Reporting <DoNotReply@appliedsystems.com>
- Subject must be exactly "Robie AI - Task Check-In"
- Exactly one CSV attachment, with the expected filename shape
- The CSV must parse under ezlynx_task_report (headers + ragged rows)

Anything else raises TaskInboxError — the intake never processes a
report it cannot positively identify. An empty mailbox (no delivery
yet) is NOT an error here; the caller decides whether that is healthy
or late — the health check owns the lateness verdict.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass

from .ezlynx_task_report import AssignedTask, TaskReportParseError, parse_task_report

logger = logging.getLogger(__name__)

EXPECTED_SENDER = "donotreply@appliedsystems.com"
EXPECTED_SUBJECT = "Robie AI - Task Check-In"
FILENAME_RE = re.compile(r"^Robie_AI_-_Task_Check-In_.*\.csv$", re.IGNORECASE)
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class TaskInboxError(ValueError):
    """Raised when the task report email cannot be safely identified."""


@dataclass(frozen=True)
class IngestedReport:
    """A positively-identified task report delivery."""
    message_id: str
    filename: str
    digest: str          # sha256 of the raw CSV bytes
    received_at: str     # Gmail internalDate, ISO-ish
    tasks: tuple[AssignedTask, ...]


def _payload_headers(payload: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = str(header.get("name") or "").lower()
        if name and name not in headers:
            headers[name] = str(header.get("value") or "")
    return headers


def _sender_address(from_header: str) -> str:
    """Extract the bare address from a From header."""
    match = re.search(r"<([^<>]+)>", from_header or "")
    if match:
        return match.group(1).strip().lower()
    return (from_header or "").strip().lower()


def _iter_parts(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _iter_parts(child)


def _decode_part(service, message_id: str, part: dict) -> bytes:
    body = part.get("body", {}) or {}
    data = body.get("data")
    if data:
        return base64.urlsafe_b64decode(data)
    attachment_id = body.get("attachmentId")
    if not attachment_id:
        raise TaskInboxError(f"attachment part has no data in {message_id}")
    attachment = (
        service.users().messages().attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    return base64.urlsafe_b64decode(attachment.get("data", ""))


def fetch_latest_task_report(service) -> IngestedReport | None:
    """Fetch and validate the latest task check-in delivery.

    Returns None when no delivery exists yet (caller/health check
    decides if that is late). Raises TaskInboxError on any envelope
    or content problem — never returns a half-identified report.
    """
    query = f'subject:"{EXPECTED_SUBJECT}" has:attachment'
    response = (
        service.users().messages().list(
            userId="me", q=query, maxResults=10
        ).execute()
    )
    metas = response.get("messages", []) or []
    if not metas:
        logger.info("No task check-in deliveries in mailbox yet")
        return None

    candidates = []
    for meta in metas:
        message_id = str(meta.get("id") or "")
        if not message_id:
            continue
        message = (
            service.users().messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = message.get("payload", {}) or {}
        headers = _payload_headers(payload)
        sender = _sender_address(headers.get("from", ""))
        subject = (headers.get("subject", "") or "").strip()
        if sender != EXPECTED_SENDER:
            logger.warning(
                f"Skipping {message_id}: sender {sender!r} is not the expected reporter"
            )
            continue
        if subject != EXPECTED_SUBJECT:
            logger.warning(
                f"Skipping {message_id}: subject {subject!r} does not match exactly"
            )
            continue
        internal_ms = int(message.get("internalDate", "0") or "0")
        candidates.append((internal_ms, message_id, message))

    if not candidates:
        raise TaskInboxError(
            "Deliveries exist but none match the exact sender/subject envelope"
        )

    # Latest delivery wins.
    candidates.sort(key=lambda c: c[0], reverse=True)
    internal_ms, message_id, message = candidates[0]
    payload = message.get("payload", {}) or {}

    csv_parts = [
        part
        for part in _iter_parts(payload)
        if str(part.get("filename") or "").strip()
        and str(part.get("filename") or "").strip().lower().endswith(".csv")
    ]
    if not csv_parts:
        raise TaskInboxError(f"{message_id}: no CSV attachment")
    if len(csv_parts) > 1:
        raise TaskInboxError(
            f"{message_id}: {len(csv_parts)} CSV attachments — refusing to pick one"
        )
    part = csv_parts[0]
    filename = str(part.get("filename") or "").strip()
    if not FILENAME_RE.match(filename):
        raise TaskInboxError(
            f"{message_id}: unexpected attachment filename {filename!r}"
        )

    content = _decode_part(service, message_id, part)
    if not content:
        raise TaskInboxError(f"{message_id}: attachment {filename!r} is empty")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise TaskInboxError(
            f"{message_id}: attachment {filename!r} exceeds size limit"
        )

    try:
        csv_text = content.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise TaskInboxError(f"{message_id}: attachment is not valid UTF-8: {e}")

    try:
        tasks = parse_task_report(csv_text)
    except TaskReportParseError as e:
        raise TaskInboxError(f"{message_id}: CSV failed validation: {e}")

    digest = hashlib.sha256(content).hexdigest()
    logger.info(
        f"Ingested {message_id} ({filename}): {len(tasks)} Robie AI tasks, "
        f"digest {digest[:12]}"
    )
    return IngestedReport(
        message_id=message_id,
        filename=filename,
        digest=digest,
        received_at=str(internal_ms),
        tasks=tuple(tasks),
    )
