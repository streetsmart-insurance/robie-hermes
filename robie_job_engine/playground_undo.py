"""Append-only undo log for Playground writes.

One row per write: who asked, when, the job id, before, after, and the
readback result. Rows are not updated or deleted.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import JobStore, utc_now

# Names of the triggers that refuse UPDATE and DELETE. All-clients mode
# checks this tuple, so emptying it turns all-clients back into the id list.
APPEND_ONLY_TRIGGERS = (
    "playground_undo_log_no_delete",
    "playground_undo_log_no_update",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playground_undo_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    client_name TEXT NOT NULL,
    applicant_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    before_value TEXT NOT NULL,
    after_value TEXT NOT NULL,
    write_kind TEXT NOT NULL,
    readback_result TEXT NOT NULL,
    readback_detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS playground_undo_log_created
    ON playground_undo_log(created_at);
"""


def ensure_schema(store: JobStore) -> None:
    with store.connect() as conn:
        conn.executescript(_SCHEMA)
        _ensure_append_only(conn)


def _ensure_append_only(conn: sqlite3.Connection) -> None:
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='playground_undo_log'"
        ).fetchall()
    }
    if "playground_undo_log_no_delete" in APPEND_ONLY_TRIGGERS and "playground_undo_log_no_delete" not in existing:
        conn.execute(
            """CREATE TRIGGER playground_undo_log_no_delete
               BEFORE DELETE ON playground_undo_log BEGIN
               SELECT RAISE(ABORT, 'playground undo log is append-only');
               END"""
        )
    if "playground_undo_log_no_update" in APPEND_ONLY_TRIGGERS and "playground_undo_log_no_update" not in existing:
        conn.execute(
            """CREATE TRIGGER playground_undo_log_no_update
               BEFORE UPDATE ON playground_undo_log BEGIN
               SELECT RAISE(ABORT, 'playground undo log is append-only');
               END"""
        )


def record_write(
    store: JobStore,
    *,
    job_id: str,
    requested_by: str,
    requested_at: str,
    client_name: str,
    applicant_id: str,
    field_name: str,
    before_value: str,
    after_value: str,
    write_kind: str,
    readback_result: str,
    readback_detail: str,
    created_at: str | None = None,
) -> int:
    ensure_schema(store)
    stamp = created_at or utc_now()
    with store.connect() as conn:
        cursor = conn.execute(
            """INSERT INTO playground_undo_log (
                   job_id, requested_by, requested_at, client_name, applicant_id,
                   field_name, before_value, after_value, write_kind,
                   readback_result, readback_detail, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                requested_by or "unknown",
                requested_at or stamp,
                client_name or "unknown client",
                applicant_id or "",
                field_name or "",
                before_value if before_value is not None else "",
                after_value if after_value is not None else "",
                write_kind or "",
                readback_result or "",
                readback_detail or "",
                stamp,
            ),
        )
        return int(cursor.lastrowid)


def list_writes_since(
    store: JobStore,
    *,
    since: datetime,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(store)
    end = until or datetime.now(timezone.utc)
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM playground_undo_log
               WHERE created_at >= ? AND created_at < ?
               ORDER BY client_name, created_at""",
            (since.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(row) for row in rows]


def render_daily_change_list(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> str:
    """Plain English, grouped by client, for the last day of Playground writes."""
    moment = now or datetime.now(timezone.utc)
    lines = ["Playground changes in the last 24 hours", ""]
    if not rows:
        lines.append("No changes.")
        lines.append("")
        lines.append(f"Through {moment.strftime('%Y-%m-%d %H:%M UTC')}.")
        return "\n".join(lines).strip() + "\n"
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("client_name") or "Unknown client"), []).append(row)
    for client in sorted(grouped):
        lines.append(client)
        for row in grouped[client]:
            who = row.get("requested_by") or "someone"
            field_name = row.get("field_name") or "field"
            before = row.get("before_value") or "blank"
            after = row.get("after_value") or "blank"
            result = row.get("readback_result") or "not checked"
            when = str(row.get("created_at") or "")
            lines.append(
                f"- {field_name}: {before} → {after}. "
                f"Asked by {who}. Readback: {result}. When: {when}."
            )
        lines.append("")
    lines.append(f"Through {moment.strftime('%Y-%m-%d %H:%M UTC')}.")
    return "\n".join(lines).strip() + "\n"


def daily_window(*, now: datetime | None = None, hours: int = 24) -> tuple[datetime, datetime]:
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end - timedelta(hours=hours), end
