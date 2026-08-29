"""Durable idempotency ledger. /tmp is never a persistent store."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import ACTION_OUTCOME_UNKNOWN
from .store import utc_now


class IdempotencyError(RuntimeError):
    pass


EPHEMERAL_PREFIXES = (
    "/tmp",
    "/private/tmp",
    "/var/tmp",
    "/private/var/tmp",
)


def _normalize_path_text(value: str) -> str:
    text = value.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def _is_ephemeral_text(value: str) -> bool:
    text = _normalize_path_text(value)
    for prefix in EPHEMERAL_PREFIXES:
        if text == prefix or text.startswith(prefix + "/"):
            return True
    return False


def path_variants(path: str | Path) -> set[str]:
    """Return lexical, absolute, resolved, and realpath forms."""
    raw = Path(path).expanduser()
    variants = {str(raw), str(raw.absolute()), os.path.normpath(str(raw))}
    try:
        variants.add(str(raw.resolve()))
    except OSError:
        pass
    try:
        variants.add(os.path.realpath(str(raw)))
    except OSError:
        pass
    return {_normalize_path_text(item) for item in variants}


def assert_durable_path(path: str | Path) -> Path:
    """Reject /tmp, /private/tmp, /var/tmp, and any symlink into them."""
    for variant in path_variants(path):
        if _is_ephemeral_text(variant):
            raise IdempotencyError(
                "persistent store cannot be /tmp, /private/tmp, or /var/tmp"
            )
    return Path(path).expanduser().resolve()


class DurableWorkLedger:
    """Atomic acquire on (workflow namespace, stable work-item key)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(assert_durable_path(db_path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
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

    def reserve(self, namespace: str, work_item_key: str) -> dict[str, Any]:
        """Create the durable work row at intake without taking an execution lease."""
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
                       (namespace,work_item_key,created_at,updated_at)
                       VALUES (?,?,?,?)""",
                    (namespace, work_item_key, stamp, stamp),
                )
            conn.commit()
        return self.get(namespace, work_item_key)

    def acquire(
        self,
        namespace: str,
        work_item_key: str,
        *,
        owner: str,
        timeout_seconds: int = 60,
        allow_unverified_existing: bool = False,
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
                if not allow_unverified_existing:
                    conn.commit()
                    raise IdempotencyError(
                        "verify before any retry; an external action already ran"
                    )
                conn.execute(
                    """UPDATE durable_work_items
                       SET lease_owner=?, lease_expires_at=?, updated_at=?
                       WHERE namespace=? AND work_item_key=?""",
                    (owner, expiry, stamp, namespace, work_item_key),
                )
                conn.commit()
                return self.get(namespace, work_item_key)
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
        stamp = utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT external_actions FROM durable_work_items
                   WHERE namespace=? AND work_item_key=?""",
                (namespace, work_item_key),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError((namespace, work_item_key))
            cur = conn.execute(
                """UPDATE durable_work_items SET external_actions=external_actions+1, updated_at=?
                   WHERE namespace=? AND work_item_key=? AND external_actions=0""",
                (stamp, namespace, work_item_key),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise IdempotencyError("at most one external action is allowed")
            conn.commit()
        finally:
            conn.close()
        return self.get(namespace, work_item_key)

    def renew_lease(
        self,
        namespace: str,
        work_item_key: str,
        *,
        owner: str,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Heartbeat the exact durable-work reservation owned by a worker."""
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=max(1, timeout_seconds))).isoformat()
        with self._connect() as conn:
            changed = conn.execute(
                """UPDATE durable_work_items
                   SET lease_expires_at=?,updated_at=?
                   WHERE namespace=? AND work_item_key=? AND lease_owner=?""",
                (
                    expiry,
                    utc_now(),
                    namespace,
                    work_item_key,
                    owner,
                ),
            ).rowcount
            if changed != 1:
                raise IdempotencyError(
                    "durable work lease is missing or owned by another worker"
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
