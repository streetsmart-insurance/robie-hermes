"""Bounded, read-only EZLynx Submission Center worker and verifier port."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .models import JobStatus, WorkerResult
from .secrets import redact_text


RESOURCE_ID = "ezlynx:submission-center:overview:submissions"
DEFAULT_SCOPE = {
    "time_frame": "All Submissions",
    "assigned_producer": "Streetsmart Insurance",
    "my_submissions": False,
    "page_size": 100,
    "status_sort": "ascending",
    "inspection_boundary": "first_closed_row",
}
LOGIN_HELPER = Path(
    os.environ.get(
        "ROBIE_EZLYNX_LOGIN_HELPER",
        "/opt/streetsmart-hermes/robie-job-engine/data/ezlynx_login_bootstrap.py",
    )
)
PYTHON = os.environ.get(
    "ROBIE_HERMES_PYTHON",
    "/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python",
)
RUNNER_TIMEOUT_SECONDS = 90
LOGIN_TIMEOUT_SECONDS = 180


class BoundedProcessError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = redact_text(detail)[-2_000:]


def _run_bounded(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a helper in its own process group and kill the whole group on timeout."""
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = (
        package_root
        if not existing_pythonpath
        else package_root + os.pathsep + existing_pythonpath
    )
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        raise BoundedProcessError("PLAYWRIGHT_TIMEOUT_RECOVERED") from exc
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _safe_status(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else "HELPER_FAILED"


def ensure_ezlynx_login() -> None:
    proc = _run_bounded([PYTHON, str(LOGIN_HELPER)], timeout=LOGIN_TIMEOUT_SECONDS)
    status = _safe_status(proc)
    if proc.returncode == 0 and status == "AUTHENTICATED":
        return
    auth_codes = {
        "MAILBOX_IDENTITY_MISMATCH",
        "ROBIE_MAILBOX_AUTH_REQUIRED",
        "MFA_CODE_NOT_FOUND",
        "MFA_INPUT_NOT_FOUND",
        "MFA_SUBMIT_NOT_FOUND",
        "MFA_NOT_ACCEPTED",
        "AUTH_STATE_REQUIRES_USERNAME_LOGIN",
    }
    if status in auth_codes:
        raise BoundedProcessError(status)
    raise BoundedProcessError("EZLYNX_LOGIN_HELPER_FAILED")


def _runner_command(*, fresh: bool) -> list[str]:
    return [
        PYTHON,
        "-m",
        "robie_job_engine.submission_audit_runner",
        "--fresh" if fresh else "--reuse",
    ]


def run_submission_read(*, fresh: bool) -> dict[str, Any]:
    proc = _run_bounded(_runner_command(fresh=fresh), timeout=RUNNER_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        status = _safe_status(proc)
        if status == "NEEDS_AUTH":
            raise BoundedProcessError("NEEDS_AUTH")
        raise BoundedProcessError(status or "PLAYWRIGHT_BLOCKED")
    try:
        result = json.loads((proc.stdout or "").strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise BoundedProcessError("PLAYWRIGHT_OUTPUT_INVALID") from exc
    if not isinstance(result, dict) or result.get("read_only") is not True:
        raise BoundedProcessError("PLAYWRIGHT_READ_ONLY_ASSERTION_MISSING")
    return result


class EzlynxSubmissionAuditWorker:
    """Authenticate through the allowlisted helper, then read the fixed scope."""

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        if job.get("payload", {}).get("read_only") is not True:
            return WorkerResult(
                False,
                "ezlynx.submission_audit",
                {},
                retryable=False,
                error="Submission Center audit is not marked read-only",
            )
        try:
            ensure_ezlynx_login()
        except BoundedProcessError as exc:
            return WorkerResult(
                False,
                "ezlynx.submission_audit",
                {},
                retryable=False,
                error=exc.code,
                hold_status=JobStatus.NEEDS_AUTH,
            )
        try:
            observed = run_submission_read(fresh=False)
        except BoundedProcessError as exc:
            return WorkerResult(
                False,
                "ezlynx.submission_audit",
                {},
                retryable=exc.code == "PLAYWRIGHT_TIMEOUT_RECOVERED",
                error=exc.code,
            )
        required = dict(job["payload"].get("expected_postcondition") or {})
        for key, expected in required.items():
            if observed.get(key) != expected:
                return WorkerResult(
                    False,
                    "ezlynx.submission_audit",
                    {},
                    retryable=False,
                    error=f"PLAYWRIGHT_BLOCKED: {key} did not match the read-only contract",
                )
        scope = dict(job["payload"].get("scope") or DEFAULT_SCOPE)
        return WorkerResult(
            True,
            "ezlynx.submission_audit",
            {
                "resource_id": RESOURCE_ID,
                "scope": scope,
                "expected_postcondition": observed,
            },
            detail={"engine": "playwright", "read_only": True},
        )


class SubprocessSubmissionReadback:
    """Independent fresh navigation/read-back used only by the Job verifier."""

    def fresh_authenticated_structured_read(self, scope: dict[str, Any]) -> dict[str, Any]:
        observed = run_submission_read(fresh=True)
        return {
            "resource_id": RESOURCE_ID,
            "authenticated": True,
            "scope": dict(scope),
            "postcondition": observed,
            "engine": "playwright",
            "fresh_navigation": True,
        }
