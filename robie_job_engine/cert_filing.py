"""Certificates chunk 3: filing verified requests into EZLynx.

Pipeline per VERIFIED record (read-only until the write steps):
  1. Claim a per-message lease (concurrent workers never double-file).
  2. Resolve the discussion: task registry -> existing holder-matching
     certificates discussion -> HOLD (never create, never guess).
  3. Triple filing guard (verify_filing_target) before every write.
  4. Append the summary note (real content via summarize_for_note).
     On an uncertain POST outcome, read the destination back BEFORE any
     retry — never blind-retry a non-idempotent POST.
  5. Upload the full original email as a PDF, then every attachment, via
     the OAuth DocumentApi, each with read-back. On an uncertain upload
     outcome, search Documents by name BEFORE any re-send: landed means
     done, verifiably absent means one re-send, unsearchable means
     UNVERIFIED (fail closed, email stays unread).
  6. Task decision via the registry + Zapier (create/reuse/reopen/hold).
     No task is created when the filing is UNVERIFIED.

Writes go through the injected ports so offline tests use fakes; production
wiring passes add_note_to_discussion / upload_document_via_api from
ezlynx_api_only_writes. The writers enforce the EZLynx write allowlist.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .cert_email_pdf import email_pdf_document
from .cert_intake import summarize_for_note
from .cert_task_registry import (
    CREATE, HOLD, NONE, REOPEN, REUSE, TASK_OPEN,
    TaskEntry, decide_task_action, holder_key_for, policy_key_for,
)
from .cert_verification import (
    ACTION_ACK, ACTION_NEW_REQUEST, VERIFIED,
    FilingTargetMismatch, verify_applicant_anchor, verify_filing_target,
)
from .ezlynx_discussions import create_discussion_with_note

FILED = "FILED"
DRY_RUN = "DRY_RUN"
HELD = "HELD"
PARTIAL = "PARTIAL"
ERROR = "ERROR"

# Auto-created discussions are reused for follow-up requests for the same
# applicant + normalized holder indefinitely (Carlo 2026-09-29): a later
# email about the same client and holder appends to the existing discussion
# instead of creating a duplicate. Reuse stays safe because the discussion
# is GET-verified in EZLynx before reuse; a discussion that no longer reads
# back is never reused.


@dataclass
class FilingDeps:
    discussions_client: Any = None   # get_discussions(applicant_id)
    verifier: Any = None             # search_policies/policy anchor reads
    note_writer: Any = None          # (applicant_id, note_text, *, title_hint, dry_run) -> dict
    doc_writer: Any = None           # (applicant_id, document_name, file_bytes, *, filename, content_type) -> dict
    doc_searcher: Any = None         # (applicant_id) -> {"results": [...]} — read-only,
                                     # used ONLY to resolve uncertain upload outcomes
    zapier: Any = None               # CertZapierClient (or fake)
    registry: Any = None             # TaskRegistry
    store: Any = None                # FilingStore
    task_prover: Any = None          # (applicant_id, task_title) -> {"task_id": str, "assignee": str}
                                     # Proves a certificate-review task exists in EZLynx and is
                                     # assigned to SCanales. Read-only. When None (not yet
                                     # implemented), task creation fails closed as UNVERIFIED.
    callback_store: Any = None       # CallbackStore (cert_callback): pending
                                     # Zap fires + validated callbacks. Gates
                                     # _task_step: a proven callback completes
                                     # without re-firing; a pending fire
                                     # HOLDs instead of risking a duplicate.


@dataclass
class FilingResult:
    status: str = HELD
    discussion_id: str | None = None
    note_id: str | None = None
    document_ids: list[str] = field(default_factory=list)
    task_action: str = "not_reached"
    task_id: str | None = None
    evidence: list[str] = field(default_factory=list)
    hold_reasons: list[str] = field(default_factory=list)


class FilingStore:
    """Per-message crash-safe filing state + worker leases."""

    def __init__(self, db_path: str) -> None:
        self._db = sqlite3.connect(db_path, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cert_filing (
                message_id TEXT PRIMARY KEY,
                note_status TEXT NOT NULL DEFAULT 'pending',
                note_id TEXT,
                discussion_id TEXT,
                thread_id TEXT,
                docs_json TEXT NOT NULL DEFAULT '{}',
                task_status TEXT NOT NULL DEFAULT 'pending',
                task_id TEXT,
                lease_owner TEXT,
                lease_expires REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )"""
        )
        # Migration for databases created before thread_id existed.
        cols = {row[1] for row in
                self._db.execute("PRAGMA table_info(cert_filing)").fetchall()}
        if "thread_id" not in cols:
            self._db.execute("ALTER TABLE cert_filing ADD COLUMN thread_id TEXT")
        # Ledger of discussions the sweep auto-created (Carlo 2026-09-28:
        # no human gate). One row per (applicant, normalized holder): a
        # follow-up request for the same holder reuses the discussion
        # indefinitely instead of creating a duplicate.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cert_auto_discussions (
                applicant_id TEXT NOT NULL,
                holder_norm TEXT NOT NULL DEFAULT '',
                discussion_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                PRIMARY KEY (applicant_id, holder_norm)
            )"""
        )
        self._db.commit()

    def claim(self, message_id: str, owner: str, ttl_s: int = 600,
              thread_id: str | None = None) -> bool:
        """Take the lease for a message. False = another worker holds it."""
        now = time.time()
        row = self._db.execute(
            "SELECT lease_owner, lease_expires FROM cert_filing WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if row and row[0] != owner and row[1] > now:
            return False
        self._db.execute(
            """INSERT INTO cert_filing (message_id, thread_id, lease_owner, lease_expires, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET
                thread_id=COALESCE(excluded.thread_id, cert_filing.thread_id),
                lease_owner=excluded.lease_owner,
                lease_expires=excluded.lease_expires,
                updated_at=excluded.updated_at""",
            (message_id, thread_id, owner, now + ttl_s, now),
        )
        self._db.commit()
        return True

    def release(self, message_id: str, owner: str) -> None:
        self._db.execute(
            "UPDATE cert_filing SET lease_owner=NULL, lease_expires=0,"
            " updated_at=? WHERE message_id=? AND lease_owner=?",
            (time.time(), message_id, owner),
        )
        self._db.commit()

    def get(self, message_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT note_status, note_id, discussion_id, docs_json, task_status,"
            " task_id FROM cert_filing WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if not row:
            return {"note_status": "pending", "note_id": None,
                    "discussion_id": None, "docs_json": "{}",
                    "task_status": "pending", "task_id": None}
        return {"note_status": row[0], "note_id": row[1],
                "discussion_id": row[2], "docs_json": row[3],
                "task_status": row[4], "task_id": row[5]}

    def set(self, message_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._db.execute(
            f"UPDATE cert_filing SET {cols}, updated_at=? WHERE message_id=?",
            (*fields.values(), time.time(), message_id),
        )
        self._db.commit()

    # -- auto-created discussion ledger ------------------------------------

    def thread_discussion(self, thread_id: str,
                         exclude_message_id: str | None = None) -> str | None:
        """Newest discussion filed for another message in the same Gmail
        thread, or None. Lets a follow-up reuse the thread's discussion."""
        row = self._db.execute(
            """SELECT discussion_id FROM cert_filing
               WHERE thread_id=? AND message_id != COALESCE(?, '')
                 AND discussion_id IS NOT NULL AND discussion_id != ''
               ORDER BY updated_at DESC LIMIT 1""",
            (thread_id, exclude_message_id),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def auto_discussion_get(self, applicant_id: Any,
                            holder_norm: str) -> dict[str, Any] | None:
        """Ledger row for (applicant, normalized holder), or None."""
        row = self._db.execute(
            """SELECT discussion_id, title, created_at
               FROM cert_auto_discussions
               WHERE applicant_id=? AND holder_norm=?""",
            (str(applicant_id), holder_norm or ""),
        ).fetchone()
        if not row:
            return None
        return {"discussion_id": str(row[0]), "title": str(row[1] or ""),
                "created_at": float(row[2] or 0)}

    def auto_discussion_record(self, applicant_id: Any, holder_norm: str,
                               discussion_id: str, title: str) -> None:
        """Upsert the auto-created discussion for (applicant, holder)."""
        self._db.execute(
            """INSERT INTO cert_auto_discussions
                 (applicant_id, holder_norm, discussion_id, title, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(applicant_id, holder_norm) DO UPDATE SET
                 discussion_id=excluded.discussion_id,
                 title=excluded.title,
                 created_at=excluded.created_at""",
            (str(applicant_id), holder_norm or "", str(discussion_id),
             title or "", time.time()),
        )
        self._db.commit()


def _discussion_title(d: dict[str, Any]) -> str:
    for key in ("title", "Title", "discussionTitle", "DiscussionTitle",
                "name", "Name"):
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _discussion_id(d: dict[str, Any]) -> str | None:
    for key in ("id", "Id", "ID", "discussionId", "DiscussionId"):
        val = d.get(key)
        if val:
            return str(val)
    return None


def _fold_alnum(value: str) -> str:
    """Lowercase alphanumeric fold for title/name comparison."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


_WORD_STOP = {"llc", "inc", "corp", "ltd", "co", "company", "pllc", "pa",
              "pc", "lp", "llp", "the", "of", "and", "for", "a", "an"}


def _sig_words(value: str) -> set[str]:
    """Significant words: lowercased, de-punctuated, no entity suffixes or
    stopwords, length >= 3."""
    words = re.sub(r"[^a-z0-9 ]", " ", (value or "").lower()).split()
    return {w for w in words if len(w) >= 3 and w not in _WORD_STOP}


def _holder_named_in_title(holder: str, title: str) -> bool:
    """True when the title names the holder: a long verbatim folded phrase,
    or all of the holder's significant words appear in the title. A single
    short word (e.g. "RMIS", "ABC") only earns the weaker fragment anchor —
    it is too easy to mismatch distinct entities on it."""
    if not holder or not title:
        return False
    fh, ft = _fold_alnum(holder), _fold_alnum(title)
    if fh and ft and len(fh) >= 6 and fh in ft:
        return True
    hw, tw = _sig_words(holder), _sig_words(title)
    if not hw or not hw <= tw:
        return False
    if len(hw) >= 2:
        return True
    word = next(iter(hw))
    return len(word) >= 6


def _parse_email_date(value: Any) -> Any:
    """Parse an email Date header (RFC 2822) or ISO string to datetime."""
    if not value:
        return None
    text = str(value).strip()
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(text)
    except Exception:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _parse_disc_date(value: Any) -> Any:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).strip())
    except Exception:
        return None


def _discussion_created(d: dict[str, Any]) -> Any:
    for key in ("created", "Created", "createdAt", "CreatedAt",
                "creationDate", "CreationDate"):
        val = d.get(key)
        if val:
            return _parse_disc_date(val)
    return None


# A discussion created this many days (or fewer) before the email arrived is
# treated as "created for this request" — but only combined with an exact
# holder anchor, never on recency alone.
_CREATED_FOR_REQUEST_DAYS = 5


def _grade_candidates(candidates: list[dict[str, Any]],
                      holder_names: list[str],
                      policy_numbers: list[str],
                      ) -> list[dict[str, Any]]:
    """Annotate cert-titled candidates with anchor evidence.

    Each annotated dict carries ``_anchors``: evidence strings for an exact
    holder phrase in the title, policy digits in the title, or a holder
    fragment in the title.
    """
    holder_phrases = [h for h in holder_names if _fold_alnum(h)]
    policy_digits = [re.sub(r"\D", "", p) for p in (policy_numbers or [])]
    policy_digits = [d for d in policy_digits if len(d) >= 4]
    annotated = []
    for did, title in candidates:
        norm_title = _fold_alnum(title)
        anchors: list[str] = []
        for h in holder_phrases:
            if _holder_named_in_title(h, title):
                anchors.append(f"holder {h!r} named in title")
                break
        for pd in policy_digits:
            if pd in norm_title:
                anchors.append(f"policy digits {pd} appear in title")
                break
        if not anchors:
            for h in holder_names:
                frag = _fold_alnum(h)
                if frag and frag in norm_title:
                    anchors.append(f"holder fragment {frag!r} in title")
                    break
        annotated.append({"id": did, "title": title,
                          "_anchors": anchors})
    return annotated


# resolve_discussion outcome codes. Carlo 2026-09-28: the two hold codes
# (NO_DISCUSSION, AMBIGUOUS) no longer need a human gate — the sweep
# auto-creates a named discussion for them via create_discussion_with_note.
DISCUSSION_RESOLVED = "resolved"
DISCUSSION_NONE = "no_discussion"
DISCUSSION_AMBIGUOUS = "ambiguous"
AUTO_CREATE_CODES = (DISCUSSION_NONE, DISCUSSION_AMBIGUOUS)


def resolve_discussion(verified: Any, holder_names: list[str],
                       registry: Any, discussions_client: Any,
                       email_date: Any = None,
                       is_followup: bool = False,
                       ) -> tuple[str | None, str | None, str, str]:
    """Find the discussion to file into. Never guesses.

    Returns (discussion_id, discussion_title, reason, code). The title is
    passed to the note writer as its title hint so the writer's own
    fail-closed selection confirms the same discussion. ``code`` is one of
    DISCUSSION_RESOLVED / DISCUSSION_NONE / DISCUSSION_AMBIGUOUS; the two
    hold codes are auto-create-eligible (see AUTO_CREATE_CODES) — the sweep
    creates a named discussion for them instead of asking a human.

    Confidence ladder (recorded in the reason):
      1. task registry hit — deterministic: this applicant+policy+holder
         filed to this discussion before;
      2. exactly one cert-titled discussion anchored by the email's exact
         holder phrase or policy digits;
      3. exactly one cert-titled discussion matching a holder fragment;
      4. HOLD — multiple candidates, or none for this request.

    Recency never resolves on its own: it only strengthens the evidence for
    a single anchored candidate ("created N days before the request — looks
    created for it") or sharpens the hold reason ("N prior threads for this
    holder; newest is M days old — none looks like this request").
    """
    policy_numbers = list(getattr(verified, "policy_numbers", []) or [])
    policy_key = policy_key_for(policy_numbers)
    holder_key = holder_key_for(holder_names)
    entry = registry.get(verified.applicant_id, policy_key, holder_key)
    discussions = discussions_client.get_discussions(verified.applicant_id) or []
    titles: dict[str, str] = {}
    created: dict[str, Any] = {}
    for d in discussions:
        if isinstance(d, dict):
            did = _discussion_id(d)
            if did:
                titles[did] = _discussion_title(d)
                created[did] = _discussion_created(d)
    if entry and entry.discussion_id:
        did = str(entry.discussion_id)
        return (did, titles.get(did),
                f"task registry: applicant+policy+holder filed to "
                f"discussion {did} before (title {titles.get(did)!r})",
                DISCUSSION_RESOLVED)

    cert_discs = []
    for d in discussions:
        if not isinstance(d, dict):
            continue
        title = _discussion_title(d)
        low = title.lower()
        if "cert" not in low and "coi" not in low:
            continue
        did = _discussion_id(d)
        if did:
            cert_discs.append((did, title))

    holder_frags = [_fold_alnum(h) for h in holder_names if h]
    filtered = []
    for did, title in cert_discs:
        norm_title = _fold_alnum(title)
        if holder_names and not (
                any(f and f in norm_title for f in holder_frags)
                or any(_holder_named_in_title(h, title)
                       for h in holder_names)):
            continue
        filtered.append((did, title))

    edt = _parse_email_date(email_date)

    def age_days(did: str) -> int | None:
        c = created.get(did)
        if not c or not edt:
            return None
        try:
            delta = edt - c
            return delta.days
        except Exception:
            return None

    if len(filtered) == 1:
        did, title = filtered[0]
        ann = _grade_candidates([(did, title)], holder_names,
                                 policy_numbers)
        anchors = ann[0]["_anchors"]
        age = age_days(did)
        ev = "; ".join(anchors) if anchors else "holder fragment match"
        if age is not None and 0 <= age <= _CREATED_FOR_REQUEST_DAYS:
            ev += (f"; created {age} day(s) before the request — "
                   "looks created for it")
        strength = "STRONG" if anchors and any(
            "named in title" in a or "policy digits" in a for a in anchors) \
            else "MEDIUM"
        return did, title, (
            f"{strength}: single certificates discussion {title!r} "
            f"({ev})"), DISCUSSION_RESOLVED

    if len(filtered) > 1:
        ann = _grade_candidates(filtered, holder_names,
                                 policy_numbers)
        # Tiebreaker 1: policy digits or exact holder phrase narrowing to one.
        strong = [a for a in ann if any(
            "named in title" in x or "policy digits" in x for x in a["_anchors"])]
        if len(strong) == 1:
            a = strong[0]
            age = age_days(a["id"])
            ev = "; ".join(a["_anchors"])
            if age is not None and 0 <= age <= _CREATED_FOR_REQUEST_DAYS:
                ev += (f"; created {age} day(s) before the request — "
                       "looks created for it")
            return a["id"], a["title"], (
                f"STRONG: {len(filtered)} candidates narrowed to one by "
                f"{ev} — discussion {a['title']!r}"), DISCUSSION_RESOLVED
        # Tiebreaker 2: exactly one holder-anchored candidate created for
        # this request — fresh (non-reply) email + brand-new discussion.
        # Never on recency alone: the holder anchor is required.
        if not is_followup and edt is not None:
            fresh = [a for a in ann if a["_anchors"]]
            fresh = [a for a in fresh
                     if (age_days(a["id"]) is not None
                         and 0 <= age_days(a["id"])
                         <= _CREATED_FOR_REQUEST_DAYS)]
            if len(fresh) == 1:
                a = fresh[0]
                return a["id"], a["title"], (
                    f"MEDIUM: {len(filtered)} candidates; "
                    f"{a['title']!r} is the only holder-anchored one "
                    f"created {age_days(a['id'])} day(s) before this new "
                    f"request ({'; '.join(a['_anchors'])})"), DISCUSSION_RESOLVED
        # Still ambiguous: say exactly why, with dates.
        parts = []
        for a in ann[:6]:
            age = age_days(a["id"])
            age_s = f", created {age}d before request" \
                if age is not None and age >= 0 else ""
            anch = ("; ".join(a["_anchors"]) if a["_anchors"]
                    else "fragment/no anchor")
            parts.append(f"{a['title']!r} [{anch}{age_s}]")
        shown = "; ".join(parts)
        holder_s = (f" for holder {holder_names[0]!r}"
                    if holder_names else " (no holder extracted)")
        stale_note = ""
        ages = [age_days(a["id"]) for a in ann
                if age_days(a["id"]) is not None]
        if ages and min(ages) > _CREATED_FOR_REQUEST_DAYS:
            stale_note = (f" Newest thread is {min(ages)} days old — none "
                          "looks created for this request.")
        return None, None, (
            f"{len(filtered)} certificates discussions match{holder_s} "
            f"({shown}){stale_note}; refusing to guess — auto-creating a "
            "new named discussion in EZLynx"), DISCUSSION_AMBIGUOUS
    return None, None, ("no certificates discussion on file for this request — "
                        "auto-creating a named one in EZLynx"), DISCUSSION_NONE


def _note_count(discussions_client: Any, applicant_id: int,
                discussion_id: str) -> int | None:
    try:
        for d in discussions_client.get_discussions(applicant_id) or []:
            if isinstance(d, dict) and _discussion_id(d) == str(discussion_id):
                for key in ("noteCount", "NoteCount", "note_count"):
                    if d.get(key) is not None:
                        return int(d[key])
    except Exception:
        return None
    return None


def file_record(record: Any, verified: Any, deps: FilingDeps,
                owner: str = "cert-worker", dry_run: bool = False) -> FilingResult:
    """File one VERIFIED record. Fail-closed at every step."""
    res = FilingResult()
    if verified.status != VERIFIED or not verified.applicant_id:
        res.hold_reasons.append("record is not VERIFIED — refusing to file")
        return res
    applicant_id = verified.applicant_id
    # Filing identity is per insured target (ledger_key), not per
    # message: a multi-insured email files one note/task chain per named
    # insured, and the store's "already filed" note/task state must not
    # leak from one target into another's. Single-insured records keep
    # ledger_key == gmail_id, so behavior there is unchanged.
    message_id = (getattr(record, "ledger_key", None)
                  or getattr(record, "gmail_id", None)
                  or getattr(record, "message_id", "unknown"))

    if deps.store and not deps.store.claim(
            message_id, owner,
            thread_id=getattr(record, "thread_id", None)):
        res.hold_reasons.append("another worker holds the lease — skipping")
        return res
    try:
        return _file_claimed(record, verified, deps, res, message_id,
                             applicant_id, owner, dry_run)
    finally:
        if deps.store:
            deps.store.release(message_id, owner)


def _looks_like_followup(subject: Any) -> bool:
    """True when the subject opens with Re:/Fwd: — a reply in a thread."""
    return bool(re.match(r"^\s*(?:re|fwd?)\s*:",
                         str(subject or ""), re.IGNORECASE))


_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
}


def _content_type_for(filename: str) -> str:
    lowered = str(filename or "").lower()
    for ext, ctype in _CONTENT_TYPES.items():
        if lowered.endswith(ext):
            return ctype
    return "application/octet-stream"


def _document_name_present(deps: FilingDeps, applicant_id: Any,
                           doc_name: str) -> bool:
    """True when Documents already contains doc_name for the applicant.

    Read-only. Raises when no search port is configured or the search
    itself fails — the caller treats that as UNVERIFIABLE, never as absent.
    """
    if deps.doc_searcher is None:
        raise RuntimeError("no document search port configured")
    payload = deps.doc_searcher(str(applicant_id))
    rows = (payload or {}).get("results") or []
    names = {str(r.get("documentName") or r.get("name") or "") for r in rows}
    return doc_name in names


def _upload_document_verified(deps: FilingDeps, applicant_id: Any,
                              doc_name: str, file_bytes: bytes,
                              filename: str, res: FilingResult
                              ) -> tuple[str | None, str]:
    """Upload one document under Carlo's retry rule (2026-09-27).

    Returns (document_id_or_None, outcome) where outcome is one of
    "uploaded", "already_present" (exact name was already in Documents —
    a previous attempt landed it; not re-uploading), "recovered" (landed
    despite the lost response), or "unverified". Never blind-retries a
    POST.

    Discipline, in order:
    1. Exact-name pre-check: present means a previous attempt landed it —
       skip, never duplicate. A search that cannot complete fails closed
       (UNVERIFIED) — never upload on an assumed-empty state.
    2. Upload. On success the exact document NAME must be present in a
       fresh Documents search (Carlo: exact-name HIT). The writer's
       numeric-ID read-back alone is not enough. A succeeded POST whose
       name cannot be confirmed is UNVERIFIED — never re-sent, because
       re-sending a succeeded POST would duplicate.
    3. On a failed POST (uncertain outcome) the destination is searched by
       name first: landed means done, verifiably absent means exactly one
       re-send (which itself must pass the exact-name proof), unsearchable
       means UNVERIFIED.
    """
    # --- 1. idempotency: exact-name pre-check ---
    try:
        already = _document_name_present(deps, applicant_id, doc_name)
    except Exception as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: cannot verify whether {doc_name} is already in "
            f"Documents ({exc}) — refusing to upload on an assumed-empty "
            "state; email left unread for human review")
        return None, "unverified"
    if already:
        res.evidence.append(
            f"document {doc_name} already present in Documents — "
            "counted as filed, not re-uploading")
        return None, "already_present"

    # --- 2. upload ---
    first_err: Exception | None = None
    try:
        up = deps.doc_writer(str(applicant_id), doc_name, file_bytes,
                             filename=filename,
                             content_type=_content_type_for(filename))
    except Exception as exc:
        first_err = exc  # uncertain outcome — read the destination back first

    if first_err is None:
        if not _exact_name_proven(deps, applicant_id, doc_name, res,
                                  "upload", up):
            return None, "unverified"
        return str((up or {}).get("document_id") or ""), "uploaded"

    # --- 3. uncertain outcome: read back before any re-send ---
    try:
        landed = _document_name_present(deps, applicant_id, doc_name)
    except Exception as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: upload of {doc_name} failed ({first_err}) and the "
            f"read-back search also failed ({exc}) — refusing to re-send "
            "blind; email left unread for human review")
        return None, "unverified"
    if landed:
        res.evidence.append(
            f"document upload outcome uncertain but {doc_name} is present in "
            "Documents — counted as landed, not re-sending")
        return None, "recovered"
    # Verifiably absent: exactly one re-send is safe.
    try:
        up = deps.doc_writer(str(applicant_id), doc_name, file_bytes,
                             filename=filename,
                             content_type=_content_type_for(filename))
    except Exception as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: upload of {doc_name} failed twice ({exc}); "
            "email left unread for human review")
        return None, "unverified"
    if not _exact_name_proven(deps, applicant_id, doc_name, res,
                              "re-send", up):
        return None, "unverified"
    return str((up or {}).get("document_id") or ""), "uploaded"


def _exact_name_proven(deps: FilingDeps, applicant_id: Any, doc_name: str,
                       res: FilingResult, how: str, up: Any) -> bool:
    """Exact-name HIT after a succeeded POST. False = UNVERIFIED (holds set).

    A succeeded POST is never re-sent (that would duplicate); when the
    name cannot be confirmed the filing fails closed instead.
    """
    try:
        hit = _document_name_present(deps, applicant_id, doc_name)
    except Exception as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: {how} of {doc_name} succeeded but the exact-name "
            f"read-back search failed ({exc}); email left unread for human "
            "review")
        return False
    if not hit:
        res.hold_reasons.append(
            f"UNVERIFIED: {how} of {doc_name} succeeded but the exact name "
            "is not in Documents — refusing to claim it; email left unread "
            "for human review")
        return False
    res.evidence.append(
        f"document {how}ed with exact-name read-back: {doc_name}")
    return True


def _build_uploads(record: Any) -> list[tuple[str, bytes, str]]:
    """(document_name, bytes, filename) for the email PDF + every attachment.

    The full original email goes first as a PDF (Carlo 2026-09-27: the
    email itself must live in Documents), then every attachment regardless
    of type — the old .pdf-only filter silently dropped photos and scans.
    """
    email_obj = getattr(record, "email", record)
    doc_name, pdf_bytes = email_pdf_document(email_obj)
    uploads = [(doc_name, pdf_bytes, doc_name)]
    for att in (getattr(record, "attachments", []) or []):
        filename = str(getattr(att, "filename", "") or "").strip()
        if not filename:
            continue
        content = getattr(att, "content", b"") or b""
        uploads.append((f"COI email attachment - {filename}", content,
                        filename))
    return uploads


def _upload_documents_step(record: Any, deps: FilingDeps, res: FilingResult,
                           applicant_id: int, dry_run: bool
                           ) -> list[tuple[str, bytes, str]] | None:
    """Build the email PDF + attachments and upload each with destination
    proof. Returns the uploads list on success; in dry-run mode the uploads
    are built but nothing is sent. Returns None when the filing must stop
    UNVERIFIED (res.status/hold_reasons already updated) — the email stays
    unread and no task is created."""
    try:
        uploads = _build_uploads(record)
    except Exception as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: could not render the email PDF ({exc}); "
            "email left unread for human review")
        res.status = ERROR
        return None
    if dry_run:
        res.evidence.append("dry run: documents not uploaded")
        return uploads
    outcome_counts = {"uploaded": 0, "already_present": 0, "recovered": 0}
    for doc_name, file_bytes, filename in uploads:
        document_id, outcome = _upload_document_verified(
            deps, applicant_id, doc_name, file_bytes, filename, res)
        if outcome == "unverified":
            res.status = ERROR
            return None
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        if document_id:
            res.document_ids.append(document_id)
    res.evidence.append(
        f"{len(uploads)} document(s) proven in Documents: "
        f"{outcome_counts['uploaded']} uploaded, "
        f"{outcome_counts['already_present']} already present, "
        f"{outcome_counts['recovered']} recovered after uncertain POST")
    return uploads


def _file_claimed(record: Any, verified: Any, deps: FilingDeps,
                  res: FilingResult, message_id: str, applicant_id: int,
                  owner: str, dry_run: bool) -> FilingResult:
    # --- discussion -------------------------------------------------------
    holder_names = list(getattr(record.facts, "holder_names", []) or [])
    discussion_id, discussion_title, how, code = resolve_discussion(
        verified, holder_names,
        deps.registry, deps.discussions_client,
        email_date=getattr(record, "date", None),
        is_followup=_looks_like_followup(getattr(record, "subject", "")))
    if discussion_id:
        res.discussion_id = discussion_id
        res.evidence.append(f"discussion resolved: {how}")
        return _file_to_discussion(
            record, verified, deps, res, message_id, applicant_id,
            discussion_id, discussion_title, dry_run)
    if code in AUTO_CREATE_CODES:
        # Carlo 2026-09-28: no human gate. The sweep auto-creates a named
        # discussion (the filing note becomes its first note) and reuses it
        # for follow-ups instead of asking Carlo.
        res.evidence.append(f"discussion hold ({code}): {how}")
        return _file_via_auto_create(
            record, verified, deps, res, message_id, applicant_id,
            holder_names, dry_run)
    res.hold_reasons.append(how)
    return res


def _file_to_discussion(record: Any, verified: Any, deps: FilingDeps,
                        res: FilingResult, message_id: str, applicant_id: int,
                        discussion_id: str, discussion_title: str | None,
                        dry_run: bool) -> FilingResult:
    # --- applicant proof before every write --------------------------------
    # Carlo's rule: the identity guard runs immediately before EACH write
    # (note, documents, task) — not just once up front. A guard that fails
    # fails the filing closed; the email stays unread.
    def _prove_target(what: str) -> bool:
        try:
            verify_filing_target(verified, discussion_id, deps.verifier)
        except FilingTargetMismatch as e:
            res.hold_reasons.append(
                f"{what} refused: applicant proof failed: {e}")
            return False
        res.evidence.append(f"applicant proof before {what}: passed")
        return True

    if not _prove_target("triple guard before note/documents/task writes"):
        return res
    res.evidence.append("triple filing guard passed")

    # --- documents to file ---------------------------------------------------
    # The full original email goes first as a PDF, then every attachment
    # regardless of type. UNVERIFIED rendering fails the whole filing — the
    # email stays unread and no task is created.
    #
    # --- documents (before the note) -----------------------------------------
    # Documents are uploaded and exact-name-proven BEFORE the note is
    # written, so a document failure can never leave a note claiming
    # everything was filed. Each upload runs under Carlo's retry rule: on
    # an uncertain outcome the destination is searched by document name
    # BEFORE any re-send. Anything UNVERIFIED fails the filing here — the
    # email stays unread and no task is created (the unread inbox is the
    # flag).
    if not dry_run and not _prove_target("document uploads"):
        res.status = ERROR
        return res
    uploads = _upload_documents_step(record, deps, res, applicant_id, dry_run)
    if uploads is None:
        return res

    # --- note --------------------------------------------------------------
    # Written only after every document is destination-proven, so the note
    # can name them truthfully.
    state = deps.store.get(message_id) if deps.store else {}
    note_text = summarize_for_note(
        getattr(record, "email", record), record.facts,
        filed_documents=[name for name, _, _ in uploads])
    pre_count = _note_count(deps.discussions_client, applicant_id,
                            discussion_id)
    if state.get("note_status") == "written" and state.get("note_id"):
        res.note_id = state["note_id"]
        res.evidence.append(
            f"note already filed as {res.note_id} — not duplicating")
    elif dry_run:
        res.status = DRY_RUN
        res.evidence.append("dry run: note validated, nothing written")
        return _task_step(record, verified, deps, res, message_id,
                          applicant_id, note_text, dry_run=True)
    else:
        if not _prove_target("note write"):
            res.status = ERROR
            return res
        try:
            filed = deps.note_writer(
                str(applicant_id), note_text,
                title_hint=discussion_title, dry_run=dry_run)
        except Exception as exc:
            # Uncertain outcome: read the destination back before any retry.
            post_count = _note_count(deps.discussions_client, applicant_id,
                                     discussion_id)
            if (pre_count is not None and post_count is not None
                    and post_count > pre_count):
                res.note_id = state.get("note_id") or "recovered-via-readback"
                res.evidence.append(
                    "note POST outcome uncertain but destination shows the "
                    "note landed — not retrying")
                if deps.store:
                    deps.store.set(message_id, note_status="written",
                                   note_id=res.note_id,
                                   discussion_id=discussion_id)
            else:
                res.hold_reasons.append(f"note write failed: {exc}")
                if deps.store:
                    deps.store.set(message_id, note_status="failed")
                res.status = ERROR
                return res
        else:
            status = (filed or {}).get("status")
            if status == "dry_run":
                res.status = DRY_RUN
                res.evidence.append("dry run: note validated, nothing written")
                return _task_step(record, verified, deps, res, message_id,
                                  applicant_id, note_text, dry_run=True)
            if status != "filed":
                res.hold_reasons.append(
                    f"note writer declined: {(filed or {}).get('reason')}")
                res.status = HELD
                return res
            res.note_id = str((filed or {}).get("note_id") or "")
            res.evidence.append(f"note filed: {res.note_id}")
            # Destination agreement: the writer reports which discussion it
            # filed to and whether it read the note back. If it names a
            # different discussion than the resolver chose, that is a
            # misfile signal — never silently mark FILED.
            writer_disc = str((filed or {}).get("discussion_id") or "")
            if writer_disc and writer_disc != str(discussion_id):
                res.hold_reasons.append(
                    f"note writer filed to discussion {writer_disc} but the "
                    f"resolver chose {discussion_id} ({discussion_title!r}) — "
                    "destination mismatch, needs human review")
                res.status = ERROR
                if deps.store:
                    deps.store.set(message_id, note_status="failed")
                return res
            if (filed or {}).get("read_back") is True:
                res.evidence.append(
                    f"writer read-back confirmed note {res.note_id} in "
                    f"discussion {discussion_id}")
            else:
                res.evidence.append(
                    "writer did not report a read-back confirmation")
            if deps.store:
                deps.store.set(message_id, note_status="written",
                               note_id=res.note_id,
                               discussion_id=discussion_id)

    if not _prove_target("task create/reuse/reopen"):
        res.status = ERROR
        return res
    res.status = FILED if res.status == HELD else res.status
    return _task_step(record, verified, deps, res, message_id, applicant_id,
                      note_text, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Auto-created named discussions (Carlo 2026-09-28: no human gate).
# ---------------------------------------------------------------------------

def _auto_discussion_title(holder_names: list[str], record: Any) -> str:
    """Title for an auto-created discussion:
    ``COI Request — {holder or "Certificate"} — {YYYY-MM-DD}``."""
    holder = ""
    if holder_names:
        holder = str(holder_names[0] or "").strip()
    parsed = _parse_email_date(getattr(record, "date", None))
    day = parsed.strftime("%Y-%m-%d") if parsed else time.strftime("%Y-%m-%d")
    return f"COI Request — {holder or 'Certificate'} — {day}"


def _find_discussion_by_exact_title(discussions_client: Any,
                                    applicant_id: int, title: str
                                    ) -> tuple[str, str] | None:
    """Exact-title lookup in the applicant's discussion list.

    Destination-based dedup: catches a discussion created by an earlier run
    whose ledger record never landed. Returns (discussion_id, title)."""
    try:
        rows = discussions_client.get_discussions(applicant_id) or []
    except Exception:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _discussion_title(row) == title:
            did = _discussion_id(row)
            if did:
                return did, title
    return None


def _verify_reused_discussion(discussions_client: Any,
                              discussion_id: str) -> str | None:
    """GET-verify a dedup-reused discussion still exists. Returns its title,
    or None when it does not read back."""
    try:
        record = discussions_client.get_discussion(discussion_id)
    except Exception:
        return None
    if not isinstance(record, dict) or not record:
        return None
    return _discussion_title(record) or None


def _reuse_discussion(record: Any, verified: Any, deps: FilingDeps,
                      res: FilingResult, message_id: str, applicant_id: int,
                      discussion_id: str, discussion_title: str | None,
                      why: str, dry_run: bool) -> FilingResult:
    """File into an already-existing discussion found by the auto-create
    dedup. The normal flow applies (documents, note append, task) — the
    note was NOT filed via with-note for this email, so no duplication."""
    res.discussion_id = discussion_id
    res.evidence.append(f"reusing discussion {discussion_id} ({why})")
    return _file_to_discussion(
        record, verified, deps, res, message_id, applicant_id,
        discussion_id, discussion_title, dry_run)


def _file_via_auto_create(record: Any, verified: Any, deps: FilingDeps,
                          res: FilingResult, message_id: str,
                          applicant_id: int, holder_names: list[str],
                          dry_run: bool) -> FilingResult:
    """No existing discussion safely fits: reuse or create a named one.

    Dedup order (never two discussions for the same request):
      (a) another email in the same Gmail thread already filed somewhere —
          reuse that discussion;
      (b) the ledger's auto-created discussion for this applicant +
          normalized holder — GET-verified, then reused. No expiry: a later
          email about the same client and holder appends to the existing
          discussion rather than creating a duplicate;
      (c) the exact auto title already on the applicant in EZLynx —
          destination-based catch for a create whose ledger row never landed;
      (d) create via POST v8/discussions/with-note with the filing note as
          the first note, then prove it with a fresh GET read-back.

    Creation failure or an unverified read-back holds UNVERIFIED: nothing
    is filed and the email stays unread.
    """
    store = deps.store
    client = deps.discussions_client
    holder_norm = holder_key_for(holder_names)
    title = _auto_discussion_title(holder_names, record)

    # (a) same Gmail thread already handled
    thread_id = getattr(record, "thread_id", None)
    if thread_id and store:
        did = store.thread_discussion(thread_id, exclude_message_id=message_id)
        if did:
            seen = _verify_reused_discussion(client, did)
            if seen is not None:
                return _reuse_discussion(
                    record, verified, deps, res, message_id, applicant_id,
                    did, seen, f"earlier email in thread {thread_id}",
                    dry_run)
            res.evidence.append(
                f"thread discussion {did} no longer reads back — "
                "not reusing")

    # (b) same applicant + normalized holder — reused indefinitely (no
    # expiry). The GET-verification below keeps this safe: a discussion
    # that no longer reads back in EZLynx is never reused.
    if store:
        entry = store.auto_discussion_get(applicant_id, holder_norm)
        if entry:
            seen = _verify_reused_discussion(client, entry["discussion_id"])
            if seen is not None:
                return _reuse_discussion(
                    record, verified, deps, res, message_id, applicant_id,
                    entry["discussion_id"], seen,
                    "auto-created for this holder (no expiry)",
                    dry_run)
            res.evidence.append(
                f"ledger discussion {entry['discussion_id']} no longer "
                "reads back — not reusing")

    # (c) exact title already on the applicant in EZLynx
    found = _find_discussion_by_exact_title(client, applicant_id, title)
    if found:
        did, _ = found
        if store:
            store.auto_discussion_record(applicant_id, holder_norm, did,
                                         title)
        return _reuse_discussion(
            record, verified, deps, res, message_id, applicant_id,
            did, title, "exact title already on the applicant", dry_run)

    # (d) create. Dry run validates everything but posts nothing.
    if dry_run:
        res.status = DRY_RUN
        res.evidence.append(
            f"dry run: would auto-create discussion {title!r} with the "
            "filing note via with-note; nothing written")
        note_text = summarize_for_note(
            getattr(record, "email", record), record.facts,
            filed_documents=[])
        return _task_step(record, verified, deps, res, message_id,
                          applicant_id, note_text, dry_run=True)

    # Applicant anchor BEFORE any write (checks 1-2 of the triple guard).
    # Check 3 (the discussion belongs to the applicant) runs after creation.
    try:
        verify_applicant_anchor(verified, deps.verifier)
    except FilingTargetMismatch as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: auto-create refused: applicant proof failed: "
            f"{exc}; email left unread for human review")
        res.status = ERROR
        return res
    res.evidence.append("applicant proof before auto-create: passed")

    # Documents first: the filing note names them truthfully.
    uploads = _upload_documents_step(record, deps, res, applicant_id,
                                    dry_run=False)
    if uploads is None:
        return res

    note_text = summarize_for_note(
        getattr(record, "email", record), record.facts,
        filed_documents=[name for name, _, _ in uploads])
    try:
        created = create_discussion_with_note(client, applicant_id, title,
                                              note_text)
    except Exception as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: auto-create discussion failed ({exc}); "
            "email left unread for human review")
        res.status = ERROR
        return res

    new_id = str(created["discussion_id"])
    res.discussion_id = new_id
    res.note_id = str(created.get("note_id") or "") or f"with-note:{new_id}"
    res.evidence.append(
        f"auto-created discussion {new_id} titled {title!r} with the "
        "filing note as its first note (POST with-note + GET read-back "
        "verified: title matches, noteCount >= 1)")
    if store:
        store.auto_discussion_record(applicant_id, holder_norm, new_id,
                                     title)
        store.set(message_id, note_status="written", note_id=res.note_id,
                  discussion_id=new_id)

    # Full triple guard now that the discussion exists.
    try:
        verify_filing_target(verified, new_id, deps.verifier)
    except FilingTargetMismatch as exc:
        res.hold_reasons.append(
            f"UNVERIFIED: applicant proof failed on the new discussion: "
            f"{exc}; email left unread for human review")
        res.status = ERROR
        return res
    res.evidence.append("applicant proof on the new discussion: passed")

    return _task_step(record, verified, deps, res, message_id,
                      applicant_id, note_text, dry_run=dry_run)


def _task_step(record: Any, verified: Any, deps: FilingDeps,
               res: FilingResult, message_id: str, applicant_id: int,
               note_text: str, dry_run: bool) -> FilingResult:
    """Task decision: create / reuse / reopen / none / hold."""
    from .cert_zapier import CertZapierClient

    policy_key = policy_key_for(verified.policy_numbers)
    holder_key = holder_key_for(
        list(getattr(record.facts, "holder_names", []) or []))
    entry = deps.registry.get(applicant_id, policy_key, holder_key)
    live_state = deps.zapier.get_task_state(entry.task_id) \
        if (deps.zapier and entry and entry.task_id) else "unknown"
    action = decide_task_action(verified.requested_action, entry, live_state)
    res.task_action = action

    # Nonce-guarded callback gate (cert_callback): the Zap's validated
    # callback is the only task proof (no Task API exists). Applies to
    # CREATE only, and runs BEFORE any fire, so a re-drive never re-fires
    # the Zap while a callback is in flight (a second fire would create a
    # duplicate EZLynx task).
    cb_store = getattr(deps, "callback_store", None)
    if action == CREATE and cb_store is not None:
        proof = cb_store.get_proof(applicant_id, policy_key, holder_key)
        if proof is not None:
            # Callback arrived and validated: complete without firing again.
            res.task_id = proof.task_id
            res.evidence.append(
                f"task {proof.task_id} proven via Zap callback "
                f"(filing {proof.filing_id[:8]}…), assigned to "
                f"{proof.assignee}")
            new_entry = entry or TaskEntry(
                applicant_id=applicant_id, policy_key=policy_key,
                holder_key=holder_key, task_status=TASK_OPEN)
            new_entry.discussion_id = res.discussion_id
            new_entry.task_id = proof.task_id
            new_entry.task_status = TASK_OPEN
            deps.registry.put(new_entry)
            return res
        pending = cb_store.find_pending(applicant_id, policy_key,
                                        holder_key)
        if pending is not None:
            # A fire is already in flight: HOLD for its callback, never
            # re-fire.
            res.hold_reasons.append(
                "UNVERIFIED: task Zap already fired "
                f"(filing {pending.filing_id[:8]}…), awaiting its callback; "
                "holding instead of risking a duplicate task; email left "
                "unread for a later sweep")
            res.status = ERROR
            return res

    if action == NONE:
        res.evidence.append("acknowledgement — task state left alone")
    elif action == REUSE:
        res.task_id = entry.task_id if entry else None
        res.evidence.append(f"reusing open task {res.task_id}")
    elif action == CREATE:
        holder = (getattr(record.facts, "holder_names", []) or [""])[0]
        title = (f"Certificate request — {holder or verified.insured_name}"
                 )[:120]
        try:
            zap = deps.zapier.create_task(
                applicant_id=applicant_id, title=title,
                email_subject=getattr(record, "subject", ""),
                note_text=note_text)
        except Exception as exc:
            res.hold_reasons.append(f"task creation failed: {exc}")
            res.status = PARTIAL if res.status == FILED else res.status
            return res
        # Record the fire against its nonce BEFORE any proof attempt: a
        # later sweep's re-drive must HOLD on this pending fire, never
        # re-fire (duplicate EZLynx task). Never let bookkeeping break
        # the filing.
        if getattr(zap, "fired", False) and cb_store is not None:
            try:
                filing_id = getattr(zap, "filing_id", "") or ""
                if filing_id:
                    cb_store.record_fire(
                        filing_id=filing_id, applicant_id=applicant_id,
                        policy_key=policy_key, holder_key=holder_key,
                        title=title,
                        assignee=getattr(deps.zapier, "assignee",
                                        "SCanales"))
            except Exception as exc:  # noqa: BLE001 — best-effort
                res.evidence.append(
                    f"callback fire bookkeeping failed ({exc}); filing "
                    "continues UNVERIFIED")
        # Carlo's rule: Zapier HTTP 200 is not proof. Proof is the Zap's
        # validated callback echoing THIS fire's filing_id (no Task API
        # exists). A title match from an older fire is never proof, so when
        # the callback store is present there is no fallback to the
        # title-based prover seam.
        proof = None
        if cb_store is not None:
            cb_proof = cb_store.get_proof_by_filing(
                getattr(zap, "filing_id", "") or "")
            if cb_proof is not None:
                proof = {"task_id": cb_proof.task_id,
                         "assignee": cb_proof.assignee}
        elif deps.task_prover is None:
            res.hold_reasons.append(
                "UNVERIFIED: certificate-review task Zap fired but no task "
                "prover is configured — cannot prove the task exists in "
                "EZLynx assigned to SCanales; email left unread for human "
                "review")
            res.status = ERROR
            return res
        else:
            try:
                proof = deps.task_prover(applicant_id, title)
            except Exception as exc:
                res.hold_reasons.append(
                    f"UNVERIFIED: task proof failed ({exc}); email left unread "
                    "for human review")
                res.status = ERROR
                return res
        if not proof:
            res.hold_reasons.append(
                "UNVERIFIED: certificate-review task Zap fired but its "
                "callback has not arrived/validated yet — cannot prove the "
                "task exists in EZLynx assigned to SCanales; email left "
                "unread for a later sweep")
            res.status = ERROR
            return res
        task_id = (proof or {}).get("task_id")
        assignee = (proof or {}).get("assignee")
        # NOTE 2026-09-27: task_id is opportunistic, not required. The
        # Zap's EZLynx step is "Create Note" with no mappable ID output;
        # the validated callback itself (filing nonce + applicant +
        # assignee) is the proof that the Zap's EZLynx step succeeded.
        if assignee != "SCanales":
            res.hold_reasons.append(
                f"UNVERIFIED: task proof did not confirm assignment to "
                f"SCanales (task_id={task_id!r}, assignee={assignee!r}); "
                "email left unread for human review")
            res.status = ERROR
            return res
        res.task_id = str(task_id or "")
        res.evidence.append(
            "callback proven in EZLynx assigned to SCanales"
            + (f" (task {task_id})" if task_id else ""))
        new_entry = entry or TaskEntry(
            applicant_id=applicant_id, policy_key=policy_key,
            holder_key=holder_key, task_status=TASK_OPEN)
        new_entry.discussion_id = res.discussion_id
        new_entry.task_id = str(task_id)
        deps.registry.put(new_entry)
    elif action == REOPEN:
        try:
            deps.zapier.reopen_task(
                task_id=entry.task_id, applicant_id=applicant_id,
                title="Certificate request (reopened)", note_text=note_text)
        except Exception as exc:
            res.hold_reasons.append(f"task reopen unavailable: {exc}")
    else:  # HOLD
        res.hold_reasons.append(
            "could not classify the task action safely — holding")

    if res.status == HELD and not res.hold_reasons:
        res.status = FILED if res.note_id else DRY_RUN
    return res
