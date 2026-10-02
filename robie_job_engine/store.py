from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .complete_guard import intended_destination_identity, require_complete_postcondition
from .models import (
    VERIFIER_AUTHORITY,
    WAITING_STATUSES,
    JobStatus,
    TERMINAL_STATUSES,
    VerificationEvidence,
)
from .secrets import redact_mapping, redact_text

logger = logging.getLogger(__name__)
PERFORM_PROGRESS_KINDS = (
    "gateway_progress",
    "perform_progress",
    "worker_progress",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class JobStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    action_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resume_status TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    verification_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_wakeup_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS job_intake (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS job_intake_no_update
                    BEFORE UPDATE ON job_intake BEGIN
                    SELECT RAISE(ABORT, 'original job intake is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS job_intake_no_delete
                    BEFORE DELETE ON job_intake BEGIN
                    SELECT RAISE(ABORT, 'original job intake is immutable'); END;
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    kind TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, kind)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    phase TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verification_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    verified INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    source TEXT NOT NULL,
                    authoritative INTEGER NOT NULL,
                    expected_json TEXT NOT NULL,
                    observed_json TEXT NOT NULL,
                    locator TEXT,
                    evidence_sha256 TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_wakeup
                    ON jobs(status, next_wakeup_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_job
                    ON verification_evidence(job_id, id);
                CREATE TABLE IF NOT EXISTS playwright_exec (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    tool TEXT NOT NULL,
                    status TEXT NOT NULL,
                    code_preview TEXT,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_playwright_exec_job
                    ON playwright_exec(job_id, id);
                CREATE TABLE IF NOT EXISTS jev_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    verdict TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    escalate INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jev_evaluations_job
                    ON jev_evaluations(job_id, id);
                """
            )

    def create_job(
        self,
        action_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        payload = redact_mapping(payload)
        key = idempotency_key or hashlib.sha256(
            f"{action_type}:{canonical_json(payload)}".encode()
        ).hexdigest()
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row:
                return self._decode_job(row)
            job_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO jobs
                (id,idempotency_key,action_type,payload_json,status,max_attempts,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (job_id, key, action_type, canonical_json(payload), JobStatus.PENDING, max_attempts, now, now),
            )
            conn.execute(
                "INSERT INTO job_intake (job_id,payload_json,created_at) VALUES (?,?,?)",
                (job_id, canonical_json(payload), now),
            )
            return self.get_job(job_id, conn=conn)

    def get_job(self, job_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        owned = conn is None
        conn = conn or self.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            return self._decode_job(row)
        finally:
            if owned:
                conn.close()

    def heartbeat_generic_chat_job(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        source: str = "hermes-gateway",
    ) -> dict[str, Any]:
        """Record that hermes-gateway is still executing this generic Chat Job.

        Generic ``hermes.google_chat_task`` Jobs are marked RUNNING with no
        JobEngine lease so the gateway can proceed. This heartbeat is the
        durable signal that distinguishes live gateway work from an abandoned
        ledger row.
        """
        at = now or datetime.now(timezone.utc)
        stamp = at.isoformat()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if (
                row["action_type"] != "hermes.google_chat_task"
                or row["status"] != JobStatus.RUNNING.value
            ):
                return self.get_job(job_id, conn=conn)
            conn.execute(
                "UPDATE jobs SET updated_at=? WHERE id=?",
                (stamp, job_id),
            )
            existing = conn.execute(
                """SELECT data_json, created_at FROM checkpoints
                   WHERE job_id=? AND kind='gateway_progress'""",
                (job_id,),
            ).fetchone()
            first_at = stamp
            if existing is not None:
                previous = json.loads(existing["data_json"] or "{}")
                first_at = (
                    str(previous.get("first_at") or "").strip()
                    or str(existing["created_at"] or "").strip()
                    or stamp
                )
            conn.execute(
                """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                   VALUES (?, 'gateway_progress', ?, ?)
                   ON CONFLICT(job_id,kind) DO UPDATE SET
                   data_json=excluded.data_json,created_at=excluded.created_at""",
                (
                    job_id,
                    canonical_json(
                        {
                            "source": source,
                            "first_at": first_at,
                            "last_at": stamp,
                        }
                    ),
                    stamp,
                ),
            )
            return self.get_job(job_id, conn=conn)

    def fail_orphaned_chat_jobs(
        self,
        *,
        older_than_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[str]:
        """Fail generic Chat Jobs that nothing is actually executing.

        JobEngine never claims ``hermes.google_chat_task`` or
        ``hermes.plain_english`` (they are not bounded actions), so
        RUNNING + attempt 0 + ``lease_owner IS NULL`` is the normal start
        state. A job is abandoned only when that ledger state is stale
        *and* hermes-gateway has not written a recent ``gateway_progress``
        heartbeat.
        """
        if older_than_seconds < 1:
            raise ValueError("orphan timeout must be positive")
        at = now or datetime.now(timezone.utc)
        cutoff = (at - timedelta(seconds=older_than_seconds)).isoformat()
        stamp = at.isoformat()
        reason = (
            "execution did not start: the generic Google Chat Job was not claimed "
            f"within {older_than_seconds} seconds"
        )
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT id FROM jobs
                   WHERE status=? AND action_type IN (
                         'hermes.google_chat_task', 'hermes.plain_english'
                     )
                     AND attempt_count=0 AND lease_owner IS NULL
                     AND updated_at<=?
                     AND NOT EXISTS (
                       SELECT 1 FROM checkpoints
                       WHERE checkpoints.job_id=jobs.id
                         AND checkpoints.kind='gateway_progress'
                         AND checkpoints.created_at>?
                     )""",
                (JobStatus.RUNNING.value, cutoff, cutoff),
            ).fetchall()
            job_ids = [str(row["id"]) for row in rows]
            for job_id in job_ids:
                conn.execute(
                    """UPDATE jobs SET status=?,last_error=?,next_wakeup_at=NULL,
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                    (JobStatus.FAILED.value, reason, stamp, job_id),
                )
                conn.execute(
                    """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                       VALUES (?, 'orphan_timeout', ?, ?)
                       ON CONFLICT(job_id,kind) DO UPDATE SET
                       data_json=excluded.data_json,created_at=excluded.created_at""",
                    (
                        job_id,
                        canonical_json({"reason": reason, "cutoff": cutoff}),
                        stamp,
                    ),
                )
        self._stop_capture(job_ids)
        return job_ids

    def fail_dead_running_jobs(
        self,
        *,
        older_than_seconds: int = 600,
        now: datetime | None = None,
    ) -> list[str]:
        """Fail RUNNING jobs that have no live lease and no fresh heartbeat.

        A ``gateway_progress`` row newer than the cutoff means a worker is
        still alive. Those jobs stay RUNNING.
        """
        if older_than_seconds < 1:
            raise ValueError("dead-running timeout must be positive")
        at = now or datetime.now(timezone.utc)
        cutoff = (at - timedelta(seconds=older_than_seconds)).isoformat()
        stamp = at.isoformat()
        reason = (
            "The job was still running with no live worker. "
            f"It was stopped after {older_than_seconds} seconds."
        )
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT id FROM jobs
                   WHERE status=?
                     AND updated_at<=?
                     AND NOT (
                       lease_owner IS NOT NULL AND TRIM(lease_owner)!=''
                       AND lease_expires_at IS NOT NULL AND lease_expires_at>?
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM checkpoints
                       WHERE checkpoints.job_id=jobs.id
                         AND checkpoints.kind='gateway_progress'
                         AND checkpoints.created_at>?
                     )""",
                (JobStatus.RUNNING.value, cutoff, stamp, cutoff),
            ).fetchall()
            job_ids = [str(row["id"]) for row in rows]
            for job_id in job_ids:
                conn.execute(
                    """UPDATE jobs SET status=?,last_error=?,next_wakeup_at=NULL,
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                    (JobStatus.FAILED.value, reason, stamp, job_id),
                )
                conn.execute(
                    """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                       VALUES (?, 'dead_running', ?, ?)
                       ON CONFLICT(job_id,kind) DO UPDATE SET
                       data_json=excluded.data_json,created_at=excluded.created_at""",
                    (
                        job_id,
                        canonical_json({"reason": reason, "cutoff": cutoff}),
                        stamp,
                    ),
                )
        self._stop_capture(job_ids)
        return job_ids

    def fail_gateway_restart_orphans(
        self,
        *,
        now: datetime | None = None,
        exclude: set[str] | None = None,
    ) -> list[str]:
        """Fail Chat jobs still RUNNING after the gateway process died.

        A fresh ``gateway_progress`` heartbeat does not keep the job alive.
        The process that wrote it is gone. Jobs this process is still
        running can be passed in ``exclude``.
        """
        actions = (
            "hermes.google_chat_task",
            "hermes.plain_english",
            "ezlynx.quote",
            "ezlynx.commercial_auto",
            "ezlynx.policy_change",
            "ezlynx.policy_setup",
            "ezlynx.certificate",
        )
        at = now or datetime.now(timezone.utc)
        stamp = at.isoformat()
        reason = (
            "The gateway restarted while this job was still running. "
            "It was stopped. Send it again if you still want it done."
        )
        keep = {str(item) for item in (exclude or set()) if item}
        placeholders = ",".join("?" for _ in actions)
        with self.transaction() as conn:
            rows = conn.execute(
                f"""SELECT id FROM jobs
                    WHERE status=? AND action_type IN ({placeholders})
                      AND lease_owner IS NULL""",
                (JobStatus.RUNNING.value, *actions),
            ).fetchall()
            job_ids = [str(row["id"]) for row in rows if str(row["id"]) not in keep]
            for job_id in job_ids:
                conn.execute(
                    """UPDATE jobs SET status=?,last_error=?,next_wakeup_at=NULL,
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                    (JobStatus.FAILED.value, reason, stamp, job_id),
                )
                conn.execute(
                    """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                       VALUES (?, 'restart_orphan', ?, ?)
                       ON CONFLICT(job_id,kind) DO UPDATE SET
                       data_json=excluded.data_json,created_at=excluded.created_at""",
                    (
                        job_id,
                        canonical_json({"reason": reason}),
                        stamp,
                    ),
                )
        self._stop_capture(job_ids)
        return job_ids

    def retarget_unattempted(
        self,
        job_id: str,
        action_type: str,
        payload_updates: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Safely reroute a zero-attempt generic Job without changing its ID."""
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["attempt_count"] != 0:
                raise ValueError("attempted jobs cannot be retargeted")
            if row["lease_owner"]:
                raise ValueError("leased jobs cannot be retargeted")
            if row["action_type"] not in {
                "hermes.google_chat_task",
                "hermes.plain_english",
                "hermes.needs_clarification",
            }:
                raise ValueError("only generic chat jobs can be retargeted")
            if row["status"] not in {
                JobStatus.PENDING.value,
                JobStatus.RUNNING.value,
                JobStatus.UNVERIFIED.value,
                JobStatus.NEEDS_CLARIFICATION.value,
            }:
                raise ValueError(f"job status {row['status']} cannot be retargeted")
            action = conn.execute(
                "SELECT 1 FROM checkpoints WHERE job_id=? AND kind='action'", (job_id,)
            ).fetchone()
            if action is not None:
                raise ValueError("jobs with destination action evidence cannot be retargeted")
            payload = json.loads(row["payload_json"] or "{}")
            payload.update(redact_mapping(payload_updates))
            conn.execute(
                """UPDATE jobs SET action_type=?,payload_json=?,status=?,resume_status=NULL,
                   next_wakeup_at=NULL,lease_owner=NULL,lease_expires_at=NULL,last_error=NULL,
                   updated_at=? WHERE id=?""",
                (
                    action_type,
                    canonical_json(payload),
                    JobStatus.PENDING.value,
                    now,
                    job_id,
                ),
            )
            conn.execute(
                """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                   VALUES (?, 'route_correction', ?, ?)
                   ON CONFLICT(job_id,kind) DO UPDATE SET
                   data_json=excluded.data_json,created_at=excluded.created_at""",
                (
                    job_id,
                    canonical_json(
                        {
                            "from_action_type": row["action_type"],
                            "to_action_type": action_type,
                            "reason": reason,
                        }
                    ),
                    now,
                ),
            )
            return self.get_job(job_id, conn=conn)

    def claim(self, job_id: str, owner: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            current = JobStatus(row["status"])
            if (
                current in TERMINAL_STATUSES
                or current in WAITING_STATUSES
                or current == JobStatus.RETRY_WAIT
            ):
                return None
            lease_expired = not row["lease_expires_at"] or row["lease_expires_at"] <= now.isoformat()
            if row["lease_owner"] and not lease_expired:
                return None
            conn.execute(
                "UPDATE jobs SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=?",
                (owner, expiry, now.isoformat(), job_id),
            )
            return self.get_job(job_id, conn=conn)

    def release_lease(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=?""",
                (now, job_id),
            )
            return self.get_job(job_id, conn=conn)

    def renew_lease(
        self,
        job_id: str,
        owner: str,
        *,
        lease_seconds: int = 120,
    ) -> dict[str, Any]:
        """Extend the exact worker-owned Job lease without changing attempts."""
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self.transaction() as conn:
            changed = conn.execute(
                """UPDATE jobs SET lease_expires_at=?,updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (expiry, now.isoformat(), job_id, owner),
            ).rowcount
            if changed != 1:
                raise RuntimeError("job lease is missing or owned by another worker")
            return self.get_job(job_id, conn=conn)

    def transition(
        self,
        job_id: str,
        status: JobStatus,
        *,
        expected: set[JobStatus] | None = None,
        error: str | None = None,
        next_wakeup_at: str | None = None,
        resume_status: JobStatus | None = None,
        release_lease: bool = False,
        authority: str = "job-engine",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            current = JobStatus(row["status"])
            if expected is not None and current not in expected:
                raise RuntimeError(f"invalid transition {current} -> {status}")
            if status == JobStatus.COMPLETE:
                evidence = conn.execute(
                    """SELECT locator,expected_json,observed_json,captured_at,
                              evidence_sha256,verified,authoritative,created_at
                       FROM verification_evidence
                       WHERE job_id=? AND verified=1 AND authoritative=1
                       ORDER BY id DESC LIMIT 1""",
                    (job_id,),
                ).fetchone()
                job_meta = conn.execute(
                    "SELECT created_at, payload_json FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                perform = conn.execute(
                    """SELECT created_at FROM attempts
                       WHERE job_id=? AND phase='perform'
                       ORDER BY id DESC LIMIT 1""",
                    (job_id,),
                ).fetchone()
                action_ckpt = conn.execute(
                    """SELECT created_at, data_json FROM checkpoints
                       WHERE job_id=? AND kind='action'""",
                    (job_id,),
                ).fetchone()
                attempt_floors = [
                    perform["created_at"] if perform else None,
                    action_ckpt["created_at"] if action_ckpt else None,
                ]
                stored_not_before = max(
                    (item for item in attempt_floors if item),
                    default=job_meta["created_at"] if job_meta else None,
                )
                action_data = (
                    json.loads(action_ckpt["data_json"])
                    if action_ckpt and action_ckpt["data_json"]
                    else None
                )
                payload = (
                    json.loads(job_meta["payload_json"])
                    if job_meta and job_meta["payload_json"]
                    else None
                )
                require_complete_postcondition(
                    current=current,
                    authority=authority,
                    verified=bool(evidence and evidence["verified"]),
                    authoritative=bool(evidence and evidence["authoritative"]),
                    expected=json.loads(evidence["expected_json"]) if evidence else None,
                    observed=json.loads(evidence["observed_json"]) if evidence else None,
                    captured_at=evidence["captured_at"] if evidence else None,
                    evidence_ref=evidence["evidence_sha256"] if evidence else None,
                    locator=evidence["locator"] if evidence else None,
                    job_id=job_id,
                    verifier_authority=VERIFIER_AUTHORITY,
                    not_before=job_meta["created_at"] if job_meta else None,
                    stored_at=evidence["created_at"] if evidence else None,
                    stored_not_before=stored_not_before,
                    intended=intended_destination_identity(
                        action=action_data, payload=payload
                    ),
                    action=action_data,
                    payload=payload,
                )
            completed_at = now if status == JobStatus.COMPLETE else None
            conn.execute(
                """UPDATE jobs SET status=?,resume_status=?,last_error=?,next_wakeup_at=?,
                lease_owner=CASE WHEN ? THEN NULL ELSE lease_owner END,
                lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                completed_at=?,updated_at=? WHERE id=?""",
                (
                    status,
                    resume_status,
                    redact_text(error) if error else None,
                    next_wakeup_at,
                    release_lease,
                    release_lease,
                    completed_at,
                    now,
                    job_id,
                ),
            )
            job = self.get_job(job_id, conn=conn)
        if status in TERMINAL_STATUSES:
            self._stop_capture([job_id])
            self._release_turn_lock(job_id)
        return job

    def _release_turn_lock(self, job_id: str) -> None:
        """A terminal job does not keep the Chat turn lock.

        Interrupt the running agent now. Job 598820fc stayed COMPLETE for
        about a minute while its turn kept calling tools. Kill registered
        tool processes. Do not set the /stop flag: a normal finish is not a
        stop, and the confirmation send still has to post.
        """
        try:
            from .chat_turn_control import (
                kill_agent_processes,
                release_finished_job_session,
            )

            status = ""
            try:
                status = str((self.get_job(job_id) or {}).get("status") or "")
            except Exception:
                status = ""
            # CANCELLED sets the stop flag so a later tool call cannot write.
            # COMPLETE and UNVERIFIED do not: the confirmation send still posts.
            # Every terminal status still drops the session lease and the clarify.
            release_finished_job_session(
                self.path,
                job_id,
                stop_agent=status == "CANCELLED",
            )
            kill_agent_processes(job_id)
        except Exception:
            logger.debug("turn lock release failed job=%s", job_id, exc_info=True)

    def _stop_capture(self, job_ids: list[str]) -> None:
        """A terminal job writes the browser_capture stop file."""
        if not job_ids:
            return
        try:
            from .recording import touch_browser_capture_stop_files
        except Exception:
            return
        for job_id in job_ids:
            touch_browser_capture_stop_files(self.path, job_id)

    def increment(self, job_id: str, field: str) -> int:
        if field not in {"attempt_count", "verification_count"}:
            raise ValueError(field)
        with self.transaction() as conn:
            conn.execute(f"UPDATE jobs SET {field}={field}+1,updated_at=? WHERE id=?", (utc_now(), job_id))
            return int(conn.execute(f"SELECT {field} FROM jobs WHERE id=?", (job_id,)).fetchone()[0])

    def update_payload(self, job_id: str, payload: dict[str, Any]) -> None:
        """Persist identifier-only payload patches. Never used as evidence."""
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            conn.execute(
                "UPDATE jobs SET payload_json=?, updated_at=? WHERE id=?",
                (canonical_json(redact_mapping(payload)), now, job_id),
            )

    def checkpoint(self, job_id: str, kind: str, data: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO checkpoints(job_id,kind,data_json,created_at) VALUES(?,?,?,?)
                ON CONFLICT(job_id,kind) DO UPDATE SET data_json=excluded.data_json,created_at=excluded.created_at""",
                (job_id, kind, canonical_json(redact_mapping(data)), utc_now()),
            )

    def fail_unpublished_completion(self, job_id: str, error: str) -> dict[str, Any]:
        """Fail a verified Job whose required Control Center publication failed.

        This narrow correction is allowed only before a successful publication
        checkpoint exists.  It prevents a terminal Chat card from advertising
        COMPLETE when the authoritative evidence or recording link is absent
        from the operator ledger.
        """
        now = utc_now()
        safe_error = redact_text(error)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            if JobStatus(row["status"]) != JobStatus.COMPLETE:
                raise RuntimeError("only an unpublished COMPLETE Job may be failed")
            published = conn.execute(
                """SELECT 1 FROM checkpoints
                   WHERE job_id=? AND kind='control_center_publication'""",
                (job_id,),
            ).fetchone()
            if published:
                raise RuntimeError("published COMPLETE Jobs are immutable")
            conn.execute(
                """UPDATE jobs SET status=?,last_error=?,completed_at=NULL,
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                (JobStatus.FAILED.value, safe_error, now, job_id),
            )
            conn.execute(
                """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                   VALUES (?, 'control_center_publication_failed', ?, ?)
                   ON CONFLICT(job_id,kind) DO UPDATE SET
                   data_json=excluded.data_json,created_at=excluded.created_at""",
                (
                    job_id,
                    canonical_json({"status": "FAILED", "error": safe_error}),
                    now,
                ),
            )
            return self.get_job(job_id, conn=conn)

    def get_checkpoint(self, job_id: str, kind: str) -> dict[str, Any] | None:
        record = self.get_checkpoint_record(job_id, kind)
        return None if record is None else record["data"]

    def get_checkpoint_record(
        self, job_id: str, kind: str
    ) -> dict[str, Any] | None:
        """Return checkpoint JSON plus the row timestamp. Missing row is None."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT data_json, created_at FROM checkpoints
                   WHERE job_id=? AND kind=?""",
                (job_id, kind),
            ).fetchone()
            if row is None:
                return None
            return {
                "kind": kind,
                "data": json.loads(row["data_json"]),
                "created_at": row["created_at"],
            }

    def list_attempts(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT phase, attempt_number, outcome, detail_json, created_at
                   FROM attempts WHERE job_id=? ORDER BY id""",
                (job_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            result.append(item)
        return result

    def add_attempt(self, job_id: str, phase: str, number: int, outcome: str, detail: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO attempts(job_id,phase,attempt_number,outcome,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                (job_id, phase, number, outcome, canonical_json(redact_mapping(detail)), utc_now()),
            )

    def report_perform_progress(
        self,
        job_id: str,
        data: dict[str, Any] | None = None,
        *,
        source: str = "worker",
    ) -> dict[str, Any]:
        """Record that the worker is still making progress on this Job.

        Lease renewals are not progress. Call this when a page advances,
        ``rows_inspected`` increases, or another durable detail changes.
        The Job Engine uses this row to refresh the perform idle deadline.
        """
        stamp = utc_now()
        incoming = redact_mapping(dict(data or {}))
        existing = self.get_checkpoint(job_id, "perform_progress") or {}
        first_at = str(existing.get("first_at") or "").strip() or stamp
        payload = {
            **incoming,
            "source": str(incoming.get("source") or source),
            "first_at": first_at,
            "last_at": stamp,
        }
        self.checkpoint(job_id, "perform_progress", payload)
        return payload

    def latest_perform_progress_fingerprint(self, job_id: str) -> tuple[Any, ...] | None:
        """Comparable token for perform-progress detection.

        Includes progress checkpoints, the newest attempt, and the newest
        ``playwright_exec`` row. ``jobs.updated_at`` and lease expiry are
        ignored so the lease heartbeat cannot keep a hung worker alive.
        """
        resolved = str(job_id or "").strip()
        if not resolved:
            return None
        parts: list[tuple[Any, ...]] = []
        placeholders = ",".join("?" for _ in PERFORM_PROGRESS_KINDS)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT kind, created_at, data_json FROM checkpoints
                    WHERE job_id=? AND kind IN ({placeholders})
                    ORDER BY kind""",
                (resolved, *PERFORM_PROGRESS_KINDS),
            ).fetchall()
            for row in rows:
                parts.append(
                    ("checkpoint", row["kind"], row["created_at"], row["data_json"])
                )
            attempt = conn.execute(
                """SELECT id, created_at, detail_json FROM attempts
                   WHERE job_id=? ORDER BY id DESC LIMIT 1""",
                (resolved,),
            ).fetchone()
            if attempt is not None:
                parts.append(
                    (
                        "attempt",
                        int(attempt["id"]),
                        attempt["created_at"],
                        attempt["detail_json"],
                    )
                )
            try:
                exec_row = conn.execute(
                    """SELECT id, updated_at, created_at FROM playwright_exec
                       WHERE job_id=? ORDER BY id DESC LIMIT 1""",
                    (resolved,),
                ).fetchone()
            except sqlite3.OperationalError:
                exec_row = None
            if exec_row is not None:
                parts.append(
                    (
                        "playwright_exec",
                        int(exec_row["id"]),
                        exec_row["updated_at"] or exec_row["created_at"],
                    )
                )
        return tuple(parts) if parts else None

    def latest_running_chat_job_id(self) -> str | None:
        """Newest RUNNING generic Chat job. Used when playwright_exec has no env id."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id FROM jobs
                   WHERE status=? AND action_type IN (
                       'hermes.google_chat_task', 'hermes.plain_english'
                   )
                   ORDER BY updated_at DESC LIMIT 1""",
                (JobStatus.RUNNING.value,),
            ).fetchone()
        return str(row["id"]) if row else None

    def add_playwright_exec(
        self,
        job_id: str,
        tool: str,
        status: str,
        *,
        code_preview: str = "",
        result: dict[str, Any] | None = None,
    ) -> int:
        """Append one playwright_exec row and commit immediately."""
        now = utc_now()
        payload = canonical_json(redact_mapping(result or {}))
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO playwright_exec
                   (job_id,tool,status,code_preview,result_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    job_id,
                    tool,
                    status,
                    redact_text(code_preview),
                    payload,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_playwright_exec(
        self,
        row_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        payload = canonical_json(redact_mapping(result or {}))
        with self.transaction() as conn:
            conn.execute(
                """UPDATE playwright_exec
                   SET status=?, result_json=?, updated_at=?
                   WHERE id=?""",
                (status, payload, now, row_id),
            )

    def list_playwright_exec(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id, job_id, tool, status, code_preview, result_json,
                          created_at, updated_at
                   FROM playwright_exec WHERE job_id=? ORDER BY id""",
                (job_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json") or "{}")
            result.append(item)
        return result

    def add_evidence(self, job_id: str, verified: bool, evidence: VerificationEvidence) -> None:
        expected = redact_mapping(evidence.expected)
        observed = redact_mapping(evidence.observed)
        body = canonical_json(
            {"method": evidence.method, "source": evidence.source, "expected": expected,
             "observed": observed, "authoritative": evidence.authoritative,
             "captured_at": evidence.captured_at, "locator": evidence.locator}
        )
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO verification_evidence
                (job_id,verified,method,source,authoritative,expected_json,observed_json,locator,
                 evidence_sha256,captured_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, int(verified), evidence.method, evidence.source, int(evidence.authoritative),
                 canonical_json(expected), canonical_json(observed), evidence.locator,
                 hashlib.sha256(body.encode()).hexdigest(), evidence.captured_at, utc_now()),
            )

    def pause(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if JobStatus(job["status"]) in TERMINAL_STATUSES:
            return job
        resume = JobStatus.VERIFYING if self.get_checkpoint(job_id, "action") else JobStatus.PENDING
        return self.transition(job_id, JobStatus.PAUSED, resume_status=resume, release_lease=True)

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        current = JobStatus(job["status"])
        if current not in WAITING_STATUSES:
            return job
        target = JobStatus(job["resume_status"] or JobStatus.PENDING)
        return self.transition(job_id, target, expected=set(WAITING_STATUSES), release_lease=True)

    def list_evidence(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT job_id,verified,method,source,authoritative,expected_json,
                          observed_json,locator,evidence_sha256,captured_at,created_at
                   FROM verification_evidence WHERE job_id=? ORDER BY id""",
                (job_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["expected"] = json.loads(item.pop("expected_json"))
            item["observed"] = json.loads(item.pop("observed_json"))
            result.append(item)
        return result

    def wake_due(self, now: str | None = None) -> list[str]:
        now = now or utc_now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id,resume_status FROM jobs WHERE status=? AND next_wakeup_at<=?",
                (JobStatus.RETRY_WAIT, now),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET status=?,next_wakeup_at=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?",
                    (row["resume_status"] or JobStatus.PENDING, now, row["id"]),
                )
            return [row["id"] for row in rows]

    def list_pending(self, action_types: set[str] | frozenset[str], limit: int = 25) -> list[str]:
        """Return durable runnable IDs; JobEngine.claim remains the concurrency gate."""
        if not action_types:
            return []
        values = sorted(action_types)
        placeholders = ",".join("?" for _ in values)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT id FROM jobs
                    WHERE status=? AND action_type IN ({placeholders})
                    ORDER BY created_at LIMIT ?""",
                [JobStatus.PENDING.value, *values, limit],
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def list_runnable(
        self,
        action_types: set[str] | frozenset[str],
        limit: int = 25,
    ) -> list[str]:
        """Return executable Jobs that can continue from a durable checkpoint."""
        if not action_types:
            return []
        values = sorted(action_types)
        placeholders = ",".join("?" for _ in values)
        now = utc_now()
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT id FROM jobs
                    WHERE status IN (?,?,?)
                      AND action_type IN ({placeholders})
                      AND (lease_owner IS NULL OR lease_expires_at<=?)
                    ORDER BY created_at LIMIT ?""",
                [
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                    JobStatus.VERIFYING.value,
                    *values,
                    now,
                    limit,
                ],
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def list_jobs_by_status(
        self,
        statuses: Iterable[JobStatus | str],
    ) -> list[dict[str, Any]]:
        """Return full job rows for the given statuses, oldest first."""
        values = sorted(
            {
                item.value if isinstance(item, JobStatus) else str(item)
                for item in statuses
            }
        )
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM jobs
                    WHERE status IN ({placeholders})
                    ORDER BY created_at""",
                values,
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def add_jev_evaluation(
        self,
        job_id: str,
        *,
        verdict: str,
        confidence: int,
        reason: str,
        escalate: bool,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> int:
        """Store one Jev score. The API key must already be absent."""
        request = redact_mapping(dict(request or {}))
        response = redact_mapping(dict(response or {}))
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO jev_evaluations
                   (job_id,verdict,confidence,reason,escalate,request_json,response_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    str(verdict or ""),
                    int(confidence),
                    redact_text(str(reason or "")),
                    int(bool(escalate)),
                    canonical_json(request),
                    canonical_json(response),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_jev_evaluations(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id,job_id,verdict,confidence,reason,escalate,
                          request_json,response_json,created_at
                   FROM jev_evaluations WHERE job_id=? ORDER BY id""",
                (job_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json") or "{}")
            item["response"] = json.loads(item.pop("response_json") or "{}")
            item["escalate"] = bool(item["escalate"])
            result.append(item)
        return result

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result


def report_current_job_perform_progress(
    data: dict[str, Any] | None = None,
    *,
    source: str = "worker",
) -> bool:
    """Best-effort progress heartbeat for the Job Engine-bound job.

    Missing ``ROBIE_JOB_ID`` / ``ROBIE_JOB_DB`` is a no-op so helper
    subprocesses can call this without knowing whether a Job is bound.
    A write failure does not raise: the idle deadline then fails closed.
    """
    job_id = (
        os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("ROBIE_CURRENT_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()
    db_path = str(os.environ.get("ROBIE_JOB_DB") or "").strip()
    if not job_id or not db_path:
        return False
    try:
        JobStore(db_path).report_perform_progress(job_id, data, source=source)
    except Exception:
        logger.exception("perform progress heartbeat failed job=%s", job_id)
        return False
    return True
