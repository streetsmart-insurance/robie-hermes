"""Isolated runs: one immutable run ID, one owner, one terminal event."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import utc_now


ACTIVE_STATUSES = frozenset({"ACTIVE"})
TERMINAL_EVENTS = frozenset(
    {
        "COMPLETE",
        "FAILED",
        "CANCELLED",
        "BLOCKED",
        "UNVERIFIED",
        "INTAKE",
        "ABANDONED",
    }
)


class RunIsolationError(RuntimeError):
    pass


class IsolatedRunStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS isolated_runs (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    terminal_event TEXT,
                    heartbeat_at TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS isolated_run_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES isolated_runs(id),
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_isolated_runs_active
                    ON isolated_runs(status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_isolated_runs_one_active
                    ON isolated_runs(status) WHERE status='ACTIVE';
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(isolated_runs)").fetchall()
            }
            if "heartbeat_at" not in columns:
                conn.execute("ALTER TABLE isolated_runs ADD COLUMN heartbeat_at TEXT")
            if "lease_expires_at" not in columns:
                conn.execute("ALTER TABLE isolated_runs ADD COLUMN lease_expires_at TEXT")

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM isolated_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._decode(row)

    def active_run(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM isolated_runs WHERE status='ACTIVE' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._decode(row) if row else None

    def list_runs(self, job_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM isolated_runs"
        args: list[Any] = []
        if job_id:
            sql += " WHERE job_id=?"
            args.append(job_id)
        sql += " ORDER BY created_at, id"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._decode(row) for row in rows]

    def record_intake(
        self,
        *,
        owner: str,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an intake receipt without acquiring the execution slot.

        Intake performs no external action. Insert its already-terminal run and
        binding atomically so a concurrent worker cannot reject queued work.
        The one-ACTIVE-run execution constraint remains unchanged.
        """
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO isolated_runs
                   (id,owner,job_id,status,terminal_event,created_at,updated_at)
                   VALUES (?,?,?,'INTAKE','INTAKE',?,?)""",
                (run_id, owner, job_id, now, now),
            )
            conn.execute(
                """INSERT INTO isolated_run_bindings
                   (run_id,kind,payload_json,created_at) VALUES (?,'intake',?,?)""",
                (run_id, json.dumps(payload, sort_keys=True), now),
            )
            conn.commit()
        return self.get(run_id)

    @staticmethod
    def _reconcile_stale_in_transaction(
        conn: sqlite3.Connection,
        *,
        now: datetime,
    ) -> list[str]:
        stamp = now.isoformat()
        rows = conn.execute(
            """SELECT id, owner, job_id, lease_expires_at
               FROM isolated_runs
               WHERE status='ACTIVE'
                 AND (lease_expires_at IS NULL OR lease_expires_at<=?)
               ORDER BY created_at, id""",
            (stamp,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE isolated_runs
                   SET status='ABANDONED', terminal_event='ABANDONED',
                       lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND status='ACTIVE'""",
                (stamp, row["id"]),
            )
            conn.execute(
                """INSERT INTO isolated_run_bindings
                   (run_id,kind,payload_json,created_at)
                   VALUES (?,?,?,?)""",
                (
                    row["id"],
                    "startup-reconciliation",
                    json.dumps(
                        {
                            "reason": "run lease expired before a terminal event",
                            "previous_owner": row["owner"],
                            "job_id": row["job_id"],
                            "lease_expires_at": row["lease_expires_at"],
                            "reconciled_at": stamp,
                        },
                        sort_keys=True,
                    ),
                    stamp,
                ),
            )
        return [str(row["id"]) for row in rows]

    def reconcile_stale(self, *, now: datetime | None = None) -> list[str]:
        """Expire dead ACTIVE runs while preserving an auditable terminal row."""
        at = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconciled = self._reconcile_stale_in_transaction(conn, now=at)
            conn.commit()
        return reconciled

    def start(
        self,
        *,
        owner: str,
        job_id: str,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expiry = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._reconcile_stale_in_transaction(conn, now=now_dt)
                conn.execute(
                    """INSERT INTO isolated_runs
                       (id,owner,job_id,status,heartbeat_at,lease_expires_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (run_id, owner, job_id, "ACTIVE", now, expiry, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                row = conn.execute(
                    "SELECT id, owner FROM isolated_runs WHERE status='ACTIVE' ORDER BY created_at LIMIT 1"
                ).fetchone()
                owner_label = row["owner"] if row else "another owner"
                run_label = row["id"] if row else "unknown"
                raise RunIsolationError(
                    f"rejecting new run; active run {run_label} is owned by {owner_label}"
                ) from exc
        finally:
            conn.close()
        return self.get(run_id)

    def renew_lease(
        self,
        run_id: str,
        *,
        owner: str,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        """Heartbeat only a live run still owned by this worker.

        A delayed heartbeat may arrive just after its timestamp expires when a
        busy runner pauses the heartbeat thread.  Ownership is still
        authoritative until ``start`` reconciles the run as ABANDONED.  The
        status and owner predicates below therefore allow that same worker to
        recover, while still refusing a terminal run or a run taken over by a
        different worker.
        """
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        expiry = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
        with self._connect() as conn:
            changed = conn.execute(
                """UPDATE isolated_runs
                   SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                   WHERE id=? AND owner=? AND status='ACTIVE'
                     AND terminal_event IS NULL""",
                (stamp, expiry, stamp, run_id, owner),
            ).rowcount
        if changed != 1:
            raise RunIsolationError(
                f"run lease is terminal, missing, or owned by another worker: {run_id}"
            )
        return self.get(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.assert_can_work(run_id)
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """UPDATE isolated_runs SET cancel_requested=1, status='CANCELLED',
                   terminal_event='CANCELLED', lease_expires_at=NULL, updated_at=? WHERE id=?""",
                (now, run_id),
            )
        self.bind(run_id, "cleanup", {"cancelled": True, "owner": run["owner"]})
        return self.get(run_id)

    def terminate(self, run_id: str, event: str) -> dict[str, Any]:
        if event not in TERMINAL_EVENTS:
            raise ValueError(f"unsupported terminal event: {event}")
        run = self.get(run_id)
        if run["terminal_event"]:
            if run["terminal_event"] != event:
                raise RunIsolationError(
                    f"run {run_id} already has terminal event {run['terminal_event']}"
                )
            return run
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """UPDATE isolated_runs SET status=?, terminal_event=?,
                   lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND terminal_event IS NULL""",
                (event, event, now, run_id),
            )
        return self.get(run_id)

    def assert_can_work(self, run_id: str) -> dict[str, Any]:
        run = self.get(run_id)
        if run["terminal_event"] or run["status"] != "ACTIVE":
            raise RunIsolationError(f"no work after terminal event on run {run_id}")
        if run["cancel_requested"]:
            raise RunIsolationError(f"run {run_id} is cancelled")
        if run.get("lease_expires_at") and run["lease_expires_at"] <= utc_now():
            raise RunIsolationError(f"run lease expired: {run_id}")
        return run

    def bind(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        run = self.get(run_id)
        if run["terminal_event"] and kind != "cleanup":
            raise RunIsolationError(f"no work after terminal event on run {run_id}")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO isolated_run_bindings(run_id,kind,payload_json,created_at)
                   VALUES (?,?,?,?)""",
                (run_id, kind, json.dumps(payload, sort_keys=True), utc_now()),
            )

    def bindings(self, run_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT kind, payload_json, created_at FROM isolated_run_bindings WHERE run_id=?"
        args: list[Any] = [run_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def resume(self, *, owner: str, job_id: str, previous_run_id: str) -> dict[str, Any]:
        previous = self.get(previous_run_id)
        if not previous["terminal_event"]:
            raise RunIsolationError("cannot resume while the previous run is active")
        nxt = self.start(owner=owner, job_id=job_id)
        self.bind(nxt["id"], "resume-from", {"previous_run_id": previous_run_id})
        return nxt
