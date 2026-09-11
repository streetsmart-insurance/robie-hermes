"""OnFailure Chat alert for robie-scheduler.service.

Posts a short CRITICAL message to the Robie Chat space (and Carlo/Jake
DMs) via the same Chat APP path as Production pre-flight
(``chat_app_post.notify_operators`` / ``post_as_chat_app``). Local syslog
plus ``scheduler-alert.log`` stay as the fallback when Chat is down.

Never @robie. Never creates a space. Always exits 0 so systemd OnFailure
cannot recurse through this oneshot.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .chat_app_post import notify_operators


DEFAULT_FAILED_UNIT = "robie-scheduler.service"
DEFAULT_ALERT_LOG = (
    "/opt/streetsmart-hermes/robie-job-engine/data/scheduler-alert.log"
)
DEFAULT_LOCAL_TZ = "America/New_York"
LOGGER_NAME = "robie-scheduler-alert"

logger = logging.getLogger(LOGGER_NAME)


def journalctl_hint(unit: str) -> str:
    name = str(unit or DEFAULT_FAILED_UNIT).strip() or DEFAULT_FAILED_UNIT
    if name.endswith(".service"):
        name = name[: -len(".service")]
    return f"journalctl -u {name} -n 50"


def format_alert_timestamps(
    now: datetime | None = None,
    *,
    local_tz: str = DEFAULT_LOCAL_TZ,
) -> tuple[str, str]:
    utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    utc_text = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        local = utc.astimezone(ZoneInfo(local_tz))
        local_text = local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        local_text = utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    return utc_text, local_text


def format_alert(
    unit: str,
    *,
    now: datetime | None = None,
    local_tz: str = DEFAULT_LOCAL_TZ,
) -> str:
    """Short CRITICAL text. Dry. Never @robie."""
    failed = str(unit or DEFAULT_FAILED_UNIT).strip() or DEFAULT_FAILED_UNIT
    utc_text, local_text = format_alert_timestamps(now, local_tz=local_tz)
    text = (
        f"CRITICAL: {failed} failed\n"
        f"utc: {utc_text}\n"
        f"local: {local_text}\n"
        f"hint: {journalctl_hint(failed)}"
    )
    if "@robie" in text.casefold():
        raise ValueError("scheduler alert Chat must not @robie")
    return text


def write_local_alert(
    text: str,
    *,
    log_path: str | Path,
    logger_argv: list[str] | None = None,
) -> dict[str, Any]:
    """Keep the existing local log + logger trail. Best-effort."""
    path = Path(log_path)
    errors: list[str] = []
    line = text.rstrip() + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception as exc:
        errors.append(f"log:{type(exc).__name__}")
    argv = logger_argv
    if argv is None:
        argv = ["logger", "-t", LOGGER_NAME, text.replace("\n", " | ")]
    try:
        subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        errors.append(f"logger:{type(exc).__name__}")
        logger.error("%s", text.replace("\n", " | "))
    return {"path": str(path), "errors": errors}


def run_scheduler_alert(
    unit: str = DEFAULT_FAILED_UNIT,
    *,
    now: datetime | None = None,
    log_path: str | Path | None = None,
    poster: Callable[..., Any] | None = None,
    dm_finder: Callable[[str], str] | None = None,
    logger_argv: list[str] | None = None,
    local_tz: str = DEFAULT_LOCAL_TZ,
) -> dict[str, Any]:
    """Format, write local fallback, then Chat. Never raises to the caller."""
    path = Path(
        log_path
        or os.environ.get("ROBIE_SCHEDULER_ALERT_LOG", DEFAULT_ALERT_LOG)
    )
    try:
        text = format_alert(unit, now=now, local_tz=local_tz)
    except Exception:
        text = (
            "CRITICAL: scheduler unit failed\n"
            f"hint: {journalctl_hint(DEFAULT_FAILED_UNIT)}"
        )
    local = write_local_alert(text, log_path=path, logger_argv=logger_argv)
    notify: dict[str, Any] = {}
    chat_error = None
    try:
        notify = notify_operators(text, poster=poster, dm_finder=dm_finder)
    except Exception as exc:
        chat_error = f"{type(exc).__name__}: {exc}"
        write_local_alert(
            f"chat_post_failed: {chat_error}",
            log_path=path,
            logger_argv=logger_argv,
        )
        logger.exception("scheduler alert Chat post failed")
    return {
        "ok": True,
        "exit_code": 0,
        "unit": str(unit or DEFAULT_FAILED_UNIT).strip() or DEFAULT_FAILED_UNIT,
        "message": text,
        "chat_posted": bool(notify.get("space_posted")),
        "fail_notify_targets": list(notify.get("targets") or []),
        "fail_notify_dm_errors": list(notify.get("dm_errors") or []),
        "chat_error": chat_error,
        "local_log": local,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "OnFailure Chat alert for robie-scheduler. Posts CRITICAL to "
            "the Robie Chat space via post_as_chat_app. Never @robie. "
            "Always exits 0 so OnFailure cannot recurse."
        )
    )
    parser.add_argument(
        "unit",
        nargs="?",
        default=os.environ.get("ROBIE_SCHEDULER_ALERT_UNIT", DEFAULT_FAILED_UNIT),
        help="Failed systemd unit name (%i from robie-scheduler-alert@.service)",
    )
    parser.add_argument(
        "--log",
        default=os.environ.get("ROBIE_SCHEDULER_ALERT_LOG", DEFAULT_ALERT_LOG),
        help="Fallback log path (default: scheduler-alert.log under the job-engine data dir)",
    )
    args = parser.parse_args(argv)
    try:
        run_scheduler_alert(unit=args.unit, log_path=args.log)
    except Exception:
        logger.exception("scheduler alert oneshot failed; exiting 0 to avoid OnFailure loop")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    raise SystemExit(main())
