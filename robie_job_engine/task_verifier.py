"""Delayed task verifier — proves Zapier-fired EZLynx tasks actually landed.

Carlo-approved design (2026-09-27):
  1. When the phone-watchdog or certificate sweep fires a Zapier task
     creation, the expected task (title, assignee, applicant ID, fired_at)
     is recorded as PENDING. Task creation itself is NOT delayed.
  2. A scheduled job runs every 15 minutes. For each PENDING item older
     than VERIFY_AFTER_MINUTES (40 — generous, to avoid report-lag false
     negatives), it pulls the EZLynx task/activity report and matches.
  3. Match -> VERIFIED. No match after 40 min -> MISSING -> alert Carlo via
     Google Chat (never Gmail — alerts must not depend on Gmail).
  4. If the report itself is unavailable/stale -> UNVERIFIED (not MISSING),
     and that is also alerted. Never silently pass.

Two producers, one verifier:
  - phone-watchdog: ingested from its processed_calls table
    (ezlynx_task_status='delivered'). No changes to that repo needed.
  - certificates: CertZapier.create_task() calls record_pending() directly.

The EZLynx task report (task title, assignee, applicant/account, created
timestamp) must exist in Report Center and be emailed as CSV to the report
mailbox — see the module docstring for the setup requirement. Until the
report ID is configured, verification is UNVERIFIED, never a false OK.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("task_verifier")

# Generous window per Carlo: 40 minutes, not 20-30, to avoid report-lag
# false negatives.
VERIFY_AFTER_MINUTES = int(os.environ.get("ROBIE_TASK_VERIFY_AFTER_MINUTES", "40"))

# The EZLynx task/activity report ID. MUST be set once the report exists in
# Report Center (browser recon required: check for a standard task report
# first; build custom only if none exists). Empty = not configured.
TASK_REPORT_ID = os.environ.get("ROBIE_TASK_REPORT_ID", "")

# Subject line of the emailed task-report CSV (same pattern as the 4359
# "ROBIE daily CSV" emails).
TASK_REPORT_SUBJECT = os.environ.get(
    "ROBIE_TASK_REPORT_SUBJECT", "ROBIE task report CSV"
)

# Max age of the report CSV before we call it stale (the report should be
# emailed at least hourly).
REPORT_MAX_AGE_HOURS = int(os.environ.get("ROBIE_TASK_REPORT_MAX_AGE_HOURS", "3"))

DEFAULT_DB_PATH = os.path.expanduser("~/.robie/task_verification/pending.db")

PHONE_WATCHDOG_DB = os.environ.get(
    "ROBIE_PHONE_WATCHDOG_DB",
    "/opt/streetsmart-phone-watchdog/data/phone_alerts.db",
)

# Box-local timezone. Producers (phone-watchdog processed_at, cert sweep)
# write naive wall-clock timestamps in this zone; the verifier must interpret
# them as such. Comparing naive local strings against UTC-aware cutoffs as
# plain strings silently defeats the VERIFY_AFTER_MINUTES grace period
# (2026-10-03: a task fired 9 min earlier was checked as "older than 40 min"
# because "10:06" < "13:35" lexicographically) — hence the normalization
# helpers below.
LOCAL_TZ = ZoneInfo("America/New_York")


def _parse_ts(s: str) -> datetime:
    """Parse a timestamp string; naive values are box-local (LOCAL_TZ)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _utc_iso(s: str) -> str:
    """Normalize any timestamp string to UTC ISO-8601 with offset."""
    return _parse_ts(s).astimezone(timezone.utc).isoformat()


@dataclass
class PendingTask:
    id: int
    producer: str  # "phone-watchdog" | "certificates"
    applicant_id: str
    title: str
    assignee: str
    fired_at: str  # ISO UTC
    status: str  # PENDING | VERIFIED | MISSING | UNVERIFIED
    detail: str = ""


class TaskVerificationStore:
    """SQLite queue of tasks awaiting verification."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    producer TEXT NOT NULL,
                    applicant_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    assignee TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_dedupe
                    ON pending_tasks (producer, applicant_id, title, fired_at);
                CREATE INDEX IF NOT EXISTS idx_pending_status
                    ON pending_tasks (status, fired_at);
                """
            )

    def record_pending(
        self,
        *,
        producer: str,
        applicant_id: str,
        title: str,
        assignee: str,
        fired_at: str | None = None,
    ) -> int:
        """Record an expected task. Idempotent — dupes are ignored.

        fired_at is normalized to UTC ISO at write time so that later
        string comparisons in due_for_verification() are correct.
        """
        now = datetime.now(timezone.utc).isoformat()
        fired = _utc_iso(fired_at) if fired_at else now
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO pending_tasks
                   (producer, applicant_id, title, assignee, fired_at, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'PENDING', ?)""",
                (producer, str(applicant_id), title, assignee, fired, now),
            )
            conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            row = conn.execute(
                """SELECT id FROM pending_tasks
                   WHERE producer=? AND applicant_id=? AND title=? AND fired_at=?""",
                (producer, str(applicant_id), title, fired),
            ).fetchone()
            return int(row["id"])

    def due_for_verification(
        self, now: datetime | None = None
    ) -> list[PendingTask]:
        """PENDING items older than VERIFY_AFTER_MINUTES."""
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=VERIFY_AFTER_MINUTES)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM pending_tasks
                   WHERE status='PENDING' AND fired_at <= ?
                   ORDER BY fired_at""",
                (cutoff,),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def resolve(self, task_id: int, status: str, detail: str = "") -> None:
        assert status in ("VERIFIED", "MISSING", "UNVERIFIED")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE pending_tasks SET status=?, detail=?, resolved_at=?
                   WHERE id=?""",
                (status, detail, now, task_id),
            )
            conn.commit()

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM pending_tasks GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    @staticmethod
    def _row_to_task(r: sqlite3.Row) -> PendingTask:
        return PendingTask(
            id=int(r["id"]),
            producer=r["producer"],
            applicant_id=r["applicant_id"],
            title=r["title"],
            assignee=r["assignee"],
            fired_at=r["fired_at"],
            status=r["status"],
            detail=r["detail"] or "",
        )


def ingest_phone_watchdog(
    store: TaskVerificationStore,
    db_path: str = PHONE_WATCHDOG_DB,
    since_hours: int = 6,
) -> int:
    """Pull newly 'delivered' phone-watchdog tasks into the pending queue.

    The watchdog's processed_calls table records ezlynx_task_status='delivered'
    when the Zapier webhook returns HTTP 200. Those are exactly the tasks whose
    EZLynx-side existence is UNVERIFIED — this ingests them as PENDING.
    Returns the number of rows ingested.
    """
    if not os.path.exists(db_path):
        logger.warning("phone-watchdog DB not found: %s", db_path)
        return 0
    now_utc = datetime.now(timezone.utc)
    # Coarse pre-filter in SQL only: processed_at is naive box-local, so a
    # plain string compare against a UTC cutoff is tz-wrong. Use a wide
    # buffer here and apply the precise tz-aware filter in Python below.
    coarse_cutoff = (now_utc - timedelta(hours=since_hours + 6)).isoformat()
    precise_cutoff = now_utc - timedelta(hours=since_hours)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT message_id, ams_account_id, account_name, assigned_user,
                      processed_at
               FROM processed_calls
               WHERE ezlynx_task_status='delivered'
                 AND processed_at >= ?
               ORDER BY processed_at""",
            (coarse_cutoff,),
        ).fetchall()
    finally:
        conn.close()

    n = 0
    for r in rows:
        try:
            if _parse_ts(str(r["processed_at"] or "")) < precise_cutoff:
                continue
        except ValueError:
            continue
        # Reconstruct the expected task title the same way the watchdog builds
        # it (see ezlynx_phone_task_sync.build_task_payload). The report match
        # uses applicant + assignee + title-substring, so an approximate title
        # is enough; the applicant/assignee carry the identity.
        title = f"[AFTER-HOURS CALLBACK] {r['account_name'] or r['message_id']}"
        store.record_pending(
            producer="phone-watchdog",
            applicant_id=str(r["ams_account_id"] or ""),
            title=title,
            assignee=str(r["assigned_user"] or ""),
            fired_at=str(r["processed_at"] or ""),
        )
        n += 1
    logger.info("ingested %d phone-watchdog delivered tasks", n)
    return n


def parse_task_report_csv(content: bytes) -> list[dict[str, str]]:
    """Parse the EZLynx task-report CSV into row dicts.

    Expected columns (confirmed when the report is built): task title,
    assignee, applicant/account, created timestamp. Matching is tolerant of
    column-name variants.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for row in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
    return rows


def _norm(s: str) -> str:
    """Normalize for fuzzy matching: lowercase, unify dashes, collapse space."""
    s = s.lower()
    for dash in ("—", "–", "−"):
        s = s.replace(dash, "-")
    return " ".join(s.split())


def _assignee_match(report_val: str, expected_val: str) -> bool:
    """Do two assignee spellings refer to the same person?

    Handles EZLynx login vs display name ("SCanales" vs "Steffany Canales")
    as well as plain substring matches.
    """
    a, b = _norm(report_val), _norm(expected_val)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    # Login-style "scanales" (single token, initial+lastname): it matches when
    # it ends with a full word from the display name ("steffany canales").
    for single, multi in ((a, b), (b, a)):
        if " " not in single and " " in multi:
            words = [w for w in multi.split() if len(w) >= 3]
            if any(single.endswith(w) or w in single for w in words):
                return True
    return False


def _row_field(row: dict[str, str], *names: str) -> str:
    # Exact match first: the Activity Detail CSV has columns like
    # "task assigned to" and "note created by" where a naive substring
    # search for "task" or "created" hits the wrong column.
    for name in names:
        if name in row:
            return row[name]
    # Fallback: substring match for column-name variants.
    for name in names:
        for key, val in row.items():
            if name in key:
                return val
    return ""


def match_task(
    pending: PendingTask, report_rows: list[dict[str, str]]
) -> dict[str, str] | None:
    """Find the report row proving this task exists. None = no match.

    Match criteria (all must hold):
      - applicant/account ID matches (exact, string-compared)
      - assignee matches (case-insensitive substring either way)
      - title matches (case-insensitive substring either way — the report
        title and our expected title may differ in prefix/suffix)
      - report created timestamp is within [fired_at, fired_at + 40min + 15min
        grace] — the task cannot predate its firing.
    """
    fired = _parse_ts(pending.fired_at)
    window_end = fired + timedelta(minutes=VERIFY_AFTER_MINUTES + 15)

    for row in report_rows:
        applicant = _row_field(row, "applicant id", "applicant", "account")
        if applicant != pending.applicant_id:
            continue
        assignee = _row_field(row, "task assigned to", "assignee", "assigned")
        if not _assignee_match(assignee, pending.assignee):
            continue
        title = _row_field(row, "title", "task title", "subject", "note")
        if not title or not (
            _norm(title) in _norm(pending.title)
            or _norm(pending.title) in _norm(title)
        ):
            continue
        created_raw = _row_field(row, "created date", "task created date", "created")
        if created_raw:
            try:
                # Naive report timestamps are agency-local (America/New_York),
                # same convention as the producers.
                created = _parse_ts(created_raw.replace("Z", "+00:00"))
                if not (fired <= created <= window_end):
                    continue
            except ValueError:
                pass  # unparseable date: don't exclude on time
        return row
    return None


def verify_due_tasks(
    store: TaskVerificationStore,
    report_rows: list[dict[str, str]] | None,
    report_available: bool,
) -> dict[str, list[PendingTask]]:
    """Verify every due PENDING task against the report.

    Returns {"verified": [...], "missing": [...], "unverified": [...]}.
    When the report is unavailable, due tasks become UNVERIFIED (never a
    silent pass, never a false MISSING).
    """
    result: dict[str, list[PendingTask]] = {
        "verified": [],
        "missing": [],
        "unverified": [],
    }
    due = store.due_for_verification()
    if not due:
        return result

    if not report_available or report_rows is None:
        for task in due:
            store.resolve(task.id, "UNVERIFIED", "task report unavailable")
            result["unverified"].append(task)
        return result

    for task in due:
        hit = match_task(task, report_rows)
        if hit:
            detail = f"matched report row: {hit.get('title', hit.get('task', ''))[:80]}"
            store.resolve(task.id, "VERIFIED", detail)
            result["verified"].append(task)
        else:
            detail = (
                f"no match in task report {VERIFY_AFTER_MINUTES}min after firing "
                f"(applicant={task.applicant_id}, assignee={task.assignee})"
            )
            store.resolve(task.id, "MISSING", detail)
            result["missing"].append(task)
    return result


def format_missing_alert(tasks: list[PendingTask]) -> str:
    lines = [
        "⚠️ EZLynx task verification FAILED — task(s) not found in the task report:",
        "",
    ]
    for t in tasks:
        lines.append(
            f"• [{t.producer}] \"{t.title}\" → {t.assignee} "
            f"(applicant {t.applicant_id}, fired {t.fired_at})"
        )
    lines += [
        "",
        "The Zapier webhook returned 200 but the task is not in EZLynx. "
        "Please research manually — the task may need to be recreated.",
    ]
    return "\n".join(lines)


def format_unverified_alert(tasks: list[PendingTask], reason: str) -> str:
    lines = [
        "⚠️ EZLynx task verification INCONCLUSIVE — report unavailable:",
        "",
        reason,
        "",
    ]
    for t in tasks:
        lines.append(
            f"• [{t.producer}] \"{t.title}\" → {t.assignee} "
            f"(applicant {t.applicant_id}, fired {t.fired_at})"
        )
    lines += ["", "These tasks are NOT confirmed. Re-run verification when the report is available."]
    return "\n".join(lines)
