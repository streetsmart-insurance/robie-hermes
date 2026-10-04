#!/usr/bin/env python3
"""Durable Job Engine jobs for EZLynx tasks assigned to Roby.

Each EZLynx task ID maps to exactly one Job Engine job, keyed by the
idempotency key ``ezlynx-task:<task_id>``. Repeated report deliveries,
intake restarts, and box reboots all resolve to the same job row — work
is never duplicated.

The job payload carries the applicant ID, task ID, and discussion ID so
the worker always acts on the right records, and the Job Engine's own
status lifecycle drives the work:

- PENDING -> RUNNING -> VERIFYING -> COMPLETE (only after EZLynx read-back
  evidence is recorded; the store refuses COMPLETE without it)
- AWAITING_HUMAN_INPUT when the worker needs an answer; the SAME job
  resumes via ``JobStore.resume`` — never a new job.
- UNVERIFIED / FAILED stay visible for the health check.

The engine's scheduler never touches these jobs: it only claims jobs by
action type, and nothing claims ``ezlynx.task_intake``. The intake
script is the sole driver of this action type.
"""

from __future__ import annotations

import logging
from typing import Any

from .ezlynx_task_report import AssignedTask
from .models import TERMINAL_STATUSES, WAITING_STATUSES, JobStatus

logger = logging.getLogger(__name__)

ACTION_TYPE = "ezlynx.task_intake"


def _workflow_id(task: AssignedTask) -> str:
    from .call_pickup import classify_call_request

    decision = classify_call_request(task.activity_labels, task.description)
    return decision.workflow_id if decision.action == "workflow" else ""


def task_idempotency_key(task_id: str) -> str:
    """Stable idempotency key for one EZLynx task ID."""
    return f"ezlynx-task:{task_id.strip()}"


def job_payload_for_task(
    task: AssignedTask,
    *,
    report_message_id: str = "",
    report_digest: str = "",
) -> dict[str, Any]:
    """Job payload carrying every ID the worker needs.

    Applicant ID, task ID, and discussion ID travel with the job so the
    worker never has to re-derive which records to touch. Reassignment
    routing fields (created_by / assigned_producer / csr) travel too.
    """
    return {
        "applicant_id": task.applicant_id,
        "task_id": task.task_id,
        "discussion_id": task.discussion_id,
        # Destination identity for the COMPLETE guard: evidence must bind
        # to this locator.
        "locator": f"ezlynx-discussion:{task.discussion_id}",
        "account_name": task.applicant_name,
        "assigned_to": task.assigned_to,
        "title": task.title,
        "description": task.description,
        "due_date": task.due_date,
        "priority": task.priority,
        "created_date": task.created_date,
        "task_status": task.status,
        "last_modified": task.last_modified,
        "task_created_by": task.created_by,
        "assigned_producer": task.assigned_producer,
        "csr": task.csr,
        "activity_labels": task.activity_labels,
        "workflow": _workflow_id(task),
        "report_message_id": report_message_id,
        "report_digest": report_digest,
    }


def _find_by_idempotency_key(store: Any, key: str) -> dict[str, Any] | None:
    """Look up a job by its idempotency key. None when absent."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE idempotency_key=?", (key,)
        ).fetchone()
    if not row:
        return None
    return store.get_job(str(row["id"]))


def ensure_task_job(
    store: Any,
    task: AssignedTask,
    *,
    report_message_id: str = "",
    report_digest: str = "",
) -> tuple[dict[str, Any], bool]:
    """Return the durable job for this task, creating it if needed.

    Returns (job, created). Behavior on repeat deliveries:

    - New task ID -> creates a PENDING job (created=True).
    - Known task, unchanged (same last_modified) -> returns the job as-is.
    - Known task, changed in EZLynx -> refreshes the payload. A job in a
      terminal state (COMPLETE / FAILED / CANCELLED / UNVERIFIED) reopens
      to PENDING so the new version of the task gets worked. A job that
      is waiting on a human keeps waiting (payload refreshed, same job).
      A RUNNING job keeps its lease (a live worker owns it).
    """
    payload = job_payload_for_task(
        task, report_message_id=report_message_id, report_digest=report_digest
    )
    key = task_idempotency_key(task.task_id)
    existing = _find_by_idempotency_key(store, key)
    if existing is None:
        # create_job is itself idempotent on the key, so a concurrent
        # intake racing us still yields exactly one job row.
        job = store.create_job(ACTION_TYPE, payload, idempotency_key=key)
        return job, True

    job = existing
    existing_payload = job.get("payload") or {}
    if existing_payload.get("last_modified") == task.last_modified:
        # Same version of the task — nothing to do. Restarts and repeat
        # deliveries land here and duplicate no work.
        return job, False

    # The task changed in EZLynx since this job was created. Refresh the
    # payload on the SAME job — never a second job for one task ID.
    #
    # A "round" separates a genuinely new request from a retry. It advances
    # only when Robie already handed the task back and it has since returned
    # to Robie; note and reassignment intents are keyed by round, so the new
    # round gets its own note while retries inside a round never repeat one.
    status = JobStatus(job["status"])
    round_no = int(existing_payload.get("round") or 0)
    if status in TERMINAL_STATUSES and (store.get_checkpoint(job["id"], "action") or {}).get("reassigned"):
        round_no += 1
    if round_no:
        payload["round"] = round_no
    store.update_payload(job["id"], payload)
    if status in TERMINAL_STATUSES:
        job = store.transition(job["id"], JobStatus.PENDING)
        logger.info(
            f"Task {task.task_id} changed in EZLynx; reopened job {job['id']} to PENDING"
        )
    elif status in WAITING_STATUSES:
        logger.info(
            f"Task {task.task_id} changed in EZLynx; job {job['id']} still {status.value} "
            "with refreshed payload"
        )
    else:
        logger.info(
            f"Task {task.task_id} changed in EZLynx; job {job['id']} is {status.value}, "
            "payload refreshed"
        )
    return store.get_job(job["id"]), False
