"""Transport-only recovery for already prepared Chat replies.

Commit the entire reply before its first HTTP call. A lease bounds competing
drainers; stable Google request/message IDs also cover server acceptance followed
by a timeout or a crash before the local receipt. Never execute a job here.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .idempotency import assert_durable_path
from .store import JobStore, canonical_json, utc_now

MAX_ATTEMPTS = 5
LEASE_SECONDS = 180
MAX_AGE_SECONDS = 7200


class ChatReplyOutbox:
    def __init__(self, db: str):
        self.store = JobStore(assert_durable_path(db))
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chat_reply_outbox (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    bodies_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    next_chunk INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    lease_token TEXT,
                    lease_until REAL NOT NULL DEFAULT 0,
                    message_name TEXT,
                    last_status INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_chat_reply_due
                    ON chat_reply_outbox(state, next_at);
            """)

    def generation(self, job_id: str) -> str:
        record = self.store.get_checkpoint_record(job_id, "clarification_reply")
        return hashlib.sha256(canonical_json(record).encode()).hexdigest() if record else ""

    def prepare(self, job_id: str, chat_id: str, kind: str,
                bodies: list[dict[str, Any]]) -> str:
        # These are the final, redacted, formatted wire bodies, not worker input.
        if not bodies or len(bodies) > 32 or any(not b.get("text") for b in bodies):
            raise ValueError("invalid or oversized Chat reply")
        if not chat_id.startswith("spaces/"):
            raise ValueError("invalid Chat space")
        for body in bodies:
            thread = body.get("thread") or {}
            if not (thread.get("name") or thread.get("threadKey")):
                raise ValueError("durable reply requires an original thread")
            if thread.get("name") and not thread["name"].startswith(chat_id + "/threads/"):
                raise ValueError("reply thread belongs to another space")
        generation = self.generation(job_id)
        # Exclude resolved thread from identity: threadKey becomes thread.name
        # after the first send. A repeated send must reuse the original envelope.
        identity = canonical_json([job_id, chat_id, kind, generation,
                                   [b["text"] for b in bodies]])
        reply_id = hashlib.sha256(identity.encode()).hexdigest()
        now = time.time()
        with self.store.transaction() as conn:
            conn.execute("""INSERT OR IGNORE INTO chat_reply_outbox
                (id,job_id,chat_id,kind,generation,bodies_json,next_at,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (reply_id, job_id, chat_id, kind, generation,
                 canonical_json(bodies), now, now))
        return reply_id

    def get(self, reply_id: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM chat_reply_outbox WHERE id=?", (reply_id,)).fetchone()
        if row is None:
            raise KeyError(reply_id)
        result = dict(row)
        result["bodies"] = json.loads(result.pop("bodies_json"))
        return result

    def due(self, *, job_id: str | None = None, limit: int = 10) -> list[str]:
        now = time.time()
        with self.store.connect() as conn:
            rows = conn.execute("""SELECT id FROM chat_reply_outbox
                WHERE state IN ('pending','sending') AND next_at<=? AND lease_until<=?
                AND (? IS NULL OR job_id=?) ORDER BY created_at, id LIMIT ?""",
                (now, now, job_id, job_id, limit)).fetchall()
        return [r[0] for r in rows]

    def claim(self, reply_id: str) -> dict[str, Any] | None:
        now = time.time()
        token = uuid.uuid4().hex
        with self.store.transaction() as conn:
            # Expired or exhausted work remains visible, never silently deleted.
            conn.execute("""UPDATE chat_reply_outbox SET state='failed',lease_token=NULL
                WHERE id=? AND state IN ('pending','sending') AND lease_until<=?
                AND (attempts>=? OR created_at<?)""",
                (reply_id, now, MAX_ATTEMPTS, now - MAX_AGE_SECONDS))
            changed = conn.execute("""UPDATE chat_reply_outbox SET state='sending',
                attempts=attempts+1,lease_token=?,lease_until=?
                WHERE id=? AND state IN ('pending','sending') AND next_at<=?
                AND lease_until<=?""", (token, now + LEASE_SECONDS, reply_id, now, now)).rowcount
        return self.get(reply_id) if changed else None

    def cancelled(self, row: dict[str, Any]) -> bool:
        from .chat_turn_control import job_was_explicitly_stopped
        from .models import TERMINAL_STATUSES, JobStatus

        # A finished/restart-failed job cannot accept an answer. Do not revive
        # it or post a stale question; final outcome replies may still recover.
        if row["kind"] == "clarify" and JobStatus(self.store.get_job(row["job_id"])["status"]) in TERMINAL_STATUSES:
            return True
        if row["kind"] not in {"stop", "ceiling", "stop_notice"} and job_was_explicitly_stopped(self.store, row["job_id"]):
            return True
        # A user already answered this turn's question. Never replay it later.
        return self.generation(row["job_id"]) != row["generation"]

    def suppress(self, row: dict[str, Any]) -> None:
        with self.store.transaction() as conn:
            conn.execute("""UPDATE chat_reply_outbox SET state='cancelled',
                lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?""",
                (row["id"], row["lease_token"]))

    def fail(self, row: dict[str, Any], status: int, retryable: bool) -> None:
        state = "pending" if retryable and row["attempts"] < MAX_ATTEMPTS else "failed"
        with self.store.transaction() as conn:
            conn.execute("""UPDATE chat_reply_outbox SET state=?,next_at=?,last_status=?,
                lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?""",
                (state, time.time() + min(30 * 2 ** (row["attempts"] - 1), 300),
                 status, row["id"], row["lease_token"]))

    def delivered_chunk(self, row: dict[str, Any], index: int, message_name: str) -> bool:
        if not message_name:
            raise ValueError("missing Chat delivery receipt")
        final = index + 1 == len(row["bodies"])
        with self.store.transaction() as conn:
            changed = conn.execute("""UPDATE chat_reply_outbox SET next_chunk=?,
                message_name=?,state=?,lease_until=?,lease_token=?
                WHERE id=? AND lease_token=? AND next_chunk=?""",
                (index + 1, message_name, "delivered" if final else "sending",
                 0 if final else time.time() + LEASE_SECONDS,
                 None if final else row["lease_token"], row["id"], row["lease_token"], index)).rowcount
            if changed and final:
                receipt = {"message_id": message_name, "reply_id": row["id"],
                           "kind": row["kind"], "posted": True}
                conn.execute("""INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                    VALUES (?,'chat_delivery',?,?) ON CONFLICT(job_id,kind) DO UPDATE SET
                    data_json=excluded.data_json,created_at=excluded.created_at""",
                    (row["job_id"], canonical_json(receipt), utc_now()))
                failure = conn.execute("""SELECT data_json FROM checkpoints
                    WHERE job_id=? AND kind='chat_delivery_failed'""", (row["job_id"],)).fetchone()
                if failure:
                    data = json.loads(failure[0])
                    if data.get("reply_id") == row["id"]:
                        data.update(posted=True, message_id=message_name)
                        conn.execute("""UPDATE checkpoints SET data_json=?,created_at=?
                            WHERE job_id=? AND kind='chat_delivery_failed'""",
                            (canonical_json(data), utc_now(), row["job_id"]))
        return bool(changed)

    @staticmethod
    def request_id(row: dict[str, Any], index: int) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"robie-chat:{row['id']}:{index}"))
