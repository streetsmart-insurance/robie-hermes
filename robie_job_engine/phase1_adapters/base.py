"""Phase 1 adapter base types: the read-only contract every carrier honors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


# The ONLY network writes a Phase 1 adapter may perform. Enforced by
# phase1_doc_pull.run_pilot before any adapter code runs.
READ_ONLY_ACTIONS = frozenset({"portal_login", "portal_download", "api_read"})


# Distinct reason recorded when a login never reaches the authenticated
# state. Kept as a module constant so the runner, the adapters, and the
# tests all use the identical string — distinguishable at a glance from
# a selector failure or a transient timeout.
LOGIN_STALL_REASON = (
    "login did not complete — possible MFA, changed login page, or bad credentials"
)


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


class LoginStalledError(RuntimeError):
    """The login flow never reached the authenticated state within the
    login timeout — possible MFA challenge, changed login page, or bad
    credentials.

    Deliberately NOT transient and NOT a SessionExpiredError: retrying
    a stalled login is pointless (an MFA page will still be there) and
    risks account lockout. The runner records the policy ``blocked``
    with LOGIN_STALL_REASON, saves the screenshot + action log, and
    moves to the next policy.
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
    # Optional CSS selector present ONLY while authenticated (e.g. a
    # logout link or account menu). The runner probes it before starting
    # work on each policy on a reused session; an absent indicator means
    # a silent mid-run logout and raises SessionExpiredError so the
    # exactly-once re-login engages. "" = not declared: the runner keeps
    # the old behavior and marks the evidence session_probe "unavailable".
    # UNVERIFIED until a read-only smoke test confirms the selector.
    logged_in_indicator: str = ""
    allowed_actions: frozenset = frozenset({"portal_login", "portal_download"})
    notes: tuple = ()


def assert_logged_in(browser: BrowserPort, spec: AdapterSpec) -> None:
    """Raise LoginStalledError unless the adapter's login indicator is present.

    Called at the end of an adapter's login steps: the authenticated
    state is not "reached" until the adapter's own declared indicator
    says so. No indicator declared, or the port cannot probe (the
    ``has_selector`` extension is optional): nothing further to assert —
    the post-login wait in the login steps already passed.
    """
    indicator = (spec.logged_in_indicator or "").strip()
    if not indicator:
        return
    probe = getattr(browser, "has_selector", None)
    if not callable(probe):
        return
    if not probe(indicator):
        raise LoginStalledError(LOGIN_STALL_REASON)


def login_or_stall(
    spec: AdapterSpec,
    browser: BrowserPort,
    login_steps: Callable[[], None],
) -> None:
    """Run an adapter's login steps; any failure becomes LoginStalledError.

    A login that does not reach the authenticated state is never
    retried — not by run_with_retries (LoginStalledError is not
    transient) and not by the session re-login path (that is for
    sessions that die mid-flow, not for logins that never complete).
    """
    try:
        login_steps()
    except LoginStalledError:
        raise
    except Exception as exc:
        raise LoginStalledError(LOGIN_STALL_REASON) from exc
    assert_logged_in(browser, spec)


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
    timestamped steps so far (selectors only — never filled values);
    ``has_selector(selector, timeout_ms=5000)`` returns True/False for a
    quiet presence probe (used for the session-expiry check; never
    raises).
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
