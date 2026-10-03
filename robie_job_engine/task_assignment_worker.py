#!/usr/bin/env python3
"""Task assignment worker — processes EZLynx tasks assigned to Roby.

For each task assigned to "Robie AI":

1. The worker reads the task note and categorizes it.
2. It attempts the work. (v1: no automated business-task handlers exist
   yet, so every non-test task is handed back.)
3. When Roby cannot complete a task, it reassigns the task in EZLynx to
   the first populated routing field: Task Created By -> Assigned
   Producer -> CSR, and posts one honest note explaining what was
   attempted and where the task went.

Job Engine lifecycle (driven by the intake, verified independently):

- PENDING -> RUNNING: the worker claims the job and does the work.
- RUNNING -> VERIFYING: work attempted; an action checkpoint records the
  note_id and any reassignment.
- VERIFYING -> COMPLETE: only the engine's independent verifier, after
  a fresh EZLynx read-back proves the note (and reassignment) landed.
  COMPLETE without that evidence is refused by the store.
- RUNNING -> AWAITING_HUMAN_INPUT: the worker needs an answer (no valid
  reassignment target, or the reassignment gate is off). The SAME job
  resumes via JobStore.resume — never a new job.
- VERIFYING -> UNVERIFIED: the read-back did not confirm the write.
  The note is never auto-reposted.

Safety invariants (repo contract):

- EZLynx notes are API-only (DiscussionApi). The browser/CDP path is
  used solely for the task assignee form edit — never for notes.
- Never deletes tasks or applicants. Never emails clients.
- Notes are concise, plain English, non-technical (agency standard),
  and never claim work that was not done.
- Fail-closed: unclear tasks and failed writes surface as
  AWAITING_HUMAN_INPUT / FAILED / UNVERIFIED — never silently dropped.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .ezlynx_task_report import AssignedTask
from .models import JobStatus, VerificationEvidence, VerificationResult

logger = logging.getLogger(__name__)

# Blast-radius limit: stop the run rather than hammering EZLynx when an
# unexpectedly large batch of Robie tasks arrives.
MAX_TASKS_PER_RUN = 25

NOTE_POST_ATTEMPTS = 3
NOTE_RETRY_DELAYS = (2.0, 4.0)

# (routing field, human label) in precedence order.
REASSIGN_PRECEDENCE = (
    ("task_created_by", "who created the task"),
    ("assigned_producer", "the assigned producer"),
    ("csr", "the CSR"),
)


class NeedsHuman(Exception):
    """The worker needs a human answer before this job can proceed."""


class UnverifiedNoteError(Exception):
    """A note post could not be confirmed via read-back."""


class TaskReassigner(Protocol):
    """Changes a task's assignee in EZLynx (browser/CDP form edit)."""

    def reassign(self, task_id: str, applicant_id: str, new_assignee: str) -> str:
        """Set the assignee; return the re-read verified assignee name."""
        ...

    def read_assignee(self, task_id: str, applicant_id: str) -> str:
        """Read-only: the task's current assignee name."""
        ...


class DiscussionClient(Protocol):
    """Subset of DiscussionApiClient used by the worker."""

    def append_note(self, discussion_id: str, body: str) -> dict[str, Any]: ...
    def get_discussion(self, discussion_id: str) -> dict[str, Any]: ...


@dataclass
class TaskResult:
    """Result of processing a single assigned task."""
    task_id: str
    applicant_id: str
    # "completed", "awaiting_human", "failed", "unverified", "skipped_seen",
    # "skipped_not_in_report", "over_batch_cap"
    action: str
    detail: str
    timestamp: str = ""


def reassign_target(task: AssignedTask) -> tuple[str | None, str | None]:
    """Who a task goes back to when Roby cannot complete it.

    Precedence: Task Created By -> Assigned Producer -> CSR. Returns
    (name, routing_field) or (None, None) when all three are blank —
    the worker never guesses a person.
    """
    values = {
        "task_created_by": task.created_by,
        "assigned_producer": task.assigned_producer,
        "csr": task.csr,
    }
    for field_name, _label in REASSIGN_PRECEDENCE:
        name = (values.get(field_name) or "").strip()
        if name:
            return name, field_name
    return None, None


def is_test_task(task: AssignedTask) -> bool:
    """True for obvious test/placeholder tasks (left alone, not reassigned)."""
    text = f"{task.title} {task.description}".lower()
    return "test" in text and "rob" in text


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskAssignmentWorker:
    """Processes tasks assigned to Roby, driving one Job Engine job each."""

    def __init__(
        self,
        *,
        discussion_client: DiscussionClient | None = None,
        task_reassigner: TaskReassigner | None = None,
        reassign_enabled: bool = False,
        max_tasks_per_run: int = MAX_TASKS_PER_RUN,
    ):
        self.client = discussion_client
        self.reassigner = task_reassigner
        self.reassign_enabled = reassign_enabled
        self.max_tasks_per_run = max_tasks_per_run
        self.results: list[TaskResult] = []

    # -- job lifecycle -------------------------------------------------

    def process_job(self, store: Any, job: dict[str, Any]) -> dict[str, Any]:
        """Claim one PENDING job and work it through to VERIFYING.

        Returns the job row after the terminal transition of this phase.
        COMPLETE is never set here — only the independent verifier may.
        """
        job_id = job["id"]
        job = store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
        try:
            action = self._do_work(job)
        except NeedsHuman as e:
            logger.warning(f"Job {job_id} needs a human: {e}")
            return store.transition(
                job_id,
                JobStatus.AWAITING_HUMAN_INPUT,
                expected={JobStatus.RUNNING},
                error=str(e)[:500],
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
        except Exception as e:  # noqa: BLE001 — fail-closed with the error on the job
            logger.error(f"Job {job_id} failed: {e}")
            return store.transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error=f"{type(e).__name__}: {e}"[:500],
                release_lease=True,
            )
        store.checkpoint(job_id, "action", action)
        return store.transition(
            job_id, JobStatus.VERIFYING, expected={JobStatus.RUNNING}, release_lease=True
        )

    # -- the work ------------------------------------------------------

    def _do_work(self, job: dict[str, Any]) -> dict[str, Any]:
        """Attempt the task. Returns the action checkpoint for the verifier."""
        payload = job.get("payload") or {}
        task = _task_from_payload(payload)
        timestamp = utcnow_iso()

        if is_test_task(task):
            note_id = self._post_note_verified(
                task.discussion_id,
                "Roby here — this looks like a test task, so I'm leaving it "
                "alone. No action taken.",
            )
            self._record(task, "completed", "Test task; note posted, no action taken.", timestamp)
            return {
                "test_task": True,
                "category": "test",
                "note": {
                    "discussion_id": task.discussion_id,
                    "note_id": note_id,
                    "text": "test-task acknowledgment",
                },
                "reassigned": None,
            }

        category = self._categorize_task(task)
        target, target_field = reassign_target(task)

        if target is None:
            # No valid person to send this back to — say so on the task,
            # then wait for a human on the SAME job.
            self._post_note_verified(
                task.discussion_id,
                "Roby here — I read this task but couldn't complete it, and I "
                "couldn't tell who to send it back to (no creator, producer, "
                "or CSR listed). Flagging for the team.",
            )
            self._record(task, "awaiting_human", "No reassignment target; flagged for team.", timestamp)
            raise NeedsHuman(
                f"Task {task.task_id}: no Task Created By / Assigned Producer / CSR — "
                "a human must pick who gets this task back."
            )

        target_label = dict(REASSIGN_PRECEDENCE)[target_field]

        reassigned: dict[str, Any] | None = None
        if self.reassign_enabled and self.reassigner is not None:
            verified_assignee = self.reassigner.reassign(
                task.task_id, task.applicant_id, target
            )
            reassigned = {"to": target, "verified_assignee": verified_assignee}
            note_body = (
                f"Roby here — I read this request ({category}) but I can't "
                f"complete it myself, so I've sent it back to {verified_assignee} "
                f"({target_label})."
            )
        else:
            note_body = (
                f"Roby here — I read this request ({category}) but I can't "
                f"complete it myself. It needs to go back to {target} "
                f"({target_label}); automatic reassignment is off, so please "
                "reassign it in EZLynx."
            )

        note_id = self._post_note_verified(task.discussion_id, note_body)

        if reassigned is None:
            self._record(task, "awaiting_human", f"Reassignment gate off; flagged for reassign to {target}.", timestamp)
            raise NeedsHuman(
                f"Task {task.task_id}: reassignment gate is off — a human must "
                f"reassign to {target} in EZLynx."
            )

        self._record(task, "completed", f"Reassigned to {reassigned['verified_assignee']}; note posted.", timestamp)
        return {
            "test_task": False,
            "category": category,
            "reassign_target": target,
            "reassign_field": target_field,
            "note": {
                "discussion_id": task.discussion_id,
                "note_id": note_id,
                "text": "handoff to " + reassigned["verified_assignee"],
            },
            "reassigned": reassigned,
        }

    def _categorize_task(self, task: AssignedTask) -> str:
        """Sort the request from its activity type and note text."""
        text = f"{task.title} {task.description}".lower()
        if any(kw in text for kw in ["call", "phone", "callback", "reach out"]):
            return "callback"
        if any(kw in text for kw in ["document", "upload", "attach", "pdf"]):
            return "document"
        if any(kw in text for kw in ["quote", "premium", "price"]):
            return "quote"
        if any(kw in text for kw in ["follow up", "follow-up", "check status"]):
            return "follow_up"
        if any(kw in text for kw in ["email", "send"]):
            return "email"
        return "general request"

    # -- verified note posting (API only) -------------------------------

    def _post_note_verified(self, discussion_id: str, body: str) -> str:
        """Post one note and prove it landed via read-back.

        Returns the confirmed note_id. Raises UnverifiedNoteError when the
        read-back does not confirm the write — the note is NEVER reposted
        automatically (a second post would duplicate it).
        """
        if not discussion_id:
            raise ValueError("No discussion ID — cannot post note")
        if self.client is None:
            raise ValueError("No discussion client — refusing to claim a note was posted")

        from .ezlynx_discussions import discussion_note_snapshot

        before = discussion_note_snapshot(self.client.get_discussion(discussion_id))

        last_error: Exception | None = None
        response: dict[str, Any] | None = None
        for attempt in range(NOTE_POST_ATTEMPTS):
            try:
                response = self.client.append_note(discussion_id, body)
                last_error = None
                break
            except Exception as e:  # noqa: BLE001 — bounded retry, then fail closed
                last_error = e
                logger.warning(
                    f"Note post attempt {attempt + 1}/{NOTE_POST_ATTEMPTS} failed: {e}"
                )
                if attempt < NOTE_POST_ATTEMPTS - 1:
                    time.sleep(NOTE_RETRY_DELAYS[attempt])
        if last_error is not None or response is None:
            raise UnverifiedNoteError(
                f"Note post failed after {NOTE_POST_ATTEMPTS} attempts: {last_error}"
            )

        api_note_id = _extract_note_id(response)
        after = discussion_note_snapshot(self.client.get_discussion(discussion_id))
        latest = after.get("most_recent_note_id") or ""

        from .ezlynx_discussions import _metadata_note_confirmation

        if api_note_id and latest == api_note_id:
            logger.info(f"Note {api_note_id} confirmed on discussion {discussion_id}")
            return api_note_id
        # Reviewed metadata confirmation: the discussion must have gained
        # exactly one note, the latest id must have changed, and the title
        # must be unchanged. A new latest id alone is not a receipt —
        # another writer can produce the same metadata.
        confirmed, reason = _metadata_note_confirmation(before, after)
        if confirmed:
            logger.info(
                f"Note confirmed on discussion {discussion_id} via read-back "
                f"(latest id {latest})"
            )
            return latest
        raise UnverifiedNoteError(
            f"Note post to discussion {discussion_id} could not be confirmed: "
            f"{reason} Not reposting."
        )

    def _record(self, task: AssignedTask, action: str, detail: str, timestamp: str) -> None:
        self.results.append(TaskResult(
            task_id=task.task_id,
            applicant_id=task.applicant_id,
            action=action,
            detail=detail,
            timestamp=timestamp,
        ))


def _extract_note_id(response: dict[str, Any]) -> str:
    for key in ("note_id", "noteId", "ezlynx_note_id", "id", "Id"):
        value = response.get(key)
        if value:
            return str(value).strip()
    # Some responses nest the created note.
    for key in ("note", "Note", "result"):
        nested = response.get(key)
        if isinstance(nested, dict):
            found = _extract_note_id(nested)
            if found:
                return found
    return ""


def _task_from_payload(payload: dict[str, Any]) -> AssignedTask:
    """Rebuild the task view from the job payload (IDs travel with the job)."""
    return AssignedTask(
        task_id=str(payload.get("task_id") or ""),
        title=str(payload.get("title") or ""),
        description=str(payload.get("description") or ""),
        applicant_id=str(payload.get("applicant_id") or ""),
        applicant_name=str(payload.get("account_name") or ""),
        assigned_to=str(payload.get("assigned_to") or "Robie AI"),
        due_date=str(payload.get("due_date") or ""),
        priority=str(payload.get("priority") or ""),
        created_date=str(payload.get("created_date") or ""),
        status=str(payload.get("task_status") or ""),
        discussion_id=str(payload.get("discussion_id") or ""),
        last_modified=str(payload.get("last_modified") or ""),
        created_by=str(payload.get("task_created_by") or ""),
        assigned_producer=str(payload.get("assigned_producer") or ""),
        csr=str(payload.get("csr") or ""),
    )


class TaskIntakeVerifier:
    """Independent verifier for ezlynx.task_intake jobs.

    Re-checks EZLynx from the source of truth — never trusts the worker's
    claim. Returns a VerificationResult the Job Engine turns into
    COMPLETE (all evidence checks pass) or UNVERIFIED / retry.
    """

    def __init__(
        self,
        *,
        discussion_client: DiscussionClient | None = None,
        task_reassigner: TaskReassigner | None = None,
    ):
        self.client = discussion_client
        self.reassigner = task_reassigner

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        """Independently verify the note (and reassignment) landed in EZLynx."""
        payload = job.get("payload") or {}
        task_id = str(payload.get("task_id") or "")
        discussion_id = str((action.get("note") or {}).get("discussion_id") or "")
        note_id = str((action.get("note") or {}).get("note_id") or "")
        captured = utcnow_iso()

        if not discussion_id or not note_id:
            return _unverified(
                job, captured, discussion_id,
                {"note_id": "", "discussion_id": discussion_id},
                {"error": "action checkpoint has no confirmed note_id"},
                error="no confirmed note in action checkpoint",
            )

        if self.client is None:
            return _unverified(
                job, captured, discussion_id,
                {"note_id": note_id, "discussion_id": discussion_id},
                {"note_id": note_id, "discussion_id": discussion_id,
                 "error": "no discussion client for read-back"},
                error="verifier has no discussion client",
                retryable=True,
            )

        from .ezlynx_discussions import discussion_note_snapshot

        try:
            snapshot = discussion_note_snapshot(self.client.get_discussion(discussion_id))
        except Exception as e:  # noqa: BLE001 — transient read failure retries
            return _unverified(
                job, captured, discussion_id,
                {"note_id": note_id, "discussion_id": discussion_id},
                {"note_id": note_id, "discussion_id": discussion_id,
                 "error": f"read-back failed: {e}"},
                error=f"discussion read-back failed: {e}",
                retryable=True,
            )

        latest = str(snapshot.get("most_recent_note_id") or "")
        expected: dict[str, Any] = {"note_id": note_id, "discussion_id": discussion_id}
        observed: dict[str, Any] = {
            "note_id": latest,
            "discussion_id": discussion_id,
        }

        reassigned = action.get("reassigned") or {}
        if reassigned:
            want = str(reassigned.get("verified_assignee") or "")
            expected["assigned_to"] = want
            if self.reassigner is None:
                return _unverified(
                    job, captured, discussion_id, expected, observed,
                    error="reassignment claimed but verifier cannot re-read assignee",
                    retryable=True,
                )
            try:
                current = self.reassigner.read_assignee(task_id, str(payload.get("applicant_id") or ""))
            except Exception as e:  # noqa: BLE001
                return _unverified(
                    job, captured, discussion_id, expected,
                    {**observed, "assigned_to": f"read failed: {e}"},
                    error=f"assignee re-read failed: {e}",
                    retryable=True,
                )
            observed["assigned_to"] = current
            if current.strip().lower() != want.strip().lower():
                return _unverified(
                    job, captured, discussion_id, expected, observed,
                    error=f"assignee is {current!r}, expected {want!r}",
                    retryable=True,
                )

        if latest != note_id:
            return _unverified(
                job, captured, discussion_id, expected, observed,
                error=f"latest note is {latest!r}, expected {note_id!r}",
                retryable=True,
            )

        evidence = VerificationEvidence(
            method="ezlynx-discussion-readback",
            source="DiscussionApi",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=captured,
            locator=f"ezlynx-discussion:{discussion_id}",
        )
        return VerificationResult(verified=True, evidence=evidence)


def _unverified(
    job: dict[str, Any],
    captured: str,
    discussion_id: str,
    expected: dict[str, Any],
    observed: dict[str, Any],
    *,
    error: str,
    retryable: bool = False,
) -> VerificationResult:
    evidence = VerificationEvidence(
        method="ezlynx-discussion-readback",
        source="DiscussionApi",
        expected=expected,
        observed=observed,
        authoritative=True,
        captured_at=captured,
        locator=f"ezlynx-discussion:{discussion_id}" if discussion_id else None,
    )
    return VerificationResult(
        verified=False, evidence=evidence, retryable=retryable, error=error
    )
