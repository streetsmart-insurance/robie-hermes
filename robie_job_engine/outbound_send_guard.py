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


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    return None


def _is_missing_scope_403(exc: BaseException) -> bool:
    """True when Gmail rejected ``messages.list?q=`` for the metadata scope.

    ``gmail.metadata`` returns HTTP 403 "Metadata scope does not support the
    q parameter". That is a missing search scope, not proof the Sent folder
    is empty.
    """
    text = str(exc).casefold()
    if _status_code(exc) != 403 and "403" not in text:
        return False
    return "metadata scope" in text or "does not support" in text and "q" in text


def delegated_mailbox(service) -> str:
    """Mailbox the Gmail client is actually delegated to, or empty if unknown."""
    if service is None:
        return ""
    try:
        users = service.users()
    except Exception:  # noqa: BLE001
        return ""
    getter = getattr(users, "getProfile", None)
    if not callable(getter):
        return ""
    try:
        profile = getter(userId="me").execute() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("sent-folder mailbox check failed (%s)", type(exc).__name__)
        return ""
    return str(profile.get("emailAddress") or "").strip().casefold()


def _list_sent_index(service, to_address: str, max_results: int) -> dict:
    """List recent Sent ids without ``q``.

    ``gmail.metadata`` 403s on ``q``. ``labelIds=SENT`` is the search that
    scope can run. A test double that only accepts ``q`` still works.
    """
    api = service.users().messages()
    attempts = (
        {"userId": "me", "labelIds": ["SENT"], "maxResults": max_results},
        {"userId": "me", "maxResults": max_results},
    )
    last_exc: BaseException | None = None
    for kwargs in attempts:
        try:
            return api.list(**kwargs).execute()
        except TypeError:
            return api.list(
                userId="me",
                q=f"in:sent to:{to_address} newer_than:2d",
                maxResults=max_results,
            ).execute()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_missing_scope_403(exc):
                logger.warning(
                    "sent-folder list hit a metadata-scope 403; retrying without q"
                )
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return {}


def find_recent_sent(
    service,
    to_address: str,
    subject: str,
    window_hours: int = 24,
    max_results: int = 10,
    *,
    expected_mailbox: str = "",
) -> list[dict]:
    """Return Sent messages to the same recipient with a matching subject in the window.

    Each match is {"id": ..., "date": iso8601-or-"unknown", "subject": ...}.
    Fails open: on any API error returns [] (a missed duplicate is annoying;
    a blocked legitimate send breaks the worker loop). A 403 from the metadata
    scope's ``q`` ban is retried without ``q``. A client delegated to the
    wrong mailbox is not treated as an empty Sent folder.
    """
    if service is None:
        return []
    want = normalize_subject(subject)
    addr = (to_address or "").strip().casefold()
    if not addr:
        return []
    expected = (expected_mailbox or "").strip().casefold()
    actual = delegated_mailbox(service) if expected else ""
    if expected and actual and actual != expected:
        logger.error(
            "sent-folder check skipped: delegated mailbox does not match the sender"
        )
        return []
    try:
        resp = _list_sent_index(service, to_address, max_results)
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


def _content_hash(text):
    """Hash normalized content for duplicate detection (2026-10-02)."""
    import hashlib
    normalized = re.sub(r"\s+", " ", (text or "").strip().casefold())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]


def should_skip_send(
    service,
    to_address: str,
    subject: str,
    window_hours: int = 24,
    *,
    expected_mailbox: str = "",
    incoming_body: str = "",
    thread_id: str = "",
) -> tuple[bool, str]:
    """Return (skip, reason). True when the same send already exists in Sent.
    
    2026-10-02: Content-aware — if incoming_body differs from thread history,
    this is a correction/follow-up, not a duplicate. Do NOT skip.
    """
    expected = (expected_mailbox or "").strip().casefold()
    actual = delegated_mailbox(service) if expected else ""
    if expected and actual and actual != expected:
        return False, "sent check skipped: wrong mailbox"
    matches = find_recent_sent(
        service,
        to_address,
        subject,
        window_hours,
        expected_mailbox=expected_mailbox,
    )
    if matches:
        first = matches[0]
        # Content-aware check: is this new content or a true duplicate?
        if incoming_body and thread_id and service:
            try:
                thread = service.users().threads().get(
                    userId="me", id=thread_id, format="full").execute()
                prev_hashes = set()
                for msg in thread.get("messages", []):
                    if "SENT" in msg.get("labelIds", []):
                        continue
                    # Extract body text
                    payload = msg.get("payload", {})
                    body_txt = ""
                    def _extract(part):
                        nonlocal body_txt
                        mime = part.get("mimeType", "")
                        data = part.get("body", {}).get("data", "")
                        if mime == "text/plain" and data:
                            import base64
                            try:
                                body_txt = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                            except:
                                pass
                        for sub in part.get("parts", []):
                            if not body_txt:
                                _extract(sub)
                    _extract(payload)
                    if body_txt:
                        prev_hashes.add(_content_hash(body_txt))
                curr_hash = _content_hash(incoming_body)
                if curr_hash not in prev_hashes:
                    logger.info("New content in thread (correction/follow-up), not skipping")
                    return False, "new content in thread - processing as correction"
            except Exception as exc:
                logger.warning("Content check failed: %s", exc)
        return True, (
            f"already sent to {to_address} "
            f"(sent id {first['id']} at {first['date']}); skipping duplicate"
        )
    return False, "no matching sent message"
