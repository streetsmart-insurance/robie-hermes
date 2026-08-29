from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from .chat_policy import forbidden_tool_request
from .models import (
    JobStatus,
    ReconciliationOutcome,
    ReconciliationResult,
    VerificationEvidence,
    VerificationResult,
    WorkerResult,
)


EZLYNX_REQUIRED_FIELDS = {
    "ezlynx.reassign": ("resource_id", "assignee_id", "assignee_name"),
    "ezlynx.move_document": (
        "document_id",
        "account_id",
        "destination_id",
        "destination_name",
    ),
    "ezlynx.apply_label": ("resource_id", "account_id", "label_id", "label"),
}


class EzlynxBrowserPort(Protocol):
    def exact_option(self, *, stable_id: str, exact_text: str, scope: Any | None = None) -> Any: ...
    def click(self, target: Any) -> None: ...
    def wait_interactable(self, *, role: str, name: str, timeout_ms: int, scope: Any | None = None) -> Any: ...
    def wait_frame_interactable(
        self,
        *,
        src_contains: str,
        heading: str,
        button: str,
        timeout_ms: int,
    ) -> Any: ...
    def fill_like_user(self, target: Any, value: str) -> None: ...
    def submit(self, *, idempotency_key: str) -> dict[str, Any]: ...


class EzlynxReadback(Protocol):
    def api_state(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any] | None: ...
    def fresh_page_state(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any]: ...


def _matches(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    return all(observed.get(key) == value for key, value in expected.items())


def _missing_ezlynx_fields(action: str, payload: dict[str, Any]) -> list[str]:
    required = EZLYNX_REQUIRED_FIELDS.get(action) or ()
    return [key for key in required if not payload.get(key)]


def _ezlynx_destination(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "ezlynx.reassign":
        return {
            "resource_id": payload["resource_id"],
            "assignment_field": payload.get("assignment_field", "Assigned Producer"),
            "assignee_id": payload["assignee_id"],
            "assignee_name": payload["assignee_name"],
        }
    if action == "ezlynx.move_document":
        return {
            "resource_id": payload["document_id"],
            "account_id": payload["account_id"],
            "document_name": payload.get("document_name"),
            "destination_id": payload["destination_id"],
            "destination_name": payload["destination_name"],
        }
    if action == "ezlynx.apply_label":
        return {
            "resource_id": payload["resource_id"],
            "account_id": payload["account_id"],
            "document_name": payload.get("document_name"),
            "label_id": payload["label_id"],
            "label": payload["label"],
        }
    return {}


class MemoryEzlynxDestination:
    """In-memory EZLynx stand-in. Never opens a live EZLynx session."""

    def __init__(self) -> None:
        from .runtime_env import forbid_memory_destination

        forbid_memory_destination("MemoryEzlynxDestination")
        self._state: dict[tuple[str, str], dict[str, Any]] = {}
        self.unavailable = False
        self.writes = 0

    def write(self, action_type: str, destination: dict[str, Any]) -> None:
        resource_id = str(destination.get("resource_id") or "")
        self._state[(action_type, resource_id)] = json.loads(json.dumps(destination))
        self.writes += 1

    def api_state(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any] | None:
        if self.unavailable:
            return None
        return self._read(action_type, expected)

    def fresh_page_state(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any]:
        if self.unavailable:
            raise RuntimeError("EZLynx destination is not available")
        observed = self._read(action_type, expected)
        if observed is None:
            return {}
        return json.loads(json.dumps(observed))

    def _read(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any] | None:
        resource_id = str(expected.get("resource_id") or "")
        item = self._state.get((action_type, resource_id))
        if item is None:
            return None
        return json.loads(json.dumps(item))


class BoundedEzlynxWorker:
    """Mutate a fake EZLynx destination only. Never opens the live host."""

    def __init__(self, destination: MemoryEzlynxDestination) -> None:
        self.destination = destination

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = dict(job["payload"])
        action = job["action_type"]
        forbidden = forbidden_tool_request(payload, str(payload.get("text") or ""))
        if forbidden:
            return WorkerResult(False, action, {}, retryable=False, error=forbidden)
        if action not in EZLYNX_REQUIRED_FIELDS:
            return WorkerResult(
                False, action, {}, retryable=False, error=f"unsupported action: {action}"
            )
        missing = _missing_ezlynx_fields(action, payload)
        if missing:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error=f"EZLynx request is missing fields: {', '.join(missing)}",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        destination = _ezlynx_destination(action, payload)
        self.destination.write(action, destination)
        return WorkerResult(
            True, action, destination, {"idempotency_key": idempotency_key}
        )


class EzlynxDestinationVerifier:
    """Prefer API/network read-back; otherwise verify after a new page load/session."""

    def __init__(self, readback: EzlynxReadback):
        self.readback = readback

    def reconcile(
        self,
        job: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ReconciliationResult:
        """Authoritatively read destination state before an interrupted retry."""
        expected = _ezlynx_destination(job["action_type"], dict(job["payload"]))
        if not expected:
            return ReconciliationResult(
                ReconciliationOutcome.UNKNOWN,
                job["action_type"],
                error="EZLynx reconciliation destination is missing",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        method = "EZLYNX_API_READBACK"
        try:
            observed = self.readback.api_state(job["action_type"], expected)
            if observed is None:
                observed = self.readback.fresh_page_state(job["action_type"], expected)
                method = "FRESH_PAGE_READBACK"
        except Exception as exc:
            return ReconciliationResult(
                ReconciliationOutcome.UNKNOWN,
                job["action_type"],
                destination=expected,
                detail={"method": method},
                error=f"EZLynx reconciliation is unavailable: {type(exc).__name__}: {exc}",
            )
        if not observed:
            return ReconciliationResult(
                ReconciliationOutcome.NOT_APPLIED,
                job["action_type"],
                destination=expected,
                detail={"method": method, "observed": {}},
                authoritative=True,
            )
        if _matches(expected, observed):
            return ReconciliationResult(
                ReconciliationOutcome.APPLIED,
                job["action_type"],
                destination=expected,
                detail={"method": method, "observed": observed},
                authoritative=True,
            )
        return ReconciliationResult(
            ReconciliationOutcome.UNKNOWN,
            job["action_type"],
            destination=expected,
            detail={"method": method, "observed": observed},
            authoritative=True,
            error="EZLynx destination exists but does not match the intended consequence",
            hold_status=JobStatus.NEEDS_CLARIFICATION,
        )

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        expected = dict(action.get("destination") or {})
        locator = expected.get("resource_id")
        if not expected:
            evidence = VerificationEvidence(
                method="FRESH_PAGE_READBACK",
                source="ezlynx-reloaded-page",
                expected={},
                observed={},
                authoritative=True,
                captured_at=datetime.now(timezone.utc).isoformat(),
                locator=locator,
            )
            return VerificationResult(
                False,
                evidence,
                retryable=False,
                error="EZLynx destination locator is missing",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        method = "EZLYNX_API_READBACK"
        source = "ezlynx-api"
        try:
            observed = self.readback.api_state(job["action_type"], expected)
            if observed is None:
                observed = self.readback.fresh_page_state(job["action_type"], expected)
                method = "FRESH_PAGE_READBACK"
                source = "ezlynx-reloaded-page"
        except Exception as exc:
            evidence = VerificationEvidence(
                method="FRESH_PAGE_READBACK",
                source="ezlynx-reloaded-page",
                expected=expected,
                observed={"error": f"{type(exc).__name__}: {exc}"},
                authoritative=True,
                captured_at=datetime.now(timezone.utc).isoformat(),
                locator=locator,
            )
            return VerificationResult(
                False,
                evidence,
                retryable=True,
                error=f"EZLynx destination is not available: {type(exc).__name__}: {exc}",
                hold_status=JobStatus.WAITING,
            )
        evidence = VerificationEvidence(
            method=method,
            source=source,
            expected=expected,
            observed=observed or {},
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=locator,
        )
        if not observed:
            return VerificationResult(
                False,
                evidence,
                retryable=True,
                error="EZLynx destination is not available yet",
                hold_status=JobStatus.WAITING,
            )
        verified = _matches(expected, observed)
        return VerificationResult(
            verified,
            evidence,
            retryable=not verified,
            error=None if verified else "EZLynx destination state does not match",
        )


class HermesCuaEzlynxWorker:
    """Keeps Hermes/cua-driver while making the three fragile actions deterministic."""

    def __init__(self, browser: EzlynxBrowserPort):
        self.browser = browser

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = job["payload"]
        action = job["action_type"]
        forbidden = forbidden_tool_request(payload, str(payload.get("text") or ""))
        if forbidden:
            return WorkerResult(False, action, {}, retryable=False, error=forbidden)
        missing = _missing_ezlynx_fields(action, payload)
        if missing:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error=f"EZLynx request is missing fields: {', '.join(missing)}",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        if action == "ezlynx.reassign":
            return self._reassign(payload, idempotency_key)
        if action == "ezlynx.move_document":
            return self._move_document(payload, idempotency_key)
        if action == "ezlynx.apply_label":
            return self._apply_label(payload, idempotency_key)
        return WorkerResult(False, action, {}, retryable=False, error=f"unsupported action: {action}")

    def _reassign(self, payload: dict[str, Any], key: str) -> WorkerResult:
        # Stable ID wins; exact text is only a guarded fallback and must resolve uniquely.
        option = self.browser.exact_option(stable_id=payload["assignee_id"], exact_text=payload["assignee_name"])
        self.browser.click(option)
        receipt = self.browser.submit(idempotency_key=key)
        return WorkerResult(
            True,
            "ezlynx.reassign",
            {
                "resource_id": payload["resource_id"],
                "assignment_field": payload.get("assignment_field", "Assigned Producer"),
                "assignee_id": payload["assignee_id"],
                "assignee_name": payload["assignee_name"],
            },
            receipt,
        )

    def _move_document(self, payload: dict[str, Any], key: str) -> WorkerResult:
        self.browser.click(payload["move_control"])
        # The real EZLynx control is a cross-frame Angular dialog. The outer
        # "Move to..." shell can exist before its iframe has rendered, so wait
        # for the server-addressed frame, its destination heading, and enabled
        # Move button rather than sleeping or clicking the shell.
        frame = self.browser.wait_frame_interactable(
            src_contains=f"/Applicant/{payload['account_id']}/DocumentLibrary/MoveDocument",
            heading="Select the folder you would like to move your file to...",
            button="Move",
            timeout_ms=15_000,
        )
        destination = self.browser.exact_option(
            stable_id=payload["destination_id"],
            exact_text=payload["destination_name"],
            scope=frame,
        )
        self.browser.click(destination)
        move_button = self.browser.wait_interactable(role="button", name="Move", timeout_ms=5_000, scope=frame)
        self.browser.click(move_button)
        receipt = self.browser.submit(idempotency_key=key)
        return WorkerResult(
            True,
            "ezlynx.move_document",
            {
                "resource_id": payload["document_id"],
                "account_id": payload["account_id"],
                "document_name": payload["document_name"],
                "destination_id": payload["destination_id"],
                "destination_name": payload["destination_name"],
            },
            receipt,
        )

    def _apply_label(self, payload: dict[str, Any], key: str) -> WorkerResult:
        self.browser.click(payload["label_control"])
        control = self.browser.wait_interactable(role="textbox", name="Search Labels", timeout_ms=10_000)
        # User-like input plus selection fires Angular input/change/blur. Never
        # assign element.value: that changes the local DOM without persisting.
        self.browser.fill_like_user(control, payload["label"])
        option = self.browser.exact_option(stable_id=payload["label_id"], exact_text=payload["label"])
        self.browser.click(option)
        apply_button = self.browser.wait_interactable(role="button", name="Apply", timeout_ms=5_000)
        self.browser.click(apply_button)
        receipt = self.browser.submit(idempotency_key=key)
        return WorkerResult(
            True,
            "ezlynx.apply_label",
            {
                "resource_id": payload["resource_id"],
                "account_id": payload["account_id"],
                "document_name": payload["document_name"],
                "label_id": payload["label_id"],
                "label": payload["label"],
            },
            receipt,
        )
