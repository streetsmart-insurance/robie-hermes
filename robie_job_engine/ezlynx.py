from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from .models import VerificationEvidence, VerificationResult, WorkerResult


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


class EzlynxDestinationVerifier:
    """Prefer API/network read-back; otherwise verify after a new page load/session."""

    def __init__(self, readback: EzlynxReadback):
        self.readback = readback

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        expected = dict(action["destination"])
        observed = self.readback.api_state(job["action_type"], expected)
        method = "EZLYNX_API_READBACK"
        source = "ezlynx-api"
        locator = expected.get("resource_id")
        if observed is None:
            observed = self.readback.fresh_page_state(job["action_type"], expected)
            method = "FRESH_PAGE_READBACK"
            source = "ezlynx-reloaded-page"
        evidence = VerificationEvidence(
            method=method,
            source=source,
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=locator,
        )
        verified = _matches(expected, observed)
        return VerificationResult(verified, evidence, retryable=not verified, error=None if verified else "EZLynx destination state does not match")


class HermesCuaEzlynxWorker:
    """Keeps Hermes/cua-driver while making the three fragile actions deterministic."""

    def __init__(self, browser: EzlynxBrowserPort):
        self.browser = browser

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = job["payload"]
        action = job["action_type"]
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
