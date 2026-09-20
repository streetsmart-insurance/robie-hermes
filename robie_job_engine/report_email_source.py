"""Morning email CSV source for live verification report fetch.

EZLynx Reports 5.0 saved-report favorites are not the system of record.
Scheduled mail to robie@streetsmart.insurance is.

4246 / 4247 still use the generic ``ROBIE daily CSV`` envelope and
header fingerprint. 4372 accepts only the email whose subject matches
``Mortgagee Verification Queue - ROBIE`` (durable fingerprint). Another
robie@ daily CSV is not 4372.

This module turns today's validated Gmail attachment (or an injected CSV)
into worker-facing row dicts.

Used by :func:`robie_job_engine.report_fetcher.fetch_report_rows` for
report ids 4247 (manual renewals), 4246 (audits; daily feed is the
4360 Active-filtered transaction CSV), and 4372 (mortgagee).

Fail closed: missing email, stale delivery, or a header/schema mismatch
raises. Rows are never guessed. A wrong-subject robie@ CSV is ignored
for 4372 (treated as missing), not ingested.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from . import gmail_report_ingestion as ing
from .gmail_report_ingestion import (
    GmailReportIngestionError,
    GmailReportMissingError,
    IngestedReport,
    ROBIE_MAILBOX,
)

logger = logging.getLogger("robie.report_email_source")

# Live workers that prefer today's robie@ CSV over Looker favorites.
EMAIL_FIRST_REPORT_IDS = frozenset({"4246", "4247", "4372"})

# 4246's daily email is scheduled report 4360 (Active policies only).
AUDIT_4360_STATUS_COLUMN = "Current Policy Status"
AUDIT_4360_ACTIVE_STATUS = "active"

MAX_EMAIL_AGE = timedelta(hours=36)

FILENAME_DAY_RE = re.compile(
    r"ROBIE_daily_CSV_(\d{4}-\d{2}-\d{2})T\d{4}", re.IGNORECASE
)


class EmailReportStaleError(GmailReportIngestionError):
    """The scheduled CSV is not today's delivery (or is older than the age cap)."""


# Display-header -> worker snake_case aliases. Original headers are kept.
# 4246 aliases include the 4360 transaction-level columns (Policy Transaction
# ID becomes audit_id; Effective Date becomes the post-renewal clock).
COLUMN_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "4247": {
        "Policy Number": ("policy_number",),
        "Account Name": ("insured_name",),
        "Applicant ID": ("applicant_id",),
        "Master Company": ("carrier_name", "carrier"),
        "Line Of Business": ("line_of_business",),
        "Policy Expiration Date": ("expiration_date",),
        "Policy Effective Date": ("effective_date", "policy_effective_date"),
        "Policy Source": ("source",),
        "Premium - Annualized": ("expiring_premium",),
        "Assigned Producer": ("assigned_agent",),
        "Department": ("department",),
        "CSR": ("csr",),
        "Branch": ("branch",),
    },
    "4246": {
        "Policy Number": ("policy_number",),
        "Account Name": ("insured_name",),
        "Applicant ID": ("applicant_id",),
        "Master Company": ("carrier", "carrier_name"),
        "Line of Business": ("line_of_business",),
        "Effective Date": (
            "renewal_effective_date",
            "effective_date",
            "policy_effective_date",
        ),
        "Expiration Date": ("expiration_date",),
        "Policy Transaction ID": ("audit_id",),
        "Current Policy Status": ("policy_status",),
        "Assigned Producer": ("assigned_agent",),
        "CSR": ("csr",),
        "Branch": ("department",),
        "Service Team": ("service_team",),
        "Policy ID": ("policy_id",),
    },
    "4372": {
        "Policy Number": ("policy_number",),
        "Account Name": ("insured_name",),
        "Applicant ID": ("applicant_id",),
        "Department": ("department",),
        "Branch": ("branch",),
        "Task ID": ("task_id",),
        "Task Due Date": ("expiration_date", "due_date"),
        "Task Status": ("task_status",),
        "Assigned Producer": ("assigned_agent",),
        "CSR": ("csr",),
        "Note": ("note",),
    },
}


def uses_email_source(report_id: str) -> bool:
    return str(report_id).strip() in EMAIL_FIRST_REPORT_IDS


def eastern_today(now: datetime | None = None) -> date:
    """Calendar day in America/New_York — the morning-email schedule zone."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:
        return now.date()


def delegated_gmail_service_account() -> str:
    return (
        os.environ.get("ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT")
        or os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT")
        or os.environ.get("ROBIE_GMAIL_DELEGATION_SA")
        or ""
    ).strip()


def build_default_gmail_service() -> Any:
    """Readonly DWD Gmail client for robie@. Fail closed if SA is unset."""
    service_account = delegated_gmail_service_account()
    if not service_account:
        raise GmailReportIngestionError(
            "email CSV path requires a delegated Gmail service account "
            "(ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT or "
            "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT); refusing to "
            "open Looker favorites for an email-first report"
        )
    return ing.build_readonly_delegated_service(service_account, ROBIE_MAILBOX)


def filename_day(filename: str) -> date | None:
    match = FILENAME_DAY_RE.search(str(filename or ""))
    if not match:
        return None
    return date.fromisoformat(match.group(1))


def assert_ingested_fresh(
    ingested: IngestedReport,
    day: date,
    *,
    now: datetime | None = None,
    filename: str = "",
) -> None:
    """Refuse a leftover or wrong-day CSV. Empty received_at is a trusted inject."""
    file_day = filename_day(filename)
    if file_day is not None and file_day != day:
        raise EmailReportStaleError(
            f"report {ingested.report_id}: email CSV filename date "
            f"{file_day.isoformat()} does not match requested {day.isoformat()}"
        )
    if not str(ingested.received_at or "").strip():
        return
    try:
        received = datetime.fromisoformat(ingested.received_at)
    except ValueError as exc:
        raise EmailReportStaleError(
            f"report {ingested.report_id}: email received_at is not ISO-8601: "
            f"{ingested.received_at!r}"
        ) from exc
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = now - received
    if age > MAX_EMAIL_AGE:
        raise EmailReportStaleError(
            f"report {ingested.report_id}: email CSV is stale "
            f"(received {ingested.received_at}, older than {MAX_EMAIL_AGE.total_seconds() / 3600:.0f}h)"
        )
    try:
        from zoneinfo import ZoneInfo

        received_day = received.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:
        received_day = received.date()
    if received_day != day:
        raise EmailReportStaleError(
            f"report {ingested.report_id}: email CSV day {received_day.isoformat()} "
            f"does not match requested {day.isoformat()}"
        )


def apply_4360_active_filter(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep only Current Policy Status = Active (4246 daily 4360 feed)."""
    kept: list[dict[str, str]] = []
    skipped = 0
    for row in rows:
        status = str(row.get(AUDIT_4360_STATUS_COLUMN) or "").strip()
        if status.casefold() != AUDIT_4360_ACTIVE_STATUS:
            skipped += 1
            continue
        kept.append(row)
    if not kept:
        raise GmailReportIngestionError(
            "report 4246: email CSV has no Active rows after the 4360 "
            f"Current Policy Status filter ({skipped} non-Active skipped); "
            "refusing to return an unfiltered or empty audit queue"
        )
    if skipped:
        logger.info("report 4246: skipped %d non-Active 4360 row(s)", skipped)
    return kept


def project_email_rows(
    report_id: str,
    rows: list[Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Copy validated email rows and add worker snake_case aliases.

    Dedupes on :func:`gmail_report_ingestion.identity_value` (Policy Number
    for 4246/4247/4372). First row wins. Does not invent missing values.
    """
    report_id = str(report_id).strip()
    aliases = COLUMN_ALIASES.get(report_id, {})
    working = [dict(row) for row in rows]
    if report_id == "4246":
        working = apply_4360_active_filter(working)

    emitted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in working:
        key = ing.identity_value(report_id, raw)
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, Any] = dict(raw)
        for source_header, dests in aliases.items():
            if source_header not in raw:
                continue
            value = raw[source_header]
            for dest in dests:
                row.setdefault(dest, value)
        row["_fetch_source"] = "gmail_email_csv"
        emitted.append(row)
    if not emitted:
        raise GmailReportIngestionError(
            f"report {report_id}: email CSV produced no work-item rows after "
            "identity dedupe; refusing to return an empty queue"
        )
    return emitted


def rows_from_csv_bytes(report_id: str, csv_bytes: bytes) -> list[dict[str, Any]]:
    """Parse injected CSV bytes with the Gmail schema gate, then project."""
    ing.check_report_gate(report_id)
    rows, _skipped = ing.parse_and_validate_csv(
        report_id, csv_bytes, source_label="injected"
    )
    return project_email_rows(report_id, rows)


def rows_from_ingested(
    ingested: IngestedReport,
    *,
    day: date,
    now: datetime | None = None,
    filename: str = "",
) -> list[dict[str, Any]]:
    assert_ingested_fresh(ingested, day, now=now, filename=filename)
    return project_email_rows(ingested.report_id, ingested.rows)


def ingest_report_from_gmail(
    service: Any,
    *,
    report_id: str,
    day: date,
    allow_unverified: bool = False,
) -> IngestedReport:
    ing.check_report_gate(report_id, allow_unverified=allow_unverified)
    got = ing.ingest_daily_reports(
        service,
        day=day,
        report_ids=[report_id],
        allow_unverified=allow_unverified,
    )
    return got[report_id]


def fetch_email_report_rows(
    *,
    report_id: str,
    fields: list[str] | None = None,
    csv_bytes: bytes | None = None,
    gmail_service: Any | None = None,
    ingested: IngestedReport | None = None,
    day: date | None = None,
    now: datetime | None = None,
    filename: str = "",
) -> list[dict[str, Any]]:
    """Return today's email CSV rows for ``report_id``.

    ``fields`` is accepted for signature parity with the Looker fetcher.
    Projection keeps original headers plus aliases; requested extras that
    the scheduled CSV never carried (underwriter_email, policy_aliases) are
    omitted rather than invented. Identity columns are always present.
    """
    report_id = str(report_id).strip()
    if report_id not in ing.EXPECTED_HEADERS:
        raise GmailReportIngestionError(f"unknown report id {report_id}")
    resolved_day = day or eastern_today(now)

    if ingested is not None:
        rows = rows_from_ingested(
            ingested, day=resolved_day, now=now, filename=filename
        )
    elif csv_bytes is not None:
        rows = rows_from_csv_bytes(report_id, csv_bytes)
    else:
        service = gmail_service if gmail_service is not None else build_default_gmail_service()
        loaded = ingest_report_from_gmail(
            service, report_id=report_id, day=resolved_day
        )
        rows = rows_from_ingested(
            loaded, day=resolved_day, now=now, filename=filename
        )

    if not any(str(row.get("policy_number") or "").strip() for row in rows):
        raise GmailReportIngestionError(
            f"report {report_id}: email CSV rows are missing policy_number "
            "after alias projection; refusing to return unkeyed rows"
        )
    logger.info(
        "report %s: ingested %d email CSV row(s) for %s (fields=%s)",
        report_id,
        len(rows),
        resolved_day.isoformat(),
        list(fields) if fields else None,
    )
    return rows
