"""Park orphan RUNNING/VERIFYING Jobs that hold no worker lease.

Same class as Chat job c282de98: status looks active, ``lease_owner`` is
empty, nothing is driving the shared Chrome. The JE-KILL inventory gate
ignores these rows; this sweeper parks them so they stop looking live.

Test first; Production later via the same scheduler tick once the release
is promoted. Never clears a lease under a live ``lease_owner``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .models import JobStatus
from .store import JobStore, canonical_json

logger = logging.getLogger(__name__)

CHECKPOINT = "orphan_unleased_sweep"
ACTIVE_STATUSES = (JobStatus.RUNNING.value, JobStatus.VERIFYING.value)
DEFAULT_AGE_SECONDS = 3600


def orphan_unleased_age_seconds() -> int:
    raw = os.environ.get("ROBIE_ORPHAN_UNLEASED_SECONDS", str(DEFAULT_AGE_SECONDS))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("ROBIE_ORPHAN_UNLEASED_SECONDS must be a positive int") from exc
    if value < 1:
        raise ValueError("ROBIE_ORPHAN_UNLEASED_SECONDS must be a positive int")
    return value


def park_orphan_unleased_jobs(
    store: JobStore,
    *,
    older_than_seconds: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Fail-close unleased RUNNING/VERIFYING rows older than the age gate.

    Returns one dict per parked job: ``{id, status, action_type}``.
    """
    age = (
        older_than_seconds
        if older_than_seconds is not None
        else orphan_unleased_age_seconds()
    )
    if age < 1:
        raise ValueError("orphan unleased age must be positive")
    at = now or datetime.now(timezone.utc)
    cutoff = (at - timedelta(seconds=age)).isoformat()
    stamp = at.isoformat()
    reason = (
        f"parked by orphan_unleased_sweeper: {ACTIVE_STATUSES[0]}/"
        f"{ACTIVE_STATUSES[1]} with empty lease_owner older than "
        f"{age}s (same class as c282de98)"
    )
    parked: list[dict[str, str]] = []
    with store.transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, action_type FROM jobs
               WHERE status IN (?, ?)
                 AND (lease_owner IS NULL OR TRIM(lease_owner)='')
                 AND updated_at<=?""",
            (*ACTIVE_STATUSES, cutoff),
        ).fetchall()
        for row in rows:
            job_id = str(row["id"])
            conn.execute(
                """UPDATE jobs SET status=?, last_error=?, next_wakeup_at=NULL,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?,
                   completed_at=COALESCE(completed_at, ?)
                   WHERE id=?""",
                (JobStatus.FAILED.value, reason, stamp, stamp, job_id),
            )
            conn.execute(
                """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(job_id,kind) DO UPDATE SET
                   data_json=excluded.data_json, created_at=excluded.created_at""",
                (
                    job_id,
                    CHECKPOINT,
                    canonical_json(
                        {
                            "reason": reason,
                            "prior_status": row["status"],
                            "cutoff": cutoff,
                            "age_seconds": age,
                        }
                    ),
                    stamp,
                ),
            )
            parked.append(
                {
                    "id": job_id,
                    "status": str(row["status"]),
                    "action_type": str(row["action_type"] or ""),
                }
            )
    return parked


def format_orphan_alert(parked: list[dict[str, str]], *, environment: str) -> str:
    lines = [
        f"ROBIE orphan unleased sweeper ({environment}): "
        f"parked {len(parked)} RUNNING/VERIFYING Job(s) with no lease_owner."
    ]
    for item in parked[:12]:
        lines.append(
            f"- {item['id']} was {item['status']} ({item['action_type'] or 'unknown'})"
        )
    if len(parked) > 12:
        lines.append(f"- …and {len(parked) - 12} more")
    lines.append(
        "These rows cannot drive Chrome. Investigate why they stayed active "
        "without a lease; do not clear a lease under a live worker."
    )
    return "\n".join(lines)


def _emit_ops_alert(text: str, *, poster: Callable[..., Any] | None = None) -> bool:
    space = (
        os.environ.get("ROBIE_ORPHAN_SWEEP_ALERT_SPACE")
        or os.environ.get("ROBIE_OPS_CHAT_SPACE")
        or os.environ.get("ROBIE_LOGIN_SECRET_ALERT_SPACE")
        or ""
    ).strip()
    if not space.startswith("spaces/"):
        return False
    send = poster
    if send is None:
        from .chat_app_post import post_as_chat_app

        send = post_as_chat_app
    try:
        send(space, text)
        return True
    except Exception:
        logger.exception("orphan unleased Chat alert failed")
        return False


def run_orphan_unleased_sweep(
    db_path: str,
    *,
    older_than_seconds: int | None = None,
    now: datetime | None = None,
    poster: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Park orphans, notify each Chat job thread, and post one ops alert."""
    store = JobStore(db_path)
    parked = park_orphan_unleased_jobs(
        store, older_than_seconds=older_than_seconds, now=now
    )
    environment = (os.environ.get("ROBIE_ENV") or "UNKNOWN").strip() or "UNKNOWN"
    chat_notices = 0
    if parked:
        from .chat_guard import notify_terminal_chat_job

        for item in parked:
            if item["action_type"] != "hermes.google_chat_task":
                continue
            try:
                if notify_terminal_chat_job(db_path, item["id"], poster=poster):
                    chat_notices += 1
            except Exception:
                logger.exception("orphan Chat notify failed job=%s", item["id"])
        alert = format_orphan_alert(parked, environment=environment)
        logger.warning("%s", alert.replace("\n", " | "))
        ops_posted = _emit_ops_alert(alert, poster=poster)
    else:
        alert = ""
        ops_posted = False
    return {
        "parked": parked,
        "parked_count": len(parked),
        "chat_notices": chat_notices,
        "ops_alert_posted": ops_posted,
        "alert_text": alert,
        "environment": environment,
    }
