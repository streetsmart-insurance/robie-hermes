"""Certificate sweep driver: intake -> verify -> file runner.

One sweep, wired end to end for the 5-minute cron:

1. Read-only Gmail intake of ``certificates@streetsmart.insurance`` via
   :class:`CertGmailAdapter` (DWD, ``gmail.readonly`` — the adapter can
   never mark, move, send, or delete).
2. Applicant matching against the full-book CSV index
   (``CERT_APPLICANT_INDEX_PATH``).
3. Identity verification via :func:`verify_record` (read-only policy
   anchors; :class:`EzlynxReadClient`).
4. Filing of VERIFIED records via :func:`file_record` with production
   :class:`FilingDeps`: email PDF + every attachment to Documents (with
   read-back), summary note, and the Steffany Canales review task through
   the Zapier certificate workflow.

CLI contract (the scheduler depends on it)::

    python -m robie_job_engine.cert_sweep --once

Exit 0 = the sweep completed cleanly (even when nothing was new).
Exit 2 = usage/config error (missing ``--once``, missing/unreadable index
CSV, missing Zapier trigger script, bad data dir).
Exit 1 = runtime failure (Gmail down, EZLynx API config broken, unexpected
exception).

Stdout is a single JSON summary with keys ``filed`` (list), ``unverified``
(list), ``errors`` (list), plus run metadata. UNVERIFIED records are a
designed fail-closed outcome, not an exit-code error — unless the sweep
itself could not file anything at all (e.g. the Zapier task path is
broken), which is exit 1.

Safety rules (non-negotiable):

- Today-forward only: the sweep processes messages with Gmail
  internalDate >= 2026-09-27 00:00 America/New_York (``CUTOFF_ET``).
  Older mail is evaluated and excluded by policy — never matched,
  never filed, never re-driven. The historical retry backlog is parked
  in the ledger (kept for audit, attempts pinned past the max) after a
  date-check against each message's internalDate.
- Read-only until the match is proven: no note, document, or task is ever
  written for an unverified or ambiguous match.
- Gmail intake is read-only by design. The one write the driver ever makes
  is removing the UNREAD label, and only after a FILED outcome (every
  document and Steffany's task destination-proven). Anything UNVERIFIED
  or errored stays unread. The modify call uses a separate
  ``gmail.modify`` token (``CERT_GMAIL_MARK_READ=0`` disables it); the
  read-only intake session is never widened. A mark-read failure never
  fails a proven filing — the message simply stays unread and the
  checkpoint still prevents re-filing.
- GETs retry with backoff (inside the adapters); POSTs are never
  blind-retried — the filing library reads the destination back before
  any re-send, and fails UNVERIFIED when the read-back cannot complete.
- Anything UNVERIFIED is not filed and not tasked. The record goes into
  the retry ledger so a later sweep re-attempts it (bounded attempts);
  the intake checkpoint is not the retry ledger — intake marks a message
  ``intake_complete`` exactly once, and this driver re-drives held
  records itself.
- If the Zapier trigger script is absent, filing does not proceed at
  all: documents without Steffany's proven task are a partial state, so
  every candidate ends UNVERIFIED instead.
- No secrets in code, logs, or the JSON summary. Applicant IDs and
  document names are logged; token material never is.

Durable state (all under ``CERT_SWEEP_DATA_DIR``, default
``~/.cert-sweep/`` — never /tmp, so it survives restarts):

- ``cert-sweep.db``: ONE SQLite file holding all four tables, so the
  whole sweep state backs up as a single file:
  - ``dedupe_keys``   — intake checkpoint (:class:`SqliteDedupeStore`),
    exactly-once per message;
  - ``cert_filing``   — per-message filing state + worker leases
    (:class:`FilingStore`);
  - ``cert_tasks``    — applicant/policy/holder -> task registry
    (:class:`TaskRegistry`);
  - ``cert_sweep_retry`` — this driver's retry ledger for UNVERIFIED
    records. Rows whose message predates the today-forward cutoff are
    parked (attempts pinned past the max, reason prefixed ``[PARKED``) —
    kept for audit, never re-driven, never deleted.
  - ``cert_index_misses`` — NO_MATCH insured names from current
    (post-cutoff) requests, for the governed index-refresh loop
    (``cert_index_refresh.py`` reports which misses a fresh export
    resolves).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Today-forward cutoff (Carlo 2026-09-27: "Just the current ones as of
#: today, going forward"). The sweep processes only certificate emails
#: with Gmail internalDate (receive time) >= this instant. Everything
#: older is evaluated and excluded by policy — never matched, never
#: filed, never re-driven. The cutoff is an explicit constant (not
#: "now minus N days") so its meaning never drifts between runs.
#:
#: Timezone note: 2026-09-27 00:00 America/New_York == 2026-09-27T04:00Z
#: (EDT, UTC-4). The comparison is done on the epoch-millisecond
#: internalDate, so the boundary is exact regardless of the sender's
#: Date: header or the server's local timezone.
CUTOFF_ET = datetime(2026, 9, 27, 0, 0, tzinfo=ZoneInfo("America/New_York"))
CUTOFF_MS = int(CUTOFF_ET.timestamp() * 1000)

#: Gmail ``after:`` is date-granular and account-timezone dependent, so the
#: discovery query keeps one day of slack before the cutoff and the
#: authoritative check is the client-side internalDate comparison above.
#: Without the slack, a message received just after midnight ET could be
#: missed on the day the account timezone disagrees with ET.
QUERY_SLACK_DAYS = 1

#: Retry ledger bounds: at a 5-minute cadence, 288 attempts ~= 24 hours of
#: retries before the record is parked for human review instead of being
#: re-driven forever.
RETRY_MAX_ATTEMPTS = 288

#: Pre-cutoff backlog migration: how many retry-ledger rows to date-check
#: per sweep (oldest first). The historical backlog is a few hundred rows;
#: the cap keeps one run bounded while the migration converges.
MIGRATION_BATCH_LIMIT = 200

#: A retry-ledger message that cannot be fetched from Gmail this many
#: consecutive times is treated as gone (deleted/expired) and parked —
#: kept in the ledger, never re-driven.
FETCH_FAILURES_BEFORE_PARK = 12

DB_FILENAME = "cert-sweep.db"

EXIT_USAGE = 2
EXIT_RUNTIME = 1


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------

_RETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS cert_sweep_retry (
    gmail_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    fetch_failures INTEGER NOT NULL DEFAULT 0
);
"""

_MISS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cert_index_misses (
    name_key TEXT PRIMARY KEY,
    raw_name TEXT NOT NULL,
    gmail_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    resolved_applicant_id INTEGER,
    policy_numbers TEXT NOT NULL DEFAULT ''
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """Idempotent ALTER for DBs created before a column existed."""
    cols = [r[1] for r in conn.execute(
        f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()


def data_dir() -> str:
    """Sweep data dir; created when missing. Fail closed when unusable."""
    path = os.path.expanduser(_env("CERT_SWEEP_DATA_DIR", "~/.cert-sweep"))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot create CERT_SWEEP_DATA_DIR {path}: {exc}")
    probe = os.path.join(path, ".write-probe")
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        raise RuntimeError(f"CERT_SWEEP_DATA_DIR {path} is not writable: {exc}")
    return path


def open_retry_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_RETRY_SCHEMA)
    conn.execute(_MISS_SCHEMA)
    # Older DBs predate fetch_failures: add it without touching rows.
    _ensure_column(conn, "cert_sweep_retry", "fetch_failures",
                   "INTEGER NOT NULL DEFAULT 0")
    # Older DBs predate the policy_numbers miss column (added with the
    # 2026-09-29 EPHE policy-matcher fix): add it without touching rows.
    _ensure_column(conn, "cert_index_misses", "policy_numbers",
                   "TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn


def retry_pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT gmail_id, reason, attempts, first_seen_at, last_attempt_at"
        " FROM cert_sweep_retry ORDER BY first_seen_at"
    ).fetchall()
    return [
        {"gmail_id": r[0], "reason": r[1], "attempts": r[2],
         "first_seen_at": r[3], "last_attempt_at": r[4]}
        for r in rows
    ]


def retry_note(conn: sqlite3.Connection, gmail_id: str,
               reason: str) -> dict[str, Any]:
    """Record an UNVERIFIED outcome. Returns the ledger entry.

    Attempts beyond RETRY_MAX_ATTEMPTS park the record for human review
    (it stays listed, but later sweeps stop re-driving it).
    """
    now = _utcnow_iso()
    row = conn.execute(
        "SELECT attempts, first_seen_at FROM cert_sweep_retry"
        " WHERE gmail_id = ?",
        (gmail_id,),
    ).fetchone()
    if row:
        attempts, first_seen = row[0] + 1, row[1]
    else:
        attempts, first_seen = 1, now
    parked = attempts > RETRY_MAX_ATTEMPTS
    conn.execute(
        """INSERT INTO cert_sweep_retry
               (gmail_id, reason, attempts, first_seen_at, last_attempt_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(gmail_id) DO UPDATE SET
               reason=excluded.reason,
               attempts=excluded.attempts,
               last_attempt_at=excluded.last_attempt_at""",
        (gmail_id, reason, attempts, first_seen, now),
    )
    conn.commit()
    return {"gmail_id": gmail_id, "reason": reason, "attempts": attempts,
            "parked": parked}


def retry_clear(conn: sqlite3.Connection, gmail_id: str) -> None:
    conn.execute("DELETE FROM cert_sweep_retry WHERE gmail_id = ?",
                 (gmail_id,))
    conn.commit()


def retry_park(conn: sqlite3.Connection, gmail_id: str,
               reason: str) -> dict[str, Any]:
    """Park a retry-ledger row: kept for audit, never re-driven.

    The row is NOT deleted — the ledger stays the complete history. The
    reason is prefixed so the parking cause is visible in reports.
    """
    now = _utcnow_iso()
    row = conn.execute(
        "SELECT reason, attempts, first_seen_at FROM cert_sweep_retry"
        " WHERE gmail_id = ?",
        (gmail_id,),
    ).fetchone()
    if row:
        old_reason, attempts, first_seen = row
    else:
        old_reason, attempts, first_seen = "", 0, now
    parked_reason = f"[PARKED {now}] {reason}"
    if old_reason and not old_reason.startswith("[PARKED"):
        parked_reason += f" | was: {old_reason}"
    conn.execute(
        """INSERT INTO cert_sweep_retry
               (gmail_id, reason, attempts, first_seen_at, last_attempt_at,
                fetch_failures)
           VALUES (?,?,?,?,?,0)
           ON CONFLICT(gmail_id) DO UPDATE SET
               reason=excluded.reason,
               attempts=excluded.attempts,
               last_attempt_at=excluded.last_attempt_at,
               fetch_failures=0""",
        (gmail_id, parked_reason, RETRY_MAX_ATTEMPTS + 1, first_seen, now),
    )
    conn.commit()
    return {"gmail_id": gmail_id, "reason": parked_reason,
            "attempts": RETRY_MAX_ATTEMPTS + 1, "parked": True}


def park_pre_cutoff_backlog(conn: sqlite3.Connection, gmail: Any,
                            cutoff_ms: int = CUTOFF_MS,
                            limit: int = MIGRATION_BATCH_LIMIT
                            ) -> dict[str, Any]:
    """One-time-date-checked parking of the historical backlog.

    Rows are parked only when the MESSAGE's Gmail internalDate is before
    the today-forward cutoff — never by first_seen_at, which is when the
    sweep first saw the row, not when the email arrived. Rows whose
    message cannot be fetched yet are left alone (with a fetch-failure
    count); after FETCH_FAILURES_BEFORE_PARK consecutive failures the
    message is treated as gone from Gmail and parked as dead. Both
    parkings keep the row — nothing is ever deleted.

    Idempotent and bounded: at most ``limit`` rows per call, oldest
    first, so repeated sweeps converge on the full backlog without one
    run ballooning.
    """
    from .cert_intake_runner import message_internal_ms

    get_message = getattr(gmail, "get_full_message", None)
    if get_message is None:
        return {"parked_pre_cutoff": 0, "parked_dead": 0, "checked": 0,
                "note": "gmail port has no get_full_message; skipped"}
    rows = conn.execute(
        "SELECT gmail_id, fetch_failures FROM cert_sweep_retry"
        " WHERE attempts <= ? ORDER BY first_seen_at LIMIT ?",
        (RETRY_MAX_ATTEMPTS, limit),
    ).fetchall()
    parked_pre_cutoff = 0
    parked_dead = 0
    for gmail_id, fetch_failures in rows:
        try:
            payload = get_message(gmail_id)
        except Exception:
            failures = (fetch_failures or 0) + 1
            if failures >= FETCH_FAILURES_BEFORE_PARK:
                retry_park(
                    conn, gmail_id,
                    "message no longer retrievable from Gmail after "
                    f"{failures} consecutive fetch attempts; treating as "
                    "gone — kept for audit, never re-driven")
                parked_dead += 1
            else:
                conn.execute(
                    "UPDATE cert_sweep_retry SET fetch_failures = ?,"
                    " last_attempt_at = ? WHERE gmail_id = ?",
                    (failures, _utcnow_iso(), gmail_id))
                conn.commit()
            continue
        internal_ms = message_internal_ms(payload)
        if internal_ms is None:
            # Cannot date the message: fail closed, leave it for a later
            # run. Never park what cannot be dated.
            continue
        if internal_ms < cutoff_ms:
            retry_park(
                conn, gmail_id,
                f"message predates the 2026-09-27 ET today-forward cutoff "
                f"(internalDate {internal_ms}); historical backlog — kept "
                f"for audit, never re-driven")
            parked_pre_cutoff += 1
    return {"parked_pre_cutoff": parked_pre_cutoff,
            "parked_dead": parked_dead, "checked": len(rows)}


def miss_note(conn: sqlite3.Connection, raw_name: str,
              gmail_id: str,
              policy_numbers: list[str] | None = None) -> dict[str, Any] | None:
    """Record a NO_MATCH insured name for the index-refresh loop.

    No EZLynx applicant-by-name API exists, so a current request that
    misses the full-book index can only be resolved by a human lookup or
    a refreshed complete index. The miss ledger is what the refresh
    script reports coverage against. Returns the entry, or None when
    there is no usable name.

    The request's extracted policy numbers are stored alongside the
    name (normalized, pipe-joined): the miss-watch health check uses
    them to detect the EPHE class of miss — a held request whose policy
    number already exists in the applicant index.
    """
    from .cert_applicant_index import normalize_account_name, \
        normalize_policy_number

    name_key = normalize_account_name(raw_name)
    if not name_key:
        return None
    pol_keys = sorted({normalize_policy_number(p)
                       for p in policy_numbers or [] if p})
    pol_stored = "|".join(pol_keys)
    now = _utcnow_iso()
    row = conn.execute(
        "SELECT occurrences, first_seen_at FROM cert_index_misses"
        " WHERE name_key = ?",
        (name_key,),
    ).fetchone()
    if row:
        occurrences, first_seen = row[0] + 1, row[1]
    else:
        occurrences, first_seen = 1, now
    conn.execute(
        """INSERT INTO cert_index_misses
               (name_key, raw_name, gmail_id, first_seen_at, last_seen_at,
                occurrences, policy_numbers)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(name_key) DO UPDATE SET
               last_seen_at=excluded.last_seen_at,
               occurrences=excluded.occurrences,
               gmail_id=excluded.gmail_id,
               policy_numbers=excluded.policy_numbers""",
        (name_key, (raw_name or "").strip(), gmail_id, first_seen, now,
         occurrences, pol_stored),
    )
    conn.commit()
    return {"name_key": name_key, "raw_name": (raw_name or "").strip(),
            "occurrences": occurrences}


# ---------------------------------------------------------------------------
# Applicant index
# ---------------------------------------------------------------------------

def load_applicant_index(csv_path: str = "") -> Any:
    """Build the applicant index from the daily directory CSV.

    Maps the directory columns onto the row keys build_index needs:
    ``account_name``, ``applicant_id``, ``email_primary``, ``phones``
    (list), ``policy_numbers`` (semicolon-separated string). Fail closed
    with a clear error when the CSV is missing, unreadable, or empty.
    """
    from .cert_applicant_index import build_index

    path = os.path.expanduser(csv_path or _env("CERT_APPLICANT_INDEX_PATH"))
    if not path:
        raise RuntimeError(
            "CERT_APPLICANT_INDEX_PATH is not set — refusing to sweep "
            "without the applicant directory")
    if not os.path.isfile(path):
        raise RuntimeError(
            f"applicant index CSV not found at {path} "
            "(CERT_APPLICANT_INDEX_PATH) — refusing to sweep without it")
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = []
            for raw in reader:
                phones = [str(v).strip() for v in (
                    raw.get("phone_cell"), raw.get("phone_home"),
                    raw.get("phone_work")) if v and str(v).strip()]
                rows.append({
                    "account_name": (raw.get("account_name") or "").strip(),
                    "applicant_id": (raw.get("applicant_id") or "").strip(),
                    "dba": (raw.get("dba") or "").strip(),
                    "email_primary": (raw.get("email_primary") or "").strip(),
                    "phones": phones,
                    "policy_numbers": (raw.get("policy_numbers") or "").strip(),
                })
    except OSError as exc:
        raise RuntimeError(
            f"cannot read applicant index CSV at {path}: {exc}")
    data_rows = [r for r in rows if r["applicant_id"]]
    if not data_rows:
        raise RuntimeError(
            f"applicant index CSV at {path} has no data rows — refusing "
            "to sweep on an empty directory")
    return build_index(data_rows, source_path=path)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def gmail_query(now: datetime | None = None) -> str:
    """Gmail discovery query for the today-forward sweep.

    The query keeps QUERY_SLACK_DAYS of slack before the cutoff because
    Gmail's ``after:`` is date-granular (and account-timezone dependent);
    the authoritative cutoff is the client-side internalDate comparison
    in the intake runner, which enforces the exact
    2026-09-27 00:00 America/New_York boundary.
    """
    anchor = (CUTOFF_ET - timedelta(days=QUERY_SLACK_DAYS)).astimezone(
        timezone.utc)
    return f"after:{anchor.strftime('%Y/%m/%d')}"


def build_gmail() -> Any:
    """Read-only Gmail adapter for certificates@streetsmart.insurance."""
    from .cert_gmail_adapter import CertGmailAdapter
    return CertGmailAdapter.with_dwd()


# ---------------------------------------------------------------------------
# Verification (read-only EZLynx)
# ---------------------------------------------------------------------------

def build_verifier() -> tuple[Any | None, str]:
    """EzlynxReadClient, or (None, reason) when it cannot be built.

    verify_record handles verifier=None (it holds NO_MATCH records for
    human review instead of anchoring through EZLynx).
    """
    try:
        from .cert_verification import EzlynxReadClient
        return EzlynxReadClient(), "ok"
    except Exception as exc:  # e.g. Secret Manager unreachable in a test env
        return None, f"unavailable: {exc}"


# ---------------------------------------------------------------------------
# Filing deps (production wiring)
# ---------------------------------------------------------------------------

def _doc_writer_for(doc_client: Any):
    """Adapt the filing port to upload_document_via_api.

    The filing library calls ``doc_writer(applicant_id, document_name,
    file_bytes, *, filename, content_type)``; the writer takes
    ``file_content_type=`` — the adapter translates, nothing else.
    """
    from .ezlynx_api_only_writes import upload_document_via_api

    def doc_writer(applicant_id: Any, document_name: str,
                   file_bytes: bytes, *, filename: str | None = None,
                   content_type: str = "application/octet-stream"
                   ) -> dict[str, Any]:
        return upload_document_via_api(
            str(applicant_id), document_name, file_bytes,
            client=doc_client, filename=filename,
            file_content_type=content_type or "application/octet-stream",
        )

    return doc_writer


def _doc_searcher_for(doc_client: Any):
    """Read-only Documents search port for the filing read-back logic."""

    def doc_searcher(applicant_id: Any) -> dict[str, Any]:
        return doc_client.search_applicant_documents(str(applicant_id))

    return doc_searcher


def _discussion_client_for(api_config: Any) -> Any:
    """DiscussionApiClient with the host-only base (never derived from the
    document base URL — that 404s; see AGENTS.md 2026-09-20)."""
    from .ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig

    parsed = urlparse(
        str(api_config.document_base_url or api_config.token_endpoint))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    config = DiscussionApiConfig(
        discussion_base_url=origin + "/DiscussionApi/",
        token_endpoint=str(api_config.token_endpoint),
        client_id=str(api_config.client_id),
        client_secret=str(api_config.client_secret),
        username=str(api_config.username),
        integration_group_id=str(api_config.integration_group_id),
        scope="DiscussionApi openid",
    )
    return DiscussionApiClient(config)


def build_filing_deps(db_path: str, verifier: Any = None) -> Any:
    """Production FilingDeps. Raises (fail closed) when any required
    piece is unavailable — including the Zapier trigger script.

    Filing documents without the task path working is a partial state;
    per Carlo's rule the task must be proven, so a missing trigger means
    the sweep files nothing and every candidate ends UNVERIFIED.
    """
    from .cert_filing import FilingDeps, FilingStore
    from .cert_task_registry import TaskRegistry
    from .cert_zapier import ASSIGNEE_SCANALES, CertZapierClient
    from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
    from .ezlynx_api_only_writes import add_note_to_discussion

    trigger = (_env("CERT_ZAPIER_TRIGGER")
               or os.path.expanduser(
                   "~/workspace/skills/zapier/bin/zap-trigger"))
    if not os.path.isfile(trigger):
        raise RuntimeError(
            f"Zapier trigger script not found at {trigger} "
            "(CERT_ZAPIER_TRIGGER) — refusing to file: documents without "
            "Steffany's review task are a partial state")

    api_config = load_ezlynx_api_config()  # ROBIE_ENV/Secret Manager path
    doc_client = EzlynxApiClient(api_config)

    return FilingDeps(
        discussions_client=_discussion_client_for(api_config),
        verifier=verifier,
        note_writer=add_note_to_discussion,  # exact port shape
        doc_writer=_doc_writer_for(doc_client),
        doc_searcher=_doc_searcher_for(doc_client),
        zapier=CertZapierClient(trigger_script=trigger,
                                assignee=ASSIGNEE_SCANALES),
        registry=TaskRegistry(db_path),
        store=FilingStore(db_path),
    )


# ---------------------------------------------------------------------------
# One sweep
# ---------------------------------------------------------------------------

def _reintake_message(gmail: Any, ledger_key: str, index: Any) -> Any:
    """Re-drive one UNVERIFIED ledger row through intake without touching
    the checkpoint (it already marked the message intake_complete exactly
    once). Mirrors run_intake_once's per-message path, including the
    multi-insured fan-out.

    ``ledger_key`` may carry a ``#N`` fan-out suffix (one ledger row per
    named insured); the suffix is stripped for the Gmail fetch and the
    matching fan-out record is selected. When the email's insured set
    changed since the row was written, returns a held record instead of
    guessing which target the row belonged to.

    Returns None when the message predates the today-forward cutoff
    (defensive: the migration should have parked it already).
    """
    from .cert_intake import CertEmail, extract_request_facts
    from .cert_intake_runner import (
        _build_records_for_email, _default_pdf_extractor,
        message_internal_ms,
    )

    base_id = ledger_key.split("#")[0]
    payload = gmail.get_full_message(base_id)
    internal_ms = message_internal_ms(payload)
    if internal_ms is not None and internal_ms < CUTOFF_MS:
        return None

    def fetcher(mid: str, aid: str, _g=gmail) -> bytes:
        return _g.get_attachment_bytes(mid, aid)

    email = CertEmail.from_gmail_api(payload, attachment_fetcher=fetcher)
    facts = extract_request_facts(
        email, pdf_text_extractor=_default_pdf_extractor(gmail))
    records = _build_records_for_email(email, facts, index,
                                       internal_ms=internal_ms)
    for record in records:
        if record.ledger_key == ledger_key:
            return record
    first = records[0]
    first.held = True
    first.hold_reason = (
        f"re-drive target {ledger_key} no longer names a current insured "
        f"({[r.facts.insured_name for r in records]}); "
        "holding for human review — never guessing")
    return first

    def fetcher(mid: str, aid: str, _g=gmail) -> bytes:
        return _g.get_attachment_bytes(mid, aid)

    email = CertEmail.from_gmail_api(payload, attachment_fetcher=fetcher)
    facts = extract_request_facts(
        email, pdf_text_extractor=_default_pdf_extractor(gmail))
    records = _build_records_for_email(email, facts, index,
                                       internal_ms=internal_ms)
    for record in records:
        if record.ledger_key == ledger_key:
            return record
    first = records[0]
    first.held = True
    first.hold_reason = (
        f"re-drive target {ledger_key} no longer names a current insured "
        f"({[r.facts.insured_name for r in records]}); "
        "holding for human review — never guessing")
    return first


def _mark_read_after_filed(gmail_id: str, mark_read_fn: Any,
                           enabled: bool) -> tuple[bool, str]:
    """Mark the source message read after a FILED outcome.

    Returns ``(ok, reason)`` and never raises: a mark-read failure must
    not fail an already destination-proven filing.
    """
    if not enabled:
        return False, "disabled (CERT_GMAIL_MARK_READ=0)"
    if mark_read_fn is None:
        return False, "gmail adapter has no mark_read port"
    try:
        ok, reason = mark_read_fn(gmail_id)
    except Exception as exc:
        return False, f"mark_read raised: {type(exc).__name__}: {exc}"
    return bool(ok), str(reason)


def _sweep_once(*, gmail: Any, checkpoint: Any, index: Any,
                verifier: Any, verifier_note: str, deps: Any,
                retry_conn: sqlite3.Connection,
                query: str, now: datetime,
                intake_fn: Any = None, file_record_fn: Any = None,
                mark_read_fn: Any = None,
                deps_error: str = "",
                ) -> dict[str, Any]:
    """Run one sweep. All ports are injected — tests pass fakes.

    ``intake_fn`` defaults to :func:`run_intake_once`; ``file_record_fn``
    defaults to :func:`file_record`; ``mark_read_fn`` defaults to the
    gmail adapter's ``mark_read`` when it has one. Tests may substitute
    any of them.
    """
    from .cert_intake_runner import run_intake_once
    from .cert_verification import VERIFIED, verify_record
    from .cert_filing import FILED, file_record
    from .cert_applicant_index import NO_MATCH

    if intake_fn is None:
        # The default intake path enforces the today-forward cutoff.
        # Injected test fakes keep their own signature (no cutoff kwarg).
        def intake_fn(gmail, checkpoint, index, query, now):
            return run_intake_once(gmail, checkpoint, index, query=query,
                                   now=now, cutoff_ms=CUTOFF_MS)

    file_record_fn = file_record_fn or file_record
    if mark_read_fn is None:
        mark_read_fn = getattr(gmail, "mark_read", None)
    mark_read_enabled = _env(
        "CERT_GMAIL_MARK_READ", "1").strip().lower() not in (
            "0", "false", "no", "off")

    summary: dict[str, Any] = {
        "sweep_at": now.isoformat(timespec="seconds"),
        "query": query,
        "cutoff_et": CUTOFF_ET.isoformat(),
        "filed": [],
        "unverified": [],
        "errors": [],
        "stats": {"discovered": 0, "matched": 0, "held": 0,
                  "retried": 0, "filed": 0, "unverified": 0,
                  "marked_read": 0, "mark_read_failed": 0,
                  "skipped_pre_cutoff": 0, "parked_historical": 0,
                  "parked_dead": 0, "index_misses": 0,
                  # Edge-case shapes (health-check counters):
                  "tie_broken": 0, "fuzzy_matched": 0,
                  "multi_insured_records": 0, "filename_sourced": 0},
        "verifier": verifier_note,
        "applicant_index": index.stats() if hasattr(index, "stats") else {},
    }
    if deps_error:
        summary["errors"].append(deps_error)

    # Park the historical backlog (date-checked against the cutoff).
    # Idempotent and bounded; converges over runs.
    try:
        migration = park_pre_cutoff_backlog(retry_conn, gmail)
        summary["stats"]["parked_historical"] = migration.get(
            "parked_pre_cutoff", 0)
        summary["stats"]["parked_dead"] = migration.get("parked_dead", 0)
        if migration.get("note"):
            summary["errors"].append(migration["note"])
    except Exception as exc:
        summary["errors"].append(f"backlog migration failed: {exc}")

    def mark_unverified(record: Any, applicant_id: Any,
                        reason: str) -> None:
        # Retry bookkeeping is per insured target (ledger_key), never
        # per message: one target's outcome must not clear another's.
        ledger_key = getattr(record, "ledger_key", None) or record.gmail_id
        ledger = retry_note(retry_conn, ledger_key, reason)
        entry = {"gmail_id": ledger_key, "subject": record.subject,
                 "insured": getattr(getattr(record, "facts", None),
                                    "insured_name", None),
                 "applicant_id": applicant_id, "reason": reason,
                 "retry_attempts": ledger["attempts"]}
        near = list(getattr(getattr(record, "match", None),
                            "near_miss_ids", None) or [])
        if near:
            # Health-check signal: names one edit outside the fuzzy
            # bound that ALMOST matched. Surfaced so the next
            # EPHE-class miss is visible within a day, not months.
            # Never used to match — informational only.
            entry["near_miss_applicant_ids"] = near
        if ledger["parked"]:
            entry["parked_for_human_review"] = True
            summary["errors"].append(
                f"{ledger_key}: parked after {ledger['attempts']} attempts — "
                "human review needed")
        summary["unverified"].append(entry)
        summary["stats"]["unverified"] += 1

    intake = intake_fn(gmail, checkpoint, index, query=query, now=now)
    if intake.get("error"):
        summary["errors"].append(intake["error"])
        return summary
    records = list(intake.get("records", []))
    stats = intake.get("stats", {})
    summary["stats"]["discovered"] = stats.get("discovered", 0)
    summary["stats"]["matched"] = stats.get("matched", 0)
    summary["stats"]["held"] = stats.get("held", 0)
    summary["stats"]["skipped_pre_cutoff"] = stats.get(
        "skipped_pre_cutoff", 0)

    # Re-drive UNVERIFIED records from the retry ledger.
    seen_ids = {getattr(r, "ledger_key", None) or r.gmail_id
                for r in records}
    for pending in retry_pending(retry_conn):
        if pending["gmail_id"] in seen_ids:
            continue
        if pending["attempts"] > RETRY_MAX_ATTEMPTS:
            continue  # parked (human review or pre-cutoff); never re-driven
        try:
            record = _reintake_message(gmail, pending["gmail_id"], index)
        except Exception as exc:
            summary["errors"].append(
                f"retry re-intake failed for {pending['gmail_id']}: {exc}")
            continue
        if record is None:
            # Defensive: the message predates the cutoff (the migration
            # should have parked it). Park it now, never re-drive.
            retry_park(retry_conn, pending["gmail_id"],
                       "message predates the 2026-09-27 ET today-forward "
                       "cutoff; caught at re-drive")
            summary["stats"]["parked_historical"] += 1
            continue
        records.append(record)
        seen_ids.add(pending["gmail_id"])
        summary["stats"]["retried"] += 1

    # Edge-case shape counters (health check): how many records this
    # sweep matched via the new paths.
    def _evidence_has(record: Any, needle: str) -> bool:
        return needle in (
            getattr(getattr(record, "match", None), "evidence", "") or "")

    summary["stats"]["tie_broken"] = sum(
        1 for r in records if _evidence_has(r, "tie-broken"))
    summary["stats"]["fuzzy_matched"] = sum(
        1 for r in records if _evidence_has(r, "fuzzy typo-tolerant"))
    summary["stats"]["multi_insured_records"] = sum(
        1 for r in records
        if "#" in (getattr(r, "ledger_key", "") or ""))
    summary["stats"]["filename_sourced"] = sum(
        1 for r in records
        if getattr(getattr(r, "facts", None),
                   "insured_name_source", "") == "pdf_filename")

    # Per-source-message filing outcomes: the mark-read pass below only
    # marks a message read when EVERY insured target filed.
    filed_by_base: dict[str, list[dict[str, Any]]] = {}

    for record in records:
        applicant_id = getattr(record.match, "applicant_id", None)

        # Intake-held: never verified, never filed.
        if record.held:
            hold_reason = record.hold_reason or "intake hold"
            if record.match.status == NO_MATCH:
                # No EZLynx applicant-by-name API exists, so a current
                # request that misses the index can only be resolved by a
                # human lookup or a refreshed complete index. Record the
                # miss for the refresh loop and say exactly which index
                # was consulted.
                miss = miss_note(retry_conn,
                                 getattr(record.facts, "insured_name", ""),
                                 record.gmail_id,
                                 getattr(record.facts, "policy_numbers",
                                         None))
                index_stats = summary["applicant_index"]
                hold_reason = (
                    f"{hold_reason} [index: "
                    f"{index_stats.get('rows', '?')} rows, built "
                    f"{index_stats.get('built_at', '?')}; "
                    f"miss recorded"
                    f"{' as #' + str(miss['occurrences']) if miss else ''} "
                    f"for the index-refresh loop — resolve via a refreshed "
                    f"full-book export or a governed EZLynx lookup]")
                if miss:
                    summary["stats"]["index_misses"] += 1
            mark_unverified(record, applicant_id, hold_reason)
            continue

        # The filing path is broken (e.g. the Zapier trigger script is
        # absent): documents without Steffany's proven task are a partial
        # state, so nothing is filed and every candidate ends UNVERIFIED.
        if deps is None:
            mark_unverified(
                record, applicant_id,
                f"UNVERIFIED: filing unavailable — {deps_error}; "
                "will retry on a later sweep")
            continue

        # Verify (read-only). Unverified/ambiguous -> never filed.
        verified = verify_record(record, index, verifier=verifier)
        if verified.status != VERIFIED or not verified.applicant_id:
            mark_unverified(
                record, applicant_id,
                "; ".join(verified.hold_reasons) or
                "verification hold — not VERIFIED")
            continue

        # File (the only write path). No retry loop around it: the filing
        # library already implements read-back-first on uncertain POSTs.
        try:
            result = file_record_fn(record, verified, deps,
                                    owner="cert-sweep")
        except Exception as exc:
            mark_unverified(record, verified.applicant_id,
                            f"filing raised unexpectedly: {exc}")
            summary["errors"].append(
                f"{record.ledger_key}: filing raised: {exc}")
            continue

        if result.status == FILED:
            retry_clear(retry_conn, record.ledger_key)
            entry = {
                "gmail_id": record.ledger_key,
                "subject": record.subject,
                "insured": record.facts.insured_name,
                "applicant_id": verified.applicant_id,
                "note_id": result.note_id,
                "documents": [d for d in result.document_ids],
                "discussion_id": result.discussion_id,
                "task_action": result.task_action,
                "task_id": result.task_id,
                "evidence": list(result.evidence),
            }
            summary["filed"].append(entry)
            filed_by_base.setdefault(
                record.base_gmail_id, []).append(entry)
            summary["stats"]["filed"] += 1
        else:
            mark_unverified(
                record, verified.applicant_id,
                "; ".join(result.hold_reasons) or
                f"filing ended {result.status} — not destination-proven")

    # Mark-read pass: a source message is marked read only when ALL of
    # its insured targets are destination-proven. Any sibling target
    # still in the retry ledger (held, errored, or parked for human
    # review) leaves the message unread — a partial filing must never
    # hide the remaining work.
    pending_keys = {p["gmail_id"] for p in retry_pending(retry_conn)}
    for base_id, entries in filed_by_base.items():
        blocked = any(k == base_id or k.startswith(base_id + "#")
                      for k in pending_keys)
        if blocked:
            for e in entries:
                e["marked_read"] = False
                e["mark_read_reason"] = (
                    "sibling insured target still unverified — "
                    "message left unread")
            continue
        marked_read, mark_reason = _mark_read_after_filed(
            base_id, mark_read_fn, mark_read_enabled)
        for e in entries:
            e["marked_read"] = marked_read
            e["mark_read_reason"] = mark_reason
        if marked_read:
            summary["stats"]["marked_read"] += len(entries)
        elif mark_read_enabled and mark_read_fn is not None:
            # A mark-read failure is bookkeeping, not a sweep error:
            # the filing is proven and checkpointed, the message
            # simply stays unread for the next run to see.
            summary["stats"]["mark_read_failed"] += len(entries)

    return summary


def run_sweep(*, gmail: Any | None = None, checkpoint: Any | None = None,
              index: Any | None = None, verifier: Any = None,
              verifier_note: str = "", deps: Any | None = None,
              now: datetime | None = None,
              intake_fn: Any = None, file_record_fn: Any = None,
              mark_read_fn: Any = None,
              ) -> dict[str, Any]:
    """Build the production ports and run one sweep.

    Every port is overridable so tests can inject fakes. Raises
    RuntimeError (fail closed) on config problems that prevent the sweep
    from running at all (missing index CSV, Gmail unavailable). A broken
    filing-deps build (e.g. missing Zapier trigger) does NOT raise: the
    sweep still runs read-only and every candidate ends UNVERIFIED with
    the reason recorded in the summary.
    """
    from .cert_checkpoint import SqliteDedupeStore

    moment = now or datetime.now(timezone.utc)
    sweep_dir = data_dir()
    db_path = os.path.join(sweep_dir, DB_FILENAME)

    if index is None:
        index = load_applicant_index()
    if gmail is None:
        try:
            gmail = build_gmail()
        except Exception as exc:
            raise RuntimeError(f"cannot build Gmail adapter: {exc}")
    if checkpoint is None:
        checkpoint = SqliteDedupeStore(db_path)
    auto_verifier = verifier is None and not verifier_note
    if auto_verifier:
        verifier, verifier_note = build_verifier()
    deps_error = ""
    if deps is None:
        # Fail closed: the sweep files nothing, every candidate ends
        # UNVERIFIED (never "filed without task"). Intake/verification
        # above this point are read-only and safe.
        try:
            deps = build_filing_deps(db_path, verifier=verifier)
        except Exception as exc:
            deps_error = (f"cannot build filing deps, nothing will be "
                          f"filed: {exc}")

    query = gmail_query(now=moment)
    retry_conn = open_retry_db(db_path)
    try:
        summary = _sweep_once(
            gmail=gmail, checkpoint=checkpoint, index=index,
            verifier=verifier, verifier_note=verifier_note, deps=deps,
            retry_conn=retry_conn, query=query, now=moment,
            intake_fn=intake_fn, file_record_fn=file_record_fn,
            mark_read_fn=mark_read_fn,
            deps_error=deps_error)
    finally:
        retry_conn.close()
    summary["data_dir"] = sweep_dir
    summary["elapsed_s"] = round(
        (datetime.now(timezone.utc) - moment).total_seconds(), 2)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cert_sweep",
        description="Certificate inbox sweep: intake -> verify -> file "
                    "(one pass with --once).")
    parser.add_argument("--once", action="store_true",
                        help="run one sweep and exit")
    args = parser.parse_args(argv)

    if not args.once:
        parser.print_usage(sys.stderr)
        print(json.dumps({
            "filed": [], "unverified": [],
            "errors": ["--once is required: "
                       "python -m robie_job_engine.cert_sweep --once"],
        }))
        return EXIT_USAGE

    try:
        summary = run_sweep()
    except RuntimeError as exc:
        # Config fail-closed: nothing was written; the sweep did not run.
        print(json.dumps({
            "filed": [], "unverified": [],
            "errors": [f"config error: {exc}"],
        }))
        return EXIT_USAGE
    except Exception as exc:  # never leak tracebacks with secrets
        print(json.dumps({
            "filed": [], "unverified": [],
            "errors": [f"sweep failed: {type(exc).__name__}: {exc}"],
        }))
        return EXIT_RUNTIME

    print(json.dumps(summary, indent=2, default=str))
    # Persist the summary for the outcome health check
    # (cert_edge_case_health.py). Best-effort: a write failure must never
    # fail the sweep itself.
    try:
        latest_path = os.path.join(data_dir(), "latest-summary.json")
        with open(latest_path, "w") as fh:
            json.dump(summary, fh, indent=2, default=str)
    except Exception:
        pass
    # UNVERIFIED records are a designed fail-closed outcome, not a crash.
    # Exit non-zero only when the sweep itself could not complete.
    return 0 if not summary["errors"] else EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
