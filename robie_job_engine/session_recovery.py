#!/usr/bin/env python3
"""Recover a logged-out EZLynx browser session without human intervention.

On 2026-09-12 Carlo restated the core Robie bug: jobs are sent and nothing
happens — they die, or EZLynx is logged out. The engine's session preflight
(`session_preflight.py`) already proves the logged-out state, but its only
response was to fail the job outright ("ROBIE Blocker: EZLynx Session Logged
Out") and wait for a human. This module is the other half of that decision:
when the preflight proves LOGGED_OUT, attempt automatic re-authentication
first, using the same Secret Manager credential path as the 6 a.m. session
timer (`ezlynx_session.ensure_ezlynx_session`), driven through the local CDP
endpoint.

Design rules:
- Only the outcome is recorded. Credential values never enter job payloads,
  logs, checkpoints, or exception text (see `redact_exception`).
- A recovery that cannot complete never asks a human to lift a guard. It
  returns a named marker and the job fails closed exactly as before.
- `INTERACTIVE_AUTH_REQUIRED` (MFA / captcha) is a legitimate human step, not
  a guard to bypass. It is reported, not worked around.

`attempt_session_recovery` is synchronous and takes an optional browser
factory so it is testable without a browser, a network, or a box.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Mapping

from .secrets import redact_exception

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
CDP_URL_ENV_VAR = "ROBIE_BROWSER_CDP_URL"

# Outcome markers. None of these are security-guard refusals.
RECOVERED = "SESSION_RECOVERED"
INTERACTIVE_AUTH_REQUIRED = "INTERACTIVE_AUTH_REQUIRED"
RECOVERY_FAILED = "SESSION_RECOVERY_FAILED"
RECOVERY_ERROR = "SESSION_RECOVERY_ERROR"


def _cdp_url() -> str:
    return os.environ.get(CDP_URL_ENV_VAR, DEFAULT_CDP_URL)


def attempt_session_recovery(
    *,
    cdp_url: str | None = None,
    lock_timeout_seconds: float = 60.0,
    browser_factory: Callable[[str], Any] | None = None,
    session_ensurer: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Try to restore the EZLynx session. Never raises.

    Returns a dict with `recovered` (bool) and either `state` (the
    `SessionState` value, on success) or `reason` (a named marker, on
    failure). Safe to call from the engine's job path: every failure mode
    is captured and reported, never thrown.
    """
    from .ezlynx_session import (
        InteractiveAuthenticationRequired,
        PlaywrightEzlynxSession,
        SessionVerificationFailed,
        ensure_ezlynx_session,
    )
    from .ezlynx_session_lock import EzlynxSessionLockTimeout, exclusive_session

    url = cdp_url or _cdp_url()
    factory = browser_factory or PlaywrightEzlynxSession
    ensurer = session_ensurer or ensure_ezlynx_session

    try:
        with exclusive_session(timeout_seconds=lock_timeout_seconds):
            browser = factory(url)
            try:
                state = ensurer(browser)
            finally:
                close = getattr(browser, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
    except InteractiveAuthenticationRequired as exc:
        return {
            "recovered": False,
            "reason": f"{INTERACTIVE_AUTH_REQUIRED}: {redact_exception(exc)}",
        }
    except SessionVerificationFailed as exc:
        return {
            "recovered": False,
            "reason": f"{RECOVERY_FAILED}: {redact_exception(exc)}",
        }
    except EzlynxSessionLockTimeout:
        return {
            "recovered": False,
            "reason": f"{RECOVERY_ERROR}: EZLYNX_SESSION_LOCK_TIMEOUT",
        }
    except Exception as exc:  # noqa: BLE001 - recovery must never raise
        return {
            "recovered": False,
            "reason": f"{RECOVERY_ERROR}: {redact_exception(exc)}",
        }
    return {"recovered": True, "state": state.value, "marker": RECOVERED}


def recovery_summary(result: Mapping[str, Any] | None) -> str:
    """One-line human summary of a recovery attempt for reports."""
    result = result or {}
    if result.get("recovered"):
        return f"EZLynx session recovered automatically (state={result.get('state')})."
    return f"Automatic EZLynx session recovery failed: {result.get('reason') or 'unknown'}."
