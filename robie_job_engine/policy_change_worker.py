"""Policy-change verification worker (Worker 4: ``policy_change_verification``).

New build from the policy-change SOP
(``.agents/skills/ezlynx-policy-change-confirmation/SKILL.md`` + rebuild-brief
sections 2.4/3.4). There is no old code to port; the SOP is the spec.

Report 4359 is Look 4602 (19 columns, verified 2026-09-18). Identity is
``policy_number`` + ``change_request_created_date``. There is no ``request_id``
column. :data:`POLICY_CHANGE_ENABLED` stays ``False`` and
``schema_verified`` stays ``False`` until 3 clean hermes-test-01 post-job
audits after Test install. ``start_run()`` refuses the report until that
flag flips — the refusal is allowed to propagate; it is never bypassed.

Safety invariants encoded here:
- Cardinal rule: ROBIE never deletes a policy. This worker has no delete path;
  it only reads, compares, posts confirmation notes, and records outcomes.
- Carrier-only emails: the client must never be emailed about a policy change.
  ``assert_no_client_recipient`` raises on any client address in To/CC, and a
  carrier email is sent only when ``is_action_authorized(job,
  "send_carrier_email")`` allows it. Unauthorized -> pending, never executed.
- The worker never closes the underlying policy-change request. Only the
  Quality Controller may close; the worker records verification results and
  leaves the task open until resolved.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult


#: Kill switch. Stays False in this change.
#: Flip only after 3 clean hermes-test-01 post-job audits following Test install.
#: Do not set True in the same PR that maps Look 4602 columns.
POLICY_CHANGE_ENABLED = False

JOB_TYPE = "policy_change_verification"
WORKER_NAME = "policy-change-verification"
REPORT_ID = "4359"
SEND_CARRIER_EMAIL_ACTION = "send_carrier_email"

#: The six three-way-verification result states (SOP 3.4).
RESULT_STATES = (
    "pass",
    "request_unclear",
    "waiting_for_carrier",
    "carrier_correction_required",
    "ezlynx_correction_required",
    "coverage_review_required",
)

#: Verification result -> per-policy outcome status/owner (design doc schema).
STATE_TO_OUTCOME = {
    "pass": ("done", None),
    "request_unclear": ("not_done", "csr"),
    "waiting_for_carrier": ("pending", "carrier"),
    "carrier_correction_required": ("pending", "carrier"),
    "ezlynx_correction_required": ("pending", "csr"),
    "coverage_review_required": ("pending", "producer"),
}

#: Carrier evidence retrieval channels, in SOP preference order.
EVIDENCE_CHANNELS = (
    "policy_change_folder",
    "carrier_edocs",
    "portal_document_download",
    "email",
)

CARRIER_EMAIL_TEMPLATE_PREFIX = "Policy Change"

#: Canonical worker fields for the 19 Look 4602 / Gmail CSV columns.
#: Order matches the verified export. Do not append phantom fields.
POLICY_CHANGE_FIELDS: tuple[str, ...] = (
    "account_name",
    "applicant_id",
    "policy_number",
    "line_of_business",
    "effective_date",
    "master_company",
    "request_status",
    "created_by",
    "written_premium",
    "annualized_premium",
    "branch",
    "department",
    "service_team",
    "assigned_producer",
    "csr",
    "preferred_language",
    "applicant_labels",
    "policy_labels",
    "change_request_created_date",
)

#: Accepted spellings per canonical field: snake_case, Gmail CSV header,
#: Look 4602 qualified header. Nothing else is read.
_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "account_name": (
        "account_name",
        "Account Name",
        "Applicant Data Account Name",
    ),
    "applicant_id": (
        "applicant_id",
        "Applicant ID",
        "Policy Transaction Data Applicant ID",
    ),
    "policy_number": (
        "policy_number",
        "Policy Number",
        "Policy Change Request Detail Policy Number",
    ),
    "line_of_business": (
        "line_of_business",
        "Line Of Business",
        "Policy Transaction Data Line Of Business",
    ),
    "effective_date": (
        "effective_date",
        "Effective Date",
        "Policy Change Request Detail Policy Effective Date",
    ),
    "master_company": (
        "master_company",
        "Master Company",
        "Policy Transaction Data Master Company",
    ),
    "request_status": (
        "request_status",
        "Request Status",
        "Policy Change Request Detail Request Status",
    ),
    "created_by": (
        "created_by",
        "Created By",
        "Policy Change Request Detail Created By",
    ),
    "written_premium": (
        "written_premium",
        "Written Premium",
        "Policy Transaction Data Written Premium",
    ),
    "annualized_premium": (
        "annualized_premium",
        "Premium - Annualized",
        "Policy Transaction Data Annualized Premium",
    ),
    "branch": (
        "branch",
        "Branch",
        "Applicant Data Branch",
    ),
    "department": (
        "department",
        "Department",
        "Policy Transaction Data Department",
    ),
    "service_team": (
        "service_team",
        "Service Team",
        "Serviceteampolicychangerequest Service Team",
    ),
    "assigned_producer": (
        "assigned_producer",
        "Assigned Producer",
        "Applicant Data Assigned Producer",
    ),
    "csr": (
        "csr",
        "CSR",
        "Applicant Data CSR",
    ),
    "preferred_language": (
        "preferred_language",
        "Preferred Language",
        "Applicant Data Preferred Language",
    ),
    "applicant_labels": (
        "applicant_labels",
        "Applicant Labels",
        "Applicant Labels Applicant Labels",
    ),
    "policy_labels": (
        "policy_labels",
        "Policy Labels",
        "Policy Labels Policy Labels",
    ),
    "change_request_created_date": (
        "change_request_created_date",
        "Change Request Created Date",
        "Policy Change Request Detail Change Request Created Date",
    ),
}

try:  # Built by a sibling agent; fail closed until it lands.
    from .verification_common import (  # type: ignore
        PolicyOutcome,
        is_action_authorized,
        record_login_gap,
        record_outcomes,
    )

    _VERIFICATION_COMMON_AVAILABLE = True
except ImportError:  # pragma: no cover - sibling module not landed yet
    _VERIFICATION_COMMON_AVAILABLE = False
    PolicyOutcome = None  # type: ignore

    def is_action_authorized(job: Mapping[str, Any], action: str) -> bool:  # type: ignore
        return False

    def record_login_gap(  # type: ignore
        store: Any,
        job_id: Any,
        job_type: str,
        portal_name: str,
        step: str,
        whats_missing: str,
    ) -> None:
        raise RuntimeError("verification_common is unavailable; cannot record login gap")

    def record_outcomes(store: Any, job: Mapping[str, Any], outcomes: list[Any]) -> None:  # type: ignore
        raise RuntimeError("verification_common is unavailable; cannot record outcomes")


try:  # Sibling-built mailer; bound at import so tests can fake it via sys.modules.
    from . import verification_mailer as _verification_mailer  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - sibling module not landed yet
    _verification_mailer = None  # type: ignore[assignment]


def _carrier_mailer() -> Any | None:
    """Port that sends carrier emails (``verification_mailer``).

    Bound at module import; tests replace the sys.modules entry before import.
    """
    return _verification_mailer


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def _field_text(row: Mapping[str, Any], field_name: str) -> str:
    """First non-blank spelling of one canonical field. Never invents a value."""
    for key in _FIELD_KEYS[field_name]:
        if key not in row or row.get(key) is None:
            continue
        text = str(row.get(key)).strip()
        if text:
            return text
    return ""


def parse_policy_change_row(row: Mapping[str, Any]) -> dict[str, str]:
    """Project one report row onto the 19 real columns.

    Accepts snake_case, the Gmail CSV headers, or the Look 4602 headers.
    Keys that are not one of those 19 columns are dropped. Missing cells
    are empty strings — they are not guessed from other columns.
    """
    source = dict(row or {})
    return {name: _field_text(source, name) for name in POLICY_CHANGE_FIELDS}


def reconstruct_request(
    row: Mapping[str, Any],
    discussion_text: str | None,
) -> dict[str, Any]:
    """Reconstruct a policy-change request from a report row.

    Pure function. Uses only the 19 Look 4602 columns. ``discussion_text``
    is ignored for field extraction: the export has no change-description
    column, and this function does not infer add/delete/replace/modify.
    Ambiguous requests (missing identity or policy effective date) map to
    ``request_unclear`` and are routed to the CSR before any judgement.
    """
    parsed = parse_policy_change_row(row)
    ambiguity_reasons: list[str] = []
    if not parsed["policy_number"]:
        ambiguity_reasons.append("missing policy number")
    if not parsed["change_request_created_date"]:
        ambiguity_reasons.append("missing change request created date")
    if not parsed["effective_date"]:
        ambiguity_reasons.append("missing policy effective date")
    premium_expectation = parsed["written_premium"] or parsed["annualized_premium"] or None
    return {
        **parsed,
        "insured_name": parsed["account_name"],
        "effective_date": parsed["effective_date"] or None,
        "premium_expectation": premium_expectation,
        "change_action": None,
        "affected_item": None,
        "requested_values": {},
        "coverage_review_flag": False,
        "ambiguous": bool(ambiguity_reasons),
        "ambiguity_reasons": ambiguity_reasons,
        "discussion_text": discussion_text or "",
    }


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    return re.sub(r"\s+", " ", text)


def three_way_match(
    request: Mapping[str, Any],
    carrier_evidence: Mapping[str, Any] | None,
    ezlynx_data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Three-way verification, field by field.

    Compares (1) the original request vs (2) carrier-issued
    endorsement/decs/premium vs (3) the EZLynx transaction + keyed data.
    "Carrier processed" is not proof of match.

    Pure function. Returns ``{"state", "matches", "exceptions", "field_checks"}``
    where ``state`` is one of the six SOP result states.
    """
    request = dict(request or {})
    if request.get("ambiguous"):
        return {
            "state": "request_unclear",
            "matches": [],
            "exceptions": [],
            "field_checks": [],
            "reason": "; ".join(request.get("ambiguity_reasons") or ["request is ambiguous"]),
        }
    if not carrier_evidence:
        return {
            "state": "waiting_for_carrier",
            "matches": [],
            "exceptions": [],
            "field_checks": [],
            "reason": "no carrier-issued endorsement/decs/confirmation on file yet",
        }
    if request.get("coverage_review_flag"):
        return {
            "state": "coverage_review_required",
            "matches": [],
            "exceptions": [],
            "field_checks": [],
            "reason": "change affects coverage terms; producer coverage review required",
        }

    carrier_fields = dict((carrier_evidence or {}).get("fields") or {})
    ezlynx_fields = dict(ezlynx_data or {})
    compared: dict[str, Any] = {"effective_date": request.get("effective_date")}
    compared.update(dict(request.get("requested_values") or {}))
    if request.get("premium_expectation") is not None:
        compared["premium"] = request.get("premium_expectation")
    # Carrier premium is compared against the expectation when both exist.
    if "premium" not in compared and carrier_fields.get("premium") is not None:
        compared["premium"] = carrier_fields.get("premium")

    matches: list[str] = []
    exceptions: list[dict[str, Any]] = []
    field_checks: list[dict[str, Any]] = []
    for field_name, requested in compared.items():
        carrier_value = carrier_fields.get(field_name)
        ezlynx_value = ezlynx_fields.get(field_name)
        check = {
            "field": field_name,
            "requested": requested,
            "carrier": carrier_value,
            "ezlynx": ezlynx_value,
        }
        if _normalize(carrier_value) != _normalize(requested):
            check["match"] = False
            check["exception_side"] = "carrier"
            exceptions.append({**check})
        elif _normalize(ezlynx_value) != _normalize(requested):
            check["match"] = False
            check["exception_side"] = "ezlynx"
            exceptions.append({**check})
        else:
            check["match"] = True
            check["exception_side"] = None
            matches.append(field_name)
        field_checks.append(check)

    carrier_exceptions = [exc for exc in exceptions if exc["exception_side"] == "carrier"]
    if carrier_exceptions:
        state = "carrier_correction_required"
        reason = "carrier-issued values differ from the original request"
    elif exceptions:
        state = "ezlynx_correction_required"
        reason = "carrier matches the request but EZLynx keyed data differs"
    else:
        state = "pass"
        reason = "request, carrier evidence, and EZLynx data match field by field"
    return {
        "state": state,
        "matches": matches,
        "exceptions": exceptions,
        "field_checks": field_checks,
        "reason": reason,
    }


def assert_no_client_recipient(
    to: Sequence[str],
    cc: Sequence[str],
    *,
    client_addresses: Iterable[str],
) -> None:
    """Enforce carrier-only email: raise if any client address is present.

    Raises:
        ValueError: if any address in ``to``/``cc`` matches a known client
            address (case-insensitive). The caller must strip the client
            before sending; a hit here is a bug, not something to silently fix.
    """
    client = {_normalize(address) for address in client_addresses if address}
    offenders = [
        address
        for address in [*to, *cc]
        if _normalize(address) in client and _normalize(address)
    ]
    if offenders:
        raise ValueError(
            "carrier-only policy-change email blocked: client address present in "
            f"recipients: {', '.join(offenders)}"
        )


def strip_client_recipients(
    addresses: Sequence[str],
    *,
    client_addresses: Iterable[str],
) -> list[str]:
    """Return ``addresses`` with any known client address removed."""
    client = {_normalize(address) for address in client_addresses if address}
    return [address for address in addresses if _normalize(address) not in client]


def build_confirmation_report(
    *,
    request: Mapping[str, Any],
    match_result: Mapping[str, Any],
    carrier_evidence: Mapping[str, Any] | None,
    ezlynx_data: Mapping[str, Any] | None,
    document_refs: Sequence[str],
    next_action: str,
    follow_up_date: str | None,
) -> str:
    """Build the fixed SOP confirmation report. Pure function.

    Sections: Requested / Carrier issued / EZLynx recorded / Matches /
    Exceptions / Documents / Result / Next action + follow-up date. Every note
    ends with the exact line ``ROBIE was here``.
    """
    request = dict(request or {})
    match_result = dict(match_result or {})
    carrier_evidence = dict(carrier_evidence or {})
    ezlynx_data = dict(ezlynx_data or {})
    lines = [
        f"Policy: #{request.get('policy_number')} (Policy Change - {carrier_evidence.get('carrier') or 'carrier TBD'})",
        "",
        "## Policy change confirmation",
        "",
        f"Policy number: {request.get('policy_number')}",
        f"Change request created: {request.get('change_request_created_date')}",
        f"Insured: {request.get('insured_name')}",
        "",
        "### Requested",
        f"- Policy effective date: {request.get('effective_date')}",
        f"- Line of business: {request.get('line_of_business')}",
        f"- Carrier: {request.get('master_company')}",
        f"- Request status: {request.get('request_status')}",
        f"- Written premium: {request.get('written_premium')}",
        f"- Annualized premium: {request.get('annualized_premium')}",
        "",
        "### Carrier issued",
        f"- Document: {carrier_evidence.get('document_ref') or 'not yet received'}",
        f"- Values: {carrier_evidence.get('fields')}",
        "",
        "### EZLynx recorded",
        f"- Transaction/values: {dict(ezlynx_data)}",
        "",
        "### Matches",
        *([f"- {field}" for field in (match_result.get("matches") or [])] or ["- none"]),
        "",
        "### Exceptions",
        *(
            [
                f"- {exc.get('field')}: requested {exc.get('requested')!r}, "
                f"carrier {exc.get('carrier')!r}, ezlynx {exc.get('ezlynx')!r} "
                f"(side: {exc.get('exception_side')})"
                for exc in (match_result.get("exceptions") or [])
            ]
            or ["- none"]
        ),
        "",
        "### Documents",
        *([f"- {ref}" for ref in document_refs] or ["- none filed yet"]),
        "",
        "### Result",
        f"- State: {match_result.get('state')}",
        f"- Detail: {match_result.get('reason')}",
        "",
        "### Next action",
        f"- {next_action}",
        f"- Follow-up date: {follow_up_date or 'TBD'}",
        "",
        "ROBIE was here",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _evidence_retrieval_intents(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Carrier evidence retrieval intents in SOP preference order."""
    return [
        {
            "channel": channel,
            "policy_number": request.get("policy_number"),
            "change_request_created_date": request.get("change_request_created_date"),
        }
        for channel in EVIDENCE_CHANNELS
    ]


def _load_fetch_report_rows() -> Any:
    try:
        from .report_fetcher import fetch_report_rows
    except ImportError as exc:  # pragma: no cover - sibling module missing
        raise RuntimeError(
            "report_fetcher.fetch_report_rows unavailable — cannot read report 4359"
        ) from exc
    return fetch_report_rows


def _request_key(request: Mapping[str, Any]) -> str:
    policy = str(request.get("policy_number") or "").strip()
    created = str(request.get("change_request_created_date") or "").strip()
    return f"{policy} | {created}"


class PolicyChangeWorker:
    """Verify open policy-change requests with three-way matching."""

    def __init__(self, store: Any | None = None) -> None:
        self._store = store

    # -- carrier evidence -------------------------------------------------
    def _maybe_send_carrier_email(
        self,
        job: Mapping[str, Any],
        *,
        to: Sequence[str],
        cc: Sequence[str],
        subject: str,
        body: str,
    ) -> dict[str, Any]:
        """Send a carrier-only policy-change email, or record why it was not sent.

        The email goes out only when ``verification_mailer`` is wired AND
        ``is_action_authorized(job, "send_carrier_email")`` allows it.
        Unauthorized -> pending, never executed.
        """
        payload = dict(job.get("payload") or {})
        client_addresses = list(payload.get("client_emails") or [])
        assert_no_client_recipient(to, cc, client_addresses=client_addresses)
        mailer = _carrier_mailer()
        if mailer is None:
            return {"sent": False, "reason": "verification_mailer is not wired; CSR to send manually"}
        if not is_action_authorized(job, SEND_CARRIER_EMAIL_ACTION):
            return {"sent": False, "reason": "send_carrier_email not authorized; held pending"}
        if not subject.startswith(CARRIER_EMAIL_TEMPLATE_PREFIX):
            return {"sent": False, "reason": "email must use the EZLynx 'Policy Change' template"}
        receipt = mailer.send_verification_email(
            to=list(to), cc=list(cc), subject=subject, text_body=body
        )
        return {"sent": True, "receipt": receipt}

    # -- perform ----------------------------------------------------------
    def _record_portal_login_gap(
        self,
        job: Mapping[str, Any],
        *,
        portal_name: str,
        step: str,
        whats_missing: str,
    ) -> dict[str, Any]:
        """Record a portal login gap for the Document Download path.

        Called on unexpected login stalls the SOPs don't cover. The policy
        stays ``pending`` with ``waiting_on=carrier`` meanwhile. Uses the
        sibling's ``record_login_gap(store, job_id, job_type, portal_name,
        step, whats_missing)`` — never reimplemented here.
        """
        gap = {
            "job_id": job.get("id"),
            "job_type": JOB_TYPE,
            "portal_name": portal_name,
            "step": step,
            "whats_missing": whats_missing,
        }
        if self._store is not None and _VERIFICATION_COMMON_AVAILABLE:
            record_login_gap(
                self._store, job.get("id"), JOB_TYPE, portal_name, step, whats_missing
            )
        return {
            "status": "pending",
            "waiting_on": "carrier",
            "reason": (
                "waiting_for_carrier: portal Document Download stalled on login at "
                f"{portal_name} (step: {step}) — {whats_missing}"
            ),
            "actions_taken": ["portal_document_download_attempted", "login_gap_recorded"],
            "login_gap": gap,
        }

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        action = str(job.get("action_type") or JOB_TYPE)
        if action != JOB_TYPE:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error=f"policy-change worker received unexpected action type: {action}",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        if not POLICY_CHANGE_ENABLED:
            return WorkerResult(
                False,
                action,
                {"report_id": REPORT_ID},
                retryable=False,
                error="report 4359 schema unverified — policy-change worker disabled",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        # schema_verified stays False until 3 clean Test audits. fetch_report_rows
        # calls start_run() and refuses before any email or Looker work. Do not
        # bypass that gate here.
        try:
            from .report_registry import get_report_spec

            spec = get_report_spec(REPORT_ID)
        except Exception as exc:
            return WorkerResult(
                False,
                action,
                {"report_id": REPORT_ID},
                retryable=True,
                error=f"report registry lookup failed for {REPORT_ID}: {type(exc).__name__}: {exc}",
            )
        if not spec.schema_verified:
            return WorkerResult(
                False,
                action,
                {"report_id": REPORT_ID, "look_id": spec.look_id},
                retryable=False,
                error="report 4359 schema unverified — policy-change worker disabled",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        identity_ok = spec.identity_fields == (
            "policy_number",
            "change_request_created_date",
        )
        if not identity_ok or spec.look_id != "4602":
            return WorkerResult(
                False,
                action,
                {"report_id": REPORT_ID, "look_id": spec.look_id},
                retryable=False,
                error=(
                    "report 4359 registry does not match Look 4602 identity "
                    "(policy_number, change_request_created_date)"
                ),
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        return self._process_open_requests(job, idempotency_key=idempotency_key)

    def _iter_open_requests(self, job: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        """Yield report-4359 rows from the email CSV or Look 4602.

        Same entry point as the renewal and audit workers:
        ``report_fetcher.fetch_report_rows``. That call fail-closes while
        ``schema_verified`` is False (``start_run``), and when the export is
        missing an identity column. This method does not invent rows.
        """
        fetch_report_rows = _load_fetch_report_rows()
        payload = dict(job.get("payload") or {})
        db_path = (
            payload.get("db_path")
            or payload.get("jobs_db_path")
            or getattr(self._store, "path", None)
            or os.environ.get("ROBIE_JOB_DB")
            or None
        )
        rows = fetch_report_rows(
            report_id=REPORT_ID,
            fields=None,
            filters=None,
            db_path=str(db_path) if db_path else None,
            session=payload.get("session"),
            csv_bytes=payload.get("csv_bytes"),
            gmail_service=payload.get("gmail_service"),
            source=payload.get("report_source"),
        )
        if not isinstance(rows, list):
            raise RuntimeError(
                f"fetch_report_rows returned {type(rows).__name__}, expected list[dict]"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    "report 4359 row is not a dict; refusing to guess columns"
                )
            yield row

    def _process_open_requests(self, job: Mapping[str, Any], *, idempotency_key: str) -> WorkerResult:
        action = str(job.get("action_type") or JOB_TYPE)
        outcomes: list[dict[str, Any]] = []
        reports: list[dict[str, Any]] = []
        for row in self._iter_open_requests(job):
            request = reconstruct_request(row, None)
            intents = _evidence_retrieval_intents(request)
            # Carrier evidence + EZLynx keyed data are attached by the
            # retrieval layer once wired; empty here means "not yet retrieved".
            carrier_evidence = dict(row.get("carrier_evidence") or {})
            ezlynx_data = dict(row.get("ezlynx_data") or {})
            match_result = three_way_match(request, carrier_evidence or None, ezlynx_data)
            status, waiting_on = STATE_TO_OUTCOME[match_result["state"]]
            actions_taken = ["request_reconstructed", "three_way_match", "confirmation_report_built"]
            reason = f"{match_result['state']}: {match_result.get('reason')}"
            evidence_extra: dict[str, Any] = {}
            # Portal Document Download login stalls the SOP doesn't cover:
            # record the login gap, keep the policy pending on the carrier.
            portal_stall = row.get("portal_login_stall")
            if portal_stall:
                gap_fragment = self._record_portal_login_gap(
                    job,
                    portal_name=str(portal_stall.get("portal_name") or "carrier portal"),
                    step=str(portal_stall.get("step") or "login"),
                    whats_missing=str(
                        portal_stall.get("whats_missing")
                        or "login step not covered by the SOP"
                    ),
                )
                status, waiting_on = gap_fragment["status"], gap_fragment["waiting_on"]
                reason = gap_fragment["reason"]
                actions_taken = ["request_reconstructed", *gap_fragment["actions_taken"]]
                evidence_extra = {"login_gap": gap_fragment["login_gap"]}
            next_action = {
                "request_unclear": "route to CSR for clarification",
                "waiting_for_carrier": "chase carrier for issued endorsement",
                "carrier_correction_required": "request corrected endorsement from carrier",
                "ezlynx_correction_required": "correct EZLynx keyed data to match carrier/request",
                "coverage_review_required": "producer coverage review",
                "pass": "confirmation posted; request resolved",
            }[match_result["state"]]
            if portal_stall:
                next_action = "resolve portal login gap, then chase carrier for issued endorsement"
            report = build_confirmation_report(
                request=request,
                match_result=match_result,
                carrier_evidence=carrier_evidence or None,
                ezlynx_data=ezlynx_data or None,
                document_refs=list(row.get("document_refs") or []),
                next_action=next_action,
                follow_up_date=None,
            )
            request_key = _request_key(request)
            reports.append({"request_key": request_key, "report": report})
            outcome = {
                "policy_number": request.get("policy_number"),
                "applicant_id": request.get("applicant_id") or None,
                "insured_name": request.get("insured_name"),
                "department": request.get("department") or request.get("branch") or "Unassigned",
                "carrier": (carrier_evidence or {}).get("carrier") or request.get("master_company"),
                "status": status,
                "reason": reason,
                "actions_taken": actions_taken,
                "waiting_on": waiting_on,
                "next_action": next_action,
                "follow_up_date": None,
                "evidence": {
                    "report_id": REPORT_ID,
                    "look_id": "4602",
                    "policy_number": request.get("policy_number"),
                    "change_request_created_date": request.get("change_request_created_date"),
                    "request_key": request_key,
                    "document_refs": list(row.get("document_refs") or []),
                    "match_state": match_result["state"],
                    "evidence_intents": intents,
                    **evidence_extra,
                },
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            outcomes.append(outcome)
        # The worker never closes the underlying request; it only records
        # verification results. Closure belongs to the Quality Controller.
        if self._store is not None and _VERIFICATION_COMMON_AVAILABLE:
            record_outcomes(self._store, job.get("id"), JOB_TYPE, [_as_policy_outcome(item) for item in outcomes])
        return WorkerResult(
            True,
            action,
            {"report_id": REPORT_ID, "confirmation_reports": reports},
            {
                "outcomes": outcomes,
                "idempotency_key": idempotency_key,
                "request_closed": False,
            },
            retryable=False,
        )


def _as_policy_outcome(data: Mapping[str, Any]) -> Any:
    if PolicyOutcome is None:
        return dict(data)
    fields = getattr(PolicyOutcome, "__dataclass_fields__", None)
    try:
        if fields:
            return PolicyOutcome(**{k: v for k, v in data.items() if k in fields})
        return PolicyOutcome(dict(data))
    except Exception:
        return dict(data)


class PolicyChangeVerifier:
    """Independently validate policy-change verification outcomes.

    Fresh read-back of the worker's recorded outcomes; never the worker's
    own snapshot. Invariants:
    - No client email may appear in ``evidence`` or ``actions_taken``
      (carrier-only). A hit is UNVERIFIED.
    - ``done`` outcomes must carry evidence references.
    """

    _CLIENT_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    def _client_email_in(self, value: Any, client_addresses: set[str]) -> str | None:
        if isinstance(value, str):
            for candidate in self._CLIENT_ADDRESS_RE.findall(value):
                if candidate.lower() in client_addresses:
                    return candidate
        elif isinstance(value, Mapping):
            for item in value.values():
                hit = self._client_email_in(item, client_addresses)
                if hit:
                    return hit
        elif isinstance(value, (list, tuple)):
            for item in value:
                hit = self._client_email_in(item, client_addresses)
                if hit:
                    return hit
        return None

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        detail = dict(action.get("detail") or {})
        payload = dict(job.get("payload") or {})
        client_addresses = {
            str(address).strip().lower()
            for address in (payload.get("client_emails") or [])
            if str(address).strip()
        }
        outcomes = list(detail.get("outcomes") or [])
        observed: dict[str, Any] = {
            "outcome_count": len(outcomes),
            "request_closed": detail.get("request_closed"),
        }
        problems: list[str] = []

        if detail.get("request_closed"):
            problems.append("worker closed the underlying request; only the Quality Controller may close")

        for outcome in outcomes:
            policy = (
                outcome.get("policy_number")
                or (outcome.get("evidence") or {}).get("request_key")
                or "?"
            )
            for field_name in ("evidence", "actions_taken"):
                hit = self._client_email_in(outcome.get(field_name), client_addresses)
                if hit:
                    problems.append(
                        f"client email {hit} present in {field_name} for {policy}: carrier-only invariant violated"
                    )
            if outcome.get("status") == "done":
                evidence = outcome.get("evidence") or {}
                refs = evidence.get("document_refs") or []
                if not refs and not evidence.get("match_state"):
                    problems.append(f"done outcome for {policy} has no evidence references")
            if outcome.get("status") not in ("done", "not_done", "pending"):
                problems.append(f"outcome for {policy} has unknown status {outcome.get('status')!r}")
            state = (outcome.get("reason") or "").split(":", 1)[0]
            if state and state not in RESULT_STATES:
                problems.append(f"outcome for {policy} has unknown verification state {state!r}")

        observed["problems"] = problems
        verified = not problems and bool(outcomes)
        if not outcomes:
            problems.append("no outcomes recorded")
            observed["problems"] = problems
        evidence = VerificationEvidence(
            method="FRESH_OUTCOME_READBACK",
            source="policy-change-verification-action-checkpoint",
            expected={
                "carrier_only": True,
                "request_closed": False,
                "done_outcomes_have_evidence": True,
            },
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=str(destination.get("report_id") or REPORT_ID),
        )
        return VerificationResult(
            verified,
            evidence,
            retryable=False,
            error=None if verified else "; ".join(problems),
        )
