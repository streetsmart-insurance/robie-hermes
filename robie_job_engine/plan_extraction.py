"""Request -> structured plan-draft extraction for the evidence-loop pilot.

The executing worker is an AI; this module is the deterministic harness
around its judgment. It defines:

- the exact JSON contract the model must return (``LLM_PLAN_PROMPT``),
- strict validation of the model's output (never trust, always verify),
- ambiguity detection that fails closed to human review.

A draft is NOT a locked plan. :func:`draft_to_locked_plan` refuses any draft
with ``needs_human_review=True`` unless a named human explicitly confirms it.
Nothing here calls a model directly: the caller supplies ``llm_json_fn`` (in
production, the Robie worker), which keeps this module unit-testable with
fakes and keeps model choice out of the pilot's critical path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .evidence import utc_now_iso
from .evidence_fetchers import _POLICY_FIELD_KEYS
from .policy_change_plan import extract_policy_change_plan


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Canonical field names the pilot understands. The seven Tier A names come
# from the PolicyApi field map; anything else must look like a coverage field
# (Tier B: fresh page read) or the draft goes to human review.
CANONICAL_FIELDS = frozenset(_POLICY_FIELD_KEYS.keys())

_TIER_B_HINTS = ("coverage", "limit", "deductible", "cov_a", "cov_b", "cov_c",
                 "cov_d", "cov_e", "cov_f")

# Natural-language -> canonical field name. First match wins; keep it small
# and obvious -- anything cleverer belongs in the model's judgment, and the
# quote check below keeps the model honest.
_FIELD_ALIASES: tuple[tuple[str, str], ...] = (
    ("written premium", "writtenPremium"),
    ("premium", "writtenPremium"),
    ("full term premium", "fullTermPremium"),
    ("full-term premium", "fullTermPremium"),
    ("expiration date", "expirationDate"),
    ("expiry date", "expirationDate"),
    ("expiration", "expirationDate"),
    ("expiry", "expirationDate"),
    ("renewal date", "expirationDate"),
    ("effective date", "effectiveDate"),
    ("policy status", "policyStatus"),
    ("status", "policyStatus"),
    ("carrier", "carrier"),
    ("policy number", "policyNumber"),
)


def normalize_field_name(name: str) -> str | None:
    """Map a natural-language or variant field name to its canonical form."""
    raw = str(name or "").strip()
    if not raw:
        return None
    if raw in CANONICAL_FIELDS:
        return raw
    lowered = raw.casefold()
    for alias, canonical in _FIELD_ALIASES:
        if lowered == alias:
            return canonical
    if any(hint in lowered for hint in _TIER_B_HINTS):
        # Tier B coverage-style field: pass through in camelCase if it
        # already is, else keep the model's spelling -- the tier classifier
        # in policy_change_plan decides A vs B by the same hints.
        return raw
    return None


def is_known_field(name: str) -> bool:
    return normalize_field_name(name) is not None


# ---------------------------------------------------------------------------
# The model contract
# ---------------------------------------------------------------------------

LLM_PLAN_PROMPT = """\
You extract a structured policy-change plan from an insurance agency change request.
Return JSON ONLY -- no prose, no markdown fences -- exactly this schema:

{
  "applicant_id": string | null,
  "policy_number": string | null,
  "changes": [
    {"field": "<canonical field name>", "value": <string or number>,
     "quote": "<exact substring of the request supporting this change>"}
  ],
  "uncertainties": ["<anything ambiguous, conflicting, or unclear>"]
}

Field names must be one of: policyNumber, writtenPremium, fullTermPremium,
policyStatus, effectiveDate, expirationDate, carrier, or a coverage field whose
name contains "coverage", "limit", or "deductible" (for example coverageA_limit).

Rules -- follow them exactly:
1. NEVER invent applicant_id or policy_number. If the request does not state
   them and no context values are given below, use null.
2. NEVER guess a value. If the request is ambiguous about which value applies,
   describe it in "uncertainties" and OMIT the change from "changes".
3. "quote" must be an EXACT substring of the request text. If you cannot quote
   the source for a change, omit the change.
4. If the request states two different values for the same thing, report the
   conflict in "uncertainties" and omit the change.
5. Premiums as plain numbers (1250.00, never "$1,250"). Dates as YYYY-MM-DD.
6. Ignore greetings, signatures, disclaimers, and anything not about the change.
7. One entry per changed field. Do not repeat a field.
"""

_PROMPT_CONTEXT_TMPL = """
Known context (from the agency's own records, not from the request text):
  applicant_id: {applicant_id}
  policy_number: {policy_number}
Use these values. If the request text clearly names different ones, prefer the
request text and describe the conflict in "uncertainties".
"""


def build_extraction_prompt(
    request_text: str,
    *,
    applicant_id: str | None = None,
    policy_number: str | None = None,
) -> str:
    """Assemble the full prompt: contract + optional known context + request."""
    parts = [LLM_PLAN_PROMPT]
    if applicant_id or policy_number:
        parts.append(
            _PROMPT_CONTEXT_TMPL.format(
                applicant_id=applicant_id or "(unknown)",
                policy_number=policy_number or "(unknown)",
            )
        )
    parts.append("Request text:\n" + str(request_text or ""))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


class PlanExtractionError(RuntimeError):
    """The model's output could not be turned into a draft at all."""


class PlanNeedsHumanReview(RuntimeError):
    """Refusal to lock a draft that still needs a human. Fail closed."""


@dataclass
class PlanDraft:
    """A structured, validated, but NOT YET locked plan."""

    applicant_id: str | None
    policy_number: str | None
    changes: dict[str, Any]
    evidence_spans: dict[str, str] = field(default_factory=dict)
    uncertainties: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    request_excerpt: str = ""
    extracted_at: str = ""

    def to_locked_plan(self, job_id: str, *, human_confirmed_by: str | None = None):
        """Convert to a LockedPlan. Refuses unreviewed drafts (fail closed)."""
        return draft_to_locked_plan(
            self, job_id, human_confirmed_by=human_confirmed_by
        )


# ---------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"^\d+(\.\d{1,2})?$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _sane_value(field_name: str, value: Any) -> str | None:
    """Return a reason string when a value is not sane for its field."""
    if field_name in ("writtenPremium", "fullTermPremium"):
        text = str(value).replace(",", "").strip().lstrip("$")
        if not _MONEY_RE.match(text):
            return f"{field_name} value {value!r} is not a plain non-negative amount"
        if float(text) < 0:
            return f"{field_name} value {value!r} is negative"
        return None
    if field_name in ("effectiveDate", "expirationDate"):
        if not _DATE_RE.match(str(value).strip()):
            return (
                f"{field_name} value {value!r} is not a YYYY-MM-DD date"
            )
        return None
    if not str(value or "").strip():
        return f"{field_name} has an empty value"
    return None


def _validate_extracted(
    data: Mapping[str, Any],
    request_text: str,
    *,
    applicant_id: str | None,
    policy_number: str | None,
    policy_exists_fn: Callable[[str], bool] | None,
) -> PlanDraft:
    text = str(request_text or "")
    reasons: list[str] = []
    uncertainties: list[str] = list(data.get("uncertainties") or [])

    # Identity: explicit IDs win; context fills gaps; nothing is invented.
    app_id = str(data.get("applicant_id") or "").strip() or (
        str(applicant_id).strip() if applicant_id else ""
    )
    pol_no = str(data.get("policy_number") or "").strip() or (
        str(policy_number).strip() if policy_number else ""
    )

    changes: dict[str, Any] = {}
    spans: dict[str, str] = {}
    seen_fields: set[str] = set()
    raw_changes = data.get("changes")
    if not isinstance(raw_changes, list):
        reasons.append("model did not return a changes list")
        raw_changes = []

    lowered_text = text.casefold()
    # Whitespace-normalized copies: the anti-hallucination check below must
    # compare words, not line breaks. A model quoting "effective 10/01/2026"
    # for a request containing "effective\n10/01/2026" is quoting the actual
    # request -- rejecting it would punish the model for formatting.
    normalized_text = re.sub(r"\s+", " ", lowered_text).strip()
    for entry in raw_changes:
        if not isinstance(entry, Mapping):
            reasons.append(f"ignoring malformed change entry: {entry!r}")
            continue
        raw_field = str(entry.get("field") or "").strip()
        canonical = normalize_field_name(raw_field)
        if canonical is None:
            reasons.append(
                f"unknown field {raw_field!r} -- not in the pilot vocabulary"
            )
            continue
        if canonical in seen_fields:
            reasons.append(f"field {canonical!r} listed twice")
            continue
        seen_fields.add(canonical)
        value = entry.get("value")
        quote = str(entry.get("quote") or "")
        if not quote:
            reasons.append(f"field {canonical!r} has no supporting quote")
            continue
        quote_norm = re.sub(r"\s+", " ", quote.casefold()).strip()
        if not quote_norm or quote_norm not in normalized_text:
            # Anti-hallucination: the model must quote the actual request
            # (same words in the same order; whitespace may differ).
            reasons.append(
                f"field {canonical!r} quotes text not found in the request"
            )
            continue
        sane_reason = _sane_value(canonical, value)
        if sane_reason is not None:
            reasons.append(sane_reason)
            continue
        # Normalize premiums to a plain numeric string for the locked plan.
        if canonical in ("writtenPremium", "fullTermPremium"):
            value = str(str(value).replace(",", "").strip().lstrip("$"))
        changes[canonical] = value
        spans[canonical] = quote

    if not changes:
        reasons.append("no usable changes extracted from the request")

    if not app_id:
        reasons.append("applicant could not be identified from the request")
    if not pol_no:
        reasons.append("policy number could not be identified from the request")

    if uncertainties:
        reasons.append(
            f"model reported {len(uncertainties)} uncertaint"
            + ("y" if len(uncertainties) == 1 else "ies")
        )

    if pol_no and policy_exists_fn is not None:
        try:
            exists = policy_exists_fn(pol_no)
        except Exception as exc:
            reasons.append(f"could not verify policy {pol_no!r} exists: {exc}")
        else:
            if not exists:
                reasons.append(
                    f"policy {pol_no!r} did not resolve at the destination"
                )

    return PlanDraft(
        applicant_id=app_id or None,
        policy_number=pol_no or None,
        changes=changes,
        evidence_spans=spans,
        uncertainties=uncertainties,
        needs_human_review=bool(reasons),
        review_reasons=reasons,
        request_excerpt=text[:500],
        extracted_at=utc_now_iso(),
    )


def parse_llm_plan_json(raw: str) -> dict[str, Any]:
    """Parse the model's raw output. Strict: JSON object, nothing else."""
    text = str(raw or "").strip()
    # Tolerate markdown fences without tolerating prose around them.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PlanExtractionError(
            f"model output is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PlanExtractionError(
            f"model output must be a JSON object, got {type(data).__name__}"
        )
    return data


def extract_plan_draft(
    request_text: str,
    *,
    llm_json_fn: Callable[[str], str],
    applicant_id: str | None = None,
    policy_number: str | None = None,
    policy_exists_fn: Callable[[str], bool] | None = None,
) -> PlanDraft:
    """Turn a raw change request into a validated PlanDraft.

    ``llm_json_fn`` receives the extraction prompt and returns the model's
    raw text. Any model failure -- bad JSON, hallucinated quotes, unknown
    fields, unresolvable identity -- becomes ``needs_human_review=True``,
    never an exception and never a silently partial plan. The one exception
    is a completely empty request, which is a caller error.
    """
    text = str(request_text or "")
    if not text.strip():
        raise ValueError("extract_plan_draft requires a non-empty request")
    prompt = build_extraction_prompt(
        text, applicant_id=applicant_id, policy_number=policy_number
    )
    try:
        raw = llm_json_fn(prompt)
    except Exception as exc:
        return PlanDraft(
            applicant_id=str(applicant_id).strip() or None,
            policy_number=str(policy_number).strip() or None,
            changes={},
            uncertainties=[],
            needs_human_review=True,
            review_reasons=[f"model call failed: {exc}"],
            request_excerpt=text[:500],
            extracted_at=utc_now_iso(),
        )
    try:
        data = parse_llm_plan_json(raw)
    except PlanExtractionError as exc:
        return PlanDraft(
            applicant_id=str(applicant_id).strip() or None,
            policy_number=str(policy_number).strip() or None,
            changes={},
            uncertainties=[],
            needs_human_review=True,
            review_reasons=[str(exc)],
            request_excerpt=text[:500],
            extracted_at=utc_now_iso(),
        )
    return _validate_extracted(
        data,
        text,
        applicant_id=applicant_id,
        policy_number=policy_number,
        policy_exists_fn=policy_exists_fn,
    )


def draft_to_locked_plan(
    draft: PlanDraft,
    job_id: str,
    *,
    human_confirmed_by: str | None = None,
) -> Any:
    """Convert a validated draft into a LockedPlan. Fail closed.

    A draft with ``needs_human_review=True`` is refused unless a named human
    explicitly confirms it. The confirmation name is recorded as the locker.
    """
    if not isinstance(draft, PlanDraft):
        raise TypeError("draft_to_locked_plan requires a PlanDraft")
    confirmed = str(human_confirmed_by or "").strip()
    if draft.needs_human_review and not confirmed:
        raise PlanNeedsHumanReview(
            "draft needs human review: " + "; ".join(draft.review_reasons)
        )
    if not draft.applicant_id or not draft.policy_number:
        raise PlanNeedsHumanReview(
            "draft is missing applicant or policy identity; refusing to lock"
        )
    if not draft.changes:
        raise PlanNeedsHumanReview("draft has no changes; refusing to lock")
    return extract_policy_change_plan(
        job_id,
        applicant_id=draft.applicant_id,
        policy_number=draft.policy_number,
        changes=dict(draft.changes),
        locked_by=f"human:{confirmed}" if confirmed else "plan_extraction",
    )


def draft_summary_text(draft: PlanDraft) -> str:
    """One-screen human-readable summary for the confirmation gate."""
    lines = [
        f"Policy change draft ({draft.extracted_at or 'undated'}):",
        f"  applicant: {draft.applicant_id or 'UNKNOWN'}",
        f"  policy:    {draft.policy_number or 'UNKNOWN'}",
        "  changes:",
    ]
    for name, value in draft.changes.items():
        quote = draft.evidence_spans.get(name, "")
        lines.append(f"    - {name} -> {value}  (request says: {quote[:80]!r})")
    if draft.uncertainties:
        lines.append("  model uncertainties:")
        for u in draft.uncertainties:
            lines.append(f"    ! {u}")
    if draft.needs_human_review:
        lines.append("  NEEDS HUMAN REVIEW:")
        for r in draft.review_reasons:
            lines.append(f"    ! {r}")
    else:
        lines.append("  ready to lock: no review flags")
    return "\n".join(lines)
