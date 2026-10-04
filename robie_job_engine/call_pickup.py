"""Map a Task Check-In row onto one of the nine call workflows.

Activity Labels and the note text use the same names as the phone label
runner. "Robie Call" by itself is not a workflow. Each note is claimed
at most once per America/New_York day.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Longer phrases first so "sales center reviewed status" wins as one id.
_WORKFLOW_PHRASES: tuple[tuple[str, str], ...] = (
    ("sales center reviewed status", "sales_center_reviewed"),
    ("additional information follow-up", "additional_information_follow_up"),
    ("recommendations follow-up", "recommendations_follow_up"),
    ("robie renewal reach-out", "renewal_reach_out"),
    ("e-signature follow-up", "esignature_follow_up"),
    ("renewal reach-out", "renewal_reach_out"),
    ("renewal reach out", "renewal_reach_out"),
    ("robie additional info", "additional_information_follow_up"),
    ("robie recommendations", "recommendations_follow_up"),
    ("robie returned mail", "returned_mail"),
    ("robie sales center", "sales_center_reviewed"),
    ("winback campaign", "winback_campaign"),
    ("audit not complete", "audit_not_complete"),
    ("robie unresponsive", "unresponsive"),
    ("robie winback", "winback_campaign"),
    ("sales center", "sales_center_reviewed"),
    ("robie e-sign", "esignature_follow_up"),
    ("returned mail", "returned_mail"),
    ("robie audit", "audit_not_complete"),
    ("winback", "winback_campaign"),
)

_ROBIE_CALL_PHRASE = "robie call"


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


def classify_call_request(activity_labels: str, note_text: str) -> CallPickup:
    """Resolve a check-in row to a workflow, a skip, or neither.

    A bare Robie Call label does not invent a script. Two workflows on
    the same row are a conflict and are not dialed.
    """
    found: set[str] = set()
    labels = _label_parts(activity_labels)
    haystack = " ".join(labels + [note_text or ""])
    for phrase, workflow_id in _WORKFLOW_PHRASES:
        if _phrase_present(haystack, phrase):
            found.add(workflow_id)
    robie_call = any(part.casefold() == _ROBIE_CALL_PHRASE for part in labels) or (
        _phrase_present(note_text or "", _ROBIE_CALL_PHRASE)
    )
    if len(found) > 1:
        return CallPickup(
            "skip_conflict",
            reason="more than one call workflow on this note; not dialed",
        )
    if len(found) == 1:
        return CallPickup("workflow", workflow_id=next(iter(found)))
    if robie_call:
        return CallPickup(
            "skip_unscripted",
            reason="Robie Call has no workflow; no script was used",
        )
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
