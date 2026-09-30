"""Chat turn ceiling and /stop.

hermes-agent's gateway/run.py only watches idle time
(``agent.gateway_timeout``, default 1800 seconds). This module is the
total-time ceiling for a Chat turn. It reads ``agent.gateway_max_turn_seconds``
and does not change ``max_turns`` (the email agent shares config.yaml).

The email ~10 minute stop is separate: ``email_agent_runner`` chat timeout,
default 600 seconds, inside the watcher's 930 second process limit.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

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


def fail_cancelled_chat_job(store: Any, job_id: str) -> str:
    """Mark the linked job FAILED/cancelled. Does not open a new job."""
    if not job_id:
        return "Stopped. There isn't a running job in this thread."
    job = store.get_job(job_id)
    reply = f"Stopped. That job is cancelled.\n\nRef: job {job_id}"
    status = JobStatus(job["status"])
    if status not in TERMINAL_STATUSES:
        store.transition(
            job_id,
            JobStatus.FAILED,
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
