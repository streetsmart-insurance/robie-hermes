#!/usr/bin/env python3
"""Canary for the EZLynx task intake: proves the pipeline end to end.

Read-only mode (default): checks that the intake ran recently, the
latest report parsed, and the canary task has a durable job. Proves
Gmail -> parse -> job without touching EZLynx.

--write mode: additionally posts a clearly-labeled canary note to the
canary task's discussion and confirms it via read-back — proving the
EZLynx write path too. The canary task is Jake's designated test task;
nothing else is touched.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from .ezlynx_task_intake import _build_discussion_client, default_db_path
from .ezlynx_task_intake_health import check_intake
from .ezlynx_task_jobs import _find_by_idempotency_key, task_idempotency_key
from .store import JobStore

logger = logging.getLogger("ezlynx_task_canary")

CANARY_TASK_ID = "63429523"
CANARY_NOTE = "Roby canary check — pipeline test, please ignore."


def run_canary(*, write: bool = False, task_id: str = CANARY_TASK_ID) -> int:
    problems: list[str] = []

    # 1. Intake health (read-only).
    try:
        health_problems = check_intake()
    except Exception as e:  # noqa: BLE001
        health_problems = [f"health check crashed: {e}"]
    problems.extend(health_problems)

    # 2. The canary task must have a durable job.
    store = JobStore(default_db_path())
    job = _find_by_idempotency_key(store, task_idempotency_key(task_id))
    if job is None:
        problems.append(f"canary task {task_id} has no durable job yet (intake may not have run)")
    else:
        logger.info(f"Canary task {task_id}: job {job['id']} status {job['status']}")

    if not write:
        if problems:
            print("CANARY FAIL (read-only):")
            for p in problems:
                print(f"  - {p}")
            return 2
        print("CANARY PASS (read-only): intake healthy, canary job exists.")
        return 0

    # 3. Write path: post + read-back on the canary discussion.
    if job is None:
        print("CANARY FAIL: no job, refusing the write canary.")
        return 2
    discussion_id = str((job.get("payload") or {}).get("discussion_id") or "")
    if not discussion_id:
        print("CANARY FAIL: canary job has no discussion ID.")
        return 2

    from .ezlynx_discussions import discussion_note_snapshot

    try:
        client = _build_discussion_client()
        before = discussion_note_snapshot(client.get_discussion(discussion_id))
        client.append_note(discussion_id, CANARY_NOTE)
        after = discussion_note_snapshot(client.get_discussion(discussion_id))
    except Exception as e:  # noqa: BLE001
        print(f"CANARY FAIL: write path raised: {e}")
        return 2

    latest_before = before.get("most_recent_note_id") or ""
    latest_after = after.get("most_recent_note_id") or ""
    if latest_after and latest_after != latest_before:
        print(f"CANARY PASS: note posted and confirmed via read-back (note {latest_after}).")
        return 0
    print(f"CANARY FAIL: write not confirmed (before={before}, after={after}).")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EZLynx task intake canary")
    parser.add_argument("--write", action="store_true",
                        help="Post a labeled canary note and verify via read-back")
    parser.add_argument("--task-id", default=CANARY_TASK_ID)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run_canary(write=args.write, task_id=args.task_id)
    except Exception as e:  # noqa: BLE001
        print(f"CANARY FAIL: crashed: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
