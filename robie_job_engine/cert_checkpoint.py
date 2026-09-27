"""Durable dedupe checkpoint for certificates intake.

Drop-in durable replacement for :class:`cert_intake.MemoryDedupeStore`.
Same interface -- ``seen(key)`` / ``mark(key, meta)`` -- backed by SQLite
so a restart, deploy, or crash never loses track of processed mail.

One row per dedupe key (``gmail:<id>``, ``rfc:<message-id>``,
``body:<sha256>``, ``att:<sha256>``). Re-marking a key refreshes
``last_seen_at`` and ``meta`` but keeps the original ``first_seen_at``,
so "when did we first see this" is never rewritten.

WAL mode is enabled for crash safety; a threading lock serializes access
because the intake worker may mark keys from more than one thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedupe_keys (
    key TEXT PRIMARY KEY,
    meta TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SqliteDedupeStore:
    """SQLite-backed dedupe store implementing the cert_intake store protocol."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False: the lock owns all cross-thread safety.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def seen(self, key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM dedupe_keys WHERE key = ?", (key,)
            ).fetchone()
        return row is not None

    def mark(self, key: str, meta: dict[str, Any] | None = None) -> None:
        now = _utcnow()
        meta_json = json.dumps(meta or {}, sort_keys=True, default=str)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dedupe_keys (key, meta, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    meta = excluded.meta,
                    last_seen_at = excluded.last_seen_at
                """,
                (key, meta_json, now, now),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> dict[str, Any] | None:
        """Return the stored meta for a key, or None if never seen."""
        with self._lock:
            row = self._conn.execute(
                "SELECT meta FROM dedupe_keys WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM dedupe_keys").fetchone()[0]
            oldest = self._conn.execute(
                "SELECT MIN(first_seen_at) FROM dedupe_keys"
            ).fetchone()[0]
        return {"keys": n, "oldest_first_seen_at": oldest, "path": str(self._path)}

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
