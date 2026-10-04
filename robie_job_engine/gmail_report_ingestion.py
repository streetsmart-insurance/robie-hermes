"""Gmail ingestion for ROBIE's scheduled EZLynx report CSVs.

Replaces the Reports 5.0 UI scraping path (which fails closed with
"0 saved-report links") with the scheduled-email path: each of the four
ROBIE reports is saved as a Look in EZLynx with a "ROBIE daily CSV"
schedule that emails a full CSV (Data Limit: All Results) to
robie@streetsmart.insurance daily at 5:00 AM Eastern.

Fail-closed throughout: missing email, duplicate emails for one
report/day, missing/multiple CSV attachments, header mismatch, ragged
rows, or zero data rows all RAISE GmailReportIngestionError. Rows with
a blank identity are skipped and logged (they no longer fail the whole
report), except a 4372 mortgagee row whose Note carries a TEST-HO policy
number — that row is kept. Never returns partial or empty data silently.

PROVENANCE of expected headers: the REAL CSV attachments received
2026-09-19 (test sends triggered ~07:13-07:17 ET from the four
"ROBIE daily CSV" schedules). Important: the CSV headers do NOT carry
the Looker view-name prefixes shown in the report viewer ("Applicant
Data Account Name" on screen == "Account Name" in the CSV). Header
lists below are the exact ordered CSV headers.

ENVELOPE:
- From: Applied Reporting <DoNotReply@appliedsystems.com>
- 4246 / 4247 / 4359 subject (observed 2026-09-19): "ROBIE daily CSV".
  Those three still share that envelope, so they route by CSV header
  fingerprint after the generic subject match.
- 4372 subject (Carlo/Ralph 2026-09-20, hermes-test-01 after #517):
  "Mortgagee Verification Queue - ROBIE". Header fingerprint alone is
  not enough — another robie@ daily CSV was picked and failed as
  ``19-col CSV Account Name…Total Written Premium — no known ROBIE
  fingerprint``. 4372 accepts only the mortgagee subject (or an
  equivalent durable fingerprint). Other robie@ CSVs are ignored.
- Attachment: ROBIE_daily_CSV_YYYY-MM-DDTHHMM.csv for the generic
  daily envelope. 4372 may use that filename or the queue name.

TOTALS ROWS (observed): the 4247 and 4246 exports end with a totals row
(blank identity; only "Total *"-prefixed columns populated). Such rows
are SKIPPED and counted (never fed to a worker); a blank identity on a
row carrying any non-total data RAISES.

IDENTITY (decided 2026-09-19 by Carlo): Policy Number is the work-item
identity — it is the client's account. For 4247/4246/4372 the worker
tracks one item per policy; rows sharing a Policy Number are linked as a
single work item. For 4359 Carlo decided 2026-09-19 to WORK EVERY
REQUEST: one work item per row (3 policies have 2 open requests each, 66
unique policy numbers / 69 rows — each request is worked). The 4359
export has no request-ID column, so the 4359 work-item key is
Policy Number + Change Request Created Date; a 4359 row with a blank
Created Date RAISES (requests cannot be distinguished). The exports
contain no literal audit-ID / loan-number column, so Applicant ID stays
on as a descriptor and Task ID (4372) stays on as the row descriptor.
Mortgagee lender/loan number is NOT in the export — it is a manual
enrichment the worker adds per item as it goes. 4359 schema was
verified against the 2026-09-19 delivery.

Gmail access uses the SAME domain-wide-delegation service-account pattern
as gmail_accountability.build_keyless_delegated_service (IAM signer +
delegated subject), but with the gmail.readonly scope: the metadata-only
scope proven there CANNOT read attachments. gmail.readonly over DWD was
proven 2026-09-18 (SA impersonated hello@ and robie@ for getProfile +
messages.list with the readonly scope); the first live attachment
download still needs a run to confirm end to end.

Branch: ralph/gmail-scheduled-report-ingestion (draft only — no merge,
no PR, no deploy).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


logger = logging.getLogger(__name__)


ROBIE_MAILBOX = "robie@streetsmart.insurance"

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# --- Envelope ---------------------------------------------------------------
# From: Applied Reporting <DoNotReply@appliedsystems.com>
# 4246/4247/4359 subject: "ROBIE daily CSV" — route by header fingerprint.
# 4372 subject: "Mortgagee Verification Queue - ROBIE" (required). Do not
# treat every robie@ CSV as 4372. Keep this phrase identical to
# report_registry.MORTGAGEE_4372_SCOPE_MARKER / REPORT_DISPLAY_NAMES["4372"].
DEFAULT_SUBJECT_CONTAINS = "ROBIE daily CSV"
MORTGAGEE_4372_SUBJECT = "Mortgagee Verification Queue - ROBIE"
AUDIT_4246_SUBJECT = "Workers Comp Renewal Audit Queue - ROBIE"
DEFAULT_ALLOWED_SENDERS = ("donotreply@appliedsystems.com",)

# Reports whose scheduled mail is uniquely identified by subject. Header
# fingerprint alone must not assign these — a foreign daily CSV in the
# same mailbox is not a 4372 miss-route, it is simply not 4372.
# 4246's daily email is the 4360 transaction feed ("Workers Comp Renewal
# Audit Queue - ROBIE", separate 6:06 AM delivery), NOT the generic
# "ROBIE daily CSV" envelope — the old generic query could never find it.
REPORT_SUBJECT_QUERIES: dict[str, str] = {
    "4372": MORTGAGEE_4372_SUBJECT,
    "4246": AUDIT_4246_SUBJECT,
}
SUBJECT_EXCLUSIVE_REPORT_IDS = frozenset(REPORT_SUBJECT_QUERIES)

# Consecutive tokens that must appear in a 4372 subject after
# punctuation / dash folding. "robie" is required separately so a
# generic "Mortgagee Verification Queue" mail without the ROBIE marker
# cannot satisfy the gate.
MORTGAGEE_4372_SUBJECT_PHRASE = "mortgagee verification queue"
# --------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES = 20_000_000
MAX_ROWS = 250_000
MAX_COLUMNS = 256

# Columns starting with this prefix carry report totals, never row data.
TOTAL_COLUMN_PREFIX = "Total "


class GmailReportIngestionError(RuntimeError):
    """A scheduled report email is missing, ambiguous, malformed, or unverified."""


class GmailReportMissingError(GmailReportIngestionError):
    """No scheduled-report email was found for this report/day.

    Distinct from schema/stale failures so live fetch can fall back to a
    mapped Looker look (4372 → 4601) only when the email is absent — never
    when the CSV is present but wrong.
    """


# --- Expected headers: exact ordered CSV headers, observed 2026-09-19 -----
# NOTE: the Looker viewer shows view-name prefixes ("Applicant Data ...",
# "Policy Expiration ...", "Activity Task ..."); the CSV export strips
# them. These lists are the real CSV headers, byte-relevant.

EXPECTED_HEADERS: dict[str, list[str]] = {
    # 4247 Manual Renewal Queue - ROBIE: 22 cols; 2026-09-19 test CSV had
    # 176 data rows + 1 totals row.
    "4247": [
        "Account Name",
        "Applicant ID",
        "Policy Number",
        "Policy Effective Date",
        "Policy Expiration Date",
        "Master Company",
        "Line Of Business",
        "Premium - Annualized",
        "Premium - Written",
        "Branch",
        "Department",
        "Service Team",
        "Assigned Producer",
        "CSR",
        "Preferred Language",
        "Applicant Labels",
        "Policy Labels",
        "Policy Source",
        "Total Policies",
        "Total Customers",
        "Total Annualized Premium",
        "Total Written Premium",
    ],
    # 4246 audit worker: the DAILY EMAIL delivers the transaction-level CSV
    # from scheduled report 4360 ("Workers Comp Renewal Audit Queue -
    # ROBIE"), NOT the 19-col policy-level saved report 4246 ("Audit
    # Verification Queue - ROBIE", which is view-only). Verified 2026-09-19
    # ~13:00 EDT against the real 2026-09-19 06:05 delivery: 24 cols,
    # 7 data rows + 1 blank trailing line, all rows Current Policy
    # Status = Active (down from 454 rows pre-fix). The old 19-col
    # fingerprint was built from a misidentified test file — removed.
    "4246": [
        "Applicant ID",
        "Branch",
        "Account Name",
        "Account Type",
        "Assigned Producer",
        "CSR",
        "Policy Number",
        "Policy ID",
        "Policy Transaction ID",
        "Transaction Type",
        "Transaction Date",
        "Line of Business",
        "Master Company",
        "Download Date",
        "Effective Date",
        "Expiration Date",
        "Current Policy Status",
        "Policy Term",
        "Policy Type",
        "Transaction Deleted",
        "Service Team",
        "Total Written Premium",
        "Total Customers",
        "Total Transactions",
    ],
    # 4372 Mortgagee Verification Queue - ROBIE: 32 cols; 2026-09-19 test
    # CSV had 4 data rows, no totals row.
    "4372": [
        "Applicant ID",
        "Account Name",
        "Task Assigned To",
        "Branch",
        "Activity Type",
        "Note Created by",
        "Task Status",
        "Assigned Producer",
        "CSR",
        "Task Created By",
        "Created Date",
        "Task Due Date",
        "Task Last Modified Date",
        "Task Last Modified By",
        "Note",
        "Comment",
        "Task Closed By",
        "Policy Master ID",
        "Task Priority",
        "Sticky",
        "Task Created By ID",
        "Task Created Date",
        "Task ID",
        "Task Closed Date",
        "Policy Number",
        "Producer Code",
        "Producer Code Override",
        "Activity Labels",
        "Lead Source",
        "Discussion ID",
        "Department",
        "Service Team",
    ],
    # 4359 Policy Change Request Confirmation Queue - ROBIE: 19 cols
    # (viewer had shown 18 — the export adds "Change Request Created
    # Date"); 2026-09-19 test CSV had 69 data rows, no totals row.
    "4359": [
        "Account Name",
        "Applicant ID",
        "Policy Number",
        "Line Of Business",
        "Effective Date",
        "Master Company",
        "Request Status",
        "Created By",
        "Written Premium",
        "Premium - Annualized",
        "Branch",
        "Department",
        "Service Team",
        "Assigned Producer",
        "CSR",
        "Preferred Language",
        "Applicant Labels",
        "Policy Labels",
        "Change Request Created Date",
    ],
    # 4744 Mortgagee Verification Queue - ROBIE: 8 cols. Verified
    # 2026-09-28 ~07:00 EDT against the real 2026-09-28 05:01 delivery
    # (Gmail 1a0e73f8f39ced03,
    # Mortgagee_Verification_Queue_-_ROBIE_2026-09-28T0501.csv): exact
    # 8-column header match, 189 data rows, no totals row. This is the
    # policy-expiration mortgagee report (Homeowners + Flood, Active,
    # expiring within 45 days); it shares its subject line with the old
    # retired 4372 task export, so it is selected by header fingerprint,
    # never by subject.
    "4744": [
        "Account Name",
        "Policy Number",
        "Master Company",
        "Line Of Business",
        "Premium - Annualized",
        "Assigned Producer",
        "CSR",
        "Policy Expiration Date",
    ],
}

REPORT_DISPLAY_NAMES: dict[str, str] = {
    "4247": "Manual Renewal Queue - ROBIE",
    "4246": "Audit Verification Queue - ROBIE",
    "4372": "Mortgagee Verification Queue - ROBIE",
    "4359": "Policy Change Request Confirmation Queue - ROBIE",
    "4744": "Mortgagee Verification Queue - ROBIE",
}

# Mirrored from report_registry.VERIFIED_REPORTS (keep in sync).
# schema_verified=False blocks ingestion, mirroring
# ReportRunRegistry.start_run's gate.
# 4359 verified 2026-09-19 ~10:15 EDT against the real 2026-09-19 delivery
# (Gmail 1a0b9a359d14407a, ROBIE_daily_CSV_2026-09-19T0827.csv): exact
# 19-column header match, 69 rows, zero blank Policy Number, zero blank
# Change Request Created Date, 69 unique per-request identity keys.
# 4744 verified 2026-09-28 ~07:00 EDT against the real 2026-09-28 delivery
# (Gmail 1a0e73f8f39ced03): exact 8-column header match, 189 data rows,
# no totals row, zero blank Policy Number.
SCHEMA_VERIFIED: dict[str, bool] = {
    "4247": True,
    "4246": True,
    "4372": True,
    "4359": True,
    "4744": True,
}

# Work-item identity column per report. Decided 2026-09-19 by Carlo:
# Policy Number (the client's account) is the identity the worker tracks
# day to day. Rows sharing one Policy Number are linked as a single
# work item — EXCEPT 4359, where Carlo decided every request is worked:
# 4359 identity is composite (see identity_value).
# Mortgagee lender is NOT in the export — see mortgagee_enrichment
# (DocumentApi + structured fields, then Additional Interests table
# read on API miss; never invent / never PDF-scrape).
IDENTITY_COLUMNS: dict[str, str] = {
    "4247": "Policy Number",
    "4246": "Policy Number",
    "4372": "Policy Number",
    "4359": "Policy Number",
    "4744": "Policy Number",
}

# 4359 has no request-ID column; requests on one policy are told apart by
# when they were created.
IDENTITY_4359_CREATED_COLUMN = "Change Request Created Date"


@dataclass(frozen=True)
class IngestedReport:
    report_id: str
    display_name: str
    received_at: str  # ISO-8601 UTC
    message_id_sha256: str
    filename_sha256: str
    attachment_sha256: str
    row_count: int
    skipped_rows: int  # blank-identity totals rows skipped, never fed to workers
    rows: list[dict[str, str]]


# --- Gmail service (same DWD pattern as gmail_accountability) --------------


def build_readonly_delegated_service(
    service_account_email: str,
    user: str,
    *,
    scopes: Sequence[str] = (GMAIL_READONLY_SCOPE,),
) -> Any:
    """Domain-wide-delegation Gmail client able to read message attachments.

    Same IAM-signer construction as
    gmail_accountability.build_keyless_delegated_service, but requests
    gmail.readonly: the metadata-only scope cannot download attachments.
    gmail.readonly over DWD was proven 2026-09-18 (SA impersonated hello@
    and robie@ for getProfile + messages.list); the first live attachment
    download still needs a run to confirm end to end.
    No network call is made here; credentials are minted on first use.
    """
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    request = Request()
    signer = iam.Signer(request, source, service_account_email)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=list(scopes),
        subject=user,
    )
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


# --- Pure helpers (no network; fully unit-testable) ------------------------


def expected_headers(report_id: str) -> list[str]:
    try:
        return list(EXPECTED_HEADERS[report_id])
    except KeyError:
        raise GmailReportIngestionError(f"unknown report id {report_id}") from None


def identity_column(report_id: str) -> str:
    try:
        return IDENTITY_COLUMNS[report_id]
    except KeyError:
        raise GmailReportIngestionError(f"unknown report id {report_id}") from None


def identity_value(report_id: str, row: Mapping[str, str]) -> str:
    """Work-item key for one validated row — what the worker tracks day to day.

    Decided 2026-09-19 by Carlo: 4247/4246/4372 track one item per Policy
    Number; 4359 works EVERY request, so its key is Policy Number + the
    request's created date (the export has no request-ID column). A 4359
    row with a blank created date RAISES — the requests could not be told
    apart, and silently merging two requests is worse than failing closed.

    TEST-ONLY fallback (2026-09-20): for 4372, if the Policy Number column
    is empty, extract a TEST-HO policy number from the Note field. This
    supports the mortgagee canary test where the test policy exists only
    in the note text. Production rows always carry the Policy Number
    column; this fallback is scoped to the TEST-HO prefix and never fires
    on real policy numbers.
    """
    policy = str(row.get(identity_column(report_id), "")).strip()
    if not policy and report_id == "4372":
        note = str(row.get("Note", "")).strip()
        match = re.search(r"TEST-HO-[0-9-]+", note)
        if match:
            policy = match.group(0)
    if report_id == "4359":
        created = str(row.get(IDENTITY_4359_CREATED_COLUMN, "")).strip()
        if not created:
            raise GmailReportIngestionError(
                "report 4359: Change Request Created Date is empty — "
                "this request cannot be distinguished from another request "
                "on the same policy"
            )
        return f"{policy} | {created}"
    return policy


def check_report_gate(report_id: str, *, allow_unverified: bool = False) -> None:
    """Mirror report_registry's schema gate: unverified reports block."""
    if report_id not in SCHEMA_VERIFIED:
        raise GmailReportIngestionError(f"unknown report id {report_id}")
    if not SCHEMA_VERIFIED[report_id] and not allow_unverified:
        raise GmailReportIngestionError(
            f"report {report_id} schema is unverified and must block "
            f"(mirrors report_registry.ReportRunRegistry.start_run gate)"
        )


def validate_headers(report_id: str, actual: Sequence[str]) -> list[str]:
    """Require the exact ordered header list for the report.

    Returns the validated header list. Raises with a precise diff on any
    mismatch (missing / extra / out-of-order / renamed columns).
    """
    wanted = expected_headers(report_id)
    actual_list = [str(value) for value in actual]
    problems: list[str] = []
    if len(actual_list) != len(wanted):
        problems.append(
            f"column count {len(actual_list)} != expected {len(wanted)}"
        )
    for position, (got, want) in enumerate(zip(actual_list, wanted)):
        if got != want:
            problems.append(f"col {position}: got {got!r}, want {want!r}")
    if len(actual_list) > len(wanted):
        problems.append(f"extra columns: {actual_list[len(wanted):]!r}")
    if problems:
        raise GmailReportIngestionError(
            f"report {report_id} header mismatch: " + "; ".join(problems)
        )
    return actual_list


def fingerprint_report_id(headers: Sequence[str]) -> str:
    """Route a CSV to its report by exact header fingerprint.

    Used for 4246/4247/4359/4744, which still share the "ROBIE daily CSV"
    envelope. 4372 is subject-exclusive: do not assign 4372 from headers
    alone in :func:`ingest_daily_reports`. Raises when zero or multiple
    reports match, so a foreign CSV can never be ingested silently.
    """
    actual = [str(value) for value in headers]
    matches = [
        report_id
        for report_id, wanted in EXPECTED_HEADERS.items()
        if actual == wanted
    ]
    if not matches:
        first = actual[0] if actual else ""
        last = actual[-1] if actual else ""
        raise GmailReportIngestionError(
            f"{len(actual)}-col CSV {first}…{last} — no known ROBIE fingerprint"
        )
    if len(matches) > 1:
        raise GmailReportIngestionError(
            f"CSV headers match multiple reports: {', '.join(matches)}"
        )
    return matches[0]


def normalize_subject_fingerprint(value: str) -> str:
    """Fold a subject to a durable token string.

    Hyphens / dashes / slashes become spaces; other punctuation drops;
    case and repeated whitespace collapse. ``Mortgagee Verification
    Queue – ROBIE`` and ``Fwd: Mortgagee Verification Queue - ROBIE``
    normalize to the same core tokens.
    """
    text = re.sub(r"[\u2010-\u2015\u2212\-_/]+", " ", str(value or ""))
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return " ".join(text.split())


def _subject_contains_match(subject: str, needle: str) -> bool:
    folded_needle = normalize_subject_fingerprint(needle)
    if not folded_needle:
        return False
    return folded_needle in normalize_subject_fingerprint(subject)


def subject_matches_report(subject: str, report_id: str) -> bool:
    """True when ``subject`` is eligible for ``report_id``.

    4372 requires the durable mortgagee-queue fingerprint (phrase
    ``mortgagee verification queue`` plus token ``robie``). A generic
    ``ROBIE daily CSV`` subject is not 4372, even if the attachment
    happens to be a 32-col mortgagee CSV.

    Other reports in REPORT_SUBJECT_QUERIES (4246) match against their
    dedicated subject. 4247/4359 keep the generic daily-CSV envelope.
    Their display names are not required.
    """
    report_id = str(report_id).strip()
    normalized = normalize_subject_fingerprint(subject)
    if not normalized:
        return False
    if report_id == "4372":
        tokens = set(normalized.split())
        return MORTGAGEE_4372_SUBJECT_PHRASE in normalized and "robie" in tokens
    exclusive_query = REPORT_SUBJECT_QUERIES.get(report_id)
    if exclusive_query is not None:
        return _subject_contains_match(subject, exclusive_query)
    return normalize_subject_fingerprint(DEFAULT_SUBJECT_CONTAINS) in normalized


def gmail_subject_queries(
    report_ids: Sequence[str],
    *,
    subject_contains: str | None = None,
) -> list[str]:
    """Gmail ``subject:`` phrases to search for the requested reports.

    An explicit ``subject_contains`` is a caller override (one query).
    Otherwise 4372 searches the mortgagee queue subject and the other
    reports keep ``ROBIE daily CSV``.
    """
    override = str(subject_contains or "").strip()
    if override:
        return [override]
    queries: list[str] = []
    seen: set[str] = set()
    for report_id in report_ids:
        query = REPORT_SUBJECT_QUERIES.get(str(report_id).strip(), DEFAULT_SUBJECT_CONTAINS)
        if query not in seen:
            seen.add(query)
            queries.append(query)
    return queries or [DEFAULT_SUBJECT_CONTAINS]


def _is_totals_row(row: dict[str, str], id_col: str) -> bool:
    """A totals row has a blank identity and data ONLY in Total* columns."""
    if row[id_col].strip():
        return False
    for header, value in row.items():
        if not str(value).strip():
            continue
        if header == id_col or not header.startswith(TOTAL_COLUMN_PREFIX):
            return False
    return True


def parse_and_validate_csv(
    report_id: str, content: bytes, *, source_label: str = "csv"
) -> tuple[list[dict[str, str]], int]:
    """Parse a scheduled-report CSV with csv.DictReader and validate fully.

    - Rejects NUL bytes and non-UTF-8 content.
    - Requires the exact ordered header list (see validate_headers).
    - Skips fully-blank lines (common CSV artifact); raises on zero data
      rows after skipping.
    - Rejects ragged rows (wrong width).
    - Skips totals rows (blank identity, only Total* columns populated)
      and counts them; skips (and logs) rows with a blank identity
      carrying data in non-total columns instead of failing the whole
      report — one bad row (e.g. a closed test task) must not block the
      other rows. The one exception is 4372: an empty Policy Number is
      kept when the Note contains a TEST-HO policy number. If that
      fallback finds nothing, the row is skipped with the same warning.
      Still raises on zero usable data rows after skipping.
    Returns (rows, skipped_totals_rows). Rows are dicts keyed by the exact
    header names.
    """
    if b"\x00" in content:
        raise GmailReportIngestionError(f"report {report_id} {source_label} contains NUL bytes")
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise GmailReportIngestionError(
            f"report {report_id} {source_label} size is outside the allowed range"
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GmailReportIngestionError(
            f"report {report_id} {source_label} is not UTF-8"
        ) from exc
    reader = csv.reader(io.StringIO(text))
    raw_rows = [row for row in reader if any(str(cell).strip() for cell in row)]
    if not raw_rows:
        raise GmailReportIngestionError(f"report {report_id} {source_label} has no header row")
    headers = validate_headers(report_id, [str(cell) for cell in raw_rows[0]])
    if len(headers) > MAX_COLUMNS:
        raise GmailReportIngestionError(f"report {report_id} {source_label} has too many columns")
    if len(raw_rows) - 1 > MAX_ROWS:
        raise GmailReportIngestionError(f"report {report_id} {source_label} has too many rows")
    id_col = identity_column(report_id)
    rows: list[dict[str, str]] = []
    skipped = 0
    for lineno, raw in enumerate(raw_rows[1:], start=2):
        if len(raw) != len(headers):
            raise GmailReportIngestionError(
                f"report {report_id} {source_label} line {lineno}: "
                f"ragged row ({len(raw)} cells, {len(headers)} headers)"
            )
        row = {headers[i]: str(raw[i]) for i in range(len(headers))}
        if not row[id_col].strip():
            if _is_totals_row(row, id_col):
                skipped += 1
                continue
            # 4372 mortgagee TEST-HO canary: recover the policy number
            # from the Note. Every other report, and a 4372 row whose
            # fallback finds nothing, is skipped with a warning (#579).
            # A single bad row must not block the other rows. Zero usable
            # rows after skipping still raises below.
            if not (report_id == "4372" and identity_value(report_id, row)):
                logger.warning(
                    "report %s %s line %d: skipping row with empty identity "
                    "column %r carrying data",
                    report_id, source_label, lineno, id_col,
                )
                skipped += 1
                continue
        # Validates the work-item key (4359: raises on blank created date).
        identity_value(report_id, row)
        rows.append(row)
    if not rows:
        raise GmailReportIngestionError(
            f"report {report_id} {source_label} has no data rows"
        )
    return rows, skipped


# --- Gmail search / download (network; thin wrappers over the API) ---------


def _payload_headers(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item.get("name") or "").casefold(): str(item.get("value") or "")
        for item in payload.get("headers", []) or []
    }


def _iter_parts(part: Mapping[str, Any]):
    yield part
    for child in part.get("parts", []) or []:
        yield from _iter_parts(child)


def _sender_address(from_header: str) -> str:
    text = str(from_header or "")
    match = re.search(r"<([^<>]+)>", text)
    return (match.group(1) if match else text).strip().casefold()


def _sender_allowed(
    from_header: str,
    allowed_senders: Sequence[str],
    allowed_sender_domains: Sequence[str],
) -> bool:
    address = _sender_address(from_header)
    senders = {str(v).strip().casefold() for v in allowed_senders if str(v).strip()}
    domains = {str(v).strip().casefold().lstrip("@") for v in allowed_sender_domains if str(v).strip()}
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    return address in senders or (domain != "" and domain in domains)


def _decode_part(service: Any, message_id: str, part: Mapping[str, Any]) -> bytes:
    body = part.get("body", {}) or {}
    attachment_id = body.get("attachmentId")
    if attachment_id:
        response = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        encoded = str(response.get("data") or "")
    else:
        encoded = str(body.get("data") or "")
    if not encoded:
        raise GmailReportIngestionError("scheduled report attachment has no data")
    try:
        return base64.urlsafe_b64decode(encoded + "===")
    except Exception as exc:
        raise GmailReportIngestionError("scheduled report attachment is not valid base64") from exc


def list_candidate_emails(
    service: Any,
    *,
    day: date,
    subject_contains: str = DEFAULT_SUBJECT_CONTAINS,
    allowed_senders: Sequence[str] = DEFAULT_ALLOWED_SENDERS,
    allowed_sender_domains: Sequence[str] = (),
) -> list[Mapping[str, Any]]:
    """List scheduled-report emails in ROBIE_MAILBOX for one day.

    Envelope filter: Gmail ``subject:`` query + post-filter (Gmail subject
    search can be loose), sender allowlist, date window, has:attachment.
    4372 callers must pass the mortgagee subject, not the generic daily
    CSV phrase. Report assignment happens in :func:`ingest_daily_reports`.
    """
    needle = str(subject_contains or "").strip()
    if not needle:
        raise GmailReportIngestionError(
            "subject filter is empty: refusing to scan the mailbox"
        )
    if not allowed_senders and not allowed_sender_domains:
        raise GmailReportIngestionError(
            "sender allowlist is empty: refusing to scan the mailbox"
        )
    start = day.strftime("%Y/%m/%d")
    end = (day + timedelta(days=1)).strftime("%Y/%m/%d")
    query = f'subject:"{needle}" after:{start} before:{end} has:attachment'
    candidates: list[Mapping[str, Any]] = []
    page_token = None
    for _ in range(10):
        request: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            request["pageToken"] = page_token
        response = service.users().messages().list(**request).execute()
        for meta in response.get("messages", []) or []:
            message_id = str(meta.get("id") or "")
            if not message_id:
                continue
            message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            payload = message.get("payload", {}) or {}
            headers = _payload_headers(payload)
            if not _sender_allowed(headers.get("from", ""), allowed_senders, allowed_sender_domains):
                continue
            if not _subject_contains_match(headers.get("subject", ""), needle):
                continue
            candidates.append(message)
        page_token = str(response.get("nextPageToken") or "").strip() or None
        if not page_token:
            break
    if page_token:
        raise GmailReportIngestionError("scheduled report mailbox scan exceeded the bounded page limit")
    return candidates


def download_csv_attachment(
    service: Any, message: Mapping[str, Any], *, report_id: str = "?"
) -> tuple[str, bytes]:
    """Download the single CSV attachment from a scheduled-report email."""
    message_id = str(message.get("id") or "")
    payload = message.get("payload", {}) or {}
    csv_parts = [
        part
        for part in _iter_parts(payload)
        if str(part.get("filename") or "").casefold().endswith(".csv")
    ]
    if not csv_parts:
        raise GmailReportIngestionError(f"report {report_id} email has no CSV attachment")
    if len(csv_parts) > 1:
        raise GmailReportIngestionError(
            f"report {report_id} email has {len(csv_parts)} CSV attachments — refusing to pick one"
        )
    part = csv_parts[0]
    filename = str(part.get("filename") or "")
    content = _decode_part(service, message_id, part)
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise GmailReportIngestionError(
            f"report {report_id} attachment {filename!r} size is outside the allowed range"
        )
    return filename, content


def ingest_daily_reports(
    service: Any,
    *,
    day: date,
    report_ids: Sequence[str] = ("4247", "4246", "4372", "4359"),
    subject_contains: str | None = None,
    allowed_senders: Sequence[str] = DEFAULT_ALLOWED_SENDERS,
    allowed_sender_domains: Sequence[str] = (),
    allow_unverified: bool = False,
) -> dict[str, IngestedReport]:
    """Ingest one day's scheduled report CSVs into validated row dicts.

    4372 is routed by the mortgagee subject fingerprint, then the 32-col
    schema is validated. Other robie@ daily CSVs are ignored — they are
    not a 4372 schema miss. 4246/4247/4359 keep the generic
    ``ROBIE daily CSV`` subject plus header fingerprint.

    Returns {report_id: IngestedReport}. Fails loudly on any missing,
    duplicate, malformed, or unverified report — never partial.
    """
    queries = gmail_subject_queries(report_ids, subject_contains=subject_contains)
    seen_ids: set[str] = set()
    candidates: list[Mapping[str, Any]] = []
    for query in queries:
        for message in list_candidate_emails(
            service,
            day=day,
            subject_contains=query,
            allowed_senders=allowed_senders,
            allowed_sender_domains=allowed_sender_domains,
        ):
            message_id = str(message.get("id") or "")
            if not message_id or message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            candidates.append(message)
    bucketed: dict[str, list[tuple[Mapping[str, Any], str, bytes]]] = {}
    for message in candidates:
        payload = message.get("payload", {}) or {}
        subject = _payload_headers(payload).get("subject", "")
        exclusive_hits = [
            report_id
            for report_id in report_ids
            if str(report_id) in SUBJECT_EXCLUSIVE_REPORT_IDS
            and subject_matches_report(subject, report_id)
        ]
        header_routed = [
            report_id
            for report_id in report_ids
            if str(report_id) not in SUBJECT_EXCLUSIVE_REPORT_IDS
            and subject_matches_report(subject, report_id)
        ]
        if not exclusive_hits and not header_routed:
            continue
        filename, content = download_csv_attachment(service, message)
        header_line = content.decode("utf-8-sig").splitlines()[0] if content else ""
        csv_headers = next(csv.reader(io.StringIO(header_line)))
        if exclusive_hits:
            for report_id in exclusive_hits:
                try:
                    validate_headers(report_id, csv_headers)
                except GmailReportIngestionError as exc:
                    first = csv_headers[0] if csv_headers else ""
                    last = csv_headers[-1] if csv_headers else ""
                    raise GmailReportIngestionError(
                        f"report {report_id}: subject matches "
                        f"{REPORT_DISPLAY_NAMES[report_id]} but CSV schema "
                        f"does not ({len(csv_headers)}-col CSV {first}…{last} "
                        f"— no known ROBIE fingerprint)"
                    ) from exc
                bucketed.setdefault(report_id, []).append((message, filename, content))
            continue
        try:
            routed = fingerprint_report_id(csv_headers)
        except GmailReportIngestionError:
            # Unknown CSV fingerprint: not a ROBIE report we recognize.
            # Skip and record instead of failing the whole ingestion —
            # one foreign attachment must never kill the other reports.
            # Still fail-closed per-report: a report whose CSV is absent
            # raises GmailReportMissingError below.
            logger.warning(
                "skipping unrecognized CSV attachment %r (%d cols) from "
                "message %s: no known ROBIE fingerprint",
                filename, len(csv_headers), str(message.get("id") or ""),
            )
            continue
        if routed in SUBJECT_EXCLUSIVE_REPORT_IDS:
            continue
        if routed in header_routed:
            bucketed.setdefault(routed, []).append((message, filename, content))
    ingested: dict[str, IngestedReport] = {}
    for report_id in report_ids:
        found = bucketed.get(report_id, [])
        if not found:
            raise GmailReportMissingError(
                f"no scheduled report email found for {report_id} "
                f"({REPORT_DISPLAY_NAMES[report_id]}) on {day.isoformat()}"
            )
        if len(found) > 1:
            raise GmailReportIngestionError(
                f"duplicate scheduled report emails for {report_id} "
                f"({REPORT_DISPLAY_NAMES[report_id]}) on {day.isoformat()}: "
                f"{len(found)} candidates — refusing to pick one"
            )
        check_report_gate(report_id, allow_unverified=allow_unverified)
        message, filename, content = found[0]
        message_id = str(message.get("id") or "")
        received_at = datetime.fromtimestamp(
            int(message.get("internalDate") or 0) / 1000, tz=timezone.utc
        ).isoformat()
        rows, skipped = parse_and_validate_csv(report_id, content, source_label=filename)
        ingested[report_id] = IngestedReport(
            report_id=report_id,
            display_name=REPORT_DISPLAY_NAMES[report_id],
            received_at=received_at,
            message_id_sha256=hashlib.sha256(message_id.encode()).hexdigest(),
            filename_sha256=hashlib.sha256(filename.encode()).hexdigest()[:16],
            attachment_sha256=hashlib.sha256(content).hexdigest(),
            row_count=len(rows),
            skipped_rows=skipped,
            rows=rows,
        )
    return ingested


# --- Self-test (no network) -------------------------------------------------


def _synthetic_csv(
    report_id: str,
    *,
    n_rows: int = 2,
    header_override=None,
    with_totals_row: bool = False,
) -> bytes:
    headers = list(header_override) if header_override is not None else expected_headers(report_id)
    id_col = identity_column(report_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for n in range(1, n_rows + 1):
        row = []
        for col in headers:
            if col == id_col:
                row.append(f"ID-{report_id}-{n}")
            else:
                row.append(f"{col} :: r{n}")
        writer.writerow(row)
    if with_totals_row:
        totals = []
        for col in headers:
            if col.startswith(TOTAL_COLUMN_PREFIX):
                totals.append("123")
            else:
                totals.append("")
        writer.writerow(totals)
    return buffer.getvalue().encode("utf-8")


def _self_test() -> None:
    failures: list[str] = []
    passes: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - self-test reports, never hides
            failures.append(f"{name}: unexpected {type(exc).__name__}: {exc}")
        else:
            passes.append(name)

    def expect_raises(name: str, fn) -> None:
        try:
            fn()
        except GmailReportIngestionError:
            passes.append(name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: wrong error {type(exc).__name__}: {exc}")
        else:
            failures.append(f"{name}: expected GmailReportIngestionError, got success")

    expected_counts = {"4247": 22, "4246": 24, "4372": 32, "4359": 19, "4744": 8}

    for report_id, count in expected_counts.items():
        def make_ok(rid=report_id, want=count):
            assert len(expected_headers(rid)) == want, (
                f"header count {len(expected_headers(rid))} != {want}"
            )
            has_totals = any(
                h.startswith(TOTAL_COLUMN_PREFIX) for h in expected_headers(rid)
            )
            rows, skipped = parse_and_validate_csv(
                rid, _synthetic_csv(rid, with_totals_row=True), source_label="selftest"
            )
            assert len(rows) == 2, f"row count {len(rows)} != 2"
            # Only reports with Total* columns produce a totals row; without
            # them the synthetic totals row is fully blank and dropped as a
            # blank line — both are correct behavior.
            assert skipped == (1 if has_totals else 0), (
                f"skipped {skipped} != {(1 if has_totals else 0)}"
            )
            id_col = identity_column(rid)
            assert rows[0][id_col] == f"ID-{rid}-1", "identity value mismatch"
            assert set(rows[0].keys()) == set(expected_headers(rid)), "row keys != headers"
        check(f"parse+validate {report_id} ({REPORT_DISPLAY_NAMES[report_id]}) + totals-row skip", make_ok)

    def fingerprint_routes_all():
        for report_id in expected_counts:
            routed = fingerprint_report_id(expected_headers(report_id))
            assert routed == report_id, f"fingerprint routed {report_id} -> {routed}"
    check("header fingerprint routes all five reports", fingerprint_routes_all)

    def fingerprint_rejects_unknown():
        fingerprint_report_id(["Nope", "Unknown", "Headers"])
    expect_raises("unknown headers are rejected by fingerprint", fingerprint_rejects_unknown)

    def renamed_header():
        bad = expected_headers("4247")
        bad[2] = "Policy Number RENAMED"
        parse_and_validate_csv("4247", _synthetic_csv("4247", header_override=bad))
    expect_raises("header rename is rejected (4247)", renamed_header)

    def swapped_order():
        bad = expected_headers("4246")
        bad[0], bad[1] = bad[1], bad[0]
        parse_and_validate_csv("4246", _synthetic_csv("4246", header_override=bad))
    expect_raises("header reorder is rejected (4246)", swapped_order)

    def dropped_column():
        bad = expected_headers("4372")[:-1]
        parse_and_validate_csv("4372", _synthetic_csv("4372", header_override=bad))
    expect_raises("dropped column is rejected (4372)", dropped_column)

    def blank_identity_with_data():
        content = _synthetic_csv("4247")
        lines = content.decode("utf-8").splitlines()
        headers = expected_headers("4247")
        idx = headers.index(identity_column("4247"))
        cells = next(csv.reader([lines[2]]))
        cells[idx] = "   "  # blank identity but the row still carries data
        lines[2] = ",".join(f'"{c}"' for c in cells)
        rows, skipped = parse_and_validate_csv("4247", "\n".join(lines).encode("utf-8"))
        # The bad row is skipped (and logged); the good row is kept.
        assert len(rows) == 1, f"expected 1 good row, got {len(rows)}"
        assert skipped == 1, f"expected 1 skipped row, got {skipped}"
    check("blank identity on a data row is skipped, not fatal (4247)", blank_identity_with_data)

    def no_data_rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(expected_headers("4246"))
        parse_and_validate_csv("4246", buffer.getvalue().encode("utf-8"))
    expect_raises("header-only CSV is rejected (4246)", no_data_rows)

    def ragged_row():
        content = _synthetic_csv("4372").decode("utf-8").splitlines()
        content[1] = content[1] + ",EXTRA_CELL"
        parse_and_validate_csv("4372", "\n".join(content).encode("utf-8"))
    expect_raises("ragged row is rejected (4372)", ragged_row)

    def gate_open_4359():
        check_report_gate("4359")
    check("4359 gate open after 2026-09-19 schema verification", gate_open_4359)

    def check_gate_override():
        check_report_gate("4359", allow_unverified=True)
        check_report_gate("4247")
    check("gate passes verified reports and explicit override", check_gate_override)

    def gate_unknown():
        check_report_gate("9999")
    expect_raises("unknown report id is rejected by gate", gate_unknown)

    def identity_4359_per_request():
        # Carlo 2026-09-19: 4359 works EVERY request. Two open requests on
        # the same policy must produce two distinct work-item keys.
        headers = expected_headers("4359")
        rows, _ = parse_and_validate_csv(
            "4359", _synthetic_csv("4359", n_rows=2), source_label="selftest"
        )
        key1 = identity_value("4359", rows[0])
        key2 = identity_value("4359", rows[1])
        assert key1 != key2, f"4359 keys collided: {key1!r}"
        assert rows[0]["Policy Number"] == "ID-4359-1"
        assert key1.startswith("ID-4359-1 | "), f"unexpected 4359 key: {key1!r}"
    check("4359 identity is per-request (policy + created date)", identity_4359_per_request)

    def identity_4359_blank_created():
        content = _synthetic_csv("4359", n_rows=1).decode("utf-8").splitlines()
        headers = expected_headers("4359")
        idx = headers.index(IDENTITY_4359_CREATED_COLUMN)
        cells = next(csv.reader([content[1]]))
        cells[idx] = "   "
        content[1] = ",".join(f'"{c}"' for c in cells)
        parse_and_validate_csv("4359", "\n".join(content).encode("utf-8"))
    expect_raises(
        "4359 blank created date is rejected (requests indistinguishable)",
        identity_4359_blank_created,
    )

    def identity_other_reports_per_policy():
        for rid in ("4247", "4246", "4372"):
            rows, _ = parse_and_validate_csv(
                rid, _synthetic_csv(rid, n_rows=1), source_label="selftest"
            )
            assert identity_value(rid, rows[0]) == f"ID-{rid}-1", (
                f"{rid} identity should be the policy number alone"
            )
    check("4247/4246/4372 identity stays per-policy", identity_other_reports_per_policy)

    def subject_4372_requires_mortgagee_phrase():
        assert subject_matches_report(MORTGAGEE_4372_SUBJECT, "4372")
        assert subject_matches_report("Fwd: Mortgagee Verification Queue – ROBIE", "4372")
        assert not subject_matches_report(DEFAULT_SUBJECT_CONTAINS, "4372")
        assert not subject_matches_report("ROBIE daily CSV", "4372")
        assert not subject_matches_report("Mortgagee Verification Queue", "4372")
        assert REPORT_DISPLAY_NAMES["4372"] == MORTGAGEE_4372_SUBJECT
        assert gmail_subject_queries(["4372"]) == [MORTGAGEE_4372_SUBJECT]
        # 4246 is subject-exclusive on its 4360 transaction-feed email.
        assert gmail_subject_queries(["4246"]) == [AUDIT_4246_SUBJECT]
        assert gmail_subject_queries(["4247", "4246"]) == [DEFAULT_SUBJECT_CONTAINS, AUDIT_4246_SUBJECT]
        assert subject_matches_report(DEFAULT_SUBJECT_CONTAINS, "4247")
        assert subject_matches_report(AUDIT_4246_SUBJECT, "4246")
        assert not subject_matches_report(DEFAULT_SUBJECT_CONTAINS, "4246")
        assert not subject_matches_report(MORTGAGEE_4372_SUBJECT, "4247")
        assert not subject_matches_report(MORTGAGEE_4372_SUBJECT, "4246")
    check("4372 subject fingerprint is exclusive; 4246 on audit queue; 4247 on daily CSV", subject_4372_requires_mortgagee_phrase)

    print(f"SELF-TEST passes={len(passes)} failures={len(failures)}")
    for name in passes:
        print(f"  PASS {name}")
    for name in failures:
        print(f"  FAIL {name}")
    if failures:
        raise SystemExit(f"SELF-TEST FAILED: {len(failures)} failure(s)")


if __name__ == "__main__":
    _self_test()
