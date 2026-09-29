"""Phase 1 adapter base types: the read-only contract every carrier honors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# The ONLY network writes a Phase 1 adapter may perform. Enforced by
# phase1_doc_pull.run_pilot before any adapter code runs.
READ_ONLY_ACTIONS = frozenset({"portal_login", "portal_download", "api_read"})


class TransientBrowserError(RuntimeError):
    """A browser failure worth retrying: navigation/action timeout,
    dropped connection, crashed target. Never a wrong-password or
    not-found failure — those are permanent."""


class SessionExpiredError(TransientBrowserError):
    """The portal's session died mid-flow (login screen reappeared).

    Adapters raise this when they observe the portal's login screen
    after having logged in. Handled by the runner with exactly one
    fresh-context re-login — never by blind retries on the dead
    session, so it is excluded from run_with_retries' transient set.
    """


@dataclass(frozen=True)
class PolicyRef:
    """One pilot policy handed to an adapter."""

    policy_number: str
    insured_name: str
    report: str  # "4246" | "4247" | "4744"
    doc_kind: str  # e.g. "audit_papers", "renewal_offer_or_bill"


@dataclass(frozen=True)
class AdapterSpec:
    """Static descriptor for one carrier adapter."""

    carrier_id: str
    carrier_name: str
    portal_url: str
    runtime: str  # "box" | "sandbox" | "api"
    runtime_reason: str
    username_env: str  # env var holding the Secret Manager ref, "" when n/a
    password_env: str  # env var holding the Secret Manager ref, "" when n/a
    doc_kind: str
    allowed_actions: frozenset = frozenset({"portal_login", "portal_download"})
    notes: tuple = ()


@dataclass
class DownloadResult:
    """Outcome of one adapter download attempt."""

    ok: bool
    file_path: Path | None = None  # local path of the downloaded document
    detail: str = ""  # human-readable, no secret values
    extra: dict = field(default_factory=dict)  # structured evidence (no secrets)


class BrowserPort(Protocol):
    """Minimal browser surface an adapter may use. Production: Playwright.

    Read-only by shape: there is no form-submit-to-bind, no send, no
    upload. ``download`` captures the file the browser receives after the
    adapter clicks the portal's own download/export control.

    Optional extensions (used by the runner when present, never
    required): ``screenshot(dest_path)`` saves a PNG; ``reset_log()``
    starts a fresh per-policy action log; ``action_log`` returns the
    timestamped steps so far (selectors only — never filled values).
    """

    def goto(self, url: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def wait_for_selector(self, selector: str, timeout_ms: int = 30000) -> None: ...
    def download(self, click_selector: str, dest_path: Path) -> Path:
        """Click ``click_selector`` and save the resulting download to ``dest_path``."""
        ...
    def close(self) -> None: ...


def new_browser(runtime: str) -> BrowserPort:
    """Production wiring point: return a Playwright-backed BrowserPort.

    Delegates to :mod:`robie_job_engine.phase1_browser` (lazy import so
    importing this module never requires playwright to be installed).
    """
    from ..phase1_browser import new_browser as _live_new_browser

    return _live_new_browser(runtime)


def record_only(*args: Any, **kwargs: Any) -> None:
    """Placeholder for steps a human must confirm before first live run."""
    raise NotImplementedError(
        "adapter navigation step is DESIGNED, not TEST VERIFIED: "
        "confirm against the live portal in a read-only smoke test first"
    )
