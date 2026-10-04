"""Job Engine adapters for the Robie Call handler.

Connects PR #746's port protocols to PR #745's real Job Engine store.
The handler defines CallJobCheckpointPort and TaskStatusPort as protocols;
this module provides the production implementations backed by the Job
Engine's durable job rows.

The handler checkpoints under "robie-call:<task_id>"; the Job Engine owns
one durable job per EZLynx task under "ezlynx-task:<task_id>". The adapter
maps between them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _job_id_for_task(store: Any, task_id: str) -> Optional[str]:
    """Find the Job Engine job ID for an EZLynx task ID."""
    try:
        from .ezlynx_task_jobs import task_idempotency_key
        key = task_idempotency_key(task_id)
    except ImportError:
        # PR #745 not merged yet: fall back to the documented key scheme.
        key = f"ezlynx-task:{task_id.strip()}"
    with store.connect() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE idempotency_key=?", (key,)
        ).fetchone()
    if not row:
        return None
    try:
        return str(row["id"])
    except (KeyError, TypeError, IndexError):
        return str(row[0])


class JobEngineCheckpointAdapter:
    """CallJobCheckpointPort backed by the Job Engine's checkpoint table.

    Checkpoints are stored via store.checkpoint(job_id, kind, data) and
    retrieved via store.get_checkpoint(job_id, kind). The kind is
    "robie-call" and the task_id is embedded in the data for the handler's
    key scheme.
    """

    CHECKPOINT_KIND = "robie-call"

    def __init__(self, store: Any):
        self._store = store

    def get_checkpoint(self, key: str) -> Dict[str, Any]:
        """Get checkpoint for handler key "robie-call:<task_id>"."""
        # Extract task_id from the handler's key scheme
        task_id = key.split(":", 1)[1] if ":" in key else key
        job_id = _job_id_for_task(self._store, task_id)
        if not job_id:
            return {}
        try:
            data = self._store.get_checkpoint(job_id, self.CHECKPOINT_KIND)
            return dict(data) if data else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint read failed for task %s: %s", task_id, exc)
            return {}

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        """Save checkpoint for handler key "robie-call:<task_id>"."""
        task_id = key.split(":", 1)[1] if ":" in key else key
        job_id = _job_id_for_task(self._store, task_id)
        if not job_id:
            logger.warning(
                "no Job Engine job for task %s; checkpoint not saved", task_id
            )
            return
        try:
            self._store.checkpoint(job_id, self.CHECKPOINT_KIND, dict(value))
        except Exception as exc:  # noqa: BLE001
            # Swallowed checkpoint errors are a blocker per review — surface it.
            # We log loudly; the handler treats a failed checkpoint save as
            # a signal to reconcile, not to proceed blindly.
            logger.error(
                "checkpoint SAVE FAILED for task %s (job %s): %s. "
                "The handler must reconcile, not redial.",
                task_id, job_id, exc,
            )
            raise


class JobEngineTaskStatusAdapter:
    """TaskStatusPort backed by the Job Engine's job status.

    The handler asks "is this EZLynx task still open?" The Job Engine tracks
    the job lifecycle; a job in a terminal state (COMPLETE/FAILED/CANCELLED)
    means the task was already handled. We map job status to task openness.
    """

    def __init__(self, store: Any):
        self._store = store

    def is_task_open(self, task_id: str) -> Optional[bool]:
        """True if the task's job is not in a terminal state.

        Returns None when the job doesn't exist or status is unknown —
        the handler treats None as "proceed with caution" (not as closed).
        """
        from .models import TERMINAL_STATUSES, JobStatus

        job_id = _job_id_for_task(self._store, task_id)
        if not job_id:
            return None
        try:
            job = self._store.get_job(job_id)
            status = JobStatus(job.get("status", ""))
            return status not in TERMINAL_STATUSES
        except Exception as exc:  # noqa: BLE001
            logger.warning("task status check failed for %s: %s", task_id, exc)
            return None


def build_robie_call_ports(
    store: Any,
    *,
    phone_lookup: Any,
    bland: Any,
    discussion_client: Any,
    task_reassign: Any = None,
    recording_upload: Any = None,
    chat_alert: Any = None,
    transfer_lookup: Any = None,
    opt_out_store: Any = None,
    opt_in_store: Any = None,
    call_dedupe: Any = None,
) -> Any:
    """Build RobieCallPorts with real Job Engine checkpoint/status adapters.

    Use this in production wiring instead of the in-memory fallbacks.
    The handler's other ports (phone, Bland, discussion, reassign) are
    passed through unchanged.
    """
    # Import here to avoid circular imports at module load
    from .robie_call_handler import RobieCallPorts

    return RobieCallPorts(
        phone_lookup=phone_lookup,
        bland=bland,
        discussion_client=discussion_client,
        task_reassign=task_reassign,
        recording_upload=recording_upload,
        chat_alert=chat_alert,
        job_checkpoint=JobEngineCheckpointAdapter(store),
        task_status=JobEngineTaskStatusAdapter(store),
        transfer_lookup=transfer_lookup,
        opt_out_store=opt_out_store,
        opt_in_store=opt_in_store,
        call_dedupe=call_dedupe,
    )
