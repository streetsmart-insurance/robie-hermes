"""Isolated runs: one immutable run ID, one owner, one terminal event."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .store import utc_now


ACTIVE_STATUSES = frozenset({"ACTIVE"})
TERMINAL_EVENTS = frozenset({"COMPLETE", "FAILED", "CANCELLED", "BLOCKED", "UNVERIFIED"})


class RunIsolationError(RuntimeError):
    pass


class IsolatedRunStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
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
                """
            )

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

    def start(self, *, owner: str, job_id: str) -> dict[str, Any]:
        active = self.active_run()
        if active:
            raise RunIsolationError(
                f"rejecting new run; active run {active['id']} is owned by {active['owner']}"
            )
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO isolated_runs
                   (id,owner,job_id,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (run_id, owner, job_id, "ACTIVE", now, now),
            )
        return self.get(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.assert_can_work(run_id)
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """UPDATE isolated_runs SET cancel_requested=1, status='CANCELLED',
                   terminal_event='CANCELLED', updated_at=? WHERE id=?""",
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
                """UPDATE isolated_runs SET status=?, terminal_event=?, updated_at=?
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
