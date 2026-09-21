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


# ---------------------------------------------------------------------------
# Outbound email sanitizer: internal model reasoning must never reach the inbox.
# ---------------------------------------------------------------------------
# The hermes email worker can resume mid-response ("Continue the SAME original
# email job ... Do not restart the task"). On 2026-09-20 a resume dumped the
# model's private reasoning into the worker response under bold headings like
# "**Continuing Thought Process**", "**Analyzing Interrupted Process**",
# "**Investigating Zapier Alert**", each followed by first-person process
# narration ("I am currently ...", "My goal is ..."). The reply composer then
# embedded that raw text in an email to Carlo. The composer is the last line
# of defense: strip reasoning sections here, no matter what the worker emits.

# A bold heading that introduces internal monologue rather than user content.
_PROCESS_HEADING_RE = re.compile(
    r"^\s*\*\*\s*("
    r"thought\s+process|"
    r"continuing|analy[sz]ing|investigating|reviewing|diagnosing|"
    r"integrating|clarifying|reconstructing|resuming|processing|"
    r"examining|assessing|evaluating"
    r")\b[^*\n]*\*\*\s*$",
    re.IGNORECASE,
)

# First-person process narration that follows those headings.
_PROCESS_NARRATION_RE = re.compile(
    r"^\s*(I am currently|I['\u2019]m currently|I['\u2019]m\s|I will then|"
    r"My goal|My focus|My aim|My current focus)\b",
)

# Fail-closed backstop: this phrase must never survive into an outbound email.
_THOUGHT_PROCESS_MARKER_RE = re.compile(r"thought\s+process", re.IGNORECASE)

_REASONING_STRIP_FALLBACK = (
    "The worker's reply contained internal processing text that was removed "
    "before sending. The underlying job result is recorded in the job log."
)


def _strip_internal_reasoning(text):
    """Remove internal-reasoning sections from a worker response.

    Drops bold process-talk headings (and their narration paragraphs). If any
    thought-process marker survives, or nothing user-facing remains, returns a
    fixed safe notice instead of the raw text. Never raises.
    """
    try:
        if not isinstance(text, str) or not text.strip():
            return text
        # Pass 1: drop process-talk heading lines wherever they appear.
        lines = [ln for ln in text.splitlines() if not _PROCESS_HEADING_RE.match(ln)]
        # Pass 2: drop paragraphs that are pure process narration.
        kept_paragraphs = []
        for paragraph in re.split(r"\n\s*\n", "\n".join(lines)):
            stripped = paragraph.strip()
            if not stripped:
                continue
            if _PROCESS_NARRATION_RE.match(stripped):
                continue
            kept_paragraphs.append(paragraph.strip("\n"))
        result = "\n\n".join(kept_paragraphs).strip()
        if not result or _THOUGHT_PROCESS_MARKER_RE.search(result):
            return _REASONING_STRIP_FALLBACK
        return result
    except Exception:
        return _REASONING_STRIP_FALLBACK


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
    from .message_results import verification_summary
    summary = verification_summary(store, job["id"])
    details = f"\n\n{summary}" if summary else ""
    status = JobStatus(final["status"])
    if status in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.RETRY_WAIT}:
        raise EmailTaskPending(f"ROBIE Job {job['id']} is {status.value}; keep email unread")
    if status == JobStatus.COMPLETE:
        return f"ROBIE Job {job['id']} — COMPLETE\n\n{summary or 'The requested result was independently verified.'}"
    # The worker response can contain the model's internal reasoning (e.g. a
    # "**Continuing Thought Process**" dump after a mid-response resume). That
    # text must never reach the inbox: sanitize before composing the reply.
    # The raw response stays in the job checkpoints for diagnosis.
    email_safe_response = _strip_internal_reasoning(response)
    return (
        f"ROBIE Job {job['id']} — {status.value}\n\n"
        f"Worker report (not proof): {email_safe_response}{details}\n\n"
        "ROBIE did not independently verify the destination state. This result must not be treated as COMPLETE."
    )
