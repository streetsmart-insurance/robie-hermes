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
is waiting on a human (JobStore.resume), never a new job.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

from .ezlynx_task_cdp import PlaywrightTaskReassigner, ReassignError, reassign_enabled
from .ezlynx_task_inbox import TaskInboxError, fetch_latest_task_report
from .ezlynx_task_jobs import ACTION_TYPE, ensure_task_job, task_idempotency_key
from .ezlynx_task_report import AssignedTask
from .models import JobStatus
from .store import JobStore
from .task_assignment_worker import (
    MAX_TASKS_PER_RUN,
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


def run_intake(*, db_path: str | None = None, dry_run: bool = False) -> int:
    """Run one intake pass. Returns 0 healthy, 2 on failure (health check alerts)."""
    store = JobStore(db_path or default_db_path())
    _ensure_intake_table(store)

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

    if _already_processed(store, report.message_id):
        logger.info(f"Delivery {report.message_id} already processed — quiet.")
        return 0

    tasks = list(report.tasks)
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
    for task in tasks:
        try:
            job, created = ensure_task_job(
                store, task,
                report_message_id=report.message_id, report_digest=report.digest,
            )
            jobs.append(job)
            if created:
                jobs_created += 1
        except Exception as e:  # noqa: BLE001 — one bad task must not kill the batch
            logger.error(f"ensure_task_job failed for {task.task_id}: {e}")

    if dry_run:
        logger.info(f"[DRY-RUN] {len(jobs)} jobs ensured ({jobs_created} new); no work performed.")
        return 0

    # 4. Build clients.
    try:
        discussion_client = _build_discussion_client()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Discussion client unavailable: {e}")
        _record_run(
            store, message_id=report.message_id, digest=report.digest,
            filename=report.filename, task_count=len(tasks), jobs_created=jobs_created,
            status="failed", error=f"discussion client: {e}",
        )
        return 2

    reassigner: Any = None
    if reassign_enabled():
        reassigner = PlaywrightTaskReassigner()
        logger.info("Reassignment gate is ON — PlaywrightTaskReassigner active")
    else:
        logger.info("Reassignment gate is OFF — tasks needing handoff will wait for a human")

    worker = TaskAssignmentWorker(
        discussion_client=discussion_client,
        task_reassigner=reassigner,
        reassign_enabled=reassign_enabled(),
    )
    verifier = TaskIntakeVerifier(
        discussion_client=discussion_client, task_reassigner=reassigner
    )
    engine = _build_engine(store, verifier)

    # 5. Work PENDING jobs for tasks in THIS report only.
    in_report = {t.task_id for t in tasks}
    for job in jobs:
        payload = job.get("payload") or {}
        task_id = str(payload.get("task_id") or "")
        if task_id not in in_report:
            continue
        if JobStatus(job["status"]) != JobStatus.PENDING:
            continue
        try:
            worker.process_job(store, job)
        except Exception as e:  # noqa: BLE001 — process_job already fail-closeds; belt and suspenders
            logger.error(f"process_job raised for {task_id}: {e}")

    # 6. Independently verify VERIFYING jobs (fresh EZLynx read-back).
    for job in jobs:
        fresh = store.get_job(job["id"])
        if JobStatus(fresh["status"]) != JobStatus.VERIFYING:
            continue
        try:
            action = store.get_checkpoint(fresh["id"], "action") or {}
            engine._verify(fresh, action)
        except Exception as e:  # noqa: BLE001
            logger.error(f"verify raised for {fresh['id']}: {e}")

    # 7. Record the run.
    _record_run(
        store, message_id=report.message_id, digest=report.digest,
        filename=report.filename, task_count=len(tasks), jobs_created=jobs_created,
        status="ok",
    )
    actions = {}
    for r in worker.results:
        actions[r.action] = actions.get(r.action, 0) + 1
    logger.info(f"Intake pass done: {actions}")
    return 0


def resume_task(task_id: str, *, db_path: str | None = None) -> int:
    """Resume the SAME job waiting on a human for this task ID."""
    from .ezlynx_task_jobs import _find_by_idempotency_key

    store = JobStore(db_path or default_db_path())
    job = _find_by_idempotency_key(store, task_idempotency_key(task_id))
    if job is None:
        print(f"No job for task {task_id}")
        return 1
    status = JobStatus(job["status"])
    print(f"Task {task_id}: job {job['id']} is {status.value}")
    resumed = store.resume(job["id"])
    print(f"Resumed to {resumed['status']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EZLynx task intake (Robie AI)")
    parser.add_argument("--db", default=None, help="Job Engine DB path")
    parser.add_argument("--dry-run", action="store_true", help="Ingest only; no EZLynx writes")
    parser.add_argument("--resume", metavar="TASK_ID", default=None,
                        help="Resume the waiting job for one task ID")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.resume:
        return resume_task(args.resume, db_path=args.db)
    try:
        return run_intake(db_path=args.db, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 — top-level fail-closed
        logger.error(f"Intake crashed: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
