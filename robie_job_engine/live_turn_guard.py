"""A live Chat turn keeps its own job, even after a later job starts.

Process env ``ROBIE_JOB_ID`` is one slot. A second message overwrites it.
The turn that is already running still has to write, stop, and time out
as itself: a cancelled job must not file a note, and the note must not be
credited to the newer job.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
from typing import Any

from .models import JobStatus

_TURN_JOB: contextvars.ContextVar[str] = contextvars.ContextVar(
    "robie_turn_job_id", default=""
)
_AGENT_JOBS: dict[int, str] = {}

CLARIFY_PENDING = "clarify_pending"
CLARIFY_REPLY_INJECTED = "clarify_reply_injected"
CLARIFY_TIMEOUT = "clarify_timeout"

WRITE_REFUSED = "EZLYNX_WRITE_REFUSED"
APPLICANT_UNTRUSTED = "EZLYNX_APPLICANT_UNTRUSTED"
FILE_SEARCH_BLOCKED = "FILE_SEARCH_BLOCKED"

CLARIFY_TIMEOUT_STOP = (
    "STOP. The user did not answer. Do not choose a discussion. "
    "Do not write a note or a document."
)
FILE_SEARCH_STOP = (
    "FILE_SEARCH_BLOCKED: do not resolve an applicant from repo, fixture, "
    "or release files. Use a live EZLynx name search or an explicit applicant id."
)

_TIMEOUT = re.compile(
    r"did not respond|clarify timed out|no response within|user did not answer",
    re.IGNORECASE,
)
_FILE_TOOLS = frozenset(
    {
        "search_files",
        "grep",
        "glob",
        "read_file",
        "list_files",
        "codebase_search",
    }
)
_FORBIDDEN_PATH = re.compile(
    r"(?:^|[/\\])(?:fixtures?|tests|testdata|releases)(?:[/\\]|$)",
    re.IGNORECASE,
)
_PERSON = re.compile(
    r"\b(?:for|about|named)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b"
)
_PERSON_LOOSE = re.compile(
    r"\b(?:for|about|named)\s+([a-z][a-z']+(?:\s+[a-z][a-z']+)+)\b",
    re.IGNORECASE,
)
_NOT_A_NAME = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "please",
        "robie",
        "client",
        "account",
        "applicant",
    }
)
_ANSWERED = re.compile(r"(?i)^answered\b[.!:]?\s*")
_STASHED_REPLIES: dict[str, str] = {}


def stash_clarify_reply(job_id: str | None, text: str) -> None:
    ident = str(job_id or "").strip()
    reply = str(text or "").strip()
    if ident and reply:
        _STASHED_REPLIES[ident] = reply


def take_stashed_clarify_reply(job_id: str | None) -> str:
    return str(_STASHED_REPLIES.pop(str(job_id or "").strip(), "") or "")


def bind_turn_owner(agent: Any, job_id: str | None) -> None:
    """Remember which job this agent object is working for."""
    ident = str(job_id or "").strip()
    if agent is None or not ident:
        return
    _AGENT_JOBS[id(agent)] = ident
    try:
        agent._robie_turn_job_id = ident
    except Exception:
        pass


def set_turn_job(job_id: str | None) -> contextvars.Token:
    """Bind the job for the tool calls on this thread. Returns the reset token."""
    return _TURN_JOB.set(str(job_id or "").strip())


def reset_turn_job(token: contextvars.Token) -> None:
    try:
        _TURN_JOB.reset(token)
    except Exception:
        pass


def turn_owner_job_id(agent: Any = None) -> str:
    """The job that owns this turn. Empty when this call is not inside one."""
    if agent is not None:
        stamped = str(getattr(agent, "_robie_turn_job_id", "") or "").strip()
        if not stamped:
            stamped = str(_AGENT_JOBS.get(id(agent), "") or "").strip()
        if stamped:
            return stamped
    current = str(_TURN_JOB.get() or "").strip()
    return current


def stamp_agent_once(agent: Any) -> str:
    """First tool call captures the env job. A later job must not replace it."""
    existing = turn_owner_job_id(agent)
    if existing:
        return existing
    ident = str(
        os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("ROBIE_CURRENT_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()
    if ident:
        bind_turn_owner(agent, ident)
    return ident


def acting_job_id(kwargs: dict | None = None, agent: Any = None) -> str:
    """Prefer the turn owner over the process env a newer job just wrote."""
    owner = turn_owner_job_id(agent)
    if owner:
        return owner
    values = dict(kwargs or {})
    return str(
        values.get("job_id")
        or os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()


def acting_db_path(kwargs: dict | None = None) -> str:
    return str(
        (kwargs or {}).get("db_path") or os.environ.get("ROBIE_JOB_DB") or ""
    ).strip()


def refuse_unless_running(store: Any, job_id: str | None) -> str | None:
    """Re-read the job. Anything except RUNNING is not allowed to write."""
    ident = str(job_id or "").strip()
    if store is None or not ident:
        return None
    try:
        job = store.get_job(ident)
    except Exception:
        return None
    status = str((job or {}).get("status") or "")
    if status == JobStatus.RUNNING.value:
        return None
    from .chat_turn_control import request_agent_stop

    request_agent_stop(ident)
    shown = status or "missing"
    return (
        f"{WRITE_REFUSED}: job {ident} is {shown}, not RUNNING. "
        "The write was not sent."
    )


def refuse_live_write(kwargs: dict | None = None) -> str | None:
    """Status check used at the last moment before an EZLynx write."""
    job_id = acting_job_id(kwargs)
    db_path = acting_db_path(kwargs)
    if not job_id or not db_path:
        return None
    try:
        from .store import JobStore

        store = JobStore(db_path)
    except Exception:
        return None
    return refuse_unless_running(store, job_id)


def assert_live_write_allowed(kwargs: dict | None = None) -> None:
    """Raise when this turn's job is not RUNNING. Call this before the HTTP write."""
    refused = refuse_live_write(kwargs)
    if refused:
        raise RuntimeError(refused)


def contains_clarify_timeout(value: Any) -> bool:
    """True when a clarify wait gave up and handed control back to the model."""
    if isinstance(value, str):
        return _TIMEOUT.search(value) is not None
    if isinstance(value, dict):
        return any(contains_clarify_timeout(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_clarify_timeout(item) for item in value)
    return False


def neutralize_clarify_timeout(value: Any) -> Any:
    """Replace a timeout payload so the model is told to stop, not to choose."""
    if isinstance(value, str):
        if _TIMEOUT.search(value):
            return CLARIFY_TIMEOUT_STOP
        return value
    if isinstance(value, list):
        return [neutralize_clarify_timeout(item) for item in value]
    if isinstance(value, tuple):
        return tuple(neutralize_clarify_timeout(item) for item in value)
    if isinstance(value, dict):
        return {key: neutralize_clarify_timeout(item) for key, item in value.items()}
    return value


def end_job_after_clarify_timeout(store: Any, job_id: str | None) -> str:
    """A clarify timeout ends the job. It does not pick a discussion."""
    ident = str(job_id or "").strip()
    if store is None or not ident:
        return CLARIFY_TIMEOUT_STOP
    from .chat_turn_control import request_agent_stop

    request_agent_stop(ident)
    try:
        job = store.get_job(ident)
    except Exception:
        return CLARIFY_TIMEOUT_STOP
    status = JobStatus(job["status"])
    if status not in {JobStatus.RUNNING, JobStatus.NEEDS_CLARIFICATION, JobStatus.PENDING}:
        store.checkpoint(
            ident,
            CLARIFY_TIMEOUT,
            {"ended": True, "status": status.value, "chose": False},
        )
        return CLARIFY_TIMEOUT_STOP
    try:
        store.transition(
            ident,
            JobStatus.FAILED,
            expected={status},
            error="clarify timed out; the job did not choose a discussion",
            release_lease=True,
        )
    except Exception:
        pass
    store.checkpoint(
        ident,
        CLARIFY_TIMEOUT,
        {"ended": True, "chose": False, "status": JobStatus.FAILED.value},
    )
    return CLARIFY_TIMEOUT_STOP


def person_name_in_text(text: str) -> str | None:
    """A person's name in the request. A company suffix is not a person."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    match = _PERSON.search(raw) or _PERSON_LOOSE.search(raw)
    if not match:
        return None
    name = " ".join(match.group(1).split())
    tokens = [token.strip(".,") for token in name.split() if token.strip(".,")]
    if len(tokens) < 2:
        return None
    if any(token.casefold() in _NOT_A_NAME for token in tokens):
        return None
    if any(token.casefold() in {"llc", "inc", "corp", "ltd"} for token in tokens):
        return None
    return " ".join(tokens)


def person_named_in_job(job: dict[str, Any] | None) -> str | None:
    payload = dict((job or {}).get("payload") or {})
    for key in ("request_text", "text", "prompt", "original_text"):
        name = person_name_in_text(str(payload.get(key) or ""))
        if name:
            return name
    return None


def name_placed_in_applicant_id(value: Any) -> str | None:
    """A person's name stored where an applicant id belongs."""
    text = " ".join(str(value or "").split()).strip()
    if not text or text.isdigit():
        return None
    if not re.search(r"[A-Za-z]", text):
        return None
    tokens = [token for token in re.findall(r"[A-Za-z][A-Za-z']+", text)]
    if len(tokens) < 2:
        return None
    return " ".join(tokens)


def applicant_ids_on_open_tabs(store: Any, job_id: str) -> list[str]:
    """Applicant ids seen on this job's browser pages. Not a trusted source."""
    from .chat_destination_binding import extract_ezlynx_urls
    from .ezlynx_write_scope import applicant_id_from_ezlynx_url

    found: list[str] = []
    try:
        rows = store.list_playwright_exec(str(job_id or ""))
    except Exception:
        rows = []
    for url in extract_ezlynx_urls(rows):
        applicant = str(applicant_id_from_ezlynx_url(url) or "").strip()
        if applicant and applicant not in found:
            found.append(applicant)
    return found


def refuse_tab_applicant(
    store: Any,
    job: dict[str, Any] | None,
    applicant_id: str,
) -> str | None:
    """Refuse an account id copied from the open browser tab."""
    applicant = str(applicant_id or "").strip()
    if store is None or not job or not applicant:
        return None
    from .client_name_lookup import trusted_applicant_ids

    if applicant in set(trusted_applicant_ids(store, job)):
        return None
    if applicant not in applicant_ids_on_open_tabs(store, str(job.get("id") or "")):
        return None
    return (
        f"{APPLICANT_UNTRUSTED}: applicant {applicant} came from the open "
        "browser tab. Use an id from the user's message or this job's "
        "client lookup. Do not take it from the tab."
    )


def refuse_untrusted_applicant(
    store: Any,
    job: dict[str, Any] | None,
    applicant_id: str,
) -> str | None:
    """An account id must be this job's name search or an id the user typed."""
    applicant = str(applicant_id or "").strip()
    if store is None or not job or not applicant:
        return None
    if not person_named_in_job(job):
        return None
    from .client_name_lookup import trusted_applicant_ids

    trusted = set(trusted_applicant_ids(store, job))
    # A payload id is not enough. The user has to have typed it, or a live
    # name search on this job has to have bound it. A fixture id stuffed
    # onto the payload (220250093) stays untrusted when the ask only
    # names a person.
    if applicant in trusted:
        return None
    name = person_named_in_job(job) or "that client"
    return (
        f"{APPLICANT_UNTRUSTED}: applicant {applicant} did not come from a live "
        f"EZLynx name search or an explicit id. Search EZLynx for {name}. "
        "Do not use an id from repo, fixture, or release files. "
        "Do not ask the user for the applicant id."
    )


def refuse_account_file_search(tool_name: str, arguments: Any = None) -> str | None:
    """Keep fixtures and old release trees out of account resolution."""
    name = str(tool_name or "").strip()
    if name not in _FILE_TOOLS:
        return None
    if name == "search_files":
        return FILE_SEARCH_STOP
    blob = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
    if _FORBIDDEN_PATH.search(str(blob or "")):
        return FILE_SEARCH_STOP
    return None


def iter_tool_calls(value: Any) -> list[tuple[str, Any]]:
    """(name, arguments) pairs from a Hermes tool-call payload."""
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("tool") or "").strip()
        if name and any(key in value for key in ("arguments", "args", "parameters")):
            found.append(
                (
                    name,
                    value.get("arguments", value.get("args", value.get("parameters"))),
                )
            )
        for item in value.values():
            found.extend(iter_tool_calls(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(iter_tool_calls(item))
    return found


def blocked_file_search(value: Any) -> str | None:
    for name, arguments in iter_tool_calls(value):
        refused = refuse_account_file_search(name, arguments)
        if refused:
            return refused
    return None


def note_clarify_pending(
    store: Any,
    job_id: str | None,
    question: str = "",
    *,
    session_key: str = "",
) -> None:
    """The turn is waiting on the user. A thread reply belongs to this job."""
    ident = str(job_id or "").strip()
    if store is None or not ident:
        return
    store.checkpoint(
        ident,
        CLARIFY_PENDING,
        {
            "open": True,
            "question": str(question or "")[:500],
            "session_key": str(session_key or ""),
        },
    )


def _thread_matches(store: Any, job: dict[str, Any], inbound_thread_id: str | None) -> bool:
    from .chat_thread import read_job_chat_thread, thread_resource_name

    inbound = thread_resource_name(inbound_thread_id)
    if not inbound:
        return False
    stored = read_job_chat_thread(store, str(job.get("id") or ""))
    return bool(stored and stored == inbound)


def clarify_job_for_reply(
    store: Any,
    text: str,
    inbound_thread_id: str | None,
) -> dict[str, Any] | None:
    """The in-thread job a clarify reply should wake. A new topic is not one."""
    del text
    if store is None:
        return None
    from .chat_job_controls import list_jobs_in_status

    statuses = {
        JobStatus.NEEDS_CLARIFICATION.value,
        JobStatus.RUNNING.value,
        JobStatus.PENDING.value,
    }
    for job in list_jobs_in_status(store, statuses):
        if not _thread_matches(store, job, inbound_thread_id):
            continue
        pending = {}
        try:
            pending = dict(store.get_checkpoint(str(job.get("id") or ""), CLARIFY_PENDING) or {})
        except Exception:
            pending = {}
        if pending.get("open"):
            return job
        if str(job.get("status") or "") == JobStatus.NEEDS_CLARIFICATION.value:
            return job
    return None


def accept_clarify_thread_reply(
    store: Any,
    job: dict[str, Any] | None,
    text: str,
    message_id: str,
) -> bool:
    """Deliver a thread reply into the waiting job. Do not open another session.

    True when the live turn is still the one that asked. The caller must not
    start a new agent for this message.
    """
    if store is None or not job:
        return False
    job_id = str(job.get("id") or "")
    if not job_id:
        return False
    pending = {}
    try:
        pending = dict(store.get_checkpoint(job_id, CLARIFY_PENDING) or {})
    except Exception:
        pending = {}
    status = str(job.get("status") or "")
    live = bool(pending.get("open")) or status == JobStatus.RUNNING.value
    if not live and status != JobStatus.NEEDS_CLARIFICATION.value:
        return False
    if not pending.get("open") and status == JobStatus.NEEDS_CLARIFICATION.value:
        # The parked job has no live turn. The existing resume path runs it.
        return False
    reply = str(text or "").strip()
    payload = dict(job.get("payload") or {})
    payload["clarification_reply"] = reply
    store.update_payload(job_id, payload)
    store.checkpoint(
        job_id,
        "clarification_reply",
        {"message_id": message_id, "text": reply},
    )
    store.checkpoint(
        job_id,
        "keep_chat_context",
        {"reason": "clarify_reply"},
    )
    store.checkpoint(
        job_id,
        CLARIFY_PENDING,
        {
            "open": False,
            "reply": reply,
            "session_key": str(pending.get("session_key") or ""),
        },
    )
    store.checkpoint(
        job_id,
        CLARIFY_REPLY_INJECTED,
        {"message_id": message_id, "text": reply},
    )
    if status == JobStatus.NEEDS_CLARIFICATION.value:
        try:
            store.transition(
                job_id,
                JobStatus.RUNNING,
                expected={JobStatus.NEEDS_CLARIFICATION},
                release_lease=True,
            )
        except Exception:
            pass
    from .chat_turn_control import clear_agent_stop

    clear_agent_stop(job_id)
    stash_clarify_reply(job_id, reply)
    _wake_parked_clarify(store, job_id)
    return True


def _wake_parked_clarify(store: Any, job_id: str) -> None:
    """Unblock the clarify wait so the stashed reply can take its place."""
    try:
        from .chat_thread import read_job_chat_thread
        from .chat_turn_control import (
            _CHAT_ADAPTERS,
            _iter_live_session_keys,
            _key_matches_stop,
            _running_agent_for_key,
            _unblock_parked_waits,
        )
    except Exception:
        return
    try:
        thread = str(read_job_chat_thread(store, job_id) or "")
        payload = dict(store.get_job(job_id).get("payload") or {})
    except Exception:
        return
    chat_id = str(payload.get("conversation_id") or "")
    for adapter in list(_CHAT_ADAPTERS):
        keys = [
            key
            for key in _iter_live_session_keys(adapter)
            if chat_id and _key_matches_stop(key, chat_id, thread)
        ]
        agent = None
        for key in keys:
            agent = _running_agent_for_key(adapter, key)
            if agent is not None:
                break
        if keys:
            _unblock_parked_waits(adapter, agent, keys)


def format_complete_answer(content: str) -> str:
    """The model's answer, short enough to read, without an Answered prefix.

    Later sentences stay. A policy number in the second sentence is part of
    the answer.
    """
    from .user_reply import format_user_reply

    raw = _ANSWERED.sub("", str(content or "").strip()).strip()
    if not raw:
        return ""
    shown = format_user_reply(raw, collapse=False)
    shown = _ANSWERED.sub("", str(shown or "").strip()).strip()
    # The whole answer is delivered. Chat splits it into 4000-character
    # messages. Cutting it here dropped every paragraph after the first.
    return shown
