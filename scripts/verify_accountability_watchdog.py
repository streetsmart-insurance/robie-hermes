#!/usr/bin/env python3
"""Fail-closed verifier for the scheduled Production accountability delivery."""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
EXPECTED_HOST = "streetsmart-accountability-prod"
TIMER_UNIT = "streetsmart-accountability.timer"
SERVICE_UNIT = "streetsmart-accountability.service"


def _run(*args: str) -> str:
    completed = subprocess.run(
        args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {args[0]} {args[1] if len(args) > 1 else ''}".strip())
    return completed.stdout.strip()


def _systemctl_value(unit: str, property_name: str) -> str:
    return _run("systemctl", "show", unit, f"--property={property_name}", "--value")


def collect_snapshot(app_root: Path) -> tuple[dict[str, Any], str, str]:
    sys.path.insert(0, str(app_root))
    from src.engine.date_utils import get_previous_business_day

    now = datetime.now(NEW_YORK)
    today = now.date().isoformat()
    expected_target = get_previous_business_day(now.date()).isoformat()

    state_path = app_root / "data" / "run_state" / "last_success.json"
    if not state_path.is_file():
        raise RuntimeError("missing verified success marker")
    state = json.loads(state_path.read_text(encoding="utf-8"))

    document_url = str(state.get("document_url") or "")
    document_match = re.search(r"/document/d/([A-Za-z0-9_-]+)", document_url)

    snapshot = {
        "host": socket.gethostname().split(".", 1)[0],
        "timer_load": _systemctl_value(TIMER_UNIT, "LoadState"),
        "timer_enabled": _run("systemctl", "is-enabled", TIMER_UNIT),
        "timer_active": _run("systemctl", "is-active", TIMER_UNIT),
        "timer_next": _systemctl_value(TIMER_UNIT, "NextElapseUSecRealtime"),
        "service_result": _systemctl_value(SERVICE_UNIT, "Result"),
        "service_exit_status": _systemctl_value(SERVICE_UNIT, "ExecMainStatus"),
        "run_date": str(state.get("run_date") or ""),
        "target_date": str(state.get("target_date") or ""),
        "document_url_present": document_url.startswith(
            "https://docs.google.com/document/"
        ),
        "document_id": document_match.group(1) if document_match else "",
    }
    return snapshot, today, expected_target


def validate_snapshot(
    snapshot: Mapping[str, Any],
    *,
    today: str,
    expected_target: str,
) -> dict[str, Any]:
    checks = {
        "host": snapshot.get("host") == EXPECTED_HOST,
        "timer_loaded": snapshot.get("timer_load") == "loaded",
        "timer_enabled": snapshot.get("timer_enabled") == "enabled",
        "timer_active": snapshot.get("timer_active") == "active",
        "timer_next_present": bool(str(snapshot.get("timer_next") or "").strip()),
        "service_result": snapshot.get("service_result") == "success",
        "service_exit_status": str(snapshot.get("service_exit_status")) == "0",
        "run_date": snapshot.get("run_date") == today,
        "target_date": snapshot.get("target_date") == expected_target,
        "document_url_present": snapshot.get("document_url_present") is True,
        "document_id_present": bool(str(snapshot.get("document_id") or "").strip()),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError("accountability watchdog failed checks: " + ", ".join(failed))

    return {
        "verified": True,
        "host": EXPECTED_HOST,
        "run_date": today,
        "target_date": expected_target,
        "timer_enabled": True,
        "timer_active": True,
        "timer_next_present": True,
        "service_result": "success",
        "service_exit_status": 0,
        "document_url_present": True,
        "document_id_present": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--include-document-id", action="store_true")
    args = parser.parse_args()

    try:
        snapshot, today, expected_target = collect_snapshot(
            Path(args.app_root).expanduser().resolve()
        )
        evidence = validate_snapshot(
            snapshot,
            today=today,
            expected_target=expected_target,
        )
        if args.include_document_id:
            evidence["document_id"] = snapshot["document_id"]
    except Exception as exc:
        print(
            f"watchdog verification failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
