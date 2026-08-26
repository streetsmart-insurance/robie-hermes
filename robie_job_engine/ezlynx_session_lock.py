"""Exclusive access to Robie's canonical persistent EZLynx browser profile."""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class EzlynxSessionLockTimeout(RuntimeError):
    pass


def _lock_path() -> Path:
    return Path(
        os.environ.get(
            "ROBIE_EZLYNX_SESSION_LOCK",
            "/opt/streetsmart-hermes/robie-job-engine/data/ezlynx-session.lock",
        )
    )


@contextmanager
def exclusive_session(timeout_seconds: float = 210) -> Iterator[None]:
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EzlynxSessionLockTimeout("EZLYNX_SESSION_LOCK_TIMEOUT")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
