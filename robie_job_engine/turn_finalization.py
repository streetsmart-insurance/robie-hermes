"""A job is finalized only from a finished model turn.

Text in an assistant message that also has tool calls is not that turn.
It is not the reply, and it does not verify, close, or stop the job.
The same rule is applied at each of these sites:

- ``guard_chat_response`` returns before it writes ``worker_response``,
  binds a destination, runs the verifier, or takes the
  "no structured destination action checkpoint" close.
- ``settle_job_when_reply_sent`` returns before it transitions the job
  or calls ``_stop_model_for_finished_turn``.
- ``send`` and ``edit_message`` do not post that text and do not record it.
- ``_stop_model_for_finished_turn`` runs only after a real finalize:
  the model's final turn (no tool calls left), the turn or step ceiling,
  a stop, or a hard-block refusal that is itself the final result.
- ``finalize_turn_if_still_open`` records one honest line when the model
  reaches the end with no write and no reply.

A final ``PLAYWRIGHT_BLOCKED`` / ``ROBIE_BLOCKED`` worker line, an
action-gate hard block, a stop, and the time ceiling still close. Those
are not mid-turn tool narration.
"""

from __future__ import annotations

import os
import json
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
import re
import threading
from typing import Any

COULD_NOT_FINISH = "I couldn't finish that; a CSR should take a look."

_LOCK = threading.Lock()
_TOOL_TEXT: dict[str, set[str]] = {}
_TOOL_MESSAGE_OPEN: set[str] = set()
_OPEN_GENERATION: dict[str, str] = {}
_GENERATION_CONTEXT: ContextVar[tuple[str, str, str] | None] = ContextVar("robie_generation", default=None)
_GENERATION_PATHS: dict[tuple[str, str], str] = {}
_GENERATION_DELIVERED: dict[tuple[str, str], threading.Event] = {}
_INSTALLED = False
_WRITE_CLAIM = re.compile(
    r"\b(?:has been filed|been filed|note has been|discussion note has been|"
    r"i (?:filed|posted|saved|added) (?:the|that|a) note|note id\s*\d+)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def current_model_job_id() -> str:
    bound = _GENERATION_CONTEXT.get()
    if bound:
        return bound[0]
    return str(os.environ.get("ROBIE_CURRENT_JOB_ID") or "").strip()


def begin_tool_call_message(job_id: str | None) -> None:
    """The model is inside an assistant message that has tool calls."""
    if not job_id:
        return
    with _LOCK:
        _TOOL_MESSAGE_OPEN.add(str(job_id))


def end_tool_call_message(job_id: str | None) -> None:
    """Those tool calls have returned. Their text is still not the reply."""
    if not job_id:
        return
    with _LOCK:
        _TOOL_MESSAGE_OPEN.discard(str(job_id))


def tool_call_message_is_open(job_id: str | None) -> bool:
    if not job_id:
        return False
    with _LOCK:
        return str(job_id) in _TOOL_MESSAGE_OPEN


def note_assistant_text_has_tool_calls(job_id: str | None, text: str) -> None:
    """Remember text that shared an assistant message with tool calls."""
    key = _norm(text)
    if not job_id or not key:
        return
    with _LOCK:
        _TOOL_TEXT.setdefault(str(job_id), set()).add(key)


def remember_if_tool_message_open(job_id: str | None, text: str) -> None:
    """A send that arrives while tools from that message are running is the same text."""
    if tool_call_message_is_open(job_id):
        note_assistant_text_has_tool_calls(job_id, text)


def assistant_text_has_tool_calls(job_id: str | None, text: str) -> bool:
    key = _norm(text)
    if not job_id or not key:
        return False
    with _LOCK:
        return key in _TOOL_TEXT.get(str(job_id), set())


def model_text_is_not_final(job_id: str | None, text: str) -> bool:
    """True when this text must not finalize the job."""
    if not job_id:
        return False
    if tool_call_message_is_open(job_id):
        note_assistant_text_has_tool_calls(job_id, text)
        return True
    return assistant_text_has_tool_calls(job_id, text)


def clear_tool_call_text(job_id: str | None) -> None:
    if not job_id:
        return
    with _LOCK:
        _TOOL_TEXT.pop(str(job_id), None)
        _TOOL_MESSAGE_OPEN.discard(str(job_id))


@contextmanager
def isolated_model_generation_context():
    """Do not leave one turn's identity on the listener task after dispatch."""
    token = _GENERATION_CONTEXT.set(None)
    try:
        yield
    finally:
        _GENERATION_CONTEXT.reset(token)


def _read_generation(conn: Any, job_id: str, kind: str = "model_generation") -> dict[str, Any]:
    row = conn.execute("SELECT data_json FROM checkpoints WHERE job_id=? AND kind=?",
                       (job_id, kind)).fetchone()
    return json.loads(row["data_json"]) if row else {}


def _save_generation(conn: Any, job_id: str, kind: str, data: dict[str, Any]) -> None:
    from .store import canonical_json, utc_now
    conn.execute(
        "INSERT INTO checkpoints(job_id,kind,data_json,created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(job_id,kind) DO UPDATE SET data_json=excluded.data_json,created_at=excluded.created_at",
        (job_id, kind, canonical_json(data), utc_now()),
    )


def begin_model_generation(job_id: str | None, *, store: Any = None) -> str:
    """Create a distinct execution epoch, persisted before starting the worker.

    UUIDs never repeat after finish/restart. Context-local identity follows asyncio
    tasks/to_thread; it is never inferred from mutable process-global job env vars.
    Store-less calls support old synthetic callers but cannot authorize a write.
    """
    if not job_id:
        return ""
    key, generation = str(job_id), uuid.uuid4().hex
    path = str(store.path) if store is not None else ""
    with _LOCK:
        if store is not None:
            with store.transaction() as conn:
                _save_generation(conn, key, "model_generation", {"generation": generation, "running": True})
        previous = _OPEN_GENERATION.get(key)
        if previous:
            event = _GENERATION_DELIVERED.pop((key, previous), None)
            if event is not None:
                event.set()
            _GENERATION_PATHS.pop((key, previous), None)
        _OPEN_GENERATION[key] = generation
        _GENERATION_DELIVERED[(key, generation)] = threading.Event()
        _GENERATION_PATHS[(key, generation)] = path
    _GENERATION_CONTEXT.set((key, generation, path))
    return generation


def bind_started_model_generation(job_id: str, generation: str) -> bool:
    """Bind the token returned by an off-loop begin; refuse a superseded start."""
    with _LOCK:
        if _OPEN_GENERATION.get(job_id) != generation:
            return False
        path = _GENERATION_PATHS.get((job_id, generation), "")
    _GENERATION_CONTEXT.set((job_id, generation, path))
    return True


def resume_model_generation(store: Any, job_id: str, generation: str) -> bool:
    """Bind a known durable epoch for retry/readback only; never execute a write.

    A caller must retain its original token. Missing/legacy/superseded tokens are
    refused; restart never adopts the latest epoch on behalf of an unknown caller.
    """
    if not isinstance(generation, str) or not generation:
        return False
    with _LOCK:
        with store.transaction() as conn:
            state = _read_generation(conn, job_id)
            if state.get("generation") != generation:
                return False
        _GENERATION_PATHS[(job_id, generation)] = str(store.path)
        event = _GENERATION_DELIVERED.setdefault((job_id, generation), threading.Event())
        if state.get("running") is True:
            _OPEN_GENERATION[job_id] = generation
        else:
            event.set()
    _GENERATION_CONTEXT.set((job_id, generation, str(store.path)))
    return True


def current_model_generation(job_id: str | None) -> str:
    bound = _GENERATION_CONTEXT.get()
    return bound[1] if bound and bound[0] == str(job_id) else ""


def model_generation_is_running(job_id: str | None) -> bool:
    if not job_id:
        return False
    key = str(job_id)
    with _LOCK:
        generation = current_model_generation(key) or _OPEN_GENERATION.get(key, "")
        if not generation or _OPEN_GENERATION.get(key) != generation:
            return False
        path = _GENERATION_PATHS.get((key, generation), "")
    if path:
        from .store import JobStore
        with JobStore(path).connect() as conn:
            state = _read_generation(conn, key)
        return state.get("generation") == generation and state.get("running") is True
    return True


def finish_model_generation(job_id: str | None, generation: str | None = None) -> None:
    """Finish only the caller's epoch; a delayed callback cannot close its successor."""
    if not job_id:
        return
    key = str(job_id)
    expected = generation or current_model_generation(key)
    if not expected:
        return
    with _LOCK:
        bound = _GENERATION_CONTEXT.get()
        path = (bound[2] if bound and bound[:2] == (key, expected) else
                _GENERATION_PATHS.get((key, expected), ""))
        if path:
            from .store import JobStore
            with JobStore(path).transaction() as conn:
                state = _read_generation(conn, key)
                if state.get("generation") != expected:
                    return
                _save_generation(conn, key, "model_generation", {"generation": expected, "running": False})
        if _OPEN_GENERATION.get(key) == expected:
            _OPEN_GENERATION.pop(key, None)
        event = _GENERATION_DELIVERED.pop((key, expected), None)
        _GENERATION_PATHS.pop((key, expected), None)
        if event is not None:
            event.set()
    bound = _GENERATION_CONTEXT.get()
    if bound and bound[:2] == (key, expected):
        _GENERATION_CONTEXT.set(None)


def note_generation_delivered(job_id: str | None) -> None:
    finish_model_generation(job_id, current_model_generation(job_id))


def wait_for_generation_delivery(job_id: str | None, timeout: float) -> bool:
    generation = current_model_generation(job_id)
    if not job_id or not generation:
        return False
    with _LOCK:
        event = _GENERATION_DELIVERED.get((str(job_id), generation))
    return bool(event and event.wait(timeout))


def generation_settle_seconds() -> float:
    raw = str(os.environ.get("ROBIE_GENERATION_SETTLE_SECONDS") or "").strip()
    if not raw:
        return 20.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 20.0


def reply_claims_a_write(text: str) -> bool:
    """True when the model says a note or other write was saved."""
    return bool(_WRITE_CLAIM.search(" ".join(str(text or "").split())))


def confirmed_write_this_turn(store: Any, job_id: str | None) -> bool:
    """True only for a write this job confirmed during the current generation.

    A ledger match from an earlier day is not a write this turn.
    """
    if store is None or not job_id:
        return False
    generation = current_model_generation(job_id)
    if not generation:
        return False
    try:
        with store.transaction() as conn:
            state = _read_generation(conn, str(job_id))
            log = _read_generation(conn, str(job_id), "turn_write_log")
    except Exception:
        return False
    return (state.get("generation") == generation and log.get("generation") == generation
            and log.get("confirmed") is True and bool(log.get("note_id")))


def engine_question_already_sent(store: Any, job_id: str | None) -> bool:
    """True when this turn already posted the engine's clarify or duplicate question."""
    if store is None or not job_id:
        return False
    try:
        sent = store.get_checkpoint(job_id, "chat_outcome_sent") or {}
    except Exception:
        sent = {}
    generation = current_model_generation(job_id)
    if generation and sent.get("generation") != generation:
        return False
    text = " ".join(str((sent or {}).get("text") or "").split())
    if not text:
        return False
    from .chat_job_controls import outbound_is_clarify

    folded = text.casefold()
    if "already added that note" in folded or "want me to add it again" in folded:
        return True
    return outbound_is_clarify(text)


def suppress_model_reply(store: Any, job_id: str | None, text: str) -> bool:
    """Drop the model's own send when the engine already asked, or the save is unproved."""
    generation = current_model_generation(job_id)
    if generation and store is not None and job_id:
        state = store.get_checkpoint(job_id, "model_generation") or {}
        if state and state.get("generation") != generation:
            return True
    if engine_question_already_sent(store, job_id):
        return True
    if reply_claims_a_write(text) and not confirmed_write_this_turn(store, job_id):
        return True
    return False


def record_turn_write(store: Any, job_id: str | None, *, note_id: str = "") -> bool:
    """Remember that this generation confirmed a write. The model may then say so."""
    if store is None or not job_id:
        return False
    generation = current_model_generation(job_id)
    if not generation or not str(note_id or "").strip():
        return False
    with store.transaction() as conn:
        state = _read_generation(conn, str(job_id))
        if state.get("generation") != generation:
            return False
        _save_generation(conn, str(job_id), "turn_write_log", {
            "confirmed": True, "note_id": str(note_id).strip(), "generation": generation,
        })
    return True


def generation_is_superseded(store: Any, job_id: str, generation: str | None = None) -> bool:
    expected = generation if generation is not None else current_model_generation(job_id)
    if not expected:
        return False
    state = store.get_checkpoint(job_id, "model_generation") or {}
    return bool(state) and state.get("generation") != expected


def visible_fallback_line(db_path: str, job_id: str | None, *, generation: str | None = None) -> str | None:
    """The line a silent turn still owes the user. This does not close the job."""
    if not db_path or not job_id:
        return None
    if model_generation_is_running(job_id):
        return None
    from .models import JobStatus
    from .store import JobStore

    store = JobStore(db_path)
    if generation_is_superseded(store, job_id, generation):
        return None
    try:
        job = store.get_job(job_id)
    except Exception:
        return None
    status = str((job or {}).get("status") or "")
    if status not in {JobStatus.RUNNING.value, JobStatus.VERIFYING.value, JobStatus.PENDING.value}:
        return None
    if _already_has_reply(store, job_id):
        return None
    return _reply_for_silent_turn(store, job) or COULD_NOT_FINISH


def close_turn_after_visible_line(db_path: str, job_id: str | None, line: str, *, generation: str | None = None) -> None:
    """The line is already on its way. Only then may the job become terminal."""
    shown = " ".join(str(line or "").split()).strip()
    if not db_path or not job_id or not shown:
        return
    if model_generation_is_running(job_id):
        return
    from .models import JobStatus
    from .store import JobStore
    from .worker_contract import sanitize_worker_response

    store = JobStore(db_path)
    if generation_is_superseded(store, job_id, generation):
        return
    store.checkpoint(
        job_id,
        "worker_response",
        sanitize_worker_response(store, job_id, shown),
    )
    try:
        current = store.get_job(job_id)
    except Exception:
        current = None
    current_status = str((current or {}).get("status") or "")
    if current_status in {JobStatus.RUNNING.value, JobStatus.VERIFYING.value, JobStatus.PENDING.value}:
        expected = {JobStatus.RUNNING, JobStatus.VERIFYING}
        if current_status == JobStatus.PENDING.value:
            store.transition(
                job_id,
                JobStatus.RUNNING,
                expected={JobStatus.PENDING},
            )
        store.transition(
            job_id,
            JobStatus.UNVERIFIED,
            expected=expected,
            error="the model finished without a reply",
            release_lease=True,
        )
    clear_tool_call_text(job_id)
    from .chat_turn_control import request_agent_stop

    request_agent_stop(job_id)


def finalize_turn_if_still_open(db_path: str, job_id: str | None) -> str | None:
    """Return the fallback line. The caller sends it before closing the job."""
    return visible_fallback_line(db_path, job_id)


def _already_has_reply(store: Any, job_id: str) -> bool:
    for kind in ("worker_response", "chat_outcome_sent", "chat_delivery"):
        try:
            if store.get_checkpoint(job_id, kind):
                return True
        except Exception:
            continue
    return False


def _reply_for_silent_turn(store: Any, job: dict[str, Any]) -> str:
    """Use the note's honest line when a note was already filed and the thread has nothing."""
    try:
        note = store.get_checkpoint(job["id"], "discussion_note") or {}
    except Exception:
        note = {}
    if not isinstance(note, dict) or not note:
        return ""
    from .chat_guard import _discussion_note_user_reply, _unproved_field_user_reply

    return str(
        _unproved_field_user_reply(store, job) or _discussion_note_user_reply(store, job) or ""
    ).strip()


def install_tool_call_text_guard() -> None:
    """Mark assistant text that shares a message with tool calls, before Chat sees it.

    Hermes emits that text, then runs the tools. The Chat send can land
    after the tools return. The text is marked first, so the late send
    still does not finalize the job. Missing Hermes is a no-op: tests
    mark the text directly.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    cls = _agent_class()
    if cls is None:
        return
    original_emit = getattr(cls, "_emit_interim_assistant_message", None)
    original_exec = getattr(cls, "_execute_tool_calls", None)
    if not callable(original_emit):
        return
    if getattr(original_emit, "_robie_tool_text_guard", False):
        _INSTALLED = True
        return

    def emit(self: Any, assistant_msg: Any) -> None:
        calls = assistant_msg.get("tool_calls") if isinstance(assistant_msg, dict) else None
        if calls:
            from .conversation_reply import refuse_conversation_tools
            refuse_conversation_tools()
        job_id = current_model_job_id()
        if job_id and calls:
            begin_tool_call_message(job_id)
            visible = ""
            reader = getattr(self, "_interim_assistant_visible_text", None)
            if callable(reader):
                try:
                    visible = str(reader(assistant_msg) or "")
                except Exception:
                    visible = ""
            if not visible and isinstance(assistant_msg, dict):
                visible = str(assistant_msg.get("content") or "")
            note_assistant_text_has_tool_calls(job_id, visible)
        return original_emit(self, assistant_msg)

    emit._robie_tool_text_guard = True  # type: ignore[attr-defined]
    cls._emit_interim_assistant_message = emit

    if callable(original_exec):

        def execute(self: Any, *args: Any, **kwargs: Any) -> Any:
            from .conversation_reply import refuse_conversation_tools
            refuse_conversation_tools()
            job_id = current_model_job_id()
            try:
                owner, generation, owner_db = bound_model_context()
                if owner:
                    from .chat_turn_control import refuse_current_tool_call
                    refusal = refuse_current_tool_call()
                    if refusal:
                        raise RuntimeError(refusal)
                return original_exec(self, *args, **kwargs)
            finally:
                end_tool_call_message(job_id)

        execute._robie_conversation_tool_guard = True
        execute._robie_tool_text_guard = True  # type: ignore[attr-defined]
        cls._execute_tool_calls = execute
    _INSTALLED = True


def _agent_class() -> Any:
    for module_name in ("run_agent", "agent.agent", "agent.conversation_loop"):
        try:
            module = __import__(module_name, fromlist=["AIAgent"])
        except Exception:
            continue
        cls = getattr(module, "AIAgent", None)
        if cls is not None and hasattr(cls, "_emit_interim_assistant_message"):
            return cls
    return None


def bound_model_context() -> tuple[str, str, str]:
    """Exact job, generation and database carried by this execution context.

    Unlike legacy current_model_job_id, this never falls back to process env.
    """
    return _GENERATION_CONTEXT.get() or ('', '', '')
