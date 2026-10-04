"""Local memory of discussion notes that were already accepted.

EZLynx DiscussionApi does not return note text on ``GET v8/discussions/{id}``,
and ``GET v8/discussions/{id}/notes`` answers HTTP 405. A text match cannot
tell a rerun that a note is already there, so a retry would post it again.

This ledger is local only. It never calls EZLynx and never creates a
discussion. The key is the applicant, the discussion, and a hash of the note
text plus the document id when there is one.

A note that was sent but could not be confirmed is stored as
``sent, unconfirmed``. That row blocks another post until a person says yes
after reviewing the uncertain outcome. Elapsed time cannot authorize a retry. A write that does not land
is a failure: the caller must not post.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LEDGER_VERSION = 1
LEDGER_FILENAME = "discussion-note-ledger.json"
PROD_LEDGER_PATH = Path(
    "/opt/streetsmart-hermes/robie-job-engine/data/discussion-note-ledger.json"
)
_PRODUCTION_ENVS = frozenset({"PRODUCTION", "PROD", "LIVE"})

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


_LEDGER_LOCKS: dict[str, threading.RLock] = {}
_LEDGER_LOCKS_GUARD = threading.Lock()
_LEDGER_LOCK_DEPTH = threading.local()


@contextmanager
def serialized_ledger(ledger_path=None):
    """Serialize reservation, POST and confirmation across threads/processes.

    Lock the stable sibling file, never the atomically replaced JSON inode.
    Nested ledger operations reuse the outer lock. A wait timeout fails closed.
    """
    import fcntl

    path = resolve_ledger_path(ledger_path).expanduser().resolve()
    key = str(path)
    with _LEDGER_LOCKS_GUARD:
        lock = _LEDGER_LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=30):
        raise DiscussionNoteLedgerError("The note ledger is busy; nothing was sent.")
    depths = getattr(_LEDGER_LOCK_DEPTH, "paths", None)
    if depths is None:
        depths = _LEDGER_LOCK_DEPTH.paths = {}
    handle = None
    try:
        if not depths.get(key):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.with_name(path.name + ".lock").open("a+b")
                deadline = time.monotonic() + 30
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise DiscussionNoteLedgerError("The note ledger is busy; nothing was sent.")
                        time.sleep(0.02)
            except OSError as exc:
                raise DiscussionNoteLedgerError("The note ledger cannot be locked; nothing was sent.") from exc
        depths[key] = depths.get(key, 0) + 1
        try:
            yield
        finally:
            depths[key] -= 1
    finally:
        if handle is not None:
            handle.close()
        lock.release()


def with_serialized_ledger(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with serialized_ledger(kwargs.get("ledger_path")):
            return function(*args, **kwargs)
    return wrapped


NOTE_REPEAT_HOURS = 24
SENT_UNCONFIRMED = "sent, unconfirmed"
CONFIRMED = "confirmed"
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
    """The six notes already posted on 2026-09-30, with no note id on file.

    These rows are not treated as sent unless ``--record-known`` wrote them
    into the ledger file, or this process is running with ``ROBIE_ENV=TEST``.
    """

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


def _is_production_process() -> bool:
    env_name = str(os.environ.get("ROBIE_ENV") or "").strip().upper()
    if env_name in _PRODUCTION_ENVS:
        return True
    host = socket.gethostname().split(".")[0].strip().lower()
    return host == "hermes-poc-01"


def default_ledger_path() -> Path:
    """Where accepted notes are remembered. Tests never use the repo tree.

    Production always uses the ledger next to the job database on
    hermes-poc-01. A missing file is not created here.
    """

    override = str(os.environ.get("ROBIE_DISCUSSION_NOTE_LEDGER") or "").strip()
    if _under_automated_test():
        if override:
            return Path(override)
        return _isolated_test_ledger_path()
    if _is_production_process():
        return PROD_LEDGER_PATH
    if override:
        return Path(override)
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


def _compact_policy(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


_AMOUNT_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")
_DUE_RE = re.compile(
    r"(?:which was due on|was due on|was due|due on)\s+(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
_INVOICE_RE = re.compile(
    r"Invoice(?:\s+No\.?)?\s+([A-Za-z0-9-]{4,})",
    re.IGNORECASE,
)


def notice_anchors(text: str) -> dict[str, list[str]]:
    """Amounts, due dates, and invoice numbers named in a notice or a note."""
    amounts: list[str] = []
    dues: list[str] = []
    invoices: list[str] = []
    seen_amounts: set[str] = set()
    seen_dues: set[str] = set()
    seen_invoices: set[str] = set()
    for match in _AMOUNT_RE.finditer(str(text or "")):
        token = match.group(1).replace(",", "")
        if token not in seen_amounts:
            seen_amounts.add(token)
            amounts.append(token)
    for match in _DUE_RE.finditer(str(text or "")):
        token = match.group(1)
        if token not in seen_dues:
            seen_dues.add(token)
            dues.append(token)
    for match in _INVOICE_RE.finditer(str(text or "")):
        token = match.group(1).strip().upper()
        if token and token not in seen_invoices:
            seen_invoices.add(token)
            invoices.append(token)
    return {"amounts": amounts, "due_dates": dues, "invoice_numbers": invoices}


def anchors_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when this is the same bill, not merely the same dollar amount.

    A due date or invoice number that both sides name and that differs
    rules the match out. The same due date with a different amount is a
    different bill. The amount counts by itself only when neither side
    has a due date and neither side has an invoice number.
    """
    left_dues = {str(item) for item in (left.get("due_dates") or []) if str(item)}
    right_dues = {str(item) for item in (right.get("due_dates") or []) if str(item)}
    left_invoices = {
        str(item).upper() for item in (left.get("invoice_numbers") or []) if str(item)
    }
    right_invoices = {
        str(item).upper() for item in (right.get("invoice_numbers") or []) if str(item)
    }
    left_amounts = {str(item) for item in (left.get("amounts") or []) if str(item)}
    right_amounts = {str(item) for item in (right.get("amounts") or []) if str(item)}
    if left_amounts and right_amounts and not left_amounts.intersection(right_amounts):
        return False
    if left_dues and right_dues and not left_dues.intersection(right_dues):
        return False
    if left_invoices and right_invoices and not left_invoices.intersection(right_invoices):
        return False
    if (
        not left_dues
        and not right_dues
        and not left_invoices
        and not right_invoices
    ):
        return bool(left_amounts.intersection(right_amounts))
    return bool(
        left_dues.intersection(right_dues) or left_invoices.intersection(right_invoices)
    )


def _row_anchors(row: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "amounts": [str(item) for item in (row.get("amounts") or []) if str(item)],
        "due_dates": [str(item) for item in (row.get("due_dates") or []) if str(item)],
        "invoice_numbers": [
            str(item).upper() for item in (row.get("invoice_numbers") or []) if str(item)
        ],
    }


@with_serialized_ledger
def remember_notice_filing(
    applicant_id: str,
    discussion_id: str,
    *,
    policy_numbers: list[str],
    notice_type: str,
    note_id: str = "",
    ledger_path: Path | str | None = None,
    posted_at: str = "",
    notice_text: str = "",
    amounts: list[str] | None = None,
    due_dates: list[str] | None = None,
    invoice_numbers: list[str] | None = None,
) -> dict[str, Any] | None:
    """Remember a posted Ascend note by policy, type, and this bill's anchors.

    A later month on the same policy is a different row. The email driver
    does not call this: its unit cannot write the shared ledger. The API
    poller does, after it has posted. Does not call EZLynx.
    """

    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    kind = str(notice_type or "").strip()
    numbers = [
        _compact_policy(number)
        for number in policy_numbers
        if len(_compact_policy(number)) >= 4
    ]
    if not applicant or not discussion or not kind or not numbers:
        return None
    extracted = notice_anchors(notice_text)
    remembered_amounts = list(amounts) if amounts is not None else list(extracted["amounts"])
    remembered_dues = list(due_dates) if due_dates is not None else list(extracted["due_dates"])
    remembered_invoices = [
        str(item).upper()
        for item in (
            invoice_numbers if invoice_numbers is not None else extracted["invoice_numbers"]
        )
        if str(item or "").strip()
    ]
    fresh = {
        "amounts": remembered_amounts,
        "due_dates": remembered_dues,
        "invoice_numbers": remembered_invoices,
    }
    path = resolve_ledger_path(ledger_path)
    payload = _read_file(path)
    notes = [item for item in payload.get("notes") or [] if isinstance(item, dict)]
    stamp = str(posted_at or "").strip() or _utc_now()
    for row in notes:
        if str(row.get("source") or "") != "notice-filing":
            continue
        if str(row.get("applicant_id") or "") != applicant:
            continue
        if str(row.get("discussion_id") or "") != discussion:
            continue
        if str(row.get("notice_type") or "") != kind:
            continue
        stored = {
            _compact_policy(number)
            for number in (row.get("policy_numbers") or [])
            if _compact_policy(number)
        }
        if not stored.intersection(numbers):
            continue
        stored_anchors = _row_anchors(row)
        if not any(stored_anchors.values()) and not any(fresh.values()):
            return dict(row)
        if anchors_overlap(stored_anchors, fresh):
            return dict(row)
    row = {
        "applicant_id": applicant,
        "discussion_id": discussion,
        "document_id": "",
        "note_text_sha256": "",
        "note_norm_sha256": "",
        "note_id": str(note_id or "").strip(),
        "posted_at": stamp,
        "source": "notice-filing",
        "confirmation": CONFIRMED,
        "notice_type": kind,
        "policy_numbers": numbers,
        "amounts": remembered_amounts,
        "due_dates": remembered_dues,
        "invoice_numbers": remembered_invoices,
    }
    notes.append(row)
    payload["version"] = LEDGER_VERSION
    payload["notes"] = notes
    _save_file(path, payload)
    return row


def find_recent_notice_filing(
    applicant_id: str,
    discussion_id: str,
    policy_numbers: list[str],
    notice_type: str,
    *,
    within_days: int = 30,
    ledger_path: Path | str | None = None,
    now: datetime | None = None,
    not_before: datetime | None = None,
    notice_text: str = "",
    amounts: list[str] | None = None,
    due_dates: list[str] | None = None,
    invoice_numbers: list[str] | None = None,
) -> dict[str, Any] | None:
    """A notice already posted for this same bill, not merely this policy.

    The row must be posted on or after ``not_before`` (the ready row's
    first_seen) and share the due date, the amount, or the invoice number.
    A prior month's late notice does not match. Rows with no anchors do
    not match.
    """

    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    kind = str(notice_type or "").strip()
    wanted_policies = {
        _compact_policy(number)
        for number in policy_numbers
        if len(_compact_policy(number)) >= 4
    }
    if not_before is None or not applicant or not discussion or not kind or not wanted_policies:
        return None
    floor = not_before
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=timezone.utc)
    floor = floor.astimezone(timezone.utc)
    extracted = notice_anchors(notice_text)
    wanted = {
        "amounts": list(amounts) if amounts is not None else list(extracted["amounts"]),
        "due_dates": list(due_dates) if due_dates is not None else list(extracted["due_dates"]),
        "invoice_numbers": [
            str(item).upper()
            for item in (
                invoice_numbers if invoice_numbers is not None else extracted["invoice_numbers"]
            )
            if str(item or "").strip()
        ],
    }
    if not any(wanted.values()):
        return None
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    clock = clock.astimezone(timezone.utc)
    window = timedelta(days=within_days)
    for row in _rows(ledger_path):
        if str(row.get("source") or "") != "notice-filing":
            continue
        if str(row.get("applicant_id") or "") != applicant:
            continue
        if str(row.get("discussion_id") or "") != discussion:
            continue
        if str(row.get("notice_type") or "") != kind:
            continue
        stored = {
            _compact_policy(number)
            for number in (row.get("policy_numbers") or [])
            if _compact_policy(number)
        }
        if not stored.intersection(wanted_policies):
            continue
        posted = _parse_stamp(row.get("posted_at"))
        if posted is None or posted < floor:
            continue
        age = clock - posted
        if age < timedelta(0) or age > window:
            continue
        if not anchors_overlap(wanted, _row_anchors(row)):
            continue
        return dict(row)
    return None


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
        if note_was_unconfirmed(row):
            return dict(row)
        posted = _parse_stamp(row.get("posted_at"))
        if posted is None:
            continue
        if timedelta(0) <= (clock - posted) <= window:
            return dict(row)
    return None


@with_serialized_ledger
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
    confirmation: str = CONFIRMED,
) -> dict[str, Any]:
    """Remember a note that was already accepted. Does not call EZLynx.

    ``confirmation`` is ``confirmed`` or ``sent, unconfirmed``. A disk error
    raises :class:`DiscussionNoteLedgerError` and the caller must not post.
    """

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
    marked = str(confirmation or CONFIRMED).strip() or CONFIRMED
    existing = _match(applicant, discussion, document, fingerprint, notes)
    if existing is None and norm:
        existing = _match_norm(applicant, discussion, norm, notes)
    if existing is not None:
        if refresh:
            _touch_posted(
                notes,
                existing,
                stamp,
                str(note_id or "").strip(),
                norm,
                confirmation=marked,
                source=str(source or ""),
            )
            payload["notes"] = notes
            _save_file(path, payload)
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
        "confirmation": marked,
    }
    notes.append(row)
    payload["version"] = LEDGER_VERSION
    payload["notes"] = notes
    _save_file(path, payload)
    return row


@with_serialized_ledger
def begin_unconfirmed_note(
    applicant_id: str,
    discussion_id: str,
    *,
    note_text: str = "",
    document_id: str = "",
    ledger_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Remember a note as sent-but-unconfirmed before it is posted.

    Returns the file row this replaces, so a post that never left can be
    undone. Raises when the file cannot be written. The caller must not post
    in that case.
    """

    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    document = str(document_id or "").strip()
    text = str(note_text or "")
    path = resolve_ledger_path(ledger_path)
    payload = _read_file(path)
    notes = [item for item in payload.get("notes") or [] if isinstance(item, dict)]
    fingerprint = note_fingerprint(text, document) if text.strip() else ""
    norm = note_norm_fingerprint(text) if text.strip() else ""
    previous = _match(applicant, discussion, document, fingerprint, notes)
    if previous is None and norm:
        previous = _match_norm(applicant, discussion, norm, notes)
    record_posted_note(
        applicant,
        discussion,
        note_text=text,
        document_id=document,
        source="sent_unconfirmed",
        ledger_path=path,
        refresh=True,
        confirmation=SENT_UNCONFIRMED,
    )
    return dict(previous) if previous else None


@with_serialized_ledger
def undo_unconfirmed_note(
    applicant_id: str,
    discussion_id: str,
    *,
    note_text: str = "",
    document_id: str = "",
    previous: dict[str, Any] | None = None,
    ledger_path: Path | str | None = None,
) -> None:
    """Remove a sent-unconfirmed row after a post that did not happen.

    When ``previous`` is set, that row is put back in its place.
    """

    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    document = str(document_id or "").strip()
    text = str(note_text or "")
    path = resolve_ledger_path(ledger_path)
    payload = _read_file(path)
    notes = [item for item in payload.get("notes") or [] if isinstance(item, dict)]
    fingerprint = note_fingerprint(text, document) if text.strip() else ""
    norm = note_norm_fingerprint(text) if text.strip() else ""
    kept: list[dict[str, Any]] = []
    for row in notes:
        if not _same_unconfirmed_target(
            row,
            applicant,
            discussion,
            document,
            fingerprint,
            norm,
        ):
            kept.append(row)
    if previous:
        kept.append(dict(previous))
    payload["version"] = LEDGER_VERSION
    payload["notes"] = kept
    _save_file(path, payload)


def note_was_unconfirmed(row: dict[str, Any] | None) -> bool:
    """True when this row was sent and the readback did not confirm it."""

    if not isinstance(row, dict):
        return False
    return str(row.get("confirmation") or "").strip() == SENT_UNCONFIRMED


def note_still_blocks_repost(
    row: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    within_hours: int = NOTE_REPEAT_HOURS,
) -> bool:
    """Uncertainty does not expire; only explicit review can permit a repost."""
    return note_was_unconfirmed(row)


def record_known_posted_notes(ledger_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Copy the six already-posted notes into the ledger file. Does not post.

    Production never does this. Those six notes were accepted on Test.
    """

    if _is_production_process():
        raise DiscussionNoteLedgerError(
            "Known Test notes are not copied onto Production. Nothing was written."
        )
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


def _same_unconfirmed_target(
    row: dict[str, Any],
    applicant: str,
    discussion: str,
    document: str,
    fingerprint: str,
    norm: str,
) -> bool:
    if str(row.get("confirmation") or "") != SENT_UNCONFIRMED:
        return False
    if str(row.get("applicant_id") or "") != applicant:
        return False
    if str(row.get("discussion_id") or "") != discussion:
        return False
    stored_document = str(row.get("document_id") or "").strip()
    stored_fingerprint = str(row.get("note_text_sha256") or "").strip()
    stored_norm = str(row.get("note_norm_sha256") or "").strip()
    if document and stored_document and stored_document == document:
        return True
    if fingerprint and stored_fingerprint and stored_fingerprint == fingerprint:
        return True
    if norm and stored_norm and stored_norm == norm:
        return True
    return False


def _touch_posted(
    notes: list[dict[str, Any]],
    existing: dict[str, Any],
    stamp: str,
    note_id: str,
    norm: str,
    *,
    confirmation: str = "",
    source: str = "",
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
        if confirmation:
            row["confirmation"] = confirmation
        if source:
            row["source"] = source
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


def _known_notes_apply() -> bool:
    """The six hard-coded Test ids count only while ROBIE_ENV is TEST.

    ``--record-known`` writes those ids into the ledger file, and that file
    is read in every environment. This injection is the other gate.
    """

    from .runtime_env import TEST_ENV_NAME, current_robie_env

    return current_robie_env() == TEST_ENV_NAME


def _rows(ledger_path: Path | str | None) -> list[dict[str, Any]]:
    rows = known_posted_notes() if _known_notes_apply() else []
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


def _save_file(path: Path, payload: dict[str, Any]) -> None:
    """Write the ledger. A full disk or a locked directory fails closed."""

    try:
        _write_file(path, payload)
    except OSError as exc:
        raise DiscussionNoteLedgerError(
            "The note list could not be saved, so the note was not sent."
        ) from exc


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
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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
        try:
            rows = record_known_posted_notes(args.ledger)
        except DiscussionNoteLedgerError as exc:
            print(json.dumps({"recorded": 0, "posted": False, "error": str(exc)}))
            return 2
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
