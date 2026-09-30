"""Chat turn ceiling and /stop.

hermes-agent's gateway/run.py only watches idle time
(``agent.gateway_timeout``, default 1800 seconds). This module is the
total-time ceiling for a Chat turn. It reads ``agent.gateway_max_turn_seconds``
and does not change ``max_turns`` (the email agent shares config.yaml).

The email ~10 minute stop is separate: ``email_agent_runner`` chat timeout,
default 600 seconds, inside the watcher's 930 second process limit.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import signal
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("robie.chat_turn")

from .models import TERMINAL_STATUSES, JobStatus

DEFAULT_GATEWAY_MAX_TURN_SECONDS = 600
STOPPED_AFTER_TEN_MINUTES = (
    "I stopped after 10 minutes. Send it again if you still want it done."
)


def is_stop_command(text: str) -> bool:
    """True for /stop or /cancel in a thread or a DM. Does not create a job."""
    normalized = " ".join(str(text or "").casefold().split())
    normalized = re.sub(r"^@\s*robie\b", "", normalized).strip()
    if not normalized:
        return False
    head = normalized.split(" ", 1)[0].strip(".,!")
    return head in {"/stop", "/cancel"}


def turn_key(chat_id: str | None, thread_id: str | None = None) -> tuple[str, str]:
    return (str(chat_id or ""), str(thread_id or ""))


def _positive_seconds(raw: str, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return value


def _agent_scalar(config_text: str, key: str) -> str | None:
    """Read one integer-like scalar from the ``agent:`` block. Ignore other keys."""
    in_agent = False
    agent_indent = 0
    for line in str(config_text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not in_agent:
            if re.match(r"^agent\s*:", stripped):
                in_agent = True
                agent_indent = indent
            continue
        if indent <= agent_indent:
            break
        match = re.match(rf"^{re.escape(key)}\s*:\s*(\d+)\s*$", stripped)
        if match:
            return match.group(1)
    return None


def gateway_max_turn_seconds(
    *,
    config_text: str | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    """Total seconds for one Chat turn. Default 600. Does not read max_turns."""
    env = os.environ if environ is None else environ
    override = str(env.get("ROBIE_GATEWAY_MAX_TURN_SECONDS") or "").strip()
    if override:
        return _positive_seconds(override, DEFAULT_GATEWAY_MAX_TURN_SECONDS)
    text = config_text
    if text is None:
        home = str(env.get("HERMES_HOME") or "").strip()
        path = Path(home) / "config.yaml" if home else None
        if path is not None and path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
        else:
            text = ""
    found = _agent_scalar(text or "", "gateway_max_turn_seconds")
    if found is None:
        return DEFAULT_GATEWAY_MAX_TURN_SECONDS
    return _positive_seconds(found, DEFAULT_GATEWAY_MAX_TURN_SECONDS)


def stopped_after_limit_reply(seconds: int | None = None) -> str:
    limit = DEFAULT_GATEWAY_MAX_TURN_SECONDS if seconds is None else int(seconds)
    if limit == DEFAULT_GATEWAY_MAX_TURN_SECONDS:
        return STOPPED_AFTER_TEN_MINUTES
    minutes = max(1, round(limit / 60))
    return (
        f"I stopped after {minutes} minutes. "
        "Send it again if you still want it done."
    )


def _abandon_timed_out_gateway_turn(
    store: Any,
    job_id: str,
    *,
    seconds: int | None = None,
) -> str:
    """Mark the Chat job FAILED and return the plain reply. One step."""
    reply = stopped_after_limit_reply(
        gateway_max_turn_seconds() if seconds is None else seconds
    )
    if not job_id:
        return reply
    job = store.get_job(job_id)
    status = JobStatus(job["status"])
    if status not in TERMINAL_STATUSES:
        store.transition(
            job_id,
            JobStatus.FAILED,
            expected={status},
            error=reply,
            release_lease=True,
        )
    limit = gateway_max_turn_seconds() if seconds is None else int(seconds)
    store.checkpoint(
        job_id,
        "gateway_turn_timeout",
        {"reason": reply, "limit_seconds": limit},
    )
    return reply


_PROC_LOCK = threading.Lock()
_AGENT_PIDS: dict[str, set[int]] = {}
_ABORTED_JOBS: set[str] = set()

HAND_DRIVEN_EZLYNX_STOP = (
    "STOP. Do not drive EZLynx screens by hand for this task. "
    "The built-in note tool has to file it. "
    "If that tool failed, report the error in plain English and stop."
)

_HAND_DRIVEN_ACTIONS = frozenset({"ezlynx.certificate", "ezlynx.policy_change"})


def request_agent_stop(job_id: str | None) -> None:
    """Remember that this job's agent must stop, including in-flight tools."""
    if not job_id:
        return
    with _PROC_LOCK:
        _ABORTED_JOBS.add(str(job_id))


def agent_stop_requested(job_id: str | None) -> bool:
    if not job_id:
        return False
    with _PROC_LOCK:
        return str(job_id) in _ABORTED_JOBS


def register_agent_process(job_id: str | None, pid: int) -> None:
    """Track a browser/tool process group so /stop and the ceiling can kill it."""
    if not job_id or not pid:
        return
    with _PROC_LOCK:
        _AGENT_PIDS.setdefault(str(job_id), set()).add(int(pid))


def unregister_agent_process(job_id: str | None, pid: int) -> None:
    if not job_id or not pid:
        return
    with _PROC_LOCK:
        pids = _AGENT_PIDS.get(str(job_id))
        if not pids:
            return
        pids.discard(int(pid))
        if not pids:
            _AGENT_PIDS.pop(str(job_id), None)


def _descendant_pids(pid: int) -> list[int]:
    """Child processes of ``pid``, including grandchildren. /proc only."""
    if pid <= 1:
        return []
    found: list[int] = []
    stack = [pid]
    seen = {pid, os.getpid()}
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    while stack:
        current = stack.pop()
        try:
            entries = list(proc.iterdir())
        except OSError:
            break
        for entry in entries:
            if not entry.name.isdigit():
                continue
            child = int(entry.name)
            if child in seen:
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            close = stat.rfind(")")
            if close < 0:
                continue
            parts = stat[close + 1 :].split()
            if len(parts) < 2:
                continue
            try:
                ppid = int(parts[1])
            except ValueError:
                continue
            if ppid != current:
                continue
            seen.add(child)
            found.append(child)
            stack.append(child)
    return found


def _kill_process_tree(pid: int) -> list[int]:
    """SIGKILL a process group and every descendant. Does not close Chrome."""
    if pid <= 1 or pid == os.getpid():
        return []
    try:
        if pid == os.getpgrp():
            return []
    except OSError:
        return []
    victims = [pid, *_descendant_pids(pid)]
    killed: list[int] = []
    seen: set[int] = set()
    for item in victims:
        if item in seen or item <= 1 or item == os.getpid():
            continue
        seen.add(item)
        try:
            os.killpg(item, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(item, signal.SIGKILL)
        except OSError:
            pass
        killed.append(item)
    return killed


def kill_agent_processes(job_id: str | None) -> list[int]:
    """SIGKILL every process group started for this job, and its children."""
    if not job_id:
        return []
    with _PROC_LOCK:
        pids = list(_AGENT_PIDS.pop(str(job_id), set()))
    killed: list[int] = []
    for pid in pids:
        killed.extend(_kill_process_tree(pid))
    return killed


def kill_job_recordings(job_id: str | None, store: Any = None) -> list[int]:
    """SIGKILL recording process groups stored for this job.

    The capture process is its own session. ffmpeg and the Playwright node
    are its children. Killing that group does not call browser.close on the
    shared Chrome.
    """
    if not job_id or store is None:
        return []
    db_path = getattr(store, "path", None) or getattr(store, "db_path", None)
    if not db_path:
        return []
    try:
        from .recording import RecordingStore

        recordings = RecordingStore(db_path)
        rows = recordings.list_for_job(job_id)
    except Exception:
        return []
    killed: list[int] = []
    for row in rows:
        stop_file = str(row.get("stop_file") or "")
        if stop_file:
            try:
                Path(stop_file).touch(exist_ok=True)
            except OSError:
                pass
        pid = int(row.get("capture_pid") or 0)
        if pid:
            killed.extend(_kill_process_tree(pid))
        status = str(row.get("status") or "")
        if status in {"STARTING", "RECORDING", "STOPPING"}:
            try:
                from .recording import _now

                recordings.update(
                    row["id"],
                    status="FAILED",
                    failure="stopped",
                    failure_stage="STOP",
                    stopped_at=_now(),
                )
            except Exception:
                pass
    return killed


def record_note_tool_failure(store: Any, job_id: str, message: str) -> None:
    """Remember a built-in note/cert tool failure so the UI path stays closed."""
    if not job_id:
        return
    store.checkpoint(
        job_id,
        "ezlynx_note_tool_failed",
        {"error": str(message or "")[:500]},
    )


def _note_tool_failed(store: Any, job_id: str) -> bool:
    if store is None or not job_id:
        return False
    try:
        note = store.get_checkpoint(job_id, "ezlynx_note_tool_failed")
    except Exception:
        return False
    return bool(note)


def refuse_hand_driven_ezlynx(job: dict[str, Any] | None, store: Any = None) -> str | None:
    """Certificates and policy changes do not fall back to hand-driven screens."""
    if not job:
        return None
    action = str(job.get("action_type") or "")
    payload = dict(job.get("payload") or {})
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("text", "request_text", "prompt")
    ).casefold()
    if action in _HAND_DRIVEN_ACTIONS or _note_tool_failed(store, str(job.get("id") or "")):
        return HAND_DRIVEN_EZLYNX_STOP
    if "certificate of insurance" in text or (
        "mailing address" in text and any(word in text for word in ("change", "update", "correct", "set"))
    ):
        return HAND_DRIVEN_EZLYNX_STOP
    return None


def session_key_from_adapter(adapter: Any, event: Any) -> str:
    """Session key the Hermes gateway used for this message, when it has one.

    The real key comes from the adapter's ``_event_session_key``. A
    ``chat:{id}:{thread}`` fallback does not match ``_active_sessions``, so
    interrupt and cancel would miss the running turn.
    """
    source = getattr(event, "source", None)
    for name, args in (
        ("_event_session_key", (event,)),
        ("_source_session_key", (source,)),
    ):
        method = getattr(adapter, name, None)
        if not callable(method):
            continue
        try:
            key = method(*args)
        except Exception:
            continue
        if key:
            return str(key)
    builder = getattr(adapter, "build_session_key", None)
    if callable(builder):
        for args in (
            (source,),
            (event,),
            (source, getattr(event, "message_id", None)),
        ):
            try:
                key = builder(*args)
            except TypeError:
                continue
            except Exception:
                break
            if key:
                return str(key)
    chat_id = str(getattr(source, "chat_id", "") or "")
    thread_id = str(getattr(source, "thread_id", "") or "")
    return f"chat:{chat_id}:{thread_id}"


STOPPED_OUTPUT = (
    "This job was stopped. Do not send another message or call another tool."
)

BUSY_SESSION_REPLY = "I'm finishing another job, one moment."
NOTHING_RUNNING_REPLY = "Nothing is running right now."


def stop_reply_line(job_id: str) -> str:
    """The only /stop sentence. No audit, no model text."""
    return f"Stopped. That job is cancelled. Ref: job {job_id}"


def job_was_stopped_or_ceiling(store: Any, job: dict | None) -> bool:
    """True when /stop or the time ceiling already ended this job."""
    if not job or store is None:
        return False
    status = str(job.get("status") or "")
    if status in {JobStatus.CANCELLED.value, "CANCELLED"}:
        return True
    if status not in {JobStatus.FAILED.value, "FAILED"}:
        return False
    job_id = str(job.get("id") or "")
    if not job_id:
        return False
    for kind in (
        "agent_abort",
        "cancelled",
        "gateway_turn_timeout",
        "hard_block",
        "waiting_expired",
    ):
        try:
            if store.get_checkpoint(job_id, kind):
                return True
        except Exception:
            continue
    return False


def conversation_must_start_fresh(store: Any, job: dict | None) -> bool:
    """The next Chat message opens a new job after stop, the ceiling, or completion."""
    if not job:
        return False
    status = str(job.get("status") or "")
    if status in {JobStatus.COMPLETE.value, "COMPLETE"}:
        return True
    return job_was_stopped_or_ceiling(store, job)


def chat_turn_keeps_context(db_path: str, job_id: str | None) -> bool:
    """A clarification reply stays on the same job and keeps its transcript."""
    if not job_id or not db_path:
        return False
    try:
        from .store import JobStore

        return bool(JobStore(db_path).get_checkpoint(job_id, "keep_chat_context"))
    except Exception:
        return False


_WAITING_ON_USER = {
    JobStatus.NEEDS_CLARIFICATION.value,
    JobStatus.AWAITING_HUMAN_INPUT.value,
}


def job_is_waiting_on_user(job: dict | None) -> bool:
    """The job has asked the user for something and is not working."""
    if not job:
        return False
    return str(job.get("status") or "") in _WAITING_ON_USER


def release_chat_lock(adapter: Any, chat_id: str, job_id: str | None) -> None:
    """Drop the in-memory chat hold as soon as the reply is out.

    A finished or parked job must not keep the space busy until the
    10-minute stop. Another job still running in this space keeps its
    own session. The job row's lease is released by the status change.
    """
    turns = getattr(adapter, "_gateway_turns", None)
    if isinstance(turns, dict) and job_id:
        for key, record in list(turns.items()):
            if isinstance(record, dict) and str(record.get("job_id") or "") == str(job_id):
                turns.pop(key, None)
    active = getattr(adapter, "_active_chat_job", None)
    if isinstance(active, dict) and job_id and active.get(str(chat_id)) == str(job_id):
        active.pop(str(chat_id), None)
    if not chat_id:
        return
    if isinstance(turns, dict):
        for key, record in turns.items():
            if not isinstance(key, tuple) or str(key[0]) != str(chat_id):
                continue
            if _turn_record_running(record):
                return
    for key in list(_iter_live_session_keys(adapter)):
        if chat_id not in str(key):
            continue
        _release_session_guard(adapter, str(key))
        _drop_runner_session(adapter, str(key))


def _drop_runner_session(adapter: Any, key: str) -> None:
    """Forget a finished turn so the next message is not stuck behind it."""
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None or not key:
        return
    for name in ("_running_agents", "_sessions", "_active_session_leases"):
        mapping = getattr(runner, name, None)
        if isinstance(mapping, dict):
            mapping.pop(key, None)
    for method in ("_drop_turn_slot", "_release_running_agent_state"):
        drop = getattr(runner, method, None)
        if not callable(drop):
            continue
        try:
            drop(key)
        except Exception:
            continue


def busy_session_should_defer(adapter: Any, event: Any, *, db_path: str = "") -> bool:
    """True when a second message must wait.

    A session looks busy while its turn record is still up. A reply in
    the thread of a job that is waiting is never the busy reply, even
    when another job in the space is still running.
    """
    if db_path:
        from .chat_job_controls import inbound_answers_waiting_job

        if inbound_answers_waiting_job(db_path, event):
            return False
    if not session_is_busy(adapter, event):
        return False
    if db_path and _only_questions_are_running(db_path, adapter, event):
        return False
    job_id = running_chat_job_id(adapter, event)
    if not job_id or not db_path:
        return True
    try:
        from .store import JobStore

        job = JobStore(db_path).get_job(job_id)
    except Exception:
        return True
    if job_is_waiting_on_user(job):
        return False
    return True


def _job_is_question_only(db_path: str, job_id: str) -> bool:
    try:
        from .answer_only import is_answer_only_job, is_informational_ask
        from .store import JobStore

        job = JobStore(db_path).get_job(job_id)
    except Exception:
        return False
    payload = dict(job.get("payload") or {})
    text = str(payload.get("text") or payload.get("request_text") or "")
    return is_answer_only_job(job) or is_informational_ask(text)


def _only_questions_are_running(db_path: str, adapter: Any, event: Any) -> bool:
    """A question-only turn does not hold the space for the next message."""
    chat_id, _thread_id = _event_chat_thread(event)
    turns = getattr(adapter, "_gateway_turns", None)
    if not isinstance(turns, dict) or not chat_id:
        return False
    live: list[str] = []
    for key, record in turns.items():
        if not isinstance(key, tuple) or str(key[0]) != chat_id:
            continue
        if not isinstance(record, dict) or not _turn_record_running(record):
            continue
        job_id = str(record.get("job_id") or "")
        if job_id:
            live.append(job_id)
    if not live:
        return False
    return all(_job_is_question_only(db_path, job_id) for job_id in live)


def incoming_message_action(*, session_busy: bool, is_stop: bool) -> str:
    """What to do with a Chat message while a turn may already be running.

    Only /stop (and the ceiling, which is not a message) cancels a job.
    A second message is queued. It is not answered on the same Hermes
    session, because that session's busy mode interrupts the running agent.
    """
    if is_stop:
        return "stop"
    if session_busy:
        return "defer"
    return "run"


def _turn_record_running(record: Any) -> bool:
    if not isinstance(record, dict) or not record.get("job_id"):
        return False
    task = record.get("task")
    watchdog = record.get("watchdog")
    if task is not None and not getattr(task, "done", lambda: True)():
        return True
    if task is None and (
        watchdog is None or not getattr(watchdog, "done", lambda: True)()
    ):
        return True
    return False


def _event_chat_thread(event: Any) -> tuple[str, str]:
    source = getattr(event, "source", None)
    return (
        str(getattr(source, "chat_id", "") or ""),
        str(getattr(source, "thread_id", "") or ""),
    )


def _key_matches_chat(key: str, chat_id: str, thread_id: str) -> bool:
    """True when this live session key is the one for this chat.

    A DM key with no thread suffix is the whole DM. A different thread
    suffix belongs to someone else's turn.
    """
    text = str(key or "")
    if not chat_id or chat_id not in text:
        return False
    suffix = text.split(chat_id, 1)[1].strip(":")
    if not suffix:
        return True
    if thread_id and thread_id in suffix:
        return True
    if thread_id and thread_id not in suffix:
        return False
    return False


def _iter_live_session_keys(adapter: Any) -> list[str]:
    """Session keys the gateway is actually holding, exact strings."""
    found: list[str] = []
    runner = getattr(adapter, "gateway_runner", None)
    if runner is not None:
        sessions = getattr(runner, "_sessions", None)
        if isinstance(sessions, dict):
            found.extend(str(key) for key in sessions)
        items = getattr(runner, "_running_agent_items", None)
        if callable(items):
            try:
                for key, _agent in items() or ():
                    found.append(str(key))
            except Exception:
                pass
        for name in ("_running_agents", "_active_session_leases"):
            mapping = getattr(runner, name, None)
            if isinstance(mapping, dict):
                found.extend(str(key) for key in mapping)
    for name in ("_active_sessions", "_session_tasks", "_pending_messages"):
        mapping = getattr(adapter, name, None)
        if isinstance(mapping, dict):
            found.extend(str(key) for key in mapping)
    unique: list[str] = []
    seen: set[str] = set()
    for key in found:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _running_agent_for_key(adapter: Any, key: str) -> Any:
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None or not key:
        return None
    peek = getattr(runner, "_peek_session_state", None)
    if callable(peek):
        try:
            state = peek(key)
        except Exception:
            state = None
        agent = getattr(getattr(state, "turn", None), "agent", None)
        if agent is not None:
            return agent
    running = getattr(runner, "_running_agents", None)
    if isinstance(running, dict):
        return running.get(key)
    return None


def _key_is_running(adapter: Any, key: str) -> bool:
    if _running_agent_for_key(adapter, key) is not None:
        return True
    active = getattr(adapter, "_active_sessions", None)
    if isinstance(active, dict) and key in active:
        return True
    tasks = getattr(adapter, "_session_tasks", None)
    if isinstance(tasks, dict):
        task = tasks.get(key)
        if task is not None and not getattr(task, "done", lambda: True)():
            return True
    return False


def resolve_running_session_key(adapter: Any, event: Any) -> tuple[str, str]:
    """Return ``(resolved, derived)``.

    ``resolved`` is the key the runner's live agent is under, such as
    ``agent:main:google_chat:dm:spaces/...``. ``derived`` is the adapter
    lookup, which can be the useless ``chat:spaces/...`` fallback.
    """
    derived = session_key_from_adapter(adapter, event)
    chat_id, thread_id = _event_chat_thread(event)
    matches = [
        key
        for key in _iter_live_session_keys(adapter)
        if _key_matches_chat(key, chat_id, thread_id) and _key_is_running(adapter, key)
    ]
    agent_keys = [key for key in matches if str(key).startswith("agent:")]
    pool = agent_keys or matches
    exact = f"agent:main:google_chat:dm:{chat_id}" if chat_id else ""
    if exact and exact in pool:
        return exact, derived
    if pool:
        return pool[0], derived
    return derived, derived


def running_chat_job_id(adapter: Any, event: Any) -> str | None:
    """Job id of the turn already running in this chat, when there is one."""
    turns = getattr(adapter, "_gateway_turns", None)
    if not isinstance(turns, dict):
        return None
    chat_id, thread_id = _event_chat_thread(event)
    wanted = turn_key(chat_id, thread_id)
    record = turns.get(wanted)
    if record is None and thread_id:
        record = turns.get((chat_id, ""))
    if isinstance(record, dict) and record.get("job_id"):
        return str(record["job_id"])
    for key, item in turns.items():
        if not isinstance(item, dict) or not item.get("job_id"):
            continue
        if not isinstance(key, tuple) or str(key[0]) != chat_id:
            continue
        other = str(key[1]) if len(key) > 1 else ""
        if thread_id and other not in {"", thread_id}:
            continue
        if _turn_record_running(item):
            return str(item["job_id"])
    return None


def session_is_busy(adapter: Any, event: Any) -> bool:
    """True when this Chat session already has a live agent turn."""
    resolved, _derived = resolve_running_session_key(adapter, event)
    if resolved and _key_is_running(adapter, resolved):
        chat_id, thread_id = _event_chat_thread(event)
        if _key_matches_chat(resolved, chat_id, thread_id):
            return True
    turns = getattr(adapter, "_gateway_turns", None)
    if not isinstance(turns, dict):
        return False
    source = getattr(event, "source", None)
    wanted = turn_key(
        getattr(source, "chat_id", None),
        getattr(source, "thread_id", None),
    )
    record = turns.get(wanted)
    if record is None and wanted[1]:
        record = turns.get((wanted[0], ""))
    return _turn_record_running(record)


def _release_session_guard(adapter: Any, key: str) -> None:
    """Drop the in-memory hold so the next message is not stuck behind it."""
    for name in ("_active_sessions", "_session_tasks", "_pending_messages"):
        mapping = getattr(adapter, name, None)
        if isinstance(mapping, dict):
            mapping.pop(key, None)


def record_busy_reply(
    store: Any,
    job_id: str | None,
    message_id: str | None,
    text: str,
) -> bool:
    """Prove the busy-session ack was posted. Does not replace the job reply."""
    posted_id = str(message_id or "").strip()
    if store is None or not job_id or not posted_id:
        return False
    store.checkpoint(
        job_id,
        "busy_reply",
        {
            "message_id": posted_id,
            "text": str(text or "")[:2000],
            "kind": "busy",
            "posted": True,
        },
    )
    return True


def record_chat_delivery(
    store: Any,
    job_id: str | None,
    message_id: str | None,
    text: str,
    kind: str,
) -> bool:
    """Prove a stop or ceiling reply was posted. Missing id is not proof."""
    posted_id = str(message_id or "").strip()
    if store is None or not job_id or not posted_id:
        return False
    store.checkpoint(
        job_id,
        "chat_delivery",
        {
            "message_id": posted_id,
            "text": str(text or "")[:2000],
            "kind": str(kind or ""),
            "posted": True,
        },
    )
    existing = None
    try:
        existing = store.get_checkpoint(job_id, "worker_response")
    except Exception:
        existing = None
    if not existing:
        from .worker_contract import sanitize_worker_response

        store.checkpoint(
            job_id,
            "worker_response",
            sanitize_worker_response(store, job_id, str(text or "")[:2000]),
        )
    return True


def sent_message_id(result: Any) -> str:
    """Google message id from a send result. Empty when the post is unproved."""
    direct = getattr(result, "message_id", None)
    if direct:
        return str(direct).strip()
    raw = getattr(result, "raw_response", None)
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("message_id")
        if name:
            return str(name).strip()
    return ""


def refuse_current_tool_call(kwargs: dict | None = None) -> str | None:
    """Block a tool call when this job was stopped or hit the ceiling."""
    job_id = str(
        (kwargs or {}).get("job_id")
        or os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()
    db_path = str(
        (kwargs or {}).get("db_path") or os.environ.get("ROBIE_JOB_DB") or ""
    ).strip()
    store = None
    if job_id and db_path:
        try:
            from .store import JobStore

            store = JobStore(db_path)
        except Exception:
            store = None
    return agent_output_blocked(job_id, store)


def agent_output_blocked(job_id: str | None, store: Any = None) -> str | None:
    """Refuse another send or tool call after /stop or the time ceiling."""
    if not job_id:
        return None
    if agent_stop_requested(job_id):
        return STOPPED_OUTPUT
    if store is None:
        return None
    try:
        job = store.get_job(job_id)
    except Exception:
        return None
    status = str((job or {}).get("status") or "")
    if status in {JobStatus.CANCELLED.value, "CANCELLED"}:
        return STOPPED_OUTPUT
    if status not in {JobStatus.FAILED.value, "FAILED"}:
        return None
    for kind in ("agent_abort", "cancelled", "gateway_turn_timeout"):
        try:
            if store.get_checkpoint(job_id, kind):
                return STOPPED_OUTPUT
        except Exception:
            continue
    return None


async def watch_turn_ceiling(
    agent: Any,
    *,
    limit: float,
    on_timeout: Any,
    stop_requested: Any,
) -> str:
    """Enforce the turn ceiling without holding the Chat message handler.

    The handler starts this as a background task and returns, so /stop and
    other messages are not stuck behind the running agent.
    """
    try:
        if agent is None:
            await asyncio.sleep(limit)
        else:
            await asyncio.wait_for(asyncio.shield(agent), timeout=limit)
    except asyncio.TimeoutError:
        if stop_requested():
            return "stopped"
        result = on_timeout()
        if inspect.isawaitable(result):
            await result
        return "timeout"
    except asyncio.CancelledError:
        # /stop cancels this watchdog. Do not also send the ceiling reply.
        raise
    if stop_requested():
        return "stopped"
    return "finished"


def running_agent_task(adapter: Any, event: Any, *, before: set | None = None) -> Any:
    """The background agent task. handle_message returns before this finishes."""
    key = session_key_from_adapter(adapter, event)
    tasks = getattr(adapter, "_session_tasks", None) or {}
    task = tasks.get(key) if isinstance(tasks, dict) else None
    if task is not None and not getattr(task, "done", lambda: False)():
        return task
    background = getattr(adapter, "_background_tasks", None) or set()
    try:
        current = set(background)
    except TypeError:
        current = set()
    prior = set(before or ())
    fresh = [
        item
        for item in current - prior
        if item is not None and not getattr(item, "done", lambda: True)()
    ]
    if len(fresh) == 1:
        return fresh[0]
    return None


def _capture_session_task(adapter: Any, event: Any, key: str) -> Any:
    """Read the agent task before cancel removes it from the session map."""
    tasks = getattr(adapter, "_session_tasks", None)
    if isinstance(tasks, dict):
        task = tasks.get(key)
        if task is not None and not getattr(task, "done", lambda: False)():
            return task
    return running_agent_task(adapter, event)


async def _await_interrupt(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


def _hard_stop_agent(agent: Any) -> None:
    """Stop a live agent object even if the session-key interrupt missed."""
    if agent is None:
        return
    interrupt = getattr(agent, "interrupt", None)
    if callable(interrupt):
        try:
            interrupt("stopped")
        except TypeError:
            try:
                interrupt()
            except Exception:
                pass
        except Exception:
            pass
    if hasattr(agent, "alive"):
        try:
            agent.alive = False
        except Exception:
            pass


def resolve_stop_target(
    turn_record: Any,
    *,
    session_busy: bool,
) -> tuple[str | None, str | None]:
    """Return ``(job_id, idle_reply)``.

    ``/stop`` with nothing running must not look up a finished conversation
    link. ``idle_reply`` is set only in that case.
    """
    job_id = None
    if isinstance(turn_record, dict) and turn_record.get("job_id"):
        job_id = str(turn_record["job_id"])
    if job_id or session_busy:
        return job_id, None
    return None, NOTHING_RUNNING_REPLY


def _signal_wait(waiter: Any) -> None:
    """Unblock a parked future or threading event. Does not enqueue a message."""
    if waiter is None:
        return
    setter = getattr(waiter, "set", None)
    if callable(setter):
        try:
            setter()
        except Exception:
            pass
    cancel = getattr(waiter, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            pass


def _unblock_parked_waits(adapter: Any, agent: Any, keys: list[str]) -> None:
    """End a clarify or model wait the asyncio cancel does not reach.

    The agent thread blocks on ``tools.clarify_gateway.wait_for_response``
    (a threading.Event polled once a second). Task.cancel() does not set
    that event, so the session lease keeps renewing until the next Chat
    message wakes the old agent.
    """
    abort = getattr(agent, "_active_request_abort", None)
    if callable(abort):
        try:
            abort("stopped")
        except Exception:
            pass
    for name in (
        "_clarify_event",
        "clarify_event",
        "_pending_future",
        "pending_future",
    ):
        _signal_wait(getattr(agent, name, None))
    runner = getattr(adapter, "gateway_runner", None)
    for owner in (runner, adapter):
        if owner is None:
            continue
        parked = getattr(owner, "_parked_waits", None)
        if not isinstance(parked, dict):
            continue
        for key in keys:
            _signal_wait(parked.get(key))
    hook = getattr(adapter, "clear_pending_clarify", None)
    if callable(hook):
        for key in keys:
            try:
                hook(key)
            except Exception:
                pass
    try:
        from tools.clarify_gateway import clear_session as clear_clarify
    except Exception:
        clear_clarify = None
    if clear_clarify is None:
        return
    for key in keys:
        if not key:
            continue
        try:
            clear_clarify(key)
        except Exception:
            logger.debug("clarify clear failed for %s", key, exc_info=True)


def _release_session_leases(adapter: Any, keys: list[str]) -> None:
    """Drop the turn lease now. Do not wait for the parked thread to notice."""
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        return
    mapping = getattr(runner, "_active_session_leases", None)
    if isinstance(mapping, dict):
        for key in keys:
            lease = mapping.pop(key, None)
            release = getattr(lease, "release", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    logger.debug("session lease release failed", exc_info=True)
    for name in ("_turn_lease_registry", "turn_leases"):
        registry = getattr(runner, name, None)
        release_fn = getattr(registry, "release_session", None)
        if not callable(release_fn):
            continue
        for key in keys:
            try:
                release_fn(key)
            except Exception:
                logger.debug("turn lease release failed", exc_info=True)


def _close_session_history(
    adapter: Any,
    keys: list[str],
    *,
    chat_id: str = "",
    thread_id: str = "",
) -> None:
    """Close this Chat session's transcript so the next message stands alone.

    Chat DM turns share one Hermes session. After /stop or the ceiling the
    dying turn writes ``Operation interrupted.`` into that transcript, and
    the next question is answered from the old conversation. A new session
    id (or an emptied transcript) is the boundary.
    """
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        return
    history = getattr(runner, "_session_history", None)
    if isinstance(history, dict):
        for key in list(history):
            if key in keys or _key_matches_chat(key, chat_id, thread_id):
                history[key] = []
    store = getattr(runner, "session_store", None)
    reset = getattr(store, "reset_session", None) if store is not None else None
    if callable(reset):
        reset_keys = list(keys)
        entries = getattr(store, "_entries", None)
        if isinstance(entries, dict):
            for key in entries:
                if key not in reset_keys and _key_matches_chat(key, chat_id, thread_id):
                    reset_keys.append(key)
        for key in reset_keys:
            if not key:
                continue
            try:
                reset(key)
            except Exception:
                logger.debug("session reset failed for %s", key, exc_info=True)
    for key in keys:
        agent = _running_agent_for_key(adapter, key)
        messages = getattr(agent, "messages", None)
        if isinstance(messages, list):
            messages.clear()
        clear = getattr(agent, "clear_history", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                pass


def fresh_turn_history(adapter: Any, event: Any) -> None:
    """A new Chat message is answered on its own, without prior Q&A."""
    resolved, derived = resolve_running_session_key(adapter, event)
    chat_id, thread_id = _event_chat_thread(event)
    keys: list[str] = []
    for key in (resolved, derived):
        if key and key not in keys:
            keys.append(key)
    exact = f"agent:main:google_chat:dm:{chat_id}" if chat_id else ""
    if exact and exact not in keys:
        keys.append(exact)
    _close_session_history(adapter, keys, chat_id=chat_id, thread_id=thread_id)


def messages_for_chat_turn(prior: Any, text: str) -> list[dict[str, str]]:
    """Prior Q&A is not part of a Chat DM turn. ``prior`` is ignored on purpose."""
    del prior
    body = str(text or "").strip()
    if not body:
        return []
    return [{"role": "user", "content": body}]


def _tasks_for_keys(adapter: Any, keys: list[str]) -> list[Any]:
    tasks = []
    mapping = getattr(adapter, "_session_tasks", None)
    if not isinstance(mapping, dict):
        return tasks
    for key in keys:
        task = mapping.get(key)
        if task is not None and task not in tasks:
            tasks.append(task)
    return tasks


async def terminate_gateway_agent(
    adapter: Any,
    event: Any,
    job_id: str | None,
    *,
    reason: str,
    store: Any = None,
) -> None:
    """Stop the agent under the key it is actually running, and release the lease.

    The derived ``chat:spaces/...`` key does not match
    ``agent:main:google_chat:dm:spaces/...``. Both keys are logged. The
    asyncio task is cancelled and the turn lease is dropped even if the
    runner interrupt cannot finish in-process.
    """
    request_agent_stop(job_id)
    resolved, derived = resolve_running_session_key(adapter, event)
    chat_id, _thread_id = _event_chat_thread(event)
    logger.warning(
        "stop session keys resolved=%s derived=%s chat=%s reason=%s",
        resolved,
        derived,
        chat_id,
        reason,
    )
    if store is not None and job_id:
        try:
            store.checkpoint(
                job_id,
                "agent_abort",
                {
                    "reason": str(reason or "")[:300],
                    "session_key": resolved,
                    "derived_session_key": derived,
                },
            )
            store.checkpoint(
                job_id,
                "session_stop_keys",
                {
                    "resolved": resolved,
                    "derived": derived,
                    "reason": str(reason or "")[:300],
                },
            )
        except Exception:
            pass
        db_path = getattr(store, "path", None) or getattr(store, "db_path", None)
        if db_path:
            try:
                from .chat_guard import stop_generic_chat_job_heartbeat

                stop_generic_chat_job_heartbeat(str(db_path), str(job_id))
            except Exception:
                pass
    source = getattr(event, "source", None)
    keys = [resolved]
    if derived and derived not in keys:
        keys.append(derived)
    for key in _iter_live_session_keys(adapter):
        if _key_matches_chat(key, chat_id, _thread_id) and key not in keys:
            keys.append(key)
    tasks = _tasks_for_keys(adapter, keys)
    captured = _capture_session_task(adapter, event, resolved)
    if captured is not None and captured not in tasks:
        tasks.append(captured)
    agent = _running_agent_for_key(adapter, resolved)
    _hard_stop_agent(agent)
    # The clarify/question wait lives on a worker thread. Cancel the
    # awaiting event now, and drop the lease before that thread notices.
    _unblock_parked_waits(adapter, agent, keys)
    _release_session_leases(adapter, keys)
    runner = getattr(adapter, "gateway_runner", None)
    clear = getattr(runner, "_interrupt_and_clear_session", None) if runner is not None else None
    if callable(clear):
        try:
            result = clear(
                resolved,
                source,
                interrupt_reason=reason,
                invalidation_reason=reason,
            )
        except TypeError:
            try:
                result = clear(resolved, source)
            except Exception:
                result = None
        except Exception:
            result = None
        await _await_interrupt(result)
    else:
        interrupt = getattr(adapter, "interrupt_session_activity", None)
        if callable(interrupt):
            try:
                result = interrupt(resolved, chat_id)
            except TypeError:
                try:
                    result = interrupt(resolved)
                except Exception:
                    result = None
            except Exception:
                result = None
            await _await_interrupt(result)
    cancel = getattr(adapter, "cancel_session_processing", None)
    if callable(cancel):
        try:
            result = cancel(resolved)
            await _await_interrupt(result)
        except Exception:
            pass
    if runner is not None:
        drop = getattr(runner, "_drop_turn_slot", None)
        if callable(drop):
            try:
                drop(resolved)
            except Exception:
                pass
        release = getattr(runner, "_release_running_agent_state", None)
        if callable(release):
            try:
                release(resolved)
            except Exception:
                pass
    kill_agent_processes(job_id)
    kill_job_recordings(job_id, store)
    current = None
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    for task in tasks:
        if task is None or task is current:
            continue
        cancel_task = getattr(task, "cancel", None)
        if callable(cancel_task) and not getattr(task, "done", lambda: False)():
            cancel_task()
        if inspect.isawaitable(task):
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
    _close_session_history(adapter, keys, chat_id=chat_id, thread_id=_thread_id)
    for key in keys:
        _release_session_guard(adapter, key)


def fail_cancelled_chat_job(store: Any, job_id: str) -> str:
    """Mark the linked running job FAILED/cancelled. Does not open a new job.

    A job that is already finished is left untouched. /stop with nothing
    running must not add a cancelled checkpoint or replay that job's answer.
    """
    if not job_id:
        return NOTHING_RUNNING_REPLY
    job = store.get_job(job_id)
    status = JobStatus(job["status"])
    if status in TERMINAL_STATUSES:
        return NOTHING_RUNNING_REPLY
    reply = stop_reply_line(job_id)
    store.transition(
        job_id,
        JobStatus.CANCELLED,
        expected={status},
        error="Cancelled.",
        release_lease=True,
    )
    store.checkpoint(
        job_id,
        "cancelled",
        {"by": "/stop", "reason": "Cancelled."},
    )
    return reply
