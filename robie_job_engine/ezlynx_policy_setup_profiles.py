"""Fail-closed business contract for EZLynx Policy Setup v0.2.0-test.

This module is deliberately side-effect free.  It validates an intake,
creates a stable duplicate identity, and compares a reopened policy with the
expected contract. Only the exact synthetic Homeowners Test drill may reach a
consequential Save; every other profile remains read-only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ezlynx_write_scope import applicant_is_write_allowed, normalize_applicant_id


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_PATH = (
    REPO_ROOT / "skills" / "ezlynx-policy-setup" / "references" / "profiles.json"
)
READY = "READY"
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
NEEDS_SKILL = "NEEDS_SKILL"
REQUIRED_IDENTITY_FIELDS = (
    "applicant_id",
    "carrier",
    "policy_number",
    "effective_date",
    "expiration_date",
    "lob",
)
REQUIRED_CHECKPOINTS = (
    "duplicate_check",
    "pre_save_snapshot",
    "post_save_readback",
    "reopen_verification",
)
PROFILE_VERSION = "0.2.0-test"
HOMEOWNERS_TEST_APPLICANT_ID = "220250093"
HOMEOWNERS_TEST_POLICY_PREFIX = "TEST-HO-"
SENSITIVE_KEY_TOKENS = frozenset(
    {
        "address",
        "applicantid",
        "dateofbirth",
        "dob",
        "driverlicense",
        "driverlicensenumber",
        "email",
        "emailaddress",
        "firstname",
        "fullname",
        "lastname",
        "license",
        "licensenumber",
        "mailingaddress",
        "name",
        "namedinsured",
        "phone",
        "phonenumber",
        "policynumber",
        "propertylocation",
        "ssn",
        "socialsecuritynumber",
        "vin",
    }
)


@dataclass(frozen=True)
class PreflightResult:
    status: str
    profile_id: str | None
    reasons: tuple[str, ...]
    duplicate_key: str | None
    consequential_writes_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "profile_id": self.profile_id,
            "reasons": list(self.reasons),
            "duplicate_key": self.duplicate_key,
            "consequential_writes_enabled": self.consequential_writes_enabled,
        }


def load_profiles(path: str | Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != PROFILE_VERSION:
        raise ValueError("unexpected EZLynx Policy Setup profile version")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != 12:
        raise ValueError("EZLynx Policy Setup requires exactly 12 draft profiles")
    enabled: list[str] = []
    for profile in profiles:
        if profile.get("state") != "Testing":
            raise ValueError(f"profile {profile.get('id')} is not in Testing")
        if profile.get("consequential_writes_enabled") is True:
            enabled.append(str(profile.get("id") or ""))
        elif profile.get("consequential_writes_enabled") is not False:
            raise ValueError(f"profile {profile.get('id')} has invalid write state")
    if enabled != ["homeowners"]:
        raise ValueError("only the Homeowners Test profile may enable writes")
    constraints = next(
        item.get("test_write_constraints") or {}
        for item in profiles
        if item.get("id") == "homeowners"
    )
    if constraints != {
        "environment": "TEST",
        "applicant_id": HOMEOWNERS_TEST_APPLICANT_ID,
        "policy_number_prefix": HOMEOWNERS_TEST_POLICY_PREFIX,
        "premium": "1.00",
        "synthetic_fixture_required": True,
        "explicit_save_authorization_required": True,
    }:
        raise ValueError("Homeowners Test write constraints are not exact")
    return data


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalized(value)).strip("_")


def _sensitive_key_token(value: Any) -> str:
    """Normalize only field names, including case and common separators."""

    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def resolve_profile(lob: Any, catalog: Mapping[str, Any]) -> dict[str, Any] | None:
    wanted = _slug(lob)
    for profile in catalog.get("profiles", []):
        names = (profile.get("id"), profile.get("name"), *(profile.get("aliases") or []))
        if wanted in {_slug(name) for name in names}:
            return dict(profile)
    return None


def policy_identity(request: Mapping[str, Any]) -> dict[str, str]:
    return {field: _normalized(request.get(field)) for field in REQUIRED_IDENTITY_FIELDS}


def duplicate_key(request: Mapping[str, Any]) -> str:
    identity = policy_identity(request)
    missing = [field for field, value in identity.items() if not value]
    if missing:
        raise ValueError("duplicate identity missing: " + ", ".join(missing))
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "ezlynx-policy:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _guard_reasons(profile_id: str, request: Mapping[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if profile_id == "dwelling_fire" and not request.get("mailing_location_rule_confirmed"):
        reasons.append("confirm mailing-versus-location handling; no absolute rule is approved")
    if profile_id == "homeowners" and _normalized(request.get("replacement_cost_basis")) in {
        "full value",
        "full value replacement cost",
    }:
        reasons.append("Homeowners Full Value treatment is unresolved")
    if profile_id == "general_liability":
        if not request.get("aggregate_basis"):
            reasons.append("General Liability aggregate basis is required when the policy is silent")
        exposures = request.get("exposures") or []
        if any("if any" in _normalized(item) for item in exposures):
            reasons.append("General Liability 'if any' exposure requires clarification")
    if profile_id == "garage_dealers":
        if not request.get("operation_type") or not request.get("coverage_basis"):
            reasons.append("Garage & Dealers operation and coverage-basis mappings must be explicit")
    if profile_id == "businessowners_policy" and not request.get("placement_confirmed"):
        reasons.append("confirm BOP policy-level versus location-level field placement")
    if profile_id == "commercial_package":
        parts = " ".join(_normalized(item) for item in (request.get("package_parts") or []))
        if ("general liability" in parts or re.search(r"(^|\s)gl($|\s)", parts)) and not request.get(
            "aggregate_basis"
        ):
            reasons.append("Commercial Package GL aggregate basis must be explicit")
    if profile_id == "commercial_umbrella" and not request.get("retained_limit"):
        reasons.append("Commercial Umbrella retained limit is required")
    return tuple(reasons)


def _homeowners_test_write_reasons(request: Mapping[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if _normalized(request.get("environment")) != "test":
        reasons.append("Homeowners consequential writes require ROBIE_ENV=TEST")
    if normalize_applicant_id(request.get("applicant_id")) != HOMEOWNERS_TEST_APPLICANT_ID:
        reasons.append("Homeowners consequential writes require ROBIE Test LLC 220250093")
    policy_number = str(request.get("policy_number") or "").strip().upper()
    if not policy_number.startswith(HOMEOWNERS_TEST_POLICY_PREFIX):
        reasons.append("Homeowners Test policy number must start with TEST-HO-")
    premium = re.sub(r"[^0-9.]", "", str(request.get("premium") or "").strip())
    try:
        premium_is_one = float(premium) == 1.0
    except (TypeError, ValueError):
        premium_is_one = False
    if not premium_is_one:
        reasons.append("Homeowners Test premium must be exactly $1.00")
    if request.get("synthetic_fixture") is not True:
        reasons.append("Homeowners Test writes require an explicitly synthetic fixture")
    if request.get("save_authorized") is not True:
        reasons.append("Homeowners Test Save requires explicit user authorization")
    return tuple(reasons)


def preflight_policy_setup(
    request: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
) -> PreflightResult:
    catalog = dict(catalog or load_profiles())
    profile = resolve_profile(request.get("lob"), catalog)
    if profile is None:
        return PreflightResult(NEEDS_SKILL, None, ("unsupported line of business",), None)

    profile_id = str(profile["id"])
    if profile_id == "commercial_auto_contractors" and "truck" in _normalized(
        request.get("business_use") or request.get("operation_type")
    ):
        return PreflightResult(
            NEEDS_SKILL,
            profile_id,
            ("Commercial Auto — Contractors must not be reused for trucking",),
            None,
        )
    if profile_id == "bonds":
        supported = {_normalized(item) for item in profile.get("supported_variants", [])}
        bond_type = _normalized(request.get("bond_type"))
        if not bond_type or bond_type not in supported:
            return PreflightResult(
                NEEDS_SKILL,
                profile_id,
                ("unsupported bond type; bid and performance bonds are disabled",),
                None,
            )

    required = list(REQUIRED_IDENTITY_FIELDS) + list(profile.get("required_fields") or [])
    missing = sorted({field for field in required if not request.get(field)})
    reasons = [f"missing required field: {field}" for field in missing]
    applicant_id = normalize_applicant_id(request.get("applicant_id"))
    if not applicant_is_write_allowed(applicant_id):
        reasons.append(
            f"applicant {applicant_id or '<missing>'} is not on the compiled "
            "EZLynx business-write allowlist"
        )
    reasons.extend(_guard_reasons(profile_id, request))
    profile_writes = profile.get("consequential_writes_enabled") is True
    if profile_writes:
        reasons.extend(_homeowners_test_write_reasons(request))
    if request.get("source_conflicts"):
        reasons.append("authoritative source documents conflict")
    if reasons:
        return PreflightResult(NEEDS_CLARIFICATION, profile_id, tuple(reasons), None)

    return PreflightResult(
        READY,
        profile_id,
        (),
        duplicate_key(request),
        consequential_writes_enabled=profile_writes,
    )


def expected_contract(request: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    fields = list(REQUIRED_IDENTITY_FIELDS) + list(profile.get("verification_fields") or [])
    return {field: request.get(field) for field in fields if field in request}


def verify_reopened_policy(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    for field in sorted(expected):
        expected_value = expected[field]
        actual_value = actual.get(field)
        if expected_value != actual_value:
            mismatches.append(
                {"field": field, "expected": expected_value, "actual": actual_value}
            )
    missing = sorted(field for field in expected if field not in actual)
    return {
        "verdict": "PASS" if not mismatches and not missing else "FAIL",
        "independently_verified": not mismatches and not missing,
        "mismatches": mismatches,
        "missing_fields": missing,
        "authorizes_complete": False,
    }


def redact_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _sensitive_key_token(key) in SENSITIVE_KEY_TOKENS
                else redact_evidence(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_evidence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_evidence(item) for item in value)
    return value
