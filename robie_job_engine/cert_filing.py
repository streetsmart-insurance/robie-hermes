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
    task_action: str = NONE
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


def resolve_discussion(verified: Any, holder_names: list[str],
                       registry: Any, discussions_client: Any) -> tuple[str | None, str]:
    """Find the discussion to file into. Never creates, never guesses.

    Order: task registry -> existing holder-matching certificates
    discussion -> (None, reason).
    """
    policy_key = policy_key_for(verified.policy_numbers)
    holder_key = holder_key_for(holder_names)
    entry = registry.get(verified.applicant_id, policy_key, holder_key)
    if entry and entry.discussion_id:
        return entry.discussion_id, "task registry"

    discussions = discussions_client.get_discussions(verified.applicant_id) or []
    holder_frags = [re.sub(r"[^a-z0-9]", "", h.lower())
                    for h in holder_names if h]
    candidates = []
    for d in discussions:
        if not isinstance(d, dict):
            continue
        title = _discussion_title(d).lower()
        if "cert" not in title and "coi" not in title:
            continue
        did = _discussion_id(d)
        if not did:
            continue
        if holder_frags and not any(f and f in re.sub(r"[^a-z0-9]", "", title)
                                    for f in holder_frags):
            continue
        candidates.append((did, _discussion_title(d)))
    if len(candidates) == 1:
        return candidates[0][0], f"existing discussion {candidates[0][1]!r}"
    if len(candidates) > 1:
        return None, (f"{len(candidates)} certificates discussions match; "
                      "refusing to guess")
    return None, ("no certificates discussion on file for this request — "
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


def _file_claimed(record: Any, verified: Any, deps: FilingDeps,
                  res: FilingResult, message_id: str, applicant_id: int,
                  owner: str, dry_run: bool) -> FilingResult:
    # --- discussion -------------------------------------------------------
    discussion_id, how = resolve_discussion(
        verified, list(getattr(record.facts, "holder_names", []) or []),
        deps.registry, deps.discussions_client)
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
                title_hint=None, dry_run=dry_run)
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
