"""Ascend API poller that files #762 notice notes before the email driver.

The email driver stays the fallback for underwriting, return premium,
failed-payment detail, refund reissue/stop, and any event this poller did
not file. This module only GETs Ascend. It does not send client or carrier
email. Dry-run is the default. Live filing requires
``ASCEND_API_SOURCE_LIVE=1``.

Webhook delivery is not wired. :func:`events_from_webhook` is the seam a
later receiver should call for ``invoice.*`` and ``payout.*``.

Prod dry-run (after this release is the live tree; do not set the live flag)::

    sudo systemctl start robie-ascend-api-notice.service

or, against the current tree. Start in ``/`` so a stray
``/tmp/robie_job_engine`` cannot shadow imports. ``ROBIE_PLAYGROUND=1``
stays: ``ROBIE_EZLYNX_WRITE_SCOPE=all`` is ignored unless Playground
guardrails are active, and without it the dry-run falls back to test
applicant ``220250093``. The flag does not turn on live filing. Live
filing is still ``ASCEND_API_SOURCE_LIVE=1``. ``ROBIE_ASCEND_API_ENABLED``,
``ROBIE_ASCEND_API_BASE_URL``, and ``ROBIE_ASCEND_API_PRODUCTION_ENABLED``
only let the GET client start; they do not file notes. The interpreter
is the shared venv. The ``.hermes`` tree is mode 0700 and owned by carlo,
so ``User=streetsmart-hermes`` fails with 203/EXEC on that path. Dry-run
state goes to ``data/ascend-api/ascend_api_notice_events.dry-run.db``, not
the live store the email driver reads. The email driver on Prod runs as
carlo. A missing live store means the email still files::

    cd /
    sudo env PYTHONPATH=/opt/streetsmart-hermes/current \\
      ROBIE_ENV=PRODUCTION \\
      ROBIE_EZLYNX_API_PROD_SECRET=projects/751771086524/secrets/ezlynx-api-prod/versions/latest \\
      ROBIE_ASCEND_API_KEY_SECRET=projects/751771086524/secrets/ascend-prod-api-key/versions/latest \\
      ROBIE_ASCEND_API_ENABLED=1 \\
      ROBIE_ASCEND_API_BASE_URL=https://api.useascend.com \\
      ROBIE_ASCEND_API_PRODUCTION_ENABLED=1 \\
      ROBIE_EZLYNX_WRITE_SCOPE=all \\
      ROBIE_PLAYGROUND=1 \\
      ASCEND_API_NOTICE_DRY_RUN_DB=/opt/streetsmart-hermes/robie-job-engine/data/ascend-api/ascend_api_notice_events.dry-run.db \\
      /opt/streetsmart-hermes/venv/bin/python \\
      -m robie_job_engine.ascend_api_notice_source
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from urllib import parse as urlparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import ascend_notice_triage as triage
from .secrets import redact_text

logger = logging.getLogger(__name__)

LIVE_ENV = "ASCEND_API_SOURCE_LIVE"
# Ready-to-file rows the digest has matched. One poll files at most this many.
READY_FILE_LIMIT = 25
# A ready row that fails this many times leaves the queue for a person to file.
FILE_ATTEMPT_LIMIT = 5
REMITTANCE_APPLICANT_ENV = "ASCEND_API_REMITTANCE_APPLICANT_ID"
DB_ENV = "ASCEND_API_NOTICE_DB"
DRY_RUN_DB_ENV = "ASCEND_API_NOTICE_DRY_RUN_DB"
LOOKBACK_ENV = "ASCEND_API_SOURCE_LOOKBACK_MINUTES"

STORE_DIR_NAME = "ascend-api"
DEFAULT_DB_PATH = Path(
    "/opt/streetsmart-hermes/robie-job-engine/data/ascend-api/ascend_api_notice_events.db"
)
DEFAULT_DRY_RUN_DB_PATH = Path(
    "/opt/streetsmart-hermes/robie-job-engine/data/ascend-api/ascend_api_notice_events.dry-run.db"
)
# The dedicated folder is readable by carlo and by streetsmart-hermes.
# The shared data directory is not touched. The file is not world-writable.
STORE_FILE_MODE = 0o644
STORE_DIR_MODE = 0o755
DEFAULT_LOOKBACK_MINUTES = 20
CURSOR_OVERLAP_MINUTES = 5
PAGE_SIZE = 25
MAX_PAGES = 40
STALL_RUNS = 4
AGENCY_PROGRAM = "agency"

FEED_PROGRAMS = "/v1/programs"
FEED_LOANS = "/v1/loans"
FEED_INVOICES = "/v1/invoices"
FEED_PAYOUTS = "/v1/payouts"
FEED_BILLABLES = "/v1/billables"
FEEDS = (FEED_PROGRAMS, FEED_LOANS, FEED_INVOICES, FEED_PAYOUTS)

# Names a later webhook receiver can pass in. Processing and voided are
# recognized and intentionally not filed: processing has no durable poll
# record, and voided is the cancellation side effect the loan poll already
# covers. Refund webhooks stay with email.
WEBHOOK_INVOICE_PAID = "invoice.paid"
WEBHOOK_INVOICE_OVERDUE = "invoice.marked_overdue"
WEBHOOK_INVOICE_CREATED = "invoice.created"
WEBHOOK_INVOICE_PROCESSING = "invoice.processing_payment"
WEBHOOK_INVOICE_VOIDED = "invoice.voided"
WEBHOOK_PAYOUT_PAYING = "payout.paying"
WEBHOOK_PAYOUT_PAID = "payout.paid"
RECOGNIZED_WEBHOOKS = frozenset(
    {
        WEBHOOK_INVOICE_PAID,
        WEBHOOK_INVOICE_OVERDUE,
        WEBHOOK_INVOICE_CREATED,
        WEBHOOK_INVOICE_PROCESSING,
        WEBHOOK_INVOICE_VOIDED,
        WEBHOOK_PAYOUT_PAYING,
        WEBHOOK_PAYOUT_PAID,
        "refund.paid",
        "refund.cancelled",
    }
)
WEBHOOKS_LEFT_TO_EMAIL = frozenset(
    {
        WEBHOOK_INVOICE_PROCESSING,
        WEBHOOK_INVOICE_VOIDED,
        "refund.paid",
        "refund.cancelled",
    }
)

# Email notice types this poller can already have filed. Failed-payment
# mail is late_payment in the email classifier and is excluded in
# email_covered_by_api, not here.
API_OWNED_EMAIL_TYPES = frozenset(
    {
        triage.LATE_PAYMENT,
        triage.INTENT_TO_CANCEL,
        triage.CANCELLATION,
        triage.REINSTATEMENT,
        triage.PAID_OFF,
        triage.PAYMENT_CONFIRMATION,
        triage.DISPUTED_CHARGE,
    }
)

_CANCELLED_PROGRAM = frozenset({"cancelled", "canceled"})
_CANCELED_LOAN = frozenset({"cancelled", "canceled"})
_PAYOUT_TYPES = frozenset({"commission", "full_premium", "overpayment"})
_SETTLEMENT_RE = re.compile(r"settlement\s+for\s+invoice\s+no\.?", re.IGNORECASE)
_DISPUTE_LINE_RE = re.compile(r"disputed\s+amount|dispute\s+fee", re.IGNORECASE)
_INVOICE_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

WINDOW = timedelta(hours=36)
# Past-due mail arrives long after the due date. The window is anchored
# to the program's overdue-flip time, and it has to cover the first email
# (observed 36h to 86h after the due date, which is inside 96h of the flip).
PAST_DUE_WINDOW = timedelta(hours=96)
SKEW = timedelta(hours=2)
_LOAN_URL_RE = re.compile(
    r"dashboard\.useascend\.com/loans/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
_FILED_SELECT = (
    "event_key, program_id, event_type, anchor, occurred_at, invoice_id, "
    "insured_name, loan_id"
)
_FILED_SELECT_BASE = (
    "event_key, program_id, event_type, anchor, occurred_at, invoice_id"
)


def live_enabled() -> bool:
    """True only for the exact env value ``1``. A CLI flag cannot turn this on."""
    return str(os.environ.get(LIVE_ENV) or "").strip() == "1"


def remittance_applicant_id() -> str:
    return str(os.environ.get(REMITTANCE_APPLICANT_ENV) or "").strip()


class ApiNoticeStoreUnavailable(RuntimeError):
    """Raised only by callers that still want a hard failure.

    The email driver does not use this. A missing or unreadable live store
    means the email files, because email is the source until the API is live.
    """


def live_db_path() -> Path:
    """Where live filings are recorded. The email driver reads this file."""
    override = str(os.environ.get(DB_ENV) or "").strip()
    if override:
        return Path(override)
    return DEFAULT_DB_PATH


def dry_run_db_path() -> Path:
    """Dry-run episodes and stall history. Never the live filing store."""
    override = str(os.environ.get(DRY_RUN_DB_ENV) or "").strip()
    if override:
        return Path(override)
    return DEFAULT_DRY_RUN_DB_PATH


def db_path() -> Path:
    """Live runs and dry-runs do not share a state file.

    A dry-run records loan episodes and the stall streak. Sharing the live
    file would let that dry-run reset the live stall counter and invent a
    reinstatement the live poller had not seen. Filed keys exist only on
    the live path, which is what the email driver reads.
    """
    return live_db_path() if live_enabled() else dry_run_db_path()


def publish_store_permissions(path: Path) -> None:
    """Mode 0644 on the sqlite file, and 0755 only on ``ascend-api``.

    The parent of that folder is the shared data directory. Other services
    keep their own mode there. This function does not chown anything.
    """
    try:
        if path.is_file():
            os.chmod(path, STORE_FILE_MODE)
        parent = path.parent
        if parent.name == STORE_DIR_NAME and parent.is_dir():
            os.chmod(parent, STORE_DIR_MODE)
    except OSError as exc:
        logger.warning(
            "could not publish notice store permissions: %s", type(exc).__name__
        )


def _create_store_directory(path: Path) -> None:
    """Create missing directories. Do not change the mode of one that exists."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)


def make_event_key(program_id: str, event_type: str, anchor: str) -> str:
    """Stable id: program id + event type + event timestamp or invoice id."""
    return "|".join(
        (
            str(program_id or "").strip().lower(),
            str(event_type or "").strip().lower(),
            normalize_anchor(anchor),
        )
    )


def normalize_anchor(value: str) -> str:
    text = str(value or "").strip()
    if "T" not in text and not text.endswith("Z"):
        return text
    parsed = parse_time(text)
    if parsed is None:
        return text
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        raw = int(text)
        if raw > 10_000_000_000:
            raw = raw / 1000.0
        return datetime.fromtimestamp(raw, timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status(record: dict[str, Any]) -> str:
    return str(record.get("status") or "").strip().lower()


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or "").strip()


def _program_id_of(record: dict[str, Any]) -> str:
    direct = str(record.get("program_id") or "").strip()
    if direct:
        return direct
    program = record.get("program")
    if isinstance(program, dict):
        return str(program.get("id") or "").strip()
    return ""


def cents_to_money(cents: Any) -> str | None:
    if cents is None or isinstance(cents, bool):
        return None
    try:
        value = int(cents)
    except (TypeError, ValueError):
        return None
    return f"${value / 100:,.2f}"


def _us_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" not in text and not text.endswith("Z"):
        parsed = parse_time(text) if "-" in text else None
        if parsed is None:
            return text
        return parsed.strftime("%m/%d/%Y")
    parsed = parse_time(text)
    if parsed is None:
        return ""
    try:
        from zoneinfo import ZoneInfo

        parsed = parsed.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - UTC date is still a date
        pass
    return parsed.strftime("%m/%d/%Y")


def insured_name_of(program: dict[str, Any] | None, fallback: str = "") -> str:
    record = program if isinstance(program, dict) else {}
    insured = record.get("insured") if isinstance(record.get("insured"), dict) else {}
    business = str(insured.get("business_name") or "").strip()
    if business:
        return business
    person = " ".join(
        part
        for part in (
            str(insured.get("first_name") or "").strip(),
            str(insured.get("last_name") or "").strip(),
        )
        if part
    ).strip()
    if person:
        return person
    for key in ("payer_name", "payee", "insured_name"):
        text = str(record.get(key) or fallback or "").strip()
        if text:
            return text
    return str(fallback or "").strip() or "Fixture Insured"


def policy_numbers_of(*records: dict[str, Any] | None) -> list[str]:
    """Carrier policy numbers only. A billable identifier is not a policy number."""
    found: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        value = str(record.get("policy_number") or "").strip()
        if value and value not in found:
            found.append(value)
        for container in ("billables", "quotes"):
            rows = record.get(container)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                nested = str(row.get("policy_number") or "").strip()
                if nested and nested not in found:
                    found.append(nested)
    return found


def insured_phone_of(*records: dict[str, Any] | None) -> str:
    """Insured phone from a program, loan, or invoice. Empty when absent."""
    keys = ("phone", "business_phone", "mobile", "mobile_phone", "phone_number")
    record_keys = ("payer_phone", "insured_phone", "phone", "mobile", "phone_number")
    for record in records:
        if not isinstance(record, dict):
            continue
        insured = record.get("insured") if isinstance(record.get("insured"), dict) else {}
        candidates = [insured.get(key) for key in keys]
        candidates.extend(record.get(key) for key in record_keys)
        for value in candidates:
            text = str(value or "").strip()
            digits = re.sub(r"\D", "", text)
            if len(digits) >= 10:
                return text
    return ""


def insured_email_of(*records: dict[str, Any] | None) -> str:
    """Insured email from a program or invoice. Empty when it is not an address."""
    candidates: list[Any] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        insured = record.get("insured") if isinstance(record.get("insured"), dict) else {}
        candidates.extend(insured.get(key) for key in ("email", "business_email"))
        candidates.extend(record.get(key) for key in ("payer_email", "insured_email", "email"))
    for value in candidates:
        text = str(value or "").strip()
        if "@" in text and " " not in text:
            return text
    return ""


def _line_labels(invoice: dict[str, Any]) -> list[str]:
    rows = (
        invoice.get("invoice_items")
        or invoice.get("items")
        or invoice.get("line_items")
        or []
    )
    labels: list[str] = []
    if not isinstance(rows, list):
        return labels
    for row in rows:
        if isinstance(row, dict):
            labels.append(
                " ".join(
                    str(row.get(key) or "")
                    for key in ("title", "name", "description", "memo")
                )
            )
        else:
            labels.append(str(row or ""))
    return labels


def is_dispute_invoice(invoice: dict[str, Any]) -> bool:
    """Settlement invoice whose lines are a disputed amount or a dispute fee."""
    blob = " ".join(
        str(invoice.get(key) or "")
        for key in ("memo", "title", "description", "invoice_number", "number")
    )
    if not _SETTLEMENT_RE.search(blob):
        return False
    return any(_DISPUTE_LINE_RE.search(label) for label in _line_labels(invoice))


def _dashboard(program_id: str) -> str:
    return f"https://dashboard.useascend.com/programs/{program_id}"


def invoice_anchor(invoice: dict[str, Any]) -> tuple[str, str]:
    """Human invoice number when the API has one, otherwise the invoice UUID.

    Past-due emails print ``Invoice No.`` as the human number. Keying the
    API event on the UUID meant the email driver never saw a match.
    The second value is the UUID, kept so a body that only has the UUID
    still matches.
    """
    invoice_id = _record_id(invoice)
    number = str(invoice.get("invoice_number") or invoice.get("number") or "").strip()
    if number:
        return number, invoice_id
    return invoice_id, invoice_id


@dataclass
class ApiNotice:
    """One filing the poller wants the #762 write path to make."""

    event_key: str
    event_type: str
    program_id: str
    anchor: str
    occurred_at: str
    invoice_id: str = ""
    policy_numbers: tuple[str, ...] = ()
    insured_name: str = ""
    program: dict[str, Any] = field(default_factory=dict)
    subject: str = ""
    body: str = ""
    alias_keys: tuple[str, ...] = ()
    remittance: bool = False
    amount_cents: int | None = None

    def as_email(self) -> Any:
        from .ascend_notice_driver import EmailNotice

        return EmailNotice(
            message_id=f"api:{self.event_key}",
            subject=self.subject,
            body=self.body,
            internal_date=self.occurred_at,
        )


def _notice(
    *,
    event_type: str,
    program_id: str,
    anchor: str,
    occurred_at: str,
    subject: str,
    body: str,
    program: dict[str, Any] | None = None,
    policy_numbers: list[str] | None = None,
    insured_name: str = "",
    invoice_id: str = "",
    amount_cents: int | None = None,
    remittance: bool = False,
    alias_keys: tuple[str, ...] = (),
) -> ApiNotice | None:
    program_key = str(program_id or "").strip()
    anchor_text = str(anchor or "").strip()
    if not program_key or not anchor_text or not event_type:
        return None
    stamp = normalize_anchor(occurred_at) or normalize_anchor(anchor_text)
    return ApiNotice(
        event_key=make_event_key(program_key, event_type, anchor_text),
        event_type=event_type,
        program_id=program_key,
        anchor=normalize_anchor(anchor_text),
        occurred_at=stamp,
        invoice_id=str(invoice_id or "").strip(),
        policy_numbers=tuple(policy_numbers or ()),
        insured_name=insured_name,
        program=dict(program or {}),
        subject=subject,
        body=body,
        alias_keys=alias_keys,
        remittance=remittance,
        amount_cents=amount_cents,
    )


def _render_common(
    *,
    program_id: str,
    insured: str,
    policy_numbers: list[str],
    extra_lines: list[str],
    insured_email: str = "",
    insured_phone: str = "",
) -> str:
    lines = list(extra_lines)
    for number in policy_numbers:
        lines.append("Policy ID " + number)
    if insured:
        lines.append(f"Customer {insured}")
    if insured_email:
        lines.append(f"Email {insured_email}")
    if insured_phone:
        lines.append(f"Phone {insured_phone}")
    if program_id and program_id != AGENCY_PROGRAM:
        lines.append(_dashboard(program_id))
    return "\n".join(line for line in lines if line)


def _ensure_filed_identity_columns(conn: sqlite3.Connection) -> None:
    """Add identity columns on a store created before program/loan dedupe."""
    have = {str(row[1]) for row in conn.execute("PRAGMA table_info(filed_events)")}
    if "insured_name" not in have:
        conn.execute(
            "ALTER TABLE filed_events ADD COLUMN insured_name TEXT NOT NULL DEFAULT ''"
        )
    if "loan_id" not in have:
        conn.execute(
            "ALTER TABLE filed_events ADD COLUMN loan_id TEXT NOT NULL DEFAULT ''"
        )


def _fetch_filed_rows(
    conn: sqlite3.Connection, where: str, params: tuple[str, ...]
) -> list[dict[str, str]]:
    """Read filed rows. A store from before the identity columns still reads."""
    try:
        fetched = conn.execute(
            f"SELECT {_FILED_SELECT} FROM filed_events WHERE {where}",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        fetched = conn.execute(
            f"SELECT {_FILED_SELECT_BASE} FROM filed_events WHERE {where}",
            params,
        ).fetchall()
    return [dict(row) for row in fetched]


def _stored_loan_id(notice: ApiNotice) -> str:
    program = notice.program if isinstance(notice.program, dict) else {}
    return str(program.get("loan_id") or "").strip()


class EventKeyStore:
    """Filed API keys, status episodes, the poll cursor, and the stall streak."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        _create_store_directory(self.path)
        self._init()
        publish_store_permissions(self.path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS filed_events (
                    event_key TEXT PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    anchor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    invoice_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    insured_name TEXT NOT NULL DEFAULT '',
                    loan_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_filed_program_type
                    ON filed_events(program_id, event_type);
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    status_holds TEXT NOT NULL,
                    anchor TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS poll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    live INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS cursors (
                    name TEXT PRIMARY KEY,
                    cursor_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS program_policies (
                    program_id TEXT PRIMARY KEY,
                    policy_numbers TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS first_seen (
                    episode_id TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL
                );
                """
            )
            _ensure_filed_identity_columns(conn)
            _ensure_poll_columns(conn)
            try:
                _ensure_unmatched_tables(conn)
            except sqlite3.Error as exc:
                # The poller unit must keep running when the accounting
                # tables cannot be added. CREATE IF NOT EXISTS is idempotent
                # and does not require the digest timer to be installed.
                logger.warning(
                    "unmatched notice tables were not created: %s",
                    type(exc).__name__,
                )

    def is_filed(self, event_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM filed_events WHERE event_key=?",
                (event_key,),
            ).fetchone()
        return bool(row) and str(row["status"]) == "filed"

    def record_filed(self, notice: ApiNotice, *, extra_keys: tuple[str, ...] = ()) -> None:
        moment = _iso(_now())
        keys = [notice.event_key, *notice.alias_keys, *extra_keys]
        with self._connect() as conn:
            for key in keys:
                if not key:
                    continue
                conn.execute(
                    """
                    INSERT INTO filed_events (
                        event_key, program_id, event_type, anchor, occurred_at,
                        invoice_id, status, recorded_at, insured_name, loan_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'filed', ?, ?, ?)
                    ON CONFLICT(event_key) DO UPDATE SET
                        status='filed',
                        occurred_at=excluded.occurred_at,
                        invoice_id=excluded.invoice_id,
                        recorded_at=excluded.recorded_at,
                        insured_name=excluded.insured_name,
                        loan_id=excluded.loan_id
                    """,
                    (
                        key,
                        notice.program_id.lower(),
                        notice.event_type,
                        notice.anchor,
                        notice.occurred_at,
                        notice.invoice_id,
                        moment,
                        str(notice.insured_name or "").strip(),
                        _stored_loan_id(notice),
                    ),
                )

    def filed_for(self, program_id: str, event_type: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            return _fetch_filed_rows(
                conn,
                "program_id=? AND event_type=? AND status='filed'",
                (str(program_id or "").strip().lower(), str(event_type or "").strip().lower()),
            )

    def filed_of_type(self, event_type: str) -> list[dict[str, str]]:
        """Every filed row of one type. Used when an email has no program id."""
        with self._connect() as conn:
            return _fetch_filed_rows(
                conn,
                "event_type=? AND status='filed'",
                (str(event_type or "").strip().lower(),),
            )

    def episode_status(self, episode_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status_holds FROM episodes WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
        if row is None:
            return ""
        return str(row["status_holds"] or "")

    def episode_anchor(
        self,
        episode_id: str,
        status: str,
        proposed: str,
        *,
        persist: bool,
    ) -> str:
        """Freeze the anchor while the status holds. A new status gets a new anchor."""
        proposed_text = normalize_anchor(proposed) or str(status or "").strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status_holds, anchor FROM episodes WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            if row is not None and str(row["status_holds"]) == status:
                return str(row["anchor"])
            if persist:
                conn.execute(
                    """
                    INSERT INTO episodes (episode_id, status_holds, anchor, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(episode_id) DO UPDATE SET
                        status_holds=excluded.status_holds,
                        anchor=excluded.anchor,
                        updated_at=excluded.updated_at
                    """,
                    (episode_id, status, proposed_text, _iso(_now())),
                )
        return proposed_text

    def cached_policy_numbers(self, program_id: str) -> list[str] | None:
        """Cached billable policy numbers. None means this program was not cached."""
        key = str(program_id or "").strip().lower()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT policy_numbers FROM program_policies WHERE program_id=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(str(row["policy_numbers"] or "[]"))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        return [str(item).strip() for item in parsed if str(item or "").strip()]

    def remember_policy_numbers(self, program_id: str, numbers: list[str]) -> None:
        key = str(program_id or "").strip().lower()
        cleaned = [str(item).strip() for item in numbers if str(item or "").strip()]
        if not key or not cleaned:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO program_policies (program_id, policy_numbers, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(program_id) DO UPDATE SET
                    policy_numbers=excluded.policy_numbers,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(cleaned), _iso(_now())),
            )

    def first_seen_at(self, episode_id: str, proposed: str) -> str:
        """The first time this invoice was seen. Later polls keep that time."""
        key = str(episode_id or "").strip()
        moment = str(proposed or "").strip()
        if not key or not moment:
            return moment
        with self._connect() as conn:
            row = conn.execute(
                "SELECT seen_at FROM first_seen WHERE episode_id=?",
                (key,),
            ).fetchone()
            if row is not None and str(row["seen_at"] or "").strip():
                return str(row["seen_at"])
            conn.execute(
                "INSERT INTO first_seen (episode_id, seen_at) VALUES (?, ?)",
                (key, moment),
            )
        return moment

    def cursor(self) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cursor_at FROM cursors WHERE name='poll'"
            ).fetchone()
        if row is None:
            return None
        return parse_time(row["cursor_at"])

    def advance_cursor(self, moment: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cursors (name, cursor_at) VALUES ('poll', ?)
                ON CONFLICT(name) DO UPDATE SET cursor_at=excluded.cursor_at
                """,
                (_iso(moment),),
            )

    def note_poll_result(
        self, *, ok: bool, error: str, started_at: str, live: bool = False
    ) -> int:
        """Record the run and return the consecutive-failure streak.

        ``live`` is whether this process was filing notes. The digest reads
        the newest row. It does not read ``ASCEND_API_SOURCE_LIVE`` itself.
        """
        cleaned = redact_text(str(error or ""))[:500]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO poll_runs (started_at, ok, error, live)
                VALUES (?, ?, ?, ?)
                """,
                (started_at, 1 if ok else 0, cleaned, 1 if live else 0),
            )
            rows = conn.execute(
                "SELECT ok FROM poll_runs ORDER BY id DESC LIMIT ?",
                (STALL_RUNS + 5,),
            ).fetchall()
        if ok:
            return 0
        streak = 0
        for row in rows:
            if int(row["ok"]):
                break
            streak += 1
        return streak

    def latest_poll_run(self) -> dict[str, Any] | None:
        """The newest poll record, including ones older than the digest window."""
        with self._connect() as conn:
            try:
                row = conn.execute(
                    """
                    SELECT id, started_at, ok, live
                    FROM poll_runs
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "started_at": str(row["started_at"] or ""),
            "ok": bool(row["ok"]),
            "live": bool(row["live"]),
        }

    def upsert_unmatched(
        self,
        notice: ApiNotice,
        *,
        reason: str,
        seen_at: str,
        suggestion_client: str = "",
        suggestion_policy: str = "",
        update_suggestion: bool = False,
    ) -> None:
        """Insert or refresh one unmatched notice. ``first_seen`` stays put.

        A later unmatched sight of a resolved key reopens it. Suggestion
        text is replaced only when ``update_suggestion`` is set, and only
        in this store. Nothing is written to EZLynx.
        """
        policies = [
            str(number).strip()
            for number in notice.policy_numbers
            if str(number or "").strip()
        ]
        flag = 1 if update_suggestion else 0
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO unmatched_notices (
                    event_key, insured_name, program_id, loan_id, policy_numbers,
                    notice_type, amount_cents, first_seen, last_seen, reason,
                    resolved_at, suggestion_client, suggestion_policy,
                    aged_out_notified_at, subject, body, program_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    insured_name=excluded.insured_name,
                    program_id=excluded.program_id,
                    loan_id=excluded.loan_id,
                    policy_numbers=excluded.policy_numbers,
                    notice_type=excluded.notice_type,
                    amount_cents=excluded.amount_cents,
                    last_seen=excluded.last_seen,
                    reason=excluded.reason,
                    resolved_at=NULL,
                    ready_at=NULL,
                    file_attempts=0,
                    last_attempt_at=NULL,
                    file_failure='',
                    subject=excluded.subject,
                    body=excluded.body,
                    program_json=excluded.program_json,
                    suggestion_client=CASE WHEN ? = 1 THEN excluded.suggestion_client
                        ELSE unmatched_notices.suggestion_client END,
                    suggestion_policy=CASE WHEN ? = 1 THEN excluded.suggestion_policy
                        ELSE unmatched_notices.suggestion_policy END,
                    aged_out_notified_at=CASE
                        WHEN unmatched_notices.resolved_at IS NOT NULL
                             AND unmatched_notices.resolved_at != ''
                        THEN NULL
                        ELSE unmatched_notices.aged_out_notified_at
                    END
                """,
                (
                    notice.event_key,
                    str(notice.insured_name or "").strip(),
                    notice.program_id,
                    _stored_loan_id(notice),
                    json.dumps(policies),
                    notice.event_type,
                    notice.amount_cents,
                    seen_at,
                    seen_at,
                    reason,
                    suggestion_client,
                    suggestion_policy,
                    str(notice.subject or ""),
                    str(notice.body or ""),
                    json.dumps(notice.program or {}, default=str),
                    flag,
                    flag,
                ),
            )

    def resolve_unmatched(self, event_key: str, resolved_at: str) -> None:
        """Mark one notice resolved. A missing row is left alone."""
        key = str(event_key or "").strip()
        moment = str(resolved_at or "").strip()
        if not key or not moment:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE unmatched_notices
                SET resolved_at=?
                WHERE event_key=? AND (resolved_at IS NULL OR resolved_at='')
                """,
                (moment, key),
            )

    def list_unmatched(self) -> list[dict[str, Any]]:
        """Every unmatched row, including resolved ones. Empty if the table is new."""
        with self._connect() as conn:
            try:
                fetched = conn.execute(
                    "SELECT * FROM unmatched_notices ORDER BY first_seen, event_key"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [_unmatched_row(row) for row in fetched]

    def mark_ready_to_file(self, event_key: str, ready_at: str) -> None:
        """Remember that one client matched. Does not file and does not resolve."""
        key = str(event_key or "").strip()
        moment = str(ready_at or "").strip()
        if not key or not moment:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE unmatched_notices
                SET ready_at=?
                WHERE event_key=? AND (resolved_at IS NULL OR resolved_at='')
                """,
                (moment, key),
            )

    def list_ready_to_file(self, limit: int = READY_FILE_LIMIT) -> list[dict[str, Any]]:
        """Open rows the digest matched. Never-tried rows come first.

        A row that has already failed sorts after every row that has not
        been tried, then by the oldest attempt, then by ``ready_at``. After
        ``FILE_ATTEMPT_LIMIT`` failures the row stays on the email and is
        not picked again. At most ``limit`` rows.
        """
        cap = max(0, int(limit))
        if cap == 0:
            return []
        with self._connect() as conn:
            try:
                fetched = conn.execute(
                    """
                    SELECT * FROM unmatched_notices
                    WHERE (resolved_at IS NULL OR resolved_at='')
                      AND ready_at IS NOT NULL AND ready_at != ''
                      AND subject != '' AND body != ''
                      AND COALESCE(file_attempts, 0) < ?
                    ORDER BY CASE
                        WHEN last_attempt_at IS NULL OR last_attempt_at = '' THEN 0
                        ELSE 1
                    END,
                    last_attempt_at,
                    ready_at,
                    event_key
                    LIMIT ?
                    """,
                    (FILE_ATTEMPT_LIMIT, cap),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [_unmatched_row(row) for row in fetched]

    def note_ready_attempt(self, event_key: str, attempted_at: str, plain_reason: str) -> int:
        """Count one failed filing. Returns the new attempt count, or 0 if the row is gone."""
        key = str(event_key or "").strip()
        moment = str(attempted_at or "").strip()
        reason = str(plain_reason or "").strip() or "The note was not filed."
        if not key or not moment:
            return 0
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE unmatched_notices
                SET file_attempts = COALESCE(file_attempts, 0) + 1,
                    last_attempt_at = ?,
                    file_failure = ?
                WHERE event_key = ?
                  AND (resolved_at IS NULL OR resolved_at = '')
                """,
                (moment, reason, key),
            )
            row = conn.execute(
                "SELECT file_attempts FROM unmatched_notices WHERE event_key=?",
                (key,),
            ).fetchone()
        if row is None or row["file_attempts"] is None:
            return 0
        return int(row["file_attempts"])

    def refresh_unmatched_policy(
        self,
        event_key: str,
        policy_numbers: list[str],
        body: str,
        program_json: str,
    ) -> None:
        """Store the policy number just read from Ascend. Does not file or resolve."""
        key = str(event_key or "").strip()
        if not key:
            return
        numbers = [str(number).strip() for number in policy_numbers if str(number or "").strip()]
        if not numbers:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE unmatched_notices
                SET policy_numbers=?, body=?, program_json=?
                WHERE event_key=? AND (resolved_at IS NULL OR resolved_at='')
                """,
                (json.dumps(numbers), str(body or ""), str(program_json or "{}"), key),
            )

    def mark_aged_out_notified(self, event_keys: list[str], notified_at: str) -> None:
        moment = str(notified_at or "").strip()
        if not moment:
            return
        with self._connect() as conn:
            for key in event_keys:
                cleaned = str(key or "").strip()
                if not cleaned:
                    continue
                conn.execute(
                    """
                    UPDATE unmatched_notices
                    SET aged_out_notified_at=?
                    WHERE event_key=?
                    """,
                    (moment, cleaned),
                )

    def set_unmatched_suggestion(
        self, event_key: str, client_name: str, policy_number: str
    ) -> None:
        """Store suggestion text on an open row. Does not write to EZLynx."""
        key = str(event_key or "").strip()
        if not key:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE unmatched_notices
                SET suggestion_client=?, suggestion_policy=?
                WHERE event_key=? AND (resolved_at IS NULL OR resolved_at='')
                """,
                (str(client_name or "").strip(), str(policy_number or "").strip(), key),
            )

    def save_policy_index(
        self,
        rows: list[dict[str, str]],
        *,
        built_at: str,
        calls: int,
        total_size: int | None,
        pages: int,
    ) -> None:
        """Replace the policy-number index. Callers pass only a complete read."""
        with self._connect() as conn:
            conn.execute("DELETE FROM ezlynx_policy_index")
            for row in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ezlynx_policy_index (
                        policy_key, policy_number, applicant_name, applicant_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(row.get("policy_key") or ""),
                        str(row.get("policy_number") or ""),
                        str(row.get("applicant_name") or ""),
                        str(row.get("applicant_id") or ""),
                    ),
                )
            conn.execute(
                """
                INSERT INTO ezlynx_policy_index_meta (
                    name, built_at, calls, row_count, total_size, complete, pages
                ) VALUES ('book', ?, ?, ?, ?, 1, ?)
                ON CONFLICT(name) DO UPDATE SET
                    built_at=excluded.built_at,
                    calls=excluded.calls,
                    row_count=excluded.row_count,
                    total_size=excluded.total_size,
                    complete=1,
                    pages=excluded.pages
                """,
                (built_at, int(calls), len(rows), total_size, int(pages)),
            )

    def policy_index(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The last complete index. An incomplete or missing index returns no rows."""
        with self._connect() as conn:
            try:
                meta_row = conn.execute(
                    "SELECT * FROM ezlynx_policy_index_meta WHERE name='book'"
                ).fetchone()
            except sqlite3.OperationalError:
                return [], {}
            if meta_row is None or int(meta_row["complete"] or 0) != 1:
                return [], dict(meta_row) if meta_row is not None else {}
            fetched = conn.execute(
                """
                SELECT policy_key, policy_number, applicant_name, applicant_id
                FROM ezlynx_policy_index
                """
            ).fetchall()
        return [dict(row) for row in fetched], dict(meta_row)


def _ensure_unmatched_tables(conn: sqlite3.Connection) -> None:
    """Add the accounting tables. Safe to run on a database that already has them."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS unmatched_notices (
            event_key TEXT PRIMARY KEY,
            insured_name TEXT NOT NULL DEFAULT '',
            program_id TEXT NOT NULL DEFAULT '',
            loan_id TEXT NOT NULL DEFAULT '',
            policy_numbers TEXT NOT NULL DEFAULT '[]',
            notice_type TEXT NOT NULL DEFAULT '',
            amount_cents INTEGER,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            reason TEXT NOT NULL,
            resolved_at TEXT,
            suggestion_client TEXT NOT NULL DEFAULT '',
            suggestion_policy TEXT NOT NULL DEFAULT '',
            aged_out_notified_at TEXT,
            ready_at TEXT,
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            program_json TEXT NOT NULL DEFAULT '{}',
            file_attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            file_failure TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS ezlynx_policy_index (
            policy_key TEXT NOT NULL,
            policy_number TEXT NOT NULL,
            applicant_name TEXT NOT NULL DEFAULT '',
            applicant_id TEXT NOT NULL,
            PRIMARY KEY (policy_key, applicant_id)
        );
        CREATE TABLE IF NOT EXISTS ezlynx_policy_index_meta (
            name TEXT PRIMARY KEY,
            built_at TEXT NOT NULL,
            calls INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            total_size INTEGER,
            complete INTEGER NOT NULL,
            pages INTEGER NOT NULL
        );
        """
    )
    _ensure_unmatched_columns(conn)


def _ensure_unmatched_columns(conn: sqlite3.Connection) -> None:
    """Add filing columns on a store created before ready-to-file existed."""
    have = {str(row[1]) for row in conn.execute("PRAGMA table_info(unmatched_notices)")}
    additions = (
        ("ready_at", "TEXT"),
        ("subject", "TEXT NOT NULL DEFAULT ''"),
        ("body", "TEXT NOT NULL DEFAULT ''"),
        ("program_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("file_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_attempt_at", "TEXT"),
        ("file_failure", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, ddl in additions:
        if name not in have:
            conn.execute(f"ALTER TABLE unmatched_notices ADD COLUMN {name} {ddl}")


def _ensure_poll_columns(conn: sqlite3.Connection) -> None:
    """Remember whether a poll was filing. Old stores gain the column once."""
    have = {str(row[1]) for row in conn.execute("PRAGMA table_info(poll_runs)")}
    if "live" not in have:
        conn.execute("ALTER TABLE poll_runs ADD COLUMN live INTEGER NOT NULL DEFAULT 0")


def plain_file_failure(reason: str) -> str:
    """One sentence for the accounting email. No field names and no code labels."""
    text = str(reason or "").strip().lower()
    if text.startswith("applicant_unresolved") or "policy_outcome" in text:
        return "The policy number still did not match one client."
    if text.startswith("write_scope_refused"):
        return "That client is outside the accounts Robie is allowed to write."
    if text.startswith("driver_gate_refused"):
        return "Robie is not allowed to write notes right now."
    if text.startswith("discussion_error"):
        return "The EZLynx discussion could not be read."
    if text.startswith("task_not") or "task_not_created" in text:
        return "The follow-up task was not created."
    return "The note was not filed."


def _already_in_ezlynx(outcome: dict[str, Any]) -> bool:
    """True when this same bill's note is already in EZLynx.

    A same-policy note from an earlier month is not this bill. That result
    is a ``recent_same_notice`` only when the due date, amount, or invoice
    number was confirmed.
    """
    if str(outcome.get("status") or "") == "done":
        return True
    reason = str(outcome.get("reason") or "")
    if reason == "api_already_filed" or reason.startswith("existing_note_duplicate"):
        return True
    detail = outcome.get("detail")
    if not isinstance(detail, dict):
        return False
    if reason.startswith("recent_same_notice"):
        return bool(detail.get("notice_anchor_matched"))
    if detail.get("existing_note_duplicate"):
        return True
    return False


def _poll_will_file(live: bool) -> bool:
    """True when this poll may file notes.

    ``ASCEND_API_SOURCE_LIVE=1`` is not enough. A lease that would refuse
    the write is recorded as not filing, so the accounting email does not
    say the notes are on.
    """
    if not live:
        return False
    try:
        from .ascend_notice_driver import _driver_gate_refusal

        refusal = _driver_gate_refusal()
    except Exception as exc:  # noqa: BLE001 - do not promise a filing we cannot check
        logger.warning("driver lease was not checked: %s", type(exc).__name__)
        return False
    return not bool(refusal)


def _unmatched_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    try:
        parsed = json.loads(item.get("policy_numbers") or "[]")
    except json.JSONDecodeError:
        parsed = []
    item["policy_numbers"] = (
        [str(number) for number in parsed if str(number or "").strip()]
        if isinstance(parsed, list)
        else []
    )
    if item.get("amount_cents") is not None:
        item["amount_cents"] = int(item["amount_cents"])
    return item


def _readonly_filed_rows(path: Path, program_id: str, event_type: str) -> list[dict[str, str]]:
    """Read filed rows. Do not create the path, the file, or any table.

    ``mode=ro`` plus ``query_only`` so a lookup cannot write a journal or
    migrate the schema. A missing, empty, or unreadable file is no rows.
    """
    if not path.is_file():
        return []
    quoted = urlparse.quote(path.resolve().as_posix())
    uri = f"file:{quoted}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        logger.warning("api notice store is not readable: %s", type(exc).__name__)
        return []
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return _fetch_filed_rows(
            conn,
            "program_id=? AND event_type=? AND status='filed'",
            (program_id, event_type),
        )
    except sqlite3.Error as exc:
        logger.warning("api notice store read failed: %s", type(exc).__name__)
        return []
    finally:
        conn.close()


def email_covered_by_api(
    *,
    program_id: str,
    notice_type: str,
    subject: str,
    body: str,
    internal_date: str = "",
    store: EventKeyStore | None = None,
) -> str:
    """Event key when the live API poller already filed this email.

    Empty means the email driver files it. A missing, unreadable, or empty
    live store is empty: email is the only source until the API is live.
    The dry-run database is never opened. Only rows with status ``filed``
    count. The read path does not create a folder, a file, or a table.
    """
    kind = str(notice_type or "").strip()
    if kind not in API_OWNED_EMAIL_TYPES:
        return ""
    if kind == triage.LATE_PAYMENT and triage._payment_failed(subject, body):
        return ""
    program = str(program_id or "").strip().lower()
    if not program and kind != triage.LATE_PAYMENT:
        return ""
    path = live_db_path()
    try:
        rows = _filed_rows_for_email(
            path,
            program,
            kind,
            subject=subject,
            body=body,
            store=store,
        )
    except Exception as exc:  # noqa: BLE001 - a bad store must not block email
        logger.warning("api notice dedupe lookup failed: %s", type(exc).__name__)
        return ""
    if not rows:
        return ""
    if kind == triage.LATE_PAYMENT:
        # No invoice number is required. Real past-due mail does not carry
        # "Invoice No."; the subject is "Past due payment for <insured>".
        return _past_due_email_key(
            rows,
            subject=subject,
            body=body,
            internal_date=internal_date,
            program=program,
        )
    return _timed_email_key(rows, body=body, internal_date=internal_date, program=program, window=WINDOW)


def _filed_rows_for_email(
    path: Path,
    program: str,
    kind: str,
    *,
    subject: str,
    body: str,
    store: EventKeyStore | None,
) -> list[dict[str, str]]:
    """Filed rows the email might be a duplicate of.

    A program id limits the lookup to that program. Past-due mail with no
    program id falls back to a loan id in the body or the insured name.
    A named program that has no filed row does not borrow another program.
    """
    if program:
        if store is not None:
            return store.filed_for(program, kind)
        return _readonly_filed_rows(path, program, kind)
    if kind != triage.LATE_PAYMENT:
        return []
    if store is not None:
        pool = store.filed_of_type(kind)
    else:
        pool = _readonly_filed_of_type(path, kind)
    return _past_due_identity_rows(pool, subject=subject, body=body)


def _readonly_filed_of_type(path: Path, event_type: str) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    quoted = urlparse.quote(path.resolve().as_posix())
    uri = f"file:{quoted}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        logger.warning("api notice store is not readable: %s", type(exc).__name__)
        return []
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return _fetch_filed_rows(
            conn,
            "event_type=? AND status='filed'",
            (event_type,),
        )
    except sqlite3.Error as exc:
        logger.warning("api notice store read failed: %s", type(exc).__name__)
        return []
    finally:
        conn.close()


def _loan_ids_in_text(body: str) -> set[str]:
    return {match.group(1).lower() for match in _LOAN_URL_RE.finditer(str(body or ""))}


def _past_due_identity_rows(
    rows: list[dict[str, str]], *, subject: str, body: str
) -> list[dict[str, str]]:
    """Rows whose loan id or insured name is on the email."""
    from .ascend_notice_driver import insured_names_match

    loans = _loan_ids_in_text(body)
    email_name = triage.extract_insured_name(subject, body) or ""
    found: list[dict[str, str]] = []
    for row in rows:
        loan_id = str(row.get("loan_id") or "").strip().lower()
        if loan_id and loan_id in loans:
            found.append(row)
            continue
        row_name = str(row.get("insured_name") or "").strip()
        if email_name and row_name and insured_names_match(email_name, row_name):
            found.append(row)
    return found


def _past_due_email_key(
    rows: list[dict[str, str]],
    *,
    subject: str,
    body: str,
    internal_date: str,
    program: str,
) -> str:
    """Skip when a filed past-due for this program or insured is near the flip.

    The window is at least 96h and is anchored to ``occurred_at``, which is
    the overdue-flip time. Invoice text is an extra hit, not a requirement.
    When the insured name is missing on either side, the program (or loan)
    match still skips: a missed dedupe files one note, not two.
    """
    if not rows:
        return ""
    needles = _email_anchors(body, program)
    for row in rows:
        invoice_id = str(row.get("invoice_id") or "")
        anchor = str(row.get("anchor") or "")
        if invoice_id and invoice_id.lower() in needles:
            return str(row["event_key"])
        if anchor and anchor.lower() in needles:
            return str(row["event_key"])
    # ``subject`` is "Past due payment for <insured>". Rows are already
    # limited to that program, or to the insured or loan when the email
    # had no program id. A missing name does not reopen the email.
    return _closest_in_window(rows, internal_date, PAST_DUE_WINDOW)


def _timed_email_key(
    rows: list[dict[str, str]],
    *,
    body: str,
    internal_date: str,
    program: str,
    window: timedelta,
) -> str:
    needles = _email_anchors(body, program)
    for row in rows:
        invoice_id = str(row.get("invoice_id") or "")
        anchor = str(row.get("anchor") or "")
        if invoice_id and invoice_id.lower() in needles:
            return str(row["event_key"])
        if anchor and anchor.lower() in needles:
            return str(row["event_key"])
    return _closest_in_window(rows, internal_date, window)


def _closest_in_window(
    rows: list[dict[str, str]], internal_date: str, window: timedelta
) -> str:
    email_at = parse_time(internal_date)
    if email_at is None:
        if len(rows) == 1:
            return str(rows[0]["event_key"])
        return ""
    closest: tuple[float, str] | None = None
    for row in rows:
        occurred = parse_time(row.get("occurred_at"))
        if occurred is None:
            continue
        delta = email_at - occurred
        if -SKEW <= delta <= window:
            score = abs(delta.total_seconds())
            key = str(row["event_key"])
            if closest is None or score < closest[0]:
                closest = (score, key)
    return closest[1] if closest else ""


def _email_anchors(body: str, program_id: str) -> set[str]:
    text = str(body or "")
    found = {match.group(0).lower() for match in _INVOICE_UUID_RE.finditer(text)}
    found.discard(program_id.lower())
    for match in re.finditer(r"Invoice(?:\s+No\.?)?\s+([A-Za-z0-9-]{4,})", text, re.IGNORECASE):
        found.add(match.group(1).strip().lower())
    return found


class GetOnlyClient:
    """Expose ``get`` and refuse every other verb."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", str(path), dict(query or {})))
        payload = self._inner.get(path, query)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Ascend API GET {path} returned a non-object")
        return payload

    def __getattr__(self, name: str) -> Any:
        if name.lower() in {"post", "put", "patch", "delete"}:
            raise RuntimeError(f"Ascend API notice source is GET only; refused {name}")
        raise AttributeError(name)


def list_updated_since(
    client: Any,
    path: str,
    since_iso: str,
    *,
    page_size: int = PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Page a list endpoint with the deepObject ``updated_at[gte]`` filter."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while page <= MAX_PAGES:
        payload = client.get(
            path,
            {"updated_at[gte]": since_iso, "page": page, "page_size": page_size},
        )
        batch = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(batch, list) or not batch:
            break
        fresh: list[dict[str, Any]] = []
        for row in batch:
            if not isinstance(row, dict):
                continue
            if not _within_since(row, since_iso):
                continue
            identifier = _record_id(row)
            if identifier and identifier in seen:
                continue
            if identifier:
                seen.add(identifier)
            fresh.append(row)
        if not fresh:
            break
        items.extend(fresh)
        nxt = _next_page(payload, page, page_size, len(batch))
        if nxt is None or nxt == page:
            break
        page = nxt
    return items


def _within_since(row: dict[str, Any], since_iso: str) -> bool:
    stamp = (
        row.get("updated_at")
        or row.get("paid_at")
        or row.get("paying_at")
        or row.get("created_at")
    )
    if not stamp:
        return True
    got = parse_time(stamp)
    start = parse_time(since_iso)
    if got is None or start is None:
        return True
    return got >= start


def _next_page(payload: dict[str, Any], page: int, page_size: int, batch_len: int) -> int | None:
    if "meta" not in payload:
        return page + 1 if batch_len >= page_size else None
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    nxt = meta.get("next") if isinstance(meta, dict) else None
    if nxt in (None, "", 0, "0"):
        return None
    if isinstance(nxt, int):
        return nxt
    text = str(nxt).strip()
    if text.isdigit():
        return int(text)
    if "page=" in text:
        from urllib import parse as urlparse

        query = urlparse.parse_qs(urlparse.urlparse(text).query)
        values = query.get("page") or []
        if values and str(values[0]).isdigit():
            return int(values[0])
    if batch_len >= page_size:
        return page + 1
    return None


def poll_feeds(
    client: Any, since_iso: str
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    found: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for path in FEEDS:
        try:
            found[path] = list_updated_since(client, path, since_iso)
        except Exception as exc:  # noqa: BLE001 - one feed must not hide the others
            errors.append(f"{path}: {type(exc).__name__}: {redact_text(str(exc))}")
            found[path] = []
    return found, errors


def _episode_anchor(
    store: EventKeyStore | None,
    episode_id: str,
    status: str,
    proposed: str,
    *,
    persist: bool,
) -> str:
    if store is None:
        return normalize_anchor(proposed) or status
    return store.episode_anchor(episode_id, status, proposed, persist=persist)


def _event_in_window(notice: ApiNotice, since: datetime) -> bool:
    """Every filed event has to fall inside the poll lookback."""
    moment = parse_time(notice.occurred_at) or parse_time(notice.anchor)
    if moment is None:
        return False
    return moment >= since


def notices_from_snapshot(
    *,
    programs: list[dict[str, Any]],
    loans: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    payouts: list[dict[str, Any]],
    store: EventKeyStore | None = None,
    persist_episodes: bool = False,
    prior_loan_status: dict[str, str] | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
) -> list[ApiNotice]:
    """Map the API shapes from the coverage study. Synthetic callers only."""
    programs_by_id = {
        _record_id(row): row for row in programs if _record_id(row)
    }
    loans_for: dict[str, list[dict[str, Any]]] = {}
    for loan in loans:
        pid = _program_id_of(loan)
        if pid:
            loans_for.setdefault(pid, []).append(loan)
    invoices_for: dict[str, list[dict[str, Any]]] = {}
    for invoice in invoices:
        pid = _program_id_of(invoice)
        if pid:
            invoices_for.setdefault(pid, []).append(invoice)

    notices: list[ApiNotice] = []
    for program in programs:
        notices.extend(
            _program_notices(
                program,
                loans_for.get(_record_id(program), []),
                store=store,
                persist=persist_episodes,
            )
        )
    for loan in loans:
        program = programs_by_id.get(_program_id_of(loan), {})
        notices.extend(
            _loan_notices(
                loan,
                program,
                invoices_for.get(_program_id_of(loan), []),
                prior_status=(prior_loan_status or {}).get(_record_id(loan), ""),
                store=store,
                persist=persist_episodes,
            )
        )
    for invoice in invoices:
        program = programs_by_id.get(_program_id_of(invoice), {})
        notice = _invoice_notice(invoice, program, store=store, now=now)
        if notice is not None:
            notices.append(notice)
    for payout in payouts:
        notice = _payout_notice(payout)
        if notice is not None:
            notices.append(notice)
    # Keep a merged notice when any pre-merge part is inside the lookback,
    # and give the keeper that in-window time. Filtering after the merge
    # used to drop the program's overdue flip along with the invoice.
    return _collapse(notices, since=since)


def _program_notices(
    program: dict[str, Any],
    loans: list[dict[str, Any]],
    *,
    store: EventKeyStore | None,
    persist: bool,
) -> list[ApiNotice]:
    program_id = _record_id(program)
    status = _status(program)
    updated = str(program.get("updated_at") or "")
    if not program_id:
        return []
    found: list[ApiNotice] = []
    if status == "payment_overdue":
        anchor = _episode_anchor(
            store,
            f"{program_id}|{triage.LATE_PAYMENT}",
            "payment_overdue",
            updated or "payment_overdue",
            persist=persist,
        )
        found.extend(
            _status_notice(
                event_type=triage.LATE_PAYMENT,
                program=program,
                anchor=anchor,
                occurred_at=anchor,
                subject=f"Past due payment for {insured_name_of(program)}",
                lines=[
                    "This policy has a past-due payment of "
                    + (cents_to_money(program.get("overdue_amount_cents")) or "the installment")
                    + (
                        f" which was due on {_us_date(program.get('due_date') or updated)}."
                        if (program.get("due_date") or updated)
                        else "."
                    )
                ],
            )
        )
    if status == "pending_cancellation":
        anchor = _episode_anchor(
            store,
            f"{program_id}|{triage.INTENT_TO_CANCEL}",
            "pending_cancellation",
            updated or "pending_cancellation",
            persist=persist,
        )
        insured = insured_name_of(program)
        found.extend(
            _status_notice(
                event_type=triage.INTENT_TO_CANCEL,
                program=program,
                anchor=anchor,
                occurred_at=updated,
                subject=f"[URGENT] {insured} - Policy(s) at risk for cancellation",
                lines=[
                    "Failure to pay will result in the cancelation of your coverage on "
                    + (_us_date(program.get("cancel_at") or updated) or "the cancel date")
                    + "."
                ],
            )
        )
    loan_canceled = any(_status(loan) in _CANCELED_LOAN for loan in loans)
    if status in _CANCELLED_PROGRAM and loan_canceled:
        anchor = _episode_anchor(
            store,
            f"{program_id}|{triage.CANCELLATION}",
            "cancelled",
            updated or "cancelled",
            persist=persist,
        )
        insured = insured_name_of(program)
        when = _us_date(updated)
        lines = [
            f"The loan has been canceled effective {when}." if when else "The loan has been canceled.",
            "Amount due: " + (cents_to_money(program.get("balance_cents")) or "$0.00"),
        ]
        found.extend(
            _status_notice(
                event_type=triage.CANCELLATION,
                program=program,
                anchor=anchor,
                occurred_at=updated,
                subject=(
                    f"The coverage policy for {insured} has been canceled due to non-payment"
                ),
                lines=lines,
                extra_policies=policy_numbers_of(*loans),
            )
        )
    elif status not in _CANCELLED_PROGRAM:
        # A program that left a status still updates the episode so the next
        # entry is a new key. Persist only; it does not file.
        if persist and store is not None and status and status != "payment_overdue":
            if status != "pending_cancellation":
                store.episode_anchor(
                    f"{program_id}|{triage.LATE_PAYMENT}",
                    status,
                    updated or status,
                    persist=True,
                )
                store.episode_anchor(
                    f"{program_id}|{triage.INTENT_TO_CANCEL}",
                    status,
                    updated or status,
                    persist=True,
                )
    return [item for item in found if item is not None]


def _status_notice(
    *,
    event_type: str,
    program: dict[str, Any],
    anchor: str,
    occurred_at: str,
    subject: str,
    lines: list[str],
    extra_policies: list[str] | None = None,
) -> list[ApiNotice]:
    program_id = _record_id(program)
    policies = policy_numbers_of(program)
    for number in extra_policies or []:
        if number not in policies:
            policies.append(number)
    insured = insured_name_of(program)
    body = _render_common(
        program_id=program_id,
        insured=insured,
        policy_numbers=policies,
        extra_lines=lines,
        insured_email=insured_email_of(program),
        insured_phone=insured_phone_of(program),
    )
    notice = _notice(
        event_type=event_type,
        program_id=program_id,
        anchor=anchor,
        occurred_at=occurred_at or anchor,
        subject=subject,
        body=body,
        program=program,
        policy_numbers=policies,
        insured_name=insured,
    )
    return [notice] if notice is not None else []


def _loan_notices(
    loan: dict[str, Any],
    program: dict[str, Any],
    invoices: list[dict[str, Any]],
    *,
    prior_status: str,
    store: EventKeyStore | None,
    persist: bool,
) -> list[ApiNotice]:
    program_id = _program_id_of(loan) or _record_id(program)
    if not program_id:
        return []
    holder = dict(program or {})
    if _record_id(holder) == "":
        holder = {"id": program_id, **holder}
    status = _status(loan)
    updated = str(loan.get("updated_at") or "")
    found: list[ApiNotice] = []
    if status == "pending_cancel":
        anchor = _episode_anchor(
            store,
            f"{program_id}|{triage.INTENT_TO_CANCEL}",
            "pending_cancel",
            updated or "pending_cancel",
            persist=persist,
        )
        insured = insured_name_of(holder, insured_name_of(loan))
        found.extend(
            _loan_status_notice(
                holder,
                loan,
                event_type=triage.INTENT_TO_CANCEL,
                anchor=anchor,
                subject=f"[URGENT] {insured} - Policy(s) at risk for cancellation",
                lines=[
                    "Notice of intent to cancel. Failure to pay will result in the "
                    "cancelation of your coverage on "
                    + (_us_date(loan.get("cancel_at") or updated) or "the cancel date")
                    + "."
                ],
            )
        )
    if status == "completed":
        anchor = _episode_anchor(
            store,
            f"{program_id}|{triage.PAID_OFF}",
            "completed",
            updated or "completed",
            persist=persist,
        )
        insured = insured_name_of(holder, insured_name_of(loan))
        found.extend(
            _loan_status_notice(
                holder,
                loan,
                event_type=triage.PAID_OFF,
                anchor=anchor,
                subject=f"The coverage policy for {insured} has been paid off",
                lines=["The loan has been paid off."],
            )
        )
    reinstatement_invoice = next(
        (row for row in invoices if bool(row.get("is_reinstatement"))),
        None,
    )
    prior = str(prior_status or "").strip().lower()
    became_bound = status == "bound" and prior in {"pending_cancel", "canceled", "cancelled"}
    if reinstatement_invoice is None and became_bound:
        anchor = _episode_anchor(
            store,
            f"{program_id}|{triage.REINSTATEMENT}",
            "bound",
            updated or "bound",
            persist=persist,
        )
        insured = insured_name_of(holder)
        found.extend(
            _loan_status_notice(
                holder,
                loan,
                event_type=triage.REINSTATEMENT,
                anchor=anchor,
                subject=f"Your reinstatement request has been approved for {insured}",
                lines=["A reinstatement was approved. The carrier still has to accept it."],
            )
        )
    if persist and store is not None and status and status != "pending_cancel":
        store.episode_anchor(
            f"{program_id}|{triage.INTENT_TO_CANCEL}",
            f"loan:{status}",
            updated or status,
            persist=True,
        )
    return found


def _loan_status_notice(
    program: dict[str, Any],
    loan: dict[str, Any],
    *,
    event_type: str,
    anchor: str,
    subject: str,
    lines: list[str],
) -> list[ApiNotice]:
    program_id = _record_id(program) or _program_id_of(loan)
    policies = policy_numbers_of(program, loan)
    insured = insured_name_of(program, insured_name_of(loan))
    body = _render_common(
        program_id=program_id,
        insured=insured,
        policy_numbers=policies,
        extra_lines=lines,
        insured_email=insured_email_of(program, loan),
        insured_phone=insured_phone_of(program, loan),
    )
    notice = _notice(
        event_type=event_type,
        program_id=program_id,
        anchor=anchor,
        occurred_at=str(loan.get("updated_at") or anchor),
        subject=subject,
        body=body,
        program=program,
        policy_numbers=policies,
        insured_name=insured,
    )
    return [notice] if notice is not None else []


def _invoice_alias_keys(
    program_id: str, event_type: str, anchor: str, invoice_id: str
) -> tuple[str, ...]:
    if not invoice_id or invoice_id == anchor:
        return ()
    return (make_event_key(program_id, event_type, invoice_id),)


def _overdue_flip_time(
    program: dict[str, Any], store: EventKeyStore | None
) -> str:
    """When the program entered ``payment_overdue``. Empty if it has not.

    The episode anchor freezes the first flip on this run's store. A later
    poll that is still overdue reuses that time. The invoice due date is
    not a flip.
    """
    if _status(program) != "payment_overdue":
        return ""
    updated = str(program.get("updated_at") or "").strip()
    if parse_time(updated) is None:
        return ""
    program_id = _record_id(program)
    if not program_id or store is None:
        return normalize_anchor(updated)
    return store.episode_anchor(
        f"{program_id}|{triage.LATE_PAYMENT}",
        "payment_overdue",
        updated,
        persist=False,
    )


def _past_due_occurred_at(
    invoice: dict[str, Any],
    program: dict[str, Any],
    *,
    store: EventKeyStore | None,
    program_id: str,
    invoice_id: str,
    now: datetime | None,
) -> str:
    """The program's overdue-flip time, else the first time this store saw it.

    Never the invoice due date. On the 15-minute timer the due date is about
    a day before the program flips, so a due-date event time falls outside
    the lookback and the notice never files.
    """
    # ``invoice`` due date, past-due date, and updated_at are display fields.
    # ``program_id`` selects the episode; the flip itself comes from ``program``.
    flip = _overdue_flip_time(program, store)
    if flip and parse_time(flip):
        return flip
    moment = _iso(now or _now())
    episode = f"invoice:{invoice_id}|late_payment_first_seen"
    if store is None:
        return moment
    return store.first_seen_at(episode, moment)


def _invoice_notice(
    invoice: dict[str, Any],
    program: dict[str, Any],
    *,
    store: EventKeyStore | None = None,
    now: datetime | None = None,
) -> ApiNotice | None:
    program_id = _program_id_of(invoice) or _record_id(program)
    anchor, invoice_id = invoice_anchor(invoice)
    if not program_id or not invoice_id:
        return None
    holder = dict(program or {})
    if not _record_id(holder):
        holder["id"] = program_id
    insured = insured_name_of(holder, str(invoice.get("payer_name") or invoice.get("payee") or ""))
    email = insured_email_of(holder, invoice)
    phone = insured_phone_of(holder, invoice)
    policies = policy_numbers_of(holder, invoice)
    amount = cents_to_money(invoice.get("total_amount_cents"))
    updated = str(invoice.get("updated_at") or invoice.get("paid_at") or "")
    if is_dispute_invoice(invoice):
        return _notice(
            event_type=triage.DISPUTED_CHARGE,
            program_id=program_id,
            anchor=anchor,
            alias_keys=_invoice_alias_keys(
                program_id, triage.DISPUTED_CHARGE, anchor, invoice_id
            ),
            occurred_at=updated or invoice_id,
            subject=f"Disputed charge for {insured}",
            body=_render_common(
                program_id=program_id,
                insured=insured,
                policy_numbers=policies,
                insured_email=email,
                insured_phone=phone,
                extra_lines=[
                    f"Your customer, {insured}, disputed the following payment.",
                    f"Payment amount {amount}" if amount else "Payment amount is on the settlement invoice.",
                    f"Invoice No. {invoice.get('invoice_number') or invoice_id}",
                ],
            ),
            program=holder,
            policy_numbers=policies,
            insured_name=insured,
            invoice_id=invoice_id,
            amount_cents=_cents(invoice.get("total_amount_cents")),
        )
    if bool(invoice.get("is_reinstatement")):
        return _notice(
            event_type=triage.REINSTATEMENT,
            program_id=program_id,
            anchor=anchor,
            alias_keys=_invoice_alias_keys(
                program_id, triage.REINSTATEMENT, anchor, invoice_id
            ),
            occurred_at=str(invoice.get("paid_at") or updated or invoice_id),
            subject=f"Your reinstatement request has been approved for {insured}",
            body=_render_common(
                program_id=program_id,
                insured=insured,
                policy_numbers=policies,
                insured_email=email,
                insured_phone=phone,
                extra_lines=["A reinstatement was approved. The carrier still has to accept it."],
            ),
            program=holder,
            policy_numbers=policies,
            insured_name=insured,
            invoice_id=invoice_id,
        )
    if _status(invoice) in {"overdue", "past_due"}:
        due = _us_date(invoice.get("due_date") or updated)
        sentence = "This policy has a past-due payment"
        if amount and due:
            sentence += f" of {amount} which was due on {due}."
        elif amount:
            sentence += f" of {amount}."
        else:
            sentence += "."
        occurred = _past_due_occurred_at(
            invoice,
            holder,
            store=store,
            program_id=program_id,
            invoice_id=invoice_id,
            now=now,
        )
        return _notice(
            event_type=triage.LATE_PAYMENT,
            program_id=program_id,
            anchor=anchor,
            alias_keys=_invoice_alias_keys(
                program_id, triage.LATE_PAYMENT, anchor, invoice_id
            ),
            occurred_at=occurred,
            subject=f"Past due payment for {insured}",
            body=_render_common(
                program_id=program_id,
                insured=insured,
                policy_numbers=policies,
                insured_email=email,
                insured_phone=phone,
                extra_lines=[sentence, f"Invoice No. {invoice.get('invoice_number') or invoice_id}"],
            ),
            program=holder,
            policy_numbers=policies,
            insured_name=insured,
            invoice_id=invoice_id,
            amount_cents=_cents(invoice.get("total_amount_cents")),
        )
    paid_at = str(invoice.get("paid_at") or "").strip()
    if paid_at:
        return _notice(
            event_type=triage.PAYMENT_CONFIRMATION,
            program_id=program_id,
            anchor=anchor,
            alias_keys=_invoice_alias_keys(
                program_id, triage.PAYMENT_CONFIRMATION, anchor, invoice_id
            ),
            occurred_at=paid_at,
            subject=f"{insured} Policy(s) Payment Confirmation",
            body=_render_common(
                program_id=program_id,
                insured=insured,
                policy_numbers=policies,
                insured_email=email,
                insured_phone=phone,
                extra_lines=[
                    f"Payment of {amount} was received." if amount else "A payment was received.",
                    f"Invoice No. {invoice.get('invoice_number') or invoice_id}",
                ],
            ),
            program=holder,
            policy_numbers=policies,
            insured_name=insured,
            invoice_id=invoice_id,
            amount_cents=_cents(invoice.get("total_amount_cents")),
        )
    return None


def _cents(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _payout_type(payout: dict[str, Any]) -> str:
    return str(
        payout.get("payout_type") or payout.get("type") or payout.get("payout_kind") or ""
    ).strip().lower()


def _payout_notice(payout: dict[str, Any]) -> ApiNotice | None:
    kind = _payout_type(payout)
    if kind not in _PAYOUT_TYPES:
        return None
    payout_id = _record_id(payout)
    if not payout_id:
        return None
    program_id = _program_id_of(payout) or AGENCY_PROGRAM
    occurred = str(payout.get("paying_at") or payout.get("paid_at") or payout.get("updated_at") or "")
    amount = cents_to_money(
        payout.get("net_payout_amount_cents")
        if payout.get("net_payout_amount_cents") is not None
        else payout.get("amount_cents")
    )
    sentence = (
        f"A payment of {amount} was remitted to the agency."
        if amount
        else "A payment was remitted to the agency."
    )
    return _notice(
        event_type=triage.AGENCY_REMITTANCE,
        program_id=program_id,
        anchor=payout_id,
        occurred_at=occurred or payout_id,
        subject="Remittance Notification",
        body=sentence,
        remittance=True,
        amount_cents=_cents(
            payout.get("net_payout_amount_cents")
            if payout.get("net_payout_amount_cents") is not None
            else payout.get("amount_cents")
        ),
    )


def _absorb(keeper: ApiNotice, other: ApiNotice, since: datetime | None) -> None:
    """Fold ``other`` into ``keeper``. An in-window part keeps the merged event."""
    keeper.alias_keys = tuple(
        dict.fromkeys((*keeper.alias_keys, other.event_key, *other.alias_keys))
    )
    if since is None or _event_in_window(keeper, since):
        return
    if _event_in_window(other, since):
        keeper.occurred_at = other.occurred_at


def _collapse(notices: list[ApiNotice], *, since: datetime | None = None) -> list[ApiNotice]:
    """One note per program per family when a richer invoice event is present.

    When ``since`` is set, a merged notice stays if any of its parts is
    inside the lookback. The keeper takes that in-window time so a due-date
    invoice cannot erase the program's overdue flip.
    """
    by_key: dict[str, ApiNotice] = {}
    for notice in notices:
        current = by_key.get(notice.event_key)
        if current is None:
            by_key[notice.event_key] = notice
            continue
        preferred = current if current.invoice_id else notice
        other = notice if preferred is current else current
        if preferred.alias_keys != current.alias_keys and preferred is not current:
            preferred.alias_keys = tuple(
                dict.fromkeys((*preferred.alias_keys, *current.alias_keys))
            )
        _absorb(preferred, other, since)
        by_key[notice.event_key] = preferred
    grouped: dict[tuple[str, str], list[ApiNotice]] = {}
    for notice in by_key.values():
        if notice.event_type in {
            triage.LATE_PAYMENT,
            triage.INTENT_TO_CANCEL,
            triage.CANCELLATION,
            triage.REINSTATEMENT,
        }:
            grouped.setdefault((notice.program_id.lower(), notice.event_type), []).append(notice)
    drop: set[int] = set()
    for bucket in grouped.values():
        unique = list({id(item): item for item in bucket}.values())
        if len(unique) < 2:
            continue
        with_invoice = [item for item in unique if item.invoice_id]
        keeper = with_invoice[0] if with_invoice else unique[0]
        for extra in unique:
            if extra is keeper:
                continue
            _absorb(keeper, extra, since)
            drop.add(id(extra))
    kept = [notice for notice in by_key.values() if id(notice) not in drop]
    if since is None:
        return kept
    return [notice for notice in kept if _event_in_window(notice, since)]


def events_from_webhook(event_name: str, payload: dict[str, Any] | None) -> list[ApiNotice]:
    """Map an Ascend webhook body into poller notices. No listener is attached.

    ``invoice.paid``, ``invoice.marked_overdue``, dispute-shaped
    ``invoice.created``, and ``payout.paying`` / ``payout.paid`` become the
    same notices the poller files. ``invoice.processing_payment``,
    ``invoice.voided``, and the refund webhooks return nothing so email keeps
    those gaps.
    """
    name = str(event_name or "").strip().lower()
    if name not in RECOGNIZED_WEBHOOKS or name in WEBHOOKS_LEFT_TO_EMAIL:
        return []
    body = payload if isinstance(payload, dict) else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    record = dict(data)
    if name == WEBHOOK_INVOICE_PAID:
        record.setdefault("paid_at", record.get("updated_at") or body.get("occurred_at") or "")
        notice = _invoice_notice(record, _program_shell(record))
        return [notice] if notice is not None else []
    if name == WEBHOOK_INVOICE_OVERDUE:
        record["status"] = record.get("status") or "overdue"
        notice = _invoice_notice(record, _program_shell(record))
        return [notice] if notice is not None and notice.event_type == triage.LATE_PAYMENT else []
    if name == WEBHOOK_INVOICE_CREATED:
        notice = _invoice_notice(record, _program_shell(record))
        if notice is not None and notice.event_type == triage.DISPUTED_CHARGE:
            return [notice]
        return []
    if name in {WEBHOOK_PAYOUT_PAYING, WEBHOOK_PAYOUT_PAID}:
        notice = _payout_notice(record)
        return [notice] if notice is not None else []
    return []


def _program_shell(record: dict[str, Any]) -> dict[str, Any]:
    program_id = _program_id_of(record)
    insured = str(record.get("payer_name") or record.get("payee") or "").strip()
    shell: dict[str, Any] = {"id": program_id}
    if insured:
        shell["insured"] = {"business_name": insured}
    return shell


def _window_start(store: EventKeyStore, now: datetime) -> datetime:
    cursor = store.cursor()
    if cursor is not None:
        return cursor - timedelta(minutes=CURSOR_OVERLAP_MINUTES)
    raw = str(os.environ.get(LOOKBACK_ENV) or "").strip()
    try:
        minutes = int(raw) if raw else DEFAULT_LOOKBACK_MINUTES
    except ValueError:
        minutes = DEFAULT_LOOKBACK_MINUTES
    if minutes < 1:
        minutes = DEFAULT_LOOKBACK_MINUTES
    return now - timedelta(minutes=minutes)


def build_client() -> Any:
    """The same client and secret path the hourly ``robie-ascend-sync`` uses."""
    from .ascend_sync import AscendApiClient

    return AscendApiClient()


def build_driver_context(*, dry_run: bool) -> Any:
    from .ascend_notice_driver import NullNoticeSource, build_processing_context

    ctx = build_processing_context(dry_run=dry_run, source=NullNoticeSource())
    ctx.dry_run = dry_run
    return ctx


def _would_file_line(notice: ApiNotice, detail: dict[str, Any]) -> str:
    task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
    task_type = str(task.get("type") or "")
    bits = [
        "would-file",
        notice.event_key,
        f"type={notice.event_type}",
        f"program={notice.program_id}",
        f"applicant={detail.get('applicant_id') or ''}",
        f"discussion={detail.get('discussion_title') or ''}",
    ]
    if task_type:
        bits.append(f"task={task_type}")
    return " ".join(bits)


def run_once(
    *,
    client: Any,
    store: EventKeyStore,
    driver_ctx: Any | None = None,
    now: datetime | None = None,
    since: datetime | None = None,
    alerter: Callable[[str], bool] | None = None,
    prior_loan_status: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Poll, map, and file. Live writes happen only when the env flag is ``1``."""
    moment = now or _now()
    started = _iso(moment)
    live = live_enabled()
    window = since or _window_start(store, moment)
    since_iso = _iso(window)
    wrapped = client if isinstance(client, GetOnlyClient) else GetOnlyClient(client)
    feeds, errors = poll_feeds(wrapped, since_iso)
    programs = list(feeds.get(FEED_PROGRAMS) or [])
    loans = list(feeds.get(FEED_LOANS) or [])
    invoices = list(feeds.get(FEED_INVOICES) or [])
    payouts = list(feeds.get(FEED_PAYOUTS) or [])
    _hydrate(wrapped, programs, loans, invoices)
    enrich_policy_numbers(wrapped, programs, store)
    observed = dict(prior_loan_status or {})
    if not errors:
        for loan in loans:
            loan_id = _record_id(loan)
            if loan_id and loan_id not in observed:
                observed[loan_id] = store.episode_status(f"loan:{loan_id}")
    notices = notices_from_snapshot(
        programs=programs,
        loans=loans,
        invoices=invoices,
        payouts=payouts,
        store=store,
        persist_episodes=live and not errors,
        prior_loan_status=observed,
        since=window,
        now=moment,
    )
    if not errors:
        for loan in loans:
            loan_id = _record_id(loan)
            if loan_id:
                store.episode_anchor(
                    f"loan:{loan_id}",
                    _status(loan),
                    _status(loan) or "unknown",
                    persist=True,
                )
    ctx = driver_ctx if driver_ctx is not None else build_driver_context(dry_run=not live)
    from .ascend_notice_driver import NullNoticeSource

    ctx.dry_run = not live
    ctx.remember_filings = True
    ctx.now = moment
    ctx.source = NullNoticeSource()
    ctx.ascend_client = _SnapshotPrograms(programs)
    if hasattr(ctx, "seen_notice_events"):
        ctx.seen_notice_events.clear()
    if hasattr(ctx, "planned_category_discussions"):
        ctx.planned_category_discussions.clear()

    try:
        from .ascend_unmatched_digest import persist_unmatched_notice, resolve_unmatched_notice
    except Exception as exc:  # noqa: BLE001 - the poll files even if the digest is absent
        logger.warning("unmatched digest helpers unavailable: %s", type(exc).__name__)
        persist_unmatched_notice = None
        resolve_unmatched_notice = None

    def _remember_unmatched(action: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn is None:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - one store error must not stop the poll
            logger.warning("unmatched notice %s failed: %s", action, type(exc).__name__)
            return None

    def _resolve_keys(notice: ApiNotice) -> None:
        for key in (notice.event_key, *notice.alias_keys):
            _remember_unmatched("resolve", resolve_unmatched_notice, store, key, seen_at)

    results: list[dict[str, Any]] = []
    lines: list[str] = []
    ask: list[dict[str, str]] = []
    seen_at = _iso(moment)
    for notice in notices:
        if store.is_filed(notice.event_key) or any(store.is_filed(key) for key in notice.alias_keys):
            _resolve_keys(notice)
            results.append(
                {
                    "event_key": notice.event_key,
                    "event_type": notice.event_type,
                    "status": "skipped",
                    "reason": "api_already_filed",
                }
            )
            continue
        if notice.remittance or notice.event_type == triage.AGENCY_REMITTANCE:
            outcome = _file_remittance(ctx, notice)
        else:
            outcome = _file_through_driver(ctx, notice)
        results.append(outcome)
        if str(outcome.get("reason") or "").startswith("applicant_unresolved"):
            plain = _remember_unmatched(
                "persist",
                persist_unmatched_notice,
                store,
                notice,
                outcome,
                seen_at=seen_at,
            )
            detail = outcome.get("detail") if isinstance(outcome.get("detail"), dict) else {}
            ask.append(
                {
                    "event_key": notice.event_key,
                    "event_type": notice.event_type,
                    "program_id": notice.program_id,
                    "insured_name": notice.insured_name,
                    "reason": str(outcome.get("reason") or ""),
                    "unmatched_reason": str(plain or detail.get("unmatched_reason") or ""),
                }
            )
        elif str(outcome.get("status") or "") in {"dry_run", "done"}:
            # Matched on this poll, including a dry run that would file and
            # a live file. The email driver resolves its own filings.
            _resolve_keys(notice)
        if outcome.get("status") in {"dry_run", "done"}:
            lines.append(_would_file_line(notice, outcome.get("detail") or {}))
            if live and outcome.get("status") == "done":
                store.record_filed(notice)
                _resolve_keys(notice)
        elif outcome.get("reason") == "remittance_target_unconfigured":
            logger.info(
                "skip %s remittance_target_unconfigured",
                notice.event_key,
            )

    filing = _poll_will_file(live)
    ready_results: list[dict[str, Any]] = []
    if filing:
        ready_results = _file_ready_unmatched(
            ctx,
            store,
            programs=programs,
            seen_at=seen_at,
        )
        results.extend(ready_results)

    ok = not errors
    streak = store.note_poll_result(
        ok=ok,
        error="; ".join(errors),
        started_at=started,
        live=filing,
    )
    alerted = False
    if streak >= STALL_RUNS:
        message = (
            f"Ascend API notice poll failed {streak} times in a row. "
            f"Last error: {redact_text('; '.join(errors))[:400]}"
        )
        alerted = _alert(message, alerter)
    if ok and filing:
        store.advance_cursor(moment)
    summary = {
        "dry_run": not live,
        "live": filing,
        "since": since_iso,
        "http_methods": ["GET"],
        "feeds": {path: len(feeds.get(path) or []) for path in FEEDS},
        "errors": errors,
        "stall_streak": streak,
        "stall_alerted": alerted,
        "would_file_lines": lines,
        "would_file_count": len(lines),
        "ask": ask,
        "results": results,
        "ready_checked": len(ready_results),
        "ready_filed": sum(1 for row in ready_results if row.get("status") == "done"),
    }
    for line in lines:
        logger.warning("%s", line)
    if ask:
        logger.warning(
            "ask: %s client notice(s) matched no single EZLynx applicant: %s",
            len(ask),
            "; ".join(
                f"{item['event_key']} {item['insured_name']} ({item['reason']})"
                for item in ask
            ),
        )
    return summary


def _alert(message: str, alerter: Callable[[str], bool] | None) -> bool:
    logger.error("STALL %s", message)
    try:
        if alerter is not None:
            alerter(message)
            return True
        from .ascend_sync import send_google_chat_alert

        send_google_chat_alert(message)
        return True
    except Exception as exc:  # noqa: BLE001 - the poll failure is already recorded
        logger.warning("stall alert failed: %s", type(exc).__name__)
        return False


def enrich_policy_numbers(
    client: GetOnlyClient,
    programs: list[dict[str, Any]],
    store: EventKeyStore | None,
) -> None:
    """GET /v1/billables?program_id= for each program and cache the policy numbers.

    Program records do not carry a policy number. The billable does. A later
    poll reuses the cache. An empty billable list is not cached, so a miss
    can be retried. Quote identifiers are not treated as policy numbers.
    """
    for program in programs:
        if not isinstance(program, dict):
            continue
        program_id = _record_id(program)
        if not program_id:
            continue
        cached = store.cached_policy_numbers(program_id) if store is not None else None
        if cached:
            _attach_policy_numbers(program, cached)
            continue
        already = policy_numbers_of(program)
        if already:
            if store is not None:
                store.remember_policy_numbers(program_id, already)
            continue
        try:
            payload = client.get(
                FEED_BILLABLES,
                {"program_id": program_id, "page_size": PAGE_SIZE},
            )
        except Exception as exc:  # noqa: BLE001 - one program must not stall the poll
            logger.warning(
                "billable lookup for program %s failed: %s",
                program_id,
                type(exc).__name__,
            )
            continue
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        numbers = policy_numbers_of({"billables": [row for row in rows if isinstance(row, dict)]})
        if not numbers:
            continue
        _attach_policy_numbers(program, numbers)
        if store is not None:
            store.remember_policy_numbers(program_id, numbers)


def _attach_policy_numbers(program: dict[str, Any], numbers: list[str]) -> None:
    existing = program.get("billables")
    rows = [row for row in existing if isinstance(row, dict)] if isinstance(existing, list) else []
    have = {str(row.get("policy_number") or "").strip() for row in rows}
    for number in numbers:
        if number and number not in have:
            rows.append({"policy_number": number})
            have.add(number)
    program["billables"] = rows


def _hydrate(
    client: GetOnlyClient,
    programs: list[dict[str, Any]],
    loans: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
) -> None:
    by_id = {_record_id(row): row for row in programs if _record_id(row)}
    needed: list[str] = []
    for row in [*loans, *invoices]:
        pid = _program_id_of(row)
        if pid and pid not in by_id and pid not in needed:
            needed.append(pid)
    for program in programs:
        status = _status(program)
        pid = _record_id(program)
        if status in _CANCELLED_PROGRAM | {"pending_cancellation"} and pid:
            if not any(_program_id_of(loan) == pid for loan in loans):
                try:
                    payload = client.get(FEED_LOANS, {"program_id": pid, "page_size": PAGE_SIZE})
                    extra = payload.get("data") if isinstance(payload.get("data"), list) else []
                    for loan in extra:
                        if isinstance(loan, dict):
                            loans.append(loan)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "loan lookup for program %s failed: %s",
                        pid,
                        type(exc).__name__,
                    )
    for pid in needed:
        try:
            payload = client.get(f"{FEED_PROGRAMS}/{pid}")
            record = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if isinstance(record, dict) and _record_id(record):
                programs.append(record)
                by_id[_record_id(record)] = record
        except Exception as exc:  # noqa: BLE001
            logger.warning("program lookup %s failed: %s", pid, type(exc).__name__)


class _SnapshotPrograms:
    """ascend_api-shaped read model so #762 triage does not GET again."""

    def __init__(self, programs: list[dict[str, Any]]) -> None:
        self._programs = {_record_id(row).lower(): row for row in programs if _record_id(row)}

    def get_program(self, program_id: str) -> dict[str, Any]:
        found = self._programs.get(str(program_id or "").strip().lower())
        if found is None:
            from .ascend_api import AscendApiError

            raise AscendApiError(None, "program was not in the API poll snapshot")
        return dict(found)

    def find_program_by_policy(self, policy_number: str) -> dict[str, Any] | None:
        wanted = str(policy_number or "").strip().upper()
        if not wanted:
            return None
        for program_id, program in self._programs.items():
            numbers = {item.upper() for item in policy_numbers_of(program)}
            if wanted in numbers:
                return {"program": dict(program), "program_id": program_id}
        return None


def _notice_from_ready_row(row: dict[str, Any]) -> ApiNotice | None:
    """Rebuild the notice the digest stored. Empty text cannot be filed."""
    subject = str(row.get("subject") or "").strip()
    body = str(row.get("body") or "")
    if not subject or not str(body).strip():
        return None
    try:
        program = json.loads(row.get("program_json") or "{}")
    except json.JSONDecodeError:
        program = {}
    if not isinstance(program, dict):
        program = {}
    numbers = row.get("policy_numbers") or []
    if isinstance(numbers, str):
        numbers = [numbers]
    return ApiNotice(
        event_key=str(row.get("event_key") or "").strip(),
        event_type=str(row.get("notice_type") or "").strip(),
        program_id=str(row.get("program_id") or "").strip(),
        anchor=str(row.get("first_seen") or "").strip(),
        occurred_at=str(row.get("first_seen") or "").strip(),
        policy_numbers=tuple(str(number) for number in numbers if str(number or "").strip()),
        insured_name=str(row.get("insured_name") or "").strip(),
        program=program,
        subject=subject,
        body=body,
        amount_cents=row.get("amount_cents"),
    )


def _file_ready_unmatched(
    ctx: Any,
    store: EventKeyStore,
    *,
    programs: list[dict[str, Any]],
    seen_at: str,
    limit: int = READY_FILE_LIMIT,
) -> list[dict[str, Any]]:
    """File digest matches through the same path as a notice from the feed.

    Live only. The caller must not invoke this while the API source is
    dry-run. At most ``limit`` rows. A note that is already in EZLynx,
    including an exact duplicate and this same bill posted on or after
    first_seen, is recorded as filed and the row is resolved. Any other
    result counts as one attempt. A failure while recording that attempt
    does not stop the remaining rows or the poll record. Write scope,
    dedupe, and discussion ownership stay inside ``_file_through_driver``.
    """
    if not live_enabled():
        return []
    ready = store.list_ready_to_file(limit)
    extra: list[dict[str, Any]] = []
    queued: list[tuple[dict[str, Any], ApiNotice]] = []
    for row in ready:
        notice = _notice_from_ready_row(row)
        if notice is None or not notice.event_key:
            continue
        if notice.program:
            extra.append(notice.program)
        queued.append((row, notice))
    ctx.ascend_client = _SnapshotPrograms([*extra, *programs])
    previous = getattr(ctx, "ready_row_filing", False)
    ctx.ready_row_filing = True
    filed: list[dict[str, Any]] = []
    try:
        for _row, notice in queued:
            try:
                if store.is_filed(notice.event_key):
                    store.resolve_unmatched(notice.event_key, seen_at)
                    filed.append(
                        {
                            "event_key": notice.event_key,
                            "event_type": notice.event_type,
                            "status": "done",
                            "reason": "api_already_filed",
                        }
                    )
                    continue
                outcome = _file_through_driver(ctx, notice)
                if _already_in_ezlynx(outcome):
                    store.record_filed(notice)
                    store.resolve_unmatched(notice.event_key, seen_at)
                    outcome = {**outcome, "status": "done"}
                else:
                    store.note_ready_attempt(
                        notice.event_key,
                        seen_at,
                        plain_file_failure(str(outcome.get("reason") or "")),
                    )
                filed.append(outcome)
            except Exception as exc:  # noqa: BLE001 - one row must not stop the poll
                logger.warning(
                    "ready-to-file %s failed: %s", notice.event_key, type(exc).__name__
                )
                try:
                    store.note_ready_attempt(
                        notice.event_key,
                        seen_at,
                        plain_file_failure(f"error: {type(exc).__name__}"),
                    )
                except Exception as record_exc:  # noqa: BLE001 - a locked store must not abort the poll
                    logger.warning(
                        "ready-to-file %s attempt was not recorded: %s",
                        notice.event_key,
                        type(record_exc).__name__,
                    )
                filed.append(
                    {
                        "event_key": notice.event_key,
                        "event_type": notice.event_type,
                        "status": "skipped",
                        "reason": f"error: {type(exc).__name__}",
                    }
                )
    finally:
        ctx.ready_row_filing = previous
    return filed


def _file_through_driver(ctx: Any, notice: ApiNotice) -> dict[str, Any]:
    from .ascend_notice_driver import process_notice

    result = process_notice(notice.as_email(), ctx)
    detail = dict(result.detail or {})
    detail["event_key"] = notice.event_key
    detail["source"] = "ascend_api"
    return {
        "event_key": notice.event_key,
        "event_type": notice.event_type,
        "status": result.status,
        "reason": result.reason,
        "detail": detail,
    }


def _file_remittance(ctx: Any, notice: ApiNotice) -> dict[str, Any]:
    applicant = remittance_applicant_id()
    detail = {
        "event_key": notice.event_key,
        "source": "ascend_api",
        "notice_type": triage.AGENCY_REMITTANCE,
        "category": "payments",
        "discussion_title": "Ascend - Payments",
    }
    if not applicant:
        logger.info(
            "agency remittance %s has no %s target; not written",
            notice.event_key,
            REMITTANCE_APPLICANT_ENV,
        )
        return {
            "event_key": notice.event_key,
            "event_type": notice.event_type,
            "status": "skipped",
            "reason": "remittance_target_unconfigured",
            "detail": detail,
        }
    from .ascend_notice_driver import CATEGORY_PAYMENTS, category_title, signed_notice_note

    title = category_title(CATEGORY_PAYMENTS)
    amount = cents_to_money(notice.amount_cents)
    sentence = (
        f"A payment of {amount} was remitted to the agency."
        if amount
        else "A payment was remitted to the agency."
    )
    note = signed_notice_note(f"AGENCY REMITTANCE notice from Ascend. {sentence}")
    filed = _file_category_note(
        ctx, applicant_id=applicant, title=title, note_text=note
    )
    detail.update(filed.get("detail") or {})
    detail["applicant_id"] = applicant
    detail["discussion_title"] = title
    return {
        "event_key": notice.event_key,
        "event_type": notice.event_type,
        "status": filed.get("status") or "skipped",
        "reason": filed.get("reason") or "",
        "detail": detail,
    }


def _file_category_note(
    ctx: Any, *, applicant_id: str, title: str, note_text: str
) -> dict[str, Any]:
    """Note-only write through the #762 scope, lease, and re-read guards."""
    from . import ezlynx_discussions as discussions
    from .ascend_notice_driver import (
        DISCUSSION_PLAN_CREATE,
        DISCUSSION_PLAN_CREATE_PLANNED,
        DISCUSSION_PLAN_USE_EXISTING,
        NonNoteWriteRefused,
        _list_category_discussion,
        _planned_discussion_token,
        _prepare_dry_run_create,
        _prepare_dry_run_note,
        _read_existing_note,
        _write_scope_refusal_reason,
        authorize_notice_write,
    )
    from .ezlynx_write_scope import EzlynxWriteScopeError

    detail: dict[str, Any] = {"discussion_title": title}
    plan_key = (str(applicant_id), title)
    remembered = ctx.planned_category_discussions.get(plan_key)
    chosen_id = ""
    display_title = title
    try:
        if remembered is not None and str(remembered).startswith("planned:"):
            discussion_plan = DISCUSSION_PLAN_CREATE_PLANNED
            if not ctx.dry_run:
                chosen, _count = _list_category_discussion(
                    ctx.discussion_client, applicant_id, title
                )
                if chosen is not None:
                    discussion_plan = DISCUSSION_PLAN_USE_EXISTING
                    chosen_id = discussions.discussion_id_of(chosen)
                    display_title = discussions.discussion_title_of(chosen) or title
                    ctx.planned_category_discussions[plan_key] = chosen_id
        elif remembered is not None:
            discussion_plan = DISCUSSION_PLAN_USE_EXISTING
            chosen_id = str(remembered)
        else:
            chosen, _count = _list_category_discussion(
                ctx.discussion_client, applicant_id, title
            )
            if chosen is not None:
                discussion_plan = DISCUSSION_PLAN_USE_EXISTING
                chosen_id = discussions.discussion_id_of(chosen)
                display_title = discussions.discussion_title_of(chosen) or title
                ctx.planned_category_discussions[plan_key] = chosen_id
            else:
                discussion_plan = DISCUSSION_PLAN_CREATE
                ctx.planned_category_discussions[plan_key] = _planned_discussion_token(
                    applicant_id, title
                )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "skipped",
            "reason": f"discussion_error: {type(exc).__name__}: {exc}",
            "detail": detail,
        }
    detail["discussion_plan"] = discussion_plan
    detail["discussion_title"] = display_title
    if chosen_id:
        note_match = _read_existing_note(
            ctx.discussion_client, applicant_id, note_text, discussion_id=chosen_id
        )
    else:
        note_match = {"read": False, "duplicate": False, "note_id": "", "reason": ""}
    scope_refusal = _write_scope_refusal_reason(applicant_id)
    if scope_refusal:
        return {"status": "skipped", "reason": scope_refusal, "detail": detail}
    if note_match.get("duplicate"):
        return {
            "status": "skipped",
            "reason": "existing_note_duplicate",
            "detail": detail,
        }
    try:
        if chosen_id:
            authorize_notice_write(
                "discussion_note",
                discussion_id=chosen_id,
                discussion_title=display_title,
            )
            if ctx.dry_run:
                filed = _prepare_dry_run_note(
                    ctx.discussion_client,
                    applicant_id,
                    note_text,
                    discussion_id=chosen_id,
                )
            else:
                from .ezlynx_driver_gate import EzlynxDriverGateRefused
                from .safety_seal import driver_gate_for_write

                try:
                    driver_gate_for_write()
                except EzlynxDriverGateRefused as exc:
                    return {
                        "status": "skipped",
                        "reason": f"driver_gate_refused: {exc}",
                        "detail": detail,
                    }
                filed = discussions.file_note_to_existing_discussion(
                    ctx.discussion_client,
                    applicant_id,
                    note_text,
                    discussion_id=chosen_id,
                    dry_run=False,
                )
        elif ctx.dry_run:
            authorize_notice_write("discussion_create_with_note", discussion_title=title)
            filed = _prepare_dry_run_create(applicant_id, title, note_text)
        else:
            from .ezlynx_driver_gate import EzlynxDriverGateRefused
            from .safety_seal import driver_gate_for_write

            authorize_notice_write("discussion_create_with_note", discussion_title=title)
            try:
                driver_gate_for_write()
            except EzlynxDriverGateRefused as exc:
                return {
                    "status": "skipped",
                    "reason": f"driver_gate_refused: {exc}",
                    "detail": detail,
                }
            try:
                filed = discussions.create_discussion_with_note(
                    ctx.discussion_client,
                    applicant_id,
                    title,
                    note_text,
                    dry_run=False,
                )
            except (discussions.DiscussionApiError, TimeoutError, OSError) as exc:
                recovered, _count = _list_category_discussion(
                    ctx.discussion_client, applicant_id, title
                )
                if recovered is None:
                    return {
                        "status": "skipped",
                        "reason": f"discussion_error: {exc}",
                        "detail": detail,
                    }
                chosen_id = discussions.discussion_id_of(recovered)
                display_title = discussions.discussion_title_of(recovered) or title
                ctx.planned_category_discussions[plan_key] = chosen_id
                detail["discussion_recovered_after_create_error"] = True
                detail["discussion_id"] = chosen_id
                authorize_notice_write(
                    "discussion_note",
                    discussion_id=chosen_id,
                    discussion_title=display_title,
                )
                recovered_note = _read_existing_note(
                    ctx.discussion_client,
                    applicant_id,
                    note_text,
                    discussion_id=chosen_id,
                )
                if recovered_note.get("duplicate"):
                    filed = {
                        "status": "filed",
                        "discussion_id": chosen_id,
                        "discussion_title": display_title,
                        "note_id": recovered_note.get("note_id") or None,
                    }
                else:
                    filed = discussions.file_note_to_existing_discussion(
                        ctx.discussion_client,
                        applicant_id,
                        note_text,
                        discussion_id=chosen_id,
                        dry_run=False,
                    )
            else:
                created_id = str(filed.get("discussion_id") or "").strip()
                if created_id:
                    ctx.planned_category_discussions[plan_key] = created_id
    except NonNoteWriteRefused as exc:
        return {"status": "skipped", "reason": str(exc), "detail": detail}
    except EzlynxWriteScopeError as exc:
        return {"status": "skipped", "reason": f"write_scope_refused: {exc}", "detail": detail}
    except discussions.DiscussionApiError as exc:
        return {"status": "skipped", "reason": f"discussion_error: {exc}", "detail": detail}
    status = str(filed.get("status") or "")
    if status not in {"filed", "dry_run", "created"}:
        return {
            "status": "skipped",
            "reason": f"note_not_filed: {filed.get('reason')}",
            "detail": detail,
        }
    detail["discussion_id"] = filed.get("discussion_id")
    detail["note_id"] = filed.get("note_id")
    if status == "created":
        status = "done"
    elif status == "filed":
        status = "done"
    return {"status": status, "reason": "ok", "detail": detail}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)
    parser = argparse.ArgumentParser(
        description=(
            "Poll Ascend for notice events and file #762 EZLynx notes. "
            "Dry-run unless ASCEND_API_SOURCE_LIVE=1. GET only."
        )
    )
    parser.parse_args(argv)
    live = live_enabled()
    if live:
        logger.warning("LIVE: ASCEND_API_SOURCE_LIVE=1, notes will be filed")
    else:
        logger.warning("DRY RUN: nothing will be written")
    try:
        store = EventKeyStore(db_path())
        summary = run_once(client=build_client(), store=store)
    except Exception as exc:  # noqa: BLE001
        logger.error("api notice source failed closed: %s: %s", type(exc).__name__, exc)
        try:
            store = EventKeyStore(db_path())
            streak = store.note_poll_result(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                started_at=_iso(_now()),
                live=_poll_will_file(live),
            )
            if streak >= STALL_RUNS:
                _alert(
                    f"Ascend API notice poll failed {streak} times in a row. "
                    f"Last error: {type(exc).__name__}",
                    None,
                )
        except Exception:  # noqa: BLE001
            streak = 0
        print(json.dumps({"dry_run": not live, "fatal": f"{type(exc).__name__}: {exc}", "stall_streak": streak}))
        return 1
    for line in summary.get("would_file_lines") or []:
        print(line)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if not summary.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
