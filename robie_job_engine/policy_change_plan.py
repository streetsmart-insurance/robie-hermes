"""Pilot job type: policy changes.

Plan extractor for the policy-change workflow: pulls the exact fields a
change request is asking for and locks them as the checklist before any
work starts.

Evidence mapping for this pilot:
- Tier A (PolicyApi read-back): premium, fullTermPremium, policyStatus,
  effective/expiration dates. These are the fields PolicyApi can actually
  see (proven read-backs).
- Tier B (fresh page read): everything else -- coverage A-F
  limits/deductibles are NOT in the PolicyApi ACORD XML, so they can only
  be verified by re-reading the page. The extractor marks them Tier B with
  a note instead of pretending Tier A covers them.

Carrier systems are slow: the default settle delay gives the destination
time to reflect a submitted change before evidence may call MISMATCH.
"""

from __future__ import annotations

from typing import Any

from .evidence import EvidenceSpan, Idempotency, LockedPlan, utc_now_iso
from .evidence_fetchers import _POLICY_FIELD_KEYS

JOB_TYPE_POLICY_CHANGE = "policy_change"

# PolicyApi is EZLynx-internal, but the write path (browser FormEntry) and
# any carrier-side posting behind it are not instant. Ten minutes keeps the
# pattern from crying wolf on healthy jobs; override per plan when a
# destination is known faster or slower.
POLICY_CHANGE_SETTLE_DELAY_SECONDS = 600

# Fields PolicyApi read-back can verify. Anything else the request asks for
# is Tier B (fresh page read) -- recorded, never silently dropped.
API_VISIBLE_POLICY_FIELDS = frozenset(_POLICY_FIELD_KEYS)

# Field-name fragments that are never API-visible (ACORD XML has no
# Coverage A-F limits/deductibles). Matched case-insensitively.
TIER_B_FIELD_HINTS = (
    "coverage",
    "limit",
    "deductible",
    "cov_a",
    "cov_b",
    "cov_c",
    "cov_d",
    "cov_e",
    "cov_f",
)


def _evidence_tier_for(field_name: str) -> tuple[str, str]:
    """Return (tier, note) for a requested change field."""

    name = str(field_name or "").strip()
    if name in API_VISIBLE_POLICY_FIELDS:
        return "A", "PolicyApi read-back"
    lowered = name.casefold()
    if any(hint in lowered for hint in TIER_B_FIELD_HINTS):
        return (
            "B",
            "not visible to PolicyApi (no coverage limits/deductibles in ACORD XML); "
            "requires fresh page read",
        )
    return (
        "B",
        "no Tier A mapping for this field; requires fresh page read",
    )


def extract_policy_change_plan(
    job_id: str,
    *,
    applicant_id: str,
    policy_number: str,
    changes: dict[str, Any],
    settle_delay_seconds: int = POLICY_CHANGE_SETTLE_DELAY_SECONDS,
    locked_by: str = "planner",
    field_spans: dict[str, EvidenceSpan] | None = None,
) -> LockedPlan:
    """Build the locked checklist for one policy-change job.

    ``changes`` maps field name -> expected value after the change, e.g.
    ``{"writtenPremium": 1284.00, "expirationDate": "2027-09-01"}``.

    ``field_spans`` optionally maps field name -> the EvidenceSpan binding
    the value to its source text; it is stored on the locked plan so
    graders and renderers can show and check provenance. Plans built
    without spans simply carry an empty mapping (callers that have
    provenance -- e.g. ``plan_extraction.draft_to_locked_plan`` -- pass it).

    Raises:
        ValueError: applicant/policy/changes missing -- a plan built from
            an unreadable request fails here, not mid-job.
    """

    job = str(job_id or "").strip()
    applicant = str(applicant_id or "").strip()
    number = str(policy_number or "").strip()
    if not job:
        raise ValueError("policy-change plan requires a job_id")
    if not applicant:
        raise ValueError("policy-change plan requires an applicant_id")
    if not number:
        raise ValueError("policy-change plan requires a policy_number")
    if not isinstance(changes, dict) or not changes:
        raise ValueError("policy-change plan requires at least one changed field")

    fields: dict[str, Any] = {}
    field_tiers: dict[str, str] = {}
    field_notes: dict[str, str] = {}
    for raw_name, expected in changes.items():
        name = str(raw_name or "").strip()
        if not name:
            raise ValueError("policy-change plan has an unnamed changed field")
        if name in fields:
            raise ValueError(f"policy-change plan lists field {name!r} twice")
        tier, note = _evidence_tier_for(name)
        fields[name] = expected
        field_tiers[name] = tier
        field_notes[name] = note
    # The policy itself must resolve at the destination; that is evidence too.
    fields.setdefault("policyNumber", number)
    field_tiers.setdefault("policyNumber", "A")
    field_notes.setdefault("policyNumber", "PolicyApi read-back")

    return LockedPlan(
        job_id=job,
        job_type=JOB_TYPE_POLICY_CHANGE,
        fields=fields,
        field_tiers=field_tiers,
        field_notes=field_notes,
        field_spans=dict(field_spans) if field_spans else {},
        settle_delay_seconds=int(settle_delay_seconds),
        locked_at=utc_now_iso(),
        locked_by=locked_by,
        # Idempotency: every change here is a field set to an exact value
        # (FormEntry write). Re-running sets the same value again -- safe.
        # If this pilot ever gains append-style steps (notes, emails),
        # those steps must declare VERIFY_BEFORE_RETRY or NON_IDEMPOTENT
        # instead.
        idempotency=Idempotency.IDEMPOTENT,
    )


def tier_a_fields(plan: LockedPlan) -> dict[str, Any]:
    """Expected values for the fields Tier A evidence can check."""

    return {
        name: expected
        for name, expected in plan.fields.items()
        if plan.field_tiers.get(name) == "A"
    }


def tier_b_fields(plan: LockedPlan) -> dict[str, str]:
    """Field -> note for fields needing a Tier B fresh page read."""

    return {
        name: plan.field_notes.get(name, "")
        for name in plan.fields
        if plan.field_tiers.get(name) != "A"
    }


def plan_summary_text(plan: LockedPlan) -> str:
    """One plain-English line describing the locked plan (Chat/digest use)."""

    changed = [n for n in plan.fields if n != "policyNumber"]
    tier_b = tier_b_fields(plan)
    summary = (
        f"Policy change on {plan.fields.get('policyNumber')}: "
        f"{len(changed)} field(s) locked "
        f"({', '.join(changed) if changed else 'none'})"
    )
    if tier_b:
        summary += (
            f"; {len(tier_b)} field(s) need a page read to verify "
            f"({', '.join(tier_b)})"
        )
    return summary
