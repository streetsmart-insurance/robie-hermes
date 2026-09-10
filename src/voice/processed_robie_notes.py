"""Local persistence for Robie Call note IDs already dispatched or clarified.

Prevents cron/rerun doom loops: once discussionNote.noteId (or discussionId+noteId)
has been handled, it must not fire again. A newer CSR noteId may fire once.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import BASE_DIR

DEFAULT_PROCESSED_NOTES_DB = BASE_DIR / "data" / "robie_call_processed_notes.sqlite"


class ProcessedRobieCallStore:
    """SQLite store of processed EZLynx note identities."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_PROCESSED_NOTES_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_robie_call_notes (
                    identity TEXT PRIMARY KEY,
                    applicant_id TEXT,
                    discussion_id TEXT,
                    note_id TEXT,
                    status TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    processed_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def has(self, identity: Optional[str]) -> bool:
        if not identity:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_robie_call_notes WHERE identity = ? AND dry_run = 0 LIMIT 1",
                (identity,),
            ).fetchone()
        return row is not None

    def mark(
        self,
        identity: str,
        *,
        applicant_id: Optional[str] = None,
        discussion_id: Optional[str] = None,
        note_id: Optional[str] = None,
        status: str = "DISPATCHED",
        dry_run: bool = False,
    ) -> None:
        if not identity or dry_run:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_robie_call_notes
                    (identity, applicant_id, discussion_id, note_id, status, dry_run, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    str(applicant_id) if applicant_id is not None else None,
                    str(discussion_id) if discussion_id is not None else None,
                    str(note_id) if note_id is not None else None,
                    status,
                    1 if dry_run else 0,
                    now,
                ),
            )
            conn.commit()
