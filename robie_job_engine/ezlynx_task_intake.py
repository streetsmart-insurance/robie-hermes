#!/usr/bin/env python3
"""Intake orchestrator: Gmail report -> one durable Job Engine job per task.

Runs on a schedule (every ~30 min, offset from the Looker delivery):

1. Fetch the latest "Robie AI - Task Check-In" delivery (fail-closed
   envelope; skips already-processed message IDs).
2. Parse the CSV into tasks assigned to Robie AI.
3. ensure_task_job(): one durable Job Engine job per EZLynx task ID —
   restarts and repeat deliveries resolve to the same job row.
4. Work PENDING jobs whose task is in the current report (blast-radius
   capped): PENDING -> RUNNING -> VERIFYING.
5. Verify VERIFYING jobs through the engine's independent verifier:
   VERIFYING -> COMPLETE only on fresh EZLynx read-back evidence.
6. Record the intake run for the health check.

Jobs for tasks NOT in the current report are left untouched (the
report's 7-day note filter can hide older open tasks — never cancel
or work a task the fresh report does not show).

HITL resume: `intake.py --resume <task-id>` resumes the SAME job that
is waiting on a human (JobStore.resume), never a new job. A person's answer
rides with it: `--assign-to NAME` picks who gets the task back, and
`--note-id ID` adopts a note a human confirmed in EZLynx after an uncertain
post. A resumed job is worked on the next pass even when its report delivery
was already processed.

A worker holds a lease on its job and renews it before every external effect;
every move it makes is fenced by that lease. A job left RUNNING is returned to
PENDING only once its lease has lapsed (or, for a job with no lease at all, it
has sat RUNNING past STALE_RUNNING_MINUTES), in one atomic step. A worker that
lost its lease stops and changes nothing. Every external effect is also
reserved before it is sent and reconciled by reading the destination, so even
a late original cannot cause a repeat.

A report older than MAX_REPORT_AGE_MINUTES is refused for NEW work, whether or not
it was already processed. Finishing work that already took an effect (reconcile, note,
verify) is separate: it is owed whatever the report says, it never needs a report, and
it runs with calls disabled.

Test-only task restriction: ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS (comma-separated task
IDs) drops every other task before any discussion lookup or job is created. With
ROBIE_ENV=TEST it is REQUIRED; unset or empty refuses the whole run.

A handed-back task only starts a new round when the report arrived after the handback
and a live read shows Robie as its owner.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from .ezlynx_task_cdp import PlaywrightTaskReassigner, ReassignError, reassign_enabled
from .ezlynx_task_inbox import TaskInboxError, fetch_latest_task_report
from .ezlynx_task_jobs import (
    ACTION_TYPE,
    _find_by_idempotency_key,
    ensure_task_job,
    task_idempotency_key,
)
from .ezlynx_task_report import AssignedTask
from .ezlynx_task_intake_health import _parse_ts
from .models import WAITING_STATUSES, JobStatus
from .store import JobStore
from .task_assignment_worker import (
    LEGACY_NOTE_KIND,
    MAX_TASKS_PER_RUN,
    NOTE_KIND_PREFIX,
    REASSIGN_KIND_PREFIX,
    ROBIE_NAME,
    human_answer_kind,
    job_round,
    TaskAssignmentWorker,
    TaskIntakeVerifier,
)

logger = logging.getLogger("ezlynx_task_intake")

INTAKE_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS ezlynx_task_intake_runs (
    message_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    filename TEXT NOT NULL,
    task_count INTEGER NOT NULL,
    jobs_created INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
"""


def default_db_path() -> str:
    return os.environ.get(
        "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
    )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_intake_table(store: JobStore) -> None:
    with store.connect() as conn:
        conn.execute(INTAKE_RUNS_DDL)


def _already_processed(store: JobStore, message_id: str) -> bool:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM ezlynx_task_intake_runs WHERE message_id=? AND status='ok'",
            (message_id,),
        ).fetchone()
    return row is not None


def _record_run(
    store: JobStore,
    *,
    message_id: str,
    digest: str,
    filename: str,
    task_count: int,
    jobs_created: int,
    status: str,
    error: str = "",
) -> None:
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO ezlynx_task_intake_runs
               (message_id, digest, filename, task_count, jobs_created, status, error, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET
                 status=excluded.status, error=excluded.error,
                 task_count=excluded.task_count, jobs_created=excluded.jobs_created,
                 created_at=excluded.created_at""",
            (message_id, digest, filename, task_count, jobs_created, status, error, utcnow_iso()),
        )


def _build_discussion_client():
    """DiscussionApiClient from the standard secret path (fail-closed)."""
    from urllib.parse import urlparse

    from .ezlynx_api import load_ezlynx_api_config
    from .ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig

    api_config = load_ezlynx_api_config()
    parsed = urlparse(str(api_config.document_base_url or api_config.token_endpoint))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    config = DiscussionApiConfig(
        discussion_base_url=origin + "/DiscussionApi/",
        token_endpoint=str(api_config.token_endpoint),
        client_id=str(api_config.client_id),
        client_secret=str(api_config.client_secret),
        username=str(api_config.username),
        integration_group_id=str(api_config.integration_group_id),
        scope="DiscussionApi openid",
    )
    return DiscussionApiClient(config)


def _build_engine(store: JobStore, verifier: TaskIntakeVerifier):
    """JobEngine with our independent verifier — the only path to COMPLETE."""
    from .engine import JobEngine

    return JobEngine(store, {}, {ACTION_TYPE: verifier})


STALE_RUNNING_MINUTES = 45
MAX_REPORT_AGE_MINUTES = 90
_UNSET: Any = object()  # "read the restriction from the environment"


class _ClientUnavailable(Exception):
    """The EZLynx discussion client could not be built (fail-closed)."""


def recover_stale_running(
    store: JobStore, *, now: datetime | None = None,
    stale_minutes: int = STALE_RUNNING_MINUTES, allowed: Any = _UNSET,
) -> list[str]:
    """Return task jobs a crash left RUNNING to PENDING so they are worked again.

    Safe only when the original worker can no longer act, so a live lease is
    never touched: a job is recovered when its lease has lapsed, or (no lease
    was ever taken) when it has been RUNNING longer than `stale_minutes`. The
    check and the update are one transaction, so a worker that renews its lease
    just before cannot be recovered underneath itself, and a worker that lost
    its lease finds its next fence (or fenced transition) refused.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=stale_minutes)
    if allowed is _UNSET:
        try:
            allowed = allowed_task_ids()
        except TaskRestrictionError:
            return []  # a Test run without its restriction changes nothing
    recovered: list[str] = []
    for candidate in store.list_jobs_by_status({JobStatus.RUNNING}):
        if candidate.get("action_type") != ACTION_TYPE or not _is_allowed(candidate, allowed):
            continue
        with store.transaction() as conn:
            row = conn.execute(
                "SELECT status, lease_owner, lease_expires_at, updated_at FROM jobs WHERE id=?",
                (candidate["id"],),
            ).fetchone()
            if row is None or row["status"] != JobStatus.RUNNING.value:
                continue
            expires = _parse_ts(row["lease_expires_at"])
            if row["lease_owner"] and expires is not None:
                if expires > now:
                    continue  # the owner is alive and renewing: never touch it
            else:
                updated = _parse_ts(row["updated_at"])
                if updated is not None and updated > cutoff:
                    continue  # no lease was taken, but it is not old enough to presume dead
            conn.execute(
                """UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL,
                   last_error=?, updated_at=? WHERE id=? AND status=?""",
                (JobStatus.PENDING.value,
                 "recovered after its worker's lease lapsed (crash); effects reconcile by "
                 "reading EZLynx and are never repeated blindly",
                 now.isoformat(), candidate["id"], JobStatus.RUNNING.value),
            )
        recovered.append(candidate["id"])
    return recovered


def _build_worker_and_engine(store: JobStore, *, allow_calls: bool = True):
    try:
        discussion_client = _build_discussion_client()
    except Exception as e:  # noqa: BLE001
        raise _ClientUnavailable(str(e)) from e

    reassigner: Any = None
    if reassign_enabled():
        reassigner = PlaywrightTaskReassigner()
        logger.info("Reassignment gate is ON — PlaywrightTaskReassigner active")
    else:
        logger.info("Reassignment gate is OFF — tasks needing handoff will wait for a human")

    if allow_calls:
        from .bland_prod_wiring import build_call_dependencies
        from .call_opt_in import CallOptInStore
        from .call_opt_out import CallOptOutStore
        from .call_pickup import CallDedupeStore

        phone_lookup, bland_client, transfer_lookup, call_dry_run = build_call_dependencies()
        store_dir = os.path.dirname(store.path)
        call_kwargs: dict[str, Any] = dict(
            phone_lookup=phone_lookup, bland_client=bland_client, call_dry_run=call_dry_run,
            transfer_lookup=transfer_lookup,
            opt_out_store=CallOptOutStore(os.path.join(store_dir, "call_opt_outs.sqlite")),
            opt_in_store=CallOptInStore(os.path.join(store_dir, "call_opt_ins.sqlite")),
            call_dedupe=CallDedupeStore(os.path.join(store_dir, "call_dedupe.sqlite")),
        )
    else:
        # Recovery of work that already took an effect never places or queues a call:
        # the worker's lease checks do not cover every effect inside the call handler.
        call_kwargs = {}
    worker = TaskAssignmentWorker(
        discussion_client=discussion_client,
        task_reassigner=reassigner,
        reassign_enabled=reassign_enabled(),
        **call_kwargs,
    )
    verifier = TaskIntakeVerifier(
        discussion_client=discussion_client, task_reassigner=reassigner, store=store
    )
    return worker, _build_engine(store, verifier)


def _has_effect_intent(store: JobStore, job: dict[str, Any]) -> bool:
    """True when this round already reserved or applied an external effect.

    Such a job still owes follow-up (reconcile, note, verify) even after the
    reassignment it made removed the task from Robie's report.
    """
    round_no = job_round(job)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM checkpoints WHERE job_id=? AND (kind=? OR kind LIKE ? OR (kind=? AND ?=0)) LIMIT 1",
            (job["id"], f"{REASSIGN_KIND_PREFIX}{round_no}", f"{NOTE_KIND_PREFIX}{round_no}:%",
             LEGACY_NOTE_KIND, round_no),
        ).fetchone()
    return row is not None


ALLOWED_TASK_IDS_ENV = "ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS"


class TaskRestrictionError(Exception):
    """The Test-only task restriction is required but missing, or unusable."""


def allowed_task_ids() -> frozenset[str] | None:
    """Task IDs this intake may touch at all; None means no restriction.

    ROBIE_ENV=TEST requires the restriction: unset, empty, or no valid ID refuses the
    run, so a Test run can never ingest an unrelated task. Elsewhere it is optional,
    but once set it is exact (a set-but-empty value allows nothing).
    """
    raw = os.environ.get(ALLOWED_TASK_IDS_ENV)
    is_test = os.environ.get("ROBIE_ENV", "").strip().upper() == "TEST"
    ids = frozenset(part.strip() for part in (raw or "").split(",")
                    if re.fullmatch(r"[1-9][0-9]*", part.strip()))
    if is_test and not ids:
        raise TaskRestrictionError(
            f"ROBIE_ENV=TEST requires {ALLOWED_TASK_IDS_ENV} (comma-separated task IDs)")
    if raw is None:
        return None
    return ids


def _is_allowed(job: dict[str, Any], allowed: frozenset[str] | None) -> bool:
    return allowed is None or str((job.get("payload") or {}).get("task_id") or "") in allowed


def _owed_jobs(store: JobStore, allowed: frozenset[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(PENDING jobs that already took an effect, jobs awaiting verification)."""
    pending = [j for j in store.list_jobs_by_status({JobStatus.PENDING})
               if j.get("action_type") == ACTION_TYPE and _is_allowed(j, allowed)
               and _has_effect_intent(store, j)]
    verifying = [j for j in store.list_jobs_by_status({JobStatus.VERIFYING})
                 if j.get("action_type") == ACTION_TYPE and _is_allowed(j, allowed)]
    return pending, verifying


def _verify_awaiting(store: JobStore, engine: Any, allowed: frozenset[str] | None) -> int:
    done = 0
    for row in store.list_jobs_by_status({JobStatus.VERIFYING}):
        if row.get("action_type") != ACTION_TYPE or not _is_allowed(row, allowed):
            continue
        try:
            fresh = store.get_job(row["id"])
            engine._verify(fresh, store.get_checkpoint(fresh["id"], "action") or {})
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.error(f"verify raised for {row['id']}: {e}")
    return done


def _recover_owed(store: JobStore, allowed: Any = _UNSET) -> int:
    """Finish work that already took an effect, whatever the latest report says.

    Never needs a report, is not subject to the report age limit, and runs with
    calls disabled. Returns how many jobs it worked or verified.
    """
    if allowed is _UNSET:
        try:
            allowed = allowed_task_ids()
        except TaskRestrictionError:
            return 0
    pending, verifying = _owed_jobs(store, allowed)
    if not pending and not verifying:
        return 0
    worker, engine = _build_worker_and_engine(store, allow_calls=False)
    handled = 0
    for job in pending:
        task_id = str((job.get("payload") or {}).get("task_id") or "")
        if JobStatus(store.get_job(job["id"])["status"]) != JobStatus.PENDING:
            continue
        try:
            worker.process_job(store, store.get_job(job["id"]))
            handled += 1
        except Exception as e:  # noqa: BLE001
            logger.error(f"process_job raised for {task_id}: {e}")
    return handled + _verify_awaiting(store, engine, allowed)


def _work_new(store: JobStore, jobs: list[dict[str, Any]], in_report: set[str],
              allowed: frozenset[str] | None = None):
    """Work NEW tasks shown by a FRESH report, then verify what is awaiting.

    Only a task the fresh report shows is started; owed recovery is separate.
    """
    worker, engine = _build_worker_and_engine(store)
    for job in jobs:
        payload = job.get("payload") or {}
        task_id = str(payload.get("task_id") or "")
        if task_id not in in_report or not _is_allowed(job, allowed):
            continue
        if JobStatus(store.get_job(job["id"])["status"]) != JobStatus.PENDING:
            continue
        try:
            worker.process_job(store, store.get_job(job["id"]))
        except Exception as e:  # noqa: BLE001 — process_job already fail-closeds; belt and suspenders
            logger.error(f"process_job raised for {task_id}: {e}")
    _verify_awaiting(store, engine, allowed)
    return worker


def _resumable_jobs(store: JobStore, tasks: list[AssignedTask]) -> list[dict[str, Any]]:
    """PENDING jobs for tasks in the report that a human resumed after its delivery ran."""
    found = []
    for task in tasks:
        job = _find_by_idempotency_key(store, task_idempotency_key(task.task_id))
        if job is not None and JobStatus(job["status"]) == JobStatus.PENDING:
            found.append(job)
    return found


# Everything the handback depends on. The report row and the live task must agree on ALL of
# them, or the row is an old snapshot: a human may have changed the Producer (or CSR, creator,
# labels, instructions) since it was generated, and the handback would go to the wrong person.
CONSEQUENTIAL_FIELDS = ("description", "created_by", "assigned_producer", "csr", "activity_labels")


def _confirm_returned(task: AssignedTask) -> bool:
    """Live, read-only: is the task back with Robie, under the request the report describes?

    Robie owning the task now is not enough, and neither is a matching description: a delayed
    report can carry an OLD snapshot of the routing (creator, producer, CSR) or labels after a
    human returned the task with changes. Every consequential field must be read live and equal
    the report row's. A field the live read cannot return is unproven, never assumed to match.
    """
    try:
        state = PlaywrightTaskReassigner().read_task_state(
            task.task_id, task.applicant_id, description=task.description)
    except Exception as e:  # noqa: BLE001 — unreadable is unproven
        logger.warning(f"Live return check failed for {task.task_id}: {e}")
        return False
    if str(state.get("assignee") or "").strip().casefold() != ROBIE_NAME.casefold():
        return False

    def norm(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    for name in CONSEQUENTIAL_FIELDS:
        if name not in state or state[name] is None:
            logger.info(f"Task {task.task_id}: {name} could not be read live; return not proven")
            return False
        if norm(state[name]) != norm(getattr(task, name)):
            logger.info(f"Task {task.task_id}: the report's {name} is not the live one "
                        "(old snapshot); no new round from this report")
            return False
    if not norm(state["description"]):
        return False
    return True


def _report_age_minutes(report: Any) -> float | None:
    try:
        received = datetime.fromtimestamp(int(report.received_at) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return (datetime.now(timezone.utc) - received).total_seconds() / 60


def run_intake(*, db_path: str | None = None, dry_run: bool = False) -> int:
    """Run one intake pass. Returns 0 healthy, 2 on failure (health check alerts)."""
    try:
        allowed = allowed_task_ids()
    except TaskRestrictionError as e:
        logger.error(f"Refusing to run: {e}")
        return 2

    store = JobStore(db_path or default_db_path())
    _ensure_intake_table(store)

    if not dry_run:
        recovered = recover_stale_running(store, allowed=allowed)
        if recovered:
            logger.warning(f"Recovered {len(recovered)} stale RUNNING job(s): {recovered}")
        # Work already under way is finished first, with calls off, whatever the report says.
        try:
            _recover_owed(store, allowed)
        except _ClientUnavailable as e:
            logger.error(f"Discussion client unavailable for owed recovery: {e}")

    # 1. Gmail -> latest delivery.
    from . import report_email_source

    try:
        service = report_email_source.build_default_gmail_service()
        report = fetch_latest_task_report(service)
    except (TaskInboxError, Exception) as e:  # noqa: BLE001 — fail-closed, recorded
        logger.error(f"Inbox fetch failed: {e}")
        return 2

    if report is None:
        logger.info("No delivery yet — quiet.")
        return 0

    # Unrelated tasks stop HERE: before any lookup, job, cap count or note.
    tasks = [t for t in report.tasks if allowed is None or t.task_id in allowed]
    if len(tasks) != len(report.tasks):
        logger.info(f"Task restriction dropped {len(report.tasks) - len(tasks)} unrelated task(s)")

    age = _report_age_minutes(report)
    fresh = age is not None and age <= MAX_REPORT_AGE_MINUTES

    if _already_processed(store, report.message_id):
        # Already ran. NEW work still needs a fresh report; a stale one starts nothing.
        if not fresh:
            logger.error(f"Latest report {report.message_id} is stale "
                         f"({'unknown age' if age is None else f'{int(age)} min'}); "
                         "no new work is started.")
            return 2
        resumed = _resumable_jobs(store, tasks)
        if not resumed:
            logger.info(f"Delivery {report.message_id} already processed — quiet.")
            return 0
        if dry_run:
            logger.info(f"[DRY-RUN] {len(resumed)} resumed job(s) to work.")
            return 0
        logger.info(f"Delivery {report.message_id} already processed; working {len(resumed)} resumed job(s).")
        try:
            _work_new(store, resumed, {t.task_id for t in tasks}, allowed)
        except _ClientUnavailable as e:
            logger.error(f"Discussion client unavailable: {e}")
            return 2
        return 0

    if not fresh:
        # A stale (or undated) report describes a past state of the queue: tasks may
        # already have been handed on. Never start new work from it.
        msg = (f"Report {report.message_id} is "
               f"{'of unknown age' if age is None else f'{int(age)} min old'} "
               f"(limit {MAX_REPORT_AGE_MINUTES}); refusing to start new work from it.")
        logger.error(msg)
        if not dry_run:
            _record_run(
                store, message_id=report.message_id, digest=report.digest,
                filename=report.filename, task_count=len(tasks), jobs_created=0,
                status="stale", error=msg,
            )
        return 2

    logger.info(f"Delivery {report.message_id}: {len(tasks)} Robie AI tasks")

    # 2. Blast-radius cap.
    if len(tasks) > MAX_TASKS_PER_RUN:
        msg = (
            f"Batch cap exceeded: {len(tasks)} tasks > {MAX_TASKS_PER_RUN}. "
            "Refusing to process; alerting."
        )
        logger.error(msg)
        _record_run(
            store, message_id=report.message_id, digest=report.digest,
            filename=report.filename, task_count=len(tasks), jobs_created=0,
            status="over_cap", error=msg,
        )
        return 2

    # 3. One durable job per task ID.
    jobs_created = 0
    jobs: list[dict[str, Any]] = []
    failures: list[str] = []
    for task in tasks:
        try:
            job, created = ensure_task_job(
                store, task,
                report_message_id=report.message_id, report_digest=report.digest,
                report_received_at=report.received_at, confirm_returned=_confirm_returned,
            )
            jobs.append(job)
            if created:
                jobs_created += 1
        except Exception as e:  # noqa: BLE001 — one bad task must not kill the batch
            logger.error(f"ensure_task_job failed for {task.task_id}: {e}")
            failures.append(f"{task.task_id}: {type(e).__name__}")

    if dry_run:
        logger.info(f"[DRY-RUN] {len(jobs)} jobs ensured ({jobs_created} new); no work performed.")
        return 0

    # 4-6. Work PENDING jobs for tasks in THIS fresh report only, then verify.
    try:
        worker = _work_new(store, jobs, {t.task_id for t in tasks}, allowed)
    except _ClientUnavailable as e:
        logger.error(f"Discussion client unavailable: {e}")
        _record_run(
            store, message_id=report.message_id, digest=report.digest,
            filename=report.filename, task_count=len(tasks), jobs_created=jobs_created,
            status="failed", error=f"discussion client: {e}",
        )
        return 2

    # 7. Record the run. A task that could not get a job is NOT a healthy run.
    status = "partial" if failures else "ok"
    _record_run(
        store, message_id=report.message_id, digest=report.digest,
        filename=report.filename, task_count=len(tasks), jobs_created=jobs_created,
        status=status,
        error=("no durable job for: " + "; ".join(failures))[:500] if failures else "",
    )
    actions = {}
    for r in worker.results:
        actions[r.action] = actions.get(r.action, 0) + 1
    logger.info(f"Intake pass done: {actions}")
    return 2 if failures else 0


def _unconfirmed_note_intents(store: JobStore, job: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Unconfirmed, receipt-less note intents of THIS round only."""
    import json

    round_no = job_round(job)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT kind, data_json FROM checkpoints WHERE job_id=? AND (kind LIKE ? OR (kind=? AND ?=0))",
            (job["id"], f"{NOTE_KIND_PREFIX}{round_no}:%", LEGACY_NOTE_KIND, round_no),
        ).fetchall()
    found = []
    for kind, data_json in rows:
        data = json.loads(data_json)
        if data.get("state") != "confirmed" and not str(data.get("note_id") or ""):
            found.append((kind, data))
    return found


def resume_task(
    task_id: str, *, db_path: str | None = None,
    assign_to: str | None = None, note_id: str | None = None,
    note_purpose: str | None = None, allow_retry_save: bool = False,
) -> int:
    """Resume the SAME job waiting on a human for this task ID.

    A human's answers are recorded for the job's CURRENT round only and never
    carry into a later one:
      assign_to          who should get the task back.
      note_id            a note the human confirmed in EZLynx after an uncertain
                         post. It is bound to this applicant, discussion, round
                         and purpose and is accepted only if the discussion shows
                         that exact ID once with exactly Robie's intended text.
      allow_retry_save   permission for ONE more Save after an unknown
                         reassignment. Robie never repeats a Save on its own.
    Everything is validated before anything is written.
    """
    import json

    try:
        allowed = allowed_task_ids()
    except TaskRestrictionError as e:
        print(f"Refusing: {e}")
        return 1
    if allowed is not None and str(task_id).strip() not in allowed:
        print(f"Task {task_id} is outside this run's task restriction ({ALLOWED_TASK_IDS_ENV}); nothing changed")
        return 1
    store = JobStore(db_path or default_db_path())
    job = _find_by_idempotency_key(store, task_idempotency_key(task_id))
    if job is None:
        print(f"No job for task {task_id}")
        return 1
    status = JobStatus(job["status"])
    payload = job.get("payload") or {}
    round_no = job_round(job)
    print(f"Task {task_id}: job {job['id']} is {status.value} (round {round_no})")
    asking = assign_to is not None or note_id is not None or allow_retry_save
    if asking and status not in WAITING_STATUSES:
        print("An answer can only be recorded on a job that is waiting on a person")
        return 1

    answer = dict(store.get_checkpoint(job["id"], human_answer_kind(round_no)) or {})
    answer.update({"task_id": str(payload.get("task_id") or ""), "applicant_id": str(payload.get("applicant_id") or ""),
                   "discussion_id": str(payload.get("discussion_id") or ""), "round": round_no})
    notes: list[str] = []

    if assign_to is not None:
        name = " ".join(str(assign_to).split())
        if not name or len(name) > 80 or name.casefold() == ROBIE_NAME.casefold():
            print("--assign-to must be a person other than Robie AI (1-80 characters)")
            return 1
        answer["assign_to"] = name
        answer["recorded_at"] = utcnow_iso()
        notes.append(f"Recorded: return the task to {name}")

    adoption: tuple[str, dict[str, Any]] | None = None
    if note_id is not None:
        clean = str(note_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", clean):
            print("--note-id must be the EZLynx note ID (letters, digits, - or _)")
            return 1
        candidates = _unconfirmed_note_intents(store, job)
        if note_purpose:
            candidates = [c for c in candidates if c[1].get("purpose") == note_purpose]
        if len(candidates) != 1:
            print("Expected exactly one unconfirmed note in this round to adopt; found "
                  f"{len(candidates)}" + ("; pass --note-purpose to choose" if candidates else ""))
            return 1
        kind, data = candidates[0]
        data = {**data, "note_id": clean, "adopted": True, "adoption": {
            "note_id": clean, "applicant_id": data.get("applicant_id"),
            "discussion_id": data.get("discussion_id"), "body_sha256": data.get("body_sha256"),
            "purpose": data.get("purpose", "legacy"), "round": round_no, "at": utcnow_iso()}}
        adoption = (kind, data)
        notes.append(f"Recorded: the {data['adoption']['purpose']} note is {clean}; it is confirmed only "
                     "if EZLynx shows that exact note, once, with the exact text Robie meant to write")

    if allow_retry_save:
        intent = store.get_checkpoint(job["id"], f"{REASSIGN_KIND_PREFIX}{round_no}") or {}
        if intent.get("state") not in ("attempting", "uncertain"):
            print("There is no unknown reassignment on this job to retry")
            return 1
        grant_target = str(answer.get("assign_to") or intent.get("target") or "")
        answer["retry_save"] = {"target": grant_target, "after_attempt": int(intent.get("attempt") or 0)}
        notes.append(f"Recorded: one more Save to {grant_target} is allowed (attempt "
                     f"{int(intent.get('attempt') or 0) + 1}); it names that person and is used once")

    if asking:
        store.checkpoint(job["id"], human_answer_kind(round_no), answer)
        if adoption is not None:
            store.checkpoint(job["id"], adoption[0], adoption[1])
        for line in notes:
            print(line)
    resumed = store.resume(job["id"])
    print(f"Resumed to {resumed['status']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EZLynx task intake (Robie AI)")
    parser.add_argument("--db", default=None, help="Job Engine DB path")
    parser.add_argument("--dry-run", action="store_true", help="Ingest only; no EZLynx writes")
    parser.add_argument("--resume", metavar="TASK_ID", default=None,
                        help="Resume the waiting job for one task ID")
    parser.add_argument("--assign-to", metavar="NAME", default=None,
                        help="With --resume: the person who should get the task back")
    parser.add_argument("--note-id", metavar="ID", default=None,
                        help="With --resume: adopt this EZLynx note ID after an uncertain post")
    parser.add_argument("--note-purpose", metavar="PURPOSE", default=None,
                        help="With --note-id: which note (handoff, needs-target, gate-off, test-ack)")
    parser.add_argument("--allow-retry-save", action="store_true",
                        help="With --resume: allow ONE more reassignment Save after an unknown result")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.resume:
        return resume_task(args.resume, db_path=args.db, assign_to=args.assign_to,
                           note_id=args.note_id, note_purpose=args.note_purpose,
                           allow_retry_save=args.allow_retry_save)
    try:
        return run_intake(db_path=args.db, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 — top-level fail-closed
        logger.error(f"Intake crashed: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
