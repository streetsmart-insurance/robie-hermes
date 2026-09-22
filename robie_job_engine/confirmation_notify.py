"""Fan-out notifications for HITL plan confirmations.

The Confirmations tab alone is not enough: Carlo will not be staring at
the sheet. When a confirmation is requested, he hears about it in Google
Chat (his DM with the Robie app, falling back to the Robie home space)
and by email. Every sent notification is recorded in the ledger, so a
re-sync never double-notifies; a channel that fails is recorded as an
error and retried on the next sync.

The Chat DM and email are the authenticated surfaces: each carries
single-purpose APPROVE and REJECT decision tokens (minted via
``confirmations.mint_decision_token`` for the approver principal) that
Carlo pastes into the sheet's decision column -- magic-link style. The
sheet itself is unauthenticated display space; without a configured
signing key no tokens are minted, the tab is display-only, and the
notification says so instead of pretending approvals work.

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
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Mapping

from . import confirmations
from .store import JobStore


logger = logging.getLogger(__name__)

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
# Decision tokens for the approver, minted at notify time and delivered
# inside the Chat DM / email (the authenticated surfaces). Carlo pastes one
# into the sheet's decision column -- the token, not the typed word, is the
# authorization.
# ---------------------------------------------------------------------------

def approver_principal() -> str:
    """The identity the decision tokens bind: the verified approver inbox."""
    return notify_email_to()


def mint_approver_tokens(
    confirmation_id: str, *, decision_key: Any = None
) -> dict[str, str] | None:
    """Mint APPROVE + REJECT tokens for the approver, or None when no
    signing key is configured (the tab is display-only then). A malformed
    key logs a warning and degrades to display-only rather than crashing
    the notification fan-out.
    """
    try:
        key = confirmations.decision_signing_key(decision_key)
    except ValueError as exc:
        logger.warning(
            "decision signing key unusable (%s); notifying without tokens", exc
        )
        return None
    if key is None:
        return None
    principal = approver_principal()
    return {
        "APPROVE": confirmations.mint_decision_token(
            confirmation_id, "APPROVE", principal, key=key
        ),
        "REJECT": confirmations.mint_decision_token(
            confirmation_id, "REJECT", principal, key=key
        ),
    }


def _decision_instructions(tokens: dict[str, str] | None, tab_url: str) -> str:
    # LEGACY sheet-paste path (deprecated as the human decision path
    # 2026-09-22; Chat-native buttons are primary). Kept working as the
    # fallback/audit mirror for one release.
    if tokens:
        return (
            f"Decide on the Confirmations tab: {tab_url}\n"
            "Paste ONE of these into the \"Your decision\" column "
            f"(they expire in {confirmations.DEFAULT_TOKEN_TTL_SECONDS // 86400} days):\n"
            f"  {tokens['APPROVE']}\n"
            f"  {tokens['REJECT']}\n"
            "The signed token is the approval -- a typed word is not."
        )
    return (
        f"Review it on the Confirmations tab: {tab_url}\n"
        "The tab is display-only right now (no decision signing key "
        "configured), so approvals cannot be taken from the sheet until "
        "the key is set and this notice is re-sent."
    )


# ---------------------------------------------------------------------------
# Message content: plain English, no jargon.
# ---------------------------------------------------------------------------

def _chat_text(
    record: Mapping[str, Any], tab_url: str,
    tokens: dict[str, str] | None = None,
) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        f"ROBIE needs your approval.\n\n{summary}\n\n"
        + _decision_instructions(tokens, tab_url)
    )


def _email_subject(record: Mapping[str, Any]) -> str:
    return f"[ROBIE] Approval needed: {confirmations.confirmation_summary(record)[:90]}"


def _email_body(
    record: Mapping[str, Any], tab_url: str,
    tokens: dict[str, str] | None = None,
) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        f"Hi Carlo,\n\nROBIE needs your approval before it can continue:\n\n"
        f"{summary}\n\n"
        + _decision_instructions(tokens, tab_url)
        + "\nIf you reject, add a short reason in the Reason column.\n\n"
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
    decision_key: Any = None,
) -> dict[str, Any]:
    """Notify Carlo (Chat + email) and the requester on their origin platform.

    LEGACY fan-out (kept working): prefer ``notify_approval_on_origin``,
    which routes the ask on its origin medium instead of blasting Chat +
    email every time. This function remains the fallback for confirmations
    filed without an origin platform, and its behavior is unchanged.

    Carlo's Chat + email are the approval ask: when a decision signing key
    is configured, both legs carry APPROVE/REJECT tokens minted for his
    inbox, which he pastes into the sheet's decision column (magic-link
    style). The requester additionally hears back on the platform they
    started on (Chat thread, email, or an EZLynx task) via
    ``notify_requester_on_origin`` -- requesters never receive tokens.
    Idempotent per (confirmation, channel): already-notified channels are
    skipped. Channel failures are returned as errors, never raised, so one
    broken channel cannot block the others -- and the ledger only records
    successes, so failures retry on the next sync.
    """
    record = confirmations.get(confirmation_id, store=store)
    if record is None:
        raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
    if str(record.get("status")) != "PENDING":
        return {"confirmation_id": confirmation_id, "notified": [], "errors": [],
                "skipped": "not PENDING"}

    tab_url = confirmations_tab_url(given_sheet_id)
    tokens = mint_approver_tokens(confirmation_id, decision_key=decision_key)
    notified: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not was_notified(store, confirmation_id, "google_chat"):
        try:
            poster = chat_poster if chat_poster is not None else _real_chat_poster()
            receipt = poster(_chat_text(record, tab_url, tokens))
            _record(store, confirmation_id, "google_chat", receipt)
            notified.append({"channel": "google_chat", **receipt})
        except Exception as exc:
            errors.append({"channel": "google_chat", "error": f"{type(exc).__name__}: {exc}"})

    if not was_notified(store, confirmation_id, "email"):
        try:
            sender = gmail_sender if gmail_sender is not None else _real_gmail_sender()
            receipt = sender(notify_email_to(), _email_subject(record), _email_body(record, tab_url, tokens))
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
# "Carlo Ferrara" fail ("Can't create note as assignee not found").
# Unknown requesters fail closed; add them via
# ROBIE_EZLYNX_LOGIN_<NAME> (e.g. ROBIE_EZLYNX_LOGIN_JAKE=jferrara3).
# ---------------------------------------------------------------------------

#: Requester name (normalized) -> EZLynx login username. Full agency
#: directory seeded from EZLynx Agency Admin > Manage Users, exported by
#: Carlo 2026-09-22 (35 users). Bare first names are included only where
#: unambiguous -- "andrea" is intentionally absent (Andrea Illanes and
#: Andrea Martinez share it). Extend/override with ROBIE_EZLYNX_LOGIN_*
#: (env wins over this table).
REQUESTER_LOGINS: dict[str, str] = {
    "accounting team": "Markley1",
    "alejandro": "Alejandro11",
    "alejandro zelaya": "Alejandro11",
    "amber": "Amber14",
    "amber voigt": "Amber14",
    "ana": "anaflores",
    "ana flores": "anaflores",
    "andrea illanes": "a_illanes",
    "andrea martinez": "Amartinez21",
    "angie": "AngieV",
    "angie valladarez": "AngieV",
    "ashley": "ahuntley",
    "ashley huntley": "ahuntley",
    "carlo": "Carlo1",
    "carlo ferrara": "Carlo1",
    "daniela": "Daniela_Aguilar",
    "daniela aguilar": "Daniela_Aguilar",
    "diana": "Diana12",
    "diana cabrera": "Diana12",
    "eimy": "Eramos1",
    "eimy ramos": "Eramos1",
    "erika": "Erika11",
    "erika palacios": "Erika11",
    "eunice": "Eunice",
    "eunice iraheta": "Eunice",
    "gabriela": "Gabrielac1",
    "gabriela chutin": "Gabrielac1",
    "jackie": "Jackie_Arriola",
    "jackie arriola": "Jackie_Arriola",
    "jake": "jferrara3",
    "jake ferrara": "jferrara3",
    "jazmin": "Jazmin11",
    "jazmin molina": "Jazmin11",
    "jimmy": "Jimmy1",
    "jimmy ferrara": "Jimmy1",
    "jose": "Josecabrera",
    "jose cabrera": "Josecabrera",
    "karla": "KarlaSS",
    "karla brown": "KarlaSS",
    "lenin": "Lperdomo1",
    "lenin perdomo": "Lperdomo1",
    "maria": "MariaB12",
    "maria bara": "MariaB12",
    "markley": "Markley1",
    "matthew": "Mancina1",
    "matthew mancina": "Mancina1",
    "mancina": "Mancina1",
    "mike": "MikeS1",
    "mike sosa": "MikeS1",
    "mitchell": "Mitch1",
    "mitchell slagle": "Mitch1",
    "nelson": "Nmaldonado2",
    "nelson maldonado": "Nmaldonado2",
    "nicole": "SSNicole",
    "nicole segovia": "SSNicole",
    "ricardo": "Ricardo2",
    "ricardo aguilar": "Ricardo2",
    "robie": "SSRobie",
    "robie ai": "SSRobie",
    "sandeep": "Sandeep11",
    "sandeep yadav": "Sandeep11",
    "sandy": "Sandy11",
    "sandy santana": "Sandy11",
    "steffany": "SCanales",
    "steffany canales": "SCanales",
    "taylor": "TCimei",
    "taylor cimei": "TCimei",
    "zeus": "Zeus12",
    "zeus quezada": "Zeus12",
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
            f"Track it on the Confirmations tab:\n"
            f"{confirmations_tab_url()}\n\n"
            "Carlo approves or rejects; the decision is signed, not typed."
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


# ---------------------------------------------------------------------------
# Origin-medium notify policy (Carlo 2026-09-22): origin wins.
#
# Reply on the medium where the ask started; never blast Chat + EZLynx +
# email for one ask.
#
# - chat origin   -> the approval CARD (Approve/Reject buttons, signed
#                    tokens) goes in the originating Chat thread. That one
#                    card is the decision surface. Email is a tokenless
#                    backup heads-up only, never a second live decision
#                    thread. EZLynx is NEVER touched from a Chat-origin ask --
#                    not even when an applicant id happens to be on the job.
# - email origin  -> the approval ask goes by email with signed tokens
#                    (the live surface, as before). No parallel Chat ask.
# - ezlynx origin -> EZLynx task only, and only when the applicant id is
#                    already proven on the job (fail closed otherwise -- an
#                    EZLynx note/task is never invented). No Chat thread up
#                    front. After the quiet window with no decision,
#                    ``maybe_quiet_nudge`` posts ONE Chat nudge with links
#                    back to the item.
# - missing/unknown origin (old rows) -> legacy ``notify_requested``
#                    fan-out (Chat DM + email to the approver), so an ask
#                    never goes silent for lack of routing data.
#
# Every leg is idempotent per (confirmation, channel) in the ledger; a
# channel that fails is returned as an error, never raised.
# ---------------------------------------------------------------------------

#: Ledger channel names used by the origin router.
CHANNEL_ORIGIN_CHAT_CARD = "origin_chat_card"
CHANNEL_BACKUP_EMAIL = "backup_email"
CHANNEL_ORIGIN_EMAIL = "origin_email"
CHANNEL_CHAT_NUDGE = "chat_nudge"


def quiet_window_hours() -> float:
    """Hours of EZLynx silence before the one Chat nudge. Env-overridable."""
    raw = _env("ROBIE_CONFIRMATION_QUIET_HOURS", "24")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 24.0
    return value if value > 0 else 24.0


def _channel_notified_at(store: Any, confirmation_id: str, channel: str) -> str | None:
    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            f"SELECT notified_at FROM {NOTIFY_TABLE} WHERE confirmation_id = ? AND channel = ?",
            (confirmation_id, channel),
        ).fetchone()
        return str(row["notified_at"]) if row and row["notified_at"] else None
    finally:
        conn.close()


def _parse_notified_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _real_approval_card_poster() -> Any:
    """Post the approval card to a Chat space/thread as the Chat app."""
    from . import confirmation_cards
    from .chat_app_post import post_card_as_chat_app

    def post(space: str, card_v2: dict[str, Any], thread: str | None = None) -> dict[str, Any]:
        if not str(space or "").startswith("spaces/"):
            raise ValueError("origin chat post needs a spaces/ space name")
        result = post_card_as_chat_app(space, [card_v2], thread_name=thread)
        return {"space": space, "thread": thread, "message_name": result.get("name")}
    return post


def _backup_email_subject(record: Mapping[str, Any]) -> str:
    return f"[ROBIE] Approval waiting in Chat: {confirmations.confirmation_summary(record)[:90]}"


def _backup_email_body(record: Mapping[str, Any], tab_url: str) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        "Hi Carlo,\n\n"
        "Heads-up only: ROBIE needs your approval and the decision is "
        "waiting in the originating Chat thread -- tap Approve or Reject "
        "there.\n\n"
        f"{summary}\n\n"
        "This email is not a decision channel (no tokens inside). "
        "Reference copy on the Confirmations tab:\n"
        f"{tab_url}\n\n"
        "Nothing moves until you decide.\n"
    )


def post_origin_approval_card(
    store: Any,
    confirmation_id: str,
    *,
    card_poster: Any = None,
    decision_key: Any = None,
) -> dict[str, Any]:
    """Post the Chat-native approval card to the originating Chat thread.

    Chat-origin asks only. The card's buttons carry the signed decision
    tokens; without a signing key the card posts display-only. Idempotent
    per confirmation (``origin_chat_card`` ledger channel).
    """
    record = confirmations.get(confirmation_id, store=store)
    if record is None:
        raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
    if str(record.get("status")) != "PENDING":
        return {"confirmation_id": confirmation_id, "skipped": "not PENDING"}
    if was_notified(store, confirmation_id, CHANNEL_ORIGIN_CHAT_CARD):
        return {"confirmation_id": confirmation_id, "skipped": "already notified"}
    ref = _origin_ref_dict(record)
    space = str(ref.get("space") or "").strip()
    thread = str(ref.get("thread") or "").strip() or None
    if not space:
        raise ValueError("chat origin needs origin_ref.space")
    from . import confirmation_cards

    tokens = mint_approver_tokens(confirmation_id, decision_key=decision_key)
    card_v2 = confirmation_cards.approval_card_v2(record, tokens)
    poster = card_poster if card_poster is not None else _real_approval_card_poster()
    receipt = poster(space, card_v2, thread)
    _record(store, confirmation_id, CHANNEL_ORIGIN_CHAT_CARD, receipt)
    return {"confirmation_id": confirmation_id, "notified": [{"channel": CHANNEL_ORIGIN_CHAT_CARD, **receipt}]}


def notify_approval_on_origin(
    store: Any,
    confirmation_id: str,
    *,
    approval_card_poster: Any = None,
    gmail_sender: Any = None,
    zap_trigger: Any = None,
    chat_poster: Any = None,
    decision_key: Any = None,
    given_sheet_id: str | None = None,
) -> dict[str, Any]:
    """Route one approval ask on its origin medium (origin wins).

    See the module section above for the full policy. ``chat_poster`` is
    the legacy text poster to the approver's Chat DM; it is used only for
    the missing-origin fallback (via ``notify_requested``). Failures are
    returned as errors, never raised.
    """
    record = confirmations.get(confirmation_id, store=store)
    if record is None:
        raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
    if str(record.get("status")) != "PENDING":
        return {"confirmation_id": confirmation_id, "notified": [], "errors": [],
                "skipped": "not PENDING"}

    origin = str(record.get("origin_platform") or "").strip().casefold()
    if not origin:
        # Old rows predate origin tracking: keep the proven fan-out so the
        # ask never goes silent for lack of routing data.
        logger.info(
            "confirmation %s has no origin_platform; using legacy fan-out",
            confirmation_id,
        )
        return notify_requested(
            store,
            confirmation_id,
            chat_poster=chat_poster,
            gmail_sender=gmail_sender,
            zap_trigger=zap_trigger,
            given_sheet_id=given_sheet_id,
            decision_key=decision_key,
        )

    notified: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    tab_url = confirmations_tab_url(given_sheet_id)
    ref = _origin_ref_dict(record)

    if origin == "chat":
        # The card in the originating thread is the decision surface.
        try:
            card_result = post_origin_approval_card(
                store,
                confirmation_id,
                card_poster=approval_card_poster,
                decision_key=decision_key,
            )
            notified.extend(card_result.get("notified", []))
        except Exception as exc:
            errors.append({"channel": CHANNEL_ORIGIN_CHAT_CARD,
                           "error": f"{type(exc).__name__}: {exc}"})
        # Backup heads-up email: tokenless, points at the Chat thread.
        # Never a second live decision thread, and never an EZLynx write:
        # a Chat-origin ask without (or with) an applicant never touches
        # EZLynx.
        if not was_notified(store, confirmation_id, CHANNEL_BACKUP_EMAIL):
            try:
                sender = gmail_sender if gmail_sender is not None else _real_gmail_sender()
                receipt = sender(
                    notify_email_to(),
                    _backup_email_subject(record),
                    _backup_email_body(record, tab_url),
                )
                _record(store, confirmation_id, CHANNEL_BACKUP_EMAIL, receipt)
                notified.append({"channel": CHANNEL_BACKUP_EMAIL, **receipt})
            except Exception as exc:
                errors.append({"channel": CHANNEL_BACKUP_EMAIL,
                               "error": f"{type(exc).__name__}: {exc}"})

    elif origin == "email":
        # Email is the live surface for email-origin asks (tokens as before).
        if not was_notified(store, confirmation_id, CHANNEL_ORIGIN_EMAIL):
            try:
                to = str(ref.get("to") or "").strip()
                if "@" not in to:
                    raise ValueError("email origin needs origin_ref.to")
                tokens = mint_approver_tokens(confirmation_id, decision_key=decision_key)
                sender = gmail_sender if gmail_sender is not None else _real_gmail_sender()
                receipt = sender(to, _email_subject(record),
                                 _email_body(record, tab_url, tokens))
                _record(store, confirmation_id, CHANNEL_ORIGIN_EMAIL, receipt)
                notified.append({"channel": CHANNEL_ORIGIN_EMAIL, **receipt})
            except Exception as exc:
                errors.append({"channel": CHANNEL_ORIGIN_EMAIL,
                               "error": f"{type(exc).__name__}: {exc}"})

    elif origin == "ezlynx":
        # EZLynx only: a task on the proven applicant. No Chat thread up
        # front, no parallel email ask. Missing applicant id fails closed --
        # an EZLynx note/task is never invented.
        try:
            applicant_id = str(ref.get("applicant_id") or "").strip()
            if not applicant_id:
                raise ValueError(
                    "ezlynx origin needs origin_ref.applicant_id (proven in "
                    "EZLynx); refusing to invent an EZLynx task"
                )
            receipt = assign_requester_task(
                store, confirmation_id, applicant_id=applicant_id,
                zap_trigger=zap_trigger,
            )
            notified.append({"channel": "ezlynx_task", **receipt})
        except Exception as exc:
            errors.append({"channel": "ezlynx_task",
                           "error": f"{type(exc).__name__}: {exc}"})

    else:
        errors.append({"channel": "origin",
                       "error": f"unknown origin platform {origin!r}; ask not routed"})

    return {"confirmation_id": confirmation_id, "notified": notified, "errors": errors}


def _nudge_text(record: Mapping[str, Any], tab_url: str, waited: str) -> str:
    summary = confirmations.confirmation_summary(record)
    return (
        "ROBIE approval still waiting (EZLynx).\n\n"
        f"{summary}\n\n"
        f"The EZLynx task went out {waited} with no decision yet. "
        "This is the one nudge -- the EZLynx task stays the record:\n"
        f"{tab_url}\n"
        "Reply here and the conversation continues in Chat."
    )


def maybe_quiet_nudge(
    store: Any,
    confirmation_id: str,
    *,
    chat_poster: Any = None,
    quiet_hours: float | None = None,
    now: datetime | None = None,
    given_sheet_id: str | None = None,
) -> dict[str, Any]:
    """Quiet escalate: one Chat nudge after the quiet window.

    Only for EZLynx-origin asks that are still PENDING: when the EZLynx leg
    went out at least ``quiet_hours`` ago (default 24, env
    ``ROBIE_CONFIRMATION_QUIET_HOURS``) with no decision, post ONE Chat
    nudge to the approver's DM with links back to the item. Idempotent via
    the ``chat_nudge`` ledger channel -- a second call never re-nudges.
    This is a nudge with links, not a second full conversation.
    """
    def _skip(reason: str) -> dict:
        return {"confirmation_id": confirmation_id, "notified": [],
                "errors": [], "skipped": reason}

    record = confirmations.get(confirmation_id, store=store)
    if record is None:
        raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
    if str(record.get("status")) != "PENDING":
        return _skip("not PENDING")
    origin = str(record.get("origin_platform") or "").strip().casefold()
    if origin != "ezlynx":
        return _skip("not ezlynx origin")
    if was_notified(store, confirmation_id, CHANNEL_CHAT_NUDGE):
        return _skip("already nudged")
    sent_at = _parse_notified_at(_channel_notified_at(store, confirmation_id, "ezlynx_task"))
    if sent_at is None:
        return _skip("ezlynx leg not sent yet")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    window = quiet_hours if quiet_hours and quiet_hours > 0 else quiet_window_hours()
    elapsed = (moment - sent_at).total_seconds() / 3600.0
    if elapsed < window:
        return _skip(f"quiet window not elapsed ({elapsed:.1f}h < {window:g}h)")
    waited = f"{elapsed:.0f}h ago" if elapsed < 48 else f"{elapsed/24:.0f}d ago"
    tab_url = confirmations_tab_url(given_sheet_id)
    try:
        poster = chat_poster if chat_poster is not None else _real_chat_poster()
        receipt = poster(_nudge_text(record, tab_url, waited))
        _record(store, confirmation_id, CHANNEL_CHAT_NUDGE, receipt)
        return {"confirmation_id": confirmation_id,
                "notified": [{"channel": CHANNEL_CHAT_NUDGE, **receipt}],
                "errors": []}
    except Exception as exc:
        return {"confirmation_id": confirmation_id,
                "errors": [{"channel": CHANNEL_CHAT_NUDGE,
                            "error": f"{type(exc).__name__}: {exc}"}]}
