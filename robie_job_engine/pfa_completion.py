"""PFA completion guarantee (Astra Gold incident, 2026-10-02/03).

On 2026-10-02/03 Jake's ASTRAGOLD LLC premium-finance request was corrected
(Diesel is the wholesaler, Fortegra is the carrier), the Ascend agreement was
created (program 6f5cec3a, ready_for_checkout) -- but the workflow hit HITL
failures and NEVER confirmed completion to Jake, nobody independently
verified the persisted fields against the request, and the agreement had been
created by a manual API call outside the workflow so no notification, EZLynx
note, or follow-up fired. Jake resent the request and escalated to Carlo.

Four mechanisms so this can never happen again:

1. verify_program_against_request: after program creation, read the program
   and its billables back from Ascend and diff every request field. ANY
   mismatch -- or an unreadable read-back -- fails closed: no checkout link
   is ever composed or sent for an unverified program.
2. PfaNotificationOutbox: a durable (SQLite) outbox decoupling "agreement
   created" from "requester notified". drain_outbox retries each pending
   notification until Gmail confirms the message actually sent (SENT label
   read-back). No silent completions, ever.
3. reconcile_pfa_completions: daily scan of Ascend for ready_for_checkout
   programs with no confirmed notification. Re-fires the pending
   notification and alerts. Catches manually-created agreements too, with
   zero reliance on human discipline. (This is different from the
   pfa-nudge-check-tuesday cron, which nudges staff to USE Robie; this one
   verifies COMPLETIONS.)
4. HITL-stuck email: when the workflow waits on a human past a threshold,
   the requester gets a plain-English "here's what's holding up your
   request" note -- via the same outbox, so it carries the same
   retry-until-confirmed guarantee -- instead of silence.

All Ascend/Gmail access goes through injected collaborators (client,
sender, alerter), so every path is unit-testable with mocks and no real
network calls happen in tests.

Production wiring (follow-up, NOT in this change):
- NotificationSender: implement against the box's Gmail path with
  send_email -> message id, confirm_sent -> SENT label read-back, and
  find_sent_for_program -> Sent search for the program id.
- alerter: post to the ROBIE health Chat space.
- reconciler: run daily via cron/scheduler, after the PFA email watcher.
- outbox DB: set PFA_OUTBOX_DB to a durable path on the box.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

READY_FOR_CHECKOUT = "ready_for_checkout"


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _norm_text(value: Any) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy compare."""
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _norm_phone(value: Any) -> str:
    """Digits only: '+1 (425) 754-5595' == '4257545595' == '14257545595'[-10:]."""
    digits = re.sub(r"\D", "", str(value or ""))
    # Tolerate a leading country '1' on either side.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _cents(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 1. Verify-before-complete
# ---------------------------------------------------------------------------

@dataclass
class PfaExpectedBillable:
    """One billable as the workflow sent it to Ascend (payload shape)."""
    billable_identifier: str = ""
    carrier_identifier: str = ""
    wholesaler_identifier: Optional[str] = None
    premium_cents: int = 0
    taxes_and_fees_cents: int = 0
    policy_fee_cents: int = 0
    agency_fees_cents: int = 0
    # The workflow sends organization_commission_rate; Ascend persists it as
    # seller_commission_rate on the read-back.
    commission_rate: float = 0.0
    effective_date: str = ""
    expiration_date: str = ""


@dataclass
class PfaExpected:
    """Everything the verifier diffs the Ascend read-back against."""
    insured_business_name: str = ""
    # Ascend mailing_address_* keys, e.g. {"mailing_address_street_one": ...}
    address: dict[str, Any] = field(default_factory=dict)
    # first_name / last_name / email / phone
    contact: dict[str, Any] = field(default_factory=dict)
    billables: list[PfaExpectedBillable] = field(default_factory=list)
    requester_email: str = ""
    requester_name: str = ""


@dataclass
class ProgramVerification:
    ok: bool
    diffs: list[str]
    program_id: str
    summary: str  # plain-English verified-facts summary for the notification


def build_expected_from_quote(
    quote: Any,
    payload_billables: list[dict[str, Any]],
    requester_email: str = "",
    requester_name: str = "",
) -> PfaExpected:
    """Build the verifier's expected values from the extracted quote and the
    exact billable payloads the workflow sent to Ascend."""
    billables: list[PfaExpectedBillable] = []
    for b in payload_billables or []:
        billables.append(
            PfaExpectedBillable(
                billable_identifier=str(b.get("billable_identifier") or ""),
                carrier_identifier=str(b.get("carrier_identifier") or ""),
                wholesaler_identifier=b.get("wholesaler_identifier"),
                premium_cents=_cents(b.get("premium_cents")),
                taxes_and_fees_cents=_cents(b.get("taxes_and_fees_cents")),
                policy_fee_cents=_cents(b.get("policy_fee_cents")),
                agency_fees_cents=_cents(b.get("agency_fees_cents")),
                commission_rate=float(b.get("organization_commission_rate") or 0.0),
                effective_date=str(b.get("effective_date") or ""),
                expiration_date=str(b.get("expiration_date") or ""),
            )
        )
    return PfaExpected(
        insured_business_name=str(getattr(quote, "insured_name", "") or ""),
        address=dict(getattr(quote, "mailing_address", "") or {}),
        contact=dict(getattr(quote, "primary_contact", "") or {}),
        billables=billables,
        requester_email=requester_email,
        requester_name=requester_name,
    )


def _diff_field(diffs: list[str], label: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        diffs.append(f"{label}: expected {expected!r}, Ascend has {actual!r}")


def verify_program_against_request(
    client: Any,
    program_id: str,
    expected: PfaExpected,
) -> ProgramVerification:
    """Read the program + billables back from Ascend and diff every field.

    Fail-closed: ANY mismatch -- or a read-back that is missing/unreadable --
    returns ok=False and the exact diffs. Never raises for a verification
    problem (transport errors propagate; the caller treats them as failures).
    """
    diffs: list[str] = []

    program = client.get_program(program_id)
    if not isinstance(program, dict):
        return ProgramVerification(
            ok=False,
            diffs=[f"program {program_id}: Ascend read-back unreadable (not a record)"],
            program_id=program_id,
            summary="",
        )

    status = program.get("status")
    if status != READY_FOR_CHECKOUT:
        diffs.append(
            f"program status: expected {READY_FOR_CHECKOUT!r}, Ascend has {status!r}"
        )

    # -- insured -----------------------------------------------------------
    insured = program.get("insured")
    if not isinstance(insured, dict):
        diffs.append("insured: Ascend read-back has no insured record")
        insured = {}
    else:
        _diff_field(
            diffs,
            "insured business name",
            _norm_text(expected.insured_business_name),
            _norm_text(insured.get("business_name")),
        )
        if expected.address:
            for key in (
                "mailing_address_street_one",
                "mailing_address_city",
                "mailing_address_state",
                "mailing_address_zip_code",
            ):
                _diff_field(
                    diffs,
                    f"insured {key}",
                    _norm_text(expected.address.get(key)),
                    _norm_text(insured.get(key)),
                )
        contacts = insured.get("insured_contacts") or []
        contact = contacts[0] if isinstance(contacts, list) and contacts else {}
        if not isinstance(contact, dict):
            contact = {}
        if expected.contact:
            _diff_field(
                diffs,
                "contact first name",
                _norm_text(expected.contact.get("first_name")),
                _norm_text(contact.get("first_name")),
            )
            _diff_field(
                diffs,
                "contact last name",
                _norm_text(expected.contact.get("last_name")),
                _norm_text(contact.get("last_name")),
            )
            _diff_field(
                diffs,
                "contact email",
                _norm_text(expected.contact.get("email")),
                _norm_text(contact.get("email")),
            )
            if expected.contact.get("phone"):
                _diff_field(
                    diffs,
                    "contact phone",
                    _norm_phone(expected.contact.get("phone")),
                    _norm_phone(contact.get("phone")),
                )

    # -- billables ----------------------------------------------------------
    try:
        resp = client.transport.request(
            "GET", "/billables", query={"program_id": program_id}
        )
    except Exception as exc:
        return ProgramVerification(
            ok=False,
            diffs=[f"billables read-back failed for program {program_id}: {exc}"],
            program_id=program_id,
            summary="",
        )
    actual_billables = resp.get("data", []) if isinstance(resp, dict) else []
    if not isinstance(actual_billables, list):
        return ProgramVerification(
            ok=False,
            diffs=[f"billables read-back unreadable for program {program_id}"],
            program_id=program_id,
            summary="",
        )
    by_identifier = {
        str(b.get("billable_identifier") or ""): b
        for b in actual_billables
        if isinstance(b, dict)
    }
    if len(by_identifier) != len(expected.billables):
        diffs.append(
            f"billable count: expected {len(expected.billables)}, "
            f"Ascend has {len(by_identifier)}"
        )
    for exp in expected.billables:
        actual = by_identifier.get(exp.billable_identifier)
        if not isinstance(actual, dict):
            diffs.append(
                f"billable {exp.billable_identifier!r}: not found in Ascend read-back"
            )
            continue
        label = f"billable {exp.billable_identifier}"
        carrier = actual.get("carrier") or {}
        _diff_field(
            diffs,
            f"{label} carrier",
            _norm_text(exp.carrier_identifier),
            _norm_text(carrier.get("identifier") if isinstance(carrier, dict) else ""),
        )
        if exp.wholesaler_identifier:
            wholesaler = actual.get("wholesaler") or {}
            _diff_field(
                diffs,
                f"{label} wholesaler",
                _norm_text(exp.wholesaler_identifier),
                _norm_text(
                    wholesaler.get("identifier") if isinstance(wholesaler, dict) else ""
                ),
            )
        _diff_field(
            diffs, f"{label} premium_cents",
            exp.premium_cents, _cents(actual.get("premium_cents")),
        )
        _diff_field(
            diffs, f"{label} taxes_and_fees_cents",
            exp.taxes_and_fees_cents, _cents(actual.get("taxes_and_fees_cents")),
        )
        _diff_field(
            diffs, f"{label} policy_fee_cents",
            exp.policy_fee_cents, _cents(actual.get("policy_fee_cents")),
        )
        _diff_field(
            diffs, f"{label} agency_fees_cents",
            exp.agency_fees_cents, _cents(actual.get("agency_fees_cents")),
        )
        actual_rate = float(actual.get("seller_commission_rate") or 0.0)
        if abs(actual_rate - exp.commission_rate) > 1e-9:
            diffs.append(
                f"{label} commission rate: expected {exp.commission_rate}, "
                f"Ascend has {actual_rate}"
            )
        expected_amount = round(exp.premium_cents * exp.commission_rate)
        actual_amount = _cents(actual.get("seller_commission_amount_cents"))
        if abs(actual_amount - expected_amount) > 1:
            diffs.append(
                f"{label} commission amount: expected ~{expected_amount} cents, "
                f"Ascend has {actual_amount}"
            )
        _diff_field(
            diffs, f"{label} effective_date",
            exp.effective_date, str(actual.get("effective_date") or ""),
        )
        _diff_field(
            diffs, f"{label} expiration_date",
            exp.expiration_date, str(actual.get("expiration_date") or ""),
        )

    ok = not diffs
    summary = _verification_summary(expected, program) if ok else ""
    return ProgramVerification(ok=ok, diffs=diffs, program_id=program_id, summary=summary)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _verification_summary(expected: PfaExpected, program: dict[str, Any]) -> str:
    """Plain-English verified-facts summary for the notification email."""
    lines = ["Verified against your request:"]
    addr = expected.address or {}
    addr_bits = " ".join(
        str(addr.get(k) or "") for k in (
            "mailing_address_street_one", "mailing_address_city",
            "mailing_address_state", "mailing_address_zip_code",
        )
    ).strip()
    lines.append(
        f"\u2022 Insured: {expected.insured_business_name}"
        + (f", {addr_bits}" if addr_bits else "")
    )
    total_premium = sum(b.premium_cents for b in expected.billables)
    total_taxes = sum(b.taxes_and_fees_cents for b in expected.billables)
    lines.append(f"\u2022 Premium: {_money(total_premium)}, taxes/fees: {_money(total_taxes)}")
    if expected.billables:
        b0 = expected.billables[0]
        lines.append(
            f"\u2022 Effective: {b0.effective_date} to {b0.expiration_date}; "
            f"commission: {b0.commission_rate:.1%}"
        )
    lines.append(f"\u2022 Ascend status: {program.get('status')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. Notification outbox
# ---------------------------------------------------------------------------

KIND_COMPLETION = "completion"
KIND_HITL_STUCK = "hitl_stuck"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pfa_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'completion',
    program_id TEXT NOT NULL DEFAULT '',
    requester_email TEXT NOT NULL DEFAULT '',
    requester_name TEXT NOT NULL DEFAULT '',
    program_url TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    verification_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    message_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT '',
    UNIQUE(kind, program_id)
);
"""


def default_outbox_path() -> str:
    return os.environ.get(
        "PFA_OUTBOX_DB",
        os.path.expanduser("~/.pfa-completion/outbox.db"),
    )


class PfaNotificationOutbox:
    """Durable SQLite outbox: 'agreement created' vs 'requester notified'."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(
        self,
        *,
        kind: str,
        program_id: str,
        requester_email: str,
        requester_name: str = "",
        program_url: str = "",
        subject: str,
        body: str,
        verification_summary: str = "",
    ) -> int:
        """Idempotent enqueue: one record per (kind, program_id)."""
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO pfa_notifications
                (kind, program_id, requester_email, requester_name, program_url,
                 subject, body, verification_summary, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (kind, program_id, requester_email, requester_name, program_url,
                 subject, body, verification_summary, now, now),
            )
            row = conn.execute(
                "SELECT id FROM pfa_notifications WHERE kind = ? AND program_id = ?",
                (kind, program_id),
            ).fetchone()
            return int(row["id"])

    def get_pending(self, kind: Optional[str] = None, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM pfa_notifications WHERE status = 'pending' "
                    "AND kind = ? ORDER BY id LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pfa_notifications WHERE status = 'pending' "
                    "ORDER BY id LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_record(self, kind: str, program_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pfa_notifications WHERE kind = ? AND program_id = ?",
                (kind, program_id),
            ).fetchone()
            return dict(row) if row else None

    def is_notified(self, program_id: str, kind: str = KIND_COMPLETION) -> bool:
        rec = self.get_record(kind, program_id)
        return bool(rec and rec["status"] == STATUS_SENT)

    def mark_sent(self, record_id: int, message_id: str) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE pfa_notifications SET status = 'sent', message_id = ?, "
                "sent_at = ?, updated_at = ?, last_error = '' WHERE id = ?",
                (message_id, now, now, record_id),
            )

    def mark_attempt(self, record_id: int, error: str, max_attempts: int = 5) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE pfa_notifications SET attempts = attempts + 1, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (error[:500], now, record_id),
            )
            conn.execute(
                "UPDATE pfa_notifications SET status = 'failed' "
                "WHERE id = ? AND attempts >= ?",
                (record_id, max_attempts),
            )

    def adopt_sent(self, program_id: str, kind: str = KIND_COMPLETION) -> None:
        """Record an externally-confirmed send (e.g. found in Gmail Sent)."""
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE pfa_notifications SET status = 'sent', sent_at = ?, "
                "updated_at = ? WHERE kind = ? AND program_id = ? "
                "AND status != 'sent'",
                (now, now, kind, program_id),
            )


class NotificationSender(Protocol):
    """Gmail binding (production wiring is a follow-up; tests use fakes)."""

    def send_email(self, to: str, subject: str, body: str) -> str:
        """Send; return the Gmail message id."""
        ...

    def confirm_sent(self, message_id: str) -> bool:
        """True when the message is confirmed in the mailbox (SENT label)."""
        ...

    def find_sent_for_program(self, program_id: str, since_days: int = 7) -> bool:
        """True when Sent already holds a completion email for this program."""
        ...


def drain_outbox(
    outbox: PfaNotificationOutbox,
    sender: NotificationSender,
    max_attempts: int = 5,
) -> dict[str, int]:
    """Send every pending notification; confirm each via the mailbox.

    Returns {"sent": n, "failed": n, "still_pending": n}. A send that is not
    confirmed stays pending for the next drain -- a completion is never
    treated as notified without mailbox proof.
    """
    stats = {"sent": 0, "failed": 0, "still_pending": 0}
    for rec in outbox.get_pending(limit=200):
        if rec["attempts"] >= max_attempts:
            outbox.mark_attempt(rec["id"], "max attempts exceeded", max_attempts)
            stats["failed"] += 1
            continue
        try:
            message_id = sender.send_email(
                rec["requester_email"], rec["subject"], rec["body"]
            )
        except Exception as exc:
            outbox.mark_attempt(rec["id"], f"send failed: {exc}", max_attempts)
            stats["still_pending"] += 1
            continue
        try:
            confirmed = sender.confirm_sent(message_id)
        except Exception as exc:
            outbox.mark_attempt(rec["id"], f"confirm failed: {exc}", max_attempts)
            stats["still_pending"] += 1
            continue
        if confirmed:
            outbox.mark_sent(rec["id"], message_id)
            stats["sent"] += 1
        else:
            outbox.mark_attempt(rec["id"], "sent but not confirmed in mailbox", max_attempts)
            stats["still_pending"] += 1
    return stats


def enqueue_completion_notification(
    outbox: PfaNotificationOutbox,
    *,
    program_id: str,
    program_url: str,
    requester_email: str,
    requester_name: str,
    subject: str,
    body: str,
    verification_summary: str,
) -> int:
    full_body = body
    if verification_summary and verification_summary not in body:
        full_body = body.rstrip() + "\n\n" + verification_summary + "\n"
    return outbox.enqueue(
        kind=KIND_COMPLETION,
        program_id=program_id,
        requester_email=requester_email,
        requester_name=requester_name,
        program_url=program_url,
        subject=subject,
        body=full_body,
        verification_summary=verification_summary,
    )


def compose_completion_email(
    *,
    requester_name: str,
    insured_name: str,
    carrier_name: str,
    wholesaler_name: str,
    premium_cents: int,
    program_url: str,
    verification_summary: str = "",
    save_only: bool = True,
) -> tuple[str, str]:
    """Concise plain-English completion email for agency staff."""
    greeting = requester_name or "Team"
    subject = f"Finance agreement ready: {insured_name}"
    lines = [
        f"Hi {greeting},",
        "",
        f"The premium finance agreement for {insured_name} is ready.",
        "",
        f"\u2022 Carrier: {carrier_name or 'N/A'}",
    ]
    if wholesaler_name:
        lines.append(f"\u2022 Wholesaler: {wholesaler_name}")
    lines.append(f"\u2022 Premium financed: {_money(premium_cents)}")
    lines.append("")
    lines.append("Agreement link:")
    lines.append(program_url)
    lines.append("")
    if save_only:
        lines.append(
            "This is saved only -- nothing has been e-signed, nothing was sent "
            "to the insured, and nothing was uploaded."
        )
        lines.append("")
    if verification_summary:
        lines.append(verification_summary)
        lines.append("")
    lines.append("Best,")
    lines.append("Robie AI")
    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Daily reconciler
# ---------------------------------------------------------------------------

def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def reconcile_pfa_completions(
    client: Any,
    outbox: PfaNotificationOutbox,
    sender: NotificationSender,
    alerter: Callable[[str], None],
    lookback_days: int = 7,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Daily sweep: every ready_for_checkout program in the window must have
    a CONFIRMED notification.

    - Pending outbox record  -> re-fire the send; alert on recovery/failure.
    - No record at all (manual creation outside the workflow) -> fail closed:
      alert staff with everything needed to verify + notify by hand. No link
      is ever auto-sent for a program that was never verified against the
      original request.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    stats: dict[str, Any] = {
        "checked": 0, "already_notified": 0, "recovered": 0,
        "alerted_unverified": 0, "errors": [],
    }
    try:
        resp = client.transport.request("GET", "/programs", query={"page_size": 50})
    except Exception as exc:
        stats["errors"].append(f"program scan failed: {exc}")
        return stats
    programs = resp.get("data", []) if isinstance(resp, dict) else []
    for program in programs:
        if not isinstance(program, dict):
            continue
        if program.get("status") != READY_FOR_CHECKOUT:
            continue
        created = _parse_dt(program.get("created_at"))
        if created is not None and created < cutoff:
            continue
        program_id = str(program.get("id") or "")
        if not program_id:
            continue
        stats["checked"] += 1
        if outbox.is_notified(program_id):
            stats["already_notified"] += 1
            continue
        try:
            if sender.find_sent_for_program(program_id, since_days=lookback_days):
                outbox.adopt_sent(program_id)
                stats["already_notified"] += 1
                continue
        except Exception as exc:
            logger.warning("Sent search failed for %s: %s", program_id, exc)
        pending = outbox.get_record(KIND_COMPLETION, program_id)
        insured = program.get("insured") or {}
        insured_name = (
            insured.get("business_name") if isinstance(insured, dict) else ""
        ) or "(unknown insured)"
        if pending and pending["status"] == STATUS_PENDING:
            # Re-fire the recorded notification.
            drain_stats = drain_outbox(outbox, sender)
            if outbox.is_notified(program_id):
                stats["recovered"] += 1
                alerter(
                    f"PFA completion recovered: the agreement for {insured_name} "
                    f"(program {program_id}) is ready and the notification email "
                    f"has now been sent and confirmed. (Drain: {drain_stats})"
                )
            else:
                stats["errors"].append(f"re-fire failed for program {program_id}")
                alerter(
                    f"PFA completion STILL failing: the agreement for {insured_name} "
                    f"(program {program_id}) is ready but the notification email "
                    f"could not be confirmed. Please notify the requester by hand."
                )
            continue
        # No outbox record: created outside the workflow (e.g. manual API
        # call). Fail closed -- alert staff with everything needed; do NOT
        # auto-send a link for an unverified program.
        producer = program.get("producer") or {}
        producer_email = producer.get("email") if isinstance(producer, dict) else ""
        program_url = program.get("program_url") or (
            "https://checkout.useascend.com/streetsmart_insurance_agency"
            f"/overview?program_id={program_id}"
        )
        stats["alerted_unverified"] += 1
        alerter(
            "PFA agreement ready but NEVER verified or notified: "
            f"{insured_name} (program {program_id}). It was created outside the "
            "workflow, so its details were not checked against the original "
            "request and no notification was sent. "
            f"Producer on file: {producer_email or 'unknown'}. "
            "Please verify the program against the request email, then send "
            f"the requester the link by hand: {program_url}"
        )
    return stats


# ---------------------------------------------------------------------------
# 4. HITL-stuck email
# ---------------------------------------------------------------------------

def stuck_threshold_exceeded(
    first_asked_at_iso: str,
    now_iso: str,
    threshold_hours: float = 24.0,
) -> bool:
    first = _parse_dt(first_asked_at_iso)
    now = _parse_dt(now_iso)
    if first is None or now is None:
        return False
    return (now - first) >= timedelta(hours=threshold_hours)


def compose_hitl_stuck_email(
    *,
    requester_name: str,
    insured_name: str,
    missing_items: list[str],
    waiting_since_display: str,
) -> tuple[str, str]:
    """Plain-English 'here's what's holding up your request' note."""
    greeting = requester_name or "Team"
    subject = f"Still need one thing: finance agreement for {insured_name}"
    lines = [
        f"Hi {greeting},",
        "",
        f"Your finance agreement request for {insured_name} (sent {waiting_since_display}) "
        "is still waiting on my end -- I haven't created the agreement yet, and "
        "I wanted you to hear that from me instead of silence.",
        "",
        "Here's what's holding it up:",
    ]
    for item in missing_items:
        lines.append(f"\u2022 {item}")
    lines.extend([
        "",
        "Just reply directly to this email with the missing piece(s) and I'll "
        "pick it right back up.",
        "",
        "Best,",
        "Robie AI",
    ])
    return subject, "\n".join(lines)


def enqueue_hitl_stuck_notification(
    outbox: PfaNotificationOutbox,
    *,
    dedupe_key: str,
    requester_email: str,
    requester_name: str,
    insured_name: str,
    missing_items: list[str],
    waiting_since_display: str,
) -> int:
    """Enqueue the stuck note through the same durable outbox, so it carries
    the same retry-until-confirmed guarantee as completion emails."""
    subject, body = compose_hitl_stuck_email(
        requester_name=requester_name,
        insured_name=insured_name,
        missing_items=missing_items,
        waiting_since_display=waiting_since_display,
    )
    return outbox.enqueue(
        kind=KIND_HITL_STUCK,
        program_id=f"hitl-stuck:{dedupe_key}",
        requester_email=requester_email,
        requester_name=requester_name,
        subject=subject,
        body=body,
    )


def notify_stuck_requests(
    outbox: PfaNotificationOutbox,
    sender: NotificationSender,
    stuck_requests: list[dict[str, Any]],
    threshold_hours: float = 24.0,
    now_iso: Optional[str] = None,
) -> dict[str, int]:
    """Enqueue + send stuck notes for requests waiting past the threshold.

    Each stuck_requests entry: dedupe_key, requester_email, requester_name,
    insured_name, missing_items (list[str]), first_asked_at (ISO),
    waiting_since_display.
    """
    now_iso = now_iso or _utcnow_iso()
    stats = {"notified": 0, "already_notified": 0, "not_yet_due": 0}
    for req in stuck_requests:
        if not stuck_threshold_exceeded(
            str(req.get("first_asked_at") or ""), now_iso, threshold_hours
        ):
            stats["not_yet_due"] += 1
            continue
        program_key = f"hitl-stuck:{req.get('dedupe_key')}"
        if outbox.is_notified(program_key, kind=KIND_HITL_STUCK):
            stats["already_notified"] += 1
            continue
        enqueue_hitl_stuck_notification(
            outbox,
            dedupe_key=str(req.get("dedupe_key") or ""),
            requester_email=str(req.get("requester_email") or ""),
            requester_name=str(req.get("requester_name") or ""),
            insured_name=str(req.get("insured_name") or "your client"),
            missing_items=list(req.get("missing_items") or []),
            waiting_since_display=str(req.get("waiting_since_display") or "recently"),
        )
        stats["notified"] += 1
    drain_outbox(outbox, sender)
    return stats
