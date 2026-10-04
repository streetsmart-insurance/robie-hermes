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

Exact client and no repeats:

- Before any write the applicant must be on the compiled write allowlist and
  EZLynx must confirm the report's Discussion ID is one of that applicant's
  discussions (`_assert_write_target`). The verifier repeats the check.
- Notes are reserved per (round, purpose) before the POST; a reassignment is
  reserved before the Save and reconciled by READING the assignee after a
  crash or unknown outcome. Nothing is repeated on a guess: an unknown Save is
  never replayed because the task still shows Robie (the UI Save is not an
  atomic compare-and-set); one more needs an explicit human authorization.
- A worker holds a lease on its job, renewed before every external effect, and
  every move it makes is fenced by that lease. A human's answer, and a note a
  human adopts, are bound to their own round and exact content.
- Return owner: Task Created By -> Assigned Producer -> CSR (or the person a
  human chose on resume); never Robie itself. The verifier recomputes it.

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
import os
import re
import uuid
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


# Worker-side checkpoint kinds. Note and reassignment intents are per round
# (a round starts when a task that was handed back returns to Robie) and, for
# notes, per purpose, so one job can legitimately write more than one note
# without ever repeating the same one.
LEGACY_NOTE_KIND = "task-note-intent"
NOTE_KIND_PREFIX = "task-note-intent:"
REASSIGN_KIND_PREFIX = "task-reassign-intent:"
HUMAN_ANSWER_PREFIX = "human-answer:"
LEASE_SECONDS = 900


def human_answer_kind(round_no: int) -> str:
    """A human's answer belongs to one round of one task; a later round asks again."""
    return f"{HUMAN_ANSWER_PREFIX}{round_no}"


def job_round(job: dict[str, Any]) -> int:
    return int((job.get("payload") or {}).get("round") or 0)

ROBIE_NAME = "Robie AI"
FIELD_LABELS = dict(REASSIGN_PRECEDENCE) | {"human_choice": "the person you chose"}


class NeedsHuman(Exception):
    """The worker needs a human answer before this job can proceed."""


class CallQueued(Exception):
    """The call is outside the calling window. Leave the job pending."""


class CallHeld(Exception):
    """The call was not placed and should be tried on a later pass."""


class UnverifiedNoteError(Exception):
    """A note post could not be confirmed via read-back."""


class LeaseLost(Exception):
    """This worker no longer holds the job's lease; it must stop and change nothing."""


class _Lease:
    """One worker's lease on one Job, renewed before every external effect.

    The store's own lease is the fence: another worker can only take the Job
    after this lease lapses and is recovered, and every transition made here
    is applied only while this worker still owns it. EZLynx cannot check a
    token, so the lease margin (LEASE_SECONDS, renewed before each effect) plus
    the durable intents bound the residual risk; they do not remove it.
    Stores without lease support (some test doubles) run unfenced.
    """

    def __init__(self, store: Any, job_id: str):
        self.store = store
        self.job_id = job_id
        self.supported = hasattr(store, "claim") and hasattr(store, "renew_lease")
        self.token = f"task-intake:{os.getpid()}:{uuid.uuid4().hex}" if self.supported else None

    def acquire(self) -> bool:
        if not self.supported:
            return True
        return self.store.claim(self.job_id, self.token, lease_seconds=LEASE_SECONDS) is not None

    def fence(self) -> None:
        if not self.supported:
            return
        try:
            self.store.renew_lease(self.job_id, self.token, lease_seconds=LEASE_SECONDS)
        except RuntimeError as exc:
            raise LeaseLost(str(exc)) from exc

    def kw(self) -> dict[str, Any]:
        return {"lease_owner": self.token} if self.supported else {}

    def release(self) -> None:
        if not self.supported:
            return
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL WHERE id=? AND lease_owner=?",
                (self.job_id, self.token),
            )


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


_TEST_WORD = re.compile(r"\btest(?:s|ing)?\b")
_ROBIE_WORD = re.compile(r"\b(?:robie|roby)\b")


def is_test_task(task: AssignedTask) -> bool:
    """True for obvious test/placeholder tasks (left alone, not reassigned).

    Whole words only: "latest" and "problem" must never make a real request
    look like a test task, because a test task is never handed back.
    """
    text = f"{task.title} {task.description}".lower()
    return bool(_TEST_WORD.search(text) and _ROBIE_WORD.search(text))


def reassign_candidates(
    task: AssignedTask, *, human_choice: str | None = None
) -> list[tuple[str, str]]:
    """Ordered return owners as (name, field).

    A person a human chose on resume replaces the precedence. Otherwise it is
    Task Created By -> Assigned Producer -> CSR, skipping blanks, duplicates,
    Robie itself and whoever currently holds the task: Robie must never be
    its own return target.
    """
    if human_choice and human_choice.strip():
        return [(human_choice.strip(), "human_choice")]
    values = {
        "task_created_by": task.created_by,
        "assigned_producer": task.assigned_producer,
        "csr": task.csr,
    }
    holder = (task.assigned_to or "").strip().casefold()
    seen: set[str] = {ROBIE_NAME.casefold(), holder}
    out: list[tuple[str, str]] = []
    for field_name, _label in REASSIGN_PRECEDENCE:
        name = (values.get(field_name) or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append((name, field_name))
    return out


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
        attempted = False
        try:
            from .ezlynx_task_cdp import validate_identity
            validate_identity(task_id, self._task.applicant_id)
            if task_id != self._task.task_id:
                raise ValueError("Foreign task identity; reassignment not sent")
            attempted = True
            verified = self._reassigner.reassign(
                task_id, self._task.applicant_id, new_assignee,
                description=self._task.description,
                expected_assignee=self._task.assigned_to)
            current = self.read_task_assignee(task_id)
            if not isinstance(verified, str) or not current or current.casefold() != new_assignee.casefold():
                raise ValueError("No destination proof for reassignment")
            return {"ok": True, "sent": True, "verified_assignee": current}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "sent": None if attempted else False,
                    "delivered": False,
                    "delivery_status": "unverified" if attempted else "not_sent",
                    "error": str(exc)[:300]}

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
        transfer_lookup: Any | None = None,
        opt_out_store: Any | None = None,
        opt_in_store: Any | None = None,
        call_dedupe: Any | None = None,
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
        self.transfer_lookup = transfer_lookup
        self.opt_out_store = opt_out_store
        self.opt_in_store = opt_in_store
        self.call_dedupe = call_dedupe
        self._lease: _Lease | None = None
        self._last_note_kind = LEGACY_NOTE_KIND

    # -- job lifecycle -------------------------------------------------

    def process_job(self, store: Any, job: dict[str, Any]) -> dict[str, Any]:
        """Claim one PENDING job and work it through to VERIFYING.

        Returns the job row after the terminal transition of this phase.
        COMPLETE is never set here — only the independent verifier may.

        The job's lease is taken first and every move is fenced by it, so a
        worker that lost its lease (and was replaced) changes nothing.
        """
        job_id = job["id"]
        lease = _Lease(store, job_id)
        if not lease.acquire():
            logger.info("Job %s is leased by another worker; not starting", job_id)
            return store.get_job(job_id)
        try:
            job = store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING}, **lease.kw())
        except Exception:
            lease.release()
            raise
        self._lease = lease
        try:
            return self._run_claimed_job(store, job, lease)
        except LeaseLost as e:
            logger.warning("Job %s: lease lost, stopping without changes: %s", job_id, e)
            return store.get_job(job_id)
        finally:
            self._lease = None

    def _move(self, store: Any, lease: _Lease, job_id: str, status: JobStatus, **kwargs: Any) -> dict[str, Any]:
        try:
            return store.transition(job_id, status, expected={JobStatus.RUNNING},
                                    release_lease=True, **lease.kw(), **kwargs)
        except RuntimeError as exc:
            if "lease" in str(exc):
                raise LeaseLost(str(exc)) from exc
            raise

    def _run_claimed_job(self, store: Any, job: dict[str, Any], lease: _Lease) -> dict[str, Any]:
        job_id = job["id"]
        try:
            action = self._do_work(store, job)
        except CallQueued as e:
            logger.info("Job %s queued until the calling window: %s", job_id, e)
            return self._move(store, lease, job_id, JobStatus.PENDING, error=str(e)[:500])
        except CallHeld as e:
            logger.info("Job %s not dialed; left pending: %s", job_id, e)
            return self._move(store, lease, job_id, JobStatus.PENDING, error=str(e)[:500])
        except NeedsHuman as e:
            logger.warning(f"Job {job_id} needs a human: {e}")
            return self._move(store, lease, job_id, JobStatus.AWAITING_HUMAN_INPUT,
                              error=str(e)[:500], resume_status=JobStatus.PENDING)
        except UnverifiedNoteError as e:
            logger.warning(f"Job {job_id} has an unconfirmed note: {e}")
            return self._move(
                store, lease, job_id, JobStatus.AWAITING_HUMAN_INPUT,
                error=(f"Note state is uncertain and will not be reposted: {e}. "
                       "Check the discussion in EZLynx, then resume with --note-id "
                       "if the note is there."),
                resume_status=JobStatus.PENDING)
        except LeaseLost:
            raise
        except Exception as e:  # noqa: BLE001 — fail-closed with the error on the job
            logger.error(f"Job {job_id} failed: {e}")
            return self._move(store, lease, job_id, JobStatus.FAILED,
                              error=f"{type(e).__name__}: {e}"[:500])
        lease.fence()
        store.checkpoint(job_id, "action", action)
        return self._move(store, lease, job_id, JobStatus.VERIFYING)

    def _fence(self) -> None:
        """Renew this worker's lease immediately before an external effect."""
        if self._lease is not None:
            self._lease.fence()

    # -- the work ------------------------------------------------------

    def _do_work(self, store: Any, job: dict[str, Any]) -> dict[str, Any]:
        """Attempt the task. Returns the action checkpoint for the verifier."""
        payload = job.get("payload") or {}
        task = _task_from_payload(payload)
        from .ezlynx_task_cdp import validate_identity
        validate_identity(task.task_id, task.applicant_id)
        # No write of any kind (note, reassignment, call) before the applicant
        # is cleared and the discussion is proven to belong to it.
        self._assert_write_target(task)
        timestamp = utcnow_iso()

        if is_test_task(task):
            note = self._note_record(
                store, job, task.discussion_id,
                "Roby here — this looks like a test task, so I'm leaving it "
                "alone. No action taken.",
                purpose="test-ack", text="test-task acknowledgment",
            )
            self._record(task, "completed", "Test task; note posted, no action taken.", timestamp)
            return {
                "test_task": True,
                "category": "test",
                "note": note,
                "reassigned": None,
            }

        category = self._categorize_task(task)

        # Route callback tasks to the Robie Call handler (PR #746) when its
        # ports are wired. The handler runs inside this same durable job —
        # one job per EZLynx task — checkpointing under "robie-call:<id>".
        if category == "callback" and self._call_handler_available():
            return self._do_call_task(store, job, task, timestamp)

        answer = self._human_answer(store, job, task)
        candidates = reassign_candidates(
            task, human_choice=str(answer.get("assign_to") or "").strip() or None
        )

        if not candidates:
            # No valid person to send this back to — say so on the task,
            # then wait for a human on the SAME job.
            self._post_note_verified(
                store, job, task.discussion_id,
                "Roby here — I read this task but couldn't complete it, and I "
                "couldn't tell who to send it back to (no creator, producer, "
                "or CSR listed). Flagging for the team.",
                purpose="needs-target",
            )
            self._record(task, "awaiting_human", "No reassignment target; flagged for team.", timestamp)
            raise NeedsHuman(
                f"Task {task.task_id}: no Task Created By / Assigned Producer / CSR — "
                "a human must pick who gets this task back "
                "(resume with --assign-to NAME)."
            )

        if not (self.reassign_enabled and self.reassigner is not None):
            target, target_field = candidates[0]
            self._post_note_verified(
                store, job, task.discussion_id,
                f"Roby here — I read this request ({category}) but I can't "
                f"complete it myself. It needs to go back to {target} "
                f"({FIELD_LABELS[target_field]}); automatic reassignment is off, so "
                "please reassign it in EZLynx.",
                purpose="gate-off",
            )
            self._record(task, "awaiting_human", f"Reassignment gate off; flagged for reassign to {target}.", timestamp)
            raise NeedsHuman(
                f"Task {task.task_id}: reassignment gate is off — a human must "
                f"reassign to {target} in EZLynx."
            )

        reassigned = self._reassign_with_reconciliation(store, job, task, candidates, answer)
        note = self._note_record(
            store, job, task.discussion_id,
            f"Roby here — I read this request ({category}) but I can't "
            f"complete it myself, so I've sent it back to "
            f"{reassigned['verified_assignee']} ({FIELD_LABELS[reassigned['field']]}).",
            purpose="handoff", text="handoff to " + reassigned["verified_assignee"],
        )
        self._record(task, "completed", f"Reassigned to {reassigned['verified_assignee']}; note posted.", timestamp)
        return {
            "test_task": False,
            "category": category,
            "reassign_target": reassigned["to"],
            "reassign_field": reassigned["field"],
            "note": note,
            "reassigned": reassigned,
        }

    def _human_answer(self, store: Any, job: dict[str, Any], task: AssignedTask) -> dict[str, Any]:
        """The human's answer for THIS round of THIS task; anything else is ignored."""
        round_no = job_round(job)
        data = store.get_checkpoint(job["id"], human_answer_kind(round_no)) or {}
        if data and (str(data.get("task_id")) != task.task_id
                     or str(data.get("applicant_id")) != task.applicant_id
                     or int(data.get("round", -1)) != round_no):
            return {}
        return data

    # -- exact client ------------------------------------------------------

    def _assert_write_target(self, task: AssignedTask) -> None:
        """Refuse every write unless the applicant is cleared and owns the discussion.

        The report's Discussion ID is data, not proof. EZLynx is asked which
        discussions belong to this applicant, and the write allowlist is the
        repo's compiled one. Anything unproven pauses the job with no write.
        """
        from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant

        try:
            applicant = require_allowed_ezlynx_write_applicant(task.applicant_id)
        except Exception as exc:  # noqa: BLE001 — scope/driver refusals pause the job
            raise NeedsHuman(
                f"Task {task.task_id}: applicant {task.applicant_id} is not cleared "
                f"for Robie writes ({type(exc).__name__}); nothing was written."
            ) from exc
        lookup = getattr(self.client, "get_discussion_ids", None)
        if lookup is None:
            raise NeedsHuman(
                f"Task {task.task_id}: cannot prove discussion {task.discussion_id} "
                f"belongs to applicant {applicant}; nothing was written."
            )
        try:
            owned = [str(item).strip() for item in lookup(applicant)]
        except Exception as exc:  # noqa: BLE001 — transient read: resume retries
            raise NeedsHuman(
                f"Task {task.task_id}: could not read applicant {applicant}'s "
                f"discussions ({type(exc).__name__}); nothing was written. "
                "Resume to retry."
            ) from exc
        if owned.count(str(task.discussion_id).strip()) != 1:
            raise NeedsHuman(
                f"Task {task.task_id}: discussion {task.discussion_id} is not one of "
                f"applicant {applicant}'s discussions; nothing was written."
            )

    # -- reassignment with durable intent and reconciliation ----------------

    def _read_assignee(self, task: AssignedTask) -> str:
        return str(self.reassigner.read_assignee(
            task.task_id, task.applicant_id, description=task.description) or "").strip()

    def _reassign_with_reconciliation(
        self, store: Any, job: dict[str, Any], task: AssignedTask,
        candidates: list[tuple[str, str]], answer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Hand the task to the first return owner EZLynx can select.

        An intent is committed before any Save. After a crash or an unknown
        outcome the assignee is READ, never assumed: already the target means
        it landed. Anything else is NOT proof the Save did not happen or will
        not still land (the UI Save is not an atomic compare-and-set), so Robie
        never repeats it on its own: the job pauses, and one more Save needs an
        explicit human authorization (--allow-retry-save) that is good for that
        one attempt only.
        """
        from .ezlynx_task_cdp import AssigneeUnresolvedError

        answer = answer or {}
        kind = f"{REASSIGN_KIND_PREFIX}{job_round(job)}"
        expected = (task.assigned_to or ROBIE_NAME).strip() or ROBIE_NAME
        intent = store.get_checkpoint(job["id"], kind) or {}
        skipped = [str(name) for name in intent.get("skipped") or []]
        attempt = int(intent.get("attempt") or 0)

        def save(state: str, target: str, **extra: Any) -> None:
            data = {"task_id": task.task_id, "applicant_id": task.applicant_id,
                    "expected_assignee": expected, "target": target, "state": state,
                    "attempt": attempt, "skipped": list(skipped), **extra}
            store.checkpoint(job["id"], kind, data)
            if store.get_checkpoint(job["id"], kind) != data:
                raise UnverifiedNoteError("Reassignment intent not durable; nothing sent")

        def result(name: str, field: str, verified: str) -> dict[str, Any]:
            return {"to": name, "field": field, "verified_assignee": verified,
                    "skipped_unresolved": list(skipped)}

        def retry_authorized(name: str) -> bool:
            grant = answer.get("retry_save") or {}
            return (str(grant.get("target") or "").casefold() == name.casefold()
                    and int(grant.get("after_attempt", -1)) == attempt)

        for name, field in candidates:
            if name.casefold() in {item.casefold() for item in skipped}:
                continue
            same_target = str(intent.get("target") or "").casefold() == name.casefold()
            if same_target and intent.get("state") in ("applied", "attempting", "uncertain"):
                try:
                    current = self._read_assignee(task)
                except Exception as exc:  # noqa: BLE001
                    raise NeedsHuman(
                        f"Task {task.task_id}: could not read the assignee to reconcile "
                        f"an earlier reassignment to {name} ({type(exc).__name__}); "
                        "nothing was sent. Resume to retry."
                    ) from exc
                if current.casefold() == name.casefold():
                    save("applied", name, verified_assignee=current)
                    return result(name, field, current)
                if not retry_authorized(name):
                    raise NeedsHuman(
                        f"Task {task.task_id}: an earlier reassignment to {name} has an unknown "
                        f"result and the task now shows {current or 'unknown'!r}. I will not repeat "
                        "the Save on my own, because it may still land. Check EZLynx; if the task is "
                        f"still with {expected} and you want me to try once more, resume with "
                        "--allow-retry-save."
                    )
                if current.casefold() != expected.casefold():
                    raise NeedsHuman(
                        f"Task {task.task_id}: the assignee is {current!r}, not {expected!r} "
                        f"or {name!r}; nothing was sent. A human must look at the task."
                    )
            attempt += 1
            save("attempting", name)
            self._fence()
            try:
                verified = self.reassigner.reassign(
                    task.task_id, task.applicant_id, name,
                    description=task.description, expected_assignee=expected)
            except AssigneeUnresolvedError:
                skipped.append(name)
                save("skipped", name)
                intent = {"target": name, "state": "skipped"}
                continue
            except Exception as exc:  # noqa: BLE001 — outcome unknown: read, never assume
                save("uncertain", name, error=str(exc)[:200])
                try:
                    current = self._read_assignee(task)
                except Exception:  # noqa: BLE001
                    current = ""
                if current.casefold() == name.casefold():
                    save("applied", name, verified_assignee=current)
                    return result(name, field, current)
                raise NeedsHuman(
                    f"Task {task.task_id}: the reassignment to {name} could not be "
                    f"confirmed ({type(exc).__name__}); the task shows {current or 'unknown'!r}. "
                    "Robie will not repeat the Save on its own. Check EZLynx, then resume this job."
                ) from exc
            if str(verified).strip().casefold() != name.casefold():
                save("uncertain", name, error=f"reassigner reported {str(verified)!r}")
                raise NeedsHuman(
                    f"Task {task.task_id}: asked to return the task to {name} but EZLynx "
                    f"reported {str(verified)!r}; nothing more was sent. A human must look at the task."
                )
            save("applied", name, verified_assignee=str(verified))
            return result(name, field, str(verified))

        raise NeedsHuman(
            f"Task {task.task_id}: none of the return owners could be selected in EZLynx "
            f"({', '.join(name for name, _ in candidates)}); a human must pick one "
            "(resume with --assign-to NAME)."
        )

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
            "Applicant Name": payload.get("account_name") or task.applicant_name or "",
            "Task Created By": task.created_by,
            "Assigned Producer": task.assigned_producer,
            "Assigned To": payload.get("assigned_to") or "Robie AI",
            "Task Due Date": task.due_date,
            "Activity Labels": task.activity_labels,
            "Discussion ID": task.discussion_id,
            "Workflow": payload.get("workflow") or "",
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
            transfer_lookup=self.transfer_lookup,
            opt_out_store=self.opt_out_store,
            opt_in_store=self.opt_in_store,
            call_dedupe=self.call_dedupe,
        )
        config = rch.RobieCallConfig(
            dry_run=self.call_dry_run,
            sms_configured=os.environ.get("ROBIE_CALL_SMS_CONFIGURED") == "1",
        )
        result = rch.handle_robie_call_task(task_dict, config, ports)
        if result.get("skipped_opt_in"):
            self._record(
                task, "skipped",
                "Marketing call skipped; no recorded opt-in.",
                timestamp,
            )
            raise CallHeld(str(result.get("error") or "no recorded opt-in"))
        if result.get("queued_for_calling_window"):
            self._record(
                task, "queued",
                f"Robie Call queued: {result.get('error')}.",
                timestamp,
            )
            raise CallQueued(str(result.get("error") or "outside calling window"))

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
        from .call_pickup import classify_call_request

        labeled = classify_call_request(task.activity_labels, task.description)
        if labeled.action in ("workflow", "freeform"):
            return "callback"
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
                            discussion_id: str, body: str, purpose: str = "note") -> str:
        """Reserve once in the real JobStore, then reconcile by exact receipt ID.

        DiscussionApi has no idempotent POST contract. An intent without a
        durable receipt is permanently uncertain, including after a crash.
        Metadata changes and HTTP acceptance cannot authorize another send.
        The reservation is per (round, purpose): a resume or a new round may
        write a different note, but never the same one twice.

        A note a human adopted after an uncertain post (--note-id) is accepted
        only when the discussion shows that exact ID once, with exactly the text
        Robie meant to write; otherwise it stays unconfirmed.
        """
        from .ezlynx_task_cdp import validate_identity
        from .store import canonical_json, utc_now
        payload = job.get("payload") or {}
        validate_identity(str(payload.get("task_id") or ""),
                          str(payload.get("applicant_id") or ""))
        if not discussion_id or self.client is None:
            raise ValueError("No discussion identity/client; note not sent")
        round_no = job_round(job)
        base = {"task_id": payload["task_id"], "applicant_id": payload["applicant_id"],
                "discussion_id": discussion_id,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
        identity = {**base, "purpose": purpose, "round": round_no}
        kind = f"{NOTE_KIND_PREFIX}{round_no}:{purpose}"
        legacy = store.get_checkpoint(job["id"], LEGACY_NOTE_KIND)
        if legacy is not None:
            if all(legacy.get(k) == v for k, v in base.items()):
                kind, identity = LEGACY_NOTE_KIND, base  # continue an intent written before per-purpose kinds
            elif legacy.get("state") != "confirmed":
                raise UnverifiedNoteError("Earlier note intent is unresolved; not sent")
        self._last_note_kind = kind
        self._fence()
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
        if intent.get("adopted") and intent.get("state") != "confirmed":
            self._assert_adopted_note(intent, payload, discussion_id, body, record, note_id)
        intent["state"] = "confirmed"
        store.checkpoint(job["id"], kind, intent)
        return note_id

    @staticmethod
    def _assert_adopted_note(intent: dict[str, Any], payload: dict[str, Any], discussion_id: str,
                             body: str, record: Any, note_id: str) -> None:
        adoption = intent.get("adoption") or {}
        if (str(adoption.get("note_id")) != note_id
                or str(adoption.get("applicant_id")) != str(payload.get("applicant_id"))
                or str(adoption.get("discussion_id")) != str(discussion_id)):
            raise UnverifiedNoteError("Adopted note is not bound to this applicant and discussion")
        text = _note_text_for_id(record, note_id)
        if text is None:
            raise UnverifiedNoteError("Adopted note's text cannot be read, so it cannot be "
                                      "confirmed as the note Robie meant to write")
        if _norm_text(text) != _norm_text(body):
            raise UnverifiedNoteError("Adopted note does not match the note Robie meant to write")

    def _note_record(self, store: Any, job: dict[str, Any], discussion_id: str, body: str,
                     *, purpose: str, text: str) -> dict[str, Any]:
        """Post (or reconcile) one note and describe it for the verifier."""
        note_id = self._post_note_verified(store, job, discussion_id, body, purpose=purpose)
        intent = store.get_checkpoint(job["id"], self._last_note_kind) or {}
        return {
            "discussion_id": discussion_id,
            "note_id": note_id,
            "text": text,
            "purpose": purpose,
            "round": job_round(job),
            "body_norm_sha256": hashlib.sha256(_norm_text(body).encode()).hexdigest(),
            "adopted": bool(intent.get("adopted")),
        }

    def _record(self, task: AssignedTask, action: str, detail: str, timestamp: str) -> None:
        self.results.append(TaskResult(
            task_id=task.task_id,
            applicant_id=task.applicant_id,
            action=action,
            detail=detail,
            timestamp=timestamp,
        ))


_NOTE_TEXT_KEYS = ("body", "Body", "text", "Text", "noteText", "NoteText", "note", "Note", "content", "Content")


def _norm_text(text: Any) -> str:
    return " ".join(str(text or "").split())


def _note_text_for_id(record: Any, note_id: str) -> str | None:
    """The text of the ONE note with this ID, or None if it is not shown exactly once with text."""
    from .ezlynx_discussions import iter_discussion_notes, _note_id_of

    rows = [row for row in iter_discussion_notes(record) if _note_id_of(row) == note_id]
    if len(rows) != 1:
        return None
    for key in _NOTE_TEXT_KEYS:
        value = rows[0].get(key) if isinstance(rows[0], dict) else None
        if isinstance(value, str) and value.strip():
            return value
    return None


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
        activity_labels=str(payload.get("activity_labels") or ""),
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
        store: Any | None = None,
    ):
        self.client = discussion_client
        self.reassigner = task_reassigner
        self.store = store

    def _return_owner_problem(self, job: dict[str, Any], action: dict[str, Any]) -> str | None:
        """Why the recorded return owner is not the one the rules require.

        Recomputed from the job payload, not from the worker's own claim:
        Created By -> Assigned Producer -> CSR, skipping only people the
        worker recorded as unselectable in EZLynx, or a person a human chose.
        """
        if action.get("call_task") or not action.get("reassigned"):
            return None
        reassigned = action["reassigned"]
        target = str(reassigned.get("to") or "").strip()
        if not target:
            return "reassignment record has no target"
        verified = str(reassigned.get("verified_assignee") or "").strip()
        if verified.casefold() != target.casefold():
            return f"assignee read back as {verified!r}, not the intended return owner {target!r}"
        task = _task_from_payload(job.get("payload") or {})
        if str(action.get("reassign_field") or "") == "human_choice":
            if self.store is None:
                return None
            round_no = job_round(job)
            answer = self.store.get_checkpoint(job["id"], human_answer_kind(round_no)) or {}
            if (str(answer.get("task_id")) != task.task_id
                    or str(answer.get("applicant_id")) != task.applicant_id
                    or int(answer.get("round", -1)) != round_no):
                return f"no human answer is recorded for round {round_no} of this task"
            chosen = str(answer.get("assign_to") or "").strip()
            if chosen.casefold() != target.casefold():
                return f"return owner {target!r} is not the person a human chose ({chosen!r})"
            return None
        names = [name for name, _ in reassign_candidates(task)]
        folded = [name.casefold() for name in names]
        if target.casefold() not in folded:
            return f"return owner {target!r} is not Task Created By, Assigned Producer or CSR"
        skipped = {str(item).casefold() for item in reassigned.get("skipped_unresolved") or []}
        passed_over = [n for n in names[:folded.index(target.casefold())]
                       if n.casefold() not in skipped]
        if passed_over:
            return (f"{passed_over[0]!r} comes before {target!r} in Task Created By -> "
                    "Assigned Producer -> CSR and was not recorded as unselectable")
        return None

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

        problem = self._return_owner_problem(job, action)
        if problem:
            return _unverified(
                job, captured, discussion_id,
                {"note_id": note_id, "discussion_id": discussion_id}, {}, error=problem,
            )

        applicant_id = str(payload.get("applicant_id") or "")
        lookup = getattr(self.client, "get_discussion_ids", None)
        if lookup is None:
            return _unverified(
                job, captured, discussion_id,
                {"note_id": note_id, "discussion_id": discussion_id}, {},
                error="verifier cannot prove the discussion belongs to the applicant",
            )
        try:
            owned = [str(item).strip() for item in lookup(applicant_id)]
        except Exception as e:  # noqa: BLE001 — transient read failure retries
            return _unverified(
                job, captured, discussion_id,
                {"note_id": note_id, "discussion_id": discussion_id},
                {"error": f"discussion ownership read failed: {e}"},
                error=f"discussion ownership read failed: {e}", retryable=True,
            )
        if owned.count(discussion_id) != 1:
            return _unverified(
                job, captured, discussion_id,
                {"note_id": note_id, "discussion_id": discussion_id, "applicant_id": applicant_id},
                {"discussion_ids": owned[:20]},
                error=f"discussion {discussion_id} is not one of applicant {applicant_id}'s discussions",
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

        if (action.get("note") or {}).get("adopted"):
            text = _note_text_for_id(record, note_id)
            want = str((action.get("note") or {}).get("body_norm_sha256") or "")
            if text is None or hashlib.sha256(_norm_text(text).encode()).hexdigest() != want:
                return _unverified(
                    job, captured, discussion_id,
                    {"note_id": note_id, "discussion_id": discussion_id}, {},
                    error="the adopted note's text does not match the note Robie meant to write",
                )

        latest = str(snapshot.get("most_recent_note_id") or "")
        expected: dict[str, Any] = {"note_id": note_id, "discussion_id": discussion_id}
        observed: dict[str, Any] = {
            "note_id": note_id if _contains_note_id(record, note_id) else latest,
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
