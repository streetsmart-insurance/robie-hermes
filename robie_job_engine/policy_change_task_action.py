"""Post-check task action for the policy-change confirmation pilot.

When ROBIE finishes checking an assigned task — or when it is not sure —
the task goes back to the person who assigned it. A new task is never
created. There is no code path that creates a task.

Two writes, both scoped to the one task ROBIE was assigned, both in the
box browser (EZLynx has no Task API — a TaskApi token request returns
400 invalid_scope):

1. The plain-English result note is posted as a comment on the task. The
   note ends with "ROBIE was here". The comment is read back afterwards.
2. The task is reassigned to the original assigner, saved, and the assignee
   is read back.

Carlo chose the task comment as the note channel (2026-10-02) over a
discussion note. This overrides the repo's API-only note rule for this
pilot's task comments; discussion notes stay API-only.

`build_task_action()` is pure decision logic and is unit-tested. The browser
half lives in `policy_change_task_executor.py`.
"""

from __future__ import annotations

from typing import Any, Mapping

TASK_ACTION_VERSION = "1"

# The drafted result note always ends with this line. The executor refuses
# to post a note that does not carry it.
NOTE_SIGNATURE = "ROBIE was here"

# Carlo 2026-10-02: the result note goes on the task as a comment.
NOTE_CHANNEL = "task_comment"

_ROBIE_OWNERS = frozenset({"ssrobie", "robie", "robie ai"})


def _is_robie(value: Any) -> bool:
    return str(value or "").strip().casefold() in _ROBIE_OWNERS


def _task_id(packet: Mapping[str, Any]) -> str:
    case = packet.get("case") if isinstance(packet.get("case"), Mapping) else {}
    task = packet.get("task") if isinstance(packet.get("task"), Mapping) else {}
    for source in (packet, case, task):
        for key in ("task_id", "id", "TaskId"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def build_task_action(packet: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Decide what happens to the task after the check. Never creates a task.

    Returns a reassign_back action when the original assigner is known
    exactly, otherwise a hold action that leaves the task with ROBIE for a
    person. The ``create_task`` key is always False; no other value is
    possible. The note travels in the action for the executor to post as a
    task comment.
    """
    packet = packet if isinstance(packet, Mapping) else {}
    result = result if isinstance(result, Mapping) else {}
    note = str(result.get("note") or "").strip()
    if note and not note.rstrip().endswith(NOTE_SIGNATURE):
        note = note.rstrip() + "\n" + NOTE_SIGNATURE + "\n"
    action: dict[str, Any] = {
        "version": TASK_ACTION_VERSION,
        "create_task": False,
        "note_channel": NOTE_CHANNEL,
        "task_id": _task_id(packet),
    }
    if not note:
        action["action"] = "hold"
        action["reason"] = (
            "No result note was drafted, so nothing was posted and the task "
            "was not reassigned. A person needs to look at this."
        )
        return action
    recipient = result.get("result_recipient")
    recipient = recipient if isinstance(recipient, Mapping) else {}
    assigner_id = str(recipient.get("id") or "").strip()
    assigner_name = str(recipient.get("name") or "").strip()
    if not assigner_id or _is_robie(assigner_id) or _is_robie(assigner_name):
        action["action"] = "hold"
        action["reason"] = (
            "The person who assigned this task could not be identified "
            "exactly, so the task stays with ROBIE for a person to handle. "
            "Nothing was posted and nothing was reassigned."
        )
        return action
    action["action"] = "reassign_back"
    action["to_assigner_id"] = assigner_id
    action["to_assigner_name"] = assigner_name
    action["note"] = note
    action["reason"] = (
        "The check finished. The result is posted as a task comment in plain "
        "English and the task goes back to the person who assigned it."
    )
    return action


def validate_note_for_task(note: str) -> str:
    """Return the note when it is safe to post as a task comment.

    Raises when the note is blank or does not end with the signature.
    """
    text = str(note or "").strip()
    if not text:
        raise ValueError("note body is required")
    if not text.rstrip().endswith(NOTE_SIGNATURE):
        raise ValueError("the result note must end with the ROBIE signature")
    return text
