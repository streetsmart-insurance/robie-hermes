"""Run inbox emails with a small pool instead of strictly one at a time.

The watcher still creates one durable job per email. Up to
``ROBIE_EMAIL_WORKER_CONCURRENCY`` jobs run together (default 2).
EZLynx-writing jobs take the existing session lock so only one of them
drives the browser. Questions do not take that lock.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any, Callable, Iterable

from .answer_only import is_informational_ask

logger = logging.getLogger("robie.email_dispatch")

DEFAULT_EMAIL_WORKERS = 2
_MAX_EMAIL_WORKERS = 8


def email_worker_concurrency(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = str(env.get("ROBIE_EMAIL_WORKER_CONCURRENCY", str(DEFAULT_EMAIL_WORKERS))).strip()
    try:
        value = int(raw or DEFAULT_EMAIL_WORKERS)
    except ValueError:
        return DEFAULT_EMAIL_WORKERS
    return max(1, min(value, _MAX_EMAIL_WORKERS))


def email_job_writes_ezlynx(text: str) -> bool:
    """Questions do not need the browser lock. Everything else might write."""
    return not is_informational_ask(text)


def ezlynx_write_session(text: str):
    """Serialize EZLynx writers on the existing session lock.

    When the lock directory cannot be created (unit tests off the VM),
    continue without it. On the VM the directory exists and the lock is held.
    """
    if not email_job_writes_ezlynx(text):
        return nullcontext()
    from .ezlynx_session_lock import _lock_path, exclusive_session

    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        if os.environ.get("ROBIE_EZLYNX_SESSION_LOCK"):
            raise
        logger.warning(
            "EZLynx session lock directory is not available at %s; "
            "this email job will not wait on the lock.",
            path,
        )
        return nullcontext()
    return exclusive_session()


def run_email_batch(
    items: Iterable[dict[str, Any]],
    worker: Callable[[dict[str, Any]], Any],
    *,
    concurrency: int | None = None,
    session_for: Callable[[str], Any] | None = None,
    text_of: Callable[[dict[str, Any]], str] | None = None,
) -> list[tuple[dict[str, Any], Any]]:
    """Start the batch together, bounded by ``concurrency``.

    The inbox scan returns as soon as the jobs are submitted. This function
    waits for the batch so a one-shot timer still finishes the work.
    """
    queued = list(items)
    if not queued:
        return []
    limit = email_worker_concurrency() if concurrency is None else max(1, int(concurrency))
    open_session = session_for or ezlynx_write_session
    read_text = text_of or (lambda item: str(item.get("text") or item.get("body") or ""))

    def _run(item: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        with open_session(read_text(item)):
            return item, worker(item)

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="robie-email") as pool:
        return list(pool.map(_run, queued))
