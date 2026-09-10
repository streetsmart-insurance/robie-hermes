"""Daily verification digest worker: ``daily_verification_digest``.

Aggregates the last 24h of per-policy outcomes from the four verification
workers (manual renewals, audit, mortgagee, policy change) and emails a
markdown report to Carlo, split by AppSheet department:

Commercial Lines, Personal Lines, Trucking and Transportation, Operations,
Accounting — each with Done / Not done / Pending sections carrying reason,
owner (waiting_on), next action, and evidence references.

Section builders are modeled on ``reporting_suite.py``. Delivery reuses
``deliver_report()`` from ``accountability_delivery`` with a ``"verification"``
mode and explicit, config-driven delivery settings — no hardcoded secrets.
A send-API response alone is not proof; the verifier does a fresh
destination read-back of the sent message in the sender's Sent mailbox,
modeled on ``AccountabilityReportVerifier.verify_delivery_receipts``.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from . import accountability_delivery
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult


JOB_TYPE = "daily_verification_digest"
WORKER_NAME = "verification-digest"
DELIVERY_MODE = "verification"
SEND_DIGEST_EMAIL_ACTION = "send_digest_email"

DIGEST_TITLE = "STREETSMART DAILY VERIFICATION DIGEST"

#: AppSheet departments, in report order.
DEPARTMENTS = (
    "Commercial Lines",
    "Personal Lines",
    "Trucking and Transportation",
    "Operations",
    "Accounting",
)

#: The four worker job types this digest aggregates, in report order.
SOURCE_JOB_TYPES = (
    "manual_renewal_verification",
    "audit_verification",
    "mortgagee_verification",
    "policy_change_verification",
)

JOB_TYPE_LABELS = {
    "manual_renewal_verification": "Manual renewals (4247)",
    "audit_verification": "Audit verifications (4246)",
    "mortgagee_verification": "Mortgagee verifications (4372)",
    "policy_change_verification": "Policy change checking (4359)",
}

#: Queue (report id) per source job type — shown on every digest policy row.
JOB_TYPE_REPORT_IDS = {
    "manual_renewal_verification": "4247",
    "audit_verification": "4246",
    "mortgagee_verification": "4372",
    "policy_change_verification": "4359",
}

SINCE_HOURS = 24

try:  # Built by a sibling agent; fail closed until it lands.
    from .verification_common import is_action_authorized, read_outcomes  # type: ignore

    _VERIFICATION_COMMON_AVAILABLE = True
except ImportError:  # pragma: no cover - sibling module not landed yet
    _VERIFICATION_COMMON_AVAILABLE = False

    def is_action_authorized(job: Mapping[str, Any], action: str) -> bool:  # type: ignore
        return False

    def read_outcomes(store: Any, job_type: str, *, since_hours: int = 24) -> list[dict]:  # type: ignore
        raise RuntimeError("verification_common is unavailable; cannot read outcomes")


# ---------------------------------------------------------------------------
# Section builders (modeled on reporting_suite.py)
# ---------------------------------------------------------------------------

def _outcome_line(outcome: Mapping[str, Any]) -> str:
    policy = outcome.get("policy_number") or outcome.get("request_id") or "?"
    queue = JOB_TYPE_REPORT_IDS.get(str(outcome.get("_source") or ""), "?")
    insured = outcome.get("insured_name") or ""
    carrier = outcome.get("carrier") or ""
    who = f" — {insured}" if insured else ""
    via = f" ({carrier})" if carrier else ""
    reason = outcome.get("reason") or ""
    owner = outcome.get("waiting_on") or "—"
    next_action = outcome.get("next_action") or ""
    follow_up = outcome.get("follow_up_date") or ""
    evidence = outcome.get("evidence") or {}
    refs: list[str] = []
    if isinstance(evidence, Mapping):
        for key in ("ezlynx_note_id", "document_path", "document_refs", "match_state"):
            value = evidence.get(key)
            if value:
                refs.append(f"{key} {value}")
    evidence_text = f" | Evidence: {', '.join(refs)}" if refs else ""
    next_text = f" | Next: {next_action}" if next_action else ""
    follow_text = f" (follow-up {follow_up})" if follow_up else ""
    return (
        f"- **{policy}** [queue {queue}]{who}{via}: {reason} "
        f"| Owner: {owner}{next_text}{follow_text}{evidence_text}"
    )


def build_digest_header(run_at: datetime, counts: Mapping[str, int]) -> list[str]:
    eastern = run_at.astimezone(ZoneInfo("America/New_York"))
    total = sum(counts.values())
    queue_lines: list[str] = []
    for job_type in SOURCE_JOB_TYPES:
        count = counts.get(job_type, 0)
        label = JOB_TYPE_LABELS.get(job_type, job_type)
        report_id = JOB_TYPE_REPORT_IDS.get(job_type, "?")
        if count:
            queue_lines.append(f"- {label} (report {report_id}): {count}")
        else:
            queue_lines.append(
                f"- {label} (report {report_id}): 0 — no items in this queue in the last 24h"
            )
    return [
        f"# {DIGEST_TITLE}",
        f"Date: {eastern.strftime('%A, %B %d, %Y')} (ET) | Window: last {SINCE_HOURS}h",
        "",
        "## Queues",
        "",
        f"- Policies touched: {total}",
        *queue_lines,
        "",
    ]


def build_status_subsection(status: str, outcomes: list[Mapping[str, Any]]) -> list[str]:
    titles = {"done": "Done", "not_done": "Not done", "pending": "Pending"}
    lines = [f"### {titles.get(status, status)} ({len(outcomes)})", ""]
    if not outcomes:
        lines.append("_None._")
    else:
        lines.extend(_outcome_line(outcome) for outcome in outcomes)
    lines.append("")
    return lines


def build_department_section(
    department: str, outcomes: list[Mapping[str, Any]]
) -> list[str]:
    lines = [f"## {department}", ""]
    for status in ("done", "not_done", "pending"):
        lines.extend(
            build_status_subsection(status, [o for o in outcomes if o.get("status") == status])
        )
    return lines


def build_digest_footer(notes: list[str] | None = None) -> list[str]:
    lines = ["---", ""]
    if notes:
        lines.extend(["### Notes", "", *[f"- {note}" for note in notes], ""])
    lines.append("ROBIE was here")
    return lines


def build_digest_report(
    outcomes_by_job_type: Mapping[str, list[Mapping[str, Any]]],
    *,
    run_at: datetime | None = None,
    notes: list[str] | None = None,
) -> str:
    """Assemble the full digest markdown. Pure function."""
    run_at = run_at or datetime.now(timezone.utc)
    counts = {job_type: len(outcomes_by_job_type.get(job_type) or []) for job_type in SOURCE_JOB_TYPES}
    lines = build_digest_header(run_at, counts)
    for department in [*DEPARTMENTS, "Unassigned"]:
        department_outcomes: list[Mapping[str, Any]] = []
        for job_type in SOURCE_JOB_TYPES:
            for outcome in outcomes_by_job_type.get(job_type) or []:
                if _department_of(outcome) == department:
                    department_outcomes.append(dict(outcome, _source=job_type))
        if department == "Unassigned" and not department_outcomes:
            continue
        lines.extend(build_department_section(department, department_outcomes))
    lines.extend(build_digest_footer(notes))
    return "\n".join(lines)


def _department_of(outcome: Mapping[str, Any]) -> str:
    department = str(outcome.get("department") or "Unassigned").strip()
    return department if department in DEPARTMENTS else "Unassigned"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class VerificationDigestWorker:
    """Aggregate 24h of verification outcomes and email the daily digest."""

    def __init__(self, store: Any | None = None) -> None:
        self._store = store

    def _read_all_outcomes(self) -> dict[str, list[dict[str, Any]]]:
        if not _VERIFICATION_COMMON_AVAILABLE:
            raise RuntimeError("verification_common is unavailable; cannot aggregate outcomes")
        store = self._store
        if store is None:
            raise RuntimeError("no job store wired for the verification digest")
        return {
            job_type: [dict(item) for item in read_outcomes(store, job_type, since_hours=SINCE_HOURS)]
            for job_type in SOURCE_JOB_TYPES
        }

    def _delivery_config(self, job: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(job.get("payload") or {})
        sender = str(payload.get("email_sender") or "").strip()
        recipients = list(payload.get("digest_recipients") or ["carlo@streetsmart.insurance"])
        return {
            "email_sender": sender,
            "email_recipients": {DELIVERY_MODE: recipients},
            "chat_spaces": [],
        }

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        action = str(job.get("action_type") or JOB_TYPE)
        if action != JOB_TYPE:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error=f"verification digest worker received unexpected action type: {action}",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        payload = dict(job.get("payload") or {})
        output_dir_value = payload.get("output_dir")
        if not output_dir_value:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error="verification digest requires output_dir in the job payload",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        try:
            outcomes_by_job_type = self._read_all_outcomes()
        except RuntimeError as exc:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error=str(exc),
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )

        notes: list[str] = []
        if not _VERIFICATION_COMMON_AVAILABLE:
            notes.append("outcome aggregation unavailable; counts only")
        run_at = datetime.now(timezone.utc)
        report = build_digest_report(outcomes_by_job_type, run_at=run_at, notes=notes or None)
        output_dir = Path(str(output_dir_value)).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / f"verification-digest-{run_at:%Y%m%dT%H%M%SZ}.md"
        artifact.write_text(report + "\n", encoding="utf-8")
        sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
        destination = {"artifact_path": str(artifact)}
        detail = {
            "sha256": sha256,
            "mode": DELIVERY_MODE,
            "idempotency_key": idempotency_key,
            "counts": {job_type: len(items) for job_type, items in outcomes_by_job_type.items()},
        }

        if not is_action_authorized(job, SEND_DIGEST_EMAIL_ACTION):
            return WorkerResult(
                False,
                action,
                destination,
                detail,
                retryable=False,
                error="send_digest_email not authorized — digest artifact built but not delivered",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        delivery = self._delivery_config(job)
        try:
            receipts = accountability_delivery.deliver_report(
                artifact,
                mode=DELIVERY_MODE,
                delivery=delivery,
                environment=os.environ,
            )
        except Exception as exc:
            return WorkerResult(
                False,
                action,
                destination,
                {**detail, "delivery_receipts": []},
                retryable=True,
                error=f"verification digest delivery failed: {type(exc).__name__}: {exc}",
            )
        destination["delivery_receipts"] = receipts
        detail["delivery_receipts"] = receipts
        return WorkerResult(True, action, destination, detail, retryable=False)


class VerificationDigestVerifier:
    """Independently verify the digest artifact and its delivery.

    Fresh read-back only: the artifact is reread from disk and its SHA-256
    compared against the worker checkpoint, and every Gmail receipt is
    verified with a fresh read of the sent message in the sender's Sent
    mailbox (a send-API response alone is not proof).
    """

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        detail = dict(action.get("detail") or {})
        path = Path(str(destination.get("artifact_path") or ""))
        observed: dict[str, Any] = {"exists": path.is_file()}
        verified = False
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            observed.update(
                {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "contains_expected_title": DIGEST_TITLE in content,
                    "simulation_marker": "SIMULATION ONLY" in content,
                }
            )
            verified = (
                observed["sha256"] == detail.get("sha256")
                and observed["contains_expected_title"]
                and not observed["simulation_marker"]
            )
        receipts = list(destination.get("delivery_receipts") or detail.get("delivery_receipts") or [])
        if verified and receipts:
            try:
                delivery_verified, delivery_observed = (
                    accountability_delivery.verify_delivery_receipts(receipts)
                )
            except Exception as exc:
                delivery_verified, delivery_observed = False, [
                    {"error": f"{type(exc).__name__}: {exc}"}
                ]
            observed["delivery"] = delivery_observed
            verified = verified and delivery_verified
        evidence = VerificationEvidence(
            method="FRESH_DESTINATION_AND_FILESYSTEM_READBACK"
            if receipts
            else "FRESH_FILESYSTEM_READBACK",
            source="verification-digest-artifact-and-destinations",
            expected={
                "sha256": detail.get("sha256"),
                "mode": DELIVERY_MODE,
                "simulation_marker": False,
            },
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=str(path),
        )
        return VerificationResult(
            verified,
            evidence,
            retryable=False,
            error=None
            if verified
            else "verification digest failed artifact or delivery read-back",
        )
