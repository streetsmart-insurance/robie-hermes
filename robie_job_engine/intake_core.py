"""Shared Phase 1 intake contracts. No production registration or completion authority.

The live EZLynx adapter is supplied by the server integration owner; these are
internal contracts, not invented EZLynx HTTP endpoints or response schemas.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .runtime_env import current_robie_env


class IntakeHold(ValueError):
    """A human or an authoritative source must resolve the item first."""


def require_test() -> None:
    if current_robie_env() != "TEST":
        raise IntakeHold("Phase 1 intake is enabled only in ROBIE_ENV=TEST")


@dataclass(frozen=True)
class SourceItem:
    system: str
    source_account: str
    source_id: str
    source_url: str
    received_at: str
    filename: str
    content: bytes

    def validate(self) -> None:
        if not all((self.system, self.source_account, self.source_id, self.source_url, self.filename, self.content)):
            raise IntakeHold("Source identity, original content and retrieval reference are required")
        if len(self.content) > 25 * 1024 * 1024:
            raise IntakeHold("Source exceeds the 25 MiB Phase 1 intake limit")
        when = datetime.fromisoformat(self.received_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise IntakeHold("Source receipt time requires a timezone")

    @property
    def key(self) -> str:
        # Process is deliberately excluded: replay through another inbox flow
        # cannot silently create a second task for this same source item.
        identity = json.dumps([self.system, self.source_account, self.source_id], separators=(",", ":"))
        return "intake:" + hashlib.sha256(identity.encode()).hexdigest()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class SourceArchive:
    """Preserve original bytes in a private, non-executable local artifact store."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def preserve(self, item: SourceItem) -> Path:
        item.validate()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise IntakeHold("Source archive directory must be private (0700)")
        path = self.root / (item.key.split(":", 1)[1] + ".source")
        # An interrupted partial write is never overwritten or silently accepted.
        # This intentionally holds for recovery if an existing artifact differs.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item.digest:
                raise IntakeHold("Existing source artifact differs; retain it for recovery")
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(item.content)
                handle.flush()
                os.fsync(handle.fileno())
        return path


@dataclass(frozen=True)
class Identifiers:
    applicant_id: str = ""
    policy_id: str = ""
    policy_number: str = ""
    policy_effective_date: str = ""
    insured_email: str = ""
    insured_name: str = ""


@dataclass(frozen=True)
class ReadResult:
    rows: tuple[Mapping[str, Any], ...]
    authoritative: bool
    complete: bool

    def checked(self) -> tuple[Mapping[str, Any], ...]:
        if not self.authoritative or not self.complete:
            raise IntakeHold("API lookup is unavailable, stale, or incomplete")
        return self.rows


class EzlynxIntakePort(Protocol):
    """Fresh reads only. Lookup pagination must finish before complete=True.

    create_task_once MUST guarantee at most one remote task per source_key,
    including concurrent requests and a timed-out response. The adapter must
    refuse writes until its idempotency implementation is independently proven.
    A process-local dictionary or lookup-then-POST is not that guarantee.
    """
    def lookup_candidates(self, identifiers: Identifiers) -> ReadResult: ...
    def lookup_assignee(self, user_id: str) -> ReadResult: ...
    def find_source_tasks(self, source_key: str) -> ReadResult: ...
    def find_related_work(self, applicant_id: str, policy_id: str, source: SourceItem) -> ReadResult: ...
    def create_task_once(self, task: Mapping[str, Any]) -> str: ...
    def read_task(self, task_id: str) -> ReadResult: ...


def resolve_match(identifiers: Identifiers, result: ReadResult) -> tuple[str, str]:
    provided = {k: v.strip() for k, v in asdict(identifiers).items() if v.strip()}
    if not any(provided.get(k) for k in ("applicant_id", "policy_id", "policy_number", "insured_email")):
        raise IntakeHold("Name-only match requires human identification")
    policy_required = any(provided.get(k) for k in ("policy_id", "policy_number", "policy_effective_date"))
    matches = set()
    for row in result.checked():
        if not all(str(row.get(k) or "").strip().casefold() == v.casefold() for k, v in provided.items()):
            continue
        applicant = str(row.get("applicant_id") or "").strip()
        policy = str(row.get("policy_id") or "").strip() if policy_required else ""
        if applicant and (policy or not policy_required):
            matches.add((applicant, policy))
    if len(matches) != 1:
        raise IntakeHold("Account/policy match is missing or ambiguous")
    return next(iter(matches))


class IntakeWorker:
    """Prepare an assigned task receipt; only JobEngine verification can finish."""
    process: str
    title: str
    source_system: str

    def __init__(self, api: EzlynxIntakePort, archive: SourceArchive):
        self.api = api
        self.archive = archive

    def assignment_for(self, applicant, policy, assignee_id, request_type):
        return assignee_id, ""

    def perform(self, source: SourceItem, identifiers: Identifiers, *, assignee_id: str = "", due_at: str,
                request_type: str = "") -> WorkerResult:
        action = "intake." + self.process
        detail: dict[str, Any] = {}
        try:
            require_test()
            if source.system != self.source_system:
                raise IntakeHold("Source belongs to a different intake system")
            artifact = self.archive.preserve(source)
            detail = {"source_artifact": str(artifact), "source_key": source.key, "manual_upload_pending": True}
            if datetime.fromisoformat(due_at.replace("Z", "+00:00")).tzinfo is None:
                raise IntakeHold("Task due time requires a timezone")
            applicant, policy = resolve_match(identifiers, self.api.lookup_candidates(identifiers))
            require_allowed_ezlynx_write_applicant(applicant)
            assignee_id, routing_note = self.assignment_for(applicant, policy, assignee_id, request_type)
            if not assignee_id.strip():
                raise IntakeHold("A configured Phase 1 assignee ID is required")
            assignees = self.api.lookup_assignee(assignee_id).checked()
            if len(assignees) != 1 or assignees[0].get("user_id") != assignee_id or assignees[0].get("active") is not True:
                raise IntakeHold("Assignee is missing, ambiguous or inactive")
            expected = {
                "applicant_id": applicant, "policy_id": policy,
                "assigned_user_id": assignee_id, "due_at": due_at,
                "source_key": source.key, "source_sha256": source.digest,
                "source_url": source.source_url, "process": self.process,
                "title": self.title, "manual_upload_required": True,
                "description": (
                    routing_note +
                    f"Source: {source.source_url}\nReceived: {source.received_at}\n"
                    f"Original file: {source.filename}\nSource key: {source.key}\n"
                    "MANUAL UPLOAD REQUIRED: retrieve the preserved original, upload it to this "
                    "EZLynx account/policy, and verify it is present. Review and perform the "
                    "requested service under the SOP. Intake does not fulfill the request."
                ),
            }
            existing = self.api.find_source_tasks(source.key).checked()
            if existing:
                if len(existing) != 1 or not task_matches(expected, existing[0]):
                    raise IntakeHold("Existing intake task conflicts with this source or assignment")
                task_id = str(existing[0].get("task_id") or "")
                detail["intake_disposition"] = "existing_task_reused"
            else:
                if self.api.find_related_work(applicant, policy, source).checked():
                    raise IntakeHold("Related work already exists; human must attach/update it to avoid duplication")
                # A failed write must not become permission to repeat a POST.
                # Adapter guarantees one task per stable source key.
                task_id = self.api.create_task_once(expected)
                detail["intake_disposition"] = "task_create_requested"
            if not task_id:
                raise IntakeHold("Task write has no stable identifier; reconcile before continuing")
            return WorkerResult(True, action, {**expected, "task_id": task_id},
                                detail, retryable=False)
        except Exception as exc:
            # Do not expose response bodies, credentials, or source content.
            reason = str(exc) if isinstance(exc, IntakeHold) else f"Intake unavailable ({type(exc).__name__}); reconcile before retry"
            return WorkerResult(False, action, {}, detail, retryable=False, error=reason,
                                hold_status=JobStatus.NEEDS_CLARIFICATION if isinstance(exc, IntakeHold) else JobStatus.UNVERIFIED)


def task_matches(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    return all(observed.get(k) == value for k, value in expected.items())


class IntakeVerifier:
    """Always re-read EZLynx independently, even when the worker saw a duplicate."""

    def __init__(self, api: EzlynxIntakePort):
        self.api = api

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        expected = dict(action.get("destination") or {})
        observed: Mapping[str, Any] = {}
        authoritative = False
        try:
            require_test()
            task_id = str(expected.get("task_id") or "")
            if not task_id:
                raise IntakeHold("No task identifier")
            result = self.api.read_task(task_id)
            rows = result.checked()
            authoritative = True
            if len(rows) == 1:
                observed = rows[0]
            verified = bool(observed) and task_matches(expected, observed)
        except Exception:
            verified = False
        evidence = VerificationEvidence(
            method="EZLYNX_API_READBACK", source="ezlynx-intake-adapter",
            expected=expected, observed=dict(observed), authoritative=authoritative,
            captured_at=datetime.now(timezone.utc).isoformat(), locator=expected.get("task_id"),
        )
        return VerificationResult(verified, evidence, retryable=False,
                                  error=None if verified else "Assigned intake task could not be independently verified",
                                  hold_status=None if verified else JobStatus.UNVERIFIED)
