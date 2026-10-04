"""Per-client opt-out of automated calls. Press 6 records one row."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class CallOptOutStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_opt_outs (
                    applicant_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    opted_out_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def is_opted_out(self, applicant_id: str) -> bool:
        key = str(applicant_id or "").strip()
        if not key:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM call_opt_outs WHERE applicant_id = ?",
                (key,),
            ).fetchone()
        return row is not None

    def record_opt_out(self, applicant_id: str, *, source: str) -> None:
        key = str(applicant_id or "").strip()
        if not key:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO call_opt_outs (applicant_id, source, opted_out_at)
                VALUES (?, ?, ?)
                ON CONFLICT(applicant_id) DO NOTHING
                """,
                (key, source, now),
            )


def pressed_opt_out(call_result: dict) -> bool:
    """True when the caller pressed 6. Other digits do not count."""
    if not isinstance(call_result, dict):
        return False
    if call_result.get("opted_out") is True:
        return True
    raw = call_result.get("digits")
    if isinstance(raw, (list, tuple)):
        return "6" in {str(item).strip() for item in raw}
    return str(raw or "").strip() == "6"
