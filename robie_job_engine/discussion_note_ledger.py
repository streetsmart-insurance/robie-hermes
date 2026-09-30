"""Local memory of discussion notes that were already accepted.

EZLynx DiscussionApi does not return note text on ``GET v8/discussions/{id}``,
and ``GET v8/discussions/{id}/notes`` answers HTTP 405. A text match cannot
tell a rerun that a note is already there, so a retry would post it again.

This ledger is local only. It never calls EZLynx, never creates a discussion,
and never deletes anything. The key is the applicant, the discussion, and a
hash of the note text plus the document id when there is one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LEDGER_VERSION = 1
LEDGER_FILENAME = "discussion-note-ledger.json"

# Notes that already posted on hermes-test-01 on 2026-09-30. DiscussionApi
# accepted each post and returned no note id. Recording them here does not
# post them again.
KNOWN_POSTED_APPLICANT_ID = "26356199"
KNOWN_POSTED_DISCUSSION_ID = "819225260"
KNOWN_POSTED_DOCUMENT_IDS = (
    "824463419",
    "824463438",
    "824463451",
    "824463468",
    "824464961",
    "824464983",
)


class DiscussionNoteLedgerError(RuntimeError):
    """The local ledger could not be read. Nothing was sent."""


NOTE_REPEAT_HOURS = 24
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_EASTERN = ZoneInfo("America/New_York")


def note_fingerprint(note_text: str, document_id: str = "") -> str:
    """Hash of the note text and document id. The raw note is not stored."""

    text = " ".join(str(note_text or "").split())
    document = str(document_id or "").strip()
    return hashlib.sha256(f"{text}\n{document}".encode("utf-8")).hexdigest()


def normalize_note_text(note_text: str) -> str:
    """Casefold, collapse whitespace, and strip punctuation."""

    folded = str(note_text or "").casefold()
    stripped = _PUNCT_RE.sub(" ", folded)
    return " ".join(stripped.split())


def note_norm_fingerprint(note_text: str) -> str:
    """Hash of the normalized note text. Empty when the note has no words."""

    text = normalize_note_text(note_text)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def format_posted_at_et(value: Any) -> str:
    """Clock time in Eastern, for the 'already added' question."""

    stamp = _parse_stamp(value)
    if stamp is None:
        return "earlier"
    eastern = stamp.astimezone(_EASTERN)
    hour = eastern.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{eastern.strftime('%M %p')} ET"


def already_added_question(posted_at: Any) -> str:
    """The one line Chat sends instead of posting the same note again."""

    return (
        f"I already added that note at {format_posted_at_et(posted_at)}. "
        "Want me to add it again?"
    )


def known_posted_notes() -> list[dict[str, Any]]:
    """The six notes already posted on 2026-09-30, with no note id on file."""

    rows = []
    for document_id in KNOWN_POSTED_DOCUMENT_IDS:
        rows.append(
            {
                "applicant_id": KNOWN_POSTED_APPLICANT_ID,
                "discussion_id": KNOWN_POSTED_DISCUSSION_ID,
                "document_id": document_id,
                "note_text_sha256": "",
                "note_id": "",
                "source": "hermes-test-01 2026-09-30",
            }
        )
    return rows


def default_ledger_path() -> Path:
    """Where accepted notes are remembered. Tests never use the repo tree."""

    override = str(os.environ.get("ROBIE_DISCUSSION_NOTE_LEDGER") or "").strip()
    if override:
        return Path(override)
    if _under_automated_test():
        return _isolated_test_ledger_path()
    db = str(os.environ.get("ROBIE_JOB_DB") or "").strip()
    if db:
        return Path(db).expanduser().resolve().parent / LEDGER_FILENAME
    return Path("data") / LEDGER_FILENAME


def resolve_ledger_path(ledger_path: Path | str | None = None) -> Path:
    if ledger_path:
        return Path(ledger_path)
    return default_ledger_path()


def find_posted_note(
    applicant_id: str,
    discussion_id: str,
    note_text: str = "",
    *,
    document_id: str = "",
    ledger_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return the accepted row, if this note was already recorded.

    A document id match wins even when the wording differs, so the six notes
    already posted can be remembered without their original text.
    """

    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    document = str(document_id or "").strip()
    if not applicant or not discussion:
        return None
    fingerprint = note_fingerprint(note_text, document) if (note_text or document) else ""
    return _match(applicant, discussion, document, fingerprint, _rows(ledger_path))


def find_recent_same_text(
    applicant_id: str,
    discussion_id: str,
    note_text: str,
    *,
    within_hours: int = NOTE_REPEAT_HOURS,
    ledger_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """A note Robie posted on this discussion with the same text in the window.

    Document-id rows with no text hash are not a text match. Those stay on
    the document download path.
    """

    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    norm = note_norm_fingerprint(note_text)
    legacy = note_fingerprint(note_text, "") if str(note_text or "").strip() else ""
    if not applicant or not discussion or not norm:
        return None
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    window = timedelta(hours=within_hours)
    for row in _rows(ledger_path):
        if str(row.get("applicant_id") or "") != applicant:
            continue
        if str(row.get("discussion_id") or "") != discussion:
            continue
        stored_norm = str(row.get("note_norm_sha256") or "").strip()
        stored_legacy = str(row.get("note_text_sha256") or "").strip()
        if stored_norm != norm and not (legacy and stored_legacy == legacy and not stored_norm):
            continue
        posted = _parse_stamp(row.get("posted_at"))
        if posted is None:
            continue
        if timedelta(0) <= (clock - posted) <= window:
            return dict(row)
    return None


def record_posted_note(
    applicant_id: str,
    discussion_id: str,
    *,
    note_text: str = "",
    document_id: str = "",
    note_id: str = "",
    source: str = "recorded_without_post",
    ledger_path: Path | str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Remember a note that was already accepted. Does not call EZLynx."""

    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    document = str(document_id or "").strip()
    text = str(note_text or "")
    if not applicant or not discussion:
        raise DiscussionNoteLedgerError("A note record needs an applicant and a discussion.")
    if not text.strip() and not document:
        raise DiscussionNoteLedgerError("A note record needs note text or a document id.")
    path = resolve_ledger_path(ledger_path)
    payload = _read_file(path)
    notes = [item for item in payload.get("notes") or [] if isinstance(item, dict)]
    fingerprint = note_fingerprint(text, document) if text.strip() else ""
    norm = note_norm_fingerprint(text) if text.strip() else ""
    stamp = _utc_now()
    existing = _match(applicant, discussion, document, fingerprint, notes)
    if existing is None and norm:
        existing = _match_norm(applicant, discussion, norm, notes)
    if existing is not None:
        if refresh:
            _touch_posted(notes, existing, stamp, str(note_id or "").strip(), norm)
            payload["notes"] = notes
            _write_file(path, payload)
            touched = _match(applicant, discussion, document, fingerprint, notes)
            return touched or existing
        return existing
    row = {
        "applicant_id": applicant,
        "discussion_id": discussion,
        "document_id": document,
        "note_text_sha256": fingerprint,
        "note_norm_sha256": norm,
        "note_id": str(note_id or "").strip(),
        "posted_at": stamp,
        "source": str(source or "recorded_without_post"),
    }
    notes.append(row)
    payload["version"] = LEDGER_VERSION
    payload["notes"] = notes
    _write_file(path, payload)
    return row


def record_known_posted_notes(ledger_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Copy the six already-posted notes into the ledger file. Does not post."""

    recorded = []
    for row in known_posted_notes():
        recorded.append(
            record_posted_note(
                row["applicant_id"],
                row["discussion_id"],
                document_id=row["document_id"],
                source=str(row["source"]),
                ledger_path=ledger_path,
            )
        )
    return recorded


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _match_norm(
    applicant: str,
    discussion: str,
    norm: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("applicant_id") or "") != applicant:
            continue
        if str(row.get("discussion_id") or "") != discussion:
            continue
        if str(row.get("note_norm_sha256") or "").strip() == norm:
            return dict(row)
    return None


def _touch_posted(
    notes: list[dict[str, Any]],
    existing: dict[str, Any],
    stamp: str,
    note_id: str,
    norm: str,
) -> None:
    for row in notes:
        if str(row.get("applicant_id") or "") != str(existing.get("applicant_id") or ""):
            continue
        if str(row.get("discussion_id") or "") != str(existing.get("discussion_id") or ""):
            continue
        same_doc = (
            str(row.get("document_id") or "").strip()
            and str(row.get("document_id") or "").strip() == str(existing.get("document_id") or "").strip()
        )
        same_hash = (
            str(row.get("note_text_sha256") or "").strip()
            and str(row.get("note_text_sha256") or "") == str(existing.get("note_text_sha256") or "")
        )
        same_norm = norm and str(row.get("note_norm_sha256") or "") == norm
        if not (same_doc or same_hash or same_norm):
            continue
        row["posted_at"] = stamp
        if note_id:
            row["note_id"] = note_id
        if norm:
            row["note_norm_sha256"] = norm
        return


def _match(
    applicant: str,
    discussion: str,
    document: str,
    fingerprint: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("applicant_id") or "") != applicant:
            continue
        if str(row.get("discussion_id") or "") != discussion:
            continue
        stored_document = str(row.get("document_id") or "").strip()
        if document and stored_document and stored_document == document:
            return dict(row)
        stored_fingerprint = str(row.get("note_text_sha256") or "").strip()
        if fingerprint and stored_fingerprint and stored_fingerprint == fingerprint:
            return dict(row)
    return None


def _rows(ledger_path: Path | str | None) -> list[dict[str, Any]]:
    rows = known_posted_notes()
    payload = _read_file(resolve_ledger_path(ledger_path))
    for item in payload.get("notes") or []:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _read_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": LEDGER_VERSION, "notes": []}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscussionNoteLedgerError(
            "The saved note list could not be read, so nothing was sent."
        ) from exc
    if not isinstance(parsed, dict):
        raise DiscussionNoteLedgerError(
            "The saved note list could not be read, so nothing was sent."
        )
    return parsed


def _write_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _under_automated_test() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    command = " ".join(sys.argv).lower()
    return "pytest" in command or "unittest" in command


def _isolated_test_ledger_path() -> Path:
    import inspect
    import unittest

    token = str(os.environ.get("PYTEST_CURRENT_TEST") or "")
    if not token:
        for frame in inspect.stack():
            self_obj = frame.frame.f_locals.get("self")
            if isinstance(self_obj, unittest.TestCase):
                token = self_obj.id()
                break
    token = f"{os.getpid()}:{token or 'process'}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]
    root = Path(__file__).resolve().parents[1] / ".robie-durable-test" / "discussion-note-ledger"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.json"


def main(argv: list[str] | None = None) -> int:
    """Record notes that already posted. This command never calls EZLynx."""

    parser = argparse.ArgumentParser(
        description="Remember discussion notes that were already accepted. Does not post."
    )
    parser.add_argument("--ledger", required=True, help="local ledger file to write")
    parser.add_argument(
        "--record-known",
        action="store_true",
        help="record the six notes already posted on 2026-09-30",
    )
    parser.add_argument("--applicant", default="")
    parser.add_argument("--discussion", default="")
    parser.add_argument("--document-id", default="")
    parser.add_argument("--note-text", default="")
    parser.add_argument("--note-id", default="")
    args = parser.parse_args(argv)
    if args.record_known:
        rows = record_known_posted_notes(args.ledger)
        print(json.dumps({"recorded": len(rows), "posted": False}))
        return 0
    if not args.applicant or not args.discussion or not (args.document_id or args.note_text):
        parser.error("pass --record-known, or an applicant, discussion, and note text or document id")
    record_posted_note(
        args.applicant,
        args.discussion,
        note_text=args.note_text,
        document_id=args.document_id,
        note_id=args.note_id,
        ledger_path=args.ledger,
    )
    print(json.dumps({"recorded": 1, "posted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
