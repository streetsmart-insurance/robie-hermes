"""Durable Test-only coordinator for EZLynx intake task browser writes.

The browser port is intentionally a narrow boundary.  Its concrete Playwright
locators must be captured and verified on ``hermes-test-01``; this module does
not guess EZLynx selectors or task URLs.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping, Protocol

from .idempotency import DurableWorkLedger, IdempotencyError
from .intake_core import Identifiers, IntakeHold, ReadResult, require_test, task_matches


NAMESPACE = "ezlynx-intake-task-v1"


class EzlynxTaskBrowserPort(Protocol):
    """Fresh, authoritative operations implemented by persistent Test Chrome."""

    def find_source_tasks(self, source_key: str) -> ReadResult: ...
    def find_related_work(
        self, applicant_id: str, policy_id: str, source: Any
    ) -> ReadResult: ...
    def create_task(self, task: Mapping[str, Any]) -> None: ...
    def read_task(self, task_id: str) -> ReadResult: ...


class DurableEzlynxIntakeTaskAdapter:
    """Combine documented API reads with at-most-one browser task creation.

    The durable ledger records permission to perform the external action before
    the browser click.  If the process dies or the browser response is
    uncertain, later runs reconcile through an authoritative source-key search
    and refuse a second create action when absence cannot be proven safely.
    """

    def __init__(
        self,
        reader: Any,
        browser: EzlynxTaskBrowserPort,
        ledger_path: str | Path,
    ) -> None:
        self.reader = reader
        self.browser = browser
        self.ledger = DurableWorkLedger(ledger_path)

    def lookup_candidates(self, identifiers: Identifiers) -> ReadResult:
        require_test()
        return self.reader.lookup_candidates(identifiers)

    def lookup_assignee(self, user_id: str) -> ReadResult:
        require_test()
        return self.reader.lookup_assignee(user_id)

    def lookup_certificates_team_member(self, user_id: str) -> ReadResult:
        require_test()
        lookup = getattr(self.reader, "lookup_certificates_team_member", None)
        if not callable(lookup):
            raise IntakeHold("Certificates team membership mapping is not connected")
        return lookup(user_id)

    def lookup_hello_owner(
        self, applicant_id: str, policy_id: str, request_type: str
    ) -> ReadResult:
        require_test()
        lookup = getattr(self.reader, "lookup_hello_owner", None)
        if not callable(lookup):
            raise IntakeHold("Hello ownership mapping is not connected")
        return lookup(applicant_id, policy_id, request_type)

    def lookup_pilot_assignee(self, assignment_key: str) -> ReadResult:
        require_test()
        lookup = getattr(self.reader, "lookup_pilot_assignee", None)
        if not callable(lookup):
            raise IntakeHold("Pilot assignment mapping is not connected")
        return lookup(assignment_key)

    def find_source_tasks(self, source_key: str) -> ReadResult:
        require_test()
        if not isinstance(source_key, str) or not source_key.startswith("intake:"):
            raise IntakeHold("A stable intake source key is required")
        return self.browser.find_source_tasks(source_key)

    def find_related_work(
        self, applicant_id: str, policy_id: str, source: Any
    ) -> ReadResult:
        require_test()
        return self.browser.find_related_work(applicant_id, policy_id, source)

    def read_task(self, task_id: str) -> ReadResult:
        require_test()
        if not isinstance(task_id, str) or not task_id.strip():
            raise IntakeHold("A stable EZLynx task ID is required")
        return self.browser.read_task(task_id)

    def _reconcile(self, task: Mapping[str, Any]) -> str:
        rows = self.find_source_tasks(str(task["source_key"])).checked()
        if len(rows) != 1 or not task_matches(task, rows[0]):
            raise IntakeHold(
                "Intake task outcome is uncertain; reconcile the source key in EZLynx before retry"
            )
        task_id = str(rows[0].get("task_id") or "").strip()
        if not task_id:
            raise IntakeHold("Reconciled intake task has no stable EZLynx task ID")
        return task_id

    def create_task_once(self, task: Mapping[str, Any]) -> str:
        require_test()
        source_key = str(task.get("source_key") or "")
        if not source_key.startswith("intake:"):
            raise IntakeHold("Task creation requires the stable intake source key")
        if task.get("manual_upload_required") is not True:
            raise IntakeHold("Task creation requires the manual-upload obligation")

        existing = self.find_source_tasks(source_key).checked()
        if existing:
            return self._reconcile(task)

        self.ledger.reserve(NAMESPACE, source_key)
        state = self.ledger.get(NAMESPACE, source_key)
        if state["external_actions"]:
            task_id = self._reconcile(task)
            self.ledger.mark_verified(NAMESPACE, source_key)
            return task_id

        owner = "intake-task-" + uuid.uuid4().hex
        try:
            self.ledger.acquire(NAMESPACE, source_key, owner=owner)
            # Record before the click. A crash in the following instruction is
            # a permanent hold until authoritative reconciliation succeeds.
            self.ledger.record_external_action(NAMESPACE, source_key)
            self.browser.create_task(task)
            task_id = self._reconcile(task)
        except IntakeHold:
            self.ledger.mark_timeout_unknown(NAMESPACE, source_key)
            raise
        except Exception as exc:
            self.ledger.mark_timeout_unknown(NAMESPACE, source_key)
            if isinstance(exc, IdempotencyError):
                raise IntakeHold(str(exc)) from None
            raise IntakeHold(
                "EZLynx task write outcome is uncertain; reconcile before retry"
            ) from None

        self.ledger.mark_verified(NAMESPACE, source_key)
        return task_id
