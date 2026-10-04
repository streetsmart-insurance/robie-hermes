"""Tasks already seen on a Task Check-In report.

The Looker export is capped at 500 rows, newest first. Older task ids
drop off. A task that has been seen stays seen: falling off the cap
does not make it new, and it is not dialed again when the row returns.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SeenTaskStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ezlynx_seen_tasks (
                task_id TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                report_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'seen'
            )"""
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(ezlynx_seen_tasks)")
        }
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE ezlynx_seen_tasks "
                "ADD COLUMN status TEXT NOT NULL DEFAULT 'seen'"
            )
        return conn

    def is_empty(self) -> bool:
        """True when the table is missing or has no task ids.

        A brand-new jobs.db takes this path so the current report is
        baselined and not dialed.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM ezlynx_seen_tasks").fetchone()
        return int(row["n"]) == 0

    def known_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT task_id FROM ezlynx_seen_tasks").fetchall()
        return {str(row["task_id"]) for row in rows}

    def statuses(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, status FROM ezlynx_seen_tasks"
            ).fetchall()
        return {
            str(row["task_id"]): str(row["status"] or "seen")
            for row in rows
        }

    def baseline(self, task_ids: list[str], *, report_digest: str) -> None:
        """Record every current id as seen and dial none of them."""
        now = utcnow_iso()
        with self._connect() as conn:
            for task_id in task_ids:
                token = str(task_id).strip()
                if not token:
                    continue
                conn.execute(
                    """INSERT INTO ezlynx_seen_tasks
                       (task_id, first_seen_at, last_seen_at, report_digest, status)
                       VALUES (?,?,?,?, 'baseline')
                       ON CONFLICT(task_id) DO NOTHING""",
                    (token, now, now, report_digest),
                )

    def mark(self, task_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE ezlynx_seen_tasks SET status=? WHERE task_id=?",
                (status, str(task_id).strip()),
            )

    def observe(self, task_ids: list[str], *, report_digest: str) -> tuple[set[str], set[str]]:
        """Record the ids in this report.

        Returns (new_ids, dropped_ids). Dropped ids were seen before and
        are absent from this report. They must not be dialed or treated
        as new.
        """
        current = {str(task_id).strip() for task_id in task_ids if str(task_id).strip()}
        prior = self.known_ids()
        new_ids = current - prior
        dropped = prior - current
        now = utcnow_iso()
        with self._connect() as conn:
            for task_id in current:
                conn.execute(
                    """INSERT INTO ezlynx_seen_tasks
                       (task_id, first_seen_at, last_seen_at, report_digest, status)
                       VALUES (?,?,?,?, 'seen')
                       ON CONFLICT(task_id) DO UPDATE SET
                         last_seen_at=excluded.last_seen_at,
                         report_digest=excluded.report_digest""",
                    (task_id, now, now, report_digest),
                )
        return new_ids, dropped
