"""Fan-out notifications for HITL plan confirmations.

The Confirmations tab alone is not enough: Carlo will not be staring at
the sheet. When a confirmation is requested, he hears about it in Google
Chat (his DM with the Robie app, falling back to the Robie home space)
and by email. Every sent notification is recorded in the ledger, so a
re-sync never double-notifies; a channel that fails is recorded as an
error and retried on the next sync.

``assign_requester_task`` creates an EZLynx follow-up task for the
requesting user through the Zapier catch-hook (the agency's real task path;
the legacy ``create_task`` fallback in ``ezlynx_note_poster`` fabricates
success and is never used). The requester is mapped to an EZLynx login
username through ``REQUESTER_LOGINS`` (env-extensible); unknown requesters
fail closed. The caller's ``applicant_id`` must already be proven in EZLynx.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
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


def _requester_chat_text(record: Mapping[str, Any], tab_url: str) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        f"Your ROBIE job is waiting on a human decision and can't continue "
        f"until it's approved.\n\n{summary}\n\n"
        f"Track it on the Confirmations tab: {tab_url}"
    )


def _requester_email_subject(record: Mapping[str, Any]) -> str:
    return f"[ROBIE] Your job is waiting on approval: {confirmations.confirmation_summary(record)[:90]}"


def _requester_email_body(record: Mapping[str, Any], tab_url: str) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        f"Hi,\n\nYour ROBIE job is waiting on a human decision and can't "
        f"continue until it's approved:\n\n{summary}\n\n"
        f"Track it on the Confirmations tab:\n{tab_url}\n\n"
        "You'll get another note here once it's decided.\n"
    )


def _real_chat_thread_poster() -> Any:
    from .chat_app_post import post_as_chat_app

    def post(space: str, text: str, thread: str | None = None) -> dict[str, Any]:
        if not str(space or "").startswith("spaces/"):
            raise ValueError("origin chat post needs a spaces/ space name")
        result = post_as_chat_app(space, text, thread_name=thread)
        return {"space": space, "thread": thread, "message_name": result.get("name")}
    return post


def _origin_ref_dict(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("origin_ref")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def notify_requester_on_origin(
    store: Any,
    confirmation_id: str,
    *,
    chat_thread_poster: Any = None,
    gmail_sender: Any = None,
    zap_trigger: Any = None,
    given_sheet_id: str | None = None,
) -> dict[str, Any]:
    """Ping the requester back on the platform they started on.

    chat -> the originating Chat thread; email -> a reply to the
    requester's address; ezlynx -> assign_requester_task (needs a proven
    applicant_id in origin_ref). Idempotent per (confirmation, platform);
    failures are returned as errors, never raised.
    """
    record = confirmations.get(confirmation_id, store=store)
    if record is None:
        raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
    if str(record.get("status")) != "PENDING":
        return {"confirmation_id": confirmation_id, "skipped": "not PENDING"}

    platform = str(record.get("origin_platform") or "").strip().casefold()
    if not platform:
        return {"confirmation_id": confirmation_id, "skipped": "no origin platform"}
    channel = f"requester_{platform}"
    if was_notified(store, confirmation_id, channel):
        return {"confirmation_id": confirmation_id, "skipped": "already notified"}

    ref = _origin_ref_dict(record)
    tab_url = confirmations_tab_url(given_sheet_id)
    try:
        if platform == "chat":
            space = str(ref.get("space") or "").strip()
            thread = str(ref.get("thread") or "").strip() or None
            if not space:
                raise ValueError("chat origin needs origin_ref.space")
            poster = chat_thread_poster if chat_thread_poster is not None else _real_chat_thread_poster()
            receipt = poster(space, _requester_chat_text(record, tab_url), thread)
        elif platform == "email":
            to = str(ref.get("to") or "").strip()
            if "@" not in to:
                raise ValueError("email origin needs origin_ref.to")
            sender = gmail_sender if gmail_sender is not None else _real_gmail_sender()
            receipt = sender(to, _requester_email_subject(record), _requester_email_body(record, tab_url))
        elif platform == "ezlynx":
            applicant_id = str(ref.get("applicant_id") or "").strip()
            if not applicant_id:
                raise ValueError("ezlynx origin needs origin_ref.applicant_id (proven in EZLynx)")
            receipt = assign_requester_task(
                store, confirmation_id, applicant_id=applicant_id, zap_trigger=zap_trigger
            )
        else:
            return {"confirmation_id": confirmation_id, "skipped": f"unknown platform {platform!r}"}
    except Exception as exc:
        return {
            "confirmation_id": confirmation_id,
            "errors": [{"channel": channel, "error": f"{type(exc).__name__}: {exc}"}],
        }

    _record(store, confirmation_id, channel, receipt)
    return {"confirmation_id": confirmation_id, "notified": [{"channel": channel, **receipt}]}


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
    chat_thread_poster: Any = None,
    zap_trigger: Any = None,
    given_sheet_id: str | None = None,
) -> dict[str, Any]:
    """Notify Carlo (Chat + email) and the requester on their origin platform.

    Carlo's Chat + email are the approval ask. The requester additionally
    hears back on the platform they started on (Chat thread, email, or an
    EZLynx task) via ``notify_requester_on_origin``. Idempotent per
    (confirmation, channel): already-notified channels are skipped. Channel
    failures are returned as errors, never raised, so one broken channel
    cannot block the others -- and the ledger only records successes, so
    failures retry on the next sync.
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

    # The requester hears back on the platform they started on.
    origin_result = notify_requester_on_origin(
        store,
        confirmation_id,
        chat_thread_poster=chat_thread_poster,
        gmail_sender=gmail_sender,
        zap_trigger=zap_trigger,
        given_sheet_id=given_sheet_id,
    )
    notified.extend(origin_result.get("notified", []))
    errors.extend(origin_result.get("errors", []))

    return {"confirmation_id": confirmation_id, "notified": notified, "errors": errors}


# ---------------------------------------------------------------------------
# EZLynx requester task: "assign a task back in EZLynx to the user who
# requested". The Zap's Task Assignee field is free text and EZLynx rejects
# anything it cannot resolve as a login username -- display names like
# "Carlo Ferrara" fail. Unknown requesters fail closed; add them via
# ROBIE_EZLYNX_LOGIN_<NAME> (e.g. ROBIE_EZLYNX_LOGIN_JAKE=JakeSS).
# ---------------------------------------------------------------------------

#: Requester name (normalized) -> EZLynx login username. Seeded from
#: usernames Carlo supplied directly. Extend with ROBIE_EZLYNX_LOGIN_*.
REQUESTER_LOGINS: dict[str, str] = {
    "carlo": "Carlo1",
    "carlo ferrara": "Carlo1",
    "karla": "KarlaSS",
    "karla brown": "KarlaSS",
    "matthew": "Mancina1",
    "matthew mancina": "Mancina1",
    "mancina": "Mancina1",
    "markley": "Accounting Team",
}

ZAP_TRIGGER = os.path.expanduser("~/workspace/skills/zapier/bin/zap-trigger")


def _normalize_requester(requested_by: str) -> str:
    return " ".join(str(requested_by or "").strip().casefold().split())


def requester_login(requested_by: str) -> str | None:
    """EZLynx login username for a requester, or None when unknown."""
    key = _normalize_requester(requested_by)
    env_key = "ROBIE_EZLYNX_LOGIN_" + "_".join(key.split()).upper()
    override = _env(env_key)
    if override:
        return override
    return REQUESTER_LOGINS.get(key)


def _default_due_date() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")


def _fire_zap_task(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fire the EZLynx follow-up-task Zap. Fail-closed on any error."""
    completed = subprocess.run(
        [ZAP_TRIGGER, "--payload", json.dumps(dict(payload)), "--applicant-verified"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError(f"zap-trigger exited {completed.returncode}: {out.strip()[:500]}")
    return {"zap_output": out.strip()[:500]}


def assign_requester_task(
    store: Any,
    confirmation_id: str,
    *,
    applicant_id: str,
    due_date: str | None = None,
    zap_trigger: Any = None,
    source: str = "confirmation-board",
) -> dict[str, Any]:
    """Create an EZLynx follow-up task for the user who requested the work.

    The task tells the requester their ROBIE job is waiting on a human
    decision and points at the Confirmations tab. Fires through the
    agency's Zapier catch-hook (the real task path).

    Fail-closed: unknown confirmation, non-PENDING confirmation, requester
    with no known EZLynx login username, or missing applicant_id all raise
    ``ValueError`` -- nothing fires. ``applicant_id`` must already be
    proven in EZLynx by the caller (the Zap refuses unverified ids).
    Idempotent per confirmation: a second call returns the recorded
    receipt instead of firing again.
    """
    record = confirmations.get(confirmation_id, store=store)
    if record is None:
        raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
    if str(record.get("status")) != "PENDING":
        return {"confirmation_id": confirmation_id, "skipped": "not PENDING"}
    if was_notified(store, confirmation_id, "ezlynx_task"):
        return {"confirmation_id": confirmation_id, "skipped": "already assigned"}

    requested_by = str(record.get("requested_by") or "")
    login = requester_login(requested_by)
    if not login:
        raise ValueError(
            f"no known EZLynx login username for requester {requested_by!r}; "
            "set ROBIE_EZLYNX_LOGIN_<NAME> to add one"
        )
    applicant_id = str(applicant_id or "").strip()
    if not applicant_id:
        raise ValueError("applicant_id is required and must already be proven in EZLynx")

    summary = confirmations.confirmation_summary(record)
    payload = {
        "applicant_id": applicant_id,
        "task_title": f"ROBIE approval needed: {summary[:80]}",
        "task_description": (
            f"Your ROBIE job is waiting on a human decision.\n\n{summary}\n\n"
            f"Approve or reject it on the Confirmations tab:\n"
            f"{confirmations_tab_url()}\n\n"
            "Type APPROVE or REJECT in the \"Your decision\" column."
        ),
        "assignee": login,
        "due_date": due_date or _default_due_date(),
        "source": source,
        "confirmation_id": confirmation_id,
    }
    fire = zap_trigger if zap_trigger is not None else _fire_zap_task
    receipt = fire(payload)
    _record(store, confirmation_id, "ezlynx_task", {"assignee": login, **receipt})
    return {"confirmation_id": confirmation_id, "assignee": login, "receipt": receipt}
