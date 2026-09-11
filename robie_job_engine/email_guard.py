from __future__ import annotations

from typing import Any, Callable

from .engine import JobEngine
from .models import JobStatus, WorkerResult
from .store import JobStore
from .skill_sync import add_synced_context, submission_center_sop_url


class EmailTaskPending(RuntimeError):
    """Leave the inbound message available until its durable job is terminal."""


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
        failed = response.lstrip().lower().startswith("error executing task:")
        return WorkerResult(
            succeeded=not failed,
            action="hermes.email_task",
            destination={"gmail_message_id": job["payload"]["gmail_message_id"]},
            detail={"response_text": response},
            retryable=True,
            error=response if failed else None,
        )


def run_guarded_email_task(
    *,
    db_path: str,
    gmail_message_id: str,
    prompt: str,
    run_agent: Callable[[str], str],
    run_agent_with_context: Callable[[str, str, str], str] | None = None,
    verifiers: dict[str, Any] | None = None,
) -> str:
    """Run once and require independent destination evidence before completion."""
    prompt = add_synced_context(
        prompt
        + "\n\nSUBMISSION CENTER SOP REFERENCE\n"
        + submission_center_sop_url()
    )
    store = JobStore(db_path)
    job = store.create_job(
        "hermes.email_task",
        {"worker": "hermes-cua", "gmail_message_id": gmail_message_id, "prompt": prompt},
        idempotency_key=f"gmail:{gmail_message_id}",
        max_attempts=3,
    )
    engine = JobEngine(
        store, {"hermes-cua": HermesEmailWorker(run_agent, store, run_agent_with_context)},
        verifiers or {}, perform_timeout_seconds=630,
    )
    final = engine.run(job["id"])
    action = store.get_checkpoint(job["id"], "action") or {}
    response = action.get("detail", {}).get("response_text") or final.get("last_error") or "No worker response was stored."
    status = JobStatus(final["status"])
    if status in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.RETRY_WAIT}:
        raise EmailTaskPending(f"ROBIE Job {job['id']} is {status.value}; keep email unread")
    if status == JobStatus.COMPLETE:
        return response
    return (
        f"ROBIE Job {job['id']} — {status.value}\n\n"
        f"{response}\n\n"
        "ROBIE did not independently verify the destination state. This result must not be treated as COMPLETE."
    )
