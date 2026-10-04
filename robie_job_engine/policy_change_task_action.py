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


def _task(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    task = packet.get("task")
    return task if isinstance(task, Mapping) else {}


def _task_id(packet: Mapping[str, Any]) -> str:
    case = packet.get("case") if isinstance(packet.get("case"), Mapping) else {}
    task = _task(packet)
    for source in (packet, case, task):
        for key in ("task_id", "id", "TaskId"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _current_owner(packet: Mapping[str, Any]) -> str:
    """The person who owns the task now, by name when the snapshot has one."""
    task = _task(packet)
    for key in ("current_owner_name", "assignee_name", "current_owner_id", "assignee_id"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    return ""


def _stay_sentence(owner: str) -> str:
    if not owner:
        return "The current owner was not named."
    if _is_robie(owner):
        return "The task stays with ROBIE."
    return f"The task stays with {owner}."


def _hold_reason(owner: str) -> str:
    stay = _stay_sentence(owner)
    if owner and not _is_robie(owner):
        return (
            f"The task is not assigned to ROBIE. {stay} "
            "Nothing was posted and nothing was reassigned."
        )
    return (
        "The person who assigned this task could not be identified exactly. "
        f"{stay} Nothing was posted and nothing was reassigned."
    )


def _human_previous_owner(packet: Mapping[str, Any]) -> bool:
    """True when an assignment event names a person other than ROBIE."""
    events = packet.get("assignment_events")
    if not isinstance(events, list):
        return False
    for item in events:
        if not isinstance(item, Mapping):
            continue
        previous = str(item.get("previous_owner_id") or "").strip()
        name = str(item.get("previous_owner_name") or "").strip()
        if (previous and not _is_robie(previous)) or (name and not _is_robie(name)):
            return True
    return False


def _creator(packet: Mapping[str, Any]) -> tuple[str, str]:
    """Return (id, name) for the person who created the task.

    A ROBIE creator is not a person to hand the task back to.
    """
    task = _task(packet)
    name = str(task.get("created_by_name") or "").strip()
    created = str(task.get("created_by_id") or task.get("created_by") or "").strip()
    if _is_robie(created) or _is_robie(name):
        return "", ""
    if name and created and created != name:
        return created, name
    if name:
        return name, name
    if created:
        return created, created
    return "", ""


def _created_already_assigned_to_robie(packet: Mapping[str, Any]) -> bool:
    """True when ROBIE owns the task and no person handed it over.

    Par-Troy was created by Carlo already assigned to Robie. There is no
    human previous owner on that task.
    """
    if not _is_robie(_current_owner(packet)):
        return False
    return not _human_previous_owner(packet)


def _hold(action: dict[str, Any], packet: Mapping[str, Any], reason: str) -> dict[str, Any]:
    owner = _current_owner(packet)
    action["action"] = "hold"
    action["judgment_call"] = False
    action["create_task"] = False
    action["current_owner"] = owner
    action["stays_with"] = _stay_sentence(owner)
    action["reason"] = reason
    return action


def build_task_action(packet: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Decide what happens to the task after the check. Never creates a task.

    Returns a reassign_back action when the original assigner is known
    exactly. A task created already assigned to ROBIE, with no person who
    handed it over, goes back to its creator. That hand-back is a judgment
    call. Every other unfinished case is a hold, and the hold names the
    person who owns the task now. The ``create_task`` key is always False.
    """
    packet = packet if isinstance(packet, Mapping) else {}
    result = result if isinstance(result, Mapping) else {}
    note = str(result.get("note") or "").strip()
    if note and not note.rstrip().endswith(NOTE_SIGNATURE):
        note = note.rstrip() + "\n" + NOTE_SIGNATURE + "\n"
    action: dict[str, Any] = {
        "version": TASK_ACTION_VERSION,
        "create_task": False,
        "judgment_call": False,
        "note_channel": NOTE_CHANNEL,
        "task_id": _task_id(packet),
    }
    if not note:
        return _hold(
            action,
            packet,
            "No result note was drafted, so nothing was posted and the task "
            f"was not reassigned. {_stay_sentence(_current_owner(packet))}",
        )
    recipient = result.get("result_recipient")
    recipient = recipient if isinstance(recipient, Mapping) else {}
    assigner_id = str(recipient.get("id") or "").strip()
    assigner_name = str(recipient.get("name") or "").strip()
    if not assigner_id or _is_robie(assigner_id) or _is_robie(assigner_name):
        if _created_already_assigned_to_robie(packet):
            creator_id, creator_name = _creator(packet)
            if creator_id and creator_name:
                action["action"] = "reassign_back"
                action["judgment_call"] = True
                action["create_task"] = False
                action["to_assigner_id"] = creator_id
                action["to_assigner_name"] = creator_name
                action["note"] = note
                action["reason"] = (
                    "This task was created already assigned to ROBIE. "
                    f"Handing it back to {creator_name} is a judgment call. "
                    "A new task was not created."
                )
                return action
        return _hold(action, packet, _hold_reason(_current_owner(packet)))
    action["action"] = "reassign_back"
    action["judgment_call"] = False
    action["create_task"] = False
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
