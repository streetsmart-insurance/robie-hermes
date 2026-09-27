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
    records.
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Gmail query window: the SQLite checkpoint is the real ledger; the window
#: just needs to cover mail that has not been checkpointed yet.
QUERY_WINDOW_DAYS = 7

#: Retry ledger bounds: at a 5-minute cadence, 288 attempts ~= 24 hours of
#: retries before the record is parked for human review instead of being
#: re-driven forever.
RETRY_MAX_ATTEMPTS = 288

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
    last_attempt_at TEXT NOT NULL
);
"""


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


# ---------------------------------------------------------------------------
# Applicant index
# ---------------------------------------------------------------------------

def load_applicant_index(csv_path: str = "") -> Any:
    """Build the applicant index from the daily directory CSV.

    Maps the directory columns onto the row keys build_index needs:
    ``account_name``, ``applicant_id``, ``email_primary``, ``phones``
    (list). Fail closed with a clear error when the CSV is missing,
    unreadable, or empty.
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
                    "email_primary": (raw.get("email_primary") or "").strip(),
                    "phones": phones,
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

def gmail_query(days: int = QUERY_WINDOW_DAYS,
                now: datetime | None = None) -> str:
    """Relative Gmail search window. The checkpoint is the ledger; the
    window only needs to cover un-checkpointed mail.

    Zap callback emails (``[cert-task-callback]``) are excluded: they are
    task proofs ingested by ``ingest_callback_emails``, not certificate
    requests.
    """
    from .cert_callback import CALLBACK_SUBJECT_PREFIX
    anchor = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return (f"after:{anchor.strftime('%Y/%m/%d')} "
            f'-subject:"{CALLBACK_SUBJECT_PREFIX}"')


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
    from .cert_callback import (
        CallbackStore, callback_task_prover, require_callback_auth,
    )
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

    # Nonce-guarded Zap callback proof prerequisites: the sweep must fail
    # BEFORE every write (document, note, Zap) unless callback sender
    # allowlist + shared secret are configured. Without them, callbacks
    # cannot be authenticated and every filing would be UNVERIFIED.
    # This runs before any EZLynx client is built — a missing config
    # means deps is never constructed and the sweep files nothing.
    require_callback_auth()

    api_config = load_ezlynx_api_config()  # ROBIE_ENV/Secret Manager path
    doc_client = EzlynxApiClient(api_config)

    # Nonce-guarded Zap callback proof (cert_callback): no Task API exists,
    # so the Zap's validated callback is the only task proof.
    callback_store = CallbackStore(db_path)

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
        callback_store=callback_store,
        task_prover=callback_task_prover(callback_store),
    )


# ---------------------------------------------------------------------------
# One sweep
# ---------------------------------------------------------------------------

def _reintake_message(gmail: Any, gmail_id: str, index: Any) -> Any:
    """Re-drive one UNVERIFIED message through intake without touching the
    checkpoint (it already marked the message intake_complete exactly
    once). Mirrors run_intake_once's per-message path."""
    from .cert_applicant_index import MATCHED, match_applicant
    from .cert_intake import CertEmail, extract_request_facts
    from .cert_intake_runner import IntakeRecord, _default_pdf_extractor

    payload = gmail.get_full_message(gmail_id)

    def fetcher(mid: str, aid: str, _g=gmail) -> bytes:
        return _g.get_attachment_bytes(mid, aid)

    email = CertEmail.from_gmail_api(payload, attachment_fetcher=fetcher)
    facts = extract_request_facts(
        email, pdf_text_extractor=_default_pdf_extractor(gmail))
    match = match_applicant(facts, index)
    held = match.status != MATCHED or facts.pdf_unreadable
    hold_reason = ""
    if facts.pdf_unreadable:
        hold_reason = ("PDF attachment unreadable (likely scanned); "
                       "holding for OCR/human review — never guessing")
    elif held:
        hold_reason = match.hold_reason()
    return IntakeRecord(
        gmail_id=email.gmail_id,
        thread_id=email.thread_id,
        subject=email.subject,
        from_header=email.from_header,
        date=email.date,
        facts=facts,
        match=match,
        held=held,
        hold_reason=hold_reason,
        attachment_count=len(email.attachments),
    )


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

    intake_fn = intake_fn or run_intake_once
    file_record_fn = file_record_fn or file_record
    if mark_read_fn is None:
        mark_read_fn = getattr(gmail, "mark_read", None)
    mark_read_enabled = _env(
        "CERT_GMAIL_MARK_READ", "1").strip().lower() not in (
            "0", "false", "no", "off")

    summary: dict[str, Any] = {
        "sweep_at": now.isoformat(timespec="seconds"),
        "query": query,
        "filed": [],
        "unverified": [],
        "errors": [],
        "stats": {"discovered": 0, "matched": 0, "held": 0,
                  "retried": 0, "filed": 0, "unverified": 0,
                  "marked_read": 0, "mark_read_failed": 0},
        "verifier": verifier_note,
        "applicant_index": index.stats() if hasattr(index, "stats") else {},
    }
    if deps_error:
        summary["errors"].append(deps_error)

    # Ingest Zap callback emails BEFORE intake: a validated callback is the
    # only task proof (no Task API exists). Best-effort — a missed callback
    # just leaves the filing UNVERIFIED for this sweep.
    cb_store = getattr(deps, "callback_store", None)
    if cb_store is not None and gmail is not None:
        from .cert_callback import ingest_callback_emails
        try:
            cb_stats = ingest_callback_emails(gmail=gmail, store=cb_store)
            summary["stats"]["callbacks_found"] = cb_stats["found"]
            summary["stats"]["callbacks_accepted"] = cb_stats["accepted"]
            summary["stats"]["callbacks_rejected"] = cb_stats["rejected"]
        except Exception as exc:  # noqa: BLE001 — ingestion is best-effort
            summary["errors"].append(f"callback ingestion failed: {exc}")

    def mark_unverified(gmail_id: str, subject: str,
                        applicant_id: Any, reason: str) -> None:
        ledger = retry_note(retry_conn, gmail_id, reason)
        entry = {"gmail_id": gmail_id, "subject": subject,
                 "applicant_id": applicant_id, "reason": reason,
                 "retry_attempts": ledger["attempts"]}
        if ledger["parked"]:
            entry["parked_for_human_review"] = True
            summary["errors"].append(
                f"{gmail_id}: parked after {ledger['attempts']} attempts — "
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

    # Re-drive UNVERIFIED records from the retry ledger.
    seen_ids = {r.gmail_id for r in records}
    for pending in retry_pending(retry_conn):
        if pending["gmail_id"] in seen_ids:
            continue
        if pending["attempts"] >= RETRY_MAX_ATTEMPTS:
            continue  # parked for human review; listed, not re-driven
        try:
            records.append(_reintake_message(gmail, pending["gmail_id"],
                                            index))
            seen_ids.add(pending["gmail_id"])
            summary["stats"]["retried"] += 1
        except Exception as exc:
            summary["errors"].append(
                f"retry re-intake failed for {pending['gmail_id']}: {exc}")

    for record in records:
        gmail_id = record.gmail_id
        applicant_id = getattr(record.match, "applicant_id", None)

        # Intake-held: never verified, never filed.
        if record.held:
            mark_unverified(gmail_id, record.subject, applicant_id,
                            record.hold_reason or "intake hold")
            continue

        # The filing path is broken (e.g. the Zapier trigger script is
        # absent): documents without Steffany's proven task are a partial
        # state, so nothing is filed and every candidate ends UNVERIFIED.
        if deps is None:
            mark_unverified(
                gmail_id, record.subject, applicant_id,
                f"UNVERIFIED: filing unavailable — {deps_error}; "
                "will retry on a later sweep")
            continue

        # Verify (read-only). Unverified/ambiguous -> never filed.
        verified = verify_record(record, index, verifier=verifier)
        if verified.status != VERIFIED or not verified.applicant_id:
            mark_unverified(
                gmail_id, record.subject, applicant_id,
                "; ".join(verified.hold_reasons) or
                "verification hold — not VERIFIED")
            continue

        # File (the only write path). No retry loop around it: the filing
        # library already implements read-back-first on uncertain POSTs.
        try:
            result = file_record_fn(record, verified, deps,
                                    owner="cert-sweep")
        except Exception as exc:
            mark_unverified(gmail_id, record.subject,
                            verified.applicant_id,
                            f"filing raised unexpectedly: {exc}")
            summary["errors"].append(f"{gmail_id}: filing raised: {exc}")
            continue

        if result.status == FILED:
            retry_clear(retry_conn, gmail_id)
            marked_read, mark_reason = _mark_read_after_filed(
                gmail_id, mark_read_fn, mark_read_enabled)
            summary["filed"].append({
                "gmail_id": gmail_id,
                "subject": record.subject,
                "applicant_id": verified.applicant_id,
                "note_id": result.note_id,
                "documents": [d for d in result.document_ids],
                "discussion_id": result.discussion_id,
                "task_action": result.task_action,
                "task_id": result.task_id,
                "evidence": list(result.evidence),
                "marked_read": marked_read,
                "mark_read_reason": mark_reason,
            })
            summary["stats"]["filed"] += 1
            if marked_read:
                summary["stats"]["marked_read"] += 1
            elif mark_read_enabled and mark_read_fn is not None:
                # A mark-read failure is bookkeeping, not a sweep error:
                # the filing is proven and checkpointed, the message
                # simply stays unread for the next run to see.
                summary["stats"]["mark_read_failed"] += 1
        else:
            mark_unverified(
                gmail_id, record.subject, verified.applicant_id,
                "; ".join(result.hold_reasons) or
                f"filing ended {result.status} — not destination-proven")

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
    # The applicant index IS the sweep's write allowlist: any client in the
    # directory is a legitimate filing destination. Register it so the
    # shared EZLynx write-scope gate (used by document upload and note
    # append) allows indexed applicants. Process-scoped — other jobs that
    # never call this keep the restrictive compiled allowlist.
    from .ezlynx_write_scope import register_cert_sweep_applicant_index
    register_cert_sweep_applicant_index(index.all_applicant_ids())
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
    # UNVERIFIED records are a designed fail-closed outcome, not a crash.
    # Exit non-zero only when the sweep itself could not complete.
    return 0 if not summary["errors"] else EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
