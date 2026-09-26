"""Dynamic passing-grade registry for evidence-loop job types.

The evidence loop itself is generic (Plan -> Execute -> Evidence -> Outcome),
but what counts as PASSING is per job type: a policy change passes when every
locked field matches at the destination; a carrier website quote passes when
the quote document exists and the quoted values match; a carrier call passes
when the call completed and its outcome was captured.

This module is the deterministic harness around that judgment:

- the registry is DATA (``grades.yaml``), not code: adding a job type is a
  config entry, never a code change;
- the grader never calls a model: it evaluates evidence items against the
  job type's declared ``passing_rule``;
- unknown job types FAIL CLOSED -- they cannot be graded, so they cannot
  pass.

Evidence item shapes (duck-typed dicts):

- ``{"kind": "field_match", "field": str, "matched": bool,
  "expected": ..., "observed": ...}``
- ``{"kind": "quote_document", "present": bool, "document_ref": str}``
- ``{"kind": "quoted_value_match", "field": str, "matched": bool, ...}``
- ``{"kind": "call_record", "completed": bool, "disposition": str,
  "recording_ref": str | None, "notes_ref": str | None}``

Items graded through the H2 enforcement point
(``board.grade_locked_plan`` / ``board.grade_derived_evidence``) must also
carry provenance -- a non-empty ``provenance`` mapping (fetch citation) or
a ``span`` object. Items without provenance are UNGRADABLE: the enforcer
refuses to grade them rather than grading clean.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


_REGISTRY_PATH = Path(__file__).with_name("grades.yaml")
_ENV_OVERRIDE = "ROBIE_GRADES_YAML"

_REQUIRED_SPEC_KEYS = (
    "label",
    "description",
    "evidence_kinds",
    "passing_rule",
    "extraction_model",
)


# Required-field-set modes for the H2 enforcement point. Declared per job
# type via the ``required_fields`` spec key (see grades.yaml):
#   "plan"              every field in the locked plan's own field set
#   "document_and_plan" quote document present + every requested value
#   "call_record"       a completed call record with a disposition
_REQUIRED_FIELD_MODES = (
    "plan",
    "document_and_plan",
    "call_record",
)


# ---------------------------------------------------------------------------
# Grade-spec pinning: bind a locked plan to the spec in force at lock time.
#
# The registry can be repointed after lock (ROBIE_GRADES_YAML env override)
# or mutated in-process (register_job_type). Either would silently change
# what PASS means for an already-locked plan. The defense: plan_lock pins
# the SHA-256 of the job type's *validated* grade spec into the locked
# plan's plan_json at lock time, and grade() refuses to grade when the
# currently-resolved spec no longer matches the pin (fail closed).
# ---------------------------------------------------------------------------

#: Reserved top-level key inside a locked plan's ``plan_json`` carrying the
#: spec pin. A sibling of ``fields``/``job_type`` -- never inside ``fields``
#: -- so it cannot collide with plan data and needs no schema migration.
GRADE_SPEC_HASH_KEY = "grade_spec_hash"

#: Reserved top-level key carrying the *effective* extraction model at lock
#: time (``ROBIE_GEMINI_MODEL`` env override if set, else the registry pin,
#: else the light default). The grader never calls a model, so this is
#: audit evidence rather than a grading input -- but pinning it closes the
#: adjacent env-override surface: the exact model that produced the draft
#: is on the immutable record.
GRADE_EXTRACTION_MODEL_KEY = "grade_extraction_model"

#: Reason fragment emitted when grading is refused because the spec moved
#: after the plan was locked. Tests and operators can match on this string.
SPEC_CHANGED_REASON = "grade spec changed since plan lock"


def grade_spec_hash(spec: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON serialization of a grade spec.

    Canonical form is ``json.dumps(spec, sort_keys=True,
    separators=(",", ":"))`` -- the same spec built with a different key
    order hashes identically.
    """
    canonical = json.dumps(
        dict(spec), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic passing rules: rule name -> grader function.
# ---------------------------------------------------------------------------

def _rule_all_fields_matched(items: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    fields = [i for i in items if i.get("kind") == "field_match"]
    if not fields:
        return False, ["no field_match evidence collected"]
    bad = [str(i.get("field") or "?") for i in fields if not i.get("matched")]
    if bad:
        return False, [f"field(s) did not match at destination: {', '.join(bad)}"]
    return True, [f"all {len(fields)} field(s) matched at destination"]


def _rule_document_present_and_values_match(
    items: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    docs = [i for i in items if i.get("kind") == "quote_document"]
    values = [i for i in items if i.get("kind") == "quoted_value_match"]
    reasons: list[str] = []
    if not any(d.get("present") for d in docs):
        reasons.append("no quote document retrieved from the carrier site")
    bad = [str(i.get("field") or "?") for i in values if not i.get("matched")]
    if values and bad:
        reasons.append(
            f"quoted value(s) did not match the request: {', '.join(bad)}"
        )
    if not values:
        reasons.append("no quoted values checked against the request")
    if reasons:
        return False, reasons
    return True, [
        f"quote document present; all {len(values)} quoted value(s) matched"
    ]


def _rule_completed_with_disposition(
    items: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    calls = [i for i in items if i.get("kind") == "call_record"]
    done = [
        c for c in calls
        if c.get("completed") and str(c.get("disposition") or "").strip()
    ]
    if not done:
        return False, ["no completed carrier call with a captured disposition"]
    return True, [f"{len(done)} carrier call(s) completed with disposition captured"]


_PASSING_RULES = {
    "all_fields_matched": _rule_all_fields_matched,
    "document_present_and_values_match": _rule_document_present_and_values_match,
    "completed_with_disposition": _rule_completed_with_disposition,
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class GradeResult:
    """The deterministic verdict for one job's evidence.

    ``grade`` is one of:
      PASS             every required check evidenced and matched;
      NEEDS_REVIEW     the check ran and something did not match;
      UNGRADABLE       the check could not be assessed at all (no locked
                       plan, caller plan diverges from the lock, no usable
                       fetch output, a required field with no evidence, or
                       evidence without provenance). Always passed=False:
                       an unassessable check must not grade clean.
      UNKNOWN_JOB_TYPE the job type is not in the registry (fail closed).
    """

    job_type: str
    label: str
    grade: str  # PASS | NEEDS_REVIEW | UNGRADABLE | UNKNOWN_JOB_TYPE
    passed: bool
    reasons: list[str] = field(default_factory=list)
    evidence_summary: dict[str, Any] = field(default_factory=dict)


class _Registry:
    def __init__(self) -> None:
        self._specs: dict[str, dict[str, Any]] = {}
        self._runtime_names: set[str] = set()
        self.reload()

    def reload(self) -> None:
        # Runtime-registered types survive a config reload.
        runtime = {
            name: self._specs[name]
            for name in self._runtime_names
            if name in self._specs
        }
        path = os.environ.get(_ENV_OVERRIDE) or str(_REGISTRY_PATH)
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        specs = data.get("job_types") or {}
        if not isinstance(specs, dict) or not specs:
            raise ValueError(f"grade registry at {path} defines no job types")
        fresh: dict[str, dict[str, Any]] = {}
        for name, spec in specs.items():
            fresh[str(name)] = _validate_spec(str(name), spec)
        fresh.update(runtime)
        self._specs = fresh

    def _validate_name(self, name: str) -> str:
        name = str(name or "").strip()
        if not name:
            raise ValueError("job type name must be a non-empty string")
        return name

    def spec_for(self, job_type: str) -> dict[str, Any] | None:
        return self._specs.get(self._validate_name(job_type))

    def job_types(self) -> list[str]:
        return sorted(self._specs)

    def register_job_type(self, name: str, spec: Mapping[str, Any]) -> None:
        """Add a job type at runtime (dynamic as we go). Validated; no code."""
        name = self._validate_name(name)
        self._specs[name] = _validate_spec(name, spec)
        self._runtime_names.add(name)

    def extraction_model_for(self, job_type: str) -> str | None:
        spec = self.spec_for(job_type)
        if spec is None:
            return None
        model = spec.get("extraction_model")
        return str(model).strip() or None if model is not None else None

    def required_fields_for(self, job_type: str) -> str | None:
        """The required-field-set mode for a job type.

        Returns one of ``_REQUIRED_FIELD_MODES``, or None when the job
        type is not in the registry. Specs that omit ``required_fields``
        default to ``"plan"`` (see ``_validate_spec``).
        """
        spec = self.spec_for(job_type)
        if spec is None:
            return None
        return str(spec.get("required_fields") or "plan")

    def validated_spec_hash(self, job_type: str) -> str | None:
        """Hash of the job type's currently-resolved, *validated* grade spec.

        Returns None when the job type is unknown to the registry. Callers
        pinning a plan at lock time treat None as "no spec to bind to" and
        fail closed.
        """
        name = self._validate_name(job_type)
        spec = self._specs.get(name)
        if spec is None:
            return None
        return grade_spec_hash(_validate_spec(name, spec))
    def grade(
        self,
        job_type: str,
        evidence: list[Mapping[str, Any]] | None,
        expected_spec_hash: str | None = None,
    ) -> GradeResult:
        """Grade evidence against the job type's passing rule.

        ``expected_spec_hash`` is the spec pin taken at plan-lock time (see
        ``plan_lock.locked_plan_grade_spec_hash``). When provided, the
        currently-resolved spec is re-hashed and compared: on mismatch the
        grade is REFUSED -- NEEDS_REVIEW with a "spec changed" reason --
        because the spec in force now is not the spec the plan was locked
        against. ``None`` (the default) preserves the legacy behavior for
        existing callers and tests.
        """
        name = self._validate_name(job_type)
        spec = self._specs.get(name)
        items = [dict(e) for e in (evidence or [])]
        summary = _summarize(items)
        if spec is None:
            return GradeResult(
                job_type=name,
                label=name,
                grade="UNKNOWN_JOB_TYPE",
                passed=False,
                reasons=[
                    f"job type {name!r} is not in the grade registry; "
                    "refusing to grade (fail closed)"
                ],
                evidence_summary=summary,
            )
        if expected_spec_hash is not None:
            current = grade_spec_hash(_validate_spec(name, spec))
            if not hmac.compare_digest(current, str(expected_spec_hash)):
                return GradeResult(
                    job_type=name,
                    label=str(spec["label"]),
                    grade="NEEDS_REVIEW",
                    passed=False,
                    reasons=[
                        f"{SPEC_CHANGED_REASON}; refusing to grade "
                        "(fail closed)"
                    ],
                    evidence_summary=summary,
                )
        rule = _PASSING_RULES[spec["passing_rule"]]
        passed, reasons = rule(items)
        return GradeResult(
            job_type=name,
            label=str(spec["label"]),
            grade="PASS" if passed else "NEEDS_REVIEW",
            passed=passed,
            reasons=reasons,
            evidence_summary=summary,
        )


def _validate_spec(name: str, spec: Any) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ValueError(f"job type {name!r}: spec must be a mapping")
    spec = dict(spec)
    missing = [k for k in _REQUIRED_SPEC_KEYS if k not in spec]
    if missing:
        raise ValueError(
            f"job type {name!r}: missing required keys: {', '.join(missing)}"
        )
    if not str(spec.get("label") or "").strip():
        raise ValueError(f"job type {name!r}: label must be non-empty")
    kinds = spec.get("evidence_kinds")
    if not isinstance(kinds, list) or not kinds or not all(
        isinstance(k, str) and k.strip() for k in kinds
    ):
        raise ValueError(
            f"job type {name!r}: evidence_kinds must be a non-empty string list"
        )
    rule = str(spec.get("passing_rule") or "").strip()
    if rule not in _PASSING_RULES:
        raise ValueError(
            f"job type {name!r}: unknown passing_rule {rule!r}; "
            f"known: {', '.join(sorted(_PASSING_RULES))}"
        )
    model = spec.get("extraction_model")
    if model is not None and not str(model).strip():
        raise ValueError(
            f"job type {name!r}: extraction_model must be a model name or null"
        )
    mode = str(spec.get("required_fields") or "plan").strip()
    if mode not in _REQUIRED_FIELD_MODES:
        raise ValueError(
            f"job type {name!r}: unknown required_fields {mode!r}; "
            f"known: {', '.join(_REQUIRED_FIELD_MODES)}"
        )
    spec["required_fields"] = mode
    return spec


def _summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for item in items:
        kind = str(item.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"total_items": len(items), "by_kind": by_kind}


# Module-level singleton: the dynamic registry.
registry = _Registry()


# Convenience passthroughs.
def grade(
    job_type: str,
    evidence: list[Mapping[str, Any]] | None,
    expected_spec_hash: str | None = None,
) -> GradeResult:
    return registry.grade(
        job_type, evidence, expected_spec_hash=expected_spec_hash
    )


def validated_spec_hash(job_type: str) -> str | None:
    """Hash of the job type's currently-resolved, validated grade spec."""
    return registry.validated_spec_hash(job_type)


def job_types() -> list[str]:
    return registry.job_types()


def spec_for(job_type: str) -> dict[str, Any] | None:
    return registry.spec_for(job_type)


def register_job_type(name: str, spec: Mapping[str, Any]) -> None:
    registry.register_job_type(name, spec)


def extraction_model_for(job_type: str) -> str | None:
    return registry.extraction_model_for(job_type)


def required_fields_for(job_type: str) -> str | None:
    return registry.required_fields_for(job_type)


def default_extraction_model(job_type: str) -> str:
    """The model a job type's extraction should use.

    Precedence: ``ROBIE_GEMINI_MODEL`` env override, then the registry pin
    for the job type, then the light default. Callers building the
    ``llm_json_fn`` for ``extract_plan_draft`` should use this -- extraction
    is a structured task and stays on a small cheap model, never a
    high-reasoning one.

    Security note (adjacent env-override surface): the spec pin
    (``GRADE_SPEC_HASH_KEY``) covers ``extraction_model`` only as stored in
    the YAML -- it does NOT cover this env override. That is handled by
    pinning the *effective* model (this function's return value) into the
    locked plan's ``plan_json`` under ``GRADE_EXTRACTION_MODEL_KEY`` at lock
    time. The override cannot change what PASS means -- the grader is
    deterministic code and never calls a model -- but the exact model that
    produced the draft is on the immutable record for audit.
    """
    override = os.environ.get("ROBIE_GEMINI_MODEL", "").strip()
    if override:
        return override
    return extraction_model_for(job_type) or "gemini-3.1-flash-lite-preview"
