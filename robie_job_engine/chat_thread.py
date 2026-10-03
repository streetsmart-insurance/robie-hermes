"""Durable Google Chat thread for one job.

Every outbound Chat message for a job stays in that job's thread.
A user reply inside a thread binds ``thread.name`` onto the job.
The first outbound message of a job that has no thread yet uses a stable
``threadKey`` so Chat opens one thread, and the returned ``thread.name``
is stored for the next send.
"""

from __future__ import annotations

import json
from typing import Any

CHAT_THREAD_KIND = "chat_thread"
REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def thread_resource_name(value: Any) -> str | None:
    """Return a ``spaces/.../threads/...`` name, or None."""
    text = str(value or "").strip()
    if not text.startswith("spaces/"):
        return None
    if "/threads/" not in text or "/messages/" in text:
        return None
    return text


def job_thread_key(job_id: str) -> str:
    """Stable threadKey so every first-send for this job opens the same thread."""
    return f"robie-job-{str(job_id).strip()}"


def job_for_chat_thread(store: Any, thread_name: Any) -> dict[str, Any] | None:
    """The job this thread belongs to, including one that already finished.

    A live job wins when the same thread was bound more than once. The
    lookup is the stored thread name, not the conversation's active link.
    """
    from .models import TERMINAL_STATUSES, JobStatus

    name = thread_resource_name(thread_name)
    if not name:
        return None
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _take(job_id: str) -> None:
        if not job_id or job_id in seen:
            return
        seen.add(job_id)
        try:
            found.append(store.get_job(job_id))
        except Exception:
            return

    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT job_id, data_json FROM checkpoints
                   WHERE kind=?
                   ORDER BY created_at DESC""",
                (CHAT_THREAD_KIND,),
            ).fetchall()
    except Exception:
        rows = []
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if thread_resource_name(data.get("thread_name")) != name:
            continue
        _take(str(row["job_id"] or ""))
    if not found:
        try:
            with store.connect() as conn:
                payload_rows = conn.execute(
                    """SELECT id, payload_json FROM jobs
                       WHERE payload_json LIKE ?
                       ORDER BY updated_at DESC
                       LIMIT 40""",
                    (f"%{name}%",),
                ).fetchall()
        except Exception:
            payload_rows = []
        for row in payload_rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            stored = thread_resource_name(
                payload.get("thread_id") or payload.get("thread_name")
            )
            if stored != name:
                continue
            _take(str(row["id"] or ""))
    if not found:
        return None
    live = [
        job for job in found if JobStatus(job["status"]) not in TERMINAL_STATUSES
    ]
    return live[0] if live else found[0]


def read_job_chat_thread(store: Any, job_id: str) -> str | None:
    if not str(job_id or "").strip():
        return None
    try:
        row = store.get_checkpoint(job_id, CHAT_THREAD_KIND) or {}
    except Exception:
        return None
    return thread_resource_name(row.get("thread_name"))


def bind_job_chat_thread(store: Any, job_id: str, thread_name: Any) -> str | None:
    """Persist the thread a user replied in. Later sends stay there.

    An empty value does not clear a thread already stored. The same name
    is written onto the job payload so HITL and terminal posts can see it.
    """
    name = thread_resource_name(thread_name)
    if not str(job_id or "").strip() or not name:
        return None
    if read_job_chat_thread(store, job_id) != name:
        store.checkpoint(job_id, CHAT_THREAD_KIND, {"thread_name": name})
    try:
        job = store.get_job(job_id)
        payload = dict(job.get("payload") or {})
        if payload.get("thread_id") != name or payload.get("thread_name") != name:
            payload["thread_id"] = name
            payload["thread_name"] = name
            store.update_payload(job_id, payload)
    except Exception:
        # The checkpoint is the source of truth for adapter sends.
        pass
    return name


def remember_created_thread(store: Any, job_id: str, response: dict | None) -> str | None:
    """Store ``thread.name`` from messages.create when the job has none yet.

    A thread the user already bound is kept. A create that falls back to a
    new thread must not pull the job out of the thread they replied in.
    """
    if not str(job_id or "").strip():
        return None
    existing = read_job_chat_thread(store, job_id)
    if existing:
        return existing
    resp = response or {}
    thread = resp.get("thread") if isinstance(resp, dict) else None
    if not isinstance(thread, dict):
        return None
    return bind_job_chat_thread(store, job_id, thread.get("name"))


def outbound_thread_spec(
    *,
    job_id: str | None,
    stored_thread_name: str | None,
    explicit_thread_name: str | None = None,
    prefer_job_thread_key: bool = False,
) -> dict[str, str]:
    """Thread body for ``spaces.messages.create``.

    A job with a stored thread uses ``thread.name``. A job that owns its
    thread and has none yet uses ``threadKey`` so the first message starts
    one thread instead of a new top-level message on every send. An
    explicit thread is used only when this send is not claiming the job
    thread (cron with a requested thread, or a message that is not a job).
    """
    stored = thread_resource_name(stored_thread_name)
    if job_id and stored:
        return {"name": stored}
    if job_id and prefer_job_thread_key:
        return {"threadKey": job_thread_key(job_id)}
    explicit = str(explicit_thread_name or "").strip()
    if explicit:
        return {"name": explicit}
    if job_id:
        return {"threadKey": job_thread_key(job_id)}
    return {}


def create_reply_option(thread_spec: dict | None) -> str | None:
    """Chat ignores thread.name and threadKey unless this option is set."""
    spec = thread_spec or {}
    if spec.get("name") or spec.get("threadKey"):
        return REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD
    return None


def message_continues_job(job: dict | None, message_id: str | None) -> bool:
    if not job:
        return False
    original = str((job.get("payload") or {}).get("message_id") or "").strip()
    current = str(message_id or "").strip()
    return bool(original and current and original != current)


def inbound_thread_to_bind(
    *,
    job: dict | None,
    message_id: str | None,
    session_thread_id: str | None,
    raw_thread_name: str | None,
    reply_in_existing_thread: bool,
) -> str | None:
    """Thread to store for this inbound message, or None.

    Bind when the user replied inside a thread: a later message on the
    job whose session kept a thread, or an inbound the adapter classified
    as a reply in a thread that already had messages. A brand-new
    top-level message does not bind Chat's auto thread. The first
    outbound starts the job thread with threadKey.
    """
    session = thread_resource_name(session_thread_id)
    raw = thread_resource_name(raw_thread_name)
    if message_continues_job(job, message_id):
        # DM main-flow strips the session thread. Do not adopt the new
        # auto thread Chat assigned to that top-level message.
        return session
    if reply_in_existing_thread:
        return session or raw
    return None
