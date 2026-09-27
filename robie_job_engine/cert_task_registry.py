"""Certificates chunk 3: durable task registry.

One task per (client + policy + certificate holder) request. The registry
is the worker's memory of which EZLynx discussion and which Zapier-created
task belong to each request, so follow-up emails reuse instead of
duplicating. Task open/closed state comes from the registry, refreshed by
the Zapier lookup when it is configured; on lookup failure the worker
treats the task as still open (fail-closed: never create a duplicate).
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

TASK_OPEN = "open"
TASK_CLOSED = "closed"
TASK_UNKNOWN = "unknown"

# decide_task_action() outcomes
CREATE = "create"    # genuinely new request, no task on file -> fire the Zap
REUSE = "reuse"      # open task on file -> file the email, no new task
REOPEN = "reopen"    # closed task + genuinely new request -> reopen, never duplicate
NONE = "none"        # acknowledgement -> leave task state alone
HOLD = "hold"        # cannot decide safely -> human


def policy_key_for(policy_numbers: list[str]) -> str:
    """Stable key for the request's policy. 'none' when the email names none."""
    nums = sorted({re.sub(r"\s+", "", n or "").upper()
                   for n in policy_numbers or [] if n})
    return "|".join(nums) if nums else "none"


def holder_key_for(holder_names: list[str]) -> str:
    """Stable key for the certificate holder. 'none' when unnamed."""
    names = sorted({re.sub(r"[^a-z0-9]", "", (n or "").lower())
                    for n in holder_names or [] if n})
    return "|".join(names) if names else "none"


@dataclass
class TaskEntry:
    applicant_id: int
    policy_key: str
    holder_key: str
    discussion_id: str | None = None
    task_id: str | None = None
    task_status: str = TASK_UNKNOWN
    created_at: float = 0.0
    updated_at: float = 0.0


class TaskRegistry:
    """SQLite-backed registry. Same seen/mark spirit as the dedupe store."""

    def __init__(self, db_path: str) -> None:
        self._db = sqlite3.connect(db_path, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cert_tasks (
                applicant_id INTEGER NOT NULL,
                policy_key TEXT NOT NULL,
                holder_key TEXT NOT NULL,
                discussion_id TEXT,
                task_id TEXT,
                task_status TEXT NOT NULL DEFAULT 'unknown',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (applicant_id, policy_key, holder_key)
            )"""
        )
        self._db.commit()

    def get(self, applicant_id: int, policy_key: str,
            holder_key: str) -> TaskEntry | None:
        row = self._db.execute(
            "SELECT applicant_id, policy_key, holder_key, discussion_id,"
            " task_id, task_status, created_at, updated_at FROM cert_tasks"
            " WHERE applicant_id=? AND policy_key=? AND holder_key=?",
            (applicant_id, policy_key, holder_key),
        ).fetchone()
        return TaskEntry(*row) if row else None

    def put(self, entry: TaskEntry) -> None:
        now = time.time()
        if not entry.created_at:
            entry.created_at = now
        entry.updated_at = now
        self._db.execute(
            """INSERT INTO cert_tasks
               (applicant_id, policy_key, holder_key, discussion_id, task_id,
                task_status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(applicant_id, policy_key, holder_key) DO UPDATE SET
                discussion_id=excluded.discussion_id,
                task_id=excluded.task_id,
                task_status=excluded.task_status,
                updated_at=excluded.updated_at""",
            (entry.applicant_id, entry.policy_key, entry.holder_key,
             entry.discussion_id, entry.task_id, entry.task_status,
             entry.created_at, entry.updated_at),
        )
        self._db.commit()


def decide_task_action(requested_action: str, entry: TaskEntry | None,
                       live_state: str = TASK_UNKNOWN) -> str:
    """Apply Carlo's task rules (2026-09-26).

    - New request + no entry -> CREATE.
    - New request + open task -> REUSE (file into the task's discussion).
    - New request + closed task -> REOPEN (never a duplicate).
    - Acknowledgement/thank-you -> NONE (file the email, leave the task).
    - Unknown action or unknown state -> HOLD for a human, never guess.
    """
    is_new = requested_action == "new_request"
    is_ack = requested_action == "acknowledgement"
    if not is_new and not is_ack:
        return HOLD
    if is_ack:
        return NONE
    if entry is None or not entry.task_id:
        return CREATE
    state = live_state if live_state != TASK_UNKNOWN else entry.task_status
    if state == TASK_CLOSED:
        return REOPEN
    # TASK_OPEN, or anything unverifiable: reuse, never duplicate.
    return REUSE
