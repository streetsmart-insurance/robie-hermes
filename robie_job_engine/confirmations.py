"""HITL plan-confirmation workflow backend for the evidence loop.

A plan draft that needs human review cannot become a locked plan until a
named human explicitly approves it. This module is the persistence and
decision layer behind that gate:

- ``request_confirmation`` -- file a draft for human decision (idempotent);
- ``approve`` / ``reject`` -- record the human's decision (PENDING only);
- ``expire_old`` -- sweep stale PENDING confirmations to EXPIRED;
- ``get`` / ``list_pending`` -- read back records;
- ``confirmation_summary`` -- one plain-English line per record;
- ``rows_for_sheet`` -- header + rows shaped for the board sheet;
- ``confirm_and_lock`` -- approve AND lock, atomically: verifies the
  supplied draft is byte-for-byte the draft that was filed for review
  (draft fingerprint), flips PENDING -> APPROVED with a compare-and-set,
  and inserts the locked plan -- all in one transaction, so a failed lock
  never leaves a false APPROVED record and an approval for one draft can
  never lock another.

Storage is a ``plan_confirmations`` table in the job engine's sqlite db,
reached through the passed-in store. Every function takes ``store``
explicitly -- there is no implicit "last used store".
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

from .plan_extraction import draft_fingerprint, draft_to_locked_plan

try:
    # Typing-only import; never instantiated here.
    from .store import JobStore  # noqa: F401
except Exception:  # pragma: no cover - typing convenience only
    JobStore = Any  # type: ignore


STATUSES = ("PENDING", "APPROVED", "REJECTED", "EXPIRED")


class ConfirmationMismatch(ValueError):
    """The confirmation record does not match the draft/job being locked:
    wrong job, missing fingerprint, or a draft that differs from the one
    filed for review. Fail closed -- never lock on a mismatch."""

_SHEET_HEADERS = [
    "Confirmation ID",
    "Job type",
    "Draft summary",
    "Status",
    "Requested by",
    "Decided by",
    "Decided at",
    "Created at",
]

_JOB_TYPE_LABELS = {
    "policy_change": "Policy change",
    "carrier_quote": "Carrier quote",
    "carrier_call": "Carrier call",
}


# ---------------------------------------------------------------------------
# Store handling. Every function takes the store explicitly -- no hidden
# module-global "last used store". In a runtime where many jobs share one
# process, an implicit fallback is how one job's approval lands in another
# job's database.
# ---------------------------------------------------------------------------

def _require_store(store: Any) -> Any:
    if store is None:
        raise ValueError("a store is required; pass store= explicitly")
    return store


@contextmanager
def _session(store: Any) -> Iterator[sqlite3.Connection]:
    """A read-write session on the store's transaction."""
    _require_store(store)
    with store.transaction() as conn:
        _ensure_schema(conn)
        yield conn


def _read_conn(store: Any) -> sqlite3.Connection:
    _require_store(store)
    conn = store.connect()
    _ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Schema + helpers
# ---------------------------------------------------------------------------

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_confirmations (
            id TEXT PRIMARY KEY,
            loop_job_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            draft_summary TEXT,
            changes_json TEXT,
            status TEXT NOT NULL,
            requested_by TEXT,
            decided_by TEXT,
            decided_at TEXT,
            decision_reason TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # Origin platform the requester started on ("chat" | "email" | "ezlynx"),
    # so the HITL ping goes back on that same platform. Added after the
    # table first shipped; migrate older databases in place.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_confirmations)")}
    if "origin_platform" not in columns:
        conn.execute("ALTER TABLE plan_confirmations ADD COLUMN origin_platform TEXT")
    if "origin_ref" not in columns:
        conn.execute("ALTER TABLE plan_confirmations ADD COLUMN origin_ref TEXT")
    # Fingerprint of the exact draft under review
    # (plan_extraction.draft_fingerprint). Added after the table first
    # shipped; older rows have NULL and therefore cannot pass
    # confirm_and_lock -- fail closed, re-file the confirmation.
    if "draft_hash" not in columns:
        conn.execute("ALTER TABLE plan_confirmations ADD COLUMN draft_hash TEXT")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_confirmations_loop
           ON plan_confirmations(loop_job_id, status)"""
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else None  # type: ignore[return-value]


def _coerce_changes(changes_json: Any) -> str | None:
    if changes_json is None:
        return None
    if isinstance(changes_json, str):
        return changes_json
    return json.dumps(changes_json, sort_keys=True, default=str)


def _require_nonempty(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

def request_confirmation(
    store: Any,
    *,
    loop_job_id: str,
    job_type: str,
    draft_summary: str = "",
    changes_json: Any = None,
    requested_by: str,
    origin_platform: str | None = None,
    origin_ref: Any = None,
    draft: Any = None,
    draft_hash: str | None = None,
) -> str:
    """File a draft for human decision. Idempotent per loop job.

    If a PENDING confirmation already exists for ``loop_job_id``, its id is
    returned instead of creating a duplicate. Returns the confirmation id.

    ``origin_platform`` is where the requester started ("chat" | "email" |
    "ezlynx") so the HITL ping goes back on that same platform;
    ``origin_ref`` carries what that platform needs (dict or JSON string):
    chat -> {"space": ..., "thread": ...}; email -> {"to": ...};
    ezlynx -> {"applicant_id": ...}.

    ``draft`` (a PlanDraft) or its ``draft_hash`` binds this confirmation to
    the exact draft under review: ``confirm_and_lock`` later refuses to lock
    any other draft against this approval. Callers that intend to use
    ``confirm_and_lock`` must file the fingerprint here.
    """
    loop_job_id = _require_nonempty(loop_job_id, "loop_job_id")
    job_type = _require_nonempty(job_type, "job_type")
    requested_by = _require_nonempty(requested_by, "requested_by")
    if draft is not None:
        fingerprint = draft_fingerprint(draft)
        if draft_hash is not None and str(draft_hash) != fingerprint:
            raise ValueError(
                "draft and draft_hash disagree; pass one canonical draft"
            )
        draft_hash = fingerprint
    if draft_hash is not None:
        draft_hash = str(draft_hash).strip().lower()
        if len(draft_hash) != 64 or any(
            c not in "0123456789abcdef" for c in draft_hash
        ):
            raise ValueError(
                "draft_hash must be a 64-char hex SHA-256 fingerprint "
                "(see plan_extraction.draft_fingerprint)"
            )
    if origin_platform is not None:
        origin_platform = _require_nonempty(origin_platform, "origin_platform").casefold()
        if origin_platform not in ("chat", "email", "ezlynx"):
            raise ValueError(
                f"origin_platform must be chat, email, or ezlynx, got {origin_platform!r}"
            )
    origin_ref_text = _coerce_changes(origin_ref)
    with _session(store) as conn:
        existing = conn.execute(
            """SELECT id FROM plan_confirmations
               WHERE loop_job_id = ? AND status = 'PENDING'
               ORDER BY created_at DESC LIMIT 1""",
            (loop_job_id,),
        ).fetchone()
        if existing:
            confirmation_id = str(existing["id"])
            return confirmation_id
        confirmation_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO plan_confirmations
               (id, loop_job_id, job_type, draft_summary, changes_json,
                status, requested_by, decided_by, decided_at,
                decision_reason, created_at, origin_platform, origin_ref,
                draft_hash)
               VALUES (?, ?, ?, ?, ?, 'PENDING', ?, NULL, NULL, NULL, ?, ?, ?, ?)""",
            (
                confirmation_id,
                loop_job_id,
                job_type,
                str(draft_summary or ""),
                _coerce_changes(changes_json),
                requested_by,
                _utc_now(),
                origin_platform,
                origin_ref_text,
                draft_hash,
            ),
        )
    return confirmation_id


def _decide(
    confirmation_id: str,
    decided_by: str,
    new_status: str,
    reason: str = "",
    *,
    store: Any,
) -> dict[str, Any]:
    confirmation_id = _require_nonempty(confirmation_id, "confirmation_id")
    decided_by = _require_nonempty(decided_by, "decided_by")
    if new_status not in ("APPROVED", "REJECTED"):
        raise ValueError(f"invalid decision status: {new_status!r}")
    with _session(store) as conn:
        row = conn.execute(
            "SELECT * FROM plan_confirmations WHERE id = ?", (confirmation_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
        if str(row["status"]) != "PENDING":
            raise ValueError(
                f"confirmation {confirmation_id!r} is already "
                f"{row['status']}; only PENDING confirmations can be decided"
            )
        now = _utc_now()
        conn.execute(
            """UPDATE plan_confirmations
               SET status = ?, decided_by = ?, decided_at = ?,
                   decision_reason = ?
               WHERE id = ?""",
            (new_status, decided_by, now, str(reason or ""), confirmation_id),
        )
        updated = conn.execute(
            "SELECT * FROM plan_confirmations WHERE id = ?", (confirmation_id,)
        ).fetchone()
        return _row_to_dict(updated)


def approve(
    confirmation_id: str, decided_by: str, *, store: Any
) -> dict[str, Any]:
    """Approve a PENDING confirmation. Raises ValueError otherwise."""
    return _decide(confirmation_id, decided_by, "APPROVED", store=store)


def reject(
    confirmation_id: str,
    decided_by: str,
    reason: str = "",
    *,
    store: Any,
) -> dict[str, Any]:
    """Reject a PENDING confirmation with an optional reason."""
    return _decide(confirmation_id, decided_by, "REJECTED", reason, store=store)


def expire_old(store: Any, max_age_hours: float = 72) -> int:
    """Mark stale PENDING confirmations EXPIRED. Returns the count expired."""
    if max_age_hours < 0:
        raise ValueError("max_age_hours must be >= 0")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    now = _utc_now()
    with _session(store) as conn:
        cursor = conn.execute(
            """UPDATE plan_confirmations
               SET status = 'EXPIRED', decided_by = 'system',
                   decided_at = ?,
                   decision_reason = ?
               WHERE status = 'PENDING' AND created_at < ?""",
            (
                now,
                f"expired after {max_age_hours:g}h without a decision",
                cutoff,
            ),
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def get(confirmation_id: str, *, store: Any) -> dict[str, Any] | None:
    """Return the confirmation record, or None for an unknown id."""
    confirmation_id = str(confirmation_id or "").strip()
    if not confirmation_id:
        return None
    conn = _read_conn(store)
    try:
        row = conn.execute(
            "SELECT * FROM plan_confirmations WHERE id = ?", (confirmation_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_pending(store: Any, limit: int = 100) -> list[dict[str, Any]]:
    """PENDING confirmations, oldest first (the human's work queue)."""
    if limit <= 0:
        raise ValueError("limit must be > 0")
    conn = _read_conn(store)
    try:
        rows = conn.execute(
            """SELECT * FROM plan_confirmations
               WHERE status = 'PENDING'
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _parse_changes(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("changes_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _friendly_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return local.strftime("%b %-d, %Y %-I:%M %p")
    except (ValueError, TypeError):
        return text


def confirmation_summary(record: Mapping[str, Any]) -> str:
    """One plain-English line describing a confirmation record."""
    record = dict(record or {})
    job_label = _JOB_TYPE_LABELS.get(
        str(record.get("job_type") or ""), str(record.get("job_type") or "Job")
    )
    changes = _parse_changes(record)
    policy = str(changes.get("policy_number") or "").strip()
    change_map = changes.get("changes")
    if isinstance(change_map, Mapping) and change_map:
        change_desc = f"{len(change_map)} change{'s' if len(change_map) != 1 else ''}"
    elif isinstance(change_map, list) and change_map:
        change_desc = f"{len(change_map)} change{'s' if len(change_map) != 1 else ''}"
    else:
        change_desc = "proposed changes"
    subject = f"{job_label} for policy {policy} ({change_desc})" if policy else (
        f"{job_label} ({change_desc})"
    )
    status = str(record.get("status") or "")
    requested_by = str(record.get("requested_by") or "unknown")
    decided_by = str(record.get("decided_by") or "unknown")
    reason = str(record.get("decision_reason") or "").strip()
    if status == "PENDING":
        return (
            f"{subject} is waiting for approval "
            f"(requested by {requested_by} on "
            f"{_friendly_datetime(record.get('created_at'))})."
        )
    if status == "APPROVED":
        return (
            f"{subject} was approved by {decided_by} on "
            f"{_friendly_datetime(record.get('decided_at'))}."
        )
    if status == "REJECTED":
        base = (
            f"{subject} was rejected by {decided_by} on "
            f"{_friendly_datetime(record.get('decided_at'))}."
        )
        return f"{base} Reason: {reason}." if reason else base
    if status == "EXPIRED":
        return (
            f"{subject} expired with no decision "
            f"(requested by {requested_by} on "
            f"{_friendly_datetime(record.get('created_at'))})."
        )
    return f"{subject} has status {status}."


def rows_for_sheet(store: Any) -> tuple[list[str], list[list[str]]]:
    """Headers + rows shaped for the board sheet. Pending first, then decided.

    Returns plain string values only ("" for missing).
    """
    conn = _read_conn(store)
    try:
        rows = conn.execute(
            """SELECT id, job_type, draft_summary, status, requested_by,
                      decided_by, decided_at, created_at
               FROM plan_confirmations
               ORDER BY CASE WHEN status = 'PENDING' THEN 0 ELSE 1 END,
                        CASE WHEN status = 'PENDING' THEN created_at END ASC,
                        decided_at DESC"""
        ).fetchall()
    finally:
        conn.close()

    def cell(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    data = [
        [
            cell(r["id"]),
            cell(r["job_type"]),
            cell(r["draft_summary"]),
            cell(r["status"]),
            cell(r["requested_by"]),
            cell(r["decided_by"]),
            cell(r["decided_at"]),
            cell(r["created_at"]),
        ]
        for r in rows
    ]
    return list(_SHEET_HEADERS), data


# ---------------------------------------------------------------------------
# Confirm + lock
# ---------------------------------------------------------------------------

def confirm_and_lock(
    store: Any,
    confirmation_id: str,
    draft: Any,
    job_id: str,
    decided_by: str,
) -> Any:
    """Approve the confirmation AND lock the draft, atomically. Fail closed.

    Everything happens inside one transaction:

    1. the confirmation must exist and be PENDING;
    2. its ``loop_job_id`` must equal ``job_id`` -- an approval filed for
       one job never executes another;
    3. its stored ``draft_hash`` must equal ``draft_fingerprint(draft)`` --
       the draft being locked is byte-for-byte the draft the human reviewed
       (``ConfirmationMismatch`` otherwise);
    4. the locked plan's job_type must match the confirmation's job_type;
    5. the status flips PENDING -> APPROVED by compare-and-set (a concurrent
       decision loses the race) and the locked plan is inserted in the same
       transaction -- a failed lock rolls the APPROVED back, so the ledger
       never shows an approval for a plan that was not locked.

    ``decided_by`` must be the authenticated principal id supplied by the
    caller's adapter (Chat user id, verified decision-token principal, ...);
    this module records it, it cannot verify it. ``PlanNeedsHumanReview``
    from the lock step propagates with the approval rolled back.
    """
    from . import plan_lock

    confirmation_id = _require_nonempty(confirmation_id, "confirmation_id")
    decided_by = _require_nonempty(decided_by, "decided_by")
    job_id = _require_nonempty(job_id, "job_id")
    with _session(store) as conn:
        row = conn.execute(
            "SELECT * FROM plan_confirmations WHERE id = ?", (confirmation_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown confirmation id: {confirmation_id!r}")
        if str(row["status"]) != "PENDING":
            raise ValueError(
                f"confirmation {confirmation_id!r} is {row['status']}, "
                "not PENDING; refusing to lock"
            )
        if str(row["loop_job_id"]) != job_id:
            raise ConfirmationMismatch(
                f"confirmation {confirmation_id!r} was filed for job "
                f"{row['loop_job_id']!r}, not {job_id!r}; refusing to lock"
            )
        stored_hash = (
            str(row["draft_hash"]) if "draft_hash" in row.keys() else ""
        )
        if not stored_hash:
            raise ConfirmationMismatch(
                f"confirmation {confirmation_id!r} has no draft fingerprint "
                "on file; re-file the confirmation with the draft so the "
                "approval binds to it"
            )
        if draft_fingerprint(draft) != stored_hash:
            raise ConfirmationMismatch(
                f"the draft being locked does not match the draft filed for "
                f"review on confirmation {confirmation_id!r}; refusing to "
                "lock"
            )
        locked = draft_to_locked_plan(
            draft, job_id, human_confirmed_by=decided_by
        )
        if str(locked.job_type) != str(row["job_type"]):
            raise ConfirmationMismatch(
                f"confirmation {confirmation_id!r} is a "
                f"{row['job_type']!r} approval but the draft locks as "
                f"{locked.job_type!r}; refusing to lock"
            )
        cur = conn.execute(
            """UPDATE plan_confirmations
               SET status = 'APPROVED', decided_by = ?, decided_at = ?
               WHERE id = ? AND status = 'PENDING'""",
            (decided_by, _utc_now(), confirmation_id),
        )
        if cur.rowcount != 1:
            raise ValueError(
                f"confirmation {confirmation_id!r} was decided concurrently; "
                "refusing to lock"
            )
        plan_lock._insert_locked_plan(conn, locked)
    return locked
