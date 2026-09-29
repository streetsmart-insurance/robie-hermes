from __future__ import annotations

from typing import Any, Callable
import re

from .engine import JobEngine
from .models import ACTION_OUTCOME_UNKNOWN, JobStatus, WorkerResult
from .store import JobStore
from .chat_policy import SECURITY_GUARD_STOP_RULE
from .skill_sync import add_synced_context, submission_center_sop_url


class EmailTaskPending(RuntimeError):
    """Leave the inbound message available until its durable job is terminal."""


# Fallback reply when a worker draft contains internal reasoning that cannot be
# safely separated from the customer-facing text. Must not itself contain the
# "thought process" marker phrase.
_REASONING_STRIP_FALLBACK = (
    "Robie held this reply for review: the draft contained internal reasoning "
    "text that could not be safely separated from the customer reply. "
    "Please review and resend."
)

# A bolded agent-reasoning heading, e.g. **Continuing Thought Process** or
# **Analyzing Interrupted Process**. Reasoning headings start with a gerund
# ("Analyzing", "Investigating", ...) or name the thought process explicitly.
_REASONING_HEADING = re.compile(
    r"^\*\*\s*(?:[A-Za-z]*ing\b.*|.*\bthought process\b.*)\s*\*\*$",
    re.IGNORECASE,
)

# First-person narration that follows a reasoning heading ("I am currently...",
# "My goal is..."). Used to tell reasoning paragraphs apart from the
# user-facing reply that follows them.
_FIRST_PERSON_NARRATION = re.compile(r"^(I\b|I'm\b|I’ve\b|My\b|We\b)", re.IGNORECASE)


def _strip_internal_reasoning(text):
    """Remove agent reasoning sections from a worker draft before replying.

    Drops bolded reasoning headings (``**Analyzing ...**``) and the
    first-person narration paragraphs under them, preserving the user-facing
    reply. Fails closed: if only reasoning remains, or the marker phrase
    "thought process" survives without a strip-able heading, returns
    ``_REASONING_STRIP_FALLBACK``. Empty and non-string inputs pass through.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    if text == "":
        return ""
    lines = text.split("\n")
    out = []
    stripped_any = False
    i = 0
    n = len(lines)
    while i < n:
        if _REASONING_HEADING.match(lines[i].strip()):
            stripped_any = True
            i += 1
            # Skip the narration under the heading: blank lines and
            # first-person sentences, stopping at user-facing content.
            while i < n:
                stripped = lines[i].strip()
                if stripped == "":
                    j = i + 1
                    while j < n and lines[j].strip() == "":
                        j += 1
                    if j < n and (
                        _REASONING_HEADING.match(lines[j].strip())
                        or _FIRST_PERSON_NARRATION.match(lines[j].strip())
                    ):
                        i = j
                        continue
                    break
                if _REASONING_HEADING.match(stripped):
                    break
                if _FIRST_PERSON_NARRATION.match(stripped):
                    i += 1
                    continue
                break
            continue
        out.append(lines[i])
        i += 1
    result = "\n".join(out)
    if stripped_any:
        result = result.strip("\n").strip()
    if not result.strip() or "thought process" in result.lower():
        return _REASONING_STRIP_FALLBACK
    return result


class HermesEmailWorker:
    def __init__(self, run_agent: Callable[[str], str], store: JobStore, run_agent_with_context=None):
        self.run_agent = run_agent
        self.store = store
        self.run_agent_with_context = run_agent_with_context

    def perform(self, job, *, idempotency_key: str) -> WorkerResult:
        if self.run_agent_with_context is not None:
            response = self.run_agent_with_context(job["payload"]["prompt"], job["id"], self.store.path)
        else:
            response = self.run_agent(job["payload"]["prompt"])
        from .chat_destination_binding import claimed_from_job, derive_destination, read_exec_rows
        binding = derive_destination(read_exec_rows(self.store, job["id"]), claimed_from_job(job, response))
        destination = {"gmail_message_id": job["payload"]["gmail_message_id"]}
        if binding.bindable:
            destination.update(binding.checkpoint(job["id"])["destination"])
            payload = dict(job["payload"])
            for key, value in binding.payload_patch().items():
                payload.setdefault(key, value)
            self.store.update_payload(job["id"], payload)
        existing = self.store.get_checkpoint(job["id"], "action") or {}
        existing_dest = dict(existing.get("destination") or {})
        payload = dict(job.get("payload") or {})
        for key in ("policy_number", "policy_id", "applicant_id"):
            value = str(existing_dest.get(key) or payload.get(key) or "").strip()
            if value:
                destination[key] = value
                payload.setdefault(key, value)
        if any(destination.get(k) for k in ("policy_number", "policy_id")):
            self.store.update_payload(job["id"], payload)
        self.store.checkpoint(job["id"], "email_response", {"response_text": response})
        route = self.store.get_checkpoint(job["id"], 'email_route') or {}
        if route.get('route') == 'finance':
            destination = {"gmail_message_id": job['payload']['gmail_message_id']}
        blocked = response.startswith("ROBIE_EXECUTION_BLOCKED:")
        failed = blocked or response.lstrip().lower().startswith("error executing task:")
        from .hitl import structured_blocker_reason
        from .hitl_ladder import stamp_hitl_posted_at
        from .policy_setup_dispatch import is_policy_setup_honest_hitl

        hold = (
            JobStatus.NEEDS_SKILL if blocked
            else (JobStatus.NEEDS_CLARIFICATION if route.get('status') == 'NEEDS_CLARIFICATION' else None)
        )
        error = response if failed or route.get('status') == 'NEEDS_CLARIFICATION' else None
        # Job 2b30d293 / c75aab5c: empty fill or mint-miss became UNVERIFIED.
        # Park HITL instead of a 20-minute RUNNING / UNVERIFIED.
        if (
            is_policy_setup_honest_hitl(response)
            or response.startswith("ROBIE HITL:")
            or structured_blocker_reason(response)
        ):
            hold = JobStatus.AWAITING_HUMAN_INPUT
            error = response
            failed = True
            payload = stamp_hitl_posted_at(dict(job.get("payload") or {}))
            for key in ("policy_number", "policy_id", "applicant_id"):
                if destination.get(key):
                    payload.setdefault(key, destination[key])
            self.store.update_payload(job["id"], payload)
        return WorkerResult(
            succeeded=not failed,
            action="hermes.email_task",
            destination=destination,
            detail={"response_text": response, **({"outcome": ACTION_OUTCOME_UNKNOWN} if response.startswith("ROBIE_OUTCOME_UNKNOWN:") else {})},
            retryable=True,
            hold_status=hold,
            error=error,
        )


def _default_email_verifiers():
    # Email uses the same independent destination reader as Chat.
    from .chat_guard import _default_chat_verifiers
    return {"hermes.email_task": _default_chat_verifiers()["hermes.google_chat_task"]}


def run_guarded_email_task(
    *,
    db_path: str,
    gmail_message_id: str,
    prompt: str,
    run_agent: Callable[[str], str],
    run_agent_with_context: Callable[[str, str, str], str] | None = None,
    verifiers: dict[str, Any] | None = None,
    attachment_names: tuple[str, ...] = (),
    thread_id: str = "",
) -> str:
    """Run once and require independent destination evidence before completion."""
    request_text = prompt
    prompt = add_synced_context(
        prompt
        + "\n\n"
        + SECURITY_GUARD_STOP_RULE
        + "\n\nSUBMISSION CENTER SOP REFERENCE\n"
        + submission_center_sop_url()
    )
    from .ezlynx_write_scope import requested_message_applicant
    targets = {}
    applicant = requested_message_applicant({'request_text': request_text})
    if applicant:
        targets['applicant_id'] = applicant
    policy_numbers = set(re.findall(
        r'\bpolicy(?:\s+number|\s*#)\s*[:=]?\s*([A-Z0-9][A-Z0-9./-]{2,})',
        request_text, flags=re.IGNORECASE,
    ))
    if len(policy_numbers) == 1:
        number = next(iter(policy_numbers)).rstrip('.')
        if any(char.isdigit() for char in number):
            targets['policy_number'] = number
    store = JobStore(db_path)
    from .email_hitl import (
        _subject_from_prompt,
        find_parked_email_hitl_job,
        ingest_email_hitl_reply,
    )

    parked = find_parked_email_hitl_job(
        store,
        subject=_subject_from_prompt(request_text),
        body=request_text,
        thread_id=thread_id,
        gmail_message_id=gmail_message_id,
    )
    if parked:
        ingest_email_hitl_reply(
            store,
            job_id=parked["id"],
            gmail_message_id=gmail_message_id,
            subject=_subject_from_prompt(request_text),
            body=request_text,
            thread_id=thread_id,
        )
        job = store.get_job(parked["id"])
        if JobStatus(job["status"]) == JobStatus.RUNNING:
            raise EmailTaskPending(
                f"ROBIE Job {job['id']} is {job['status']}; keep email unread"
            )
    else:
        payload = {
            "worker": "hermes-cua",
            "gmail_message_id": gmail_message_id,
            "prompt": prompt,
            "request_text": request_text,
            "document_names": list(attachment_names),
            **targets,
        }
        if str(thread_id or "").strip():
            payload["gmail_thread_id"] = str(thread_id).strip()
        job = store.create_job(
            "hermes.email_task",
            payload,
            idempotency_key=f"gmail:{gmail_message_id}",
            max_attempts=3,
        )
    engine = JobEngine(
        store, {"hermes-cua": HermesEmailWorker(run_agent, store, run_agent_with_context)},
        _default_email_verifiers() if verifiers is None else verifiers, perform_timeout_seconds=960,
    )
    final = engine.run(job["id"])
    action = store.get_checkpoint(job["id"], "action") or {}
    response = action.get("detail", {}).get("response_text") or (store.get_checkpoint(job["id"], "email_response") or {}).get("response_text") or final.get("last_error") or "No worker response was stored."
    # Never let agent-internal reasoning leak into the outbound reply.
    response = _strip_internal_reasoning(response)
    from .message_results import verification_summary
    summary = verification_summary(store, job["id"])
    status = JobStatus(final["status"])
    if status in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.RETRY_WAIT}:
        raise EmailTaskPending(f"ROBIE Job {job['id']} is {status.value}; keep email unread")
    return _render_email_terminal(
        job_id=job["id"],
        status=status,
        response=response,
        summary=str(summary or ""),
        store=store,
    )


def _render_email_terminal(
    *,
    job_id: str,
    status: JobStatus,
    response: str,
    summary: str,
    store: JobStore | None = None,
) -> str:
    """Render the email reply in the simple shared format.

    Same shape as the Chat terminal renderer (Jake's What happened /
    Anything needed / Status, plain words first, job ref at the bottom),
    via the shared status_format module. Internal worker codes are
    translated for display only.

    When ``ROBIE_END_STATE_REPORT`` is on, the reply is the end-state
    report instead, in Test or in Production. The old
    "Worker report (not proof)" line is display-only and is skipped.
    Destination verifiers still run before this render.
    """
    from .end_state_report import end_state_report_enabled, render_job_end_state

    if end_state_report_enabled() and store is not None:
        job = store.get_job(job_id)
        return render_job_end_state(
            store, job, response, channel="email"
        )

    from . import status_format

    if status == JobStatus.COMPLETE:
        return status_format.render_simple_status(
            headline="Done.",
            what_happened=str(summary or "The requested result was independently verified."),
            anything_needed="No.",
            status_line="Verified \u2014 the result was checked against the destination.",
            job_id=job_id,
        )
    headline = {
        JobStatus.FAILED: "Couldn't finish.",
        JobStatus.UNVERIFIED: "Not verified.",
    }.get(status, "Waiting.")
    status_line = {
        JobStatus.FAILED: "Failed.",
        JobStatus.UNVERIFIED: "Not verified \u2014 don't treat this as done.",
    }.get(status, "Not finished.")
    details = "Worker report (not proof): " + str(response or "").strip()
    if summary:
        details += "\n\n" + str(summary).strip()
    what_happened = status_format.plain_reason(response)
    # The retry guidance lives in the "Anything needed" line; don't repeat it.
    what_happened = what_happened.replace(
        "Check saved results before retrying.", "").replace("  ", " ").strip(" .")
    if what_happened and not what_happened.endswith("."):
        what_happened += "."
    return status_format.render_simple_status(
        headline=headline,
        what_happened=what_happened or "The job ended without a clear result.",
        anything_needed="Check the saved results before retrying.",
        status_line=status_line,
        details=details,
        job_id=job_id,
    )
