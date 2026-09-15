"""Worker-unforgeable write markers (slice 4, step 1).

Every successful Playwright write intercepted by
``playwright_write_guard._wrap_write``, and every EZLynx / Ascend API mutation,
records one row in the ``write_markers`` table, attributed to the current job.

The worker never calls this module:

* the marker functions are not exposed in the Playwright exec scope, and
* the job id comes from the server-set ``ROBIE_JOB_ID`` environment — never
  from worker input.

Step 1 is inert by design: rows are written and nothing reads them yet.
Later steps build the verification tiers on ``writes_observed``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_JOBS_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"


def current_job_id() -> str | None:
    """Job id from the server-set environment. Never from worker input."""
    for key in ("ROBIE_JOB_ID", "ROBIE_CURRENT_JOB_ID", "JOB_ID"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return None


def resolve_jobs_db(db_path: str | Path | None = None) -> str:
    return str(db_path or os.environ.get("ROBIE_JOB_DB") or DEFAULT_JOBS_DB)


def record_write_marker(
    *,
    method: str,
    url: str,
    job_id: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """Insert one ``write_markers`` row. Never raises into the guarded path.

    Returns True when a row was written. Returns False — without raising —
    when there is no current job, no database file, an unknown job id, or the
    insert fails. A marker must never break the write it records.
    """
    resolved_id = (job_id or "").strip() or current_job_id()
    if not resolved_id:
        return False
    db = resolve_jobs_db(db_path)
    if not Path(db).is_file():
        return False
    try:
        from .playwright_observability import sanitize_tab_url
        from .store import JobStore

        store = JobStore(db)
        store.add_write_marker(
            resolved_id,
            method=str(method or "write"),
            url=sanitize_tab_url(url),
        )
        return True
    except KeyError:
        # Unknown job id: nothing to attribute the write to.
        return False
    except Exception:
        logger.exception("write marker insert failed job=%s", resolved_id)
        return False


def writes_observed(store: Any, job_id: str) -> int:
    """Count of worker-unforgeable writes recorded for a job."""
    return store.count_write_markers(job_id)


def list_write_markers(store: Any, job_id: str) -> list[dict[str, Any]]:
    """All write markers for a job, oldest first."""
    return store.list_write_markers(job_id)
