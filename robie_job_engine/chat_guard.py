from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .attachments import AttachmentRef, ingest_attachment_refs
from .chat_policy import execution_contract_lines, forbidden_tool_request
from .ezlynx_account_nav import account_nav_contract_lines
from .context_policy import (
    CONTINUATION_PREFIXES,
    CORRECTION_PREFIXES,
)
from .chat_queue import DurableChatEventQueue
from .idempotency import DurableWorkLedger, IdempotencyError
from .hitl import (
    coverage_fill_miss_hitl_text,
    formentry_mint_miss_hitl_text,
    interaction_for_blocker,
    policy_setup_fail_closed_hitl_text,
    policy_setup_fail_closed_reason,
    structured_blocker_reason,
)
from .worker_contract import classify_chat_close_without_checkpoint
from .action_gate import apply_action_gate, is_action_gate_refusal
from .job_schema import bounded_schema_hold_reason
from .models import TERMINAL_STATUSES, WAITING_STATUSES, JobStatus
from .runs import IsolatedRunStore, RunIsolationError, MessageMaintenanceDeferred
from .operations import ingest_chat_attachments
from .recording import RecordingManager
from .request_routing import BOUNDED_ENGINE_ACTIONS, classify_request
from .secrets import redact_mapping, redact_text
from .sheets_sync import publish_job_to_control_center
from .submission_routing import resolve_submission_route, submission_verification_requirements
from .playwright_observability import (
    bind_current_playwright_job,
    fail_closed_zero_playwright_rows,
    maybe_snapshot_and_bind,
)
from .post_job_audit import maybe_audit_terminal_job
from .store import JobStore


logger = logging.getLogger(__name__)

# The live Chat adapter installs this so a note tool can post on the job
# thread before the model speaks. Tests pass a poster instead.
_OUTCOME_POSTER: Callable[..., Any] | None = None


def install_chat_outcome_poster(poster: Callable[..., Any] | None) -> None:
    """Register the process-wide Chat poster for note outcomes."""
    global _OUTCOME_POSTER
    _OUTCOME_POSTER = poster

_BOUND_POLICY_TERMS = re.compile(
    r"(?:\b(?:renew|endorse|cancel|reassign)\b|"
    r"\b(?:process|complete|execute|handle|do)\s+(?:this\s+|the\s+)?"
    r"(?:renewal|endorsement|cancellation|reassignment)\b)",
    re.IGNORECASE,
)


def require_message_execution_available(db_path: str) -> None:
    """Leave durable intake retryable while the service configuration is fenced."""
    active = IsolatedRunStore(db_path).active_run()
    if active and active.get("owner") == "message-runtime-configuration":
        raise MessageMaintenanceDeferred("Message runtime maintenance is active; retry this durable event")


def pre_execution_hold_reason(
    text: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Return the deterministic exceptions to skipping generic confirmation."""
    values = dict(payload or {})
    if _BOUND_POLICY_TERMS.search(str(text or "")):
        return "clarify/HITL required for an already-bound policy action"
    if values.get("ambiguous_fields"):
        return "clarify/HITL required for ambiguous fields"
    if values.get("conflicting_fields"):
        return "clarify/HITL required for conflicting fields"
    if "target_match_count" in values:
        try:
            match_count = int(values["target_match_count"])
        except (TypeError, ValueError):
            match_count = -1
        if match_count != 1:
            return "clarify/HITL required unless the target resolves to exactly one match"
    return None

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
    bind_current_playwright_job(db_path, job_id)
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


def question_only_skips_recording(store: Any, job: dict[str, Any] | None, inbound_text: str = "") -> bool:
    """A question never records, including when an in-thread answer resumes it.

    The resume text is the answer, not the question. The original request
    and a question-only exemption already stored on the job still count.
    """
    if not job:
        return False
    job_id = str(job.get("id") or "")
    if job_id and store is not None:
        try:
            if store.get_checkpoint(job_id, "question_only"):
                return True
        except Exception:
            pass
    from .answer_only import is_answer_only_job, is_informational_ask

    if is_answer_only_job(job) or is_informational_ask(inbound_text):
        return True
    payload = dict(job.get("payload") or {})
    for key in ("request_text", "text", "prompt", "original_text"):
        if is_informational_ask(str(payload.get(key) or "")):
            return True
    job_id = str(job.get("id") or "")
    if not job_id or store is None:
        return False
    try:
        note = store.get_checkpoint(job_id, "recording_exemption") or {}
    except Exception:
        note = {}
    return "question only" in str(note.get("reason") or "").casefold()


def reopen_resumed_generic_chat_job(
    db_path: str,
    job_id: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Re-open a generic Chat Job after HITL. The Chat ack is not a claim.

    ``resume_human_input`` writes ``human_input_resume`` and clears
    ``lease_owner``. JobEngine never claims ``hermes.google_chat_task``, so
    that unleased RUNNING row looks abandoned unless the same
    ``open_chat_job`` claim signal (RUNNING + ``gateway_progress``) is
    written again. Production jobs 6cf6f6ae and
    da53765b-f2c7-4eef-8f48-72b8dc9d2157 died as
    ``orphan_timeout`` for this reason.
    """
    if not job_id:
        return None
    store = JobStore(db_path)
    job = store.get_job(job_id)
    refused = apply_action_gate(store, job, text=str((job.get("payload") or {}).get("text") or ""))
    if refused is not None:
        return refused
    if job["action_type"] != "hermes.google_chat_task":
        return job
    if job["status"] == JobStatus.PENDING.value:
        store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
        job = store.get_job(job_id)
    if job["status"] != JobStatus.RUNNING.value:
        return job
    resumed_text = str((job.get("payload") or {}).get("text") or "")
    if question_only_skips_recording(store, job, resumed_text):
        store.checkpoint(job_id, "question_only", {"reason": "question only"})
        store.checkpoint(
            job_id,
            "recording_exemption",
            {"reason": "question only; no browser recording"},
        )
    else:
        RecordingManager(db_path).safe_start(job_id)
    return start_generic_chat_job_heartbeat(db_path, job_id, now=now)


def notify_terminal_chat_job(
    db_path: str,
    job_id: str,
    *,
    poster: Any | None = None,
) -> dict[str, Any] | None:
    """Post the real terminal Chat status when no worker is left to send it.

    The 300s orphan watcher fails unclaimed generic Chat Jobs in the
    scheduler. That path used to persist ``post_job_audit`` and stay
    silent in Chat.
    """
    store = JobStore(db_path)
    try:
        job = store.get_job(job_id)
    except KeyError:
        return None
    if JobStatus(job["status"]) not in TERMINAL_STATUSES:
        return None
    from .user_reply import format_user_reply

    message = format_user_reply(
        guard_chat_response(db_path, job_id, job.get("last_error") or "job ended")
    )
    from .chat_app_post import conversation_target, post_as_chat_app
    from .chat_thread import read_job_chat_thread

    target = conversation_target(job)
    stored_thread = read_job_chat_thread(store, job_id)
    posted = False
    if target is not None:
        send = poster or post_as_chat_app
        space, thread = target
        if stored_thread:
            thread = stored_thread
        try:
            try:
                if thread:
                    send(space, message, thread_name=thread)
                else:
                    from .chat_thread import job_thread_key

                    send(space, message, thread_key=job_thread_key(job_id))
            except TypeError:
                try:
                    send(space, message, thread_name=thread)
                except TypeError:
                    send(space, message)
            posted = True
        except Exception:
            logger.exception("terminal Chat post failed job=%s", job_id)
    return {"job_id": job_id, "message": message, "posted": posted}


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


def _overdue_submission_report_payload() -> dict[str, Any]:
    """Return the server-owned scope and authorization for producer reports."""
    payload = _submission_audit_payload()
    payload.update(
        {
            "manifest_path": os.environ.get("ROBIE_ACCOUNTABILITY_MANIFEST", "").strip(),
            "authorized_actions": ["send_producer_reports"],
        }
    )
    return payload


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


_IN_PROGRESS_MAX_CHARS = 280


def _looks_in_progress(content: str) -> bool:
    text = str(content or "")
    # A finished essay can contain the word "working". Only a short status
    # line keeps the job and the recording open.
    if len(" ".join(text.split())) > _IN_PROGRESS_MAX_CHARS:
        return False
    # FAIL_CLOSED_MESSAGE contains the substring "playwright_exec". Job
    # c282de98 stored that exact fail-closed line and Chat rendered
    # "still working" because this helper treated it as in-progress.
    if policy_setup_fail_closed_reason(text):
        return False
    from .policy_setup_dispatch import is_formentry_mint_miss

    if is_formentry_mint_miss(text):
        return False
    if text.casefold().lstrip().startswith("robie_outcome_unknown:"):
        return False
    normalized = " ".join(text.casefold().split())
    return any(marker in normalized for marker in _IN_PROGRESS_MARKERS)


def chat_hermes_should_run(db_path: str, job_id: str | None) -> bool:
    """False when Chat already parked HITL / left a non-RUNNING state.

    After fail-closed policy setup, do not start a google_chat_task worker
    that will sit in RUNNING with a "still working" bubble. A HITL coverage
    resume fills through ezlynx_policy_setup — Hermes must not wander.
    """
    if not job_id:
        return True
    job = JobStore(db_path).get_job(job_id)
    if bool(dict(job.get("payload") or {}).get("hitl_resume")):
        return False
    return JobStatus(job["status"]) in {JobStatus.PENDING, JobStatus.RUNNING}


def park_policy_setup_fail_closed(
    db_path: str,
    job_id: str,
    content: str,
    *,
    recordings: RecordingManager | None = None,
    store: JobStore | None = None,
) -> str:
    """Fail closed AND post honest HITL. Leave AWAITING_HUMAN_INPUT, not RUNNING."""
    store = store or JobStore(db_path)
    job = store.get_job(job_id)
    recordings = recordings or RecordingManager(db_path)
    from .policy_setup_dispatch import is_coverage_fill_miss, is_formentry_mint_miss

    error = (
        policy_setup_fail_closed_reason(content)
        or str(content or "").strip()
        or "ezlynx_policy_setup is not registered; failing closed"
    )
    if is_formentry_mint_miss(content):
        prompt = formentry_mint_miss_hitl_text(
            job_id=job_id, detail=error, channel="chat"
        )
    elif is_coverage_fill_miss(content):
        prompt = coverage_fill_miss_hitl_text(
            job_id=job_id, detail=error, channel="chat"
        )
    else:
        prompt = policy_setup_fail_closed_hitl_text(
            job_id=job_id, detail=error, channel="chat"
        )
    from .hitl_ladder import stamp_hitl_posted_at

    payload = stamp_hitl_posted_at(dict(job.get("payload") or {}))
    store.update_payload(job_id, payload)
    interaction = {
        "awaiting": "human_input",
        "action_type": job["action_type"],
        "field_name": "operator_response",
        "field_label": "RETRY",
        "checkpoint": "policy_setup_tool_missing",
        "blocked_reason": error,
        "prompt": prompt,
        "accepts_value": True,
    }
    from .chat_app_post import post_hitl_to_originating_thread

    post_hitl_to_originating_thread(
        prompt, job_id=job_id, store=store, db_path=db_path
    )
    if JobStatus(job["status"]) in {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
    }:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        parked = False
        if conversation_id:
            try:
                DurableChatEventQueue(db_path).park_direct_human_input(
                    conversation_id=conversation_id,
                    job_id=job_id,
                    interaction_state=interaction,
                    error=error,
                )
                parked = True
            except Exception:
                parked = False
        if not parked:
            store.transition(
                job_id,
                JobStatus.AWAITING_HUMAN_INPUT,
                expected={
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.VERIFYING,
                },
                error=error,
                resume_status=(
                    JobStatus.PENDING
                    if JobStatus(job["status"]) == JobStatus.PENDING
                    else JobStatus.RUNNING
                ),
                release_lease=True,
            )
    recordings.safe_stop(job_id, JobStatus.AWAITING_HUMAN_INPUT.value)
    try:
        stop_generic_chat_job_heartbeat(db_path, job_id)
    except Exception:
        pass
    return prompt


def register_chat_verifier(action_type: str, verifier: Any) -> None:
    """Register a destination verifier during gateway bootstrap."""
    _CHAT_VERIFIERS[action_type] = verifier


def clear_chat_verifiers() -> None:
    """Test/bootstrap helper; never changes persisted Job evidence."""
    _CHAT_VERIFIERS.clear()


class _UnavailableVerifier:
    """Stands in for a verifier that failed to construct at registration.

    Leaving the slot empty makes engine._verify report the generic
    "no independent verifier registered", which cannot be told apart from an
    action type nobody ever wrote a verifier for. Registering this instead
    means the job's last_error names the real cause. engine._verify already
    routes an exception from verify() to _verification_retry_or_unverified,
    so the job still lands UNVERIFIED - it just says why.
    """

    def __init__(self, action_type: str, reason: str) -> None:
        self.action_type = action_type
        self.reason = reason

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> Any:
        raise RuntimeError(
            f"verifier for {self.action_type} failed to register at startup: {self.reason}"
        )


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
    verifiers: dict[str, Any] = {
        "filesystem.skill_update": FilesystemSkillUpdateVerifier(roots),
    }
    # Browser jobs driven from Chat previously found no verifier here and
    # fell to UNVERIFIED ("no independent verifier registered") even when
    # the work succeeded. Register the read-only CDP verifier so
    # verification-backed actions (mortgagee checks, renewal checks,
    # policy-change review) can actually complete.
    try:
        from .browser_read import BrowserReadVerifier
        from .chat_verifier_ports import CdpReadPort

        verifiers["browser.read"] = BrowserReadVerifier(CdpReadPort())
    except Exception as exc:
        logger.exception("browser.read verifier unavailable; those jobs stay UNVERIFIED")
        verifiers["browser.read"] = _UnavailableVerifier(
            "browser.read", f"{type(exc).__name__}: {exc}"
        )
    try:
        from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
        from .ezlynx_api_read_port import EzlynxApiClientReadPort
        from .chat_ezlynx_destination_verifier import HermesChatEzlynxDestinationVerifier

        try:
            client = EzlynxApiClient(load_ezlynx_api_config())
        except Exception:
            # Register anyway. Reads fail closed if Secret Manager is absent.
            client = None
        from .message_verification import MessageOutcomeVerifier
        from .answer_only import SkipDestinationReadback

        verifiers["hermes.google_chat_task"] = SkipDestinationReadback(
            MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(
                EzlynxApiClientReadPort(client)
            ))
        )
    except Exception as exc:
        logger.exception(
            "hermes.google_chat_task verifier unavailable; those jobs stay UNVERIFIED"
        )
        verifiers["hermes.google_chat_task"] = _UnavailableVerifier(
            "hermes.google_chat_task", f"{type(exc).__name__}: {exc}"
        )
    verifiers.update(_CHAT_VERIFIERS)
    return verifiers


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


def _policy_setup_contract(text: str) -> list[str]:
    """Hard routing for the homeowners policy-setup job class.

    When the Chat message asks to create/set up a homeowners policy on
    applicant 220250093 (policy numbers TEST-HO-*), the runner must invoke
    ezlynx_policy_setup as a real tool call before any playwright_exec. If the
    tool is missing/unregistered, fail closed with that error — never fall
    through to playwright_exec for this job class.
    """
    from .policy_setup_dispatch import FAIL_CLOSED_MESSAGE, detect_policy_setup_request

    args = detect_policy_setup_request(text)
    if not args:
        return []
    return [
        "Homeowners policy-setup job class detected "
        f"(policy {args['policy_number']} on applicant 220250093):",
        "- You MUST call the 'ezlynx_policy_setup' tool FIRST, before any "
        "'playwright_exec' call. It runs the Job Engine path: search-first, "
        "gold carrier create, Save & Continue Edit, FormEntry coverages by "
        "literal label. Pass the policy number from this request.",
        "- If 'ezlynx_policy_setup' is not in your available tools, STOP "
        "immediately and begin your response with exactly: "
        f"ROBIE_OUTCOME_UNKNOWN: {FAIL_CLOSED_MESSAGE}",
        "- Do NOT call 'playwright_exec' for this job class. The "
        "playwright_exec tool will refuse policy-setup jobs until "
        "ezlynx_policy_setup has been called.",
    ]


def _bind_chat_policy_setup(
    store: JobStore, job: dict[str, Any], text: str, db_path: str
) -> list[str]:
    """Register + invoke ezlynx_policy_setup for Chat, like the email runner.

    Prompt-only routing left job c282de98 at tool_called=false. The Chat
    worker must get a callable handler. When the handler is missing, fail
    closed and park honest HITL — never fall through to playwright_exec.
    """
    from .hermes_tool_visibility import register_policy_setup_callable
    from .policy_setup_dispatch import (
        FAIL_CLOSED_MESSAGE,
        POLICY_SETUP_REQUIRED_KIND,
        PolicySetupToolMissing,
        extract_policy_setup_args,
        invoke_policy_setup_tool,
        is_policy_setup_honest_hitl,
    )

    job_id = str(job.get("id") or "")
    payload = dict(job.get("payload") or {})
    if job_id and payload.get("hitl_resume"):
        from .chat_hitl import run_chat_hitl_coverage_resume

        applied = run_chat_hitl_coverage_resume(db_path, job_id)
        extra = [
            "- HITL resume applied Coverage A–F through ezlynx_policy_setup.",
            "- Do not call playwright_exec. POLICY_SETUP_ORDER still refuses wander.",
        ]
        if applied:
            extra.append(applied)
        return extra + _policy_setup_contract(text)

    contract = _policy_setup_contract(text)
    if not contract:
        return []
    if not job_id:
        return contract
    policy_args = extract_policy_setup_args(text) or {}
    existing = store.get_checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND) or {}
    handler = register_policy_setup_callable()
    marker = {
        "policy_number": policy_args.get("policy_number")
        or existing.get("policy_number"),
        "tool_called": bool(existing.get("tool_called")),
        "handler_registered": bool(handler),
    }
    store.checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND, marker)
    extra: list[str] = []
    if not handler:
        park_policy_setup_fail_closed(
            db_path, job_id, f"ROBIE_OUTCOME_UNKNOWN: {FAIL_CLOSED_MESSAGE}",
            store=store,
        )
        extra.extend(
            [
                f"ROBIE_OUTCOME_UNKNOWN: {FAIL_CLOSED_MESSAGE}",
                "- STOP. Do not call playwright_exec. The job is parked "
                "AWAITING_HUMAN_INPUT (not still working).",
            ]
        )
        return contract + extra
    extra.append(
        "- ezlynx_policy_setup is registered as a callable Chat worker tool "
        "(toolset=playwright). Call it — do not fall through to playwright_exec."
    )
    if marker["tool_called"]:
        extra.append(
            "- ezlynx_policy_setup already ran for this job (tool_called=true). "
            "Do not call playwright_exec for policy setup."
        )
        return extra + contract
    if not policy_args.get("policy_number"):
        return extra + contract
    try:
        report = invoke_policy_setup_tool(policy_args)
    except PolicySetupToolMissing as exc:
        park_policy_setup_fail_closed(
            db_path,
            job_id,
            f"ROBIE_OUTCOME_UNKNOWN: {exc}",
            store=store,
        )
        extra.extend(
            [
                f"ROBIE_OUTCOME_UNKNOWN: {exc}",
                "- STOP. Do not call playwright_exec. The job is parked "
                "AWAITING_HUMAN_INPUT (not still working).",
            ]
        )
        return extra + contract
    except Exception as exc:  # noqa: BLE001 - do not wander with playwright_exec
        extra.append(
            f"- Job Engine invoke raised {type(exc).__name__}: {exc}. "
            "Do not call playwright_exec for policy setup."
        )
        return extra + contract
    marker["tool_called"] = True
    store.checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND, marker)
    if isinstance(report, dict):
        payload = dict(job.get("payload") or {})
        number = str(
            report.get("policy_number") or policy_args.get("policy_number") or ""
        ).strip()
        pid = str(report.get("policy_id") or report.get("policyId") or "").strip()
        if number:
            payload["policy_number"] = number
        if pid:
            payload["policy_id"] = pid
        payload.setdefault("applicant_id", "220250093")
        store.update_payload(job_id, payload)
        dest = {"applicant_id": "220250093"}
        if number:
            dest["policy_number"] = number
        if pid:
            dest["policy_id"] = pid
        store.checkpoint(
            job_id,
            "action",
            {"action": "ezlynx_policy_setup", "destination": dest, "detail": {"report": report}},
        )
    extra.append(
        "- Job Engine invoked the callable ezlynx_policy_setup handler "
        "in-process (tool_called=true). Do not call playwright_exec for "
        "this job class."
    )
    blob = ""
    if isinstance(report, dict):
        blob = str(report.get("error") or report.get("message") or report)
    else:
        blob = str(report or "")
    if is_policy_setup_honest_hitl(blob) or "PLAYWRIGHT_BLOCKED" in blob:
        park_policy_setup_fail_closed(db_path, job_id, blob, store=store)
        from .policy_setup_dispatch import is_coverage_fill_miss

        if is_coverage_fill_miss(blob):
            extra.append(
                "- STOP AND ASK. Coverage labels were not filled. Amounts "
                "that were not on the job were not guessed. Job is "
                "AWAITING_HUMAN_INPUT (not still working)."
            )
        else:
            extra.append(
                "- STOP AND ASK. FormEntry was not minted. Job is "
                "AWAITING_HUMAN_INPUT (not still working)."
            )
    return extra + contract


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
    if store.get_checkpoint(job_id, "keep_chat_context") or store.get_checkpoint(
        job_id, "clarification_reply"
    ):
        combined = str((job.get("payload") or {}).get("text") or "").strip()
        if combined:
            text = combined
    if (
        job["action_type"] == "hermes.google_chat_task"
        and job["status"] == JobStatus.RUNNING.value
    ):
        start_generic_chat_job_heartbeat(db_path, job_id)
    if job["status"] == JobStatus.FAILED:
        if is_action_gate_refusal(job):
            return (
                text
                + "\n\n[ROBIE JOB ENGINE EXECUTION CONTRACT]\n"
                + "Do not attempt this Job: the Job Engine refused because Test "
                + "has no clean pass for this action. Do not open Ascend, "
                + "Playwright, or CDP. HITL after a miss is not the gate.\n"
                + "[END ROBIE JOB ENGINE EXECUTION CONTRACT]"
            )
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
    framing = str(dict(job.get("payload") or {}).get("task_framing") or "").strip()
    if framing:
        lines.append(framing)
    from .answer_only import (
        FORBIDDEN_READ_RULE,
        is_answer_only_job,
        purpose_built_instructions,
    )

    route = purpose_built_instructions(str(dict(job.get("payload") or {}).get("text") or text))
    if route and route not in lines:
        lines.append(route)
    if is_answer_only_job(job):
        lines.append(
            "This is a question. Answer it in plain English. "
            "Do not open EZLynx. Do not write a note or a document. "
            "Do not include your reasoning or thinking."
        )
    from .write_verification_loop import (
        get_locked_plan,
        is_ezlynx_write_job,
        locked_plan_instructions,
    )

    if is_ezlynx_write_job(job):
        locked = get_locked_plan(store, job_id)
        if locked:
            lines.append(locked_plan_instructions(locked))
        else:
            lines.append(
                "Before any tool or write, state a plan of exactly the write, "
                "the target, and the values. Do not invent a value. Do not write "
                "until that plan is locked. target may be the account id, the "
                "policy number, or the discussion name. If a write tool names "
                "the wrong field, fix that field once. If it refuses again, stop. "
                "Do not call the tool again."
            )
    lines.append(FORBIDDEN_READ_RULE)
    from .engine import is_retry_text
    from .runtime_env import playground_enabled

    if playground_enabled() and is_retry_text(text):
        lines.append(
            "This is a retry of the previous job. Do the original Chat request again. "
            "Do not bind, take payment, or email the client."
        )
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
        "When the request requires browser interaction on a website or web application (EZLynx, carrier portals, or external sites), you MUST execute it by calling the 'playwright_exec' tool directly. Do not output text claiming 'playwright_exec is unavailable' or simulating error messages without having actually executed the tool. Do not use generic terminal/bash commands for browser automation.",
        "When the user request provides every field the job schema requires, do not re-prompt with a generic 'Ready to proceed?' confirmation — proceed directly to execution. This does not relax clarify/HITL for genuinely ambiguous or conflicting fields, for any request affecting an already-bound policy (renewal, endorsement, cancellation, reassignment) regardless of field completeness, or for any case where the target account/applicant can't be resolved to exactly one match. Every action remains subject to independent post-job verification — destination evidence and structured playwright_exec proof — before COMPLETE is authorized; skipping the pre-execution prompt does not skip or weaken that check in any way.",
        "When navigating to an EZLynx account, try the direct URL (e.g. https://app.ezlynx.com/web/account/<id>/policies). If direct navigation does not find the applicant or stays on a listing page, use the global search bar to locate the applicant.",
        "EZLynx notes and documents are API-only. File notes with ezlynx_discussion_note (DiscussionApi add_note_to_discussion / file_note_to_existing_discussion). Upload files with ezlynx_document_upload (DocumentApi). Never use playwright_exec, a file chooser, Add Note, or Save Note to write notes or documents to EZLynx. Playwright is for forms and portals only. COMPLETE is refused without a DiscussionApi note_id or DocumentApi document_id.",
        "Complete every requested mutation (including status, premium, document attachment, and note when requested) through those APIs, not the browser.",
        "After saving, navigate away and reopen the exact destination. Read the freshly loaded server-backed state.",
        "Never claim success from modal text, a local DOM value, quote data, or your own prior action. If any requested field is absent, say the action is not verified.",
        "Do not write that you identified a carrier, are Filling Policy Shell, filled, saved, or uploaded unless a destination-action checkpoint already exists. With no destination-action checkpoint and no destination-verified evidence, say you were stuck and made no verified progress.",
        "If the request is ambiguous and does not name a client, policy, carrier, or task, stop immediately and begin the response with exactly: ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: <what is missing>. Ask one plain-English question. Do not investigate the server, read source code, read jobs.db, or open token files.",
        "Do not read Robie's own source, jobs.db, .hermes/google_token.json, or any token file during a job. Do not grep the server. Use the purpose-built tool named in the task.",
        "If execution is blocked because a required value is missing, stop and begin the response with exactly: ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: <field name>.",
        "If execution is blocked at an unresolved browser step or locator, stop and begin the response with exactly: ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: <specific step or locator>.",
        "If a write is PLAYWRIGHT_BLOCKED or a modal cannot be uniquely named, stop, describe the dialog title and visible labels only (no passwords), ask Gemini for one unique field, and HITL Carlo if Gemini is unsure. Never guess a field. Never use .first/.nth/.last.",
        "EZLynx notes must go on an existing titled discussion only (New Business, New Policy, Renewal, Cancellation, Submission Center, or another existing titled discussion) via the Discussion API. Never write a note on Untitled. Never create a discussion from Playwright. If the right title is missing, HITL Carlo — do not file on Untitled. Always include the exact phrase Robie was here. COMPLETE requires the DiscussionApi note_id from read-back.",
        "A commercial auto policy SHELL is not done. After the shell, click Save and Continue Edit (or Actions → Edit) and finish vehicles, drivers, garaging, symbols, limits, and banks from the quote. Do not bind. Keep the named insured on the file. Do not invent coverage.",
        "Do not emit ROBIE_BLOCKED for a completed action, a general question, or an ordinary explanation.",
        "The Job Engine, not the Computer Worker, has final completion authority.",
    ])
    payload = dict(job.get("payload") or {})
    payload.setdefault("action_type", job.get("action_type"))
    lines.extend(account_nav_contract_lines(text, payload))
    payload = dict(job.get("payload") or {})
    original = str(payload.get("text") or "").strip()
    if original and original != text:
        lines.append(f"Original Chat request: {original}")
    values = dict(payload.get("human_input_values") or {})
    if values:
        lines.append("Human operator replies from the HITL checkpoint:")
        for field, value in values.items():
            lines.append(f"- {redact_text(str(field))}: {redact_text(str(value))}")
    lines.extend(execution_contract_lines())
    lines.extend(_submission_contract(text))
    policy_setup_lines = _bind_chat_policy_setup(store, job, text, db_path)
    lines.extend(policy_setup_lines)
    lines.append("[END ROBIE JOB ENGINE EXECUTION CONTRACT]")
    return text + "\n".join(lines)


def _apply_explicit_retry(store: JobStore, job: dict[str, Any]) -> str | None:
    """Checkpoint RETRY. Return a refusal reason, or None after an allowed resume.

    AWAITING_HUMAN_INPUT is left for the existing resume path. FAILED and
    UNVERIFIED are re-opened only when leftover_retry_hold_reason allows it
    (playground on, younger than 24 hours).
    """
    from .engine import leftover_retry_hold_reason, resume_terminal_for_playground_retry

    reason = leftover_retry_hold_reason(job)
    store.checkpoint(
        job["id"],
        "leftover_retry",
        {"refused": bool(reason), "reason": reason, "auto_retry": False},
    )
    if reason:
        return reason
    status = JobStatus(job["status"])
    if status in {JobStatus.FAILED, JobStatus.UNVERIFIED}:
        resume_terminal_for_playground_retry(store, job)
    return None


def _retry_target_job_id(
    store: JobStore,
    queue: DurableChatEventQueue,
    conversation_id: str,
    active_job_id: str | None,
) -> str | None:
    """Pick the job a retry message should touch.

    Flag off keeps today's active link. Playground keeps a fresh HITL, and
    otherwise uses the last failed or unverified job on the thread.
    """
    from .runtime_env import playground_enabled

    if not playground_enabled():
        return active_job_id
    if active_job_id:
        active = store.get_job(active_job_id)
        if active.get("status") == JobStatus.AWAITING_HUMAN_INPUT.value:
            return active_job_id
    found = queue.latest_terminal_job_id(
        conversation_id, statuses=("FAILED", "UNVERIFIED")
    )
    return found or active_job_id


def retry_refusal_reply(store: JobStore, job_id: str) -> str | None:
    """Plain-English Chat note when this job's latest RETRY was refused."""
    checkpoint = store.get_checkpoint(job_id, "leftover_retry") or {}
    if not checkpoint.get("refused"):
        return None
    job = store.get_job(job_id)
    reason = str(checkpoint.get("reason") or "")
    from . import status_format

    return status_format.render_simple_status(
        headline="Not retrying.",
        what_happened=status_format.plain_retry_refusal(
            reason, status=str(job.get("status") or "")
        ),
        anything_needed="Send the request again if you still want it done.",
        status_line="Not restarted.",
        details=f"Technical detail: {reason}",
        job_id=job_id,
    )


def retry_without_job_reply() -> str:
    """Plain-English note when retry has no job on the thread."""
    from . import status_format

    return status_format.render_simple_status(
        headline="Not retrying.",
        what_happened="There's no failed or unfinished job in this thread to retry.",
        anything_needed="Send the request again.",
        status_line="Not restarted.",
        details="Technical detail: no job was linked to this thread.",
    )


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
    inbound_thread_id: str | None = None,
) -> str | None:
    """Create the Job before execution and bind durable attachment artifacts."""
    store = JobStore(db_path)
    orphaned = store.fail_orphaned_chat_jobs()
    from .chat_job_controls import stop_recordings_for_jobs, sweep_dead_running_jobs

    dead = sweep_dead_running_jobs(store)
    stop_recordings_for_jobs(
        db_path,
        list(dict.fromkeys([*orphaned, *dead])),
        JobStatus.FAILED.value,
    )
    try:
        from .chat_job_controls import expire_stale_waiting_jobs

        expire_stale_waiting_jobs(store)
    except Exception:
        logger.exception("stale waiting-job expire failed; continuing")
    try:
        from .hitl_ladder import expire_unanswered_hitl_jobs

        expire_unanswered_hitl_jobs(store)
    except Exception:
        logger.exception("unanswered HITL expire failed; continuing")
    try:
        from .tab_cleanup import flush_tabs_at_job_start

        flush_tabs_at_job_start(db_path=db_path)
    except Exception:
        logger.exception("start-of-job tab flush failed; continuing")
    queue = DurableChatEventQueue(db_path)
    context_key = conversation_id or f"google-chat:{requested_by or 'unknown'}"
    from .chat_hitl import (
        find_parked_chat_hitl_job,
        ingest_chat_hitl_reply,
        is_chat_coverage_hitl_resume_reply,
    )

    parked_hitl = find_parked_chat_hitl_job(store, context_key, queue=queue)
    if parked_hitl and is_chat_coverage_hitl_resume_reply(store, parked_hitl, text):
        from .engine import is_retry_text

        if is_retry_text(text) and _apply_explicit_retry(store, parked_hitl):
            return parked_hitl["id"]
        ingest_chat_hitl_reply(
            store,
            job_id=parked_hitl["id"],
            message_id=message_id,
            text=text,
        )
        queue.link_conversation_job(
            conversation_id=context_key,
            job_id=parked_hitl["id"],
            message_id=message_id,
            event_id=message_id,
            relation="CONTINUATION",
        )
        return parked_hitl["id"]
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
    from .engine import is_retry_text

    if is_retry_text(normalized):
        explicit_continuation = True
    if classification.hold_status == JobStatus.FAILED.value:
        # Removed integrations never resume or retarget an existing executable
        # job, even when the message uses continuation-shaped language.
        related_only = False
        explicit_continuation = False
    resume_context = queue.active_conversation_job(context_key)
    from .chat_turn_control import conversation_must_start_fresh

    active_for_turn = None
    active_for_turn_id = (resume_context or {}).get("job_id")
    if active_for_turn_id:
        try:
            active_for_turn = store.get_job(active_for_turn_id)
        except KeyError:
            active_for_turn = None
    from .chat_job_controls import waiting_job_to_bind

    bind_target = waiting_job_to_bind(store, text, inbound_thread_id)
    continue_clarification = bool(
        bind_target is not None
        and not conversation_must_start_fresh(store, bind_target)
    )
    if continue_clarification:
        active_for_turn = bind_target
    elif (
        active_for_turn
        and str(active_for_turn.get("status") or "") == JobStatus.NEEDS_CLARIFICATION.value
    ):
        # Only a reply inside the waiting job's thread answers it.
        # A top-level message starts a new job.
        related_only = False
        explicit_continuation = False
        queue.deactivate_conversation(context_key)
        resume_context = None
        active_for_turn = None
    if active_for_turn and conversation_must_start_fresh(store, active_for_turn):
        # The stopped, timed-out, or finished job must not swallow the next message.
        queue.deactivate_conversation(context_key)
        resume_context = None
        related_only = False
        explicit_continuation = False
    resume_state = dict((resume_context or {}).get("interaction_state") or {})
    if (
        resume_state.get("resume_mode") == "direct"
        and resume_state.get("resumed_by_message_id") == message_id
    ):
        explicit_continuation = True
    continued_job: dict[str, Any] | None = None
    revival = ""
    if active_for_turn is not None:
        from .chat_job_controls import dedupe_note_revival

        revival = dedupe_note_revival(
            store, active_for_turn, text, inbound_thread_id
        )
    if revival == "block":
        # A parked or unverified repeat-note job is not a continuation of
        # the next message. Leave it waiting and open a new job.
        queue.deactivate_conversation(context_key)
        related_only = False
        explicit_continuation = False
        continue_clarification = False
        active_for_turn = None
        resume_context = None
    elif revival == "no" and not continue_clarification and active_for_turn is not None:
        from .chat_job_controls import close_declined_note_repost

        close_declined_note_repost(store, active_for_turn["id"], text)
        return active_for_turn["id"]
    elif revival == "yes" and not continue_clarification and active_for_turn is not None:
        from .chat_job_controls import (
            _yes_already_spent,
            mark_job_waiting_for_user,
            note_repost_confirmed_by_reply,
            repeat_note_question,
        )
        from .chat_turn_control import clear_agent_stop

        dedupe_id = active_for_turn["id"]
        if _yes_already_spent(store, dedupe_id):
            revival = ""
        else:
            clear_agent_stop(dedupe_id)
            note_repost_confirmed_by_reply(store, dedupe_id, text)
        if revival == "yes" and JobStatus(active_for_turn["status"]) == JobStatus.UNVERIFIED:
            mark_job_waiting_for_user(
                store,
                dedupe_id,
                repeat_note_question(store, dedupe_id),
                from_unverified=True,
            )
        if revival == "yes":
            clear_agent_stop(dedupe_id)
            resumed = store.get_job(dedupe_id)
            if JobStatus(resumed["status"]) in WAITING_STATUSES:
                store.resume(dedupe_id)
            queue.link_conversation_job(
                conversation_id=context_key,
                job_id=dedupe_id,
                message_id=message_id,
                event_id=message_id,
                relation="CONTINUATION",
            )
            return dedupe_id
    from .chat_job_controls import ALREADY_DONE, note_already_done_target

    done_id = note_already_done_target(
        store,
        text,
        conversation_id=context_key,
        inbound_thread_id=inbound_thread_id,
    )
    if done_id and not continue_clarification:
        store.checkpoint(done_id, "note_already_done", {"reply": ALREADY_DONE})
        return done_id
    if continue_clarification and active_for_turn is not None:
        original = str((active_for_turn.get("payload") or {}).get("text") or "").strip()
        reply = str(text or "").strip()
        combined = original
        if reply and reply not in original:
            combined = f"{original}\n\nUser reply: {reply}".strip()
        payload = dict(active_for_turn.get("payload") or {})
        payload["text"] = combined
        if not str(payload.get("original_text") or "").strip() and original:
            payload["original_text"] = original
        payload["clarification_reply"] = reply
        store.update_payload(active_for_turn["id"], payload)
        store.checkpoint(
            active_for_turn["id"],
            "clarification_reply",
            {"message_id": message_id, "text": reply, "combined": combined},
        )
        from .chat_job_controls import (
            close_declined_note_repost,
            note_repost_confirmed_by_reply,
        )

        if close_declined_note_repost(store, active_for_turn["id"], reply):
            return active_for_turn["id"]
        note_repost_confirmed_by_reply(store, active_for_turn["id"], reply)
        store.checkpoint(
            active_for_turn["id"],
            "keep_chat_context",
            {"reason": "needs_clarification"},
        )
        from .chat_turn_control import clear_agent_stop

        clear_agent_stop(active_for_turn["id"])
        store.resume(active_for_turn["id"])
        queue.link_conversation_job(
            conversation_id=context_key,
            job_id=active_for_turn["id"],
            message_id=message_id,
            event_id=message_id,
            relation="CONTINUATION",
        )
        continued_job = store.get_job(active_for_turn["id"])
    elif related_only or explicit_continuation:
        current = queue.active_conversation_job(context_key)
        active_job_id = current.get("job_id") if current else None
        if is_retry_text(text):
            active_job_id = _retry_target_job_id(
                store, queue, context_key, active_job_id
            )
        if not active_job_id:
            return None
        active_job = store.get_job(active_job_id)
        would_resume = (
            explicit_continuation
            or JobStatus(active_job["status"]) in WAITING_STATUSES
        )
        if would_resume:
            refused = apply_action_gate(store, active_job, text=text)
            if refused is not None:
                return refused["id"]
        if would_resume:
            if is_retry_text(text) and _apply_explicit_retry(store, active_job):
                return active_job_id
            active_job = store.get_job(active_job_id)
        skip_bind = False
        if JobStatus(active_job["status"]) in WAITING_STATUSES:
            from .chat_job_controls import dedupe_note_revival

            if dedupe_note_revival(store, active_job, text, inbound_thread_id) == "block":
                queue.deactivate_conversation(context_key)
                related_only = False
                explicit_continuation = False
                skip_bind = True
            else:
                store.resume(active_job_id)
        elif JobStatus(active_job["status"]) == JobStatus.UNVERIFIED:
            from .chat_job_controls import dedupe_note_revival
            from .chat_turn_control import clear_agent_stop

            if dedupe_note_revival(store, active_job, text, inbound_thread_id) == "block":
                queue.deactivate_conversation(context_key)
                related_only = False
                explicit_continuation = False
                skip_bind = True
            else:
                clear_agent_stop(active_job_id)
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
        if not skip_bind:
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
    if classification.action_type == "ezlynx.overdue_submission_reports":
        server_payload.update(_overdue_submission_report_payload())
    from .request_routing import PLAYGROUND_TASK_FRAMING, chat_turn_expects_ui

    framing = PLAYGROUND_TASK_FRAMING.get(classification.action_type)
    if framing:
        server_payload["task_framing"] = framing
    server_payload.update(dict(action_payload or {}))
    server_payload["expected_ui"] = chat_turn_expects_ui(
        text,
        classification.action_type,
        answer_only=bool(getattr(classification, "answer_only", False)),
    )
    if continued_job is None:
        if is_retry_text(text):
            parked = resume_context or queue.active_conversation_job(context_key)
            parked_id = (parked or {}).get("job_id")
            if not parked_id:
                parked_id = _retry_target_job_id(store, queue, context_key, None)
            if parked_id:
                existing = store.get_job(parked_id)
                if _apply_explicit_retry(store, existing):
                    return parked_id
                existing = store.get_job(parked_id)
                if JobStatus(existing["status"]) == JobStatus.AWAITING_HUMAN_INPUT:
                    store.resume(parked_id)
                    continued_job = store.get_job(parked_id)
                elif JobStatus(existing["status"]) in {
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.VERIFYING,
                }:
                    continued_job = existing
    if continued_job is not None:
        if is_chat_coverage_hitl_resume_reply(store, continued_job, text):
            ingest_chat_hitl_reply(
                store,
                job_id=continued_job["id"],
                message_id=message_id,
                text=text,
            )
            continued_job = store.get_job(continued_job["id"])
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
                    "request_text": text,
                    "original_text": text,
                    "answer_only": bool(getattr(classification, "answer_only", False)),
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
    refused = (
        None
        if classification.hold_status == JobStatus.FAILED.value
        else apply_action_gate(store, store.get_job(job["id"]), text=text)
    )
    if refused is not None:
        return refused["id"]
    if classification.hold_status == JobStatus.FAILED.value:
        current = store.get_job(job["id"])
        if JobStatus(current["status"]) not in TERMINAL_STATUSES:
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={JobStatus.PENDING, JobStatus.RUNNING},
                error="ASCEND_UNAVAILABLE: Ascend is excluded from this release",
                release_lease=True,
            )
        store.checkpoint(
            job["id"],
            "destination_verification",
            {"verified": False, "reason": "ASCEND_UNAVAILABLE"},
        )
        return job["id"]
    from .chat_job_controls import hard_block_reply

    blocked_line = None if continued_job is not None else hard_block_reply(text)
    if blocked_line:
        current = store.get_job(job["id"])
        if JobStatus(current["status"]) not in TERMINAL_STATUSES:
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.NEEDS_CLARIFICATION,
                },
                error=blocked_line,
                release_lease=True,
            )
        store.checkpoint(job["id"], "hard_block", {"reply": blocked_line})
        return job["id"]
    pre_execution_hold = pre_execution_hold_reason(text, server_payload)
    if pre_execution_hold:
        current = store.get_job(job["id"])
        if current["status"] == JobStatus.PENDING.value:
            store.transition(
                job["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                error=pre_execution_hold,
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
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
    job = store.get_job(job["id"])
    if is_action_gate_refusal(job) or job["status"] == JobStatus.FAILED.value:
        return job["id"]
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
            from .answer_only import CLARIFICATION_QUESTION

            store.transition(
                job["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                error="request is too vague to execute safely",
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
            store.checkpoint(
                job["id"],
                "clarification",
                {"question": CLARIFICATION_QUESTION, "asked": True},
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
        require_message_execution_available(db_path)
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
            if question_only_skips_recording(store, current, text):
                store.checkpoint(
                    job["id"],
                    "question_only",
                    {"reason": "question only"},
                )
                store.checkpoint(
                    job["id"],
                    "recording_exemption",
                    {"reason": "question only; no browser recording"},
                )
            else:
                RecordingManager(db_path).safe_start(job["id"])
    current = store.get_job(job["id"])
    if current["status"] not in {JobStatus.COMPLETE.value, JobStatus.FAILED.value}:
        try:
            from .tab_cleanup import refuse_wrong_host_at_job_start

            verdict = refuse_wrong_host_at_job_start(db_path=db_path, job=current, text=text)
            if verdict.get("refused") and verdict.get("reason"):
                store.transition(
                    current["id"],
                    JobStatus.FAILED,
                    expected={
                        JobStatus.PENDING,
                        JobStatus.RUNNING,
                        JobStatus.VERIFYING,
                        JobStatus.NEEDS_CLARIFICATION,
                        JobStatus.WAITING,
                    },
                    error=verdict["reason"],
                    release_lease=True,
                )
                return current["id"]
        except Exception:
            logger.exception("wrong-host refuse check failed; continuing without attach")
    current = store.get_job(job["id"])
    if (
        current["action_type"] == "hermes.google_chat_task"
        and current["status"] == JobStatus.RUNNING.value
    ):
        start_generic_chat_job_heartbeat(db_path, current["id"])
        maybe_snapshot_and_bind(db_path, current["id"], phase="start")
    return job["id"]


def guard_chat_notice(db_path: str, job_id: str | None, content: str) -> str:
    """Keep a ceiling notice to the one line. The audit stays in the ledger."""
    text = str(content or "").strip()
    if not job_id:
        return text
    try:
        from .post_job_audit import maybe_audit_terminal_job

        maybe_audit_terminal_job(db_path, job_id)
    except Exception:
        logger.exception("ceiling notice left the audit in the ledger only job=%s", job_id)
    return text


def _post_job_audit_note(
    db_path: str,
    job_id: str,
    recordings: RecordingManager | None = None,
) -> str:
    """Persist the audit on the job. The CSR reply does not include it.

    The full checklist stays in the job log and the health-channel poster.
    Stop the recording first so the audit does not read a row still
    marked RECORDING.
    """
    try:
        if recordings is not None:
            status = JobStatus.UNVERIFIED.value
            try:
                status = str(JobStore(db_path).get_job(job_id).get("status") or status)
            except Exception:
                status = JobStatus.UNVERIFIED.value
            if status not in _RECORDING_KEEP_OPEN:
                recordings.safe_stop(job_id, status)
        maybe_audit_terminal_job(db_path, job_id)
        if recordings is not None:
            recordings.release_local_after_audit(job_id)
    except Exception:
        logger.exception("post-job audit stayed in the ledger only job=%s", job_id)
    return ""


def _login_secret_chat_note(store: JobStore, job_id: str) -> str:
    """Surface leftover DESTROYED versions without calling the password destroyed."""
    report = store.get_checkpoint(job_id, "login_secret_health") or {}
    if not report:
        return ""
    from .login_secret_health import format_leftover_note

    note = format_leftover_note(report)
    if not note or "[REDACTED]" in note:
        return ""
    return f"\n\nLogin secret: {note}."


def _recording_chat_note(recordings: RecordingManager, job_id: str) -> str:
    """Return a user-facing recording link without weakening the status gate."""
    recording = recordings.store.latest(job_id)
    if not recording:
        return ""
    if recording.get("status") == "READY" and recording.get("drive_url"):
        from .answer_only import saved_span_sentence

        start = str(recording.get("local_path") or "").strip()
        end = str(recording.get("drive_url") or "").strip()
        span = saved_span_sentence(start, end)
        saved = f"\n{span}" if start and end else ""
        return (
            "\n\n🎥 Review this job recording: "
            f"{recording['drive_url']}"
            f"{saved}\n"
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


def _playground_answer_detail(content: str) -> str:
    """Robie's actual words, included only while playground is on."""
    from .runtime_env import playground_enabled

    if not playground_enabled():
        return ""
    text = str(content or "").strip()
    if not text:
        return ""
    return "Robie's answer:\n" + text


def _is_playground_informational_answer(
    store: JobStore,
    job: dict[str, Any],
    content: str,
) -> bool:
    """A plain question with no destination action, playground only.

    EZLynx quote / policy-change / certificate jobs stay on the normal
    status line. Their answer is still included via ``_playground_answer_detail``.
    """
    from .runtime_env import playground_enabled
    from .worker_contract import (
        claimed_destination_progress_or_complete,
        extract_infra_close_error,
    )

    if not playground_enabled():
        return False
    if str(job.get("action_type") or "").startswith("ezlynx."):
        return False
    if store.get_checkpoint(job["id"], "action"):
        return False
    if JobStatus(job["status"]) != JobStatus.UNVERIFIED:
        return False
    error = str(job.get("last_error") or "")
    if error not in {"", "no structured destination action checkpoint"}:
        return False
    if not str(content or "").strip():
        return False
    if claimed_destination_progress_or_complete(content):
        return False
    if extract_infra_close_error(content, error):
        return False
    return True


def _render_chat_terminal(
    store: JobStore,
    job: dict[str, Any],
    content: str,
    recordings: RecordingManager,
) -> str:
    """Render the terminal status in the simple shared format.

    Jake's template (What happened / Anything needed / Status) leads in
    plain words; our verification detail (confirmed facts, gaps, evidence)
    follows below it. Shared with the email renderer via status_format so
    every user gets the same shape. Internal codes are translated for
    display only -- detection on raw worker text is untouched.
    """
    from .answer_only import (
        FIXTURE_POLICY_MARKER,
        LIVE_LOOKUP_FAILED,
        is_answer_only_job,
    )
    from .end_state_report import end_state_report_enabled, render_job_end_state

    if FIXTURE_POLICY_MARKER in str(content or ""):
        return LIVE_LOOKUP_FAILED + "\n"
    forced = _unproved_field_user_reply(store, job)
    if forced:
        from . import status_format

        ref = status_format.short_job_ref(job["id"])
        body = forced if not ref else f"{forced}\n\n{ref}"
        _post_job_audit_note(str(store.path), job["id"], recordings)
        return body.strip() + "\n"
    note_reply = _discussion_note_user_reply(store, job)
    if note_reply:
        _post_job_audit_note(str(store.path), job["id"], recordings)
        return note_reply if note_reply.endswith("\n") else note_reply + "\n"
    # A question is answered in the reply. EZLynx is not the destination.
    if is_answer_only_job(job) or dict(job.get("payload") or {}).get("answered"):
        from .user_reply import format_user_reply

        _post_job_audit_note(str(store.path), job["id"], recordings)
        answer = str(content or "").strip() or "Answered."
        if not answer.lower().startswith("answered"):
            answer = f"Answered. {answer}"
        text = format_user_reply(answer)
        return text if text.endswith("\n") else text + "\n"
    from .runtime_env import playground_enabled
    from .write_verification_loop import (
        is_ezlynx_write_job,
        nothing_written_line,
        unwritten_write_reason,
        write_landed,
        write_reply_if_planned,
    )

    keep_quote = (
        playground_enabled() and str(job.get("action_type") or "") == "ezlynx.quote"
    )
    if is_ezlynx_write_job(job) and not keep_quote:
        planned_reply = write_reply_if_planned(store, job, content)
        if write_landed(store, job) and planned_reply:
            return planned_reply
        if not write_landed(store, job):
            return nothing_written_line(unwritten_write_reason(store, job)) + "\n"
    else:
        planned_reply = write_reply_if_planned(store, job, content)
        if planned_reply:
            return planned_reply
    # Flag on: one end-state report scored by Jev. The old "Not verified"
    # wording is display-only and is skipped here. Deterministic verifiers
    # still ran before this render; a failed hard readback forces wrong.
    if end_state_report_enabled():
        return render_job_end_state(
            store, job, content, recordings=recordings, channel="chat"
        )

    from . import status_format

    job_id = job["id"]
    status = JobStatus(job["status"])
    from .message_results import verification_summary
    checked = str(verification_summary(store, job_id) or "").strip()

    if _is_playground_informational_answer(store, job, content):
        answer = str(content or "").strip()
        raw_reason = str(
            job.get("last_error") or "no structured destination action checkpoint"
        )
        return status_format.render_simple_status(
            headline="Answered.",
            what_happened=answer,
            anything_needed="No.",
            status_line="Answered — nothing was changed.",
            details="\n\n".join(
                part
                for part in (
                    checked,
                    "No destination check was required. This was a question, not an action.",
                    f"Technical detail: {raw_reason}",
                    _recording_chat_note(recordings, job_id),
                    _post_job_audit_note(str(store.path), job_id, recordings),
                )
                if str(part or "").strip()
            ),
            job_id=job_id,
        )

    def _details(*chunks: str) -> str:
        return "\n\n".join(c for c in (str(s or "").strip() for s in chunks) if c)

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
        worker_detail = "" if verified_summary or (checked and job.get("action_type") == "hermes.google_chat_task") else str(content or "")
        return status_format.render_simple_status(
            headline="Done.",
            what_happened=completion_line,
            anything_needed="No.",
            status_line="Verified \u2014 the result was checked against the destination.",
            details=_details(
                verified_summary,
                worker_detail,
                checked,
                _recording_chat_note(recordings, job_id),
                _post_job_audit_note(str(store.path), job_id, recordings),
            ),
            job_id=job_id,
        )
    if status == JobStatus.FAILED:
        raw_reason = str(job.get("last_error") or "unknown error")
        return status_format.render_simple_status(
            headline="Couldn't finish.",
            what_happened=status_format.plain_reason(raw_reason),
            anything_needed="Needs a human to review and retry if appropriate.",
            status_line="Failed.",
            details=_details(
                _playground_answer_detail(content),
                checked,
                _recording_chat_note(recordings, job_id),
                _login_secret_chat_note(store, job_id),
                _post_job_audit_note(str(store.path), job_id, recordings),
                f"Technical detail: {raw_reason}",
            ),
            job_id=job_id,
        )
    if status == JobStatus.UNVERIFIED:
        raw_reason = str(job.get("last_error") or "destination verification produced no authoritative evidence")
        return status_format.render_simple_status(
            headline="Not verified.",
            what_happened=(
                "Robie tried the work, but the full result couldn't be independently confirmed. "
                "Unconfirmed success claims are suppressed. "
                + status_format.plain_reason(raw_reason)
            ),
            anything_needed="Review the details below, then retry or confirm manually \u2014 don't treat this as done.",
            status_line="Not verified \u2014 treat as incomplete until confirmed.",
            details=_details(
                _playground_answer_detail(content),
                checked,
                _recording_chat_note(recordings, job_id),
                _login_secret_chat_note(store, job_id),
                _post_job_audit_note(str(store.path), job_id, recordings),
                f"Technical detail: {raw_reason}",
            ),
            job_id=job_id,
        )
    needs_input = status == JobStatus.AWAITING_HUMAN_INPUT
    raw_reason = str(job.get("last_error") or "waiting for a human or destination update")
    return status_format.render_simple_status(
        headline="Waiting on you." if needs_input else "Waiting.",
        what_happened=f"The job is {status.value} \u2014 {status_format.plain_reason(raw_reason)}.",
        anything_needed=(
            "Your input is needed \u2014 see the details below."
            if needs_input
            else "Nothing yet \u2014 the job hasn't finished."
        ),
        status_line="Not finished.",
        details=_details(
            _playground_answer_detail(content),
            checked,
            _recording_chat_note(recordings, job_id),
            _login_secret_chat_note(store, job_id),
        ),
        job_id=job_id,
    )


def _account_display_name(job: dict[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    for key in (
        "account_name",
        "client_name",
        "company_name",
        "applicant_name",
        "customer_name",
    ):
        name = " ".join(str(payload.get(key) or "").split()).strip()
        if name:
            return name
    return "the account"


def _is_address_or_holder_job(job: dict[str, Any]) -> bool:
    payload = dict(job.get("payload") or {})
    text = str(payload.get("text") or "")
    folded = text.casefold()
    action = str(job.get("action_type") or "")
    address = action == "ezlynx.policy_change" or "mailing address" in folded
    holder = action == "ezlynx.certificate" or (
        "certificate" in folded or "certificate holder" in folded
    )
    return address or holder


def _discussion_note_user_reply(store: JobStore, job: dict[str, Any]) -> str:
    """One line from the readback. The audit stays off this reply."""
    note = store.get_checkpoint(job["id"], "discussion_note") or {}
    discussion_id = str(note.get("discussion_id") or "").strip()
    if not discussion_id or _is_address_or_holder_job(job):
        return ""
    from .post_job_audit import api_readback_confirms_write

    name = _account_display_name(job)
    title = " ".join(str(note.get("discussion_title") or "").split()).strip() or "the discussion"
    if str(note.get("verified_by") or "") == "count" or str(note.get("status") or "") == "sent":
        return _count_note_user_line(name, title)
    if api_readback_confirms_write(store, job["id"]):
        sentence = f'Added the note to {name} on "{title}".'
    else:
        sentence = "I couldn't confirm that landed, please check."
    return sentence


def _count_note_user_line(name: str, title: str) -> str:
    """One line when the discussion gained one note and its text could not be read."""
    who = " ".join(str(name or "").split()).strip() or "the account"
    heading = " ".join(str(title or "").split()).strip() or "the discussion"
    return (
        f'Added the note to {who} on "{heading}". '
        "I couldn't read its text to double-check."
    )


def _merge_discussion_note_destination(store: JobStore, job_id: str) -> None:
    """Put the filed note's keys on the action claim so readback can re-read it.

    Policy number still wins for a policy-level write. note_id stays off the
    destination so the discussion id remains the identity when there is no
    policy number.
    """
    note = store.get_checkpoint(job_id, "discussion_note") or {}
    discussion_id = str(note.get("discussion_id") or "").strip()
    note_text = str(note.get("note_text") or "").strip()
    if not discussion_id or not note_text:
        return
    action = store.get_checkpoint(job_id, "action")
    if not isinstance(action, dict):
        return
    destination = dict(action.get("destination") or {})
    destination["discussion_id"] = discussion_id
    destination["note_text"] = note_text
    applicant = str(note.get("applicant_id") or "").strip()
    if applicant and not str(destination.get("applicant_id") or "").strip():
        destination["applicant_id"] = applicant
    title = str(note.get("discussion_title") or "").strip()
    if title and not str(destination.get("discussion_title") or "").strip():
        destination["discussion_title"] = title
    destination["write_kind"] = "discussion_note"
    updated = dict(action)
    updated["destination"] = destination
    store.checkpoint(job_id, "action", updated)


def _unproved_field_user_reply(store: JobStore, job: dict[str, Any]) -> str:
    """The user-facing line when a note was filed and the field was not changed."""
    note = store.get_checkpoint(job["id"], "discussion_note") or {}
    status = str(note.get("status") or "")
    if (
        status not in {"filed", "posted, verifying", "sent"}
        and not note.get("read_back")
    ):
        return ""
    title = str(note.get("discussion_title") or "").strip()
    if not title:
        return ""
    from .answer_only import (
        address_readback_proved,
        field_change_user_reply,
        holder_readback_proved,
    )

    payload = dict(job.get("payload") or {})
    text = str(payload.get("text") or "")
    folded = text.casefold()
    action = str(job.get("action_type") or "")
    proof_args = {"job_id": job["id"], "db_path": str(getattr(store, "path", "") or "")}
    address = action == "ezlynx.policy_change" or "mailing address" in folded
    holder = action == "ezlynx.certificate" or (
        "certificate" in folded or "certificate holder" in folded
    )
    if address and not holder:
        if address_readback_proved(proof_args):
            return ""
        return field_change_user_reply(title, kind="address")
    if holder and not address:
        if holder_readback_proved(proof_args):
            return ""
        return field_change_user_reply(title, kind="holder")
    if address and holder:
        if "mailing address" in folded or action == "ezlynx.policy_change":
            if address_readback_proved(proof_args):
                return ""
            return field_change_user_reply(title, kind="address")
        if holder_readback_proved(proof_args):
            return ""
        return field_change_user_reply(title, kind="holder")
    return ""


_RECORDING_KEEP_OPEN = {
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.VERIFYING.value,
}


def _release_chat_recording(
    db_path: str,
    job_id: str | None,
    recordings: RecordingManager | None,
) -> None:
    """Stop the screen recording unless the job is still in progress.

    The verifier can set UNVERIFIED after this function has already read
    the job. A later check of that first copy skips safe_stop, and the
    webm keeps growing. This reads the job again.
    """
    if not job_id or not db_path:
        return
    status = JobStatus.UNVERIFIED.value
    try:
        status = str(JobStore(db_path).get_job(job_id).get("status") or status)
    except Exception:
        status = JobStatus.UNVERIFIED.value
    if status in _RECORDING_KEEP_OPEN:
        return
    manager = recordings or RecordingManager(db_path)
    manager.safe_stop(job_id, status)


_VERIFIED_CLAIM = re.compile(
    r"posted\s*&\s*verified|posted and verified|(?<![\w])verified\b|(?<![\w])done\b",
    re.IGNORECASE,
)
_EXECUTION_SUMMARY = re.compile(r"execution summary", re.IGNORECASE)


def agent_reply_is_replaced(store: Any, job_id: str | None) -> bool:
    """True when the model's own final text must not be posted.

    A ledger park and a confirmed note each have one built line. A second
    send of the model's prose is not another message.
    """
    if not job_id or store is None:
        return False
    try:
        note = store.get_checkpoint(job_id, "discussion_note") or {}
    except Exception:
        return False
    status = str(note.get("status") or "")
    if status == "already_posted" and str(note.get("reason") or "").strip():
        return True
    if status == "sent":
        return True
    if status == "held" and str(note.get("reason") or "").strip():
        return True
    if status != "filed":
        return False
    from .post_job_audit import api_readback_confirms_write

    try:
        return bool(api_readback_confirms_write(store, job_id))
    except Exception:
        return False


def _stop_job_recording(store: JobStore, job_id: str, status: str) -> None:
    """Stop the screen recording when the job is no longer working."""
    path = str(getattr(store, "path", "") or "")
    if not path or not job_id:
        return
    try:
        from .chat_job_controls import stop_recordings_for_jobs

        stop_recordings_for_jobs(path, [job_id], status)
    except Exception:
        logger.exception("could not stop recording job=%s", job_id)


def _leave_partial_note_unverified(store: JobStore, job_id: str) -> None:
    """A filed request note is not the whole job. Never mark that COMPLETE."""
    job = store.get_job(job_id)
    status = JobStatus(job["status"])
    if status == JobStatus.PENDING:
        store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
        status = JobStatus.RUNNING
    if status in {JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.COMPLETE}:
        store.transition(
            job_id,
            JobStatus.UNVERIFIED,
            expected={status},
            error="only part of the request was done",
            release_lease=True,
        )
    _stop_job_recording(store, job_id, JobStatus.UNVERIFIED.value)


def close_confirmed_note_job(store: JobStore, job_id: str) -> bool:
    """A note whose readback matched is COMPLETE. That is the verified close.

    The live send used to leave these RUNNING and then settle them to
    UNVERIFIED. COMPLETE is only allowed from VERIFYING, with the note id
    on authoritative evidence.
    """
    if not job_id:
        return False
    note = store.get_checkpoint(job_id, "discussion_note") or {}
    if str(note.get("status") or "") != "filed":
        return False
    from .post_job_audit import api_readback_confirms_write

    if not api_readback_confirms_write(store, job_id):
        return False
    note_id = str(note.get("note_id") or "").strip()
    discussion_id = str(note.get("discussion_id") or "").strip()
    note_text = str(note.get("note_text") or "").strip()
    applicant_id = str(note.get("applicant_id") or "").strip()
    if not note_id or not discussion_id or not note_text:
        return False
    job = store.get_job(job_id)
    if _unproved_field_user_reply(store, job):
        # The note landed. The address or holder did not. That is not COMPLETE.
        _leave_partial_note_unverified(store, job_id)
        return False
    action = dict(store.get_checkpoint(job_id, "action") or {})
    destination = dict(action.get("destination") or {})
    destination.update(
        {
            "applicant_id": applicant_id or destination.get("applicant_id") or "",
            "discussion_id": discussion_id,
            "note_text": note_text,
            "note_id": note_id,
            "write_kind": "discussion_note",
        }
    )
    action["destination"] = destination
    store.checkpoint(job_id, "action", action)
    expected = {
        "applicant_id": str(destination.get("applicant_id") or ""),
        "discussion_id": discussion_id,
        "note_text": note_text,
        "note_id": note_id,
    }
    observed = dict(expected)
    observed["note_text_matched"] = True
    from .models import VERIFIER_AUTHORITY, VerificationEvidence
    from .store import utc_now

    already = any(
        item.get("verified") and item.get("authoritative") and item.get("locator") == note_id
        for item in store.list_evidence(job_id)
    )
    if not already:
        store.add_evidence(
            job_id,
            True,
            VerificationEvidence(
                method="DiscussionApi",
                source="ezlynx-discussionapi",
                expected=expected,
                observed=observed,
                authoritative=True,
                captured_at=utc_now(),
                locator=note_id,
            ),
        )
    job = store.get_job(job_id)
    status = JobStatus(job["status"])
    if status == JobStatus.COMPLETE:
        _stop_job_recording(store, job_id, JobStatus.COMPLETE.value)
        return True
    if status not in {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
        JobStatus.UNVERIFIED,
    }:
        return False
    if status != JobStatus.VERIFYING:
        store.transition(
            job_id,
            JobStatus.VERIFYING,
            expected={status},
            release_lease=True,
        )
    store.transition(
        job_id,
        JobStatus.COMPLETE,
        expected={JobStatus.VERIFYING},
        authority=VERIFIER_AUTHORITY,
        release_lease=True,
    )
    _stop_job_recording(store, job_id, JobStatus.COMPLETE.value)
    return True


def _leave_spoken_note_unverified(store: JobStore, job_id: str, error: str) -> None:
    """The user has the one line. The note is not called done."""
    job = store.get_job(job_id)
    status = JobStatus(job["status"])
    if status == JobStatus.PENDING:
        store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
        status = JobStatus.RUNNING
    if status in {JobStatus.RUNNING, JobStatus.VERIFYING}:
        store.transition(
            job_id,
            JobStatus.UNVERIFIED,
            expected={status},
            error=error,
            release_lease=True,
        )
    _stop_job_recording(store, job_id, JobStatus.UNVERIFIED.value)


def publish_discussion_note_outcome(
    db_path: str,
    job_id: str,
    poster: Callable[..., Any] | None = None,
) -> str | None:
    """Post the ledger question or the readback line on the stored thread.

    The model does not send this line. A later model send has nothing to add.
    """
    if not db_path or not job_id:
        return None
    store = JobStore(db_path)
    try:
        note = store.get_checkpoint(job_id, "discussion_note") or {}
    except Exception:
        return None
    status = str(note.get("status") or "")
    line = ""
    if status == "already_posted":
        line = " ".join(str(note.get("reason") or "").split())
        if not line:
            return None
        from .chat_job_controls import mark_job_waiting_for_user

        mark_job_waiting_for_user(store, job_id, line, from_unverified=True)
    elif status == "filed":
        from .post_job_audit import api_readback_confirms_write

        if str(note.get("verified_by") or "") == "count":
            job = store.get_job(job_id)
            unproved = _unproved_field_user_reply(store, job)
            if unproved:
                _leave_partial_note_unverified(store, job_id)
                line = unproved
            else:
                line = _count_note_user_line(
                    _account_display_name(job),
                    str(note.get("discussion_title") or ""),
                )
                _leave_spoken_note_unverified(
                    store, job_id, "the note was added, but its text could not be read"
                )
        elif not api_readback_confirms_write(store, job_id):
            return None
        else:
            job = store.get_job(job_id)
            unproved = _unproved_field_user_reply(store, job)
            if unproved:
                _leave_partial_note_unverified(store, job_id)
                line = unproved
            else:
                if not close_confirmed_note_job(store, job_id):
                    return None
                line = _discussion_note_user_reply(store, store.get_job(job_id))
    elif status in {"sent", "held"}:
        job = store.get_job(job_id)
        unproved = _unproved_field_user_reply(store, job) if status == "sent" else ""
        if unproved:
            _leave_partial_note_unverified(store, job_id)
            line = unproved
        elif status == "sent" or str(note.get("verified_by") or "") == "count":
            line = _count_note_user_line(
                _account_display_name(job),
                str(note.get("discussion_title") or ""),
            )
            _leave_spoken_note_unverified(
                store, job_id, "the note was added, but its text could not be read"
            )
        else:
            line = " ".join(str(note.get("reason") or "").split())
            if not line:
                return None
            _leave_spoken_note_unverified(
                store, job_id, "the note could not be confirmed"
            )
    else:
        return None
    line = " ".join(str(line or "").split()).strip()
    if not line:
        return None
    from .user_reply import format_user_reply

    line = format_user_reply(line)
    prior = store.get_checkpoint(job_id, "chat_outcome_sent") or {}
    if " ".join(str(prior.get("text") or "").split()) == line:
        from .chat_turn_control import request_agent_stop

        request_agent_stop(job_id)
        return line
    from .chat_thread import read_job_chat_thread

    thread = read_job_chat_thread(store, job_id)
    job = store.get_job(job_id)
    space = str((job.get("payload") or {}).get("conversation_id") or "").strip()
    send = poster if poster is not None else _OUTCOME_POSTER
    if send is not None and space:
        send(space, line, thread, job_id)
    store.checkpoint(
        job_id,
        "chat_outcome_sent",
        {"text": line, "thread": thread or "", "space": space},
    )
    from .chat_turn_control import request_agent_stop

    request_agent_stop(job_id)
    return line


def enforce_note_reply_wording(db_path: str, job_id: str | None, text: str) -> str:
    """Done or verified only when the deterministic read-back passed.

    The model's words are not proof. A multi-line Execution Summary is
    replaced with the one-line result.
    """
    if not job_id or not db_path:
        return text
    try:
        store = JobStore(db_path)
        job = store.get_job(job_id)
    except Exception:
        return text
    note = store.get_checkpoint(job_id, "discussion_note") or {}
    if str(note.get("status") or "") == "already_posted":
        question = " ".join(str(note.get("reason") or "").split())
        if question:
            return question if question.endswith("\n") else question + "\n"
    from .post_job_audit import api_readback_confirms_write

    confirmed = api_readback_confirms_write(store, job_id)
    line = _discussion_note_user_reply(store, job)
    if line:
        return line if line.endswith("\n") else line + "\n"
    body = str(text or "")
    if _EXECUTION_SUMMARY.search(body):
        if line:
            return line if line.endswith("\n") else line + "\n"
        from . import status_format

        sentence = _EXECUTION_SUMMARY.split(body, maxsplit=1)[0]
        sentence = " ".join(sentence.split()).strip() or "Couldn't finish."
        ref = status_format.short_job_ref(job_id)
        collapsed = sentence if not ref else f"{sentence}\n{ref}"
        return collapsed if collapsed.endswith("\n") else collapsed + "\n"
    if note and _VERIFIED_CLAIM.search(body) and not confirmed:
        if line:
            return line if line.endswith("\n") else line + "\n"
    return text


def guard_chat_response(
    db_path: str,
    job_id: str | None,
    content: str,
    *,
    verifiers: dict[str, Any] | None = None,
    recordings: RecordingManager | None = None,
) -> str:
    """User-facing Chat reply. The recording stops on the way out."""
    from .answer_only import scrub_user_reply

    try:
        body = _guard_chat_response_impl(
            db_path,
            job_id,
            content,
            verifiers=verifiers,
            recordings=recordings,
        )
        body = enforce_note_reply_wording(db_path, job_id, body)
        return scrub_user_reply(body)
    finally:
        _release_chat_recording(db_path, job_id, recordings)


def _guard_chat_response_impl(
    db_path: str,
    job_id: str | None,
    content: str,
    *,
    verifiers: dict[str, Any] | None = None,
    recordings: RecordingManager | None = None,
) -> str:
    from .answer_only import strip_blank_saved_span

    content = strip_blank_saved_span(redact_text(content))
    from .hitl import sanitize_hitl_chat_text

    content = sanitize_hitl_chat_text(content, job_id=str(job_id or ""))
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
    forced = _unproved_field_user_reply(store, job)
    if forced:
        _leave_partial_note_unverified(store, job_id)
        recordings = recordings or RecordingManager(db_path)
        recordings.safe_stop(job_id, JobStatus.UNVERIFIED.value)
        return forced if forced.endswith("\n") else forced + "\n"
    recordings = recordings or RecordingManager(db_path)
    repeat = store.get_checkpoint(job_id, "discussion_note") or {}
    if str(repeat.get("status") or "") == "already_posted":
        question = " ".join(str(repeat.get("reason") or "").split())
        if question:
            from .chat_job_controls import mark_job_waiting_for_user

            mark_job_waiting_for_user(
                store, job_id, question, from_unverified=True
            )
            recordings.safe_stop(job_id, JobStatus.NEEDS_CLARIFICATION.value)
            return question if question.endswith("\n") else question + "\n"
    if close_confirmed_note_job(store, job_id):
        line = _discussion_note_user_reply(store, store.get_job(job_id))
        recordings.safe_stop(job_id, JobStatus.COMPLETE.value)
        if line:
            return line if line.endswith("\n") else line + "\n"
    from .policy_setup_dispatch import is_policy_setup_honest_hitl

    if is_policy_setup_honest_hitl(content) and JobStatus(job["status"]) in {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
    }:
        return park_policy_setup_fail_closed(
            db_path, job_id, content, recordings=recordings, store=store
        )
    blocker = structured_blocker_reason(content)
    if blocker and JobStatus(job["status"]) in {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
    }:
        from .hitl_ladder import stamp_hitl_posted_at

        payload = stamp_hitl_posted_at(dict(job.get("payload") or {}))
        store.update_payload(job_id, payload)
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
    if JobStatus(job["status"]) == JobStatus.NEEDS_CLARIFICATION:
        error = str(job.get("last_error") or "")
        note = store.get_checkpoint(job_id, "clarification") or {}
        question = " ".join(str(note.get("question") or "").split())
        if "destination locator" in error.casefold() and question:
            recordings.safe_stop(job_id, JobStatus.NEEDS_CLARIFICATION.value)
            return question if question.endswith("\n") else question + "\n"
    if JobStatus(job["status"]) in WAITING_STATUSES:
        status = JobStatus(job["status"]).value
        recordings.safe_stop(job_id, status)
        return _render_chat_terminal(store, job, content, recordings)

    # The Computer Worker response is diagnostic only. A separate structured
    # action checkpoint and registered destination verifier are required.
    # Success-shaped fill/save prose without that checkpoint is rewritten so
    # the ledger cannot look like progress (job 468d1575).
    from .worker_contract import sanitize_worker_response

    from .answer_only import is_answer_only_job
    from .email_guard import _strip_internal_reasoning

    if is_answer_only_job(job):
        from .chat_turn_control import is_gateway_status_notice

        if is_gateway_status_notice(content):
            # The gateway's interrupt line is not the answer. Leave the job open.
            return ""
        content = _strip_internal_reasoning(content) or content
        store.checkpoint(
            job_id, "worker_response", sanitize_worker_response(store, job_id, content)
        )
        store.checkpoint(
            job_id,
            "answer_only_close",
            {"reason": "answered", "wrote": False},
        )
        current = store.get_job(job_id)
        if JobStatus(current["status"]) in {JobStatus.RUNNING, JobStatus.PENDING, JobStatus.VERIFYING}:
            from .answer_only import mark_answered_question

            mark_answered_question(store, job_id, content)
        recordings.safe_stop(job_id, JobStatus.COMPLETE.value)
        final = store.get_job(job_id)
        if JobStatus(final["status"]) in TERMINAL_STATUSES:
            maybe_snapshot_and_bind(db_path, job_id, phase="end")
            final = _publish_terminal_job(db_path, store, final)
        return _render_chat_terminal(store, final, content, recordings)
    store.checkpoint(job_id, "worker_response", sanitize_worker_response(store, job_id, content))
    if store.get_checkpoint(job_id, "action") is None:
        from .chat_destination_binding import bind_destination_for_job, claimed_from_job

        bind_destination_for_job(store, job, claimed=claimed_from_job(job, content))
    _merge_discussion_note_destination(store, job_id)
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
        decision = classify_chat_close_without_checkpoint(
            content,
            action=action,
            last_error=current.get("last_error"),
            verifier_missing=bool(action) and verifier is None,
            action_type=job["action_type"],
        )
        if decision.status == JobStatus.AWAITING_HUMAN_INPUT.value:
            from .hitl_ladder import stamp_hitl_posted_at

            payload = stamp_hitl_posted_at(dict(current.get("payload") or {}))
            store.update_payload(job_id, payload)
            interaction = interaction_for_blocker(
                decision.error,
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
                    expected={JobStatus.VERIFYING, JobStatus.RUNNING},
                    error=decision.error,
                    resume_status=JobStatus.RUNNING,
                    release_lease=True,
                )
            else:
                DurableChatEventQueue(db_path).park_direct_human_input(
                    conversation_id=conversation_id,
                    job_id=job_id,
                    interaction_state=interaction,
                    error=decision.error,
                )
            recordings.safe_stop(job_id, JobStatus.AWAITING_HUMAN_INPUT.value)
            return interaction["prompt"]
        if decision.reason == "answered question":
            from .answer_only import mark_answered_question

            mark_answered_question(store, job_id, content)
            recordings.safe_stop(job_id, JobStatus.COMPLETE.value)
        elif decision.status == JobStatus.FAILED.value:
            store.transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.VERIFYING, JobStatus.RUNNING},
                error=decision.error,
                release_lease=True,
            )
            recordings.safe_stop(job_id, JobStatus.FAILED.value)
        else:
            closed = fail_closed_zero_playwright_rows(
                store,
                current,
                expected={JobStatus.VERIFYING, JobStatus.RUNNING, JobStatus.UNVERIFIED},
            )
            if JobStatus(closed["status"]) == JobStatus.FAILED:
                recordings.safe_stop(job_id, JobStatus.FAILED.value)
            elif JobStatus(current["status"]) == JobStatus.VERIFYING:
                store.transition(
                    job_id,
                    JobStatus.UNVERIFIED,
                    expected={JobStatus.VERIFYING},
                    error=decision.error,
                    release_lease=True,
                )
                recordings.safe_stop(job_id, JobStatus.UNVERIFIED.value)
    final = store.get_job(job_id)
    if JobStatus(final["status"]) == JobStatus.UNVERIFIED:
        final = fail_closed_zero_playwright_rows(
            store,
            final,
            expected={JobStatus.UNVERIFIED},
        )
    if JobStatus(final["status"]) in TERMINAL_STATUSES:
        maybe_snapshot_and_bind(db_path, job_id, phase="end")
        final = _publish_terminal_job(db_path, store, final)
    return _render_chat_terminal(store, final, content, recordings)
