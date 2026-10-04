"""Recorded opt-in for marketing calls.

Sales Center and Winback dial only when a row exists. The store does not
infer opt-in from a missing row.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class CallOptInStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_opt_ins (
                    applicant_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def has_opt_in(self, applicant_id: str) -> bool:
        key = str(applicant_id or "").strip()
        if not key:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM call_opt_ins WHERE applicant_id = ?",
                (key,),
            ).fetchone()
        return row is not None

    def record_opt_in(self, applicant_id: str, *, source: str) -> None:
        key = str(applicant_id or "").strip()
        if not key:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO call_opt_ins (applicant_id, source, recorded_at)
                VALUES (?, ?, ?)
                ON CONFLICT(applicant_id) DO NOTHING
                """,
                (key, source, now),
            )
