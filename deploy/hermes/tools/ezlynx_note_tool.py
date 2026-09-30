#!/usr/bin/env python3
"""Hermes tool: ezlynx_discussion_note.

Email and Chat jobs that need to file an EZLynx discussion note must call
this tool — not playwright_exec and not a hand-rolled browser click. The
handler invokes DiscussionApi ``add_note_to_discussion`` /
``file_note_to_existing_discussion``, which enforces the write-allowlist
and reads the note back before success. Playwright is for forms and
portals only.
"""
from __future__ import annotations

from tools.registry import registry, tool_error, tool_result

DISCUSSION_NOTE_SCHEMA = {
    "name": "ezlynx_discussion_note",
    "description": (
        "Append a note to an existing titled EZLynx discussion via the "
        "DiscussionApi. USE THIS TOOL — not playwright_exec, not Add Note, "
        "and not Save Note — whenever the job needs to file a note. Never "
        "creates a discussion and never writes on Untitled. The engine "
        "enforces the write-allowlist and requires a fresh read-back of "
        "note_id before success."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "applicant_id": {
                "type": "string",
                "description": "EZLynx applicant/account id (e.g. 220250093).",
            },
            "note_text": {
                "type": "string",
                "description": (
                    "Note body. Must include the exact phrase "
                    "'Robie was here' or 'ROBIE was here'."
                ),
            },
            "title_hint": {
                "type": "string",
                "description": (
                    "Optional existing discussion title to append to "
                    "(New Business, Renewal, Submission Center, ...)."
                ),
            },
            "plan": {
                "type": "object",
                "description": (
                    "The plan stated before the write: write, target, and values. "
                    "Required when this job has no locked plan. The note is not "
                    "posted until that plan is locked."
                ),
            },
        },
        "required": ["applicant_id", "note_text"],
    },
}


def _file_note(args: dict) -> dict:
    from robie_job_engine.ezlynx_api_only_writes import add_note_to_discussion

    applicant_id = str(args.get("applicant_id") or "").strip()
    note_text = str(args.get("note_text") or "").strip()
    title_hint = str(args.get("title_hint") or "").strip() or None
    if not applicant_id:
        raise ValueError("applicant_id is required")
    if not note_text:
        raise ValueError("note_text is required")
    from robie_job_engine.answer_only import (
        address_readback_proved,
        holder_readback_proved,
        rewrite_unproved_address_note,
        rewrite_unproved_holder_note,
    )

    note_text = rewrite_unproved_address_note(
        note_text,
        proved=address_readback_proved(args),
    )
    note_text = rewrite_unproved_holder_note(
        note_text,
        proved=holder_readback_proved(args),
    )
    folded = note_text.casefold()
    if "robie was here" not in folded:
        note_text = note_text + "\n\nRobie was here"
    filed = add_note_to_discussion(
        applicant_id,
        note_text,
        title_hint=title_hint,
        discussion_title=title_hint,
    )
    status = str(filed.get("status") or "")
    if status not in {"filed", "posted, verifying"}:
        reason = str(filed.get("reason") or "").strip()
        raise RuntimeError(reason or "The note was not sent.")
    if status == "filed" and not filed.get("note_id") and not filed.get("read_back"):
        raise RuntimeError("The note was not confirmed, so it is not marked done.")
    confirmed = status == "filed" and bool(filed.get("read_back") or filed.get("note_id"))
    posted = status in {"filed", "posted, verifying"}
    return {
        "ok": confirmed,
        "status": status,
        "note_id": filed.get("note_id"),
        "discussion_id": filed.get("discussion_id"),
        "discussion_title": filed.get("discussion_title"),
        "applicant_id": applicant_id,
        "note_text": note_text,
        "read_back": bool(filed.get("read_back")),
        "verified_by": filed.get("verified_by"),
        "reason": filed.get("reason"),
        "do_not_repost": posted,
        "instruction": (
            "Posted. Do not post this note again."
            if status == "posted, verifying"
            else ""
        ),
    }


def _remember_note_tool_failure(kwargs: dict, message: str) -> None:
    import os

    job_id = str(
        (kwargs or {}).get("job_id")
        or os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()
    db_path = str(
        (kwargs or {}).get("db_path") or os.environ.get("ROBIE_JOB_DB") or ""
    ).strip()
    if not job_id or not db_path:
        return
    try:
        from robie_job_engine.chat_turn_control import record_note_tool_failure
        from robie_job_engine.store import JobStore

        record_note_tool_failure(JobStore(db_path), job_id, message)
    except Exception:
        return


def _remember_discussion_note(kwargs: dict, report: dict) -> None:
    import os

    job_id = str(
        (kwargs or {}).get("job_id")
        or os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()
    db_path = str(
        (kwargs or {}).get("db_path") or os.environ.get("ROBIE_JOB_DB") or ""
    ).strip()
    if not job_id or not db_path:
        return
    try:
        from robie_job_engine.store import JobStore

        from robie_job_engine.chat_job_controls import discussion_note_step_id

        JobStore(db_path).checkpoint(
            job_id,
            "discussion_note",
            {
                "status": report.get("status"),
                "discussion_title": report.get("discussion_title"),
                "discussion_id": report.get("discussion_id"),
                "applicant_id": report.get("applicant_id"),
                "note_text": report.get("note_text"),
                "request_note": str((kwargs or {}).get("request_note") or ""),
                "step_id": discussion_note_step_id(
                    JobStore(db_path), job_id, kwargs.get("step_args") or {}
                ),
                "note_id": report.get("note_id"),
                "read_back": bool(report.get("read_back")),
                "verified_by": report.get("verified_by"),
                "reason": report.get("reason"),
            },
        )
    except Exception:
        return


def ezlynx_discussion_note_handler(args: dict, **kwargs):
    from robie_job_engine.chat_turn_control import refuse_current_tool_call

    stopped = refuse_current_tool_call(kwargs)
    if stopped:
        return tool_error(stopped)
    from robie_job_engine.write_verification_loop import refuse_tool_write

    refused = refuse_tool_write(args, kwargs)
    if refused:
        return tool_error(refused)
    from robie_job_engine.chat_job_controls import refuse_repeat_note_post

    repeat = refuse_repeat_note_post(args, kwargs)
    if repeat:
        return tool_error(repeat)
    try:
        report = _file_note(args or {})
    except Exception as exc:  # noqa: BLE001 - tool boundary
        message = f"{type(exc).__name__}: {exc}"
        _remember_note_tool_failure(kwargs, message)
        return tool_error(
            message
            + " STOP. Do not drive EZLynx screens by hand. Report this error and stop."
        )
    remembered = dict(kwargs or {})
    remembered["request_note"] = str((args or {}).get("note_text") or "")
    remembered["step_args"] = dict(args or {})
    _remember_discussion_note(remembered, report)
    return tool_result(report)


def _available() -> bool:
    try:
        from robie_job_engine import ezlynx_api_only_writes  # noqa: F401
        return True
    except ImportError:
        return False


registry.register(
    name="ezlynx_discussion_note",
    toolset="ezlynx",
    schema=DISCUSSION_NOTE_SCHEMA,
    handler=ezlynx_discussion_note_handler,
    check_fn=_available,
    emoji="📝",
)
