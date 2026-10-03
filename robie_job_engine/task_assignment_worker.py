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
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .ezlynx_task_report import AssignedTask
from .models import JobStatus, VerificationEvidence, VerificationResult

logger = logging.getLogger(__name__)

# Blast-radius limit: stop the run rather than hammering EZLynx when an
# unexpectedly large batch of Robie tasks arrives.
MAX_TASKS_PER_RUN = 25


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

    def reassign(
        self, task_id: str, applicant_id: str, new_assignee: str,
        description: str = "", expected_assignee: str = "Robie AI",
    ) -> str:
        """Set the assignee; return the re-read verified assignee name."""
        ...

    def read_assignee(
        self, task_id: str, applicant_id: str, description: str = ""
    ) -> str:
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


class _WorkerReassignPortAdapter:
    """Bridge the worker's TaskReassigner to the call handler's port.

    Worker protocol:  reassign(task_id, applicant_id, new_assignee,
                               description="") -> verified assignee name (str)
                      read_assignee(task_id, applicant_id, description="") -> str
    Handler protocol: reassign_task(task_id, new_assignee, note) -> {"ok": ...}
                      read_task_assignee(task_id) -> str | None (optional)
    """

    def __init__(self, reassigner: TaskReassigner, task: AssignedTask):
        self._reassigner = reassigner
        self._task = task

    def reassign_task(
        self, task_id: str, new_assignee: str, note: str = ""
    ) -> dict[str, Any]:
        try:
            from .ezlynx_task_cdp import validate_identity
            validate_identity(task_id, self._task.applicant_id)
            if task_id != self._task.task_id:
                raise ValueError("Foreign task identity; reassignment not sent")
            verified = self._reassigner.reassign(
                task_id, self._task.applicant_id, new_assignee,
                description=self._task.description,
                expected_assignee=self._task.assigned_to)
            current = self.read_task_assignee(task_id)
            if not isinstance(verified, str) or not current or current.casefold() != new_assignee.casefold():
                raise ValueError("No destination proof for reassignment")
            return {"ok": True, "sent": True, "verified_assignee": current}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "sent": False, "error": str(exc)[:300]}

    def read_task_assignee(self, task_id: str) -> str | None:
        try:
            from .ezlynx_task_cdp import validate_identity
            validate_identity(task_id, self._task.applicant_id)
            if task_id != self._task.task_id:
                return None
            return self._reassigner.read_assignee(
                task_id, self._task.applicant_id, description=self._task.description)
        except Exception:  # noqa: BLE001
            return None


class TaskAssignmentWorker:
    """Processes tasks assigned to Roby, driving one Job Engine job each."""

    def __init__(
        self,
        *,
        discussion_client: DiscussionClient | None = None,
        task_reassigner: TaskReassigner | None = None,
        reassign_enabled: bool = False,
        max_tasks_per_run: int = MAX_TASKS_PER_RUN,
        phone_lookup: Any | None = None,
        bland_client: Any | None = None,
        call_dry_run: bool = True,
    ):
        self.client = discussion_client
        self.reassigner = task_reassigner
        self.reassign_enabled = reassign_enabled
        self.max_tasks_per_run = max_tasks_per_run
        self.results: list[TaskResult] = []
        # Robie Call handler ports (PR #746). When phone_lookup and
        # bland_client are both wired, "callback" tasks route to the real
        # call handler; otherwise they take the generic handoff path.
        self.phone_lookup = phone_lookup
        self.bland_client = bland_client
        self.call_dry_run = call_dry_run

    # -- job lifecycle -------------------------------------------------

    def process_job(self, store: Any, job: dict[str, Any]) -> dict[str, Any]:
        """Claim one PENDING job and work it through to VERIFYING.

        Returns the job row after the terminal transition of this phase.
        COMPLETE is never set here — only the independent verifier may.
        """
        job_id = job["id"]
        job = store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
        try:
            action = self._do_work(store, job)
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

    def _do_work(self, store: Any, job: dict[str, Any]) -> dict[str, Any]:
        """Attempt the task. Returns the action checkpoint for the verifier."""
        payload = job.get("payload") or {}
        task = _task_from_payload(payload)
        from .ezlynx_task_cdp import validate_identity
        validate_identity(task.task_id, task.applicant_id)
        timestamp = utcnow_iso()

        if is_test_task(task):
            note_id = self._post_note_verified(
                store, job, task.discussion_id,
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

        # Route callback tasks to the Robie Call handler (PR #746) when its
        # ports are wired. The handler runs inside this same durable job —
        # one job per EZLynx task — checkpointing under "robie-call:<id>".
        if category == "callback" and self._call_handler_available():
            return self._do_call_task(store, job, task, timestamp)

        if target is None:
            # No valid person to send this back to — say so on the task,
            # then wait for a human on the SAME job.
            self._post_note_verified(
                store, job, task.discussion_id,
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
                task.task_id, task.applicant_id, target,
                description=task.description, expected_assignee=task.assigned_to,
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

        note_id = self._post_note_verified(store, job, task.discussion_id, note_body)

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

    def _call_handler_available(self) -> bool:
        """True when the Robie Call handler and its required ports are wired."""
        if self.phone_lookup is None or self.bland_client is None:
            return False
        try:
            from . import robie_call_handler  # noqa: F401
            return True
        except ImportError:
            logger.warning("robie_call_handler not importable; "
                           "callback tasks take the generic handoff path")
            return False

    def _do_call_task(
        self, store: Any, job: dict[str, Any], task: AssignedTask, timestamp: str
    ) -> dict[str, Any]:
        """Route a callback task through the Robie Call handler.

        Runs inside the same durable job (idempotency key
        "ezlynx-task:<task_id>"); the handler checkpoints under
        "robie-call:<task_id>" via the real Job Engine adapters, so a
        restart reconciles instead of redialing. Returns the action
        checkpoint for the independent verifier.
        """
        from . import robie_call_handler as rch
        from .robie_call_job_engine_adapters import build_robie_call_ports

        payload = job.get("payload") or {}
        task_dict: dict[str, Any] = {
            "task_id": task.task_id,
            "Task Subject": task.title,
            "Task Description": task.description,
            "Applicant ID": task.applicant_id,
            "Applicant Name": payload.get("account_name") or "",
            "Task Created By": task.created_by,
            "Assigned To": payload.get("assigned_to") or "Robie AI",
            "Task Due Date": task.due_date,
        }
        reassign_port = (
            _WorkerReassignPortAdapter(self.reassigner, task)
            if self.reassigner is not None else None
        )
        ports = build_robie_call_ports(
            store,
            phone_lookup=self.phone_lookup,
            bland=self.bland_client,
            discussion_client=self.client,
            task_reassign=reassign_port,
        )
        config = rch.RobieCallConfig(dry_run=self.call_dry_run)
        result = rch.handle_robie_call_task(task_dict, config, ports)

        note = result.get("writeback") or {}
        action: dict[str, Any] = {
            "call_task": True,
            "ok": result.get("ok"),
            "error": result.get("error"),
            "note": {
                "discussion_id": note.get("discussion_id"),
                "note_id": note.get("note_id"),
                "text": "robie call outcome",
            },
            "reassigned": (
                {"to": task.created_by,
                 "verified_assignee": task.created_by}
                if result.get("reassigned") else None
            ),
            "outcome_verified": result.get("outcome_verified"),
            "outcome_successful": result.get("outcome_successful"),
            "duplicate_suppressed": result.get("duplicate_suppressed", False),
        }
        if result.get("ok"):
            self._record(task, "completed",
                         f"Robie Call handled: {result.get('error') or 'done'}.",
                         timestamp)
        else:
            self._record(task, "failed",
                         f"Robie Call failed: {result.get('error')}.",
                         timestamp)
            raise NeedsHuman(
                f"Task {task.task_id}: Robie Call handler failed: "
                f"{result.get('error')} — a human must review."
            )
        return action

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

    def _post_note_verified(self, store: Any, job: dict[str, Any],
                            discussion_id: str, body: str) -> str:
        """Reserve once in the real JobStore, then reconcile by exact receipt ID.

        DiscussionApi has no idempotent POST contract. An intent without a
        durable receipt is permanently uncertain, including after a crash.
        Metadata changes and HTTP acceptance cannot authorize another send.
        """
        from .ezlynx_task_cdp import validate_identity
        from .store import canonical_json, utc_now
        payload = job.get("payload") or {}
        validate_identity(str(payload.get("task_id") or ""),
                          str(payload.get("applicant_id") or ""))
        if not discussion_id or self.client is None:
            raise ValueError("No discussion identity/client; note not sent")
        identity = {"task_id": payload["task_id"], "applicant_id": payload["applicant_id"],
                    "discussion_id": discussion_id,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
        kind = "task-note-intent"
        # INSERT is a durable compare-and-set, following the outbox contract.
        # No time-based lease can allow a second POST of an uncertain note.
        with store.transaction() as conn:
            row = conn.execute("SELECT data_json FROM checkpoints WHERE job_id=? AND kind=?",
                               (job["id"], kind)).fetchone()
            fresh = row is None
            intent = {**identity, "state": "uncertain", "note_id": ""} if fresh else json.loads(row[0])
            if any(intent.get(k) != v for k, v in identity.items()):
                raise UnverifiedNoteError("Existing note intent differs; not sent")
            if fresh:
                conn.execute("INSERT INTO checkpoints(job_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                             (job["id"], kind, canonical_json(intent), utc_now()))
        if fresh:
            # Verify the committed checkpoint before the external side effect.
            if store.get_checkpoint(job["id"], kind) != intent:
                raise UnverifiedNoteError("Note intent persistence failed; not sent")
            try:
                response = self.client.append_note(discussion_id, body)
            except Exception as exc:
                raise UnverifiedNoteError("Note acceptance uncertain; not reposting") from exc
            intent["note_id"] = _extract_note_id(response)
            store.checkpoint(job["id"], kind, intent)
        note_id = str(intent.get("note_id") or "")
        if not note_id:
            raise UnverifiedNoteError("No durable destination note ID; not reposting")
        record = self.client.get_discussion(discussion_id)
        if not _contains_note_id(record, note_id):
            raise UnverifiedNoteError("Exact destination note ID not found; not reposting")
        intent["state"] = "confirmed"
        store.checkpoint(job["id"], kind, intent)
        return note_id

    def _record(self, task: AssignedTask, action: str, detail: str, timestamp: str) -> None:
        self.results.append(TaskResult(
            task_id=task.task_id,
            applicant_id=task.applicant_id,
            action=action,
            detail=detail,
            timestamp=timestamp,
        ))


def _contains_note_id(record: dict[str, Any], note_id: str) -> bool:
    from .ezlynx_discussions import discussion_note_snapshot, iter_discussion_notes, _note_id_of
    return (discussion_note_snapshot(record).get("most_recent_note_id") == note_id
            or any(_note_id_of(row) == note_id for row in iter_discussion_notes(record)))


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
            record = self.client.get_discussion(discussion_id)
            snapshot = discussion_note_snapshot(record)
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
                current = self.reassigner.read_assignee(
                    task_id, str(payload.get("applicant_id") or ""),
                    description=str(payload.get("description") or ""),
                )
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

        if not _contains_note_id(record, note_id):
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
