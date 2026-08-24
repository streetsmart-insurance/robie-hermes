from __future__ import annotations

from typing import Iterable

from .attachments import AttachmentRef, ingest_attachment_refs
from .chat_policy import execution_contract_lines
from .context_policy import JobContextManager
from .models import WAITING_STATUSES, JobStatus
from .operations import ingest_chat_attachments
from .recording import RecordingManager
from .request_routing import BOUNDED_ENGINE_ACTIONS, classify_request
from .submission_routing import resolve_submission_route, submission_verification_requirements
from .store import JobStore


_CONVERSATION_ONLY_EXACT = {
    "ok",
    "okay",
    "thanks",
    "thank you",
    "cool",
    "cool cool",
    "sweet",
    "wow",
    "got it",
}

_CONVERSATION_ONLY_PREFIXES = (
    "what does this mean",
    "how is it going",
    "how are we doing",
    "what are you working on",
    "where did we leave off",
    "what did you do",
    "give me a rundown",
    "can you give me a rundown",
    "can you tell me what was done",
    "is this good or bad",
)


def chat_message_requires_job(
    text: str,
    *,
    expected_attachment_count: int = 0,
) -> bool:
    """Return whether a Chat message belongs in the operational Job Engine.

    The safety default is ``True``.  We bypass the Job Engine only for a small,
    deterministic set of conversational/status messages and explicit no-action
    decision-card tests.  Attachments always remain operational so ingestion
    can fail closed and preserve the supplied bytes.
    """
    if expected_attachment_count < 0:
        raise ValueError("expected attachment count cannot be negative")
    if expected_attachment_count:
        return True
    normalized = " ".join(str(text or "").casefold().split()).strip(" .!?\t\r\n")
    if not normalized:
        return False
    if normalized in _CONVERSATION_ONLY_EXACT:
        return False
    if any(normalized.startswith(prefix) for prefix in _CONVERSATION_ONLY_PREFIXES):
        return False
    if (
        normalized.startswith("test only")
        and ("decision card" in normalized or "clarify" in normalized)
        and (
            "do not perform any ezlynx action" in normalized
            or "no ezlynx action" in normalized
        )
    ):
        return False
    return True


def _submission_contract(text: str) -> list[str]:
    normalized = text.casefold()
    if "submission" not in normalized and "quote" not in normalized:
        return []
    is_renewal = "renewal" in normalized or "renew" in normalized
    is_manual = is_renewal and "manual" in normalized
    is_high_risk = is_renewal and ("high risk" in normalized or "high-risk" in normalized)
    route = resolve_submission_route(
        is_renewal=is_renewal,
        is_manual_renewal=is_manual,
        is_high_risk=is_high_risk,
    )
    verification = submission_verification_requirements(route)
    lines = [
        "StreetSmart Submission Center routing (SOP-aware):",
        f"- File the document in the {route.document_destination}.",
        f"- Put the upload/action note in the {route.note_destination}.",
        "- Never create or use an Untitled discussion.",
        f"- Discussion resolution: {route.selection_rule}",
    ]
    if route.workflow_to_start:
        lines.append(f"- Start the {route.workflow_to_start} workflow and independently verify it started.")
    lines.append(
        "- Completion evidence must independently prove: "
        f"{verification['document']['location']}; {verification['note']['location']}; "
        "the exact filename and exact note text after fresh navigation."
    )
    return lines


def build_chat_execution_text(db_path: str, job_id: str | None, text: str) -> str:
    """Add trusted, non-user-visible execution constraints for Hermes.

    Google Chat downloads attachments to opaque cache names.  The Job Engine
    has already copied those bytes into its private artifact store, so Hermes
    must use the durable staged path for an actual browser file chooser rather
    than merely reading the PDF and then clicking an empty Upload dialog.
    """
    if not job_id:
        return text
    store = JobStore(db_path)
    job = store.get_job(job_id)
    if job["status"] == JobStatus.FAILED:
        return (
            text
            + "\n\n[ROBIE JOB ENGINE EXECUTION CONTRACT]\n"
            + "Do not attempt this Job: required attachment ingestion failed. "
            + "Report the Job Engine failure without claiming any EZLynx action.\n"
            + "[END ROBIE JOB ENGINE EXECUTION CONTRACT]"
        )
    ingestion = store.get_checkpoint(job_id, "ingestion") or {}
    artifacts = ingestion.get("artifacts") or []
    if not artifacts:
        return text
    lines = [
        "\n\n[ROBIE JOB ENGINE EXECUTION CONTRACT]",
        f"Job ID: {job_id}",
        "Attachments below are trusted, private staged files owned by StreetSmart.",
    ]
    for item in artifacts:
        lines.append(
            "- staged_path={path} filename={name} mime={mime} sha256={sha}".format(
                path=item.get("stored_path", ""),
                name=item.get("name", "attachment"),
                mime=item.get("mime_type", "application/octet-stream"),
                sha=item.get("sha256", ""),
            )
        )
    lines.extend([
        "When the request requires an upload, set the browser file chooser to the exact staged_path before clicking Upload.",
        "Complete every requested mutation (including status, premium, document attachment, and note when requested).",
        "After saving, navigate away and reopen the exact destination. Read the freshly loaded server-backed state.",
        "Never claim success from modal text, a local DOM value, or your own prior action. If any requested field is absent, say the action is not verified.",
        "The Job Engine, not the Computer Worker, has final completion authority.",
    ])
    lines.extend(execution_contract_lines())
    lines.extend(_submission_contract(text))
    lines.append("[END ROBIE JOB ENGINE EXECUTION CONTRACT]")
    return text + "\n".join(lines)


def open_chat_job(
    db_path: str,
    message_id: str,
    text: str,
    attachments: Iterable[tuple[str, str]] | None = None,
    requested_by: str | None = None,
    conversation_id: str | None = None,
    expected_attachment_count: int = 0,
    attachment_refs: Iterable[AttachmentRef] | None = None,
    drive_port: object | None = None,
    artifact_root: str | None = None,
) -> str | None:
    """Create the Job before execution and bind durable attachment artifacts."""
    if not chat_message_requires_job(
        text,
        expected_attachment_count=expected_attachment_count,
    ):
        return None
    store = JobStore(db_path)
    context = JobContextManager(db_path)
    context_key = conversation_id or f"google-chat:{requested_by or 'unknown'}"
    context_decision = context.decide(context_key, text)
    files = list(attachments or [])
    refs = list(attachment_refs or [])
    classification = classify_request(
        text, attachment_count=max(expected_attachment_count, len(files), len(refs))
    )
    if context_decision.action == "RESUME" and context_decision.active_job_id:
        job = store.get_job(context_decision.active_job_id)
        store.checkpoint(job["id"], f"continuation:{message_id}", {
            "message_id": message_id,
            "text": text,
            "requested_by": requested_by or "Google Chat user",
        })
    else:
        job = store.create_job(
            classification.action_type,
            {
                "message_id": message_id,
                "text": text,
                "requested_by": requested_by or "Google Chat user",
                "source": "Google Chat",
                "conversation_id": context_key,
                "worker": classification.worker,
            },
            idempotency_key=f"gchat:{message_id}",
        )
        context.bind_job(
            context_key,
            job["id"],
            summary={
                "objective": text[:1_000],
                "status": job["status"].value if isinstance(job["status"], JobStatus) else str(job["status"]),
                "next_action": "Execute the selected Skill and independently verify destination state.",
            },
        )
    if classification.hold_status == JobStatus.NEEDS_CLARIFICATION.value:
        current = store.get_job(job["id"])
        if current["status"] == JobStatus.PENDING:
            store.transition(
                job["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                error="request is too vague to execute safely",
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
            return job["id"]
    # Bounded workers stay PENDING so JobEngine can claim them. Hermes chat
    # tasks still move to RUNNING so the existing adapter path can proceed.
    if (
        job["status"] == JobStatus.PENDING
        and classification.action_type not in BOUNDED_ENGINE_ACTIONS
    ):
        store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    staged_count = len(files) + sum(
        1 for ref in refs if ref.local_path or (ref.kind == "drive_chip" and drive_port)
    )
    if expected_attachment_count < 0:
        raise ValueError("expected attachment count cannot be negative")
    if expected_attachment_count > staged_count:
        current = store.get_job(job["id"])
        if current["status"] not in {JobStatus.COMPLETE, JobStatus.FAILED}:
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.VERIFYING,
                    JobStatus.UNVERIFIED,
                    JobStatus.NEEDS_CLARIFICATION,
                    JobStatus.WAITING,
                },
                error=(
                    f"attachment ingestion incomplete: received {expected_attachment_count} "
                    f"attachment reference(s), staged {staged_count}"
                ),
                release_lease=True,
            )
        return job["id"]
    if (files or refs) and not store.get_checkpoint(job["id"], "ingestion"):
        try:
            records = []
            if files:
                records.extend(ingest_chat_attachments(
                    db_path, job["id"], message_id, files, artifact_root=artifact_root
                ))
            if refs:
                records.extend(ingest_attachment_refs(
                    db_path, job["id"], message_id, refs,
                    drive_port=drive_port, artifact_root=artifact_root,
                ))
            store.checkpoint(job["id"], "ingestion", {
                "source": "google_chat",
                "message_id": message_id,
                "artifacts": [{
                    "id": item["id"],
                    "name": item["original_name"],
                    "mime_type": item["mime_type"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                    "stored_path": item["stored_path"],
                } for item in records],
            })
        except Exception as exc:
            current = store.get_job(job["id"])
            if current["status"] not in {JobStatus.COMPLETE, JobStatus.FAILED}:
                store.transition(
                    job["id"], JobStatus.FAILED,
                    expected={
                        JobStatus.PENDING,
                        JobStatus.RUNNING,
                        JobStatus.VERIFYING,
                        JobStatus.UNVERIFIED,
                        JobStatus.NEEDS_CLARIFICATION,
                        JobStatus.WAITING,
                    },
                    error=f"attachment ingestion failed: {type(exc).__name__}: {exc}",
                    release_lease=True,
                )
    current = store.get_job(job["id"])
    if current["status"] not in {JobStatus.COMPLETE, JobStatus.FAILED}:
        RecordingManager(db_path).safe_start(job["id"])
    return job["id"]


def _recording_chat_note(recordings: RecordingManager, job_id: str) -> str:
    """Return a user-facing recording link without weakening the status gate."""
    recording = recordings.store.latest(job_id)
    if not recording:
        return ""
    if recording.get("status") == "READY" and recording.get("drive_url"):
        return (
            "\n\n🎥 Review this job recording: "
            f"{recording['drive_url']}\n"
            "Reply in this thread with what ROBIE should correct or retry."
        )
    if recording.get("status") == "FAILED":
        return (
            "\n\n⚠️ The diagnostic recording could not be uploaded. "
            "The Job remains governed by the status above; check the Job ledger for details."
        )
    return ""


def guard_chat_response(db_path: str, job_id: str | None, content: str) -> str:
    if not job_id:
        return content
    store = JobStore(db_path)
    job = store.get_job(job_id)
    recordings = RecordingManager(db_path)
    if job["status"] == JobStatus.COMPLETE:
        recordings.safe_stop(job_id, JobStatus.COMPLETE.value)
        return content + _recording_chat_note(recordings, job_id)
    if job["status"] == JobStatus.FAILED:
        recordings.safe_stop(job_id, JobStatus.FAILED.value)
        return (
            f"ROBIE Job {job_id} — FAILED\n\n"
            "ROBIE could not safely finish the requested work. No success claims from the Computer Worker are being reported.\n\n"
            f"Reason: {job.get('last_error') or 'unknown error'}."
            + _recording_chat_note(recordings, job_id)
        )
    if job["status"] == JobStatus.UNVERIFIED:
        recordings.safe_stop(job_id, JobStatus.UNVERIFIED.value)
        return (
            f"ROBIE Job {job_id} — UNVERIFIED\n\n"
            "ROBIE attempted the work, but the destination state was not independently verified. "
            "Any success wording produced by the Computer Worker has been suppressed.\n\n"
            "Do not treat this Job as COMPLETE; it remains open for review or retry."
            + _recording_chat_note(recordings, job_id)
        )
    if JobStatus(job["status"]) in WAITING_STATUSES:
        status = JobStatus(job["status"]).value
        recordings.safe_stop(job_id, status)
        return (
            f"ROBIE Job {job_id} — {status}\n\n"
            "ROBIE is not treating this request as successful. "
            f"Reason: {job.get('last_error') or 'waiting for a human or destination update'}."
            + _recording_chat_note(recordings, job_id)
        )
    store.checkpoint(job_id, "action", {
        "action": "hermes.google_chat_task",
        "destination": {"message_id": job["payload"]["message_id"]},
        "detail": {"response_text": content},
    })
    if job["status"] == JobStatus.RUNNING:
        store.transition(job_id, JobStatus.VERIFYING, expected={JobStatus.RUNNING})
    store.transition(
        job_id, JobStatus.UNVERIFIED, expected={JobStatus.VERIFYING},
        error="no independent destination verifier registered", release_lease=True,
    )
    recordings.safe_stop(job_id, JobStatus.UNVERIFIED.value)
    return (
        f"ROBIE Job {job_id} — UNVERIFIED\n\n"
        "ROBIE attempted the work, but the destination state was not independently verified. "
        "Any success wording produced by the Computer Worker has been suppressed.\n\n"
        "Do not treat this Job as COMPLETE; it remains open for review or retry."
        + _recording_chat_note(recordings, job_id)
    )
