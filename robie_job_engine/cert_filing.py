"""Certificates chunk 3: filing verified requests into EZLynx.

Pipeline per VERIFIED record (read-only until the write steps):
  1. Claim a per-message lease (concurrent workers never double-file).
  2. Resolve the discussion: task registry -> existing holder-matching
     certificates discussion -> HOLD (never create, never guess).
  3. Triple filing guard (verify_filing_target) before every write.
  4. Append the summary note (real content via summarize_for_note).
     On an uncertain POST outcome, read the destination back BEFORE any
     retry — never blind-retry a non-idempotent POST.
  5. Upload PDF attachments via the OAuth DocumentApi, with read-back.
  6. Task decision via the registry + Zapier (create/reuse/reopen/hold).

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

from .cert_intake import summarize_for_note
from .cert_task_registry import (
    CREATE, HOLD, NONE, REOPEN, REUSE, TASK_OPEN,
    TaskEntry, decide_task_action, holder_key_for, policy_key_for,
)
from .cert_verification import (
    ACTION_ACK, ACTION_NEW_REQUEST, VERIFIED,
    FilingTargetMismatch, verify_filing_target,
)

FILED = "FILED"
DRY_RUN = "DRY_RUN"
HELD = "HELD"
PARTIAL = "PARTIAL"
ERROR = "ERROR"


@dataclass
class FilingDeps:
    discussions_client: Any = None   # get_discussions(applicant_id)
    verifier: Any = None             # search_policies/policy anchor reads
    note_writer: Any = None          # (applicant_id, note_text, *, title_hint, dry_run) -> dict
    doc_writer: Any = None           # (applicant_id, document_name, file_bytes, *, filename) -> dict
    zapier: Any = None               # CertZapierClient (or fake)
    registry: Any = None             # TaskRegistry
    store: Any = None                # FilingStore


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
                docs_json TEXT NOT NULL DEFAULT '{}',
                task_status TEXT NOT NULL DEFAULT 'pending',
                task_id TEXT,
                lease_owner TEXT,
                lease_expires REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )"""
        )
        self._db.commit()

    def claim(self, message_id: str, owner: str, ttl_s: int = 600) -> bool:
        """Take the lease for a message. False = another worker holds it."""
        now = time.time()
        row = self._db.execute(
            "SELECT lease_owner, lease_expires FROM cert_filing WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if row and row[0] != owner and row[1] > now:
            return False
        self._db.execute(
            """INSERT INTO cert_filing (message_id, lease_owner, lease_expires, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET
                lease_owner=excluded.lease_owner,
                lease_expires=excluded.lease_expires,
                updated_at=excluded.updated_at""",
            (message_id, owner, now + ttl_s, now),
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


def resolve_discussion(verified: Any, holder_names: list[str],
                       registry: Any, discussions_client: Any,
                       email_date: Any = None,
                       is_followup: bool = False,
                       ) -> tuple[str | None, str | None, str]:
    """Find the discussion to file into. Never creates, never guesses.

    Returns (discussion_id, discussion_title, reason). The title is passed
    to the note writer as its title hint so the writer's own fail-closed
    selection confirms the same discussion.

    Confidence ladder (recorded in the reason):
      1. task registry hit — deterministic: this applicant+policy+holder
         filed to this discussion before;
      2. exactly one cert-titled discussion anchored by the email's exact
         holder phrase or policy digits;
      3. exactly one cert-titled discussion matching a holder fragment;
      4. HOLD — multiple candidates, none for this request, or a single
         candidate with NO holder/policy anchor (a lone discussion is not
         evidence the request belongs in it).

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
                f"discussion {did} before (title {titles.get(did)!r})")

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
        if not anchors:
            # A lone certificate discussion with NO holder or policy anchor
            # is not evidence this request belongs in it — the old code
            # printed "holder fragment match" here, a fabricated reason.
            # Hold with the honest evidence, never file.
            return None, None, (
                f"HOLD: single certificates discussion {title!r} but no "
                f"holder/policy anchor for this request — holding for "
                f"human, never guessing")
        ev = "; ".join(anchors)
        if age is not None and 0 <= age <= _CREATED_FOR_REQUEST_DAYS:
            ev += (f"; created {age} day(s) before the request — "
                   "looks created for it")
        strength = "STRONG" if anchors and any(
            "named in title" in a or "policy digits" in a for a in anchors) \
            else "MEDIUM"
        return did, title, (
            f"{strength}: single certificates discussion {title!r} "
            f"({ev})")

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
                f"{ev} — discussion {a['title']!r}")
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
                    f"request ({'; '.join(a['_anchors'])})")
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
            f"({shown}){stale_note}; refusing to guess — create a new "
            "named discussion in EZLynx or pick one by hand")
    return None, None, ("no certificates discussion on file for this request — "
                        "create a named one in EZLynx or approve auto-creation")


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
    message_id = getattr(record, "gmail_id", None) or getattr(
        record, "message_id", "unknown")

    if deps.store and not deps.store.claim(message_id, owner):
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


def _file_claimed(record: Any, verified: Any, deps: FilingDeps,
                  res: FilingResult, message_id: str, applicant_id: int,
                  owner: str, dry_run: bool) -> FilingResult:
    # --- discussion -------------------------------------------------------
    discussion_id, discussion_title, how = resolve_discussion(
        verified, list(getattr(record.facts, "holder_names", []) or []),
        deps.registry, deps.discussions_client,
        email_date=getattr(record, "date", None),
        is_followup=_looks_like_followup(getattr(record, "subject", "")))
    if not discussion_id:
        res.hold_reasons.append(how)
        return res
    res.discussion_id = discussion_id
    res.evidence.append(f"discussion resolved: {how}")

    # --- triple guard before ANY write ------------------------------------
    try:
        verify_filing_target(verified, discussion_id, deps.verifier)
    except FilingTargetMismatch as e:
        res.hold_reasons.append(f"triple guard refused: {e}")
        return res
    res.evidence.append("triple filing guard passed")

    # --- note --------------------------------------------------------------
    state = deps.store.get(message_id) if deps.store else {}
    note_text = summarize_for_note(
        getattr(record, "email", record), record.facts,
        filed_documents=[])
    pre_count = _note_count(deps.discussions_client, applicant_id,
                            discussion_id)
    if state.get("note_status") == "written" and state.get("note_id"):
        res.note_id = state["note_id"]
        res.evidence.append(
            f"note already filed as {res.note_id} — not duplicating")
    else:
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

    # --- documents ----------------------------------------------------------
    attachments = [a for a in (getattr(record, "attachments", []) or [])
                   if (getattr(a, "filename", "") or "").lower().endswith(".pdf")]
    for att in attachments:
        doc_name = att.filename if att.filename.lower().endswith(".pdf") \
            else att.filename + ".pdf"
        try:
            up = deps.doc_writer(str(applicant_id), doc_name, att.content,
                                 filename=att.filename)
        except Exception as exc:
            res.hold_reasons.append(
                f"document upload failed for {att.filename}: {exc}")
            res.status = PARTIAL
            return _task_step(record, verified, deps, res, message_id,
                              applicant_id, note_text, dry_run=dry_run)
        res.document_ids.append(str((up or {}).get("document_id") or ""))
    if attachments:
        res.evidence.append(f"{len(attachments)} document(s) uploaded "
                            "with read-back")

    res.status = FILED if res.status == HELD else res.status
    return _task_step(record, verified, deps, res, message_id, applicant_id,
                      note_text, dry_run=dry_run)


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
        res.evidence.append(f"task Zap fired: {zap.reason}; task ID arrives "
                            "via the Zap callback")
        new_entry = entry or TaskEntry(
            applicant_id=applicant_id, policy_key=policy_key,
            holder_key=holder_key, task_status=TASK_OPEN)
        new_entry.discussion_id = res.discussion_id
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
