"""Mortgagee verification worker (job type ``mortgagee_verification``).

Builds on the Home/Flood renewal mortgagee SOP (``.agents/skills/`` SOP;
rebuild brief sections 2.3/3.3). The worker:

1. Fetches EZLynx report 4372 (Home Flood Renewal Queue - ROBIE) rows.
2. Filters LOB in {Homeowners, Flood}; buckets by days-to-expiration:
   >45d waits, 30-45d is the work window, <30d is an overdue exception.
3. Enforces the PRODUCER GATE: nothing is ever sent to a lender without a
   recorded ``producer_review_complete`` for that policy.
4. Routes by payer: mortgagee-paid -> verify lender inputs, verify the lender
   of record against a portal agent-section loan lookup (stale servicer data
   must never receive another lender's documents), upload dec+invoice after
   producer clearance over the portal's AGENT section (no login, no SSN) or —
   when there is no lender portal — place a Bland voice call to the MORTGAGE
   COMPANY (never the borrower/client); weekly payment monitoring to confirm
   the mortgagee payment for the renewal term. Insured-paid -> CSR handoff.
5. Escalates to CSR when payment is unconfirmed at <=20 days to expiration.
6. Records login gaps (portal unexpectedly demands credentials) via
   ``record_login_gap`` so Carlo can schedule a walkthrough.

CARDINAL RULE: ROBIE must never delete a policy. This module contains no
deletion, no payment movement, and no bind logic. Every outbound action goes
through ``is_action_authorized()``; anything unauthorized becomes a pending
intent, never an execution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult

# ---------------------------------------------------------------------------
# Imports provided by sibling agents (fail-closed when unavailable).
# ---------------------------------------------------------------------------

try:  # Sibling-built report row fetcher; exact signature per contract.
    from .report_fetcher import fetch_report_rows  # type: ignore
    _REPORT_FETCHER_AVAILABLE = True
except ImportError:  # pragma: no cover - sibling module not yet landed
    _REPORT_FETCHER_AVAILABLE = False

    def fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None, session=None):  # type: ignore
        raise NotImplementedError(
            "report_fetcher.fetch_report_rows is not yet installed; "
            "mortgagee_verification cannot fetch report 4372"
        )

try:  # Sibling-built verification commons.
    from . import verification_common as _verification_common  # type: ignore[no-redef]
    from .verification_common import (  # type: ignore
        PolicyOutcome,
        is_action_authorized,
        record_login_gap,
    )
    _VERIFICATION_COMMON_AVAILABLE = True
except ImportError:  # pragma: no cover - sibling module not yet landed
    _verification_common = None  # type: ignore[assignment]
    _VERIFICATION_COMMON_AVAILABLE = False

    @dataclass
    class PolicyOutcome:  # type: ignore[no-redef]
        """Per-policy outcome; mirrors the four-worker design doc schema."""

        policy_number: str
        policy_aliases: list = field(default_factory=list)
        applicant_id: str | None = None
        insured_name: str | None = None
        department: str | None = None
        carrier: str | None = None
        status: str = "pending"
        reason: str = ""
        actions_taken: list = field(default_factory=list)
        waiting_on: str | None = None
        evidence: dict = field(default_factory=dict)
        updated_at: str = ""

    def record_login_gap(store, job_id, job_type, portal_name, step, whats_missing):  # type: ignore
        raise NotImplementedError("verification_common.record_login_gap not installed")

    def is_action_authorized(job: dict, action_name: str) -> bool:  # type: ignore
        """Fail closed: without the authorizer nothing outbound is allowed."""
        return False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_TYPE = "mortgagee_verification"
WORKER_NAME = "mortgagee-verification"
DURABLE_NAMESPACE = "mortgagee_verification"
REPORT_ID_4372 = "4372"

ALLOWED_LOBS = {"Homeowners", "Flood"}
WINDOW_MIN_DAYS = 30
WINDOW_MAX_DAYS = 45
PAYMENT_ESCALATION_DAYS = 20
PAYMENT_CHECK_CADENCE_DAYS = 7

NOTE_SIGNATURE = "ROBIE was here"

# Lender portals usually require NO login: the agent-section loan lookup uses
# loan number + identifiers (borrower name, property ZIP) — never SSN.
LENDER_PORTAL_NEEDS_LOGIN_DEFAULT = False

OUTCOME_STATUSES = {"done", "not_done", "pending"}

# Durable per-policy keys for the producer gate / payment monitoring.
_REVIEW_COMPLETE_KEY = "producer_review_complete"
_LAST_PAYMENT_CHECK_KEY = "last_payment_check"
_NEXT_PAYMENT_CHECK_KEY = "next_payment_check"
_PAYMENT_CONFIRMED_KEY = "payment_confirmed"
_CSR_LOOPED_IN_KEY = "csr_looped_in"

_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


# ---------------------------------------------------------------------------
# Pure, unit-testable functions
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def days_to_expiration(expiration: Any, *, today: date | None = None) -> int | None:
    """Whole days from ``today`` until expiration; None when unparseable."""
    day = _parse_date(expiration)
    if day is None:
        return None
    ref = today or date.today()
    return (day - ref).days


def bucket_window(days: int | None) -> str:
    """Bucket a policy by days-to-expiration.

    Returns "in_window" (30-45d, work now), "not_yet_in_window" (>45d, waits),
    "overdue" (<30d, overdue exception), or "unknown" (unparseable date).
    """
    if days is None:
        return "unknown"
    if days > WINDOW_MAX_DAYS:
        return "not_yet_in_window"
    if days < WINDOW_MIN_DAYS:
        return "overdue"
    return "in_window"


def is_policy_stopped(policy_state: dict[str, Any]) -> tuple[bool, str]:
    """True when normal delivery must stop (cancelled / nonrenewed / inactive)."""
    status = str(policy_state.get("policy_status") or policy_state.get("status") or "").strip().lower()
    cancelled = bool(policy_state.get("cancelled") or policy_state.get("cancellation_date"))
    nonrenewed = bool(policy_state.get("nonrenewed") or policy_state.get("non_renewal"))
    inactive = status in {
        "cancelled", "canceled", "nonrenewed", "non-renewed", "inactive",
        "terminated", "void", "expired",
    }
    if cancelled or inactive or nonrenewed:
        return True, (
            "EXCLUDED_INACTIVE_ACCOUNT: policy is cancelled/nonrenewed/inactive; "
            "normal lender delivery stopped"
        )
    return False, ""


def producer_gate(policy_state: dict[str, Any]) -> tuple[bool, str]:
    """Producer clearance before any lender delivery.

    Returns (clear, reason). Fails CLOSED: when the review state is unknown or
    any coverage/premium/UW/retention review is pending, delivery is blocked.
    """
    if bool(policy_state.get(_REVIEW_COMPLETE_KEY)):
        return True, "producer review recorded complete"
    pending: list[str] = []
    review = policy_state.get("review_pending")
    if isinstance(review, dict):
        pending = [str(k) for k, v in review.items() if v]
    elif isinstance(review, (list, tuple)):
        pending = [str(v) for v in review]
    else:
        for flag in ("coverage_review_pending", "premium_review_pending",
                     "uw_review_pending", "retention_review_pending"):
            if policy_state.get(flag):
                pending.append(flag)
    if pending:
        return False, (
            "producer review pending (%s); lender delivery blocked until "
            "producer_review_complete is recorded" % ", ".join(pending)
        )
    return False, (
        "producer review state unknown; lender delivery blocked until "
        "producer_review_complete is recorded"
    )


def verify_lender(
    mortgage_company: Any,
    loan_number: Any,
    property_zip: Any,
) -> tuple[bool, str]:
    """Verify lender identity inputs. Fails closed on any missing/invalid."""
    company = str(mortgage_company or "").strip()
    loan = str(loan_number or "").strip()
    zip_code = str(property_zip or "").strip()
    missing = [
        name
        for name, value in (
            ("mortgage_company", company),
            ("loan_number", loan),
            ("property_zip", zip_code),
        )
        if not value
    ]
    if missing:
        return False, f"lender verification failed: missing {', '.join(missing)}"
    if not _ZIP_RE.match(zip_code):
        return False, "lender verification failed: property ZIP is invalid"
    return True, "lender verified (mortgage company, loan#, property ZIP all present)"


_LENDER_SUFFIX_STOPWORDS = frozenset({
    "inc", "incorporated", "llc", "co", "company", "corp", "corporation",
    "ltd", "limited", "mortgage", "mortgages", "servicing", "services",
    "bank", "financial", "group", "holdings", "na", "n-a",
})


def _normalize_lender_name(name: Any) -> str:
    """Normalize a lender/servicer name for comparison (case, punctuation,
    and common corporate suffixes stripped)."""
    text = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower())
    tokens = [t for t in text.split() if t not in _LENDER_SUFFIX_STOPWORDS]
    return " ".join(tokens)


def verify_lender_of_record(
    row_lender: Any,
    loan_number: Any,
    property_zip: Any,
    portal_lookup_result: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Verify the lender of record before uploading ANYTHING.

    The mortgage company on file may be stale/changed. ``portal_lookup_result``
    is the agent-section loan lookup from the lender portal (no login, loan# +
    borrower name + property ZIP). On mismatch this returns False and the
    caller must NOT upload — flag for CSR review instead. Safety invariant on
    par with the producer gate.
    """
    if portal_lookup_result is None:
        return False, (
            "lender of record not yet verified: portal agent-section loan lookup "
            "not performed (loan# + borrower name + property ZIP; no login, no SSN)"
        )
    if not isinstance(portal_lookup_result, dict):
        return False, (
            "lender of record verification failed: portal lookup result is not a mapping"
        )
    row_loan = str(loan_number or "").strip()
    portal_loan = str(portal_lookup_result.get("loan_number") or "").strip()
    if row_loan and portal_loan and row_loan != portal_loan:
        return False, (
            f"lender of record mismatch — row loan# {row_loan!r} vs portal {portal_loan!r}; "
            "not uploading to wrong lender"
        )
    portal_lender = str(
        portal_lookup_result.get("servicer")
        or portal_lookup_result.get("lender")
        or portal_lookup_result.get("mortgage_company")
        or ""
    ).strip()
    if not portal_lender:
        return False, (
            "lender of record verification failed: portal lookup returned no servicer/lender"
        )
    row_norm = _normalize_lender_name(row_lender)
    portal_norm = _normalize_lender_name(portal_lender)
    if not row_norm:
        return False, (
            "lender of record verification failed: mortgage company on file is empty"
        )
    if row_norm != portal_norm and row_norm not in portal_norm and portal_norm not in row_norm:
        return False, (
            f"lender of record mismatch — row shows {str(row_lender).strip()!r} but "
            f"portal shows {portal_lender!r}; not uploading to wrong lender"
        )
    return True, f"lender of record confirmed ({portal_lender})"


def _normalize_phone_us(value: Any) -> str | None:
    """Normalize to E.164-ish US format; None when not a valid US number."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return "+1" + digits


def build_mortgagee_voice_intent(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Bounded Bland voice-call intent to the MORTGAGE COMPANY only.

    Returns (intent, reason). The intent is None — the call must NOT be placed —
    when there is no mortgage-company number on file, the number is invalid, or
    the number matches a borrower/client number. These workers never dial
    clients/borrowers; numbers are never invented.
    """
    payer_info = row.get("payer_info") if isinstance(row.get("payer_info"), dict) else {}
    phone = row.get("mortgage_company_phone") or (payer_info or {}).get("mortgage_company_phone")
    borrower_numbers: list[Any] = []
    for key in ("borrower_phones", "borrower_phone", "insured_phone", "client_phone"):
        value = row.get(key) or (payer_info or {}).get(key)
        if isinstance(value, (list, tuple)):
            borrower_numbers.extend(value)
        elif value:
            borrower_numbers.append(value)
    borrower_digits = {_normalize_phone_us(n) for n in borrower_numbers}
    borrower_digits.discard(None)
    if not phone:
        return None, (
            "mortgage-company voice call blocked: no mortgage-company phone on file; "
            "never inventing a number and never dialing the borrower/client"
        )
    e164 = _normalize_phone_us(phone)
    if e164 is None:
        return None, (
            "mortgage-company voice call blocked: mortgage-company phone is not a valid US number"
        )
    if e164 in borrower_digits:
        return None, (
            "mortgage-company voice call REFUSED: number matches a borrower/client phone; "
            "these workers never dial clients"
        )
    company = str(row.get("mortgage_company") or (payer_info or {}).get("mortgage_company") or "").strip()
    return {
        "action": "place_mortgagee_voice_call",
        "provider": "bland",
        "to": e164,
        "to_name": company,
        "purpose": "confirming mortgagee payment for renewal term",
        "never_dial_borrower": True,
        "context": {
            "policy_number": str(row.get("policy_number") or ""),
            "loan_number": str(row.get("loan_number") or ""),
            "borrower_name": str(row.get("insured_name") or ""),
            "property_zip": str(row.get("property_zip") or row.get("zip") or ""),
            "carrier": str(row.get("carrier") or ""),
        },
    }, "mortgage-company voice intent built"


def route_payer(payer_info: dict[str, Any]) -> tuple[str, str]:
    """Three-way payer routing: "mortgagee" | "insured" | "unknown"."""
    payer = str(payer_info.get("payer") or "").strip().lower()
    if payer in {"mortgagee", "lender", "mortgage"}:
        return "mortgagee", "mortgagee pays"
    if payer in {"insured", "client", "customer"}:
        return "insured", "insured (client) pays"
    return "unknown", "payer not identified"


def next_payment_check_date(last_check: date | datetime | str | None,
                            *, today: date | None = None) -> date:
    """Next weekly payment check: 7 days after the last one (or today)."""
    ref = _parse_date(last_check)
    if ref is None:
        ref = today or date.today()
    return ref + timedelta(days=PAYMENT_CHECK_CADENCE_DAYS)


def payment_check_due(last_check: Any, *, today: date | None = None) -> tuple[bool, date]:
    """Whether a weekly payment check is due; also returns the next check date."""
    next_check = next_payment_check_date(last_check, today=today)
    ref = today or date.today()
    return next_check <= ref, next_check


def escalation_decision(
    days: int | None,
    payment_confirmed: bool,
    csr_looped_in: bool,
) -> tuple[bool, str]:
    """Escalate when payment is unconfirmed at <=20 days to expiration."""
    if payment_confirmed or csr_looped_in:
        return False, ""
    if days is None:
        return False, ""
    if days <= PAYMENT_ESCALATION_DAYS:
        return True, (
            f"payment unconfirmed {days}d before expiration "
            f"(threshold {PAYMENT_ESCALATION_DAYS}d): loop CSR into the same discussion"
        )
    return False, ""


def build_discussion_note(policy_number: str, lob: str, carrier: str, body: str) -> str:
    """Thread into the existing Homeowners/Flood Renewal discussion."""
    header = f"Policy: #{policy_number} ({lob} - {carrier})"
    return f"{header}\n\n{body.strip()}\n\n{NOTE_SIGNATURE}"


# ---------------------------------------------------------------------------
# Durable per-policy state (namespace "mortgagee_verification")
# ---------------------------------------------------------------------------


def _ledger_for_job(job: dict[str, Any], store: Any | None = None):
    """DurableWorkLedger for this job, or None when no durable DB is known."""
    from .idempotency import DurableWorkLedger

    payload = dict(job.get("payload") or {})
    db_path = (
        payload.get("db_path")
        or payload.get("jobs_db_path")
        or getattr(store, "path", None)
        or os.environ.get("ROBIE_JOB_DB")
    )
    if not db_path:
        return None
    return DurableWorkLedger(str(db_path))


def _work_item_key(policy_identity: str) -> str:
    return f"{DURABLE_NAMESPACE}:{policy_identity}"


def _policy_identity(row: dict[str, Any]) -> str:
    """Stable per-policy key: 4372 identity field (loan_number), else policy #."""
    loan = str(row.get("loan_number") or "").strip()
    if loan:
        return loan
    return str(row.get("policy_number") or "").strip()


def _read_policy_state(ledger, work_item_key: str) -> dict[str, Any]:
    if ledger is None:
        return {}
    try:
        item = ledger.get(DURABLE_NAMESPACE, work_item_key)
    except KeyError:
        return {}
    outcome = item.get("outcome")
    try:
        data = json.loads(outcome) if outcome else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_policy_state(ledger, work_item_key: str, state: dict[str, Any]) -> None:
    """Upsert the durable per-policy JSON state (never through leases here)."""
    if ledger is None:
        return
    ledger.reserve(DURABLE_NAMESPACE, work_item_key)
    payload = json.dumps(state, default=str)
    stamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(ledger.path, timeout=30, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            "UPDATE durable_work_items SET outcome=?, updated_at=? "
            "WHERE namespace=? AND work_item_key=?",
            (payload, stamp, DURABLE_NAMESPACE, work_item_key),
        )


def _merge_policy_state(ledger, work_item_key: str, updates: dict[str, Any]) -> dict[str, Any]:
    state = _read_policy_state(ledger, work_item_key)
    state.update(updates)
    _write_policy_state(ledger, work_item_key, state)
    return state


def _record_login_gap(
    job: dict[str, Any],
    db_path: str | None,
    *,
    portal_name: str,
    step: str,
    whats_missing: str,
) -> dict[str, Any]:
    """Record a lender-portal login gap for Carlo's walkthrough scheduling.

    Uses ``verification_common.record_login_gap`` when installed; always falls
    back to a worker-side record so the gap is never silently dropped.
    """
    gap: dict[str, Any] = {
        "job_id": job.get("id"),
        "job_type": JOB_TYPE,
        "portal_name": portal_name,
        "step": step,
        "whats_missing": whats_missing,
        "recorded_at": _utcnow(),
        "recorded_via": "worker-fallback",
    }
    try:
        store = None
        if db_path:
            from .store import JobStore

            store = JobStore(str(db_path))
        result = record_login_gap(store, job.get("id"), JOB_TYPE, portal_name, step, whats_missing)
        if isinstance(result, dict):
            gap.update(result)
        gap["recorded_via"] = "verification_common.record_login_gap"
    except Exception as exc:  # fail closed: keep the fallback record
        gap["record_error"] = f"{type(exc).__name__}: {exc}"
    return gap


def _payment_monitoring_outcome(
    row: dict[str, Any],
    job: dict[str, Any],
    ledger,
    today: date,
    work_item_key: str,
    prior_state: dict[str, Any],
    days: int | None,
    *,
    delivery_actions: list[str],
    delivery_evidence: dict[str, Any],
    actions_prefix: list[str],
) -> dict[str, Any]:
    """Shared tail after a mortgagee delivery/voice intent: weekly payment
    checks (confirming the mortgagee payment for the renewal term), CSR
    escalation when payment is unconfirmed at <=20 days to expiration."""
    last_check = prior_state.get(_LAST_PAYMENT_CHECK_KEY)
    payment_confirmed = bool(prior_state.get(_PAYMENT_CONFIRMED_KEY))
    csr_looped = bool(prior_state.get(_CSR_LOOPED_IN_KEY))
    escalate, esc_reason = escalation_decision(days, payment_confirmed, csr_looped)
    next_check = next_payment_check_date(last_check, today=today)
    _merge_policy_state(
        ledger, work_item_key,
        {
            _REVIEW_COMPLETE_KEY: True,
            _LAST_PAYMENT_CHECK_KEY: today.isoformat(),
            _NEXT_PAYMENT_CHECK_KEY: next_check.isoformat(),
            _CSR_LOOPED_IN_KEY: csr_looped or escalate,
        },
    )
    payment = {
        "confirmed": False,
        "purpose": "confirming mortgagee payment for renewal term",
        "last_payment_check": today.isoformat(),
        "next_check": next_check.isoformat(),
    }
    evidence = {**delivery_evidence, "payment": payment}
    if escalate:
        return _outcome_dict(
            row, status="pending", waiting_on="csr",
            reason=esc_reason,
            actions_taken=[*actions_prefix, *delivery_actions, "csr_looped_in"],
            evidence=evidence,
        )
    return _outcome_dict(
        row, status="pending", waiting_on="mortgagee",
        reason=("confirming mortgagee payment for renewal term: "
                f"weekly payment check scheduled (next {next_check.isoformat()})"),
        actions_taken=[*actions_prefix, *delivery_actions],
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Per-policy outcome construction
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _outcome_dict(
    row: dict[str, Any],
    *,
    status: str,
    reason: str,
    actions_taken: list[str] | None = None,
    waiting_on: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "policy_number": str(row.get("policy_number") or ""),
        "loan_number": str(row.get("loan_number") or "") or None,
        "policy_aliases": list(row.get("policy_aliases") or []),
        "applicant_id": str(row.get("applicant_id") or "") or None,
        "insured_name": row.get("insured_name"),
        "department": row.get("department"),
        "carrier": row.get("carrier"),
        "status": status,
        "reason": reason,
        "actions_taken": list(actions_taken or []),
        "waiting_on": waiting_on,
        "evidence": dict(evidence or {}),
        "updated_at": _utcnow(),
    }


def record_outcomes(
    job: dict[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Record per-policy outcomes via the shared ``verification_common`` path.

    Writes to the job's ``action`` checkpoint detail and mirrors each outcome
    into ``durable_work_items`` under the ``mortgagee_verification`` namespace.
    Returns the recorded outcome dicts (the engine writes the WorkerResult detail).
    """
    from .store import JobStore

    def _as_dict(outcome):
        if hasattr(outcome, "to_dict"):
            return outcome.to_dict()
        if hasattr(outcome, "__dataclass_fields__"):
            return asdict(outcome)
        return dict(outcome)

    payload = dict(job.get("payload") or {})
    resolved_db = db_path or payload.get("jobs_db_path") or os.environ.get("ROBIE_JOB_DB")
    field_names = frozenset(PolicyOutcome.__dataclass_fields__)
    shared = [
        PolicyOutcome(**{k: v for k, v in o.items() if k in field_names})
        if isinstance(o, dict)
        else o
        for o in outcomes
    ]
    recorded = [_as_dict(o) for o in shared]
    if _verification_common is None or not _VERIFICATION_COMMON_AVAILABLE:
        logger.warning("record_outcomes: verification_common unavailable; outcomes returned unpersisted")
        return recorded
    if not resolved_db:
        # Degraded mode (matches _ledger_for_job / the verifier): no jobs DB
        # is known, so there is nothing to persist to. The outcomes are still
        # returned so the engine records them in the job's action checkpoint.
        logger.warning("record_outcomes: no jobs DB path known; outcomes returned unpersisted")
        return recorded
    store = JobStore(str(resolved_db))
    _verification_common.record_outcomes(store, job.get("id"), JOB_TYPE, shared)
    return recorded


# ---------------------------------------------------------------------------
# Row processing
# ---------------------------------------------------------------------------


def _expiration_of(row: dict[str, Any]) -> Any:
    for key in ("expiration_date", "expiration", "expires_on", "policy_expiration",
                "expirationDate"):
        if row.get(key):
            return row.get(key)
    return None


def _edocs_downloaded(row: dict[str, Any]) -> bool:
    for key in ("edocs_downloaded", "dec_package_downloaded", "renewal_docs_downloaded",
                "edocs_available"):
        if row.get(key):
            return True
    return False


def _process_row(
    row: dict[str, Any],
    job: dict[str, Any],
    ledger,
    *,
    today: date,
    authorized_actions: dict[str, bool] | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    policy_number = str(row.get("policy_number") or "").strip()
    lob = str(row.get("lob") or row.get("line_of_business") or "").strip()
    carrier = str(row.get("carrier") or "").strip()
    identity = _policy_identity(row)
    work_item_key = _work_item_key(identity or policy_number)
    prior_state = _read_policy_state(ledger, work_item_key)

    def auth(action_name: str) -> bool:
        if authorized_actions is not None and action_name in authorized_actions:
            return bool(authorized_actions[action_name])
        try:
            return bool(is_action_authorized(job, action_name))
        except Exception:
            return False

    # --- window bucketing --------------------------------------------------
    days = days_to_expiration(_expiration_of(row), today=today)
    bucket = bucket_window(days)
    if bucket == "not_yet_in_window":
        return _outcome_dict(
            row, status="pending", waiting_on="schedule",
            reason=f"not yet in window: {days}d to expiration (work window 30-45d)",
            actions_taken=["window_check"],
        )
    if bucket == "overdue":
        _merge_policy_state(ledger, work_item_key, {"overdue_exception": True})
        return _outcome_dict(
            row, status="pending", waiting_on="csr",
            reason=f"overdue exception: only {days}d to expiration (<{WINDOW_MIN_DAYS}d)",
            actions_taken=["window_check", "csr_escalation"],
            evidence={"note": build_discussion_note(
                policy_number, lob, carrier,
                f"Overdue mortgagee verification: {days} days to expiration. "
                "CSR ownership requested.")},
        )
    if bucket == "unknown":
        return _outcome_dict(
            row, status="not_done", waiting_on="csr",
            reason="blocked: expiration date is missing or unparseable",
            actions_taken=["window_check"],
        )

    # --- stop: cancelled / nonrenewed --------------------------------------
    stopped, stop_reason = is_policy_stopped(row)
    if stopped:
        return _outcome_dict(
            row, status="not_done",
            reason=stop_reason,
            actions_taken=["status_check"],
        )

    # --- renewal dec package ------------------------------------------------
    if _edocs_downloaded(row):
        dec_actions = ["edocs_reused"]
        dec_evidence: dict[str, Any] = {
            "dec_source": "carrier_edocs",
            "no_duplicate_retrieval": True,
        }
    else:
        # Retrieval intent only: actual portal/email retrieval is an authorized
        # action handled by a browser/email step, never silently executed here.
        return _outcome_dict(
            row, status="pending", waiting_on="carrier",
            reason="renewal dec package not yet retrieved; portal/email retrieval intent recorded",
            actions_taken=["window_check", "retrieval_intent_recorded"],
            evidence={"retrieval_intent": {"channels": ["carrier_portal", "carrier_email"],
                                           "documents": ["renewal_dec", "invoice"]}},
        )

    # --- producer gate ------------------------------------------------------
    policy_state = dict(prior_state)
    for flag in ("producer_review_complete", "coverage_review_pending",
                 "premium_review_pending", "uw_review_pending",
                 "retention_review_pending", "review_pending",
                 "cancelled", "nonrenewed", "cancellation_date"):
        if flag in row:
            policy_state[flag] = row[flag]
    clear, gate_reason = producer_gate(policy_state)
    if not clear:
        return _outcome_dict(
            row, status="pending", waiting_on="producer",
            reason=gate_reason,
            actions_taken=dec_actions + ["producer_review_requested"],
            evidence=dec_evidence,
        )

    # --- payer routing ------------------------------------------------------
    payer_path, payer_reason = route_payer(row.get("payer_info") or row)
    if payer_path == "unknown":
        return _outcome_dict(
            row, status="pending", waiting_on="csr",
            reason=f"payer unidentified: {payer_reason}; CSR ownership",
            actions_taken=dec_actions + ["producer_gate_passed", "payer_identification_pending"],
            evidence=dec_evidence,
        )

    if payer_path == "insured":
        # Client-paid: hand to CSR with full history per Carlo.
        handoff = row.get("csr_handoff_evidence") or (job.get("payload") or {}).get(
            "csr_handoff_evidence", {}
        )
        if isinstance(handoff, dict) and handoff.get(str(policy_number)):
            evidence_doc = dict(handoff[str(policy_number)])
            return _outcome_dict(
                row, status="done",
                reason="client-paid renewal handed to CSR with full history (documented handoff)",
                actions_taken=dec_actions + ["producer_gate_passed", "csr_handoff_documented"],
                evidence={**dec_evidence, "csr_handoff": evidence_doc},
            )
        note = build_discussion_note(
            policy_number, lob, carrier,
            "Client-paid renewal. Handing to CSR with full history per SOP: "
            "producer review complete, dec package on file. CSR owns the client conversation.")
        return _outcome_dict(
            row, status="pending", waiting_on="csr",
            reason="client-paid renewal handed to CSR with full history (handoff requested)",
            actions_taken=dec_actions + ["producer_gate_passed", "csr_handoff_requested"],
            evidence={**dec_evidence, "note": note},
        )

    # --- mortgagee pays ------------------------------------------------------
    payer_info = row.get("payer_info") if isinstance(row.get("payer_info"), dict) else {}
    mortgage_company = row.get("mortgage_company") or payer_info.get("mortgage_company")
    loan_number = row.get("loan_number")
    property_zip = row.get("property_zip") or row.get("zip") or row.get("property_zip_code")
    lender_ok, lender_reason = verify_lender(mortgage_company, loan_number, property_zip)
    if not lender_ok:
        return _outcome_dict(
            row, status="pending", waiting_on="csr",
            reason=lender_reason,
            actions_taken=dec_actions + ["producer_gate_passed", "lender_verification_failed"],
            evidence=dec_evidence,
        )

    # Safety invariant (on par with the producer gate): verify the lender of
    # record BEFORE uploading anything. The mortgage company on file may have
    # changed; stale servicer data must never receive another lender's docs.
    lookup = row.get("portal_lender_lookup")
    if lookup is None:
        return _outcome_dict(
            row, status="pending", waiting_on="mortgagee",
            reason=("confirming mortgagee payment for renewal term: lender of record "
                    "not yet verified — portal agent-section loan lookup intent recorded "
                    "(loan# + borrower name + property ZIP; no portal login, no SSN)"),
            actions_taken=dec_actions + ["producer_gate_passed", "lender_verified",
                                         "lender_of_record_lookup_intent_recorded"],
            evidence={**dec_evidence,
                      "lender_lookup_intent": {
                          "action": "lender_portal_loan_lookup",
                          "flow": "agent_section_loan_lookup",
                          "identifiers": ["loan_number", "borrower_name", "property_zip"],
                          "portal_login_required": LENDER_PORTAL_NEEDS_LOGIN_DEFAULT,
                          "no_ssn": True,
                      }},
        )
    record_ok, record_reason = verify_lender_of_record(
        mortgage_company, loan_number, property_zip, lookup)
    if not record_ok:
        _merge_policy_state(ledger, work_item_key, {"csr_review_flagged": True})
        return _outcome_dict(
            row, status="pending", waiting_on="mortgagee",
            reason=record_reason,
            actions_taken=dec_actions + ["producer_gate_passed", "lender_verified",
                                         "lender_of_record_mismatch", "csr_review_flagged"],
            evidence={**dec_evidence,
                      "lender_of_record": {"match": False, "detail": record_reason}},
        )

    # Lender portals need NO login: the agent-section loan lookup uses loan# +
    # identifiers only. If a portal unexpectedly demands credentials, record a
    # login gap for Carlo's walkthrough and stop — never enter credentials
    # blindly, never upload in the meantime.
    lender_portal = row.get("lender_portal") or payer_info.get("lender_portal")
    if row.get("portal_demands_login"):
        gap = _record_login_gap(
            job, db_path,
            portal_name=str(lender_portal or mortgage_company or "").strip() or "unknown-lender-portal",
            step="lender_portal_agent_section",
            whats_missing="portal unexpectedly demands username/password credentials",
        )
        return _outcome_dict(
            row, status="pending", waiting_on="mortgagee",
            reason=("confirming mortgagee payment for renewal term: lender portal "
                    "unexpectedly demands credentials; login gap recorded for walkthrough; "
                    "no upload attempted"),
            actions_taken=dec_actions + ["producer_gate_passed", "lender_verified",
                                         "lender_of_record_confirmed", "login_gap_recorded"],
            evidence={**dec_evidence, "login_gap": gap},
        )

    lender_evidence = {
        "mortgage_company": str(mortgage_company).strip(),
        "loan_number": str(loan_number).strip(),
        "property_zip": str(property_zip).strip(),
        "lender_of_record": record_reason,
        "portal_login_required": LENDER_PORTAL_NEEDS_LOGIN_DEFAULT,
        "no_ssn": True,
    }

    if lender_portal:
        # Portal agent-section upload (no login).
        delivery_action = "upload_lender_document"
        delivery_evidence = {
            **dec_evidence,
            "lender": {**lender_evidence, "channel": "lender_portal",
                       "lender_portal": lender_portal},
            "note": build_discussion_note(
                policy_number, lob, carrier,
                f"Mortgagee verification: {lender_reason}; {record_reason}. "
                "Dec + invoice delivery via lender portal agent section "
                "(no login, no SSN)."),
        }
        if not auth(delivery_action):
            return _outcome_dict(
                row, status="pending", waiting_on="authorization",
                reason=(f"confirming mortgagee payment for renewal term: lender delivery "
                        f"intent recorded ({delivery_action} via lender portal agent section) "
                        "but not executed — action is not authorized"),
                actions_taken=dec_actions + ["producer_gate_passed", "lender_verified",
                                             "lender_of_record_confirmed",
                                             f"{delivery_action}_intent_recorded"],
                evidence={**delivery_evidence, "intended_action": delivery_action},
            )
        delivery_evidence["upload_intent"] = {
            "action": delivery_action,
            "flow": "agent_section_loan_lookup",
            "documents": ["renewal_dec", "invoice"],
            "portal_login_required": LENDER_PORTAL_NEEDS_LOGIN_DEFAULT,
            "no_ssn": True,
            "authorized": True,
        }
        return _payment_monitoring_outcome(
            row, job, ledger, today, work_item_key, prior_state, days,
            delivery_actions=["producer_gate_passed", "lender_verified",
                              "lender_of_record_confirmed",
                              f"{delivery_action}_intent_recorded",
                              "payment_check_scheduled"],
            delivery_evidence=delivery_evidence,
            actions_prefix=dec_actions,
        )

    # No lender portal -> Bland voice call to the MORTGAGE COMPANY.
    # voice_enabled defaults True (Carlo-approved); execution still requires
    # explicit authorization. Borrower/client numbers are never dialed.
    voice_enabled = bool((job.get("payload") or {}).get("voice_enabled", True))
    intent, intent_reason = build_mortgagee_voice_intent(row)
    voice_evidence = {
        **dec_evidence,
        "lender": {**lender_evidence, "channel": "mortgagee_voice_call"},
        "note": build_discussion_note(
            policy_number, lob, carrier,
            f"Mortgagee verification: {lender_reason}; {record_reason}. "
            "No lender portal — placing Bland voice call to the mortgage company "
            "to confirm payment for the renewal term (never dialing the borrower)."),
    }
    if intent is None:
        return _outcome_dict(
            row, status="pending", waiting_on="mortgagee",
            reason=f"confirming mortgagee payment for renewal term: {intent_reason}",
            actions_taken=dec_actions + ["producer_gate_passed", "lender_verified",
                                         "lender_of_record_confirmed",
                                         "voice_intent_blocked"],
            evidence=voice_evidence,
        )
    if voice_enabled and auth("place_mortgagee_voice_call"):
        voice_evidence["voice_intent"] = {**intent, "authorized": True}
        return _payment_monitoring_outcome(
            row, job, ledger, today, work_item_key, prior_state, days,
            delivery_actions=["producer_gate_passed", "lender_verified",
                              "lender_of_record_confirmed",
                              "mortgagee_voice_call_intent_recorded",
                              "payment_check_scheduled"],
            delivery_evidence=voice_evidence,
            actions_prefix=dec_actions,
        )
    return _outcome_dict(
        row, status="pending", waiting_on="authorization",
        reason=(f"confirming mortgagee payment for renewal term: mortgage-company voice "
                f"call intent recorded (place_mortgagee_voice_call) but not executed — "
                f"action is not authorized (voice_enabled={voice_enabled})"),
        actions_taken=dec_actions + ["producer_gate_passed", "lender_verified",
                                     "lender_of_record_confirmed",
                                     "voice_call_intent_recorded"],
        evidence={**voice_evidence, "intended_action": "place_mortgagee_voice_call",
                  "voice_intent": intent},
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class MortgageeVerificationWorker:
    """Work the 4372 mortgagee queue for job type ``mortgagee_verification``."""

    def __init__(self, store: Any | None = None):
        self._store = store

    def _db_path(self, job: dict[str, Any]) -> Optional[str]:
        payload = job.get("payload") or {}
        p = (
            payload.get("db_path")
            or payload.get("jobs_db_path")
            or getattr(self._store, "path", None)
            or os.environ.get("ROBIE_JOB_DB")
            or None
        )
        return str(p) if p else None

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        action = str(job.get("action_type") or JOB_TYPE)
        payload = dict(job.get("payload") or {})
        report_id = str(payload.get("report_id") or REPORT_ID_4372)
        today = _parse_date(payload.get("as_of")) or date.today()
        db_path = self._db_path(job)
        authorized_actions = payload.get("authorized_actions")

        try:
            rows = fetch_report_rows(
                report_id=report_id,
                fields=None,
                filters=None,
                db_path=db_path,
                session=payload.get("session"),
            )
        except Exception as exc:
            return WorkerResult(
                False, action, {"report_id": report_id},
                {"error": f"{type(exc).__name__}: {exc}"},
                retryable=True,
                error=f"report {report_id} fetch failed: {type(exc).__name__}: {exc}",
            )
        rows = [dict(r) for r in (rows or [])]

        ledger = None
        try:
            ledger = _ledger_for_job(job, store=self._store)
        except Exception:
            ledger = None

        in_scope = [
            r for r in rows
            if str(r.get("lob") or r.get("line_of_business") or "").strip() in ALLOWED_LOBS
        ]
        skipped = len(rows) - len(in_scope)

        outcomes = [
            _process_row(r, job, ledger, today=today,
                         authorized_actions=authorized_actions, db_path=db_path)
            for r in in_scope
        ]
        recorded = record_outcomes(job, outcomes, db_path=db_path)

        counts: dict[str, int] = {}
        for outcome in recorded:
            key = str(outcome.get("status") or "unknown")
            counts[key] = counts.get(key, 0) + 1

        return WorkerResult(
            True,
            action,
            {
                "report_id": report_id,
                "row_count": len(rows),
                "in_scope_count": len(in_scope),
                "skipped_lob": skipped,
            },
            {
                "report_id": report_id,
                "policy_outcomes": recorded,
                "counts": counts,
                "idempotency_key": idempotency_key,
            },
            retryable=False,
        )


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


def _fresh_checkpoint(job: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """Fresh read-back of the action checkpoint; never trust the worker snapshot."""
    payload = dict(job.get("payload") or {})
    db_path = payload.get("jobs_db_path") or os.environ.get("ROBIE_JOB_DB")
    job_id = job.get("id")
    if db_path and job_id:
        try:
            conn = sqlite3.connect(str(db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT data_json FROM checkpoints WHERE job_id=? AND kind='action'",
                    (job_id,),
                ).fetchone()
            finally:
                conn.close()
            if row and row["data_json"]:
                data = json.loads(row["data_json"])
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return dict(action)


def _durable_policy_states(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fresh read-back of durable_work_items for this worker's namespace."""
    payload = dict(job.get("payload") or {})
    db_path = payload.get("jobs_db_path") or os.environ.get("ROBIE_JOB_DB")
    states: dict[str, dict[str, Any]] = {}
    if not db_path:
        return states
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT work_item_key, outcome FROM durable_work_items WHERE namespace=?",
                (DURABLE_NAMESPACE,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return states
    for row in rows:
        try:
            data = json.loads(row["outcome"]) if row["outcome"] else {}
        except (TypeError, ValueError):
            data = {}
        states[str(row["work_item_key"])] = data if isinstance(data, dict) else {}
    return states


def _lender_delivery_claimed(outcome: dict[str, Any]) -> bool:
    evidence = dict(outcome.get("evidence") or {})
    if evidence.get("upload_intent"):
        return True
    delivered = evidence.get("lender_delivery") or evidence.get("lender")
    return bool(delivered and evidence.get("payment") is not None)


class MortgageeVerificationVerifier:
    """Independent verification for ``mortgagee_verification`` jobs.

    CRITICAL invariant: no outcome may claim a lender delivery (done with
    lender evidence) unless a ``producer_review_complete`` record exists for
    that policy in ``durable_work_items``. Any violation is UNVERIFIED.
    """

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        checkpoint = _fresh_checkpoint(job, action)
        detail = dict(checkpoint.get("detail") or {})
        outcomes = list(detail.get("policy_outcomes") or [])
        durable = _durable_policy_states(job)

        problems: list[str] = []
        checked = 0
        delivered_checked = 0
        if not outcomes:
            problems.append("no per-policy outcomes in the action checkpoint")

        for outcome in outcomes:
            checked += 1
            policy_number = str(outcome.get("policy_number") or "")
            status = str(outcome.get("status") or "")
            if not policy_number:
                problems.append(f"outcome #{checked}: missing policy_number")
                continue
            if status not in OUTCOME_STATUSES:
                problems.append(f"{policy_number}: invalid status {status!r}")
                continue
            if not outcome.get("updated_at"):
                problems.append(f"{policy_number}: missing updated_at")
            if status == "pending" and not outcome.get("waiting_on"):
                problems.append(f"{policy_number}: pending outcome missing waiting_on")
            if status == "done" and not outcome.get("evidence"):
                problems.append(f"{policy_number}: done outcome carries no evidence refs")
            if not outcome.get("reason"):
                problems.append(f"{policy_number}: outcome missing reason")

            # CRITICAL: lender delivery requires recorded producer clearance.
            if _lender_delivery_claimed(outcome):
                delivered_checked += 1
                candidates = {
                    _work_item_key(policy_number),
                    _work_item_key(str(outcome.get("policy_number") or "")),
                }
                identity = _policy_identity(outcome)
                if identity:
                    candidates.add(_work_item_key(identity))
                state: dict[str, Any] = {}
                for key in candidates:
                    if key in durable:
                        state = durable[key]
                        break
                if not state.get(_REVIEW_COMPLETE_KEY):
                    problems.append(
                        f"{policy_number}: lender delivery claimed without a recorded "
                        "producer_review_complete in durable_work_items"
                    )

        verified = not problems
        observed = {
            "outcomes_checked": checked,
            "lender_deliveries_checked": delivered_checked,
            "problems": problems,
            "fresh_checkpoint_read": True,
            "durable_states_read": len(durable),
        }
        evidence = VerificationEvidence(
            method="FRESH_CHECKPOINT_AND_DURABLE_READBACK",
            source="mortgagee-verification-job-and-durable-work-items",
            expected={
                "outcome_schema": "valid",
                "producer_gate_invariant": "no lender delivery without producer_review_complete",
            },
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=str((job.get("payload") or {}).get("jobs_db_path")
                        or os.environ.get("ROBIE_JOB_DB") or "checkpoint-action"),
        )
        return VerificationResult(
            verified,
            evidence,
            retryable=False,
            error=None if verified else "; ".join(problems),
        )
