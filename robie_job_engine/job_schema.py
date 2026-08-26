"""Registered schemas for bounded Job types.

Intake must match COMPLETE: a missing or unregistered schema cannot start
an external action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .request_routing import BOUNDED_ENGINE_ACTIONS


@dataclass(frozen=True)
class ExecutableSkillContract:
    """Server-owned controls required before an executable Skill may run."""

    expected_destination_result: str
    recording_policy: str
    independent_verifier: str
    maximum_attempts: int
    success_conditions: tuple[str, ...]
    failure_conditions: tuple[str, ...]

    def validate(self) -> None:
        if not self.expected_destination_result.strip():
            raise ValueError("expected destination result is required")
        if self.recording_policy not in {"REQUIRED", "EXEMPT"}:
            raise ValueError("recording policy must be REQUIRED or EXEMPT")
        if not self.independent_verifier.strip():
            raise ValueError("independent verifier is required")
        if self.maximum_attempts < 1:
            raise ValueError("maximum attempts must be at least one")
        if not self.success_conditions:
            raise ValueError("success conditions are required")
        if not self.failure_conditions:
            raise ValueError("failure conditions are required")


def _contract(result: str, verifier: str, *, attempts: int = 3) -> ExecutableSkillContract:
    contract = ExecutableSkillContract(
        expected_destination_result=result,
        recording_policy="REQUIRED",
        independent_verifier=verifier,
        maximum_attempts=attempts,
        success_conditions=(
            "fresh authoritative destination read matches the expected result",
            "verification evidence is persisted",
            "every recording segment is uploaded and linked",
        ),
        failure_conditions=(
            "recorder cannot start before work",
            "worker or verifier exhausts the attempt limit",
            "recording upload fails",
        ),
    )
    contract.validate()
    return contract


EXECUTABLE_SKILL_CONTRACTS: dict[str, ExecutableSkillContract] = {
    "drive.skill_sync": ExecutableSkillContract(
        expected_destination_result=(
            "an immutable local snapshot contains only approved Core Rules and Active Skills"
        ),
        recording_policy="EXEMPT",
        independent_verifier="DriveSkillSyncVerifier",
        maximum_attempts=3,
        success_conditions=(
            "the current snapshot pointer is reread from durable storage",
            "every ingested file matches its recorded SHA-256",
            "the include and exclusion folder sets match the server allowlist",
        ),
        failure_conditions=(
            "the Drive folder path is missing or ambiguous",
            "a source document cannot be downloaded or decoded",
            "the immutable destination snapshot fails exact hash reread",
        ),
    ),
    "carrier.proposal": _contract(
        "the destination contains the generated proposal with the requested content",
        "CarrierProposalVerifier",
    ),
    "browser.read": _contract(
        "the fresh server-backed page state contains the requested fields",
        "BrowserReadVerifier",
    ),
    "ezlynx.reassign": _contract(
        "the exact EZLynx resource is assigned to the requested user",
        "EzlynxDestinationVerifier",
    ),
    "ezlynx.move_document": _contract(
        "the exact document exists at the requested EZLynx destination",
        "EzlynxDestinationVerifier",
    ),
    "ezlynx.apply_label": _contract(
        "the requested label exists on the exact EZLynx resource",
        "EzlynxDestinationVerifier",
    ),
    "ezlynx.submission_audit": _contract(
        "a fresh authenticated Submission Center read matches the requested scope and postcondition",
        "EzlynxSubmissionAuditVerifier",
    ),
    "ezlynx.session_refresh": ExecutableSkillContract(
        expected_destination_result=(
            "Robie's Gmail API identity is verified and the canonical EZLynx "
            "browser profile has a fresh authenticated application session"
        ),
        recording_policy="EXEMPT",
        independent_verifier="EzlynxSessionVerifier",
        maximum_attempts=2,
        success_conditions=(
            "Robie's Gmail API mailbox identity is independently verified",
            "a fresh EZLynx application read contains authenticated internal navigation",
            "no EZLynx login or MFA controls are present",
        ),
        failure_conditions=(
            "Secret Manager or Gmail OAuth access is unavailable",
            "MFA cannot be completed from Robie's mailbox",
            "the canonical browser profile cannot be locked or verified",
        ),
    ),
    "filesystem.skill_update": _contract(
        "the allowlisted SKILL.md path contains the exact requested bytes and hash",
        "FilesystemSkillUpdateVerifier",
    ),
}


BOUNDED_JOB_SCHEMAS: dict[str, dict[str, Any]] = {
    "drive.skill_sync": {
        "schema_verified": True,
        "required": ("destination_root",),
        "identity": ("destination_root",),
    },
    "carrier.proposal": {
        "schema_verified": True,
        "required": (),
        "identity": ("proposal_id",),
    },
    "browser.read": {
        "schema_verified": True,
        "required": (),
        "identity": ("locator", "url"),
    },
    "ezlynx.reassign": {
        "schema_verified": True,
        "required": (),
        "identity": ("resource_id",),
    },
    "ezlynx.move_document": {
        "schema_verified": True,
        "required": (),
        "identity": ("resource_id",),
    },
    "ezlynx.apply_label": {
        "schema_verified": True,
        "required": (),
        "identity": ("resource_id",),
    },
    "ezlynx.submission_audit": {
        "schema_verified": True,
        "required": ("resource_id", "expected_postcondition"),
        "identity": ("resource_id",),
    },
    "ezlynx.session_refresh": {
        "schema_verified": True,
        "required": ("resource_id", "profile_id"),
        "identity": ("resource_id",),
    },
    "filesystem.skill_update": {
        "schema_verified": True,
        "required": ("target_path", "expected_content", "expected_sha256"),
        "identity": ("target_path",),
    },
}


def get_executable_skill_contract(action_type: str) -> ExecutableSkillContract | None:
    return EXECUTABLE_SKILL_CONTRACTS.get(action_type)


def get_bounded_job_schema(action_type: str) -> dict[str, Any] | None:
    return BOUNDED_JOB_SCHEMAS.get(action_type)


def bounded_schema_hold_reason(
    action_type: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Return a hold reason if a bounded Job must not start an action."""
    if action_type not in BOUNDED_ENGINE_ACTIONS:
        return None
    payload = dict(payload or {})
    if payload.get("missing_schema"):
        return "missing_schema"
    spec = get_bounded_job_schema(action_type)
    if spec is None or not spec.get("schema_verified"):
        return f"unregistered or unverified schema for {action_type}"
    contract = get_executable_skill_contract(action_type)
    if contract is None:
        return f"missing executable Skill contract for {action_type}"
    try:
        contract.validate()
    except ValueError as exc:
        return f"invalid executable Skill contract for {action_type}: {exc}"
    for field in spec.get("required") or ():
        if not payload.get(field):
            return f"missing required schema field: {field}"
    return None
