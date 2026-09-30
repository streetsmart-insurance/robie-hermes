"""Chat job controls for duplicate notes, waiting replies, and dead runs.

A discussion-note write posts at most once per plan step. A clarify question
parks the job so the next reply in that thread answers it. A waiting job
does not swallow an unrelated message. A running job with no live worker
is failed and its recording is stopped.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import JobStatus

# The Chat turn ceiling is 10 minutes. A clarify older than that is stale.
WAITING_EXPIRE_SECONDS = 600
# Fallback bind when the message has no thread: only a fresh waiting job.
WAITING_BIND_MAX_SECONDS = 900
# A RUNNING row with no lease and no heartbeat is dead after this long.
DEAD_RUNNING_AFTER_SECONDS = 600

WAITING_EXPIRED_NOTE = (
    "That question expired. Send it again if you still want an answer."
)
REPEAT_NOTE_REFUSAL = (
    "Stop. This note was already posted for this step. Do not post it again."
)
CANCEL_POLICY_REFUSAL = "I can't cancel a policy. A person has to do that."

_NOTE_POSTED = frozenset({"filed", "posted, verifying"})
_FRESH_TASK = re.compile(
    r"\b(?:essay|add a note|file a note|post a note|cancel|quote|"
    r"policy setup|3000-word|thousand-word|write a |write me )\b",
    re.IGNORECASE,
)
_HARD_CANCEL = re.compile(
    r"\bcancel(?:lation|led|ling)?\b",
    re.IGNORECASE,
)
_POLICY_WORD = re.compile(r"\bpolic", re.IGNORECASE)
_MISSING_FIELD = re.compile(
    r"MISSING_REQUIRED_FIELD\s*:\s*([^\n]+)",
    re.IGNORECASE,
)


def hard_block_reply(text: str) -> str | None:
    """One-line refusal for a destructive policy cancel. None otherwise.

    A question about cancellation is not a cancel request. Renew and
    endorse stay on the existing clarify hold.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    from .answer_only import is_informational_ask

    if is_informational_ask(raw):
        return None
    if _HARD_CANCEL.search(raw) and _POLICY_WORD.search(raw):
        return CANCEL_POLICY_REFUSAL
    return None


def job_is_hard_blocked(store: Any, job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    job_id = str(job.get("id") or "")
    if job_id and store is not None:
        try:
            if store.get_checkpoint(job_id, "hard_block"):
                return True
        except Exception:
            pass
    payload = dict(job.get("payload") or {})
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("text", "request_text", "prompt")
    )
    return hard_block_reply(text) is not None


def plain_missing_field_question(text: str) -> str:
    """Turn a worker field marker into one plain question."""
    raw = str(text or "")
    match = _MISSING_FIELD.search(raw)
    if not match:
        return raw.strip()
    field = " ".join(match.group(1).split()).strip(" .:")
    if not field:
        field = "details"
    question = f"I need the {field} before I can do that."
    replaced = _MISSING_FIELD.sub(question, raw)
    return " ".join(replaced.split()).strip()


def outbound_is_clarify(raw: str, cleaned: str = "") -> bool:
    """True when this outbound text is a question for the user."""
    source = str(raw or "")
    body = " ".join(str(cleaned or source).split())
    if _MISSING_FIELD.search(source) or _MISSING_FIELD.search(body):
        return True
    folded = body.casefold()
    if folded.startswith("i need the ") and "before i can do that" in folded:
        return True
    if not body.endswith("?"):
        return False
    words = body.split()
    if not words or len(words) > 40:
        return False
    if folded.startswith("nothing was changed") or folded.startswith("stopped."):
        return False
    return True


def mark_job_waiting_for_user(store: Any, job_id: str, question: str = "") -> bool:
    """Park a live job so the next message answers it instead of busy-defer."""
    if not job_id or store is None:
        return False
    try:
        job = store.get_job(job_id)
    except Exception:
        return False
    status = JobStatus(job["status"])
    if status in {
        JobStatus.COMPLETE,
        JobStatus.UNVERIFIED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }:
        return False
    prompt = str(question or "").strip()[:500]
    if status in {JobStatus.NEEDS_CLARIFICATION, JobStatus.AWAITING_HUMAN_INPUT}:
        store.checkpoint(job_id, "clarification", {"question": prompt, "asked": True})
        store.checkpoint(
            job_id, "keep_chat_context", {"reason": "needs_clarification"}
        )
        return True
    if status not in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.VERIFYING}:
        return False
    store.transition(
        job_id,
        JobStatus.NEEDS_CLARIFICATION,
        expected={status},
        error="waiting on the user",
        resume_status=JobStatus.PENDING,
        release_lease=True,
    )
    store.checkpoint(job_id, "clarification", {"question": prompt, "asked": True})
    store.checkpoint(job_id, "keep_chat_context", {"reason": "needs_clarification"})
    return True


def _parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def job_age_seconds(job: dict[str, Any] | None, now: datetime | None = None) -> float:
    stamp = _parse_stamp((job or {}).get("updated_at"))
    if stamp is None:
        return 0.0
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return (clock - stamp).total_seconds()


def list_jobs_in_status(store: Any, statuses: set[str]) -> list[dict[str, Any]]:
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    with store.connect() as conn:
        rows = conn.execute(
            f"""SELECT id FROM jobs WHERE status IN ({placeholders})
                ORDER BY updated_at DESC""",
            tuple(statuses),
        ).fetchall()
    found: list[dict[str, Any]] = []
    for row in rows:
        try:
            found.append(store.get_job(str(row["id"])))
        except Exception:
            continue
    return found


def plausibly_answers_waiting_question(text: str) -> bool:
    """A short reply can answer a clarify. A new task cannot."""
    body = " ".join(str(text or "").split())
    if not body or hard_block_reply(body):
        return False
    if _FRESH_TASK.search(body):
        return False
    from .request_routing import classify_request

    classified = classify_request(body)
    if str(classified.action_type).startswith("ezlynx."):
        return False
    return len(body.split()) <= 12


_EXPLICIT_YES = re.compile(
    r"^(?:yes|yeah|yep|yup|yes please|add it again|yes[, ]+add it again|"
    r"please add it again)\.?$",
    re.IGNORECASE,
)


def explicit_note_repost(text: str) -> bool:
    """True only for a direct yes to posting the same note again."""
    body = " ".join(str(text or "").split())
    return bool(body) and _EXPLICIT_YES.match(body) is not None


def note_repost_confirmed_by_reply(store: Any, job_id: str, text: str) -> bool:
    """Remember an explicit yes so the next note post is allowed once."""
    if not job_id or store is None or not explicit_note_repost(text):
        return False
    try:
        note = store.get_checkpoint(job_id, "discussion_note") or {}
    except Exception:
        return False
    if str(note.get("status") or "") != "already_posted":
        return False
    store.checkpoint(job_id, "note_repost_confirmed", {"text": " ".join(str(text).split())})
    return True


def should_bind_waiting_reply(
    store: Any,
    job: dict[str, Any] | None,
    text: str,
    inbound_thread_id: str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Bind only a reply inside this job's thread.

    A top-level message never answers a waiting job. It starts a new job.
    """
    if not job or str(job.get("status") or "") != JobStatus.NEEDS_CLARIFICATION.value:
        return False
    if hard_block_reply(text):
        return False
    from .chat_thread import read_job_chat_thread, thread_resource_name

    inbound = thread_resource_name(inbound_thread_id)
    if not inbound:
        return False
    stored = read_job_chat_thread(store, str(job.get("id") or ""))
    if not stored or stored != inbound:
        return False
    clock = now or datetime.now(timezone.utc)
    return job_age_seconds(job, clock) <= WAITING_EXPIRE_SECONDS


def waiting_job_to_bind(
    store: Any,
    text: str,
    inbound_thread_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The clarify job this in-thread reply answers, if the bind rules allow it."""
    from .chat_thread import read_job_chat_thread, thread_resource_name

    inbound = thread_resource_name(inbound_thread_id)
    if not inbound:
        return None
    waiting = list_jobs_in_status(store, {JobStatus.NEEDS_CLARIFICATION.value})
    for job in waiting:
        stored = read_job_chat_thread(store, str(job.get("id") or ""))
        if stored and stored == inbound and should_bind_waiting_reply(
            store, job, text, inbound_thread_id, now=now
        ):
            return job
    return None


def expire_stale_waiting_jobs(
    store: Any,
    *,
    older_than_seconds: int = WAITING_EXPIRE_SECONDS,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Fail clarify jobs older than the 10-minute ceiling. One line each."""
    if older_than_seconds < 1:
        raise ValueError("waiting expire window must be positive")
    clock = now or datetime.now(timezone.utc)
    expired: list[dict[str, str]] = []
    for job in list_jobs_in_status(store, {JobStatus.NEEDS_CLARIFICATION.value}):
        if job_age_seconds(job, clock) <= older_than_seconds:
            continue
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        try:
            store.transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.NEEDS_CLARIFICATION},
                error=WAITING_EXPIRED_NOTE,
                release_lease=True,
            )
        except Exception:
            continue
        store.checkpoint(
            job_id,
            "waiting_expired",
            {"reply": WAITING_EXPIRED_NOTE},
        )
        from .chat_thread import read_job_chat_thread

        payload = dict(job.get("payload") or {})
        conversation_id = str(payload.get("conversation_id") or "").strip()
        expired.append(
            {
                "id": job_id,
                "reply": WAITING_EXPIRED_NOTE,
                "thread_id": read_job_chat_thread(store, job_id) or "",
                "conversation_id": conversation_id,
            }
        )
    return expired


def waiting_job_to_cancel(db_path: str, conversation_id: str) -> str | None:
    """The clarify job /stop should cancel when nothing else is running."""
    if not db_path or not conversation_id:
        return None
    from .chat_queue import DurableChatEventQueue
    from .chat_turn_control import job_is_waiting_on_user
    from .store import JobStore

    store = JobStore(db_path)
    current = DurableChatEventQueue(db_path).active_conversation_job(conversation_id)
    job_id = str((current or {}).get("job_id") or "")
    if not job_id:
        return None
    try:
        job = store.get_job(job_id)
    except Exception:
        return None
    if job_is_waiting_on_user(job):
        return job_id
    return None


def discussion_note_step_id(store: Any, job_id: str, args: dict[str, Any] | None) -> str:
    """One id per locked plan, otherwise the applicant, title, and note text."""
    plan: dict[str, Any] = {}
    if store is not None and job_id:
        try:
            plan = dict(store.get_checkpoint(job_id, "write_plan") or {})
        except Exception:
            plan = {}
    if plan.get("locked"):
        target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
        return "plan:" + str(plan.get("write") or "") + "|" + repr(sorted(target.items()))
    values = dict(args or {})
    applicant = str(values.get("applicant_id") or "").strip()
    title = " ".join(str(values.get("title_hint") or "").casefold().split())
    note = " ".join(str(values.get("note_text") or "").casefold().split())
    return f"text:{applicant}|{title}|{note}"


def _same_utc_day(stamp: Any, now: datetime | None = None) -> bool:
    parsed = _parse_stamp(stamp)
    if parsed is None:
        return True
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return parsed.date() == clock.astimezone(timezone.utc).date()


def refuse_repeat_note_post(
    args: dict[str, Any] | None,
    kwargs: dict[str, Any] | None,
) -> str | None:
    """Refuse a second successful discussion-note post for this step today.

    No job context means a unit call, and the write is unchanged.
    """
    import os

    values = dict(kwargs or {})
    job_id = str(
        values.get("job_id")
        or os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()
    db_path = str(values.get("db_path") or os.environ.get("ROBIE_JOB_DB") or "").strip()
    if not job_id or not db_path:
        return None
    from .store import JobStore

    try:
        store = JobStore(db_path)
        record = store.get_checkpoint_record(job_id, "discussion_note")
    except Exception:
        return None
    if not record:
        return None
    data = dict(record.get("data") or {})
    if str(data.get("status") or "") not in _NOTE_POSTED:
        return None
    if not _same_utc_day(record.get("created_at")):
        return None
    step = discussion_note_step_id(store, job_id, args)
    prior = str(data.get("step_id") or "")
    same_step = bool(prior) and prior == step
    prior_note = " ".join(str(data.get("request_note") or data.get("note_text") or "").casefold().split())
    this_note = " ".join(str((args or {}).get("note_text") or "").casefold().split())
    same_text = bool(this_note) and this_note == prior_note
    if same_step or same_text or (prior.startswith("plan:") and prior == step):
        return REPEAT_NOTE_REFUSAL
    if prior.startswith("plan:") and step.startswith("plan:") and prior == step:
        return REPEAT_NOTE_REFUSAL
    return None


def stop_recordings_for_jobs(db_path: str, job_ids: list[str], status: str) -> None:
    """Stop screen capture for these jobs. Missing recordings are ignored."""
    if not db_path or not job_ids:
        return
    from .recording import RecordingManager

    manager = RecordingManager(db_path)
    for job_id in job_ids:
        if job_id:
            manager.safe_stop(job_id, status)


def sweep_dead_running_jobs(
    store: Any,
    *,
    older_than_seconds: int = DEAD_RUNNING_AFTER_SECONDS,
    now: datetime | None = None,
) -> list[str]:
    """Fail RUNNING jobs with no live lease and no fresh heartbeat."""
    failed = store.fail_dead_running_jobs(
        older_than_seconds=older_than_seconds,
        now=now,
    )
    path = str(getattr(store, "path", "") or "")
    stop_recordings_for_jobs(path, failed, JobStatus.FAILED.value)
    if path:
        try:
            from .recording import RecordingManager

            RecordingManager(path).sweep_stale_recordings()
        except Exception:
            pass
    return failed


def settle_job_when_reply_sent(db_path: str, job_id: str, content: str) -> None:
    """The reply is out. Leave RUNNING and stop the recording.

    A short in-progress line stays open. A clarify parks the job instead.
    """
    if not db_path or not job_id:
        return
    from .chat_guard import _looks_in_progress
    from .store import JobStore

    text = str(content or "")
    if outbound_is_clarify(text):
        store = JobStore(db_path)
        if mark_job_waiting_for_user(store, job_id, text):
            try:
                status = str(store.get_job(job_id).get("status") or "")
            except Exception:
                status = JobStatus.NEEDS_CLARIFICATION.value
            stop_recordings_for_jobs(db_path, [job_id], status)
        return
    if _looks_in_progress(text):
        return
    store = JobStore(db_path)
    try:
        job = store.get_job(job_id)
    except Exception:
        return
    status = JobStatus(job["status"])
    if status in {JobStatus.RUNNING, JobStatus.VERIFYING}:
        store.transition(
            job_id,
            JobStatus.UNVERIFIED,
            expected={JobStatus.RUNNING, JobStatus.VERIFYING},
            error="reply sent",
            release_lease=True,
        )
        stop_recordings_for_jobs(db_path, [job_id], JobStatus.UNVERIFIED.value)
        return
    if status in {
        JobStatus.COMPLETE,
        JobStatus.FAILED,
        JobStatus.UNVERIFIED,
        JobStatus.CANCELLED,
        JobStatus.NEEDS_CLARIFICATION,
        JobStatus.AWAITING_HUMAN_INPUT,
    }:
        stop_recordings_for_jobs(db_path, [job_id], status.value)
