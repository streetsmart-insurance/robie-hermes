from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .attachments import AttachmentRef, ingest_attachment_refs
from .chat_policy import execution_contract_lines, forbidden_tool_request
from .context_policy import (
    CONTINUATION_PREFIXES,
    CORRECTION_PREFIXES,
)
from .chat_queue import DurableChatEventQueue
from .idempotency import DurableWorkLedger, IdempotencyError
from .hitl import interaction_for_blocker, structured_blocker_reason
from .job_schema import bounded_schema_hold_reason
from .models import TERMINAL_STATUSES, WAITING_STATUSES, JobStatus
from .runs import IsolatedRunStore, RunIsolationError
from .operations import ingest_chat_attachments
from .recording import RecordingManager
from .request_routing import BOUNDED_ENGINE_ACTIONS, classify_request
from .secrets import redact_mapping, redact_text
from .sheets_sync import publish_job_to_control_center
from .submission_routing import resolve_submission_route, submission_verification_requirements
from .post_job_audit import format_audit_chat_message, maybe_audit_terminal_job
from .store import JobStore


logger = logging.getLogger(__name__)

_CHAT_VERIFIERS: dict[str, Any] = {}
_GENERIC_CHAT_HEARTBEATS: dict[tuple[str, str], tuple[threading.Event, threading.Thread]] = {}
_GENERIC_CHAT_HEARTBEAT_LOCK = threading.Lock()
_GENERIC_CHAT_HEARTBEAT_INTERVAL_SECONDS = 30.0


def _generic_chat_heartbeat_key(db_path: str, job_id: str) -> tuple[str, str]:
    return (str(Path(db_path).resolve()), job_id)


def stop_generic_chat_job_heartbeat(db_path: str, job_id: str) -> None:
    """Stop a process-local generic Chat heartbeat. Tests use this to fail-close."""
    key = _generic_chat_heartbeat_key(db_path, job_id)
    with _GENERIC_CHAT_HEARTBEAT_LOCK:
        existing = _GENERIC_CHAT_HEARTBEATS.pop(key, None)
    if existing is None:
        return
    stop, thread = existing
    stop.set()
    thread.join(timeout=2.0)


def start_generic_chat_job_heartbeat(
    db_path: str,
    job_id: str | None,
    *,
    now: datetime | None = None,
    interval_seconds: float = _GENERIC_CHAT_HEARTBEAT_INTERVAL_SECONDS,
    source: str = "hermes-gateway",
) -> dict[str, Any] | None:
    """Write ``gateway_progress`` to *this* jobs.db and keep writing while live.

    hermes-gateway may load a stale ``.hermes/hermes-agent`` adapter that never
    calls ``JobStore.heartbeat_generic_chat_job``. The live Job Engine path
    (``open_chat_job`` / ``build_chat_execution_text``) still runs, so the
    heartbeat has to start there and use the same ``db_path`` the orphan
    watcher reads. The first write is synchronous. A daemon thread continues
    on a 30s-or-better cadence. If this process dies or the thread errors,
    heartbeats stop and ``fail_orphaned_chat_jobs`` still fail-closes.
    """
    if not job_id:
        return None
    if interval_seconds <= 0:
        raise ValueError("heartbeat interval must be positive")
    store = JobStore(db_path)
    job = store.get_job(job_id)
    if (
        job["action_type"] != "hermes.google_chat_task"
        or job["status"] != JobStatus.RUNNING.value
    ):
        return job
    written = store.heartbeat_generic_chat_job(job_id, now=now, source=source)
    key = _generic_chat_heartbeat_key(db_path, job_id)
    with _GENERIC_CHAT_HEARTBEAT_LOCK:
        existing = _GENERIC_CHAT_HEARTBEATS.get(key)
        if existing is not None and existing[1].is_alive():
            return written
        if existing is not None:
            existing[0].set()
        stop = threading.Event()
        thread = threading.Thread(
            target=_maintain_generic_chat_job_heartbeat,
            args=(db_path, job_id, stop, interval_seconds, source),
            name=f"robie-generic-chat-heartbeat:{job_id}",
            daemon=True,
        )
        _GENERIC_CHAT_HEARTBEATS[key] = (stop, thread)
        thread.start()
    return written


def _maintain_generic_chat_job_heartbeat(
    db_path: str,
    job_id: str,
    stop: threading.Event,
    interval_seconds: float,
    source: str,
) -> None:
    store = JobStore(db_path)
    while not stop.wait(interval_seconds):
        try:
            job = store.heartbeat_generic_chat_job(job_id, source=source)
            if job["status"] != JobStatus.RUNNING.value:
                return
        except Exception:
            logger.exception(
                "generic Chat heartbeat failed; orphan watcher will fail-close job=%s db=%s",
                job_id,
                db_path,
            )
            return


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
    "yes",
    "no",
    "sure",
    "done",
    "good",
    "good?",
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
    "change your profile",
    "talk to me like",
    "speak to me like",
)

_RELATED_JOB_COMMANDS = ("/jobs", "/skills", "/status", "status", "approve", "approved")

_IN_PROGRESS_MARKERS = (
    "accepted",
    "queued",
    "working",
    "still working",
    "processing",
    "running",
    "playwright_exec",
)

_UNBOUND_POLICY_SUCCESS_MARKERS = (
    "i set up the policy",
    "i created the policy",
    "i finished setting up the policy",
    "policy has been set up",
    "policy was created",
    "policy setup is complete",
    "successfully set up the policy",
    "successfully created the policy",
    "the policy is set up",
    "the insurance policy is set up",
)


def _looks_like_unbound_policy_success(content: str) -> bool:
    normalized = " ".join(str(content or "").casefold().split())
    return any(marker in normalized for marker in _UNBOUND_POLICY_SUCCESS_MARKERS)


def _submission_audit_payload() -> dict[str, Any]:
    """Return the server-owned read-only scope for a Submission Center audit."""
    return {
        "resource_id": "ezlynx:submission-center:overview:submissions",
        "scope": {
            "time_frame": "All Submissions",
            "assigned_producer": "Streetsmart Insurance",
            "my_submissions": False,
            "page_size": 100,
            "status_sort": "ascending",
            "inspection_boundary": "first_closed_row",
        },
        "expected_postcondition": {
            "mat_row_count": 100,
            "pager_total_present": True,
            "status_aria_sort": "ascending",
            "first_row_non_closed": True,
            "first_closed_row_inspected": True,
        },
        "read_only": True,
    }


def _retarget_bounded_correction(
    store: JobStore,
    db_path: str,
    job: dict[str, Any],
    classification: Any,
    text: str,
    server_payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Enforce that a bounded correction cannot remain a generic Chat Job."""
    if not (
        classification.action_type in BOUNDED_ENGINE_ACTIONS
        and job["action_type"] in {
            "hermes.google_chat_task",
            "hermes.plain_english",
            "hermes.needs_clarification",
        }
        and job["attempt_count"] == 0
        and not job.get("lease_owner")
        and store.get_checkpoint(job["id"], "action") is None
    ):
        return job, False

    recordings = RecordingManager(db_path)
    previous_segment = recordings.stop_and_upload(job["id"], "RETRY")
    if previous_segment and (
        previous_segment.get("status") != "READY"
        or not previous_segment.get("drive_url")
    ):
        current = store.get_job(job["id"])
        if JobStatus(current["status"]) not in TERMINAL_STATUSES:
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.UNVERIFIED,
                    JobStatus.NEEDS_CLARIFICATION,
                },
                error=(
                    "Recording upload failed during route correction"
                    if previous_segment.get("failure_stage") == "UPLOAD"
                    else "Recording failed during route correction"
                ),
                release_lease=True,
            )
        return store.get_job(job["id"]), True

    return (
        store.retarget_unattempted(
            job["id"],
            classification.action_type,
            {
                "text": text,
                "worker": classification.worker,
                **server_payload,
            },
            reason="corrective DM supplied a destination-specific bounded action",
        ),
        False,
    )


def _looks_in_progress(content: str) -> bool:
    normalized = " ".join(str(content or "").casefold().split())
    return any(marker in normalized for marker in _IN_PROGRESS_MARKERS)


def register_chat_verifier(action_type: str, verifier: Any) -> None:
    """Register a destination verifier during gateway bootstrap."""
    _CHAT_VERIFIERS[action_type] = verifier


def clear_chat_verifiers() -> None:
    """Test/bootstrap helper; never changes persisted Job evidence."""
    _CHAT_VERIFIERS.clear()


def _default_chat_verifiers() -> dict[str, Any]:
    from .chat_verifiers import FilesystemSkillUpdateVerifier

    roots = tuple(
        item for item in os.environ.get(
            "ROBIE_SKILL_ROOTS",
            "/opt/streetsmart-hermes/.hermes/skills"
            + os.pathsep
            + "/opt/streetsmart-hermes-test/.hermes/skills",
        ).split(os.pathsep) if item
    )
    return {"filesystem.skill_update": FilesystemSkillUpdateVerifier(roots), **_CHAT_VERIFIERS}


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
        "no operational work is requested" in normalized
        and ("validation marker" in normalized or normalized.startswith("test only"))
        and not any(
            word in normalized
            for word in (
                "upload", "move", "delete", "apply", "send", "create",
                "write", "update", "edit", "submit",
            )
        )
    ):
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
    question_prefixes = ("what ", "why ", "when ", "where ", "who ", "how ", "is ", "are ")
    mutation_words = ("upload", "move", "delete", "apply", "send", "create", "write", "update", "edit", "submit")
    if normalized.startswith(question_prefixes) and not any(
        word in normalized for word in mutation_words
    ):
        return False
    return True


def chat_message_is_related_only(
    text: str,
    *,
    expected_attachment_count: int = 0,
) -> bool:
    normalized = " ".join(str(text or "").casefold().split()).strip()
    return (
        not chat_message_requires_job(
            text,
            expected_attachment_count=expected_attachment_count,
        )
        or any(normalized.startswith(prefix) for prefix in _RELATED_JOB_COMMANDS)
    )


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
    from .skill_sync import add_synced_context

    text = add_synced_context(text)
    store = JobStore(db_path)
    job = store.get_job(job_id)
    if (
        job["action_type"] == "hermes.google_chat_task"
        and job["status"] == JobStatus.RUNNING.value
    ):
        start_generic_chat_job_heartbeat(db_path, job_id)
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
    lines = [
        "\n\n[ROBIE JOB ENGINE EXECUTION CONTRACT]",
        f"Job ID: {job_id}",
    ]
    if artifacts:
        lines.append(
            "Attachments below are trusted, private staged files owned by StreetSmart."
        )
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
        "If execution is blocked because a required value is missing, stop and begin the response with exactly: ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: <field name>.",
        "If execution is blocked at an unresolved browser step or locator, stop and begin the response with exactly: ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: <specific step or locator>.",
        "If a write is PLAYWRIGHT_BLOCKED or a modal cannot be uniquely named, stop, describe the dialog title and visible labels only (no passwords), ask Gemini for one unique field, and HITL Carlo if Gemini is unsure. Never guess a field. Never use .first/.nth/.last.",
        "EZLynx notes must go on an existing titled discussion only (New Business, New Policy, Renewal, Cancellation, Submission Center, or another existing titled discussion). Never write a note on Untitled. If the right title is missing, create a properly named discussion first. Always include the exact phrase Robie was here.",
        "A commercial auto policy SHELL is not done. After the shell, click Save and Continue Edit (or Actions → Edit) and finish vehicles, drivers, garaging, symbols, limits, and banks from the quote. Do not bind. Keep the named insured on the file. Do not invent coverage.",
        "Do not emit ROBIE_BLOCKED for a completed action, a general question, or an ordinary explanation.",
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
    action_payload: dict[str, Any] | None = None,
) -> str | None:
    """Create the Job before execution and bind durable attachment artifacts."""
    store = JobStore(db_path)
    store.fail_orphaned_chat_jobs()
    queue = DurableChatEventQueue(db_path)
    context_key = conversation_id or f"google-chat:{requested_by or 'unknown'}"
    files = list(attachments or [])
    refs = list(attachment_refs or [])
    classification = classify_request(
        text, attachment_count=max(expected_attachment_count, len(files), len(refs))
    )
    related_only = chat_message_is_related_only(
        text,
        expected_attachment_count=expected_attachment_count,
    )
    normalized = " ".join(text.casefold().split())
    explicit_continuation = any(
        normalized.startswith(prefix)
        for prefix in (*CONTINUATION_PREFIXES, *CORRECTION_PREFIXES)
    )
    resume_context = queue.active_conversation_job(context_key)
    resume_state = dict((resume_context or {}).get("interaction_state") or {})
    if (
        resume_state.get("resume_mode") == "direct"
        and resume_state.get("resumed_by_message_id") == message_id
    ):
        explicit_continuation = True
    continued_job: dict[str, Any] | None = None
    if related_only or explicit_continuation:
        current = queue.active_conversation_job(context_key)
        active_job_id = current.get("job_id") if current else None
        if not active_job_id:
            return None
        active_job = store.get_job(active_job_id)
        if JobStatus(active_job["status"]) in WAITING_STATUSES:
            store.resume(active_job_id)
        elif JobStatus(active_job["status"]) == JobStatus.UNVERIFIED:
            target = (
                JobStatus.VERIFYING
                if store.get_checkpoint(active_job_id, "action")
                else JobStatus.PENDING
            )
            store.transition(
                active_job_id,
                target,
                expected={JobStatus.UNVERIFIED},
                error=None,
                release_lease=True,
            )
        store.checkpoint(
            active_job_id,
            f"continuation:{message_id}",
            {
                "message_id": message_id,
                "text": text,
                "requested_by": requested_by or "Google Chat user",
                "related_only": related_only,
            },
        )
        queue.link_conversation_job(
            conversation_id=context_key,
            job_id=active_job_id,
            message_id=message_id,
            event_id=message_id,
            relation=(
                "CORRECTION"
                if classification.action_type in BOUNDED_ENGINE_ACTIONS
                else "CONTINUATION"
            ),
        )
        if related_only:
            return active_job_id
        continued_job = store.get_job(active_job_id)
    server_payload: dict[str, Any] = {}
    if classification.action_type == "drive.skill_sync":
        from .skill_sync import ALLOWED_FOLDERS, EXCLUDED_FOLDERS, skill_sync_root

        server_payload.update(
            {
                "destination_root": str(skill_sync_root()),
                "included_folders": list(ALLOWED_FOLDERS),
                "excluded_folders": sorted(EXCLUDED_FOLDERS),
            }
        )
    if classification.action_type == "ezlynx.submission_audit":
        server_payload.update(_submission_audit_payload())
    server_payload.update(dict(action_payload or {}))
    if continued_job is not None:
        job = continued_job
    else:
        job = store.create_job(
            classification.action_type,
            redact_mapping(
                {
                    "message_id": message_id,
                    "text": text,
                    "requested_by": requested_by or "Google Chat user",
                    "source": "Google Chat",
                    "conversation_id": context_key,
                    "worker": classification.worker,
                    **server_payload,
                }
            ),
            idempotency_key=f"gchat:{message_id}",
        )
        queue.link_conversation_job(
            conversation_id=context_key,
            job_id=job["id"],
            message_id=message_id,
            event_id=message_id,
            relation="CREATED",
        )
    # Final routing invariant: destination-specific executable corrections can
    # never leave a zero-attempt generic Chat Job behind. Keeping this outside
    # the context-decision branch also protects callers that supply an explicit
    # Job reference while preserving the same bound conversation context.
    job, route_failed = _retarget_bounded_correction(
        store,
        db_path,
        store.get_job(job["id"]),
        classification,
        text,
        server_payload,
    )
    if route_failed:
        return job["id"]
    try:
        ledger = DurableWorkLedger(db_path)
        ledger.reserve(job["action_type"], job["idempotency_key"])
        IsolatedRunStore(db_path).record_intake(
            owner=f"google-chat-intake:{message_id}",
            job_id=job["id"],
            payload={
                "namespace": job["action_type"],
                "work_item_key": job["idempotency_key"],
                "source": "google-chat-intake",
            },
        )
    except (IdempotencyError, RunIsolationError) as exc:
        current = store.get_job(job["id"])
        if JobStatus(current["status"]) not in TERMINAL_STATUSES:
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.NEEDS_CLARIFICATION,
                    JobStatus.WAITING,
                },
                error=f"durable intake failed: {exc}",
                release_lease=True,
            )
        return job["id"]
    store.checkpoint(
        job["id"],
        "durable_work",
        {
            "namespace": job["action_type"],
            "work_item_key": job["idempotency_key"],
            "source": "google-chat-intake",
        },
    )
    schema_hold = bounded_schema_hold_reason(job["action_type"], job["payload"])
    if schema_hold:
        current = store.get_job(job["id"])
        if current["status"] == JobStatus.PENDING:
            store.transition(
                job["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                error=schema_hold,
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
        return job["id"]
    forbidden = forbidden_tool_request({}, text)
    if forbidden:
        current = store.get_job(job["id"])
        if JobStatus(current["status"]) not in TERMINAL_STATUSES:
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.NEEDS_CLARIFICATION,
                    JobStatus.WAITING,
                },
                error=forbidden,
                release_lease=True,
            )
        return job["id"]
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
    try:
        from .login_secret_health import maybe_preflight_login_secrets

        maybe_preflight_login_secrets(store, store.get_job(job["id"]))
    except Exception:
        logger.exception("login secret preflight failed; continuing fail-open")
    job = store.get_job(job["id"])
    if job["status"] == JobStatus.NEEDS_AUTH.value:
        return job["id"]
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
        from .job_schema import get_executable_skill_contract

        contract = get_executable_skill_contract(job["action_type"])
        if contract and contract.recording_policy == "EXEMPT":
            reason = (
                "recording exemption: read-only Drive ingestion has no browser UI and "
                "is verified by immutable snapshot hash reread"
                if job["action_type"] == "drive.skill_sync"
                else "recording exemption: authentication may display credentials or MFA data"
            )
            store.checkpoint(job["id"], "recording_exemption", {"reason": reason})
        else:
            RecordingManager(db_path).safe_start(job["id"])
    current = store.get_job(job["id"])
    if (
        current["action_type"] == "hermes.google_chat_task"
        and current["status"] == JobStatus.RUNNING.value
    ):
        start_generic_chat_job_heartbeat(db_path, current["id"])
    return job["id"]


def _post_job_audit_note(db_path: str, job_id: str) -> str:
    """Append the four-answer audit. Posted by the existing Chat APP send()."""
    try:
        audit = maybe_audit_terminal_job(db_path, job_id)
    except Exception as exc:
        return (
            f"\n\nROBIE post-job audit — {job_id} — UNKNOWN\n"
            f"1. Heartbeat gateway_progress: UNKNOWN ({type(exc).__name__})\n"
            f"2. Destination evidence: UNKNOWN\n"
            f"3. Recording motion: FAIL (audit crashed; fail-closed)\n"
            f"4. Tool vs recording: UNKNOWN\n"
            "Audit verdict: FAIL (does not authorize COMPLETE)"
        )
    if not audit:
        return ""
    return "\n\n" + format_audit_chat_message(audit)


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
        failure = (
            "Recording upload failed"
            if recording.get("failure_stage") == "UPLOAD"
            else "Recording failed"
        )
        return (
            f"\n\n⚠️ {failure}. "
            "The Job remains governed by the status above; check the Job ledger for details."
        )
    return ""


def _submission_audit_summary(store: JobStore, job_id: str) -> str:
    evidence = [
        item for item in store.list_evidence(job_id)
        if item.get("verified") and item.get("authoritative")
        and item.get("method") == "EZLYNX_PLAYWRIGHT_FRESH_READBACK"
    ]
    if not evidence:
        return ""
    observed = dict(evidence[-1].get("observed") or {})
    scope = dict(observed.get("scope") or {})
    result = dict(observed.get("postcondition") or {})
    required = (
        "pager_total", "mat_row_count", "status_aria_sort",
        "first_row_status", "first_closed_row_index",
        "rows_inspected_through_boundary",
    )
    if any(key not in result for key in required):
        return ""
    first_closed_number = int(result["first_closed_row_index"]) + 1
    statuses = ", ".join(str(item) for item in result.get("distinct_non_closed_statuses", []))
    return (
        "\n\nVerified Submission Center read-back:\n"
        f"• Time frame: {scope.get('time_frame', 'All Submissions')}\n"
        f"• Assigned producer scope: {scope.get('assigned_producer', 'Streetsmart Insurance')}\n"
        f"• My Submissions selected: {'yes' if scope.get('my_submissions') else 'no'}\n"
        f"• Visible rows: {result['mat_row_count']}\n"
        f"• Live pager total: {result['pager_total']}\n"
        f"• Status sort: {result['status_aria_sort']}\n"
        f"• First row status: {result['first_row_status']}\n"
        f"• First closed row: row {first_closed_number}\n"
        f"• Rows inspected through the boundary: {result['rows_inspected_through_boundary']}\n"
        f"• Non-closed statuses observed: {statuses or 'none'}\n"
        "• Record changes: none\n"
        "• Emails sent: none"
    )


def _publish_terminal_job(
    db_path: str,
    store: JobStore,
    job: dict[str, Any],
) -> dict[str, Any]:
    """Publish and reread one terminal Job before Chat advertises success."""
    job_id = str(job["id"])
    if store.get_checkpoint(job_id, "control_center_publication"):
        return store.get_job(job_id)
    sheet_id = os.environ.get("ROBIE_DASHBOARD_SHEET_ID", "").strip()
    if not sheet_id:
        store.checkpoint(
            job_id,
            "control_center_publication_exemption",
            {"reason": "ROBIE_DASHBOARD_SHEET_ID is not configured"},
        )
        return store.get_job(job_id)
    try:
        published = publish_job_to_control_center(db_path, sheet_id, job_id)
    except Exception as exc:
        error = f"Control Center publication failed: {type(exc).__name__}: {exc}"
        store.checkpoint(
            job_id,
            "control_center_publication_failed",
            {"status": "FAILED", "error": error},
        )
        if JobStatus(job["status"]) == JobStatus.COMPLETE:
            job = store.fail_unpublished_completion(job_id, error)
            # Best effort: publish the explicit failure row. The original
            # exception remains the Job result if the sheet is unavailable.
            try:
                publish_job_to_control_center(db_path, sheet_id, job_id)
            except Exception:
                pass
        return store.get_job(job_id)
    store.checkpoint(job_id, "control_center_publication", published)
    return store.get_job(job_id)


def _render_chat_terminal(
    store: JobStore,
    job: dict[str, Any],
    content: str,
    recordings: RecordingManager,
) -> str:
    job_id = job["id"]
    status = JobStatus(job["status"])
    if status == JobStatus.COMPLETE:
        publication = store.get_checkpoint(job_id, "control_center_publication")
        completion_line = (
            "The expected destination state was independently verified, the evidence was stored, "
            "and the Control Center row was reread successfully."
            if publication
            else "The expected destination state was independently verified and the evidence was stored."
        )
        verified_summary = (
            _submission_audit_summary(store, job_id)
            if job.get("action_type") == "ezlynx.submission_audit"
            else ""
        )
        worker_detail = "" if verified_summary else f"\n\n{content}"
        return (
            f"ROBIE Job {job_id} — COMPLETE\n\n"
            + completion_line
            + verified_summary
            + worker_detail
            + _recording_chat_note(recordings, job_id)
            + _post_job_audit_note(str(store.path), job_id)
        )
    if status == JobStatus.FAILED:
        return (
            f"ROBIE Job {job_id} — FAILED\n\n"
            "ROBIE could not safely finish the requested work. No success claims from the Computer Worker are being reported.\n\n"
            f"Reason: {job.get('last_error') or 'unknown error'}."
            + _recording_chat_note(recordings, job_id)
            + _post_job_audit_note(str(store.path), job_id)
        )
    if status == JobStatus.UNVERIFIED:
        return (
            f"ROBIE Job {job_id} — UNVERIFIED\n\n"
            "ROBIE attempted the work, but the destination state was not independently verified. "
            "Any success wording produced by the Computer Worker has been suppressed.\n\n"
            f"Reason: {job.get('last_error') or 'destination verification produced no authoritative evidence'}.\n\n"
            "Do not treat this Job as COMPLETE; it remains open for review or retry."
            + _recording_chat_note(recordings, job_id)
            + _post_job_audit_note(str(store.path), job_id)
        )
    return (
        f"ROBIE Job {job_id} — {status.value}\n\n"
        "ROBIE is not treating this request as successful. "
        f"Reason: {job.get('last_error') or 'waiting for a human or destination update'}."
        + _recording_chat_note(recordings, job_id)
    )


def guard_chat_response(
    db_path: str,
    job_id: str | None,
    content: str,
    *,
    verifiers: dict[str, Any] | None = None,
    recordings: RecordingManager | None = None,
) -> str:
    content = redact_text(content)
    if not job_id:
        if _looks_like_unbound_policy_success(content):
            return (
                "ROBIE is not treating this as successful. "
                "No bound Job and no fresh destination evidence were available, "
                "so a policy setup cannot be reported as complete."
            )
        return content
    store = JobStore(db_path)
    job = store.get_job(job_id)
    recordings = recordings or RecordingManager(db_path)
    blocker = structured_blocker_reason(content)
    if blocker and JobStatus(job["status"]) in {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
    }:
        payload = dict(job.get("payload") or {})
        interaction = interaction_for_blocker(
            blocker,
            action_type=job["action_type"],
            requester_name=payload.get("requested_by"),
            job_id=job_id,
            subject_name=(
                payload.get("company_name")
                or payload.get("client_name")
                or payload.get("account_name")
            ),
        )
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            store.transition(
                job_id,
                JobStatus.AWAITING_HUMAN_INPUT,
                expected={JobStatus(job["status"])},
                error=blocker,
                resume_status=(
                    JobStatus.PENDING
                    if JobStatus(job["status"]) == JobStatus.PENDING
                    else JobStatus.RUNNING
                ),
                release_lease=True,
            )
        else:
            DurableChatEventQueue(db_path).park_direct_human_input(
                conversation_id=conversation_id,
                job_id=job_id,
                interaction_state=interaction,
                error=blocker,
            )
        recordings.safe_stop(job_id, JobStatus.AWAITING_HUMAN_INPUT.value)
        return interaction["prompt"]
    if job["status"] == JobStatus.COMPLETE:
        recordings.safe_stop(job_id, JobStatus.COMPLETE.value)
        job = _publish_terminal_job(db_path, store, store.get_job(job_id))
        return _render_chat_terminal(store, job, content, recordings)
    if job["status"] == JobStatus.FAILED:
        recordings.safe_stop(job_id, JobStatus.FAILED.value)
        job = _publish_terminal_job(db_path, store, store.get_job(job_id))
        return _render_chat_terminal(store, job, content, recordings)
    if job["status"] == JobStatus.UNVERIFIED:
        recordings.safe_stop(job_id, JobStatus.UNVERIFIED.value)
        job = _publish_terminal_job(db_path, store, store.get_job(job_id))
        return _render_chat_terminal(store, job, content, recordings)
    if JobStatus(job["status"]) == JobStatus.AWAITING_HUMAN_INPUT:
        recordings.safe_stop(job_id, JobStatus.AWAITING_HUMAN_INPUT.value)
        payload = dict(job.get("payload") or {})
        return interaction_for_blocker(
            job.get("last_error") or "PLAYWRIGHT_BLOCKED",
            action_type=job["action_type"],
            requester_name=payload.get("requested_by"),
            job_id=job_id,
            subject_name=(
                payload.get("company_name")
                or payload.get("client_name")
                or payload.get("account_name")
            ),
        )["prompt"]
    if JobStatus(job["status"]) in WAITING_STATUSES:
        status = JobStatus(job["status"]).value
        recordings.safe_stop(job_id, status)
        return _render_chat_terminal(store, job, content, recordings)

    # The Computer Worker response is diagnostic only. A separate structured
    # action checkpoint and registered destination verifier are required.
    store.checkpoint(job_id, "worker_response", {"response_text": content})
    action = store.get_checkpoint(job_id, "action")
    registry = dict(verifiers or _default_chat_verifiers())
    verifier = registry.get(job["action_type"])
    if action and verifier:
        from .engine import JobEngine

        if JobStatus(job["status"]) == JobStatus.RUNNING:
            store.transition(job_id, JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        JobEngine(
            store,
            {},
            {job["action_type"]: verifier},
            recordings=recordings,
        ).run(job_id)
    elif _looks_in_progress(content):
        # A progress/wrapper response is not the worker's terminal result.
        # Keep the Job and recorder open so a later structured result can be
        # verified, cancelled, or reported with a precise blocker.
        return (
            f"ROBIE Job {job_id} — RUNNING\n\n"
            "ROBIE accepted the request and is still working. Completion has not been claimed."
        )
    else:
        current = store.get_job(job_id)
        if JobStatus(current["status"]) == JobStatus.RUNNING:
            store.transition(job_id, JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        current = store.get_job(job_id)
        if JobStatus(current["status"]) == JobStatus.VERIFYING:
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.VERIFYING},
                error=(
                    "no structured destination action checkpoint"
                    if not action
                    else f"no independent verifier registered for {job['action_type']}"
                ),
                release_lease=True,
            )
            recordings.safe_stop(job_id, JobStatus.UNVERIFIED.value)
    final = store.get_job(job_id)
    if JobStatus(final["status"]) in TERMINAL_STATUSES:
        final = _publish_terminal_job(db_path, store, final)
    return _render_chat_terminal(store, final, content, recordings)
