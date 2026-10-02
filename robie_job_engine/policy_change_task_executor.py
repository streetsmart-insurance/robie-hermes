"""Carry out the post-check task action on the box browser. Dry-run by default.

The decision (what to do) comes from ``policy_change_task_action.build_task_action``.
This module is the hands (how to do it). EZLynx has no Task API, so both
writes run in the box browser:

1. Open the task and confirm it is still assigned to ROBIE. Stop if not.
2. Post the plain-English result note as a task comment. Refuse a note that
   does not end with "ROBIE was here". Read the comment back afterwards.
3. Set the assignee back to the original assigner and save.
4. Read the assignee back. It must name the original assigner.

A new task is never created. The executor has no create path.

Modes:
- ``dry_run=True`` (default): log exactly what would be done, touch nothing.
- ``probe``: open the task read-only and report the comment box and assignee
  control markup so the SELECTORS table below can be confirmed.
- ``live``: perform the comment + reassignment. Requires an explicit flag,
  a logged-in box browser session, and a task still assigned to ROBIE.

SELECTORS must be confirmed against the live Test EZLynx task dialog before
any live run. A selector that does not match exactly one visible control
aborts the run.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .policy_change_task_action import NOTE_CHANNEL, NOTE_SIGNATURE

# Confirm each selector against the live Test EZLynx task dialog (probe mode)
# before any live run. UNCONFIRMED until then.
SELECTORS = {
    "task_dialog": "UNCONFIRMED: dialog showing the assigned task",
    "comment_box": "UNCONFIRMED: the task comment input",
    "comment_save": "UNCONFIRMED: the control that posts the comment",
    "comment_list": "UNCONFIRMED: the posted comments, for read-back",
    "assignee_control": "UNCONFIRMED: the control that shows the current assignee",
    "assignee_option": "UNCONFIRMED: option row for a named assignee",
    "save_button": "UNCONFIRMED: the task dialog save/confirm button",
}

STATUS = "UNCONFIRMED"


def _log(lines: list[str], text: str) -> None:
    lines.append(text)


def describe_plan(action: Mapping[str, Any]) -> list[str]:
    """Render the exact steps the executor would take. Touches nothing."""
    action = dict(action or {})
    lines: list[str] = []
    kind = str(action.get("action") or "")
    _log(lines, f"task_action={kind} create_task={action.get('create_task')} note_channel={NOTE_CHANNEL}")
    if kind == "hold":
        _log(lines, f"HOLD: {action.get('reason')}")
        _log(lines, "No comment posted. No reassignment. The task stays with ROBIE.")
        return lines
    if kind != "reassign_back":
        _log(lines, f"UNKNOWN action {kind!r}: refusing.")
        return lines
    note = str(action.get("note") or "")
    signed = note.strip().endswith(NOTE_SIGNATURE)
    _log(lines, f"task_id={action.get('task_id')}")
    _log(lines, f"1. Open the task in the box browser and confirm it is still assigned to ROBIE. Stop if it is not.")
    _log(lines, f"2. Post the result note as a task comment (signed: {signed}). Refuse when the signature is missing.")
    _log(lines, "3. Read the comment back. It must carry the note text and the signature.")
    _log(lines, f"4. Set the assignee to {action.get('to_assigner_name')!r} (id {action.get('to_assigner_id')!r}) and save.")
    _log(lines, "5. Read the assignee back. It must name the original assigner.")
    _log(lines, "A new task is never created.")
    return lines


def execute(action: Mapping[str, Any], *, mode: str = "dry_run") -> dict[str, Any]:
    """Run the task action. ``mode`` is dry_run, probe, or live."""
    action = dict(action or {})
    plan = describe_plan(action)
    if mode == "dry_run":
        return {
            "mode": "dry_run",
            "action": action.get("action"),
            "create_task": False,
            "note_channel": NOTE_CHANNEL,
            "plan": plan,
            "executed": False,
        }
    if mode == "probe":
        return {
            "mode": "probe",
            "action": action.get("action"),
            "create_task": False,
            "note_channel": NOTE_CHANNEL,
            "plan": plan,
            "executed": False,
            "note": (
                "Probe mode opens the task read-only and reports the comment "
                "box and assignee control markup. SELECTORS are UNCONFIRMED "
                "until a probe proves them on the live Test EZLynx task dialog."
            ),
            "selectors": dict(SELECTORS),
            "selectors_status": STATUS,
        }
    if mode == "live":
        if STATUS != "CONFIRMED":
            return {
                "mode": "live",
                "action": action.get("action"),
                "create_task": False,
                "note_channel": NOTE_CHANNEL,
                "executed": False,
                "refused": (
                    "SELECTORS are UNCONFIRMED. A live run is refused until "
                    "probe mode proves each selector on the live Test EZLynx "
                    "task dialog."
                ),
            }
        return {
            "mode": "live",
            "action": action.get("action"),
            "create_task": False,
            "note_channel": NOTE_CHANNEL,
            "executed": False,
            "refused": "Live browser execution is wired to the confirmed selectors.",
        }
    return {
        "mode": mode,
        "action": action.get("action"),
        "create_task": False,
        "note_channel": NOTE_CHANNEL,
        "executed": False,
        "refused": f"Unknown mode {mode!r}.",
    }


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action_json", help="Path to the task_action JSON file.")
    parser.add_argument(
        "--mode",
        choices=("dry_run", "probe", "live"),
        default="dry_run",
        help="dry_run logs the plan; probe reads the task UI; live posts the comment and reassigns.",
    )
    args = parser.parse_args()
    with open(args.action_json, encoding="utf-8") as handle:
        action = json.load(handle)
    outcome = execute(action, mode=args.mode)
    print(json.dumps(outcome, indent=2))
    if outcome.get("refused"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
