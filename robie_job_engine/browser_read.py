from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from .chat_policy import forbidden_tool_request
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult


class BrowserReadPort(Protocol):
    def read_fresh(self, locator: dict[str, Any]) -> dict[str, Any]:
        """Independently open the destination and return server-backed state."""


class BoundedBrowserReadWorker:
    """One browser-only read. No mutation, terminal, or code execution."""

    def __init__(self, port: BrowserReadPort) -> None:
        self.port = port

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = job["payload"]
        forbidden = forbidden_tool_request(payload, str(payload.get("text") or ""))
        if forbidden:
            return WorkerResult(
                False, "browser.read", {}, retryable=False, error=forbidden
            )
        locator = dict(payload.get("locator") or {})
        if not locator.get("url") and not locator.get("title"):
            return WorkerResult(
                False,
                "browser.read",
                {},
                retryable=False,
                error="browser read requires a destination locator",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        try:
            snapshot = self.port.read_fresh(locator)
        except Exception as exc:
            return WorkerResult(
                False,
                "browser.read",
                locator,
                retryable=True,
                error=f"browser destination is not available: {type(exc).__name__}: {exc}",
                hold_status=JobStatus.WAITING,
            )
        return WorkerResult(
            True,
            "browser.read",
            locator,
            {"snapshot": snapshot, "idempotency_key": idempotency_key},
        )


class BrowserReadVerifier:
    """Re-read the live page. Worker snapshots are never treated as proof."""

    def __init__(self, port: BrowserReadPort) -> None:
        self.port = port

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        locator = dict(action.get("destination") or job["payload"].get("locator") or {})
        expected = dict(job["payload"].get("expected") or {})
        if not expected:
            expected = {key: locator[key] for key in ("url", "title") if key in locator}
        try:
            observed = self.port.read_fresh(locator)
        except Exception as exc:
            evidence = VerificationEvidence(
                method="FRESH_BROWSER_READBACK",
                source="browser-destination",
                expected=expected,
                observed={"error": f"{type(exc).__name__}: {exc}"},
                authoritative=True,
                captured_at=datetime.now(timezone.utc).isoformat(),
                locator=locator.get("url") or locator.get("title"),
            )
            return VerificationResult(
                False,
                evidence,
                retryable=True,
                error=f"{type(exc).__name__}: {exc}",
                hold_status=JobStatus.WAITING,
            )
        evidence = VerificationEvidence(
            method="FRESH_BROWSER_READBACK",
            source="browser-destination",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=locator.get("url") or locator.get("title"),
        )
        verified = all(observed.get(key) == value for key, value in expected.items())
        return VerificationResult(
            verified,
            evidence,
            retryable=not verified,
            error=None if verified else "fresh browser state does not match",
        )
