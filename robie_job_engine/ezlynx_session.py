"""Bounded EZLynx/Gmail session refresh worker and independent verifier."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .submission_audit import (
    LOGIN_HELPER,
    LOGIN_TIMEOUT_SECONDS,
    PYTHON,
    BoundedProcessError,
    _run_bounded,
    _safe_status,
)
from .ezlynx_session_lock import EzlynxSessionLockTimeout, exclusive_session


RESOURCE_ID = "ezlynx:authenticated-browser-session"
PROFILE_ID = "robie-ezlynx-canonical-profile"
AUTH_CODES = {
    "NEEDS_AUTH",
    "MAILBOX_IDENTITY_MISMATCH",
    "ROBIE_MAILBOX_AUTH_REQUIRED",
    "MFA_CODE_NOT_FOUND",
    "MFA_INPUT_NOT_FOUND",
    "MFA_SUBMIT_NOT_FOUND",
    "MFA_NOT_ACCEPTED",
    "AUTH_STATE_REQUIRES_USERNAME_LOGIN",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_login_helper(*, verify_only: bool) -> str:
    command = [PYTHON, str(LOGIN_HELPER)]
    if verify_only:
        command.append("--verify-only")
    proc = _run_bounded(command, timeout=LOGIN_TIMEOUT_SECONDS)
    status = _safe_status(proc)
    if proc.returncode == 0 and status == "AUTHENTICATED":
        return status
    if status in AUTH_CODES:
        raise BoundedProcessError(status)
    raise BoundedProcessError("EZLYNX_LOGIN_HELPER_FAILED")


class EzlynxSessionRefreshWorker:
    def perform(self, job: dict, *, idempotency_key: str) -> WorkerResult:
        del idempotency_key
        try:
            with exclusive_session():
                run_login_helper(verify_only=False)
        except (BoundedProcessError, EzlynxSessionLockTimeout) as exc:
            code = getattr(exc, "code", "EZLYNX_SESSION_LOCK_TIMEOUT")
            return WorkerResult(
                False,
                "ezlynx.session_refresh",
                {},
                retryable=code == "EZLYNX_SESSION_LOCK_TIMEOUT",
                error=code,
                hold_status=JobStatus.NEEDS_AUTH if code in AUTH_CODES else None,
            )
        return WorkerResult(
            True,
            "ezlynx.session_refresh",
            {
                "resource_id": RESOURCE_ID,
                "profile_id": PROFILE_ID,
                "authenticated": True,
                "gmail_identity_verified": True,
            },
            detail={"engine": "playwright-cdp", "sensitive_recording_exempt": True},
        )


class EzlynxSessionVerifier:
    def verify(self, job: dict, action: dict) -> VerificationResult:
        del action
        expected = {
            "resource_id": str(job["payload"].get("resource_id") or RESOURCE_ID),
            "profile_id": str(job["payload"].get("profile_id") or PROFILE_ID),
            "authenticated": True,
            "gmail_identity_verified": True,
        }
        try:
            with exclusive_session():
                run_login_helper(verify_only=True)
            observed = dict(expected)
            error = None
            verified = True
            hold_status = None
        except (BoundedProcessError, EzlynxSessionLockTimeout) as exc:
            code = getattr(exc, "code", "EZLYNX_SESSION_LOCK_TIMEOUT")
            observed = {
                "resource_id": expected["resource_id"],
                "profile_id": expected["profile_id"],
                "authenticated": False,
                "gmail_identity_verified": code != "MAILBOX_IDENTITY_MISMATCH",
                "blocker": code,
            }
            error = code
            verified = False
            hold_status = JobStatus.NEEDS_AUTH if code in AUTH_CODES else None
        evidence = VerificationEvidence(
            method="EZLYNX_SESSION_FRESH_READBACK",
            source="canonical-playwright-profile-and-robie-gmail-api",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=_now(),
            locator=expected["resource_id"],
        )
        return VerificationResult(
            verified,
            evidence,
            retryable=error == "EZLYNX_SESSION_LOCK_TIMEOUT",
            error=error,
            hold_status=hold_status,
        )
