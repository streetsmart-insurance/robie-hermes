"""Fire EZLynx follow-up tasks through the agency's Zapier catch-hook Zap.

The hook URL lives in the Secure Vault as ``custom.zapier-webhook``; this
module never sees it.  Firing goes through the zapier skill's
``bin/zap-trigger`` script, which swaps the real URL in on approved egress.
Use ``dry_run=True`` to validate a payload without firing.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Any

ZAP_TRIGGER_SCRIPT = os.path.expanduser("~/workspace/skills/zapier/bin/zap-trigger")

# due_date is required by standing agency rule: every EZLynx follow-up task
# must carry an ISO YYYY-MM-DD due date, and fire_task rejects payloads
# without a valid one.
REQUIRED_PAYLOAD_KEYS = ("applicant_id", "task_title", "assignee", "source", "due_date")


def validate_due_date(value: Any) -> str:
    """Validate an ISO YYYY-MM-DD due date; return the normalized date string."""
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(
            f"Zapier task due_date must be ISO YYYY-MM-DD, got {value!r}"
        ) from None


def validate_task_payload(payload: dict[str, Any]) -> None:
    """Raise ValueError when the Zap payload is missing required keys."""
    if not isinstance(payload, dict):
        raise ValueError("Zapier task payload must be a JSON object")
    missing = [key for key in REQUIRED_PAYLOAD_KEYS if not payload.get(key)]
    if missing:
        raise ValueError(
            "Zapier task payload missing required keys: " + ", ".join(missing)
        )
    # Normalize in place so the fired payload always carries a canonical date.
    payload["due_date"] = validate_due_date(payload.get("due_date"))


def fire_task(payload: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """POST the payload to the Zapier catch hook (or dry-run validate it).

    Returns the script's parsed result dict: {"ok": bool, ...}.  Raises
    ValueError on a bad payload and RuntimeError when the script itself
    cannot run.
    """
    validate_task_payload(payload)
    if not os.path.isfile(ZAP_TRIGGER_SCRIPT):
        raise RuntimeError(f"zap-trigger script not found at {ZAP_TRIGGER_SCRIPT}")
    command = ["python3", ZAP_TRIGGER_SCRIPT, "--payload", json.dumps(payload)]
    if dry_run:
        command.append("--dry-run")
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=90
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"zap-trigger failed to run: {exc}") from exc
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"zap-trigger returned non-JSON output: {completed.stdout[:200]}"
        ) from exc
    if completed.returncode not in (0, 1, 2):
        raise RuntimeError(f"zap-trigger exited {completed.returncode}: {result}")
    return result
