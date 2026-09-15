from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .chat_queue import DurableChatEventQueue
from .operations import OperationsStore
from .store import JobStore


def _next_daily(
    local_time: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    hour, minute = (int(part) for part in local_time.split(":", 1))
    zone = ZoneInfo(timezone_name)
    now = now.astimezone(zone) if now is not None else datetime.now(zone)
    candidate = datetime.combine(now.date(), time(hour=hour, minute=minute), tzinfo=zone)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat()


def _ensure_default_schedules(ops: OperationsStore) -> None:
    # Repo default is off. The hourly GitHub monitor
    # (.github/workflows/monitor-ezlynx-session.yml) is the intended healer.
    # A Production zip must not set ROBIE_ENABLE_EZLYNX_SESSION_REFRESH=1.
    # The live hermes-poc-01 scheduler unit still has that env =1 until a
    # later zip Carlo has not authorized. Repo default-off is not box-off.
    if os.environ.get("ROBIE_ENABLE_EZLYNX_SESSION_REFRESH", "0") != "1":
        return
    local_time = os.environ.get("ROBIE_EZLYNX_SESSION_REFRESH_LOCAL_TIME", "05:30")
    timezone_name = os.environ.get("ROBIE_EZLYNX_SESSION_REFRESH_TIMEZONE", "America/New_York")
    payload = {
        "worker": "session-refresh",
        "resource_id": "ezlynx:authenticated-browser-session",
        "profile_id": "robie-ezlynx-canonical-profile",
        "perform_timeout_seconds": 210,
        "_daily_local_time": local_time,
        "_schedule_timezone": timezone_name,
    }
    next_run_at = _next_daily(local_time, timezone_name)
    # Preserve the former table during migration, while scheduled_jobs is the
    # canonical 60-second poller source for all new executions.
    ops.ensure_schedule(
        "Daily EZLynx and Gmail session refresh",
        "ezlynx.session_refresh",
        payload,
        1440,
        next_run_at=next_run_at,
        reconcile=True,
    )
    cron = f"{int(local_time.split(':')[1])} {int(local_time.split(':')[0])} * * *"
    ops.ensure_recurring_job(
        "Daily EZLynx and Gmail session refresh",
        "ezlynx.session_refresh",
        payload,
        cron,
        timezone_name,
        next_run_at=next_run_at,
        target_ref="ezlynx:authenticated-browser-session",
        reconcile=True,
    )


def run_once(db_path: str) -> dict[str, int]:
    jobs = JobStore(db_path)
    ops = OperationsStore(db_path)
    _ensure_default_schedules(ops)
    woke = jobs.wake_due()
    try:
        from .login_secret_health import maybe_periodic_login_secret_check

        maybe_periodic_login_secret_check(db_path)
    except Exception:
        pass
    orphaned_chat_jobs = jobs.fail_orphaned_chat_jobs()
    if orphaned_chat_jobs:
        from .chat_guard import notify_terminal_chat_job
        from .recording import RecordingManager

        for orphan_id in orphaned_chat_jobs:
            try:
                notify_terminal_chat_job(db_path, orphan_id)
                RecordingManager(db_path).release_local_after_audit(orphan_id)
            except Exception:
                pass
    expired_contexts = DurableChatEventQueue(db_path).expire_inactive_conversations(
        inactivity_minutes=int(os.environ.get("ROBIE_DM_CONTEXT_TTL_MINUTES", "120"))
    )
    created = 0
    from .chat_admin import next_cron_time

    for schedule in ops.claim_due_recurring_jobs():
        # The persisted occurrence timestamp is the idempotency boundary. It
        # stays stable across restarts and clock/hour boundaries until the
        # schedule has been durably advanced.
        key = f"schedule:{schedule['id']}:{schedule['next_run_at']}"
        payload = dict(schedule["parameters"])
        payload.setdefault("task_name", schedule["task_name"])
        payload.setdefault("db_path", db_path)
        job = jobs.create_job(schedule["action_type"], payload, idempotency_key=key)
        ops.advance_recurring_job(
            schedule["id"],
            job["id"],
            next_run_at=next_cron_time(
                schedule["cron_spec"],
                schedule["timezone"],
                now=datetime.fromisoformat(schedule["next_run_at"]),
            ),
        )
        created += 1
    executed = 0
    from .request_routing import BOUNDED_ENGINE_ACTIONS
    from .test_runtime import maybe_run_bounded_job

    for job_id in jobs.list_runnable(BOUNDED_ENGINE_ACTIONS):
        if maybe_run_bounded_job(db_path, job_id):
            executed += 1
    synced = 0
    try:
        from .sheets_sync import sync_from_env
        synced = 1 if sync_from_env(db_path) is not None else 0
    except Exception as exc:
        # Scheduling remains durable if the dashboard is temporarily unavailable.
        print(json.dumps({"dashboard_sync_error": f"{type(exc).__name__}: {exc}"}))
    return {
        "woke": len(woke),
        "orphaned_chat_jobs": len(orphaned_chat_jobs),
        "expired_contexts": len(expired_contexts),
        "created": created,
        "executed": executed,
        "dashboard_synced": synced,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ROBIE durable scheduler tick")
    parser.add_argument("--db", default=os.environ.get(
        "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"))
    args = parser.parse_args()
    print(json.dumps(run_once(args.db), sort_keys=True))


if __name__ == "__main__":
    main()
