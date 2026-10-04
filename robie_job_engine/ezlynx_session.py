"""EZLynx session refresh (Job Engine) and Secret Manager login CLI.

This module holds two cooperating surfaces that previously lived on separate
PRs under the same filename:

- PR #10: bounded Job Engine worker/verifier (`EzlynxSessionRefreshWorker`,
  `EzlynxSessionVerifier`) that refresh the canonical Playwright profile.
- PR #7: Secret Manager-backed login CLI (`ensure_ezlynx_session`,
  `python -m robie_job_engine.ezlynx_session`) used by the weekday 6 a.m. session timer.

Neither path stores, prints, or rotates credential values.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol

from .ezlynx_session_lock import EzlynxSessionLockTimeout, exclusive_session
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .secret_manager import SecretAccessor, load_ezlynx_credentials
from .submission_audit import (
    LOGIN_HELPER,
    LOGIN_TIMEOUT_SECONDS,
    PYTHON,
    BoundedProcessError,
    _run_bounded,
    _safe_status,
)


RESOURCE_ID = "ezlynx:authenticated-browser-session"
PROFILE_ID = "robie-ezlynx-canonical-profile"
AUTH_CODES = {
    "NEEDS_AUTH",
    "MAILBOX_IDENTITY_MISMATCH",
    "ROBIE_MAILBOX_AUTH_REQUIRED",
    "MFA_CODE_NOT_FOUND",
    "MFA_INPUT_NOT_FOUND",
    "MFA_SUBMIT_NOT_FOUND",
    "MFA_NOT_ACCEPTED",
    "AUTH_STATE_REQUIRES_USERNAME_LOGIN",
}
LOGIN_URL = "https://app.ezlynx.com/auth/account/login"
APP_URL = "https://app.ezlynx.com/"
APP_WEB_URL = "https://app.ezlynx.com/web/"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_login_helper(*, verify_only: bool) -> str:
    command = [PYTHON, str(LOGIN_HELPER)]
    if verify_only:
        command.append("--verify-only")
    proc = _run_bounded(command, timeout=LOGIN_TIMEOUT_SECONDS)
    status = _safe_status(proc)
    if proc.returncode == 0 and status == "AUTHENTICATED":
        return status
    if status in AUTH_CODES:
        raise BoundedProcessError(status)
    raise BoundedProcessError("EZLYNX_LOGIN_HELPER_FAILED")


class EzlynxSessionRefreshWorker:
    def perform(self, job: dict, *, idempotency_key: str) -> WorkerResult:
        del idempotency_key
        try:
            with exclusive_session():
                run_login_helper(verify_only=False)
        except (BoundedProcessError, EzlynxSessionLockTimeout) as exc:
            code = getattr(exc, "code", "EZLYNX_SESSION_LOCK_TIMEOUT")
            return WorkerResult(
                False,
                "ezlynx.session_refresh",
                {},
                retryable=code == "EZLYNX_SESSION_LOCK_TIMEOUT",
                error=code,
                hold_status=JobStatus.NEEDS_AUTH if code in AUTH_CODES else None,
            )
        return WorkerResult(
            True,
            "ezlynx.session_refresh",
            {
                "resource_id": RESOURCE_ID,
                "profile_id": PROFILE_ID,
                "authenticated": True,
                "gmail_identity_verified": True,
            },
            detail={"engine": "playwright-cdp", "sensitive_recording_exempt": True},
        )


class EzlynxSessionVerifier:
    def verify(self, job: dict, action: dict) -> VerificationResult:
        del action
        expected = {
            "resource_id": str(job["payload"].get("resource_id") or RESOURCE_ID),
            "profile_id": str(job["payload"].get("profile_id") or PROFILE_ID),
            "authenticated": True,
            "gmail_identity_verified": True,
        }
        try:
            with exclusive_session():
                run_login_helper(verify_only=True)
            observed = dict(expected)
            error = None
            verified = True
            hold_status = None
        except (BoundedProcessError, EzlynxSessionLockTimeout) as exc:
            code = getattr(exc, "code", "EZLYNX_SESSION_LOCK_TIMEOUT")
            observed = {
                "resource_id": expected["resource_id"],
                "profile_id": expected["profile_id"],
                "authenticated": False,
                "gmail_identity_verified": code != "MAILBOX_IDENTITY_MISMATCH",
                "blocker": code,
            }
            error = code
            verified = False
            hold_status = JobStatus.NEEDS_AUTH if code in AUTH_CODES else None
        evidence = VerificationEvidence(
            method="EZLYNX_SESSION_FRESH_READBACK",
            source="canonical-playwright-profile-and-robie-gmail-api",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=_now(),
            locator=expected["resource_id"],
        )
        return VerificationResult(
            verified,
            evidence,
            retryable=error == "EZLYNX_SESSION_LOCK_TIMEOUT",
            error=error,
            hold_status=hold_status,
        )


class SessionState(str, Enum):
    SIGNED_IN = "SIGNED_IN"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    INTERACTIVE_AUTH_REQUIRED = "INTERACTIVE_AUTH_REQUIRED"
    UNVERIFIED = "UNVERIFIED"


class EzlynxSessionPort(Protocol):
    def state(self) -> SessionState: ...
    def login(self, username: str, password: str) -> SessionState: ...


class InteractiveAuthenticationRequired(RuntimeError):
    pass


class SessionVerificationFailed(RuntimeError):
    pass


# After credential submit the login URL can linger. One immediate state()
# read (about the old 4s load) false-fails SESSION_LOGGED_OUT.
POST_LOGIN_STATE_ATTEMPTS = 10
POST_LOGIN_STATE_DELAY_SECONDS = 3.0

# A logged-out browser stays on the app URL until the SPA client-redirects
# to /auth/account/login. domcontentloaded fires before that redirect, so
# one immediate read is UNVERIFIED (CLI exit 3) instead of logged out.
# Bound the wait: login URL, or a dashboard-ready shell, or give up.
NAVIGATION_SETTLE_ATTEMPTS = 8
NAVIGATION_SETTLE_DELAY_SECONDS = 1.0


def wait_for_post_login_state(
    read_state: Callable[[], SessionState],
    *,
    attempts: int = POST_LOGIN_STATE_ATTEMPTS,
    delay_seconds: float = POST_LOGIN_STATE_DELAY_SECONDS,
    sleeper: Callable[[float], None] | None = None,
) -> SessionState:
    """Poll until the page leaves the login URL, or the attempt budget ends.

    MFA and other non-login states return immediately. Only LOGIN_REQUIRED
    is retried, because that URL is what the preflight treats as logged out.
    """
    pause = sleeper or time.sleep
    state = read_state()
    total = max(1, int(attempts))
    for index in range(total - 1):
        if state is not SessionState.LOGIN_REQUIRED:
            return state
        pause(delay_seconds)
        state = read_state()
    return state


def classify_page_snapshot(
    url: str,
    body: str,
    *,
    internal_web_links: int,
    login_controls: int,
) -> SessionState | None:
    """Decide from one page read, or None while navigation has not settled.

    None means the URL is not the login page and the dashboard shell is not
    ready yet. Callers poll until a login URL, a dashboard-ready signal, an
    MFA challenge, or the attempt budget.
    """
    normalized_url = (url or "").casefold()
    normalized_body = (body or "").casefold()
    if (
        "captcha" in normalized_body
        or "verification code" in normalized_body
        or "multi-factor" in normalized_body
    ):
        return SessionState.INTERACTIVE_AUTH_REQUIRED
    if "limited to 2 active sessions" in normalized_body and (
        "log out the session" in normalized_body or "continue will log out" in normalized_body
    ):
        return SessionState.LOGIN_REQUIRED
    if "/auth/account/login" in normalized_url or "/auth/account/logout" in normalized_url:
        return SessionState.LOGIN_REQUIRED
    if authenticated_app_evidence(
        url,
        internal_web_links=internal_web_links,
        login_controls=login_controls,
    ):
        return SessionState.SIGNED_IN
    return None


def wait_for_settled_session(
    read_snapshot: Callable[[], dict[str, Any] | None],
    *,
    attempts: int = NAVIGATION_SETTLE_ATTEMPTS,
    delay_seconds: float = NAVIGATION_SETTLE_DELAY_SECONDS,
    sleeper: Callable[[float], None] | None = None,
) -> SessionState:
    """Poll until login, dashboard-ready, or the bounded attempt budget ends.

    A snapshot that cannot be read counts as not settled and is retried.
    The budget ending without a login URL or a dashboard signal is UNVERIFIED.
    """
    pause = sleeper or time.sleep
    total = max(1, int(attempts))
    for index in range(total):
        snapshot = read_snapshot()
        if snapshot is not None:
            state = classify_page_snapshot(
                str(snapshot.get("url") or ""),
                str(snapshot.get("body") or ""),
                internal_web_links=int(snapshot.get("internal_web_links") or 0),
                login_controls=int(snapshot.get("login_controls") or 0),
            )
            if state is not None:
                return state
        if index < total - 1:
            pause(delay_seconds)
    return SessionState.UNVERIFIED


def read_playwright_snapshot(page: Any) -> dict[str, Any] | None:
    """Read URL, body text, and the two counts state() decides from."""
    try:
        url = str(page.url or "")
    except Exception:
        return None
    try:
        body = page.locator("body").inner_text(timeout=3_000)
    except Exception:
        body = ""
    try:
        internal_web_links = page.locator('a[href*="/web/"]').count()
        login_controls = page.locator("#txtUserName,#txtPassword,#btnLogin").count()
    except Exception:
        return None
    return {
        "url": url,
        "body": str(body or ""),
        "internal_web_links": int(internal_web_links),
        "login_controls": int(login_controls),
    }


def authenticated_app_evidence(
    url: str,
    *,
    internal_web_links: int,
    login_controls: int,
) -> bool:
    """Fail closed unless fresh page state proves the authenticated web shell."""
    normalized = url.casefold().split("?", 1)[0]
    return (
        normalized.startswith("https://app.ezlynx.com/web/")
        and internal_web_links > 0
        and login_controls == 0
    )


class PlaywrightEzlynxSession:
    """Use the persistent Hermes Chrome profile through its local CDP endpoint."""

    def __init__(self, cdp_url: str):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("playwright is required for EZLynx session management") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = [page for context in self._browser.contexts for page in context.pages]
        ezlynx_pages = [page for page in pages if "ezlynx.com" in page.url.casefold()]
        self._page = (ezlynx_pages or pages or [self._context.new_page()])[0]

    def close(self) -> None:
        # Disconnect from CDP without closing the server-owned Chrome process.
        self._playwright.stop()

    def _ensure_page(self) -> None:
        if "ezlynx.com" not in self._page.url.casefold():
            self._page.goto(APP_URL, wait_until="domcontentloaded")

    def state(self) -> SessionState:
        """Judge the session after the SPA redirect has had time to finish.

        ``domcontentloaded`` on ``/web/`` returns before a logged-out browser
        is redirected to ``/auth/account/login``. Waiting for that URL, or for
        a dashboard-ready shell, is what distinguishes LOGIN_REQUIRED (logged
        out) from UNVERIFIED (exit 3).
        """
        try:
            self._page.goto(APP_WEB_URL, wait_until="domcontentloaded")
        except Exception:
            return SessionState.UNVERIFIED
        return wait_for_settled_session(
            lambda: read_playwright_snapshot(self._page),
            attempts=NAVIGATION_SETTLE_ATTEMPTS,
            delay_seconds=NAVIGATION_SETTLE_DELAY_SECONDS,
        )

    def _wait_for_login_form(self) -> None:
        """A blank login page has no form until it reloads."""
        field = self._page.locator("#txtUserName")
        try:
            field.wait_for(state="visible", timeout=8_000)
            return
        except Exception:
            self._page.reload(wait_until="domcontentloaded")
        field.wait_for(state="visible", timeout=15_000)

    def _continue_two_session(self, username: str, password: str) -> SessionState:
        """The login helper's one-Continue path. Same fill, same proof, same rc."""
        from ezlynx_login_bootstrap import (
            SESSION_LIMIT_LOGIN_FAILED,
            SESSION_LIMIT_REFUSED,
            handle_two_session_prompt,
        )
        from robie_job_engine.ezlynx_driver_gate import (
            EzlynxDriverGateRefused,
            require_driver_in,
        )

        code = handle_two_session_prompt(
            self._page,
            gate=require_driver_in,
            username=username,
            password=password,
        )
        if code == 0:
            return SessionState.SIGNED_IN
        if code == SESSION_LIMIT_REFUSED:
            raise EzlynxDriverGateRefused(
                "This environment does not hold the EZLynx driver, so Continue was not pressed."
            )
        if code == SESSION_LIMIT_LOGIN_FAILED:
            raise SessionVerificationFailed(
                "EZLynx did not confirm login after one Continue. "
                "The other session is still active."
            )
        raise SessionVerificationFailed(
            "EZLynx two-session prompt did not reach the app page."
        )

    def login(self, username: str, password: str) -> SessionState:
        from ezlynx_login_bootstrap import begin_login_run, session_limit_on

        begin_login_run()
        self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
        if session_limit_on(self._page):
            return self._continue_two_session(username, password)
        try:
            self._wait_for_login_form()
            self._page.locator("#txtUserName").fill(username)
            self._page.locator("#txtPassword").fill(password)
            self._page.locator("#btnLogin").click()
            self._page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception as exc:
            raise RuntimeError("EZLynx login interaction failed") from exc
        if session_limit_on(self._page):
            return self._continue_two_session(username, password)
        return wait_for_post_login_state(self.state)


def ensure_ezlynx_session(
    browser: EzlynxSessionPort,
    *,
    accessor: SecretAccessor | None = None,
) -> SessionState:
    state = browser.state()
    if state is SessionState.SIGNED_IN:
        return state
    if state is SessionState.INTERACTIVE_AUTH_REQUIRED:
        raise InteractiveAuthenticationRequired(
            "EZLynx requires an interactive verification step"
        )
    if state is SessionState.UNVERIFIED:
        raise SessionVerificationFailed(
            "fresh EZLynx page state did not prove an authenticated application shell"
        )
    credentials = load_ezlynx_credentials(accessor)
    state = browser.login(credentials.username, credentials.password)
    if state is SessionState.UNVERIFIED:
        raise SessionVerificationFailed(
            "credential submission completed but fresh destination state was not verified"
        )
    if state is not SessionState.SIGNED_IN:
        raise InteractiveAuthenticationRequired(
            "EZLynx requires interactive authentication after credential submission"
        )
    return state


def main() -> None:
    from .ezlynx_driver_gate import EzlynxDriverGateRefused

    parser = argparse.ArgumentParser(description="Ensure the Hermes EZLynx session is authenticated")
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("ROBIE_BROWSER_CDP_URL", "http://127.0.0.1:9222"),
    )
    args = parser.parse_args()
    try:
        with exclusive_session():
            browser = PlaywrightEzlynxSession(args.cdp_url)
            try:
                state = ensure_ezlynx_session(browser)
                print(json.dumps({"ezlynx_session": state.value}, sort_keys=True))
            finally:
                browser.close()
    except EzlynxDriverGateRefused as exc:
        print(json.dumps({"ezlynx_session": "DRIVER_NOT_IN", "error": str(exc)}, sort_keys=True))
        raise SystemExit(4)
    except EzlynxSessionLockTimeout:
        print(json.dumps({"ezlynx_session": "LOCK_TIMEOUT"}, sort_keys=True))
        raise SystemExit(5)
    except InteractiveAuthenticationRequired:
        print(json.dumps({"ezlynx_session": SessionState.INTERACTIVE_AUTH_REQUIRED.value}, sort_keys=True))
        raise SystemExit(2)
    except SessionVerificationFailed as exc:
        print(json.dumps({"ezlynx_session": SessionState.UNVERIFIED.value, "error": str(exc)}, sort_keys=True))
        raise SystemExit(3)


if __name__ == "__main__":
    main()
