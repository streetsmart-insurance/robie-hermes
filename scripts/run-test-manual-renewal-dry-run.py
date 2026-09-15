#!/usr/bin/env python3
"""Create and inspect one strictly read-only Manual Renewals job on Hermes Test."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

TEST_ROOT = Path("/opt/streetsmart-hermes-test/robie-job-engine")
TEST_DB = TEST_ROOT / "data/jobs.db"
sys.path.insert(0, str(TEST_ROOT))

from robie_job_engine.store import JobStore  # noqa: E402


EXTERNAL_EFFECT_KEYS = frozenset(
    {
        "email_message_id",
        "ezlynx_note_id",
        "note_id",
        "task_id",
    }
)
EXPECTED_HOST = "hermes-test-01"
EXPECTED_JOB_TYPE = "manual_renewal_verification"
EXPECTED_AUTHORIZED_ACTIONS = {"read_report_rows", "check_cancellation"}


def find_external_effects(value: object, path: str = "result") -> list[str]:
    """Return evidence paths that prove an outward action occurred."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in EXTERNAL_EFFECT_KEYS and child:
                found.append(child_path)
            elif key in {"posted", "created", "call_placed"} and child is True:
                found.append(child_path)
            elif key == "actions_taken" and isinstance(child, list):
                for index, action in enumerate(child):
                    action_text = str(action)
                    if action_text.endswith("_email_sent") or action_text == "carrier_voice_call_placed":
                        found.append(f"{child_path}[{index}]={action_text}")
            found.extend(find_external_effects(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_external_effects(child, f"{path}[{index}]"))
    return found


def build_summary(job_id: str, job: dict[str, object]) -> dict[str, object]:
    """Build fail-closed evidence from the stored job and current host."""
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    result = job.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    detail = result.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}

    host = socket.gethostname().split(".", 1)[0]
    job_type = job.get("action_type")
    status = job.get("status", "missing")
    dry_run = payload.get("dry_run") is True and detail.get("dry_run") is True
    authorized_actions = payload.get("authorized_actions")
    authorized_set = (
        {str(action) for action in authorized_actions}
        if isinstance(authorized_actions, list)
        else set()
    )
    external_effects = find_external_effects(detail)

    violations: list[str] = []
    if host != EXPECTED_HOST:
        violations.append(f"unexpected host: {host}")
    if job_type != EXPECTED_JOB_TYPE:
        violations.append(f"unexpected job type: {job_type}")
    if not dry_run:
        violations.append("stored payload/result do not both prove dry_run=true")
    if authorized_set != EXPECTED_AUTHORIZED_ACTIONS:
        violations.append(f"unexpected authorized_actions: {sorted(authorized_set)}")
    if external_effects:
        violations.append("stored result contains external-effect evidence")

    execution_violations: list[str] = []
    if status != "completed":
        execution_violations.append(f"job did not complete: {status}")
    if not detail:
        execution_violations.append("job result detail is missing")
    if not isinstance(detail.get("rows_fetched"), int):
        execution_violations.append("rows_fetched evidence is missing")

    return {
        "expected_environment": "Test",
        "host": host,
        "job_id": job_id,
        "job_type": job_type,
        "status": status,
        "dry_run": dry_run,
        "authorized_actions": sorted(authorized_set),
        "rows_fetched": detail.get("rows_fetched"),
        "rows_in_window": detail.get("rows_in_window"),
        "rows_filtered": detail.get("rows_filtered"),
        "counts": detail.get("counts"),
        "error": job.get("error"),
        "external_effects": external_effects,
        "safety_violations": violations,
        "execution_violations": execution_violations,
        "read_only_verified": not violations,
        "rehearsal_verified": not violations and not execution_violations,
    }


def create(run_id: str) -> None:
    store = JobStore(str(TEST_DB))
    job = store.create_job(
        action_type="manual_renewal_verification",
        payload={
            "worker": "manual-renewal",
            "report_id": "4247",
            "window_min_days": 30,
            "window_max_days": 45,
            "dry_run": True,
            "voice_enabled": False,
            "authorized_actions": ["read_report_rows", "check_cancellation"],
            "test_rehearsal": True,
        },
        idempotency_key=f"rh-023-test-readonly-{run_id}",
    )
    print(job["id"])


def status(job_id: str) -> None:
    job = JobStore(str(TEST_DB)).get_job(job_id)
    print((job or {}).get("status", "missing"))


def summary(job_id: str) -> None:
    job = JobStore(str(TEST_DB)).get_job(job_id) or {}
    safe = build_summary(job_id, job)
    print(json.dumps(safe, indent=2, sort_keys=True))
    if not safe["rehearsal_verified"]:
        raise SystemExit("read-only rehearsal did not produce complete, safe evidence")


if __name__ == "__main__":
    command, value = sys.argv[1:3]
    {"create": create, "status": status, "summary": summary}[command](value)
