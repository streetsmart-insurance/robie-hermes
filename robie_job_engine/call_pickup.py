"""Decide which Task Check-In labels place a call.

Only two labels dial. "Robie Call" uses the free-form handler. "Robie
Lead Follow Up" (and the alias "Robie Call Follow Up") uses the lead
script. The nine Splice workflow names are not labels. Each note is
claimed at most once per America/New_York day.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Exact keys after case-folding and stripping hyphens, spaces, and
# underscores. "Robie Call" must not match as a prefix of
# "Robie Call Follow Up".
_LEAD_KEYS = frozenset({"robieleadfollowup", "robiecallfollowup"})
_CALL_KEY = "robiecall"
LEAD_WORKFLOW_ID = "lead_follow_up"


@dataclass(frozen=True)
class CallPickup:
    action: str
    workflow_id: str = ""
    reason: str = ""


def _label_parts(activity_labels: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[,;|]", activity_labels or "")
        if part.strip()
    ]


def label_key(value: str) -> str:
    """Case-fold and drop hyphens, spaces, and underscores."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def classify_call_request(activity_labels: str, note_text: str = "") -> CallPickup:
    """Resolve a check-in row to a lead script, a free-form call, or neither.

    The live label is "Robie lead follow-up". "Robie Lead Follow Up" and
    "Robie Call Follow Up" are the same lead script. A label matches only
    when the full normalized text is exactly that label, so "Robie Call"
    does not swallow "Robie Call Follow Up". Audit, sales, winback, and
    the other Splice names do not start a call.

    ``note_text`` is ignored. A title or description that says "call",
    "Do not call", or "[CALLBACK REQUIRED]" does not dial.
    """
    del note_text
    keys = [label_key(part) for part in _label_parts(activity_labels)]
    if any(key in _LEAD_KEYS for key in keys):
        return CallPickup("workflow", workflow_id=LEAD_WORKFLOW_ID)
    if any(key == _CALL_KEY for key in keys):
        return CallPickup("freeform")
    return CallPickup("not_labeled")


def note_dedupe_key(
    applicant_id: str, discussion_id: str, note_text: str, task_id: str,
) -> str:
    basis = (note_text or "").strip().casefold() or (task_id or "").strip()
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"{applicant_id}:{discussion_id}:{digest}"


def calling_day(moment: datetime) -> str:
    """Eastern calendar day.

    ``moment`` is the call clock or a Created Date that has already been
    converted from America/Chicago. A naive value is Eastern, not Central.
    Report strings go through report_created_et before they reach here.
    """
    zone = ZoneInfo("America/New_York")
    if moment.tzinfo is None:
        current = moment.replace(tzinfo=zone)
    else:
        current = moment.astimezone(zone)
    return current.date().isoformat()


class CallDedupeStore:
    """One claim per note per America/New_York day."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_note_dedupe (
                    note_key TEXT NOT NULL,
                    call_day TEXT NOT NULL,
                    PRIMARY KEY (note_key, call_day)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def already_called(self, note_key: str, day: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM call_note_dedupe WHERE note_key = ? AND call_day = ?",
                (note_key, day),
            ).fetchone()
        return row is not None

    def record(self, note_key: str, day: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO call_note_dedupe (note_key, call_day)
                VALUES (?, ?)
                ON CONFLICT(note_key, call_day) DO NOTHING
                """,
                (note_key, day),
            )
