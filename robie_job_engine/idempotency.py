"""Durable idempotency ledger. /tmp is never a persistent store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import ACTION_OUTCOME_UNKNOWN
from .store import utc_now


class IdempotencyError(RuntimeError):
    pass


def assert_durable_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    as_text = str(resolved)
    if as_text == "/tmp" or as_text.startswith("/tmp/") or as_text.startswith("/var/tmp/"):
        raise IdempotencyError("persistent store cannot be /tmp")
    return resolved


class DurableWorkLedger:
    """Atomic acquire on (workflow namespace, stable work-item key)."""

    def __init__(self, db_path: str | Path, *, require_durable: bool = False) -> None:
        self.path = str(assert_durable_path(db_path) if require_durable else Path(db_path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS durable_work_items (
                    namespace TEXT NOT NULL,
                    work_item_key TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    external_actions INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, work_item_key)
                );
                """
            )

    def acquire(
        self,
        namespace: str,
        work_item_key: str,
        *,
        owner: str,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=timeout_seconds)).isoformat()
        stamp = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM durable_work_items
                   WHERE namespace=? AND work_item_key=?""",
                (namespace, work_item_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO durable_work_items
                       (namespace,work_item_key,lease_owner,lease_expires_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (namespace, work_item_key, owner, expiry, stamp, stamp),
                )
                conn.commit()
                return self.get(namespace, work_item_key)
            item = dict(row)
            expired = not item["lease_expires_at"] or item["lease_expires_at"] <= now.isoformat()
            if item["lease_owner"] and item["lease_owner"] != owner and not expired:
                raise IdempotencyError("work item is leased by another owner")
            if expired and item["outcome"] != ACTION_OUTCOME_UNKNOWN:
                conn.execute(
                    """UPDATE durable_work_items SET outcome=?, updated_at=?
                       WHERE namespace=? AND work_item_key=?""",
                    (ACTION_OUTCOME_UNKNOWN, stamp, namespace, work_item_key),
                )
                item["outcome"] = ACTION_OUTCOME_UNKNOWN
            if item["external_actions"] >= 1 and not item["verified"]:
                conn.commit()
                raise IdempotencyError(
                    "verify before any retry; an external action already ran"
                )
            if item["external_actions"] >= 1 and item["verified"]:
                conn.commit()
                return self.get(namespace, work_item_key)
            conn.execute(
                """UPDATE durable_work_items SET lease_owner=?, lease_expires_at=?, updated_at=?
                   WHERE namespace=? AND work_item_key=?""",
                (owner, expiry, stamp, namespace, work_item_key),
            )
            conn.commit()
        return self.get(namespace, work_item_key)

    def record_external_action(self, namespace: str, work_item_key: str) -> dict[str, Any]:
        item = self.get(namespace, work_item_key)
        if item["external_actions"] >= 1:
            raise IdempotencyError("at most one external action is allowed")
        with self._connect() as conn:
            conn.execute(
                """UPDATE durable_work_items SET external_actions=external_actions+1, updated_at=?
                   WHERE namespace=? AND work_item_key=?""",
                (utc_now(), namespace, work_item_key),
            )
        return self.get(namespace, work_item_key)

    def mark_verified(self, namespace: str, work_item_key: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """UPDATE durable_work_items SET verified=1, outcome='VERIFIED', updated_at=?
                   WHERE namespace=? AND work_item_key=?""",
                (utc_now(), namespace, work_item_key),
            )
        return self.get(namespace, work_item_key)

    def mark_timeout_unknown(self, namespace: str, work_item_key: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """UPDATE durable_work_items SET outcome=?, verified=0, updated_at=?
                   WHERE namespace=? AND work_item_key=?""",
                (ACTION_OUTCOME_UNKNOWN, utc_now(), namespace, work_item_key),
            )
        return self.get(namespace, work_item_key)

    def get(self, namespace: str, work_item_key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM durable_work_items
                   WHERE namespace=? AND work_item_key=?""",
                (namespace, work_item_key),
            ).fetchone()
        if row is None:
            raise KeyError((namespace, work_item_key))
        return dict(row)
