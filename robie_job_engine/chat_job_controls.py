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
    if _DEDUCTIBLE_CHANGE.search(raw):
        if _HOLDER_ADD.search(raw):
            return (
                "I can't change the deductible. I can't add the certificate "
                "holder yet either, so staff still has to do that."
            )
        return "I can't change the deductible. Staff still has to do that."
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
    if re.match(r"(?i)^(which|what|who|where|when|how)\b", field) or field.endswith("?"):
        asked = field[0].upper() + field[1:].rstrip("?")
        asked = asked.rstrip()
        return asked + "?"
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


def mark_job_waiting_for_user(
    store: Any,
    job_id: str,
    question: str = "",
    *,
    from_unverified: bool = False,
) -> bool:
    """Park a live job so the next message answers it instead of busy-defer.

    A repeat-note question may also pull a job back from UNVERIFIED. That
    status was a premature "reply sent", and the yes or no still has to land.
    """
    if not job_id or store is None:
        return False
    try:
        job = store.get_job(job_id)
    except Exception:
        return False
    status = JobStatus(job["status"])
    prompt = str(question or "").strip()[:500]
    if status in {JobStatus.NEEDS_CLARIFICATION, JobStatus.AWAITING_HUMAN_INPUT}:
        store.checkpoint(job_id, "clarification", {"question": prompt, "asked": True})
        store.checkpoint(
            job_id, "keep_chat_context", {"reason": "needs_clarification"}
        )
        return True
    if status == JobStatus.UNVERIFIED and from_unverified:
        store.transition(
            job_id,
            JobStatus.NEEDS_CLARIFICATION,
            expected={JobStatus.UNVERIFIED},
            error="waiting on the user",
            resume_status=JobStatus.PENDING,
            release_lease=True,
        )
        store.checkpoint(job_id, "clarification", {"question": prompt, "asked": True})
        store.checkpoint(
            job_id, "keep_chat_context", {"reason": "needs_clarification"}
        )
        return True
    if status in {
        JobStatus.COMPLETE,
        JobStatus.UNVERIFIED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }:
        return False
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


def repeat_note_question(store: Any, job_id: str) -> str:
    """The ledger question, when this job already has that note."""
    if not job_id or store is None:
        return ""
    try:
        note = store.get_checkpoint(job_id, "discussion_note") or {}
    except Exception:
        return ""
    if str(note.get("status") or "") != "already_posted":
        return ""
    return " ".join(str(note.get("reason") or "").split())


def note_reply_is_pending(store: Any, job_id: str) -> bool:
    """True while the note tool is in flight and the model must not finish the job."""
    if not job_id or store is None:
        return False
    try:
        row = store.get_checkpoint(job_id, "discussion_note_pending") or {}
    except Exception:
        return False
    return bool(row.get("open"))


def dedupe_note_revival(
    store: Any,
    job: dict[str, Any] | None,
    text: str,
    inbound_thread_id: str | None = None,
) -> str:
    """How an inbound message may touch a repeat-note job.

    ``yes`` and ``no`` are allowed only inside that job's thread. Anything
    else, including a later unrelated message, must not reopen the job.
    """
    if not job or store is None:
        return ""
    job_id = str(job.get("id") or "")
    if not repeat_note_question(store, job_id):
        return ""
    from .chat_thread import read_job_chat_thread, thread_resource_name

    inbound = thread_resource_name(inbound_thread_id)
    stored = read_job_chat_thread(store, job_id)
    in_thread = bool(inbound and stored and inbound == stored)
    if in_thread and explicit_note_decline(text):
        return "no"
    if in_thread and explicit_note_repost(text):
        return "yes"
    return "block"


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
_EXPLICIT_NO = re.compile(
    r"^(?:no|nope|nah|no thanks|leave it|don't|do not)\.?$",
    re.IGNORECASE,
)
LEFT_AS_IS = "OK, I left it as is."
ALREADY_DONE = "Already done."
_DEDUCTIBLE_CHANGE = re.compile(
    r"\b(?:change|update|set|raise|lower)\b.{0,40}\bdeductibles?\b"
    r"|\bdeductibles?\b.{0,24}\b(?:to|of)\b",
    re.IGNORECASE,
)
_HOLDER_ADD = re.compile(
    r"\b(?:add|put)\b.{0,24}\bholders?\b|\bcertificate holder\b",
    re.IGNORECASE,
)


def explicit_note_repost(text: str) -> bool:
    """True only for a direct yes to posting the same note again."""
    body = " ".join(str(text or "").split())
    return bool(body) and _EXPLICIT_YES.match(body) is not None


def explicit_note_decline(text: str) -> bool:
    """True only for a direct no to posting the same note again."""
    body = " ".join(str(text or "").split())
    return bool(body) and _EXPLICIT_NO.match(body) is not None


def close_declined_note_repost(store: Any, job_id: str, text: str) -> str | None:
    """Close a repeat-note question with one line. Does not post the note."""
    if not job_id or store is None or not explicit_note_decline(text):
        return None
    try:
        note = store.get_checkpoint(job_id, "discussion_note") or {}
    except Exception:
        return None
    if str(note.get("status") or "") != "already_posted":
        return None
    job = store.get_job(job_id)
    status = JobStatus(job["status"])
    if status not in {JobStatus.COMPLETE, JobStatus.CANCELLED, JobStatus.FAILED}:
        store.transition(
            job_id,
            JobStatus.CANCELLED,
            expected={status},
            error=LEFT_AS_IS,
            release_lease=True,
        )
    store.checkpoint(job_id, "note_left_as_is", {"reply": LEFT_AS_IS})
    stop_recordings_for_jobs(
        str(getattr(store, "path", "") or ""),
        [job_id],
        JobStatus.CANCELLED.value,
    )
    return LEFT_AS_IS


def waiting_jobs_for_requester(
    db_path: str,
    conversation_id: str,
    requester: str | None = None,
) -> list[str]:
    """Waiting jobs in this space that belong to the person who said stop."""
    if not db_path or not conversation_id:
        return []
    from .chat_turn_control import job_is_waiting_on_user
    from .store import JobStore

    who = " ".join(str(requester or "").casefold().split())
    store = JobStore(db_path)
    found: list[str] = []
    for job in store.list_jobs_by_status(
        {JobStatus.NEEDS_CLARIFICATION, JobStatus.AWAITING_HUMAN_INPUT}
    ):
        if not job_is_waiting_on_user(job):
            continue
        payload = dict(job.get("payload") or {})
        space = str(payload.get("conversation_id") or "")
        if space and space != conversation_id:
            continue
        owner = " ".join(
            str(payload.get("requested_by") or payload.get("requester_user_id") or "")
            .casefold()
            .split()
        )
        if who and owner and who not in owner and owner not in who:
            continue
        job_id = str(job.get("id") or "")
        if job_id:
            found.append(job_id)
    return found


def consume_note_repost_allowance(store: Any, job_id: str) -> bool:
    """Allow one post after an explicit yes. The next call is blocked again."""
    if not job_id or store is None:
        return False
    try:
        row = store.get_checkpoint(job_id, "note_repost_confirmed") or {}
    except Exception:
        return False
    if not row or row.get("used"):
        return False
    used = dict(row)
    used["used"] = True
    try:
        store.checkpoint(job_id, "note_repost_confirmed", used)
    except Exception:
        return False
    return True


def _yes_already_spent(store: Any, job_id: str) -> bool:
    """True when this thread already used its one yes, or the note is filed."""
    try:
        confirmed = store.get_checkpoint(job_id, "note_repost_confirmed") or {}
    except Exception:
        confirmed = {}
    if confirmed.get("used"):
        return True
    try:
        note = store.get_checkpoint(job_id, "discussion_note") or {}
    except Exception:
        return False
    return str(note.get("status") or "") == "filed"


def note_already_done_target(
    store: Any,
    text: str,
    *,
    conversation_id: str | None,
    inbound_thread_id: str | None,
) -> str | None:
    """The finished note job a second in-thread yes must not reopen.

    The first yes resumes the parked job. After that note is filed, another
    yes in the same thread is not a new task.
    """
    if store is None or not explicit_note_repost(text):
        return None
    from .chat_thread import read_job_chat_thread, thread_resource_name

    inbound = thread_resource_name(inbound_thread_id)
    if not inbound or not str(conversation_id or "").strip():
        return None
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT job_id FROM conversation_job_links
                   WHERE conversation_id=?
                   ORDER BY created_at DESC LIMIT 30""",
                (str(conversation_id),),
            ).fetchall()
    except Exception:
        return None
    seen: set[str] = set()
    for row in rows:
        job_id = str(row["job_id"] or "")
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        if read_job_chat_thread(store, job_id) != inbound:
            continue
        if _yes_already_spent(store, job_id):
            return job_id
    return None


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
    try:
        prior = store.get_checkpoint(job_id, "note_repost_confirmed") or {}
    except Exception:
        prior = {}
    if prior.get("used"):
        return False
    if prior:
        return True
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


def inbound_answers_waiting_job(db_path: str, event: Any) -> bool:
    """True when this message is inside a waiting job's stored thread."""
    source = getattr(event, "source", None)
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    if not db_path or not thread_id:
        return False
    from .chat_thread import read_job_chat_thread
    from .store import JobStore

    store = JobStore(db_path)
    for job_id in waiting_jobs_for_requester(db_path, chat_id, None):
        stored = read_job_chat_thread(store, job_id) or ""
        try:
            payload = dict(store.get_job(job_id).get("payload") or {})
        except Exception:
            payload = {}
        if thread_id in {stored, str(payload.get("thread_id") or "").strip()}:
            return True
    return False


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
    """Refuse another discussion-note post once this job has already written one.

    An explicit yes that has not been used yet may post once. No job context
    means a unit call, and the write is unchanged.
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
    if not note_checkpoint_already_wrote(data):
        return None
    try:
        allowance = store.get_checkpoint(job_id, "note_repost_confirmed") or {}
    except Exception:
        allowance = {}
    if allowance and not allowance.get("used"):
        return None
    return REPEAT_NOTE_REFUSAL


def note_checkpoint_already_wrote(data: dict[str, Any] | None) -> bool:
    """True when this job already sent a note. A matched older note is not a write."""
    row = dict(data or {})
    status = str(row.get("status") or "")
    if status == "already_posted":
        return False
    if status in _NOTE_POSTED or status == "sent":
        return True
    if status == "held" and str(row.get("confirmation") or "") == "sent, unconfirmed":
        return True
    return bool(row.get("wrote") is True and status not in {"", "pending"})


def note_job_already_wrote(store: Any, job_id: str) -> bool:
    """True when this job's discussion-note checkpoint records a real write."""
    if not job_id or store is None:
        return False
    try:
        record = store.get_checkpoint_record(job_id, "discussion_note") or {}
    except Exception:
        return False
    if not record:
        return False
    return note_checkpoint_already_wrote(dict(record.get("data") or {}))


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


def settle_job_when_reply_sent(db_path: str, job_id: str, content: str) -> bool:
    """The reply is out. Leave RUNNING and stop the recording.

    A short in-progress line stays open. A clarify parks the job instead.
    True means the job is no longer working, so the chat lock can drop.
    """
    if not db_path or not job_id:
        return False
    from .chat_guard import _looks_in_progress
    from .store import JobStore
    from .write_verification_loop import is_plan_refusal_text

    text = str(content or "")
    # The validator is talking to the model. The job stays open so it can re-plan.
    if is_plan_refusal_text(text):
        return False
    from .chat_turn_control import (
        is_progress_heartbeat_or_thinking,
        is_refused_tool_text,
        is_tool_progress_text,
    )

    # A refused tool call is the model's to continue. It does not end the turn.
    # A heartbeat or thinking line is not the reply either.
    if (
        is_tool_progress_text(text)
        or is_progress_heartbeat_or_thinking(text)
        or is_refused_tool_text(text)
    ):
        return False
    from .turn_finalization import model_text_is_not_final

    # Text from an assistant message that still has tool calls is not the reply.
    if model_text_is_not_final(job_id, text):
        return False
    if outbound_is_clarify(text):
        store = JobStore(db_path)
        if mark_job_waiting_for_user(store, job_id, text):
            try:
                status = str(store.get_job(job_id).get("status") or "")
            except Exception:
                status = JobStatus.NEEDS_CLARIFICATION.value
            stop_recordings_for_jobs(db_path, [job_id], status)
            return True
        return False
    if _looks_in_progress(text):
        return False
    store = JobStore(db_path)
    try:
        job = store.get_job(job_id)
    except Exception:
        return False
    if note_reply_is_pending(store, job_id) and not repeat_note_question(store, job_id):
        return False
    question = repeat_note_question(store, job_id)
    if question and mark_job_waiting_for_user(
        store, job_id, question, from_unverified=True
    ):
        try:
            parked = str(store.get_job(job_id).get("status") or "")
        except Exception:
            parked = JobStatus.NEEDS_CLARIFICATION.value
        stop_recordings_for_jobs(db_path, [job_id], parked)
        return True
    status = JobStatus(job["status"])
    if status in {JobStatus.RUNNING, JobStatus.VERIFYING}:
        try:
            from .chat_guard import close_confirmed_note_job

            if close_confirmed_note_job(store, job_id):
                stop_recordings_for_jobs(db_path, [job_id], JobStatus.COMPLETE.value)
                _stop_model_for_finished_turn(job_id, db_path)
                return True
        except Exception:
            pass
        try:
            status = JobStatus(store.get_job(job_id)["status"])
        except Exception:
            return False
        if status not in {JobStatus.RUNNING, JobStatus.VERIFYING}:
            # A partial note close already left the job. Do not transition again.
            if status in {
                JobStatus.COMPLETE,
                JobStatus.FAILED,
                JobStatus.UNVERIFIED,
                JobStatus.CANCELLED,
                JobStatus.NEEDS_CLARIFICATION,
                JobStatus.AWAITING_HUMAN_INPUT,
            }:
                stop_recordings_for_jobs(db_path, [job_id], status.value)
                if status in {
                    JobStatus.COMPLETE,
                    JobStatus.FAILED,
                    JobStatus.UNVERIFIED,
                    JobStatus.CANCELLED,
                }:
                    _stop_model_for_finished_turn(job_id, db_path)
                return True
            return False
        store.transition(
            job_id,
            JobStatus.UNVERIFIED,
            expected={JobStatus.RUNNING, JobStatus.VERIFYING},
            error="reply sent",
            release_lease=True,
        )
        stop_recordings_for_jobs(db_path, [job_id], JobStatus.UNVERIFIED.value)
        _stop_model_for_finished_turn(job_id, db_path)
        return True
    if status in {JobStatus.NEEDS_CLARIFICATION, JobStatus.AWAITING_HUMAN_INPUT}:
        # Parked is not /stop. The person's answer has to be delivered.
        stop_recordings_for_jobs(db_path, [job_id], status.value)
        return True
    if status in {
        JobStatus.COMPLETE,
        JobStatus.FAILED,
        JobStatus.UNVERIFIED,
        JobStatus.CANCELLED,
    }:
        stop_recordings_for_jobs(db_path, [job_id], status.value)
        _stop_model_for_finished_turn(job_id, db_path)
        return True
    return False


def _stop_model_for_finished_turn(job_id: str, db_path: str = "") -> None:
    """The reply is out. Stop the agent and drop the session lock."""
    try:
        from .chat_turn_control import release_finished_job_session

        release_finished_job_session(db_path, job_id, stop_agent=True)
    except Exception:
        return
