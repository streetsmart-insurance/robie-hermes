"""Deterministic Google Chat administration with no browser execution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import TERMINAL_STATUSES, JobStatus
from .operations import OperationsStore
from .request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION
from .store import JobStore


UTC = timezone.utc
ADMIN_QUERY_PHRASES = {
    "show pending jobs": "pending",
    "what jobs are pending": "pending",
    "show active schedules": "schedules",
    "show schedules": "schedules",
    "show scheduled jobs": "schedules",
    "show my scheduled jobs": "schedules",
    "show me my scheduled jobs": "schedules",
    "show me all my scheduled jobs": "schedules",
    "show me all my currently scheduled jobs": "schedules",
    "what is scheduled": "schedules",
}

_DAILY_MARKERS = ("daily", "everyday", "every day", "each day")
_CHROME_RESTART_TIME = "3:30 AM Eastern"


@dataclass(frozen=True)
class AdminCommand:
    name: str
    argument: str = ""


def parse_admin_command(text: str) -> AdminCommand | None:
    normalized = " ".join(str(text or "").strip().split())
    lowered = normalized.casefold().strip(" .!?")
    if lowered in ADMIN_QUERY_PHRASES:
        return AdminCommand(ADMIN_QUERY_PHRASES[lowered])
    if (
        any(marker in lowered for marker in _DAILY_MARKERS)
        and any(noun in lowered for noun in ("chrome", "browser session"))
        and any(
            verb in lowered
            for verb in ("restart", "reboot", "refresh", "start", "starting", "launch", "open")
        )
    ):
        return AdminCommand("chrome_restart_schedule", normalized)
    if (
        any(marker in lowered for marker in _DAILY_MARKERS)
        and any(
            phrase in lowered
            for phrase in (
                "sync skills",
                "skill sync",
                "refresh skills",
                "session refresh",
                "refresh the session",
                "ezlynx login",
                "ezlynx authentication",
            )
        )
    ):
        return AdminCommand("natural_schedule", normalized)
    if not lowered.startswith("/"):
        return None
    command, _, argument = normalized.partition(" ")
    name = command.casefold().lstrip("/")
    aliases = {"pending", "schedules", "schedule", "cancel"}
    return AdminCommand(name, argument.strip()) if name in aliases else None


def _cron_values(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(low, high + 1))
            continue
        if "/" in part:
            base, step_text = part.split("/", 1)
            step = int(step_text)
            if step < 1:
                raise ValueError("cron step must be positive")
            base_values = _cron_values(base, low, high)
            first = min(base_values)
            values.update(value for value in base_values if (value - first) % step == 0)
            continue
        if "-" in part:
            start, end = (int(value) for value in part.split("-", 1))
            values.update(range(start, end + 1))
            continue
        values.add(int(part))
    if not values or min(values) < low or max(values) > high:
        raise ValueError(f"cron field {field!r} is outside {low}-{high}")
    return values


def next_cron_time(
    cron_spec: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    zone = ZoneInfo(timezone_name)
    current = (now or datetime.now(UTC)).astimezone(zone)
    every = re.fullmatch(r"@every\s+(\d+)m", cron_spec.strip(), re.IGNORECASE)
    if every:
        minutes = int(every.group(1))
        if minutes < 1:
            raise ValueError("recurring interval must be positive")
        return (current + timedelta(minutes=minutes)).astimezone(UTC).isoformat()
    fields = cron_spec.split()
    if len(fields) != 5:
        raise ValueError("cron must have five fields: minute hour day month weekday")
    minutes = _cron_values(fields[0], 0, 59)
    hours = _cron_values(fields[1], 0, 23)
    days = _cron_values(fields[2], 1, 31)
    months = _cron_values(fields[3], 1, 12)
    weekdays = _cron_values(fields[4], 0, 7)
    weekdays = {0 if value == 7 else value for value in weekdays}
    candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 370):
        cron_weekday = (candidate.weekday() + 1) % 7
        if (
            candidate.minute in minutes
            and candidate.hour in hours
            and candidate.day in days
            and candidate.month in months
            and cron_weekday in weekdays
        ):
            return candidate.astimezone(UTC).isoformat()
        candidate += timedelta(minutes=1)
    raise ValueError("cron produced no occurrence within 370 days")


def _schedule_label(item: dict) -> str:
    cron = str(item["cron_spec"])
    zone = ZoneInfo(str(item["timezone"]))
    next_at = datetime.fromisoformat(str(item["next_run_at"])).astimezone(zone)
    parts = cron.split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
        prefix = "Weekdays" if parts[4] == "1-5" else "Daily" if parts[4] == "*" else cron
        return f"{prefix} @ {next_at.strftime('%-I:%M %p %Z')}"
    return f"{cron} ({item['timezone']})"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "No matching records."
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(value.replace("|", "\\|") for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _pending(ops: OperationsStore) -> str:
    rows = ops.list_job_queue(limit=25)
    table = _table(
        ["Job ID", "Task Name", "Status", "Checkpoint"],
        [
            [row["job_id"][:8], row["task_name"], row["status"], row["checkpoint_state"]]
            for row in rows
        ],
    )
    return "ROBIE pending and recent blocked jobs\n\n" + table


def _schedules(ops: OperationsStore) -> str:
    rows = ops.list_recurring_jobs(enabled_only=True)
    table = _table(
        ["Schedule ID", "Task", "Schedule", "Target"],
        [
            [
                row["id"][:8],
                row["task_name"],
                _schedule_label(row),
                str(row.get("target_ref") or row["action_type"]),
            ]
            for row in rows
        ],
    )
    return "ROBIE active schedules\n\n" + table


def _create_schedule(ops: OperationsStore, argument: str) -> str:
    if not argument:
        return (
            "Usage: /schedule Task name | action.type | minute hour day month weekday "
            "| optional target skill/doc link | optional JSON parameters"
        )
    parts = [part.strip() for part in argument.split("|")]
    if len(parts) not in {3, 4, 5} or not all(parts[:3]):
        raise ValueError(
            "schedule format must be: Task name | action.type | cron spec | optional target | optional JSON parameters"
        )
    task_name, action_type, cron_spec = parts[:3]
    target_ref = parts[3] if len(parts) == 4 and parts[3] else None
    if action_type not in BOUNDED_ENGINE_ACTIONS:
        raise ValueError(f"unsupported scheduled action type: {action_type}")
    parameters = json.loads(parts[4]) if len(parts) == 5 and parts[4] else {}
    if not isinstance(parameters, dict):
        raise ValueError("schedule parameters must be a JSON object")
    parameters.update(
        {
            "worker": WORKER_FOR_ACTION[action_type],
            "task_name": task_name,
            "target_ref": target_ref,
        }
    )
    if action_type == "drive.skill_sync":
        from .skill_sync import ALLOWED_FOLDERS, EXCLUDED_FOLDERS, skill_sync_root

        parameters.update(
            {
                "destination_root": str(skill_sync_root()),
                "included_folders": list(ALLOWED_FOLDERS),
                "excluded_folders": sorted(EXCLUDED_FOLDERS),
            }
        )
    timezone_name = "America/New_York"
    next_at = next_cron_time(cron_spec, timezone_name)
    item = ops.ensure_recurring_job(
        task_name,
        action_type,
        parameters,
        cron_spec,
        timezone_name,
        next_run_at=next_at,
        target_ref=target_ref,
        reconcile=True,
    )
    item = _verify_schedule_reread(
        ops,
        item["id"],
        task_name=task_name,
        action_type=action_type,
        parameters=parameters,
        cron_spec=cron_spec,
        timezone_name=timezone_name,
        target_ref=target_ref,
    )
    return (
        f"Scheduled and independently verified {item['task_name']} ({item['id'][:8]}): "
        f"{_schedule_label(item)}. No Playwright run was launched."
    )


def _verify_schedule_reread(
    ops: OperationsStore,
    schedule_id: str,
    *,
    task_name: str,
    action_type: str,
    parameters: dict,
    cron_spec: str,
    timezone_name: str,
    target_ref: str | None,
) -> dict:
    """Freshly reread the authoritative SQLite row before reporting success."""
    observed = ops.get_recurring_job(schedule_id)
    expected = {
        "task_name": task_name,
        "action_type": action_type,
        "parameters": parameters,
        "cron_spec": cron_spec,
        "timezone": timezone_name,
        "target_ref": target_ref,
        "enabled": 1,
    }
    actual = {key: observed.get(key) for key in expected}
    if actual != expected:
        raise ValueError(
            "schedule reread verification failed; the stored schedule does not match the request"
        )
    return observed


def _natural_daily_schedule(ops: OperationsStore, text: str) -> str:
    lowered = " ".join(text.casefold().split())
    time_match = re.search(
        r"(?:\bat\b|@)\s*(1[0-2]|0?\d)(?::([0-5]\d))?\s*(am|pm)\b",
        lowered,
    )
    if not time_match:
        return (
            "What time Eastern should I run this every day? "
            "For example: ‘Schedule skill sync every day at 8:00 AM.’ "
            "No Job or schedule was created."
        )
    hour = int(time_match.group(1)) % 12
    if time_match.group(3) == "pm":
        hour += 12
    minute = int(time_match.group(2) or "0")
    cron_spec = f"{minute} {hour} * * *"
    if any(phrase in lowered for phrase in ("sync skills", "skill sync", "refresh skills")):
        action_type = "drive.skill_sync"
        task_name = "Daily approved skill sync"
        target_ref = "StreetSmart/Robie/Skills"
    else:
        action_type = "ezlynx.session_refresh"
        task_name = "Daily EZLynx and Gmail session refresh"
        target_ref = "ezlynx:authenticated-browser-session"
    parameters = {
        "worker": WORKER_FOR_ACTION[action_type],
        "task_name": task_name,
        "target_ref": target_ref,
    }
    if action_type == "drive.skill_sync":
        from .skill_sync import ALLOWED_FOLDERS, EXCLUDED_FOLDERS, skill_sync_root

        parameters.update(
            {
                "destination_root": str(skill_sync_root()),
                "included_folders": list(ALLOWED_FOLDERS),
                "excluded_folders": sorted(EXCLUDED_FOLDERS),
            }
        )
    else:
        parameters.update(
            {
                "resource_id": "ezlynx:authenticated-browser-session",
                "profile_id": "robie-ezlynx-canonical-profile",
                "perform_timeout_seconds": 210,
                "_daily_local_time": f"{hour:02d}:{minute:02d}",
                "_schedule_timezone": "America/New_York",
            }
        )
    next_at = next_cron_time(cron_spec, "America/New_York")
    item = ops.ensure_recurring_job(
        task_name,
        action_type,
        parameters,
        cron_spec,
        "America/New_York",
        next_run_at=next_at,
        target_ref=target_ref,
        reconcile=True,
    )
    item = _verify_schedule_reread(
        ops,
        item["id"],
        task_name=task_name,
        action_type=action_type,
        parameters=parameters,
        cron_spec=cron_spec,
        timezone_name="America/New_York",
        target_ref=target_ref,
    )
    return (
        f"Scheduled and independently verified {item['task_name']} ({item['id'][:8]}): "
        f"{_schedule_label(item)}. No Playwright run was launched."
    )


def _unique_prefix(rows: list[dict], value: str, key: str = "id") -> dict | None:
    matches = [row for row in rows if str(row[key]).startswith(value)]
    if len(matches) > 1:
        raise ValueError(f"identifier prefix {value!r} is ambiguous")
    return matches[0] if matches else None


def _cancel(db_path: str, ops: OperationsStore, argument: str, actor: str) -> str:
    value = argument.strip()
    if not value:
        return "Usage: /cancel <Job ID or Schedule ID>"
    schedule = _unique_prefix(ops.list_recurring_jobs(enabled_only=False), value)
    if schedule:
        ops.disable_recurring_job(schedule["id"])
        return f"Schedule {schedule['id'][:8]} ({schedule['task_name']}) is disabled."
    store = JobStore(db_path)
    with store.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM jobs WHERE id LIKE ?", (value + "%",))]
    job = _unique_prefix(rows, value)
    if not job:
        raise ValueError(f"no Job or schedule matches {value!r}")
    status = JobStatus(job["status"])
    if status in TERMINAL_STATUSES:
        return f"Job {job['id'][:8]} is already {status.value}; no change was made."
    store.checkpoint(job["id"], "cancelled", {"actor": actor or "Google Chat administrator"})
    store.transition(
        job["id"],
        JobStatus.FAILED,
        expected={status},
        error=f"Cancelled by {actor or 'Google Chat administrator'}",
        release_lease=True,
    )
    return f"Job {job['id'][:8]} was cancelled and marked FAILED."


def handle_admin_command(
    db_path: str,
    text: str,
    *,
    actor: str = "",
) -> str | None:
    command = parse_admin_command(text)
    if command is None:
        return None
    ops = OperationsStore(db_path)
    try:
        if command.name == "pending":
            return _pending(ops)
        if command.name == "schedules":
            return _schedules(ops)
        if command.name == "schedule":
            return _create_schedule(ops, command.argument)
        if command.name == "natural_schedule":
            return _natural_daily_schedule(ops, command.argument)
        if command.name == "chrome_restart_schedule":
            return (
                "Chrome is already scheduled to restart every day at "
                f"{_CHROME_RESTART_TIME}, before ROBIE's 5:30 AM authenticated "
                "session refresh. I did not create a duplicate Job or schedule."
            )
        if command.name == "cancel":
            return _cancel(db_path, ops, command.argument, actor)
    except (KeyError, ValueError) as exc:
        return f"ROBIE admin command was not applied: {exc}"
    return None
