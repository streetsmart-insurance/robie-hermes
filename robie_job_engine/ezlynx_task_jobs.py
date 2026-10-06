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

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .ezlynx_task_report import AssignedTask
from .models import TERMINAL_STATUSES, WAITING_STATUSES, JobStatus

logger = logging.getLogger(__name__)

ACTION_TYPE = "ezlynx.task_intake"


def _workflow_id(task: AssignedTask) -> str:
    from .call_pickup import classify_call_request

    decision = classify_call_request(task.activity_labels, "")
    return decision.workflow_id if decision.action == "workflow" else ""


def job_is_dialable(payload: dict[str, Any]) -> bool:
    """False for a non-live queue, a missing flag, or a pre-live timestamp.

    Jobs created before live mode have ``dialable`` false. A missing flag
    on an older row is not dialable. ``queued_at`` before
    ``live_enabled_at`` is not dialable even if the flag was copied forward.
    """
    if payload.get("dialable") is not True:
        return False
    queued = str(payload.get("queued_at") or "")
    enabled = str(payload.get("live_enabled_at") or "")
    if enabled and queued and queued < enabled:
        return False
    return True


def remember_splice_workflows(store: Any, *, enabled: bool, now: str) -> str:
    """Timestamp of the current on-period for the nine Splice labels.

    Empty while they are off. Each off-to-on transition records a new
    timestamp, so a task created while the labels were off is older than
    this moment and is baselined instead of dialed. Staying on does not
    move the timestamp.
    """
    with store.connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS splice_workflow_enablement (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            )"""
        )
        columns = {
            str(info[1])
            for info in conn.execute("PRAGMA table_info(splice_workflow_enablement)")
        }
        if "enabled" not in columns:
            conn.execute(
                "ALTER TABLE splice_workflow_enablement "
                "ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        row = conn.execute(
            "SELECT enabled_at, enabled FROM splice_workflow_enablement WHERE id=1"
        ).fetchone()
        if not enabled:
            if row is None:
                conn.execute(
                    "INSERT INTO splice_workflow_enablement "
                    "(id, enabled_at, enabled) VALUES (1, '', 0)"
                )
            elif int(row["enabled"] or 0) != 0:
                conn.execute(
                    "UPDATE splice_workflow_enablement SET enabled=0 WHERE id=1"
                )
            return ""
        if (
            row is not None
            and int(row["enabled"] or 0) == 1
            and str(row["enabled_at"] or "").strip()
        ):
            return str(row["enabled_at"])
        if row is None:
            conn.execute(
                "INSERT INTO splice_workflow_enablement "
                "(id, enabled_at, enabled) VALUES (1, ?, 1)",
                (now,),
            )
        else:
            conn.execute(
                "UPDATE splice_workflow_enablement "
                "SET enabled_at=?, enabled=1 WHERE id=1",
                (now,),
            )
    return now


def remember_live_mode(store: Any, *, live: bool, now: str) -> str:
    """Return when live mode was first enabled. Record it on the first live run."""
    with store.connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS call_live_mode (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled_at TEXT NOT NULL
            )"""
        )
        row = conn.execute(
            "SELECT enabled_at FROM call_live_mode WHERE id=1"
        ).fetchone()
        if row is not None:
            return str(row["enabled_at"] or "")
        if not live:
            return ""
        conn.execute(
            "INSERT INTO call_live_mode (id, enabled_at) VALUES (1, ?)",
            (now,),
        )
    return now


def task_idempotency_key(task_id: str) -> str:
    """Stable idempotency key for one EZLynx task ID."""
    return f"ezlynx-task:{task_id.strip()}"


def job_payload_for_task(
    task: AssignedTask,
    *,
    report_message_id: str = "",
    report_digest: str = "",
    live: bool = False,
    queued_at: str = "",
    live_enabled_at: str = "",
    splice_enabled_at: str = "",
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
        "created_at": task.created_at,
        "created_at_et": task.created_at_et,
        "workflow": _workflow_id(task),
        "splice_enabled_at": splice_enabled_at,
        "report_message_id": report_message_id,
        "report_digest": report_digest,
        "queued_at": queued_at,
        "dialable": bool(live),
        "live_enabled_at": live_enabled_at,
    }


def _fingerprint(*, title: Any, description: Any, applicant_id: Any, discussion_id: Any,
                 created_by: Any, assigned_producer: Any, csr: Any, activity_labels: Any) -> str:
    """What the request IS: its wording, target client and routing, not its timestamps."""
    parts = [" ".join(str(part or "").split()) for part in (
        title, description, applicant_id, discussion_id, created_by,
        assigned_producer, csr, activity_labels)]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def request_fingerprint(task: AssignedTask) -> str:
    return _fingerprint(
        title=task.title, description=task.description, applicant_id=task.applicant_id,
        discussion_id=task.discussion_id, created_by=task.created_by,
        assigned_producer=task.assigned_producer, csr=task.csr,
        activity_labels=task.activity_labels)


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    return _fingerprint(
        title=payload.get("title"), description=payload.get("description"),
        applicant_id=payload.get("applicant_id"), discussion_id=payload.get("discussion_id"),
        created_by=payload.get("task_created_by"), assigned_producer=payload.get("assigned_producer"),
        csr=payload.get("csr"), activity_labels=payload.get("activity_labels"))


def _as_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_newer(new: str, old: str) -> bool:
    """True only when this report row was modified AFTER the version already held.

    An equal or older Last Modified is a repeat delivery or a stale snapshot,
    never a new request.
    """
    new_time, old_time = _as_time(new), _as_time(old)
    if new_time is not None and old_time is not None:
        return new_time > old_time
    return str(new or "") > str(old or "")


def _find_by_idempotency_key(store: Any, key: str) -> dict[str, Any] | None:
    """Look up a job by its idempotency key. None when absent."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE idempotency_key=?", (key,)
        ).fetchone()
    if not row:
        return None
    return store.get_job(str(row["id"]))


def _received_time(value: Any) -> datetime | None:
    """When a report was received: Gmail's millisecond epoch, or an ISO time."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return _as_time(text)


def _return_proven(
    store: Any, job: dict[str, Any], payload: dict[str, Any], task: AssignedTask,
    report_received_at: Any, confirm_returned: Callable[[AssignedTask], bool] | None,
) -> bool:
    """Evidence that a task Robie handed back is really back with Robie.

    A newer Last Modified is not enough, and neither is a report row: a delayed
    report can still list the task under Robie. Both must hold: the report arrived
    AFTER the handback was applied, and a live read of the task shows Robie as its
    owner right now. Without a way to read it, nothing is proven.
    """
    if confirm_returned is None:
        return False
    from .task_assignment_worker import REASSIGN_KIND_PREFIX

    round_no = int(payload.get("round") or 0)
    intent = store.get_checkpoint(job["id"], f"{REASSIGN_KIND_PREFIX}{round_no}") or {}
    applied = _as_time(str(intent.get("applied_at") or ""))
    received = _received_time(report_received_at)
    if applied is None or received is None or received <= applied:
        logger.info(f"Task {task.task_id}: report does not post-date the handback; no new round")
        return False
    try:
        if confirm_returned(task):
            return True
    except Exception as exc:  # noqa: BLE001 — unreadable means unproven
        logger.warning(f"Task {task.task_id}: live return check failed: {exc}")
        return False
    logger.info(f"Task {task.task_id}: live read does not show Robie as owner; no new round")
    return False


def ensure_task_job(
    store: Any,
    task: AssignedTask,
    *,
    report_message_id: str = "",
    report_digest: str = "",
    report_received_at: Any = None,
    confirm_returned: Callable[[AssignedTask], bool] | None = None,
    reopen_terminal: bool = True,
    live: bool = False,
    queued_at: str = "",
    live_enabled_at: str = "",
    splice_enabled_at: str = "",
) -> tuple[dict[str, Any], bool]:
    """Return the durable job for this task, creating it if needed.

    Returns (job, created). Behavior on repeat deliveries:

    - New task ID -> creates a PENDING job (created=True).
    - Known task, unchanged (same last_modified) -> returns the job as-is.
    - Known task, changed in EZLynx -> refreshes the payload. A job in a
      terminal state (COMPLETE / FAILED / CANCELLED / UNVERIFIED) reopens
      to PENDING so the new version of the task gets worked, unless
      reopen_terminal is False (an already-seen task stays finished). A job
      that is waiting on a human keeps waiting (payload refreshed, same job).
      A RUNNING job keeps its lease (a live worker owns it).
    - A job queued while calls were not live stays not dialable: the
      original dialable/queued_at/live_enabled_at are kept, never upgraded.
    """
    payload = job_payload_for_task(
        task, report_message_id=report_message_id, report_digest=report_digest,
        live=live, queued_at=queued_at, live_enabled_at=live_enabled_at,
        splice_enabled_at=splice_enabled_at,
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
    if not _is_newer(task.last_modified, str(existing_payload.get("last_modified") or "")):
        # Same or OLDER version of the task: a repeat delivery or a stale
        # snapshot. Restarts and repeat deliveries duplicate no work, and an
        # old snapshot never rewinds or reopens anything.
        return job, False

    # A newer Last Modified is not by itself a new request: Robie's own note
    # or Save changes it too. Only a changed request (wording, client, routing)
    # or a task a human handed back to Robie again counts.
    prior_status = JobStatus(job["status"])
    handed_back = prior_status in TERMINAL_STATUSES and bool(
        (store.get_checkpoint(job["id"], "action") or {}).get("reassigned"))
    if handed_back and not _return_proven(
            store, job, existing_payload, task, report_received_at, confirm_returned):
        # Robie handed this task back. Without evidence it came back, the row is a
        # lagging or stale snapshot: leave the finished job exactly as it is.
        return job, False
    if _payload_fingerprint(existing_payload) == request_fingerprint(task) and not handed_back:
        store.update_payload(job["id"], {**existing_payload, "last_modified": task.last_modified})
        return store.get_job(job["id"]), False

    # The task changed in EZLynx since this job was created. Refresh the
    # payload on the SAME job — never a second job for one task ID.
    #
    # A "round" separates a genuinely new request from a retry. It advances
    # only when Robie already handed the task back and it has since returned
    # to Robie; note and reassignment intents are keyed by round, so the new
    # round gets its own note while retries inside a round never repeat one.
    #
    # A job queued while calls were not live stays not dialable: the original
    # dialable/queued_at/live_enabled_at travel forward, never upgraded.
    if "dialable" in existing_payload:
        payload["dialable"] = existing_payload.get("dialable") is True
        payload["queued_at"] = existing_payload.get("queued_at") or payload["queued_at"]
        payload["live_enabled_at"] = (
            existing_payload.get("live_enabled_at") or payload["live_enabled_at"]
        )
        payload["splice_enabled_at"] = (
            existing_payload.get("splice_enabled_at") or payload["splice_enabled_at"]
        )
    else:
        payload["dialable"] = False
    status = prior_status
    round_no = int(existing_payload.get("round") or 0)
    if handed_back:
        round_no += 1
    if round_no:
        payload["round"] = round_no
    store.update_payload(job["id"], payload)
    if status in TERMINAL_STATUSES and not reopen_terminal and not handed_back:
        # Already seen and not a proven handback return: leave the finished
        # job exactly as it is. A proven return (handed_back) always reopens.
        logger.info(
            f"Task {task.task_id} already seen; not reopening job {job['id']}"
        )
        return store.get_job(job["id"]), False
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
