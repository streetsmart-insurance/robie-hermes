"""Verified producer email reports for overdue EZLynx submissions.

The worker fails closed before any email when the live Submission Center read,
approved employee roster, or producer-to-mailbox resolution is incomplete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .accountability_delivery import verify_delivery_receipts
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .submission_audit import (
    BoundedProcessError,
    ensure_ezlynx_login,
    run_submission_read,
)
from .ezlynx_session_lock import EzlynxSessionLockTimeout, exclusive_session
from .verification_common import is_action_authorized
from .verification_mailer import send_verification_email
from .runtime_env import TEST_ENV_NAME, current_robie_env
from .google_sheets_accountability import (
    SheetsRosterAccessError,
    classify_sheets_auth_error,
)


JOB_TYPE = "ezlynx.overdue_submission_reports"
ACTION = "send_producer_reports"
RESOURCE_ID = "ezlynx:submission-center:overview:submissions"
SUBJECT = "Action required: EZLynx submissions 31+ days overdue"
CC = ("carlo@streetsmart.insurance", "jake@streetsmart.insurance")
SOP_URL = "https://docs.google.com/document/d/1nggrFQY-q9PEDOjGcje04qYKUTx-qTGTx3Wv-4qD80M/edit"
CLOSED = {"Closed - Not Sold", "Closed - Bound"}
EXPECTED_RED = "rgb(211, 47, 47)"
AGENCY_EMAIL_SUFFIX = "@streetsmart.insurance"
logger = logging.getLogger(__name__)


class SubmissionReportContractError(RuntimeError):
    """Live source evidence did not satisfy the send contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_submission_observation(observed: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact live read contract without retaining applicant data."""
    exact = {
        "read_only": True,
        "source_status": "available",
        "scope_time_frame": "All Submissions",
        "scope_assigned_producer": "Streetsmart Insurance",
        "scope_my_submissions": False,
        "mat_row_count": 100,
        "pager_total_present": True,
        "status_aria_sort": "ascending",
        "first_row_non_closed": True,
        "day_31_qualifies": True,
        "headers_present": True,
        "email_delivery_enabled": False,
        "emails_sent": 0,
    }
    mismatches = [key for key, expected in exact.items() if observed.get(key) != expected]
    if mismatches:
        raise SubmissionReportContractError("live evidence mismatch: " + ", ".join(mismatches))

    records = observed.get("qualifying_records")
    count = observed.get("open_over_30_count")
    if not isinstance(records, list) or isinstance(count, bool) or not isinstance(count, int):
        raise SubmissionReportContractError("qualifying records/count are invalid")
    if count < 0 or len(records) != count:
        raise SubmissionReportContractError("qualifying record count did not reconcile")

    closed_boundary = observed.get("first_closed_row_inspected") is True
    exhausted_boundary = observed.get("full_dataset_exhausted") is True
    if closed_boundary == exhausted_boundary:
        raise SubmissionReportContractError("exactly one terminal boundary is required")
    pages = observed.get("pages_reviewed")
    pager_total = observed.get("pager_total")
    rows = observed.get("rows_inspected_through_boundary")
    non_closed = observed.get("non_closed_rows_inspected")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (pages, pager_total, rows, non_closed)):
        raise SubmissionReportContractError("pagination evidence is invalid")
    if rows > pager_total:
        raise SubmissionReportContractError("rows inspected exceeded pager total")
    if closed_boundary:
        if rows != non_closed + 1 or str(observed.get("first_closed_row_status") or "") not in CLOSED:
            raise SubmissionReportContractError("closed-row boundary did not reconcile")
    elif rows != pager_total or non_closed != pager_total:
        raise SubmissionReportContractError("exhausted boundary did not reconcile")

    urls: set[str] = set()
    producers: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for record in records:
        if not isinstance(record, Mapping):
            raise SubmissionReportContractError("qualifying record is invalid")
        producer = str(record.get("assigned_producer") or "").strip()
        status = str(record.get("status") or "").strip()
        url = str(record.get("submission_url") or "").strip()
        age = record.get("age_days")
        red = record.get("red_state_evidence")
        source_page = record.get("source_page")
        if not producer or producer.casefold() == "unassigned":
            raise SubmissionReportContractError("qualifying record has no assigned producer")
        if status in CLOSED:
            raise SubmissionReportContractError("closed record appeared in qualifying results")
        if not url.startswith("https://app.ezlynx.com/") or url in urls:
            raise SubmissionReportContractError("submission link is missing, foreign, or duplicated")
        if isinstance(age, bool) or not isinstance(age, int) or age <= 30:
            raise SubmissionReportContractError("day-31 threshold was not enforced")
        if not isinstance(red, Mapping) or red.get("overdue_class") is not True or red.get("computed_color") != EXPECTED_RED:
            raise SubmissionReportContractError("live red-overdue evidence is invalid")
        if isinstance(source_page, bool) or not isinstance(source_page, int) or not 1 <= source_page <= pages:
            raise SubmissionReportContractError("record source page is invalid")
        urls.add(url)
        producers[producer] += 1
        statuses[status] += 1
    if dict(producers) != dict(observed.get("counts_by_producer") or {}):
        raise SubmissionReportContractError("producer counts did not reconcile")
    if dict(statuses) != dict(observed.get("counts_by_status") or {}):
        raise SubmissionReportContractError("status counts did not reconcile")
    return {
        "verified": True,
        "qualifying_count": count,
        "producer_count": len(producers),
        "pages_reviewed": pages,
        "boundary_kind": "first_closed_row" if closed_boundary else "pager_exhausted",
        "evidence_sha256": _canonical_hash(observed),
    }


def _roster_skip_reason(name: str, raw: Mapping[str, Any] | None) -> str | None:
    """Return why an Active row is excluded from the agency email directory."""
    details = dict(raw or {})
    email = str(details.get("email") or "").strip().casefold()
    display = " ".join(str(name).split()) or "unnamed"
    extras = []
    for field in ("role", "department"):
        value = str(details.get(field) or "").strip()
        if value:
            extras.append(f"{field}={value}")
    extra = f"; {'; '.join(extras)}" if extras else ""
    if not email:
        return f"{display}: missing work email{extra}"
    if not email.endswith(AGENCY_EMAIL_SUFFIX):
        return f"{display}: non-agency work email ({email}){extra}"
    return None


def agency_email_directory_from_registry(registry: Mapping[str, Any]) -> dict[str, str]:
    """Keep Active @streetsmart.insurance mailboxes; skip missing or non-agency emails.

    External Producer / 1099 rows with personal mailboxes (for example gmail)
    are logged and excluded. The directory still fails closed when no usable
    agency emails remain after those skips.
    """
    if registry.get("source_status") != "available":
        raise SubmissionReportContractError("approved active-employee roster is unavailable or partial")
    directory: dict[str, str] = {}
    for name, raw in dict(registry.get("employees") or {}).items():
        skip_reason = _roster_skip_reason(name, raw)
        if skip_reason:
            logger.warning("Skipping Active roster row from agency email directory: %s", skip_reason)
            continue
        email = str((raw or {}).get("email") or "").strip().casefold()
        key = " ".join(str(name).casefold().split())
        if not key or key in directory:
            raise SubmissionReportContractError("approved roster contains an ambiguous employee name")
        directory[key] = email
    if not directory:
        raise SubmissionReportContractError("approved roster contains no active employees")
    return directory


def load_approved_producer_directory(manifest_path: str) -> dict[str, str]:
    """Fetch the current allowlisted employee roster from its approved Sheet."""
    path = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    config = dict(manifest.get("google_sheets") or {})
    if not config.get("enabled"):
        raise SubmissionReportContractError("approved active-employee roster is not enabled")
    from .google_sheets_accountability import collect_allowlisted_tables, role_registry_from_snapshot

    try:
        snapshot = collect_allowlisted_tables(config, as_of=datetime.now(timezone.utc))
        registry = role_registry_from_snapshot(snapshot, config)
    except SheetsRosterAccessError as exc:
        raise SubmissionReportContractError(str(exc)) from exc
    except SubmissionReportContractError:
        raise
    except Exception as exc:
        raise SubmissionReportContractError(str(classify_sheets_auth_error(exc))) from exc
    return agency_email_directory_from_registry(registry)


def resolve_recipients(records: list[Mapping[str, Any]], directory: Mapping[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for record in records:
        producer = str(record.get("assigned_producer") or "").strip()
        email = str(directory.get(" ".join(producer.casefold().split())) or "").strip()
        if not email:
            missing.append(producer or "Unassigned")
        else:
            resolved[producer] = email
    if missing:
        raise SubmissionReportContractError(
            "producer work email could not be resolved for: " + ", ".join(sorted(set(missing)))
        )
    return resolved


def build_producer_report(producer: str, records: list[Mapping[str, Any]]) -> str:
    lines = [
        f"Hi {producer},",
        "",
        "The following EZLynx submissions assigned to you are 31 or more days overdue and still open:",
        "",
        "Applicant | Status | Quote Due Date | Effective Date | Submission",
        "--- | --- | --- | --- | ---",
    ]
    for item in sorted(records, key=lambda row: (str(row.get("quote_due_date") or ""), str(row.get("applicant") or ""))):
        lines.append(
            " | ".join(
                (
                    str(item.get("applicant") or ""),
                    str(item.get("status") or ""),
                    str(item.get("quote_due_date") or ""),
                    str(item.get("effective_date") or ""),
                    str(item.get("submission_url") or ""),
                )
            )
        )
    lines.extend(
        [
            "",
            "Please open My Submissions, sort by Quote Due Date, and update each item. Close inactive opportunities as Closed - Not Sold or completed business as Closed - Bound.",
            "",
            f"Submission Center cleanup SOP: {SOP_URL}",
            "",
            "-ROBIE AI on behalf of Carlo",
        ]
    )
    return "\n".join(lines)


class OverdueSubmissionReportWorker:
    def __init__(
        self,
        *,
        audit_reader: Callable[[], dict[str, Any]] | None = None,
        directory_loader: Callable[[str], dict[str, str]] = load_approved_producer_directory,
        mailer: Callable[..., dict[str, Any]] = send_verification_email,
    ) -> None:
        self.audit_reader = audit_reader
        self.directory_loader = directory_loader
        self.mailer = mailer

    def _read(self) -> dict[str, Any]:
        if self.audit_reader is not None:
            return self.audit_reader()
        with exclusive_session():
            ensure_ezlynx_login()
            return run_submission_read(fresh=True)

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = dict(job.get("payload") or {})
        if not is_action_authorized(job, ACTION):
            return WorkerResult(False, JOB_TYPE, {}, retryable=False, error="producer email delivery is not authorized", hold_status=JobStatus.NEEDS_CLARIFICATION)
        manifest_path = str(payload.get("manifest_path") or "").strip()
        if not manifest_path:
            return WorkerResult(False, JOB_TYPE, {}, retryable=False, error="approved roster manifest_path is required", hold_status=JobStatus.NEEDS_CLARIFICATION)
        try:
            observed = self._read()
            audit_summary = validate_submission_observation(observed)
            records = list(observed.get("qualifying_records") or [])
            if not records:
                return WorkerResult(
                    True,
                    JOB_TYPE,
                    {"resource_id": RESOURCE_ID, "delivery_receipts": [], "producer_count": 0, "qualifying_count": 0},
                    {"audit_summary": audit_summary, "idempotency_key": idempotency_key},
                    retryable=False,
                )
            directory = self.directory_loader(manifest_path)
            recipients = resolve_recipients(records, directory)
            grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for record in records:
                grouped[str(record["assigned_producer"]).strip()].append(record)
            test_sink = ""
            if current_robie_env() == TEST_ENV_NAME:
                test_sink = os.environ.get("ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT", "").strip().casefold()
                if not test_sink.endswith("@streetsmart.insurance"):
                    raise SubmissionReportContractError(
                        "Test delivery requires an approved agency test recipient"
                    )
            # Test remaps To: to the sink only after every producer resolved
            # against the live roster. A Sheets/IAM miss is fail-closed — never
            # invent producer emails or a producers→carlo@ map.
            receipts: list[dict[str, Any]] = []
            for producer in sorted(grouped):
                subject = SUBJECT
                body = build_producer_report(producer, grouped[producer])
                to = [recipients[producer]]
                cc = list(CC)
                if test_sink:
                    producer_key = hashlib.sha256(producer.casefold().encode("utf-8")).hexdigest()[:10]
                    subject = f"TEST ONLY - {SUBJECT} - {producer_key}"
                    body = "TEST ONLY - no producer delivery\n\n" + body
                    to = [test_sink]
                    cc = []
                receipts.append(
                    self.mailer(
                        to=to,
                        cc=cc,
                        subject=subject,
                        text_body=body,
                        plain_only=True,
                    )
                )
        except EzlynxSessionLockTimeout:
            return WorkerResult(False, JOB_TYPE, {}, retryable=True, error="EZLYNX_SESSION_LOCK_TIMEOUT")
        except BoundedProcessError as exc:
            auth = exc.code in {"NEEDS_AUTH", "MAILBOX_IDENTITY_MISMATCH", "ROBIE_MAILBOX_AUTH_REQUIRED", "MFA_CODE_NOT_FOUND", "MFA_INPUT_NOT_FOUND", "MFA_SUBMIT_NOT_FOUND", "MFA_NOT_ACCEPTED", "AUTH_STATE_REQUIRES_USERNAME_LOGIN"}
            return WorkerResult(False, JOB_TYPE, {}, retryable=False, error=exc.code, hold_status=JobStatus.NEEDS_AUTH if auth else JobStatus.AWAITING_HUMAN_INPUT)
        except SubmissionReportContractError as exc:
            return WorkerResult(False, JOB_TYPE, {}, retryable=False, error=str(exc), hold_status=JobStatus.NEEDS_CLARIFICATION)
        except Exception as exc:
            return WorkerResult(False, JOB_TYPE, {}, retryable=False, error=f"producer report delivery failed: {type(exc).__name__}: {exc}", hold_status=JobStatus.AWAITING_HUMAN_INPUT)
        return WorkerResult(
            True,
            JOB_TYPE,
            {"resource_id": RESOURCE_ID, "delivery_receipts": receipts, "producer_count": len(grouped), "qualifying_count": len(records)},
            {"audit_summary": audit_summary, "idempotency_key": idempotency_key},
            retryable=False,
        )


class OverdueSubmissionReportVerifier:
    def __init__(self, *, delivery_readback: Callable[[list[dict[str, Any]]], tuple[bool, list[dict[str, Any]]]] = verify_delivery_receipts, audit_reader: Callable[[], dict[str, Any]] | None = None) -> None:
        self.delivery_readback = delivery_readback
        self.audit_reader = audit_reader

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        receipts = list(destination.get("delivery_receipts") or [])
        expected_count = int(destination.get("producer_count") or 0)
        qualifying_count = int(destination.get("qualifying_count") or 0)
        try:
            if expected_count:
                unique_ids = {str(item.get("message_id") or "") for item in receipts}
                delivery_ok, observed_receipts = self.delivery_readback(receipts)
                mailbox_ok = bool(observed_receipts) and all(
                    bool(item.get("exists_in_sent_mailbox") or item.get("exists"))
                    for item in observed_receipts
                )
                verified = (
                    len(receipts) == expected_count
                    and len(unique_ids) == expected_count
                    and "" not in unique_ids
                    and delivery_ok
                    and mailbox_ok
                )
                expected = {
                    "resource_id": RESOURCE_ID,
                    "exists_in_sent_mailbox": True,
                    "gmail_receipt_count": expected_count,
                    "producer_count": expected_count,
                    "qualifying_count": qualifying_count,
                }
                observed = {
                    "resource_id": RESOURCE_ID,
                    "exists_in_sent_mailbox": verified,
                    "gmail_receipt_count": len(receipts),
                    "producer_count": expected_count,
                    "qualifying_count": qualifying_count,
                    "unique_message_ids": len(unique_ids),
                    "delivery": observed_receipts,
                }
            else:
                if self.audit_reader is None:
                    with exclusive_session():
                        ensure_ezlynx_login()
                        current = run_submission_read(fresh=True)
                else:
                    current = self.audit_reader()
                summary = validate_submission_observation(current)
                verified = summary["qualifying_count"] == 0 and not receipts
                expected = {
                    "resource_id": RESOURCE_ID,
                    "qualifying_count": 0,
                    "gmail_receipt_count": 0,
                }
                observed = {
                    "resource_id": RESOURCE_ID,
                    "qualifying_count": summary["qualifying_count"],
                    "gmail_receipt_count": len(receipts),
                    "fresh_qualifying_count": summary["qualifying_count"],
                }
            error = None if verified else "Gmail delivery or zero-result source read-back did not verify"
        except Exception as exc:
            expected = {
                "resource_id": RESOURCE_ID,
                "exists_in_sent_mailbox": True,
                "gmail_receipt_count": expected_count,
                "producer_count": expected_count,
                "qualifying_count": qualifying_count,
            }
            observed = {"error": f"{type(exc).__name__}: {exc}"}
            verified = False
            error = "producer report destination could not be independently verified"
        evidence = VerificationEvidence(
            method="GMAIL_SENT_READBACK" if expected_count else "EZLYNX_PLAYWRIGHT_FRESH_READBACK",
            source="gmail-delegated-sent-mailbox" if expected_count else "ezlynx-authenticated-playwright",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=_utc_now(),
            locator=RESOURCE_ID,
        )
        return VerificationResult(verified, evidence, retryable=False, error=error)
