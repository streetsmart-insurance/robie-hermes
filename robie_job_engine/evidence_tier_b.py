"""Tier B browser evidence driver.

Tier A evidence comes from the EZLynx API (fast, structured). Tier B is the
fallback for fields the API cannot see (coverage limits, deductibles, and
anything else only rendered on the policy page): a fresh read of the EZLynx
policy page through a browser session.

This module owns the session-lock lifecycle around that read. The rules,
learned the hard way:

- The EZLynx session lock (``ezlynx-lock.sh``) MUST be held continuously
  from the first browser navigation through the final verification
  read-back, with a heartbeat at least every 5 minutes. A running
  heartbeat with no held slot protects nothing (2026-09-18 lesson).
- Browser tasks share ONE Chromium profile, so the holder string always
  names the EZLynx login in use -- a stuck slot must be attributable.
- Stale slots (no heartbeat for 10 minutes) may be taken over by the lock
  script; the driver never touches another holder's slot.

The actual page read is an injected ``reader`` callable::

    reader(applicant_id: str, policy_number: str, fields: list[str])
        -> dict[field_name, observed_value]

In production on the box this is the Playwright page-read function; in the
pilot and in tests it is a fake. What this module proves -- and what its
tests pin -- is the lock discipline around the read, not DOM parsing.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

DEFAULT_LOCK_SCRIPT = str(
    Path.home() / "workspace" / "robie-ops" / "ezlynx-session-lock"
    / "ezlynx-lock.sh"
)

# Heartbeat cadence: inside the 5-minute standing rule, well inside the
# 10-minute stale-takeover window.
DEFAULT_HEARTBEAT_INTERVAL = 240


class TierBLockError(RuntimeError):
    """The EZLynx session lock could not be acquired."""


class TierBReadError(RuntimeError):
    """The browser read returned something unusable."""


Reader = Callable[[str, str, Sequence[str]], Mapping[str, Any]]


def _run_lock_script(script: str, *args: str, timeout: int = 120) -> str:
    proc = subprocess.run(
        [script, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise TierBLockError(
            f"ezlynx-lock.sh {' '.join(args)} failed "
            f"(exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


class TierBDriver:
    """Holds an EZLynx session-lock slot around browser evidence reads.

    Use as a context manager::

        with TierBDriver(login="SSRobie", reader=page_reader) as driver:
            observed = driver.fetch(applicant_id, policy_number, fields)

    The slot is acquired on entry (blocking up to ``acquire_timeout``),
    heartbeated every ``heartbeat_interval`` seconds on a daemon thread,
    and released on exit -- including when the read raises.
    """

    def __init__(
        self,
        *,
        login: str,
        reader: Reader,
        lock_script: str = DEFAULT_LOCK_SCRIPT,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        acquire_timeout: int = 600,
        purpose: str = "tier-b evidence read",
    ) -> None:
        if not login or not login.strip():
            raise ValueError("TierBDriver requires the EZLynx login in use")
        if not callable(reader):
            raise ValueError("TierBDriver requires a reader callable")
        self.login = login.strip()
        self.reader = reader
        self.lock_script = lock_script
        self.heartbeat_interval = heartbeat_interval
        self.acquire_timeout = acquire_timeout
        self.holder = f"tier-b evidence driver ({purpose}): {self.login}"
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._held = False

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "TierBDriver":
        _run_lock_script(
            self.lock_script,
            "acquire",
            self.holder,
            str(self.acquire_timeout),
            timeout=self.acquire_timeout + 60,
        )
        self._held = True
        self._stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="tier-b-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        try:
            _run_lock_script(self.lock_script, "release", self.holder)
        finally:
            self._held = False
        # Never swallow the read's exception.
        return None

    # -- reads ------------------------------------------------------------

    @property
    def lock_held(self) -> bool:
        return self._held

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            try:
                _run_lock_script(self.lock_script, "heartbeat", self.holder)
            except Exception:
                # A failed heartbeat must not kill the read; the lock
                # script's stale-takeover window (10 min) is the backstop,
                # and the next heartbeat retries.
                continue

    def fetch(
        self,
        applicant_id: str,
        policy_number: str,
        fields: Sequence[str],
    ) -> dict[str, Any]:
        """Read destination fields through the browser. Lock must be held."""
        if not self._held:
            raise TierBLockError(
                "cannot Tier-B read without a held lock slot; "
                "use the driver as a context manager"
            )
        observed = self.reader(str(applicant_id), str(policy_number), list(fields))
        if not isinstance(observed, Mapping):
            raise TierBReadError(
                "browser reader returned unusable shape: "
                f"{type(observed).__name__}"
            )
        return dict(observed)


def read_for_evidence(
    driver: TierBDriver,
    applicant_id: str,
    policy_number: str,
    fields: Sequence[str],
) -> dict[str, Any]:
    """Fetch Tier B fields in the shape ``run_evidence_check`` expects."""
    return driver.fetch(applicant_id, policy_number, fields)
