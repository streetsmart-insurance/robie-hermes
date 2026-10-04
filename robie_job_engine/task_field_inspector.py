#!/usr/bin/env python3
"""Read-only inspection of the EZLynx task fields, to establish the field contract.

PURPOSE: record what is ACTUALLY on the page and in the discussion record so a person can
declare each consequential field (and what it means) in
`deploy/ezlynx_task_field_contract.json`. This module names no field and guesses no selector:
it enumerates every labelled or textual element it can see, with the label, role, value and
whether it is editable, and leaves the meaning of each one blank for a human to fill in.

READ-ONLY, by construction:
- It never saves, reassigns, posts a note, or clicks anything but the existing identity-checked
  helpers that open the task's Edit dialog and cancel it. (A test asserts the source contains no
  Save, reassign or note-posting call.)
- It refuses unless ROBIE_ENV=TEST, the applicant is the Test account, and the exact task ID is
  listed in ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS.
- The output file is created exclusively (never overwritten) with mode 0600.

It is NOT run by CI or by any scheduler. See docs/EZLYNX_TASK_FIELD_DISCOVERY.md.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from . import ezlynx_task_cdp as cdp

TEST_ACCOUNT = "220250093"
ALLOWED_TASK_IDS_ENV = "ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS"
MAX_ELEMENTS = 400
MAX_VALUE_CHARS = 200

# Pure reads: collect what is visible. No clicks, no input, no navigation.
_COLLECT = """
(root) => {
  const scope = (root && root.querySelectorAll) ? root : document;
  const out = [];
  const seen = new Set();
  const text = (el) => ((el.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, %(max)d);
  const nameOf = (el) => {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria;
    const by = el.getAttribute && el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\\s+/).map(i => { const n = document.getElementById(i); return n ? text(n) : ''; }).join(' ').trim(); if (t) return t; }
    if (el.labels && el.labels.length) return Array.from(el.labels).map(text).join(' ').trim();
    return (el.getAttribute && (el.getAttribute('placeholder') || el.getAttribute('title'))) || '';
  };
  const add = (el) => {
    if (seen.has(el) || out.length >= %(cap)d) return;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const isInput = ['input', 'textarea', 'select'].includes(tag);
    out.push({
      tag, role: el.getAttribute('role') || '', name: nameOf(el),
      text: isInput ? '' : text(el),
      value: isInput ? String(el.value || '').slice(0, %(max)d) : '',
      editable: isInput ? !(el.readOnly || el.disabled) : el.isContentEditable === true,
      visible: !!(el.offsetParent !== null || el.getClientRects().length),
      type: el.getAttribute('type') || '',
      data_attributes: Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name),
    });
  };
  scope.querySelectorAll('input, textarea, select, [role], [aria-label], label, [contenteditable="true"]').forEach(add);
  scope.querySelectorAll('*').forEach(el => { if (!el.children.length && text(el) && text(el).length < 120) add(el); });
  return out;
}
""" % {"max": MAX_VALUE_CHARS, "cap": MAX_ELEMENTS}


class InspectionRefused(Exception):
    """A precondition for a safe, read-only Test inspection does not hold."""


def check_preconditions(task_id: str, applicant_id: str) -> None:
    if os.environ.get("ROBIE_ENV", "").strip().upper() != "TEST":
        raise InspectionRefused("the inspection runs only with ROBIE_ENV=TEST")
    if str(applicant_id).strip() != TEST_ACCOUNT:
        raise InspectionRefused(f"the inspection is limited to the Test account {TEST_ACCOUNT}")
    listed = {part.strip() for part in os.environ.get(ALLOWED_TASK_IDS_ENV, "").split(",")
              if re.fullmatch(r"[1-9][0-9]*", part.strip())}
    if str(task_id).strip() not in listed:
        raise InspectionRefused(f"task {task_id} is not listed in {ALLOWED_TASK_IDS_ENV}")


def _meaning_review() -> dict[str, Any]:
    return {
        "status": "PENDING HUMAN REVIEW",
        "instructions": ("For each required field, a person identifies which observed element carries it, "
                         "what it MEANS, and which report column it corresponds to. Nothing here is inferred."),
        "fields": {name: None for name in cdp.REQUIRED_FIELDS},
    }


def _write_exclusive(output_path: Any, record: dict[str, Any]) -> None:
    fd = os.open(str(output_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")


def run_dom_inspection(*, task_id: str, applicant_id: str, output_path: Any,
                       report_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Open the task's Edit dialog, record every visible element, then Cancel. Never saves."""
    check_preconditions(task_id, applicant_id)
    with cdp._browser_page() as page:
        cdp._goto_activity(page, applicant_id)
        panel = cdp._search_and_open_edit(page, task_id, applicant_id)
        try:
            dialog_elements = list(panel.evaluate(_COLLECT))
            page_elements = list(page.evaluate(_COLLECT))
        finally:
            cdp._cancel_dialog(panel)
    record = {
        "kind": "dom", "environment": "TEST", "applicant_id": str(applicant_id), "task_id": str(task_id),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "dialog_elements": dialog_elements, "page_elements": page_elements,
        "report_row": report_row,
        "note": "Observation only. No selector was guessed and no field was mapped.",
        "meaning_review": _meaning_review(),
    }
    _write_exclusive(output_path, record)
    return record


def _key_paths(value: Any, prefix: str = "", depth: int = 0) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict) and depth < 3:
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(inner, (dict, list)):
                paths |= _key_paths(inner, path, depth + 1)
            else:
                paths.add(path)
    elif isinstance(value, list):
        for item in value[:20]:
            paths |= _key_paths(item, prefix, depth + 1)
    return paths


def run_api_inspection(*, client: Any, task_id: str, applicant_id: str, discussion_id: str,
                       output_path: Any) -> dict[str, Any]:
    """Read ONE discussion and summarize the shape of its notes (key names; values truncated)."""
    check_preconditions(task_id, applicant_id)
    from .ezlynx_discussions import iter_discussion_notes

    ownership = "not checked (the client offers no ownership lookup)"
    lookup = getattr(client, "get_discussion_ids", None)
    if lookup is not None:
        owned = [str(item) for item in lookup(str(applicant_id))]
        if str(discussion_id) not in owned:
            raise InspectionRefused(f"discussion {discussion_id} is not one of applicant {applicant_id}'s")
        ownership = "confirmed"
    record_in = client.get_discussion(str(discussion_id))
    notes = list(iter_discussion_notes(record_in))
    keys: set[str] = set()
    for note in notes:
        keys |= _key_paths(note)

    def clip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clip(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clip(v) for v in value[:5]]
        return str(value)[:MAX_VALUE_CHARS] if isinstance(value, str) else value

    record = {
        "kind": "api", "environment": "TEST", "applicant_id": str(applicant_id), "task_id": str(task_id),
        "discussion_id": str(discussion_id), "discussion_ownership": ownership,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "discussion_keys": sorted(record_in.keys()) if isinstance(record_in, dict) else [],
        "note_count": len(notes), "note_keys": sorted(keys),
        "sample_note": clip(notes[0]) if notes else None,
        "note": "Observation only. No key was assumed to mean anything.",
        "meaning_review": _meaning_review(),
    }
    _write_exclusive(output_path, record)
    return record
