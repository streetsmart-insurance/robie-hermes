"""Fan-out notifications for HITL plan confirmations.

The Confirmations tab alone is not enough: Carlo will not be staring at
the sheet. When a confirmation is requested, he hears about it in Google
Chat (his DM with the Robie app, falling back to the Robie home space)
and by email. Every sent notification is recorded in the ledger, so a
re-sync never double-notifies; a channel that fails is recorded as an
error and retried on the next sync.

Future: ``assign_requester_task`` will create an EZLynx follow-up task
for the user who requested the work. It is an explicit stub today: it
needs the requester's EZLynx user identity on the confirmation record and
a real task-create API. The legacy ``create_task`` fallback in
``ezlynx_note_poster`` fabricates a success dict when no API exists --
never build on it.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Mapping

from . import confirmations
from .store import JobStore


NOTIFY_TABLE = "confirmation_notifications"

DEFAULT_NOTIFY_EMAIL = "carlo@streetsmart.insurance"
DEFAULT_SHEET_ID = "1Qj0-i4QJuNkfWtjdfAdpQpI1JmC44KmThr_rrzGG6EQ"
SHEET_TAB = "Confirmations"


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def notify_email_to() -> str:
    return _env("ROBIE_CONFIRMATION_NOTIFY_EMAIL", DEFAULT_NOTIFY_EMAIL)


def sheet_id() -> str:
    return _env("ROBIE_DASHBOARD_SHEET_ID", DEFAULT_SHEET_ID)


def confirmations_tab_url(given_sheet_id: str | None = None) -> str:
    sid = (given_sheet_id or "").strip() or sheet_id()
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit"


# ---------------------------------------------------------------------------
# Ledger: which (confirmation, channel) pairs have been notified.
# ---------------------------------------------------------------------------

def _ensure_schema(conn: Any) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {NOTIFY_TABLE} (
            confirmation_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            notified_at TEXT NOT NULL,
            receipt_json TEXT,
            PRIMARY KEY (confirmation_id, channel)
        )
        """
    )


def was_notified(store: Any, confirmation_id: str, channel: str) -> bool:
    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            f"SELECT 1 FROM {NOTIFY_TABLE} WHERE confirmation_id = ? AND channel = ?",
            (confirmation_id, channel),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _record(store: Any, confirmation_id: str, channel: str, receipt: Mapping[str, Any]) -> None:
    conn = store.connect()
    try:
        _ensure_schema(conn)
        conn.execute(
            f"""INSERT OR IGNORE INTO {NOTIFY_TABLE}
                (confirmation_id, channel, notified_at, receipt_json)
                VALUES (?, ?, ?, ?)""",
            (
                confirmation_id,
                channel,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(dict(receipt), default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Message content: plain English, no jargon.
# ---------------------------------------------------------------------------

def _chat_text(record: Mapping[str, Any], tab_url: str) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        f"ROBIE needs your approval.\n\n{summary}\n\n"
        f"Approve or reject it on the Confirmations tab: {tab_url}\n"
        "Type APPROVE or REJECT in the \"Your decision\" column."
    )


def _email_subject(record: Mapping[str, Any]) -> str:
    return f"[ROBIE] Approval needed: {confirmations.confirmation_summary(record)[:90]}"


def _email_body(record: Mapping[str, Any], tab_url: str) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        f"Hi Carlo,\n\nROBIE needs your approval before it can continue:\n\n"
        f"{summary}\n\n"
        f"Review it on the Confirmations tab:\n{tab_url}\n\n"
        "Type APPROVE or REJECT in the \"Your decision\" column. "
        "If you reject, add a short reason in the Reason column.\n\n"
        "Nothing moves until you decide.\n"
    )


# ---------------------------------------------------------------------------
# Channel senders. Real clients are built from the environment; tests inject
# fakes. Each sender returns a receipt dict.
# ---------------------------------------------------------------------------

def _real_chat_poster() -> Any:
    from .chat_app_post import find_direct_message_space, post_as_chat_app, robie_home_space

    def post(text: str) -> dict[str, Any]:
        target: str | None = None
        try:
            target = find_direct_message_space(notify_email_to())
        except Exception:
            target = None
        if not target:
            target = robie_home_space()
        if not target:
            raise RuntimeError("no Chat target available (no DM, no home space)")
        result = post_as_chat_app(target, text)
        return {"space": target, "message_name": result.get("name")}
    return post


def _real_gmail_sender() -> Any:
    from .accountability_delivery import _delegated_gmail_sender

    service_account = _env("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT")
    sender = _env("ROBIE_CONFIRMATION_EMAIL_SENDER", "robie@streetsmart.insurance")
    if not service_account or not sender:
        raise RuntimeError(
            "email notify needs ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT "
            "and ROBIE_CONFIRMATION_EMAIL_SENDER"
        )
    gmail = _delegated_gmail_sender(service_account, sender)

    def send(to: str, subject: str, body: str) -> dict[str, Any]:
        message = EmailMessage()
        message["To"] = to
        message["From"] = sender
        message["Subject"] = subject
        message.set_content(body)
        sent = gmail.users().messages().send(
            userId="me",
            body={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")},
        ).execute()
        # Read-back: the sent mailbox must return that exact id.
        check = gmail.users().messages().get(
            userId="me", id=sent.get("id"), format="minimal"
        ).execute()
        if not check.get("id"):
            raise RuntimeError("email send receipt did not read back from sent mailbox")
        return {"message_id": sent.get("id"), "to": to, "sender": sender}
    return send


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_requested(
    store: Any,
    confirmation_id: str,
    *,
    chat_poster: Any = None,
    gmail_sender: Any = None,
    given_sheet_id: str | None = None,
) -> dict[str, Any]:
    """Notify Carlo (Chat + email) that a confirmation needs his decision.

    Idempotent per (confirmation, channel): already-notified channels are
    skipped. Channel failures are returned as errors, never raised, so one
    broken channel cannot block the other -- and the ledger only records
    successes, so failures retry on the next sync.
    """
    record = confirmations.get(confirmation_id, store=store)
    if record is None:
        raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
    if str(record.get("status")) != "PENDING":
        return {"confirmation_id": confirmation_id, "notified": [], "errors": [],
                "skipped": "not PENDING"}

    tab_url = confirmations_tab_url(given_sheet_id)
    notified: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not was_notified(store, confirmation_id, "google_chat"):
        try:
            poster = chat_poster if chat_poster is not None else _real_chat_poster()
            receipt = poster(_chat_text(record, tab_url))
            _record(store, confirmation_id, "google_chat", receipt)
            notified.append({"channel": "google_chat", **receipt})
        except Exception as exc:
            errors.append({"channel": "google_chat", "error": f"{type(exc).__name__}: {exc}"})

    if not was_notified(store, confirmation_id, "email"):
        try:
            sender = gmail_sender if gmail_sender is not None else _real_gmail_sender()
            receipt = sender(notify_email_to(), _email_subject(record), _email_body(record, tab_url))
            _record(store, confirmation_id, "email", receipt)
            notified.append({"channel": "email", **receipt})
        except Exception as exc:
            errors.append({"channel": "email", "error": f"{type(exc).__name__}: {exc}"})

    return {"confirmation_id": confirmation_id, "notified": notified, "errors": errors}


def assign_requester_task(store: Any, confirmation_id: str, **kwargs: Any) -> dict[str, Any]:
    """FUTURE: create an EZLynx follow-up task for the requesting user.

    Not implemented. Needs, before it can be built:
      1. the requester's EZLynx user identity on the confirmation record
         (``requested_by`` today is a free-text name, not a user id);
      2. a real EZLynx task-create API. The legacy ``create_task`` in
         ``ezlynx_note_poster`` returns a fabricated success dict when no
         API exists -- building on it would silently pretend tasks exist.
    """
    raise NotImplementedError(
        "assign_requester_task is not implemented: needs the requester's EZLynx "
        "user identity and a real task-create API (see docstring)"
    )
