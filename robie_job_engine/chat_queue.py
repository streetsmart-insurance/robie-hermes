"""Durable Google Chat event handoff queue.

The gateway may acknowledge an inbound event only after this queue has
committed the normalized executable request.  Workers claim rows with a
lease, so a gateway or worker restart cannot lose an acknowledged request.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .idempotency import assert_durable_path


class ChatEventConflict(RuntimeError):
    """The same Google event identifier was reused for different work."""


class ConversationJobLinkConflict(RuntimeError):
    """A Chat event was rebound to a different durable Job."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class DurableChatEventQueue:
    """SQLite-backed queue and idempotency ledger for executable Chat work."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = assert_durable_path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_event_queue (
                    event_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    job_id TEXT,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'QUEUED','INFLIGHT','AWAITING_HUMAN_INPUT',
                            'COMPLETE','FAILED'
                        )
                    ),
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_job_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    relation TEXT NOT NULL CHECK (
                        relation IN (
                            'CREATED','CONTINUATION','CORRECTION',
                            'APPROVAL','STATUS'
                        )
                    ),
                    pending_decision_id TEXT,
                    interaction_state_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            link_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(conversation_job_links)"
                )
            }
            queue_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(chat_event_queue)")
            }
            if "max_attempts" not in queue_columns:
                conn.execute(
                    "ALTER TABLE chat_event_queue ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3"
                )
            if "available_at" not in queue_columns:
                conn.execute("ALTER TABLE chat_event_queue ADD COLUMN available_at TEXT")
                conn.execute(
                    """UPDATE chat_event_queue SET available_at=COALESCE(updated_at,created_at)
                       WHERE available_at IS NULL"""
                )
            if "pending_decision_id" not in link_columns:
                conn.execute(
                    "ALTER TABLE conversation_job_links ADD COLUMN pending_decision_id TEXT"
                )
            if "interaction_state_json" not in link_columns:
                conn.execute(
                    """ALTER TABLE conversation_job_links
                       ADD COLUMN interaction_state_json TEXT NOT NULL DEFAULT '{}'"""
                )
            queue_sql = str(
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_event_queue'"
                ).fetchone()[0]
            )
            if "AWAITING_HUMAN_INPUT" not in queue_sql:
                # SQLite cannot alter a CHECK constraint in place. Rebuild the
                # queue losslessly so older durable databases gain the HITL
                # state without discarding an acknowledged event or lease.
                conn.execute("DROP INDEX IF EXISTS idx_chat_event_queue_runnable")
                conn.execute(
                    "ALTER TABLE chat_event_queue RENAME TO chat_event_queue_before_hitl"
                )
                conn.execute(
                    """
                    CREATE TABLE chat_event_queue (
                        event_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        job_id TEXT,
                        state TEXT NOT NULL CHECK (
                            state IN (
                                'QUEUED','INFLIGHT','AWAITING_HUMAN_INPUT',
                                'COMPLETE','FAILED'
                            )
                        ),
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        available_at TEXT NOT NULL,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """INSERT INTO chat_event_queue
                       SELECT event_id,conversation_id,message_id,payload_json,
                              payload_sha256,job_id,state,lease_owner,
                              lease_expires_at,attempt_count,max_attempts,
                              available_at,last_error,created_at,updated_at
                       FROM chat_event_queue_before_hitl"""
                )
                conn.execute("DROP TABLE chat_event_queue_before_hitl")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_job_links_active
                ON conversation_job_links(conversation_id, active, created_at DESC)
                """
            )
            expected_queue_index = [
                "state", "available_at", "lease_expires_at", "created_at"
            ]
            current_queue_index = [
                row[2]
                for row in conn.execute(
                    "PRAGMA index_info(idx_chat_event_queue_runnable)"
                )
            ]
            if current_queue_index != expected_queue_index:
                conn.execute("DROP INDEX IF EXISTS idx_chat_event_queue_runnable")
                conn.execute(
                    """CREATE INDEX idx_chat_event_queue_runnable
                       ON chat_event_queue(
                           state,available_at,lease_expires_at,created_at
                       )"""
                )
            # One-time, lossless migration from the former context lookup.
            # Synthetic event identifiers preserve auditability while making
            # conversation_job_links the sole active-Job lookup thereafter.
            has_legacy_context = conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='conversation_contexts'"""
            ).fetchone()
            if has_legacy_context:
                conn.execute(
                    """INSERT OR IGNORE INTO conversation_job_links
                       (conversation_id,job_id,message_id,event_id,relation,
                        pending_decision_id,interaction_state_json,
                        active,created_at,updated_at)
                       SELECT conversation_id,active_job_id,
                              'migration:' || conversation_id || ':' || active_job_id,
                              'migration:' || conversation_id || ':' || active_job_id,
                              'CONTINUATION',NULL,'{}',1,
                              legacy.updated_at,legacy.updated_at
                       FROM conversation_contexts legacy
                       WHERE legacy.context_state='ACTIVE'
                         AND legacy.active_job_id IS NOT NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM conversation_job_links links
                             WHERE links.conversation_id=legacy.conversation_id
                               AND links.active=1
                         )""",
                )

    def enqueue(
        self,
        *,
        event_id: str,
        conversation_id: str,
        message_id: str,
        payload: dict[str, Any],
        job_id: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Commit one normalized event and return the canonical queue row.

        Google may redeliver an event.  An exact duplicate returns the
        original row; reusing an event ID for different content fails closed.
        """

        if not event_id.strip() or not conversation_id.strip() or not message_id.strip():
            raise ValueError("event_id, conversation_id, and message_id are required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        payload_json = _canonical(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = _stamp()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM chat_event_queue WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != payload_hash:
                    conn.rollback()
                    raise ChatEventConflict(
                        f"event {event_id!r} was redelivered with different content"
                    )
                conn.commit()
                result = dict(existing)
                result["duplicate"] = True
                result["payload"] = json.loads(result.pop("payload_json"))
                return result
            conn.execute(
                """
                INSERT INTO chat_event_queue (
                    event_id,conversation_id,message_id,payload_json,
                    payload_sha256,job_id,state,max_attempts,available_at,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?, 'QUEUED',?,?,?,?)
                """,
                (
                    event_id,
                    conversation_id,
                    message_id,
                    payload_json,
                    payload_hash,
                    job_id,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM chat_event_queue WHERE event_id=?", (event_id,)
            ).fetchone()
            conn.commit()
        result = dict(row)
        result["duplicate"] = False
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def bind_job(self, event_id: str, job_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT job_id FROM chat_event_queue WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(event_id)
            if row["job_id"] not in (None, job_id):
                conn.rollback()
                raise ChatEventConflict(
                    f"event {event_id!r} is already bound to another job"
                )
            conn.execute(
                "UPDATE chat_event_queue SET job_id=?,updated_at=? WHERE event_id=?",
                (job_id, _stamp(), event_id),
            )
            conn.commit()
        return self.get(event_id)

    def link_conversation_job(
        self,
        *,
        conversation_id: str,
        job_id: str,
        message_id: str,
        event_id: str,
        relation: str = "CREATED",
        pending_decision_id: str | None = None,
        interaction_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist an append-only Chat-to-Job correlation.

        One Google event may identify only one Job. Exact redelivery is
        idempotent; an attempted rebind fails closed. Older links remain as
        audit history but cease to be the active link for the conversation.
        """

        relation = relation.upper().strip()
        allowed = {"CREATED", "CONTINUATION", "CORRECTION", "APPROVAL", "STATUS"}
        if relation not in allowed:
            raise ValueError(f"unsupported conversation Job relation: {relation}")
        if not all(item.strip() for item in (conversation_id, job_id, message_id, event_id)):
            raise ValueError("conversation_id, job_id, message_id, and event_id are required")
        now = _stamp()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM conversation_job_links WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                expected = (
                    conversation_id, job_id, message_id, relation,
                    (pending_decision_id or None),
                    _canonical(interaction_state or {}),
                )
                actual = (
                    existing["conversation_id"], existing["job_id"],
                    existing["message_id"], existing["relation"],
                    existing["pending_decision_id"],
                    existing["interaction_state_json"],
                )
                if actual != expected:
                    conn.rollback()
                    raise ConversationJobLinkConflict(
                        f"event {event_id!r} is already linked to different work"
                    )
                conn.commit()
                result = self._decode_link(existing)
                result["duplicate"] = True
                return result
            conn.execute(
                """
                UPDATE conversation_job_links SET active=0,updated_at=?
                WHERE conversation_id=? AND active=1 AND job_id<>?
                """,
                (now, conversation_id, job_id),
            )
            conn.execute(
                """
                INSERT INTO conversation_job_links (
                    conversation_id,job_id,message_id,event_id,relation,
                    pending_decision_id,interaction_state_json,
                    active,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,1,?,?)
                """,
                (
                    conversation_id, job_id, message_id, event_id, relation,
                    (pending_decision_id or None), _canonical(interaction_state or {}),
                    now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM conversation_job_links WHERE event_id=?", (event_id,)
            ).fetchone()
            conn.commit()
        result = self._decode_link(row)
        result["duplicate"] = False
        return result

    def active_conversation_job(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM conversation_job_links
                WHERE conversation_id=? AND active=1
                ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return self._decode_link(row) if row is not None else None

    def conversation_job_for_event(self, event_id: str) -> dict[str, Any] | None:
        """Resolve an inbound/reply message to its durable Job correlation."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_job_links WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._decode_link(row) if row is not None else None

    def bind_pending_approval(
        self,
        *,
        conversation_id: str,
        job_id: str,
        message_id: str,
        event_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        """Persist the exact pending operation with the active conversation."""
        with self._connect() as conn:
            decision = conn.execute(
                "SELECT job_id,status FROM decisions WHERE id=?", (decision_id,)
            ).fetchone()
        if decision is None or decision["job_id"] != job_id:
            raise ConversationJobLinkConflict(
                "pending decision does not belong to the linked Job"
            )
        if decision["status"] != "PENDING":
            raise ConversationJobLinkConflict("linked decision is not pending")
        return self.link_conversation_job(
            conversation_id=conversation_id,
            job_id=job_id,
            message_id=message_id,
            event_id=event_id,
            relation="APPROVAL",
            pending_decision_id=decision_id,
            interaction_state={"awaiting": "approval"},
        )

    def clear_pending_approval(self, decision_id: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                """UPDATE conversation_job_links
                   SET pending_decision_id=NULL,interaction_state_json='{}',updated_at=?
                   WHERE pending_decision_id=?""",
                (_stamp(), decision_id),
            ).rowcount

    def deactivate_conversation(self, conversation_id: str) -> int:
        """Clear the active correlation without deleting its audit history."""
        with self._connect() as conn:
            changed = conn.execute(
                """
                UPDATE conversation_job_links SET active=0,updated_at=?
                WHERE conversation_id=? AND active=1
                """,
                (_stamp(), conversation_id),
            ).rowcount
        return changed

    def expire_inactive_conversations(
        self,
        *,
        inactivity_minutes: int,
        now: datetime | None = None,
    ) -> list[str]:
        """Deactivate stale conversational links without losing audit history.

        Approval links remain active until their own durable decision expires;
        an inactivity sweep must never detach a still-pending operation.
        """
        if inactivity_minutes < 1:
            raise ValueError("inactivity_minutes must be positive")
        cutoff = (now or _now()) - timedelta(minutes=inactivity_minutes)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT event_id,job_id FROM conversation_job_links
                   WHERE active=1 AND pending_decision_id IS NULL AND updated_at<=?""",
                (_stamp(cutoff),),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            if event_ids:
                conn.executemany(
                    """UPDATE conversation_job_links
                       SET active=0,updated_at=? WHERE event_id=?""",
                    [(_stamp(now), event_id) for event_id in event_ids],
                )
            conn.commit()
        if event_ids:
            # Pausing preserves the exact checkpoint for an explicit future
            # continuation while preventing stale unattended execution.
            from .models import TERMINAL_STATUSES, WAITING_STATUSES, JobStatus
            from .store import JobStore

            jobs = JobStore(self.path)
            for row in rows:
                job = jobs.get_job(str(row["job_id"]))
                status = JobStatus(job["status"])
                if status not in TERMINAL_STATUSES and status not in WAITING_STATUSES:
                    jobs.pause(job["id"])
                    jobs.checkpoint(
                        job["id"],
                        "context_archive",
                        {
                            "reason": "conversation inactivity expiration",
                            "conversation_event_id": row["event_id"],
                        },
                    )
        return event_ids

    def claim_next(self, owner: str, *, lease_seconds: int = 120) -> dict[str, Any] | None:
        if not owner.strip():
            raise ValueError("owner is required")
        now = _now()
        expires = now + timedelta(seconds=max(1, lease_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT event_id FROM chat_event_queue
                WHERE state='QUEUED'
                      AND available_at <= ?
                   OR (state='INFLIGHT' AND lease_expires_at < ?)
                ORDER BY created_at,event_id
                LIMIT 1
                """,
                (_stamp(now), _stamp(now)),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            event_id = row["event_id"]
            conn.execute(
                """
                UPDATE chat_event_queue
                SET state='INFLIGHT',lease_owner=?,lease_expires_at=?,
                    attempt_count=attempt_count+1,updated_at=?
                WHERE event_id=?
                """,
                (owner, _stamp(expires), _stamp(now), event_id),
            )
            claimed = conn.execute(
                "SELECT * FROM chat_event_queue WHERE event_id=?", (event_id,)
            ).fetchone()
            conn.commit()
        return self._decode(claimed)

    def renew_lease(
        self, event_id: str, owner: str, *, lease_seconds: int = 120
    ) -> dict[str, Any]:
        """Heartbeat one in-flight row without changing its attempt count."""
        now = _now()
        expires = now + timedelta(seconds=max(1, lease_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE chat_event_queue
                   SET lease_expires_at=?,updated_at=?
                   WHERE event_id=? AND state='INFLIGHT' AND lease_owner=?""",
                (_stamp(expires), _stamp(now), event_id, owner),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise RuntimeError("queue lease is missing or owned by another worker")
            conn.commit()
        return self.get(event_id)

    def complete(self, event_id: str, owner: str) -> dict[str, Any]:
        return self._finish(event_id, owner, "COMPLETE", None)

    def fail(self, event_id: str, owner: str, error: str) -> dict[str, Any]:
        return self._finish(event_id, owner, "FAILED", error)

    def defer(
        self,
        event_id: str,
        owner: str,
        *,
        available_at: str,
        error: str,
    ) -> dict[str, Any]:
        """Release an owned attempt back to the queue for bounded recovery."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE chat_event_queue
                   SET state='QUEUED',lease_owner=NULL,lease_expires_at=NULL,
                       available_at=?,last_error=?,updated_at=?
                   WHERE event_id=? AND state='INFLIGHT' AND lease_owner=?
                     AND attempt_count < max_attempts""",
                (available_at, error, _stamp(), event_id, owner),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise RuntimeError("queue retry is exhausted or lease ownership was lost")
            conn.commit()
        return self.get(event_id)

    def await_human_input(
        self,
        event_id: str,
        owner: str,
        *,
        interaction_state: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        """Park an owned event and persist its exact resumable Chat context."""
        if interaction_state.get("awaiting") != "human_input":
            raise ValueError("human-input interaction state is required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT conversation_id,job_id FROM chat_event_queue WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None or not row["job_id"]:
                conn.rollback()
                raise RuntimeError("queued event has no durable Job binding")
            changed = conn.execute(
                """UPDATE chat_event_queue
                   SET state='AWAITING_HUMAN_INPUT',lease_owner=NULL,
                       lease_expires_at=NULL,last_error=?,updated_at=?
                   WHERE event_id=? AND state='INFLIGHT' AND lease_owner=?""",
                (error, _stamp(), event_id, owner),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise RuntimeError("queue lease is missing or owned by another worker")
            linked = conn.execute(
                """UPDATE conversation_job_links
                   SET interaction_state_json=?,active=1,updated_at=?
                   WHERE conversation_id=? AND job_id=? AND active=1""",
                (
                    _canonical(interaction_state),
                    _stamp(),
                    row["conversation_id"],
                    row["job_id"],
                ),
            ).rowcount
            if linked < 1:
                conn.rollback()
                raise RuntimeError("active conversation Job link is missing")
            conn.commit()
        return self.get(event_id)

    def park_direct_human_input(
        self,
        *,
        conversation_id: str,
        job_id: str,
        interaction_state: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        """Atomically park a synchronous Hermes Chat Job for a human reply."""
        if interaction_state.get("awaiting") != "human_input":
            raise ValueError("human-input interaction state is required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            link = conn.execute(
                """SELECT id FROM conversation_job_links
                   WHERE conversation_id=? AND job_id=? AND active=1
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (conversation_id, job_id),
            ).fetchone()
            job = conn.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if link is None or job is None:
                conn.rollback()
                raise RuntimeError("active direct human-input correlation is missing")
            current = str(job["status"])
            if current not in {"PENDING", "RUNNING", "VERIFYING"}:
                conn.rollback()
                raise RuntimeError("direct Job cannot be parked from its current state")
            resume_status = "PENDING" if current == "PENDING" else "RUNNING"
            now = _stamp()
            conn.execute(
                """UPDATE jobs SET status='AWAITING_HUMAN_INPUT',resume_status=?,
                   last_error=?,next_wakeup_at=NULL,lease_owner=NULL,
                   lease_expires_at=NULL,updated_at=? WHERE id=?""",
                (resume_status, error, now, job_id),
            )
            conn.execute(
                """UPDATE conversation_job_links
                   SET interaction_state_json=?,active=1,updated_at=? WHERE id=?""",
                (_canonical(interaction_state), now, link["id"]),
            )
            conn.execute(
                """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                   VALUES (?, 'human_input_wait', ?, ?)
                   ON CONFLICT(job_id,kind) DO UPDATE SET
                   data_json=excluded.data_json,created_at=excluded.created_at""",
                (
                    job_id,
                    _canonical(
                        {
                            "checkpoint": interaction_state.get("checkpoint"),
                            "field_name": interaction_state.get("field_name"),
                        }
                    ),
                    now,
                ),
            )
            conn.commit()
        return self.active_conversation_job(conversation_id) or {}

    def resume_human_input(
        self,
        *,
        conversation_id: str,
        job_id: str,
        reply_message_id: str,
        field_name: str,
        value: str,
    ) -> dict[str, Any]:
        """Atomically apply a bound reply and release its parked queue event."""
        clean_field = str(field_name or "").strip()
        clean_value = str(value or "").strip()
        if not clean_field or not clean_value:
            raise ValueError("field name and reply value are required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            link = conn.execute(
                """SELECT * FROM conversation_job_links
                   WHERE conversation_id=? AND job_id=? AND active=1
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (conversation_id, job_id),
            ).fetchone()
            if link is None:
                conn.rollback()
                raise RuntimeError("active human-input correlation is missing")
            state = json.loads(link["interaction_state_json"] or "{}")
            if state.get("awaiting") != "human_input":
                conn.rollback()
                raise RuntimeError("conversation is not awaiting human input")
            event = conn.execute(
                """SELECT event_id FROM chat_event_queue
                   WHERE conversation_id=? AND job_id=?
                     AND state='AWAITING_HUMAN_INPUT'
                   ORDER BY created_at DESC LIMIT 1""",
                (conversation_id, job_id),
            ).fetchone()
            job = conn.execute(
                "SELECT payload_json,status,resume_status FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job is None or job["status"] != "AWAITING_HUMAN_INPUT":
                conn.rollback()
                raise RuntimeError("bound Job is not awaiting human input")
            payload = json.loads(job["payload_json"] or "{}")
            values = dict(payload.get("human_input_values") or {})
            values[clean_field] = clean_value
            payload["human_input_values"] = values
            now = _stamp()
            value_hash = hashlib.sha256(clean_value.encode("utf-8")).hexdigest()
            conn.execute(
                """UPDATE jobs SET payload_json=?,status=?,resume_status=NULL,
                   last_error=NULL,next_wakeup_at=NULL,lease_owner=NULL,
                   lease_expires_at=NULL,completed_at=NULL,updated_at=? WHERE id=?""",
                (
                    _canonical(payload),
                    job["resume_status"] or "PENDING",
                    now,
                    job_id,
                ),
            )
            conn.execute(
                """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                   VALUES (?, 'human_input_resume', ?, ?)
                   ON CONFLICT(job_id,kind) DO UPDATE SET
                   data_json=excluded.data_json,created_at=excluded.created_at""",
                (
                    job_id,
                    _canonical(
                        {
                            "field_name": clean_field,
                            "value_sha256": value_hash,
                            "reply_message_id": reply_message_id,
                        }
                    ),
                    now,
                ),
            )
            if event is not None:
                changed = conn.execute(
                    """UPDATE chat_event_queue
                       SET state='QUEUED',available_at=?,last_error=NULL,updated_at=?
                       WHERE event_id=? AND state='AWAITING_HUMAN_INPUT'""",
                    (now, now, event["event_id"]),
                ).rowcount
                if changed != 1:
                    conn.rollback()
                    raise RuntimeError("parked queue event changed during resume")
            state.update(
                {
                    "awaiting": None,
                    "resumed_by_message_id": reply_message_id,
                    "resumed_at": now,
                    "resume_mode": "queue" if event is not None else "direct",
                }
            )
            conn.execute(
                """UPDATE conversation_job_links
                   SET interaction_state_json=?,updated_at=? WHERE id=?""",
                (_canonical(state), now, link["id"]),
            )
            conn.commit()
        if event is None:
            result = {
                "event_id": None,
                "job_id": job_id,
                "state": "DIRECT_RESUME",
            }
        else:
            result = self.get(str(event["event_id"]))
        # Chat "Resuming from the saved checkpoint" is not a claim.
        # Generic hermes.google_chat_task rows stay unleased; reopen
        # writes gateway_progress so the 300s orphan watcher does not
        # fail the Job as unclaimed (6cf6f6ae, da53765b).
        self._reopen_resumed_generic_chat_job(job_id)
        return result

    def _reopen_resumed_generic_chat_job(self, job_id: str) -> None:
        from .chat_guard import reopen_resumed_generic_chat_job

        reopen_resumed_generic_chat_job(str(self.path), job_id)

    def _finish(
        self, event_id: str, owner: str, state: str, error: str | None
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE chat_event_queue
                SET state=?,lease_owner=NULL,lease_expires_at=NULL,
                    last_error=?,updated_at=?
                WHERE event_id=? AND state='INFLIGHT' AND lease_owner=?
                """,
                (state, error, _stamp(), event_id, owner),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise RuntimeError("queue lease is missing or owned by another worker")
            conn.commit()
        return self.get(event_id)

    def get(self, event_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_event_queue WHERE event_id=?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._decode(row)

    @staticmethod
    def worker_id(prefix: str = "chat-worker") -> str:
        return f"{prefix}:{uuid.uuid4()}"

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    @staticmethod
    def _decode_link(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["interaction_state"] = json.loads(
            result.pop("interaction_state_json") or "{}"
        )
        return result
