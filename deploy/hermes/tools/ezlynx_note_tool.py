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
    folded = note_text.casefold()
    if "robie was here" not in folded:
        note_text = note_text + "\n\nRobie was here"
    filed = add_note_to_discussion(
        applicant_id,
        note_text,
        title_hint=title_hint,
        discussion_title=title_hint,
    )
    if filed.get("status") != "filed":
        raise RuntimeError(
            f"DiscussionApi did not file the note: {filed.get('reason') or filed.get('status')}"
        )
    if not filed.get("note_id"):
        raise RuntimeError("DiscussionApi filed without note_id; refusing success")
    return {
        "ok": True,
        "note_id": filed.get("note_id"),
        "discussion_id": filed.get("discussion_id"),
        "discussion_title": filed.get("discussion_title"),
        "applicant_id": applicant_id,
        "read_back": True,
    }


def ezlynx_discussion_note_handler(args: dict, **kwargs):
    try:
        report = _file_note(args or {})
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return tool_error(f"{type(exc).__name__}: {exc}")
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
