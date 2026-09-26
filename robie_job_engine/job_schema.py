"""Registered schemas for bounded Job types.

Intake must match COMPLETE: a missing or unregistered schema cannot start
an external action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .job_type_gate import production_hold_reason
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
    perform_timeout_seconds: float | None = None
    perform_max_seconds: float | None = None

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
        if self.perform_timeout_seconds is not None and self.perform_timeout_seconds <= 0:
            raise ValueError("perform timeout must be positive")
        if self.perform_max_seconds is not None and self.perform_max_seconds <= 0:
            raise ValueError("perform max must be positive")
        if (
            self.perform_timeout_seconds is not None
            and self.perform_max_seconds is not None
            and self.perform_max_seconds < self.perform_timeout_seconds
        ):
            raise ValueError("perform max must be at least the perform timeout")


def _contract(
    result: str,
    verifier: str,
    *,
    attempts: int = 3,
    perform_timeout_seconds: float | None = None,
    perform_max_seconds: float | None = None,
) -> ExecutableSkillContract:
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
        perform_timeout_seconds=perform_timeout_seconds,
        perform_max_seconds=perform_max_seconds,
    )
    contract.validate()
    return contract


EXECUTABLE_SKILL_CONTRACTS: dict[str, ExecutableSkillContract] = {
    **{
        action: ExecutableSkillContract(
            expected_destination_result=(
                "a read-only accountability report artifact exists and its fresh SHA-256 "
                "and report title match the requested reporting period"
            ),
            recording_policy="EXEMPT",
            independent_verifier="AccountabilityReportVerifier",
            maximum_attempts=2,
            success_conditions=(
                "the artifact is freshly reread from the configured report directory",
                "the artifact SHA-256 matches the worker checkpoint",
                "the report contains the requested period title and no simulation marker",
            ),
            failure_conditions=(
                "the connection manifest is missing or invalid",
                "report generation does not create an artifact",
                "fresh artifact read-back or checksum verification fails",
            ),
        )
        for action in ("accountability.daily", "accountability.weekly", "accountability.monthly")
    },
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
        perform_max_seconds=3600,
    ),
    "ezlynx.overdue_submission_reports": ExecutableSkillContract(
        expected_destination_result=(
            "one individualized Gmail report exists in Robie's sent mailbox for every "
            "producer with a live, red, 31+ day overdue open submission"
        ),
        recording_policy="EXEMPT",
        independent_verifier="OverdueSubmissionReportVerifier",
        maximum_attempts=1,
        success_conditions=(
            "the live Submission Center evidence satisfies the full pagination and day-31 contract",
            "every producer resolves uniquely through the current approved active-employee roster",
            "every Gmail message id is independently reread from Robie's sent mailbox",
        ),
        failure_conditions=(
            "the live audit, roster, or producer recipient is missing or ambiguous",
            "email delivery is not explicitly authorized by the job contract",
            "any Gmail delivery receipt cannot be independently reread",
        ),
        # Agency-wide pagination often exceeds the 120s starting budget while
        # pages and rows are still advancing. The engine refreshes the idle
        # deadline on that progress; this ceiling is the hard cap, not a hang.
        perform_max_seconds=3600,
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
    "manual_renewal_verification": _contract(
        "per-policy manual renewal outcomes are recorded with carrier evidence "
        "or a pending reason, and every policy in report 4247 is accounted for",
        "ManualRenewalVerifier",
    ),
    "audit_verification": _contract(
        "per-policy audit outcomes are recorded with carrier evidence or a "
        "pending reason, and every audit in report 4246 is accounted for",
        "AuditVerificationVerifier",
    ),
    "mortgagee_verification": _contract(
        "per-policy mortgagee outcomes are recorded with lender-delivery "
        "evidence or a pending reason, and every item in report 4372 is "
        "accounted for",
        "MortgageeVerificationVerifier",
    ),
    "policy_change_verification": _contract(
        "per-request policy-change outcomes are recorded with carrier evidence "
        "or a pending reason, keyed by policy number and change-request created "
        "date (report 4359 / look 4602), and every request in report 4359 is "
        "accounted for; no request is ever closed by the worker",
        "PolicyChangeVerifier",
    ),
    "daily_verification_digest": ExecutableSkillContract(
        expected_destination_result=(
            "the daily verification digest artifact exists and the digest email "
            "to carlo@streetsmart.insurance is sent with per-policy "
            "queue/status/reason/owner/next action/evidence across all five "
            "departments"
        ),
        recording_policy="EXEMPT",
        independent_verifier="VerificationDigestVerifier",
        maximum_attempts=2,
        success_conditions=(
            "the digest artifact is freshly reread from the configured output directory",
            "the artifact SHA-256 matches the worker checkpoint",
            "every Gmail delivery receipt is verified with a fresh read of the sent message",
        ),
        failure_conditions=(
            "output_dir or email_sender is missing from the job payload",
            "fresh artifact read-back or checksum verification fails",
            "delivery receipt verification fails",
        ),
    ),
    "meeting.synthesis.weekly": ExecutableSkillContract(
        expected_destination_result=(
            "the weekly synthesis email is sent from robie@streetsmart.insurance "
            "to Carlo and the social-post drafts are appended to the "
            "'StreetSmart social drafts' Google Doc"
        ),
        recording_policy="EXEMPT",
        independent_verifier="MeetingSynthesisVerifier",
        maximum_attempts=2,
        success_conditions=(
            "the synthesis email exists in Gmail with From robie@streetsmart.insurance",
            "the social drafts doc exists and is readable in Drive",
            "no social post is published automatically",
        ),
        failure_conditions=(
            "the Drive notes listing or Gemini synthesis fails",
            "the Gmail send to Carlo fails",
            "the social drafts doc cannot be created or appended",
        ),
    ),
    "staff.fun.monthly": ExecutableSkillContract(
        expected_destination_result=(
            "the monthly staff-fun announcement is posted to the general "
            "Google Chat space and Carlo receives the gift-card reminder email"
        ),
        recording_policy="EXEMPT",
        independent_verifier="StaffFunVerifier",
        maximum_attempts=2,
        success_conditions=(
            "the chat webhook post returns 2xx",
            "the gift-card reminder email exists in Gmail with From robie@streetsmart.insurance",
            "gift cards stay manual: nothing is purchased automatically",
        ),
        failure_conditions=(
            "the chat webhook post fails",
            "the Gmail send to Carlo fails",
            "the Gemini content generation fails",
        ),
    ),
}


BOUNDED_JOB_SCHEMAS: dict[str, dict[str, Any]] = {
    **{
        action: {
            "schema_verified": True,
            "required": ("manifest_path",),
            "identity": ("manifest_path",),
        }
        for action in ("accountability.daily", "accountability.weekly", "accountability.monthly")
    },
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
    "ezlynx.overdue_submission_reports": {
        "schema_verified": True,
        "required": ("resource_id", "manifest_path", "authorized_actions"),
        "identity": ("resource_id", "manifest_path"),
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
    "meeting.synthesis.weekly": {
        "schema_verified": True,
        "required": ("worker",),
        "identity": ("worker",),
    },
    "staff.fun.monthly": {
        "schema_verified": True,
        "required": ("worker",),
        "identity": ("worker",),
    },
    "manual_renewal_verification": {
        "schema_verified": True,
        "required": ("report_id",),
        "identity": ("report_id",),
    },
    "audit_verification": {
        "schema_verified": True,
        "required": ("report_id",),
        "identity": ("report_id",),
    },
    "mortgagee_verification": {
        "schema_verified": True,
        "required": ("report_id",),
        "identity": ("report_id",),
    },
    "policy_change_verification": {
        # Look 4602's 19 columns are mapped. schema_verified stays False
        # until 3 clean hermes-test-01 post-job audits after Test install.
        # Do not flip this in the same change as POLICY_CHANGE_ENABLED.
        # Job payload still requires report_id. Work-item identity matches
        # report_registry (no request_id column exists on the export).
        "schema_verified": False,
        "required": ("report_id",),
        "identity": ("policy_number", "change_request_created_date"),
    },
    "daily_verification_digest": {
        "schema_verified": True,
        "required": ("output_dir", "email_sender"),
        "identity": ("output_dir",),
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
    production_hold = production_hold_reason(action_type)
    if production_hold:
        return production_hold
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
