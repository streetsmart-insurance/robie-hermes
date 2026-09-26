"""Shared infrastructure for the four daily verification workers.

Job types: ``manual_renewal_verification`` (report 4247), ``audit_verification``
(report 4246), ``mortgagee_verification`` (report 4372),
``policy_change_verification`` (report 4359), plus the ``daily_verification_digest``
aggregator.

Contents:
- :data:`DEPARTMENTS` — AppSheet department split for the digest.
- :class:`PolicyOutcome` — the per-policy done/not_done/pending record every
  worker writes.
- :func:`record_outcomes` / :func:`read_outcomes` — durable per-policy state in
  the job's ``action`` checkpoint (``data_json["detail"]["policies"]``) mirrored
  into ``durable_work_items`` under namespace ``<job_type>`` for idempotent resume.
- :func:`record_login_gap` / :func:`read_login_gaps` — structured portal-login
  gap reports (namespace ``<job_type>:login_gaps``) so walkthroughs with Carlo
  can be scheduled around exactly the portal + missing step.
- :data:`CONTRACT_ACTIONS` + :func:`is_action_authorized` — the contract gate:
  an action runs only if it is BOTH in the job payload's ``authorized_actions``
  AND in the per-job-type allowlist. Unauthorized intent must be recorded as
  ``pending`` and never executed.
- Voice helpers: :data:`VOICE_ENABLED_DEFAULT`, :func:`is_voice_call_authorized`,
  :func:`assert_voice_target_allowed` (hard invariant: never dial clients —
  only numbers from the carrier/mortgage-company directory), and the once-only
  ``carrier_voice_attempted`` guard helpers.
- Note builders: every EZLynx note carries the policy header and ends with the
  ``ROBIE was here`` signoff line.

Cardinal rule: nothing here deletes, mutates, or closes policies. Workers stay
within portal download + email + upload flows.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Collection

from .idempotency import DurableWorkLedger
from .store import JobStore, utc_now

logger = logging.getLogger("robie.verification_common")


# ---------------------------------------------------------------------------
# Departments (AppSheet split for the digest)
# ---------------------------------------------------------------------------

DEPARTMENTS = [
    "Commercial Lines",
    "Personal Lines",
    "Trucking and Transportation",
    "Operations",
    "Accounting",
]

POLICY_STATUSES = ("done", "not_done", "pending")

# ---------------------------------------------------------------------------
# Contract actions: per-job-type allowlist.
# An action executes ONLY if it is in the job payload's ``authorized_actions``
# AND in this allowlist. Anything else is recorded as pending, never executed.
#
# Voice was approved for carriers and mortgage companies across all four
# workers (Carlo 2026-09-10; previously deferred). The worker implementations
# gate on per-queue voice action names, so the contracts carry both the
# original generic ``place_voice_call`` and the concrete names the workers
# actually check. ``send_followup_email`` and ``portal_retrieval`` are the
# concrete names the manual-renewal worker gates its follow-up cadence and
# (stubbed) portal step on.
# ---------------------------------------------------------------------------

CONTRACT_ACTIONS: dict[str, frozenset[str]] = {
    "ezlynx.overdue_submission_reports": frozenset({"send_producer_reports"}),
    "manual_renewal_verification": frozenset(
        {
            "send_underwriter_email",
            "send_followup_email",
            "portal_retrieval",
            "post_ezlynx_note",
            "upload_document",
            "create_ezlynx_task",
            "place_carrier_voice_call",
        }
    ),
    "audit_verification": frozenset(
        {
            "send_carrier_email",
            "post_ezlynx_note",
            "place_voice_call",
            "place_carrier_voice_call",
        }
    ),
    "mortgagee_verification": frozenset(
        {
            "send_lender_email",
            "upload_lender_document",
            "post_ezlynx_note",
            "place_mortgagee_voice_call",
        }
    ),
    "policy_change_verification": frozenset(
        {
            "send_carrier_email",
            "post_ezlynx_note",
            "upload_document",
        }
    ),
    "daily_verification_digest": frozenset({"send_digest_email"}),
}

# Bland AI outbound is approved for carriers and mortgage companies in these
# workers (Carlo 2026-09-10). A job may still opt out via payload
# ``voice_enabled: false``.
VOICE_ENABLED_DEFAULT = True

# Hard invariant: voice may NEVER dial clients/applicants. Only these target
# kinds are dialable, and only with a number from the carrier/mortgage-company
# directory (never invented, never an applicant/client number).
VOICE_ALLOWED_TARGET_KINDS = frozenset({"carrier", "mortgage_company"})


class VoiceTargetNotAllowed(RuntimeError):
    """Raised when a voice-call target violates the never-dial-clients invariant."""


ROBIE_SIGNOFF = "ROBIE was here"


# ---------------------------------------------------------------------------
# PolicyOutcome
# ---------------------------------------------------------------------------


@dataclass
class PolicyOutcome:
    """Per-policy done / not_done / pending record written by each worker.

    Status semantics:
    - ``done`` — the worker completed its responsibility for this policy today.
    - ``pending`` — in-flight with a known next step and owner.
    - ``not_done`` — blocked or failed today (needs a human).
    """

    policy_number: str
    status: str
    reason: str = ""
    policy_aliases: list[str] = field(default_factory=list)
    applicant_id: str | None = None
    insured_name: str | None = None
    department: str | None = None
    carrier: str | None = None
    actions_taken: list[str] = field(default_factory=list)
    waiting_on: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.status not in POLICY_STATUSES:
            raise ValueError(
                f"invalid policy status {self.status!r}; must be one of {POLICY_STATUSES}"
            )
        if not str(self.policy_number or "").strip():
            raise ValueError("policy_number is required")
        if self.updated_at is None:
            self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to exactly the digest schema (key order is stable)."""
        return {
            "policy_number": self.policy_number,
            "policy_aliases": list(self.policy_aliases),
            "applicant_id": self.applicant_id,
            "insured_name": self.insured_name,
            "department": self.department,
            "carrier": self.carrier,
            "status": self.status,
            "reason": self.reason,
            "actions_taken": list(self.actions_taken),
            "waiting_on": self.waiting_on,
            "evidence": dict(self.evidence),
            "updated_at": self.updated_at,
        }


def _as_outcome_dict(outcome: PolicyOutcome | dict[str, Any]) -> dict[str, Any]:
    if isinstance(outcome, PolicyOutcome):
        return outcome.to_dict()
    data = dict(outcome)
    status = data.get("status")
    if status not in POLICY_STATUSES:
        raise ValueError(
            f"invalid policy status {status!r}; must be one of {POLICY_STATUSES}"
        )
    if not str(data.get("policy_number") or "").strip():
        raise ValueError("policy_number is required")
    data.setdefault("policy_aliases", [])
    data.setdefault("actions_taken", [])
    data.setdefault("evidence", {})
    data.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    return data


# ---------------------------------------------------------------------------
# Policy identity keying (for idempotent resume in durable_work_items)
# ---------------------------------------------------------------------------

# Report-identity hints per job type, mirroring report_registry identity_fields.
# Workers should also stash the report identity value in ``evidence`` (e.g.
# evidence["audit_id"]) so resume keying survives policy-number flips.
_JOB_TYPE_IDENTITY_HINTS: dict[str, tuple[str, ...]] = {
    "manual_renewal_verification": ("policy_number",),
    "audit_verification": ("audit_id",),
    "mortgagee_verification": ("policy_number",),
    "policy_change_verification": ("policy_number", "change_request_created_date"),
    "daily_verification_digest": ("policy_number",),
}


def _identity_value(evidence: dict[str, Any], outcome: dict[str, Any], name: str) -> str:
    for source in (evidence, outcome):
        raw = source.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _policy_identity_key(job_type: str, outcome: dict[str, Any]) -> str:
    evidence = outcome.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    hints = tuple(_JOB_TYPE_IDENTITY_HINTS.get(job_type, ()))
    # Composite report identity (4359: policy number + created date). Every
    # hint must be present so two requests on one policy do not collapse.
    if len(hints) > 1:
        parts: list[str] = []
        missing: list[str] = []
        for name in hints:
            value = _identity_value(evidence, outcome, name)
            if not value:
                missing.append(name)
            else:
                parts.append(f"{name}:{value}")
        if missing:
            raise ValueError(
                f"cannot derive a policy identity key for job type {job_type!r}: "
                f"missing {', '.join(missing)}"
            )
        return "policy:" + "|".join(parts)
    candidates = list(hints)
    candidates.extend(["policy_number", "applicant_id"])
    for name in candidates:
        value = _identity_value(evidence, outcome, name)
        if value:
            return f"policy:{name}:{value}"
    raise ValueError(f"cannot derive a policy identity key for job type {job_type!r}")


def _write_work_item_outcome(
    db_path: str, namespace: str, work_item_key: str, outcome_json: str, now: str
) -> None:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """UPDATE durable_work_items
               SET outcome=?, updated_at=?
               WHERE namespace=? AND work_item_key=?""",
            (outcome_json, now, namespace, work_item_key),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# record_outcomes / read_outcomes
# ---------------------------------------------------------------------------


def record_outcomes(
    store: JobStore,
    job_id: str,
    job_type: str,
    outcomes: list[PolicyOutcome | dict[str, Any]],
) -> None:
    """Write per-policy outcomes to the job's ``action`` checkpoint AND mirror
    each into ``durable_work_items`` (namespace ``<job_type>``, keyed by policy
    identity) for idempotent resume.

    Merge semantics: an outcome for an already-recorded policy identity
    replaces the previous one; new identities are appended.
    """
    import json as _json

    now = utc_now()
    normalized = [_as_outcome_dict(item) for item in (outcomes or [])]
    for item in normalized:
        if not item.get("updated_at"):
            item["updated_at"] = now

    existing = store.get_checkpoint(job_id, "action") or {}
    detail = dict(existing.get("detail") or {})
    merged: dict[str, dict[str, Any]] = {}
    for item in detail.get("policies") or []:
        item_dict = dict(item)
        try:
            merged[_policy_identity_key(job_type, item_dict)] = item_dict
        except ValueError:
            logger.warning(
                "record_outcomes: skipping stored policy without identity (job %s)",
                job_id,
            )
    for item in normalized:
        merged[_policy_identity_key(job_type, item)] = item
    detail["policies"] = list(merged.values())
    detail["policies_updated_at"] = now
    store.checkpoint(
        job_id,
        "action",
        {
            "action": existing.get("action") or job_type,
            "destination": existing.get("destination") or {},
            "detail": detail,
        },
    )

    ledger = DurableWorkLedger(store.path)
    for item in normalized:
        key = _policy_identity_key(job_type, item)
        ledger.reserve(job_type, key)
        _write_work_item_outcome(
            ledger.path, job_type, key, _json.dumps(item, sort_keys=True), now
        )
    logger.info(
        "record_outcomes: job=%s type=%s policies=%d", job_id, job_type, len(normalized)
    )


def read_outcomes(
    store: JobStore, job_type: str, *, since_hours: int = 24
) -> list[dict[str, Any]]:
    """Read back per-policy outcomes for ``job_type`` (for the digest).

    Reads the ``durable_work_items`` mirror (namespace ``<job_type>``), which
    is the resume source of truth. Engine-internal ledger rows (non-JSON
    outcomes) are skipped.
    """
    import json as _json

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    conn = sqlite3.connect(store.path, timeout=30)
    results: list[dict[str, Any]] = []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT work_item_key, outcome, updated_at FROM durable_work_items
               WHERE namespace=? AND work_item_key LIKE 'policy:%' AND updated_at >= ?
               ORDER BY updated_at ASC""",
            (job_type, cutoff),
        ).fetchall()
        for row in rows:
            raw = row["outcome"]
            if not raw:
                continue
            try:
                item = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict) and item.get("policy_number"):
                results.append(item)
    finally:
        conn.close()
    return results


# ---------------------------------------------------------------------------
# Login-gap reporter
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold())
    return text.strip("-") or "unknown"


def record_login_gap(
    store: JobStore,
    job_id: str,
    job_type: str,
    portal_name: str,
    step: str,
    whats_missing: str,
) -> dict[str, Any]:
    """Record a portal-login gap the SOPs don't cover.

    Workers call this whenever a portal login flow stalls (unknown MFA step,
    unexpected credential field, changed login URL, ...) so the digest can
    report exactly which portal + what's missing and walkthroughs with Carlo
    can be scheduled around those gaps.

    Stored under namespace ``<job_type>:login_gaps`` in ``durable_work_items``
    and appended to the job's ``action`` checkpoint ``detail["login_gaps"]``.
    """
    import json as _json

    if not str(portal_name or "").strip():
        raise ValueError("portal_name is required")
    if not str(whats_missing or "").strip():
        raise ValueError("whats_missing is required")
    now = utc_now()
    namespace = f"{job_type}:login_gaps"
    key = f"gap:{_slug(portal_name)}:{_slug(step)}:{now[:10]}"
    gap = {
        "portal_name": str(portal_name).strip(),
        "step": str(step or "").strip(),
        "whats_missing": str(whats_missing).strip(),
        "job_id": job_id,
        "job_type": job_type,
        "recorded_at": now,
    }
    ledger = DurableWorkLedger(store.path)
    ledger.reserve(namespace, key)
    _write_work_item_outcome(
        ledger.path, namespace, key, _json.dumps(gap, sort_keys=True), now
    )

    existing = store.get_checkpoint(job_id, "action") or {}
    detail = dict(existing.get("detail") or {})
    gaps = [dict(item) for item in (detail.get("login_gaps") or [])]
    if not any(
        item.get("portal_name") == gap["portal_name"]
        and item.get("step") == gap["step"]
        and item.get("whats_missing") == gap["whats_missing"]
        for item in gaps
    ):
        gaps.append(gap)
    detail["login_gaps"] = gaps
    detail["login_gaps_updated_at"] = now
    store.checkpoint(
        job_id,
        "action",
        {
            "action": existing.get("action") or job_type,
            "destination": existing.get("destination") or {},
            "detail": detail,
        },
    )
    logger.info(
        "record_login_gap: job=%s portal=%s step=%s",
        job_id,
        gap["portal_name"],
        gap["step"],
    )
    return gap


def read_login_gaps(
    store: JobStore, job_type: str, *, since_hours: int = 24 * 7
) -> list[dict[str, Any]]:
    """Read back recorded login gaps for ``job_type`` (for the digest)."""
    import json as _json

    namespace = f"{job_type}:login_gaps"
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    conn = sqlite3.connect(store.path, timeout=30)
    results: list[dict[str, Any]] = []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT work_item_key, outcome, updated_at FROM durable_work_items
               WHERE namespace=? AND work_item_key LIKE 'gap:%' AND updated_at >= ?
               ORDER BY updated_at ASC""",
            (namespace, cutoff),
        ).fetchall()
        for row in rows:
            raw = row["outcome"]
            if not raw:
                continue
            try:
                item = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict) and item.get("portal_name"):
                results.append(item)
    finally:
        conn.close()
    return results


# ---------------------------------------------------------------------------
# Contract gate
# ---------------------------------------------------------------------------


def is_action_authorized(job: dict[str, Any], action_name: str) -> bool:
    """True only if ``action_name`` is in the job payload's ``authorized_actions``
    AND in the module-level :data:`CONTRACT_ACTIONS` allowlist for the job type.

    Workers must call this before any external action. If it returns False, the
    worker records the intended action as ``pending`` and does NOT execute it.
    """
    job = job or {}
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    job_type = str(job.get("action_type") or "")
    authorized = payload.get("authorized_actions") or []
    allowed = CONTRACT_ACTIONS.get(job_type, frozenset())
    name = str(action_name or "")
    return name in {str(item) for item in authorized} and name in allowed


def is_voice_call_authorized(job: dict[str, Any]) -> bool:
    """Voice additionally requires payload ``voice_enabled``.

    Defaults to :data:`VOICE_ENABLED_DEFAULT` (True — Bland AI outbound to
    carriers/mortgage companies is approved); a job may explicitly opt out with
    ``voice_enabled: false``.
    """
    job = job or {}
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    return is_action_authorized(job, "place_voice_call") and bool(
        payload.get("voice_enabled", VOICE_ENABLED_DEFAULT)
    )


def pending_intent_outcome(
    job_type: str,
    *,
    policy_number: str,
    action_name: str,
    reason: str | None = None,
    **kwargs: Any,
) -> PolicyOutcome:
    """Build a ``pending`` outcome for an action that was NOT executed because
    the contract gate refused it."""
    return PolicyOutcome(
        policy_number=policy_number,
        status="pending",
        reason=reason
        or f"action not authorized for {job_type}: {action_name} (recorded, not executed)",
        waiting_on="authorization",
        evidence={"intended_action": action_name, "executed": False},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Voice target invariant: never dial clients
# ---------------------------------------------------------------------------


def _normalize_voice_number(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise VoiceTargetNotAllowed(
            "voice target must be a dialable 10-digit NANP number; "
            "never invent phone numbers"
        )
    return "+1" + digits


def assert_voice_target_allowed(
    *,
    phone: str,
    kind: str,
    directory_numbers: Collection[str] | None = None,
) -> str:
    """Enforce the never-dial-clients invariant. Returns the normalized number.

    Raises :class:`VoiceTargetNotAllowed` unless ``kind`` is ``"carrier"`` or
    ``"mortgage_company"`` AND the number is present in the provided
    carrier/mortgage-company directory. Applicant/client numbers, unknown kinds,
    and numbers not in the directory are all refused — never invent numbers.
    """
    if kind not in VOICE_ALLOWED_TARGET_KINDS:
        raise VoiceTargetNotAllowed(
            f"voice calls are never placed to {kind!r}; "
            f"allowed target kinds: {sorted(VOICE_ALLOWED_TARGET_KINDS)}"
        )
    normalized = _normalize_voice_number(phone)
    directory = {_normalize_voice_number(item) for item in (directory_numbers or [])}
    if normalized not in directory:
        raise VoiceTargetNotAllowed(
            "voice target number is not in the carrier/mortgage-company "
            "directory; refusing to dial"
        )
    return normalized


def carrier_voice_attempted(outcome: dict[str, Any]) -> bool:
    """Once-only guard: has the carrier voice call already fired for this policy?"""
    evidence = (outcome or {}).get("evidence") or {}
    return bool(evidence.get("carrier_voice_attempted"))


def mark_carrier_voice_attempted(outcome: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the outcome dict with the once-only voice guard set."""
    updated = dict(outcome or {})
    evidence = dict(updated.get("evidence") or {})
    evidence["carrier_voice_attempted"] = True
    evidence["carrier_voice_attempted_at"] = datetime.now(timezone.utc).isoformat()
    updated["evidence"] = evidence
    return updated


# ---------------------------------------------------------------------------
# EZLynx note builders — every note carries the policy header and ends with
# the ROBIE was here signoff on its own final line.
# ---------------------------------------------------------------------------


def policy_note_header(policy_number: str, lob: str, carrier: str) -> str:
    """``Policy: #{num} ({LOB} - {carrier})``."""
    return f"Policy: #{policy_number} ({lob} - {carrier})"


def _with_signoff(body: str) -> str:
    return body.rstrip("\n") + "\n" + ROBIE_SIGNOFF


def build_portal_check_note(
    *, header: str, carrier: str, lines: list[str] | None = None
) -> str:
    """Portal document-retrieval check note."""
    body_lines = [header, f"Carrier portal check — {carrier}"]
    body_lines.extend(lines or [])
    return _with_signoff("\n".join(body_lines))


def build_outreach_email_note(
    *,
    header: str,
    subject: str,
    sent_to: str,
    lines: list[str] | None = None,
) -> str:
    """Outbound outreach email filing note."""
    body_lines = [header, f"Outreach email sent — subject: {subject}", f"To: {sent_to}"]
    body_lines.extend(lines or [])
    return _with_signoff("\n".join(body_lines))


def build_reply_received_note(
    *, header: str, lines: list[str] | None = None
) -> str:
    """Inbound carrier/lender reply filing note."""
    body_lines = [header, "Reply received and filed"]
    body_lines.extend(lines or [])
    return _with_signoff("\n".join(body_lines))


def build_confirmation_report_note(
    *,
    header: str,
    requested: str,
    carrier_issued: str,
    ezlynx_recorded: str,
    matches: list[str] | None = None,
    exceptions: list[str] | None = None,
    documents: list[str] | None = None,
    result: str = "",
    next_action: str = "",
) -> str:
    """Policy-change three-way confirmation report note."""
    sections = [
        header,
        "Policy change confirmation report",
        f"Requested: {requested}",
        f"Carrier issued: {carrier_issued}",
        f"EZLynx recorded: {ezlynx_recorded}",
        "Matches:",
        *[f"  - {item}" for item in (matches or [])],
        "Exceptions:",
        *[f"  - {item}" for item in (exceptions or [])],
        "Documents:",
        *[f"  - {item}" for item in (documents or [])],
        f"Result: {result}",
        f"Next action: {next_action}",
    ]
    return _with_signoff("\n".join(sections))
