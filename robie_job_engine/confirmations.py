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

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Iterator, Mapping

from .evidence import EvidenceSpan
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


# ---------------------------------------------------------------------------
# Decision tokens
#
# A decision typed into an unauthenticated surface (the board sheet, where
# any editor can type APPROVE and write any name in "Decided by") is not an
# authorization. Decisions are authorized by an HMAC-signed token that an
# authenticated adapter (e.g. the Chat HITL ping, where the platform
# authenticates the user) mints for one confirmation, one decision, and one
# principal. The token is what the human pastes onto the sheet; ingestion
# trusts only the token, never the typed name.
# ---------------------------------------------------------------------------

DECISION_TOKEN_ENV = "ROBIE_DECISION_SIGNING_KEY"
DECISION_TOKEN_PREFIX = "rbd1"
DEFAULT_TOKEN_TTL_SECONDS = 7 * 24 * 3600
_DECISIONS = ("APPROVE", "REJECT")


def decision_signing_key(key: str | bytes | None = None) -> bytes | None:
    """Resolve the HMAC key for decision tokens.

    Explicit ``key`` wins; otherwise the ROBIE_DECISION_SIGNING_KEY env var
    (a secret mount, never repo-committed). Returns None when no key is
    configured -- callers must fail closed (display-only surfaces).
    """
    if key is None:
        env = os.environ.get(DECISION_TOKEN_ENV, "").strip()
        if not env:
            return None
        key = env
    if isinstance(key, str):
        key = key.encode("utf-8")
    if len(key) < 16:
        raise ValueError("decision signing key must be at least 16 bytes")
    return key


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def mint_decision_token(
    confirmation_id: str,
    decision: str,
    principal: str,
    *,
    key: str | bytes | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> str:
    """Mint a signed decision token for one confirmation + decision + principal.

    Only an adapter that has already authenticated ``principal`` may mint.
    The token expires after ``ttl_seconds`` (default 7 days).
    """
    signing = decision_signing_key(key)
    if signing is None:
        raise RuntimeError(
            f"no decision signing key configured ({DECISION_TOKEN_ENV}); "
            "refusing to mint an unsigned decision"
        )
    confirmation_id = _require_nonempty(confirmation_id, "confirmation_id")
    principal = _require_nonempty(principal, "principal")
    decision = str(decision or "").strip().upper()
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be APPROVE or REJECT, got {decision!r}")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be > 0")
    moment = now or datetime.now(timezone.utc)
    expires = int(moment.timestamp()) + int(ttl_seconds)
    payload = f"{confirmation_id}|{decision}|{principal}|{expires}"
    sig = hmac.new(signing, payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{DECISION_TOKEN_PREFIX}.{_b64e(payload.encode('utf-8'))}.{_b64e(sig)}"


def verify_decision_token(
    token: str,
    *,
    key: str | bytes | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Verify a decision token. Returns {confirmation_id, decision, principal}.

    Raises ValueError on a malformed, forged, or expired token, and
    RuntimeError when no signing key is configured.
    """
    signing = decision_signing_key(key)
    if signing is None:
        raise RuntimeError(
            f"no decision signing key configured ({DECISION_TOKEN_ENV}); "
            "decision tokens cannot be verified"
        )
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != DECISION_TOKEN_PREFIX:
        raise ValueError("malformed decision token")
    try:
        payload = _b64d(parts[1]).decode("utf-8")
        sig = _b64d(parts[2])
    except Exception as exc:
        raise ValueError("malformed decision token") from exc
    expected = hmac.new(signing, payload.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("decision token signature mismatch")
    fields = payload.split("|")
    if len(fields) != 4:
        raise ValueError("malformed decision token payload")
    confirmation_id, decision, principal, expires = fields
    if decision not in _DECISIONS:
        raise ValueError(f"token carries invalid decision {decision!r}")
    try:
        expires_at = int(expires)
    except ValueError as exc:
        raise ValueError("malformed decision token expiry") from exc
    moment = now or datetime.now(timezone.utc)
    if int(moment.timestamp()) > expires_at:
        raise ValueError("decision token expired")
    if not confirmation_id or not principal:
        raise ValueError("decision token payload is incomplete")
    return {
        "confirmation_id": confirmation_id,
        "decision": decision,
        "principal": principal,
    }


def has_confirmation(db_path: str, confirmation_id: str) -> bool:
    """Whether ``db_path`` already contains this confirmation id.

    Read-only. A missing file, a missing ``plan_confirmations`` table, or
    any sqlite error means this gateway does not own the click. This never
    calls ``_ensure_schema`` and never creates a table.
    """
    confirmation_id = str(confirmation_id or "").strip()
    if not confirmation_id:
        return False
    from pathlib import Path

    path = Path(db_path)
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("plan_confirmations",),
        ).fetchone()
        if present is None:
            return False
        row = conn.execute(
            "SELECT 1 FROM plan_confirmations WHERE id = ?",
            (confirmation_id,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def read_confirmation_status(db_path: str, confirmation_id: str) -> str:
    """Return the stored status, or "" when the row or table is absent.

    Read-only, same rules as ``has_confirmation``: never creates schema.
    Used to re-read a card click before the Chat update so a lost race
    cannot claim a fresh decision.
    """
    confirmation_id = str(confirmation_id or "").strip()
    if not confirmation_id:
        return ""
    from pathlib import Path

    path = Path(db_path)
    if not path.is_file():
        return ""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("plan_confirmations",),
        ).fetchone()
        if present is None:
            return ""
        row = conn.execute(
            "SELECT status FROM plan_confirmations WHERE id = ?",
            (confirmation_id,),
        ).fetchone()
        if row is None:
            return ""
        return str(row[0] or "").strip()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def peek_confirmation_id(token: str) -> str:
    """Read the confirmation id from an rbd1 token without checking the HMAC.

    This is not authorization. A gateway uses it only to see whether that
    id exists in its own database before it verifies or replies. Returns
    "" when the token is not a readable rbd1 payload.
    """
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != DECISION_TOKEN_PREFIX:
        return ""
    try:
        payload = _b64d(parts[1]).decode("utf-8")
    except Exception:
        return ""
    confirmation_id = payload.split("|", 1)[0].strip()
    if not confirmation_id or len(confirmation_id) > 200:
        return ""
    if any(char in confirmation_id for char in "\r\n\x00"):
        return ""
    return confirmation_id


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
    ``confirm_and_lock`` must file the fingerprint here. When ``draft`` is
    given, its evidence spans are also filed on the record (under
    ``field_spans`` in the changes payload) so the approval surfaces --
    sheet, Chat ping, email -- can show each value's quote and source, not
    just the value.
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
        changes_json = _with_draft_spans(changes_json, draft)
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


def _with_draft_spans(changes_json: Any, draft: Any) -> Any:
    """Merge a draft's evidence spans into the filed changes payload.

    The approval renderers (``confirmation_summary``, the board's Details
    column) read ``field_spans`` from this payload to show each value's
    quote and source. Callers that file an explicit changes payload keep
    it -- spans are added under the ``field_spans`` key, nothing else is
    reshaped. A legacy string span degrades to a quote-only entry.
    """
    spans = getattr(draft, "evidence_spans", None) or {}
    if not isinstance(spans, Mapping) or not spans:
        return changes_json
    span_payload: dict[str, Any] = {}
    for name, span in spans.items():
        if isinstance(span, EvidenceSpan):
            span_payload[str(name)] = span.to_dict()
        else:
            span_payload[str(name)] = {"quote": str(span)}
    base: dict[str, Any] = {}
    if isinstance(changes_json, str) and changes_json.strip():
        try:
            parsed = json.loads(changes_json)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, Mapping):
            base = dict(parsed)
    elif isinstance(changes_json, Mapping):
        base = dict(changes_json)
    base["field_spans"] = span_payload
    base.setdefault("policy_number", getattr(draft, "policy_number", None))
    base.setdefault("applicant_id", getattr(draft, "applicant_id", None))
    changes = getattr(draft, "changes", None)
    if isinstance(changes, Mapping):
        base.setdefault("changes", dict(changes))
    return base


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
        # Compare-and-set: a second click that passed the read above must
        # not overwrite the decision that won the race.
        updated = conn.execute(
            """UPDATE plan_confirmations
               SET status = ?, decided_by = ?, decided_at = ?,
                   decision_reason = ?
               WHERE id = ? AND status = 'PENDING'""",
            (new_status, decided_by, now, str(reason or ""), confirmation_id),
        )
        if updated.rowcount != 1:
            current = conn.execute(
                "SELECT status FROM plan_confirmations WHERE id = ?",
                (confirmation_id,),
            ).fetchone()
            seen = str(current["status"]) if current is not None else "UNKNOWN"
            raise ValueError(
                f"confirmation {confirmation_id!r} is already {seen}; "
                "only PENDING confirmations can be decided"
            )
        # Append-only audit of the approval decision (H4). Lives in the same
        # transaction as the status flip, so the audit row commits or rolls
        # back with the decision itself.
        from . import plan_lock as _plan_lock

        _plan_lock.append_transition(
            conn,
            str(row["loop_job_id"]),
            "approved" if new_status == "APPROVED" else "rejected",
            actor=decided_by,
            detail={
                "confirmation_id": confirmation_id,
                "status": new_status,
                "reason": str(reason or ""),
            },
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


_CARD_DISPLAY_TZ = ZoneInfo("America/New_York")


def _friendly_datetime(value: Any) -> str:
    """Card clock in Eastern Time. The label is ET in both EST and EDT."""
    text = str(value or "").strip()
    if not text:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_CARD_DISPLAY_TZ)
        return local.strftime("%b %-d, %Y %-I:%M %p") + " ET"
    except (ValueError, TypeError):
        return text


def _evidence_clause(changes: Mapping[str, Any]) -> str:
    """Plain-English evidence trailer: each value's quote and source.

    Appended to confirmation summaries so the human reviews provenance,
    not just bare values. Empty when the record carries no spans.
    """
    spans = changes.get("field_spans")
    if not isinstance(spans, Mapping) or not spans:
        return ""
    parts: list[str] = []
    for name, raw in spans.items():
        if isinstance(raw, Mapping):
            source = str(raw.get("source_id") or "").strip() or "unknown source"
            quote = str(raw.get("quote") or "").strip()
        else:
            source, quote = "unknown source", str(raw)
        snippet = (quote[:60] + "...") if len(quote) > 60 else quote
        parts.append(f'{name} from {source} ("{snippet}")')
    return " Evidence: " + "; ".join(parts) + "."


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
    evidence = _evidence_clause(changes)
    status = str(record.get("status") or "")
    requested_by = str(record.get("requested_by") or "unknown")
    decided_by = str(record.get("decided_by") or "unknown")
    reason = str(record.get("decision_reason") or "").strip()
    if status == "PENDING":
        return (
            f"{subject} is waiting for approval "
            f"(requested by {requested_by} on "
            f"{_friendly_datetime(record.get('created_at'))}).{evidence}"
        )
    if status == "APPROVED":
        return (
            f"{subject} was approved by {decided_by} on "
            f"{_friendly_datetime(record.get('decided_at'))}.{evidence}"
        )
    if status == "REJECTED":
        base = (
            f"{subject} was rejected by {decided_by} on "
            f"{_friendly_datetime(record.get('decided_at'))}."
        )
        if reason:
            base += f" Reason: {reason}."
        return base + evidence
    if status == "EXPIRED":
        return (
            f"{subject} expired with no decision "
            f"(requested by {requested_by} on "
            f"{_friendly_datetime(record.get('created_at'))}).{evidence}"
        )
    return f"{subject} has status {status}.{evidence}"


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
        # Append-only audit of the approval decision (H4): same transaction
        # as the approval flip and the lock insert below.
        plan_lock.append_transition(
            conn,
            job_id,
            "approved",
            actor=decided_by,
            detail={"confirmation_id": confirmation_id, "status": "APPROVED"},
        )
        plan_lock._insert_locked_plan(conn, locked)
    return locked
