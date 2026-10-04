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

_LEAD_LABELS = ("robie lead follow up", "robie call follow up")
_ROBIE_CALL = "robie call"
LEAD_WORKFLOW_ID = "lead_follow_up"


@dataclass(frozen=True)
class CallPickup:
    action: str
    workflow_id: str = ""
    reason: str = ""


def _phrase_present(text: str, phrase: str) -> bool:
    return re.search(
        r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.IGNORECASE,
    ) is not None


def _label_parts(activity_labels: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[,;|]", activity_labels or "")
        if part.strip()
    ]


def _normalize_label(value: str) -> str:
    text = (value or "").casefold().replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def classify_call_request(activity_labels: str, note_text: str) -> CallPickup:
    """Resolve a check-in row to a lead script, a free-form call, or neither.

    "Robie Lead Follow Up" and "Robie Call Follow Up" are the lead script.
    A Robie Call label with no lead label is free-form: Eva follows the
    task description. Audit, sales, winback, and the other Splice names
    do not start a call.
    """
    labels = [_normalize_label(part) for part in _label_parts(activity_labels)]
    note = _normalize_label(note_text)
    lead = any(label in _LEAD_LABELS for label in labels) or any(
        _phrase_present(note, phrase) for phrase in _LEAD_LABELS
    )
    if lead:
        return CallPickup("workflow", workflow_id=LEAD_WORKFLOW_ID)
    robie_call = any(label == _ROBIE_CALL for label in labels) or _phrase_present(
        note, _ROBIE_CALL,
    )
    if robie_call:
        return CallPickup("freeform")
    return CallPickup("not_labeled")


def note_dedupe_key(
    applicant_id: str, discussion_id: str, note_text: str, task_id: str,
) -> str:
    basis = (note_text or "").strip().casefold() or (task_id or "").strip()
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"{applicant_id}:{discussion_id}:{digest}"


def calling_day(moment: datetime) -> str:
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
