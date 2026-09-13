"""Resume a parked Chat HITL job from a same-thread reply.

Same rule as email: a Coverage A–F / RETRY reply in the Robie Chat HITL
thread must resume the waiting hermes.google_chat_task. Do not mint a
second Chat job. Do not invent an omitted letter. Fill goes through
ezlynx_policy_setup, not playwright_exec.
"""

from __future__ import annotations

from typing import Any

from .models import JobStatus
from .store import JobStore


def is_coverage_amount_reply(text: str) -> bool:
    """True when the reply states at least one Coverage A–F dollar amount."""
    from .policy_setup_dispatch import parse_coverage_amounts_from_reply

    return bool(parse_coverage_amounts_from_reply(text))


def is_chat_coverage_hitl_job(store: JobStore, job: dict[str, Any]) -> bool:
    """True when this parked Chat job is a coverage / policy-setup HITL."""
    from .policy_setup_dispatch import (
        POLICY_SETUP_REQUIRED_KIND,
        is_coverage_fill_miss,
    )

    if is_coverage_fill_miss(str(job.get("last_error") or "")):
        return True
    job_id = str(job.get("id") or "")
    if not job_id:
        return False
    marker = store.get_checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND) or {}
    if marker:
        return True
    action = store.get_checkpoint(job_id, "action") or {}
    return str(action.get("action") or "") == "ezlynx_policy_setup"


def is_chat_coverage_hitl_resume_reply(
    store: JobStore,
    job: dict[str, Any],
    text: str,
) -> bool:
    """RETRY or stated A–F amounts on a coverage HITL. Not a new Chat job."""
    from .engine import is_retry_text

    if is_coverage_amount_reply(text):
        return True
    return is_retry_text(text) and is_chat_coverage_hitl_job(store, job)


def find_parked_chat_hitl_job(
    store: JobStore,
    conversation_id: str,
    queue: Any | None = None,
) -> dict[str, Any] | None:
    """Find the AWAITING_HUMAN_INPUT Chat job this thread reply belongs to."""
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return None
    if queue is not None:
        context = queue.active_conversation_job(conversation_id) or {}
        state = dict(context.get("interaction_state") or {})
        job_id = str(context.get("job_id") or "").strip()
        if job_id and state.get("awaiting") == "human_input":
            try:
                job = store.get_job(job_id)
            except KeyError:
                job = None
            if (
                job is not None
                and str(job.get("action_type") or "") == "hermes.google_chat_task"
                and JobStatus(job["status"]) == JobStatus.AWAITING_HUMAN_INPUT
            ):
                return job
    matches: list[dict[str, Any]] = []
    for row in store.list_jobs_by_status({JobStatus.AWAITING_HUMAN_INPUT}):
        if str(row.get("action_type") or "") != "hermes.google_chat_task":
            continue
        payload = dict(row.get("payload") or {})
        if str(payload.get("conversation_id") or "").strip() == conversation_id:
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    return None


def ingest_chat_hitl_reply(
    store: JobStore,
    *,
    job_id: str,
    message_id: str,
    text: str,
) -> dict[str, Any]:
    """Merge the Chat reply onto the parked job and resume it. No second job."""
    from .email_hitl import missing_coverage_letters
    from .engine import is_retry_text
    from .policy_setup_dispatch import parse_coverage_amounts_from_reply

    job = store.get_job(job_id)
    if str(job.get("action_type") or "") != "hermes.google_chat_task":
        raise RuntimeError(f"job {job_id} is not a Chat task")
    reply = str(text or "")
    amounts = parse_coverage_amounts_from_reply(reply)
    payload = dict(job.get("payload") or {})
    human = dict(payload.get("human_input_values") or {})
    coverage = dict(human.get("coverage") or {})
    coverage.update(amounts)
    human["coverage"] = coverage
    if is_retry_text(reply):
        human["operator_response"] = "RETRY"
    else:
        human["operator_response"] = reply
    payload["human_input_values"] = human
    payload["hitl_resume"] = True
    payload["hitl_reply_text"] = reply
    payload["hitl_reply_message_id"] = str(message_id or "").strip()
    store.update_payload(job_id, payload)
    store.checkpoint(
        job_id,
        "human_input_resume",
        {
            "channel": "chat",
            "message_id": str(message_id or "").strip(),
            "coverage_keys": sorted(coverage.keys()),
            "missing_letters": missing_coverage_letters(coverage),
        },
    )
    if JobStatus(job["status"]) == JobStatus.AWAITING_HUMAN_INPUT:
        resumed = store.resume(job_id)
    else:
        resumed = store.get_job(job_id)
    return {
        "resumed": True,
        "job_id": job_id,
        "coverage": coverage,
        "missing_letters": missing_coverage_letters(coverage),
        "status": resumed["status"],
    }


def run_chat_hitl_coverage_resume(db_path: str, job_id: str) -> str | None:
    """Apply HITL amounts through ezlynx_policy_setup. None if not a resume.

    FormEntry may already be open. Do not start playwright_exec.
    Omitted letters park honest Chat HITL and are never invented.
    """
    store = JobStore(db_path)
    job = store.get_job(job_id)
    payload = dict(job.get("payload") or {})
    if not payload.get("hitl_resume"):
        return None
    if str(job.get("action_type") or "") != "hermes.google_chat_task":
        return None
    from .email_hitl import apply_hitl_coverage_fill
    from .policy_setup_dispatch import is_coverage_fill_miss

    text = apply_hitl_coverage_fill(store, job_id)
    if is_coverage_fill_miss(text):
        current = store.get_job(job_id)
        if JobStatus(current["status"]) != JobStatus.AWAITING_HUMAN_INPUT:
            from .chat_guard import park_policy_setup_fail_closed

            park_policy_setup_fail_closed(db_path, job_id, text, store=store)
    return text
