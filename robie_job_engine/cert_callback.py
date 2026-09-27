"""Certificates chunk 4: nonce-guarded Zap callback proof.

EZLynx exposes no Task API (Postman docs contain zero task endpoints;
our OAuth token request for a ``TaskApi`` scope returns
``400 invalid_scope`` — confirmed 2026-09-27). The worker therefore
cannot read a task back directly.

Fallback: the Zap's final step sends a structured callback (email to the
polled certificates mailbox) echoing a per-fire ``filing_id``. This module:

- mints the ``filing_id`` (one per Zap fire),
- records pending fires,
- parses + validates callbacks against the pending fire they claim,
- exposes the ``task_prover`` callable the filing path needs.

Proof model (revised 2026-09-27 after live Zap inspection): the Zap's
EZLynx step is "Create Note", not "Create Task" — its test was skipped,
so no task/note ID is mappable into a callback email. Zapier executes
steps strictly in order, so the callback email is sent ONLY when the
Create Note step succeeded. The email's existence — carrying a
``filing_id`` nonce minted by THIS worker for THIS fire, plus matching
applicant and assignee — IS the destination proof. An EZLynx task/note
ID is accepted opportunistically when present (``task_id``), but it is
not required and its absence never fails a filing.

Validation is fail-closed: a callback is accepted only when its
``filing_id`` matches a pending fire recorded by THIS worker, the
applicant matches, the assignee is exactly ``SCanales``, and the status
is open. A ``filing_id`` can be consumed once — replays are rejected.
Anything else leaves the filing UNVERIFIED.

Sender authenticity (defense in depth on the nonce): when
``CERT_CALLBACK_SENDERS`` is set, the callback must come From an
allowlisted address/domain; when ``CERT_CALLBACK_SECRET`` is set, the
body must echo ``auth: <secret>`` (a static value in the Zap's callback
step). Either check failing rejects the callback — the filing stays
UNVERIFIED.

Trust note (stated plainly): this is the Zap's own success signal
relayed through Zapier, not an independent API read-back. A real Task
API (question sent to EZLynx Web Services 2026-09-27) remains the
stronger proof.
"""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

#: The certificates reviewer — the only acceptable callback assignee.
ASSIGNEE_SCANALES = "SCanales"

#: Subject prefix the Zap's callback email must carry.
CALLBACK_SUBJECT_PREFIX = "[cert-task-callback]"

#: Gmail search for callback emails (tuned in ingest_callback_emails).
CALLBACK_GMAIL_QUERY = (
    f'subject:"{CALLBACK_SUBJECT_PREFIX}" newer_than:7d'
)

#: Callback body fields, one per line, ``key: value``.
#: ``task_id`` is opportunistic (the Zap's Create Note step exposes no
#: mappable ID) — accepted when present, never required.
_BODY_RE = re.compile(r"^(?P<key>[A-Za-z_]+)\s*:\s*(?P<value>.*)$")

#: Env var: comma-separated allowlist of acceptable callback senders.
#: Each entry is a full address (``zapier@zapier.com``) or a domain
#: (``zapier.com`` / ``@zapier.com``), matched case-insensitively
#: against the From header's address. Empty = sender check skipped.
CALLBACK_SENDERS_ENV = "CERT_CALLBACK_SENDERS"

#: Env var: shared secret the Zap's callback email must echo as an
#: ``auth:`` body field. Empty = secret check skipped. Set it in
#: production: without it, anyone who observes a filing_id in flight
#: (e.g. Zapier task history) could forge a callback.
CALLBACK_SECRET_ENV = "CERT_CALLBACK_SECRET"

#: Callback body field carrying the shared secret (when configured).
_AUTH_FIELD = "auth"


def require_callback_auth() -> tuple[str, str]:
    """Fail closed unless callback sender/secret auth is configured.

    Returns ``(senders, secret)`` when both ``CERT_CALLBACK_SENDERS``
    and ``CERT_CALLBACK_SECRET`` are set. Raises ``RuntimeError``
    otherwise — the sweep must not file anything (no document, note,
    or Zap write) when the callback cannot be authenticated, because
    an unauthenticated callback is forgeable by anyone who observes a
    filing_id in flight.

    Call once at sweep startup (from ``build_filing_deps``), before
    any filing dependency is constructed.
    """
    senders = (os.environ.get(CALLBACK_SENDERS_ENV) or "").strip()
    secret = (os.environ.get(CALLBACK_SECRET_ENV) or "").strip()
    missing = []
    if not senders:
        missing.append(CALLBACK_SENDERS_ENV)
    if not secret:
        missing.append(CALLBACK_SECRET_ENV)
    if missing:
        raise RuntimeError(
            f"callback auth not configured ({', '.join(missing)} "
            "missing) — refusing to file: without sender allowlist and "
            "shared secret, Zap callbacks cannot be authenticated and "
            "any filing would be UNVERIFIED-by-design; set both env vars "
            "before enabling the sweep"
        )
    return senders, secret


def _extract_address(from_header: str) -> str:
    """Pull the bare address out of a From header.

    ``"Zapier <zapier@zapier.com>"`` -> ``"zapier@zapier.com"``.
    Never raises.
    """
    try:
        header = (from_header or "").strip()
        m = re.search(r"<([^<>@\s]+@[^<>@\s]+)>", header)
        if m:
            return m.group(1).lower()
        m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", header)
        return m.group(0).lower() if m else ""
    except Exception:
        return ""


def _sender_allowed(from_header: str, allowlist: str) -> bool:
    """True when the From address matches the allowlist.

    An allowlist entry matches a full address exactly, or a domain
    entry matches the address's domain. Empty allowlist = no
    constraint (caller decides whether that is acceptable).
    """
    try:
        addr = _extract_address(from_header)
        if not addr or "@" not in addr:
            return False
        domain = addr.split("@", 1)[1]
        for entry in (allowlist or "").split(","):
            e = entry.strip().lower().lstrip("@")
            if not e:
                continue
            if "@" in e:
                if addr == e:
                    return True
            elif domain == e or domain.endswith("." + e):
                # exact domain or any subdomain (mail.zapier.com)
                return True
        return False
    except Exception:
        return False

_REQUIRED_FIELDS = ("filing_id", "applicant_id", "assignee", "status")


def new_filing_id() -> str:
    """Mint a unique filing ID for one Zap fire."""
    return uuid.uuid4().hex


@dataclass
class PendingFire:
    filing_id: str
    applicant_id: int
    policy_key: str
    holder_key: str
    title: str
    assignee: str
    fired_at: float


@dataclass
class TaskProof:
    filing_id: str
    task_id: str
    applicant_id: int
    assignee: str
    status: str
    title: str
    proven_at: float


class CallbackStore:
    """SQLite-backed pending-fire + validated-proof store.

    Same crash-safe spirit as the task registry: a fire recorded here
    survives worker restarts, so a re-drive never re-fires the Zap while
    a callback is still in flight.
    """

    def __init__(self, db_path: str) -> None:
        self._db = sqlite3.connect(db_path, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cert_callback_fires (
                filing_id TEXT PRIMARY KEY,
                applicant_id INTEGER NOT NULL,
                policy_key TEXT NOT NULL,
                holder_key TEXT NOT NULL,
                title TEXT NOT NULL,
                assignee TEXT NOT NULL,
                fired_at REAL NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cert_callback_proofs (
                filing_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                applicant_id INTEGER NOT NULL,
                assignee TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                proven_at REAL NOT NULL
            )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cert_callback_emails (
                gmail_id TEXT PRIMARY KEY,
                processed_at REAL NOT NULL
            )"""
        )
        self._db.commit()

    # -- fires ----------------------------------------------------------
    def record_fire(self, *, filing_id: str, applicant_id: int,
                    policy_key: str, holder_key: str, title: str,
                    assignee: str) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO cert_callback_fires
               (filing_id, applicant_id, policy_key, holder_key, title,
                assignee, fired_at, consumed)
               VALUES (?,?,?,?,?,?,?,0)""",
            (filing_id, applicant_id, policy_key, holder_key, title,
             assignee, time.time()),
        )
        self._db.commit()

    def find_pending(self, applicant_id: int, policy_key: str,
                     holder_key: str) -> PendingFire | None:
        """Newest unconsumed fire for this request, if any."""
        row = self._db.execute(
            """SELECT filing_id, applicant_id, policy_key, holder_key,
                      title, assignee, fired_at
               FROM cert_callback_fires
               WHERE applicant_id=? AND policy_key=? AND holder_key=?
                 AND consumed=0
               ORDER BY fired_at DESC LIMIT 1""",
            (applicant_id, policy_key, holder_key),
        ).fetchone()
        return PendingFire(*row) if row else None

    def get_fire(self, filing_id: str) -> PendingFire | None:
        row = self._db.execute(
            """SELECT filing_id, applicant_id, policy_key, holder_key,
                      title, assignee, fired_at
               FROM cert_callback_fires WHERE filing_id=?""",
            (filing_id,),
        ).fetchone()
        return PendingFire(*row) if row else None

    # -- proofs ---------------------------------------------------------
    def validate_callback(self, *, filing_id: str, task_id: str,
                          applicant_id: int, assignee: str,
                          status: str, title: str = ""
                          ) -> tuple[bool, str]:
        """Validate a callback against its pending fire.

        Returns (accepted, reason). On acceptance the fire is consumed
        and the proof stored. Never raises on bad input — a bad callback
        is rejected, not fatal.
        """
        fire = self.get_fire(filing_id)
        if fire is None:
            return False, (f"rejected: filing_id {filing_id!r} matches no "
                           "fire recorded by this worker")
        if self._db.execute(
                "SELECT 1 FROM cert_callback_proofs WHERE filing_id=?",
                (filing_id,)).fetchone():
            return False, (f"rejected: filing_id {filing_id!r} already "
                           "consumed (replay)")
        if int(applicant_id or 0) != int(fire.applicant_id):
            return False, (
                f"rejected: callback applicant {applicant_id} != fired "
                f"applicant {fire.applicant_id}")
        if (assignee or "").strip() != ASSIGNEE_SCANALES:
            return False, (
                f"rejected: callback assignee {assignee!r} != "
                f"{ASSIGNEE_SCANALES!r}")
        # NOTE: task_id is NOT required. The Zap's EZLynx step is
        # "Create Note" with no mappable ID output (verified 2026-09-27);
        # the callback's existence after that sequential step IS the
        # proof. An ID is stored when supplied, for correlation.
        if (status or "").strip().lower() not in ("open", "scheduled"):
            return False, (f"rejected: callback status {status!r} is not an "
                           "open task state")
        now = time.time()
        self._db.execute(
            """INSERT OR IGNORE INTO cert_callback_proofs
               (filing_id, task_id, applicant_id, assignee, status, title,
                proven_at)
               VALUES (?,?,?,?,?,?,?)""",
            (filing_id, (task_id or "").strip(), int(applicant_id),
             ASSIGNEE_SCANALES, status.strip(), title or fire.title, now),
        )
        self._db.execute(
            "UPDATE cert_callback_fires SET consumed=1 WHERE filing_id=?",
            (filing_id,),
        )
        self._db.commit()
        proven_id = (task_id or "").strip()
        return True, (f"accepted: callback proven for filing "
                       f"{filing_id!r}"
                       + (f" (task {proven_id})" if proven_id else ""))

    def get_proof(self, applicant_id: int, policy_key: str,
                  holder_key: str) -> TaskProof | None:
        """Validated proof for this request, if the callback arrived."""
        row = self._db.execute(
            """SELECT p.filing_id, p.task_id, p.applicant_id, p.assignee,
                      p.status, p.title, p.proven_at
               FROM cert_callback_proofs p
               JOIN cert_callback_fires f ON f.filing_id = p.filing_id
               WHERE f.applicant_id=? AND f.policy_key=? AND f.holder_key=?
               ORDER BY p.proven_at DESC LIMIT 1""",
            (applicant_id, policy_key, holder_key),
        ).fetchone()
        return TaskProof(*row) if row else None

    def get_proof_by_title(self, applicant_id: int,
                           title: str) -> TaskProof | None:
        """Proof lookup for the legacy ``task_prover`` seam
        ``(applicant_id, title)``."""
        row = self._db.execute(
            """SELECT p.filing_id, p.task_id, p.applicant_id, p.assignee,
                      p.status, p.title, p.proven_at
               FROM cert_callback_proofs p
               JOIN cert_callback_fires f ON f.filing_id = p.filing_id
               WHERE f.applicant_id=? AND f.title=?
               ORDER BY p.proven_at DESC LIMIT 1""",
            (applicant_id, title),
        ).fetchone()
        return TaskProof(*row) if row else None

    def get_proof_by_filing(self, filing_id: str) -> TaskProof | None:
        """Nonce-precise proof lookup: only a callback echoing THIS fire's
        ``filing_id`` counts. Use for the fire-then-prove step so a
        same-title proof from an older fire can never be mistaken."""
        row = self._db.execute(
            """SELECT filing_id, task_id, applicant_id, assignee,
                      status, title, proven_at
               FROM cert_callback_proofs WHERE filing_id=?""",
            (filing_id or "",),
        ).fetchone()
        return TaskProof(*row) if row else None

    # -- email bookkeeping ----------------------------------------------
    def email_processed(self, gmail_id: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM cert_callback_emails WHERE gmail_id=?",
            (gmail_id,)).fetchone() is not None

    def mark_email_processed(self, gmail_id: str) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO cert_callback_emails
               (gmail_id, processed_at) VALUES (?,?)""",
            (gmail_id, time.time()),
        )
        self._db.commit()


def parse_callback_email(subject: str, body: str) -> dict[str, str] | None:
    """Parse the Zap's structured callback email.

    Returns the field dict, or None when this is not a callback email.
    Never raises on malformed input.
    """
    try:
        if CALLBACK_SUBJECT_PREFIX not in (subject or ""):
            return None
        fields: dict[str, str] = {}
        for line in (body or "").splitlines():
            m = _BODY_RE.match(line.strip())
            if m:
                fields[m.group("key").strip().lower()] = \
                    m.group("value").strip()
        if not all(fields.get(k) for k in _REQUIRED_FIELDS):
            return None
        return fields
    except Exception:
        return None


def _message_text(full_message: dict) -> str:
    """Best-effort plain-text extraction from a Gmail full message."""
    try:
        payload = full_message.get("payload", {}) or {}
        parts: list[dict] = []

        def walk(p: dict) -> None:
            parts.append(p)
            for sub in p.get("parts", []) or []:
                walk(sub)

        walk(payload)
        for p in parts:
            if "text/plain" in str(p.get("mimeType", "")):
                data = (p.get("body", {}) or {}).get("data", "")
                if data:
                    import base64
                    return base64.urlsafe_b64decode(data + "===").decode(
                        "utf-8", "replace")
        # fallback: snippet
        return str(full_message.get("snippet", "") or "")
    except Exception:
        return ""


def ingest_callback_emails(*, gmail: Any, store: CallbackStore,
                           query: str = CALLBACK_GMAIL_QUERY,
                           max_messages: int = 25,
                           expected_senders: str | None = None,
                           auth_secret: str | None = None,
                           ) -> dict[str, int]:
    """Poll the mailbox for Zap callback emails and validate them.

    Runs at the start of each sweep, before intake. ``gmail`` needs
    ``list_message_ids(query, max_results)`` and
    ``get_full_message(gmail_id)`` (the CertGmailAdapter shape).
    Returns counts; never raises — ingestion is best-effort, a missed
    callback just means the filing stays UNVERIFIED this sweep.

    Sender authenticity (defense in depth on top of the unguessable
    single-use filing_id nonce):

    - ``expected_senders`` (default: ``CERT_CALLBACK_SENDERS`` env):
      comma-separated allowlist of sender addresses/domains the
      callback must come From. A callback from any other sender is
      rejected. Empty = check skipped.
    - ``auth_secret`` (default: ``CERT_CALLBACK_SECRET`` env): when
      set, the callback body must carry ``auth: <secret>`` (the Zap's
      callback step echoes this static value). Compared with
      ``hmac.compare_digest``. Missing or wrong secret = rejected.

    Rejection is fail-closed: the filing the callback claims stays
    UNVERIFIED and will be retried on a later sweep.
    """
    if expected_senders is None:
        expected_senders = os.environ.get(CALLBACK_SENDERS_ENV, "")
    if auth_secret is None:
        auth_secret = os.environ.get(CALLBACK_SECRET_ENV, "")
    stats = {"found": 0, "parsed": 0, "accepted": 0, "rejected": 0,
             "errors": 0}
    try:
        ids = gmail.list_message_ids(query, max_messages) or []
    except Exception:
        return stats
    for gmail_id in ids:
        stats["found"] += 1
        try:
            if store.email_processed(gmail_id):
                continue
            full = gmail.get_full_message(gmail_id) or {}
            headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in (full.get("payload", {}) or {}).get(
                           "headers", []) or []}
            subject = headers.get("subject", "")
            if expected_senders and not _sender_allowed(
                    headers.get("from", ""), expected_senders):
                store.mark_email_processed(gmail_id)
                stats["rejected"] += 1
                continue
            fields = parse_callback_email(subject, _message_text(full))
            if fields is None:
                store.mark_email_processed(gmail_id)
                continue
            if auth_secret and not hmac.compare_digest(
                    str(fields.get(_AUTH_FIELD, "")), auth_secret):
                store.mark_email_processed(gmail_id)
                stats["rejected"] += 1
                continue
            stats["parsed"] += 1
            ok, _reason = store.validate_callback(
                filing_id=fields["filing_id"],
                task_id=fields.get("task_id", ""),
                applicant_id=int(fields["applicant_id"]),
                assignee=fields["assignee"],
                status=fields["status"],
                title=fields.get("title", ""),
            )
            stats["accepted" if ok else "rejected"] += 1
            store.mark_email_processed(gmail_id)
        except Exception:
            stats["errors"] += 1
    return stats


def callback_task_prover(store: CallbackStore):
    """Build the ``task_prover`` callable for ``FilingDeps``.

    ``(applicant_id, task_title) -> {"task_id": str, "assignee": str}`` —
    backed by validated Zap callbacks. ``task_id`` may be ``""``: the
    Zap's Create Note step exposes no mappable ID, so the validated
    callback itself (filing nonce + applicant + assignee) is the proof.
    Empty dict when no proof exists yet; the filing path fails closed
    on that.
    """

    def prove(applicant_id: Any, task_title: str) -> dict[str, str]:
        try:
            proof = store.get_proof_by_title(int(applicant_id),
                                            task_title or "")
        except Exception:
            return {}
        if proof is None:
            return {}
        return {"task_id": proof.task_id, "assignee": proof.assignee}

    return prove
