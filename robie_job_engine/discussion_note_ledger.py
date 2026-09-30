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
import sys
import tempfile
from pathlib import Path
from typing import Any


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


def note_fingerprint(note_text: str, document_id: str = "") -> str:
    """Hash of the note text and document id. The raw note is not stored."""

    text = " ".join(str(note_text or "").split())
    document = str(document_id or "").strip()
    return hashlib.sha256(f"{text}\n{document}".encode("utf-8")).hexdigest()


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


def record_posted_note(
    applicant_id: str,
    discussion_id: str,
    *,
    note_text: str = "",
    document_id: str = "",
    note_id: str = "",
    source: str = "recorded_without_post",
    ledger_path: Path | str | None = None,
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
    existing = _match(applicant, discussion, document, fingerprint, notes)
    if existing is not None:
        return existing
    row = {
        "applicant_id": applicant,
        "discussion_id": discussion,
        "document_id": document,
        "note_text_sha256": note_fingerprint(text, document) if text.strip() else "",
        "note_id": str(note_id or "").strip(),
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
