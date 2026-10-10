"""Delayed task verifier — proves Zapier-fired EZLynx tasks actually landed.

Carlo-approved design (2026-09-27):
  1. When the phone-watchdog or certificate sweep fires a Zapier task
     creation, the expected task (title, assignee, applicant ID, fired_at)
     is recorded as PENDING. Task creation itself is NOT delayed.
  2. A scheduled job runs every 15 minutes. For each PENDING item older
     than VERIFY_AFTER_MINUTES (40 — generous, to avoid report-lag false
     negatives), it pulls the EZLynx task/activity report and matches.
  3. Match -> VERIFIED. No match after 40 min -> MISSING -> alert Carlo via
     Google Chat (never Gmail — alerts must not depend on Gmail). Two
     guards against false MISSING (2026-10-10): the report's timestamps are
     Central (REPORT_TZ), and a miss only counts when the report's newest row
     is past the task's firing (the report trails real time by 1-3 h). A task
     the phone-watchdog already confirmed in EZLynx is VERIFIED outright.
  4. If the report itself is unavailable/stale -> UNVERIFIED (not MISSING),
     and that is also alerted. Never silently pass.

Two producers, one verifier:
  - phone-watchdog: ingested from its processed_calls table when
    ezlynx_task_status is 'delivered' (Zapier returned HTTP 200) or
    'sent_to_relay' (handed to the relay). Both are queued PENDING and
    checked against a fresh EZLynx report. 'sent_to_relay' is not proof
    the task landed in EZLynx. No changes to that repo needed.
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
import re
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

# Hand-off statuses to queue for verification. Neither one proves the task
# exists in EZLynx:
#   delivered — Zapier webhook returned HTTP 200
#   sent_to_relay — watchdog handed the task to the relay
PHONE_WATCHDOG_TRACK_STATUSES = ("delivered", "sent_to_relay")

# Box-local timezone. Producers (phone-watchdog processed_at, cert sweep)
# write naive wall-clock timestamps in this zone; the verifier must interpret
# them as such. Comparing naive local strings against UTC-aware cutoffs as
# plain strings silently defeats the VERIFY_AFTER_MINUTES grace period
# (2026-10-03: a task fired 9 min earlier was checked as "older than 40 min"
# because "10:06" < "13:35" lexicographically) — hence the normalization
# helpers below.
LOCAL_TZ = ZoneInfo("America/New_York")

# The EZLynx task report CSV carries naive timestamps in the *report's* zone,
# which is Central, not the agency's Eastern wall clock. Verified 2026-10-10
# against the EZLynx DiscussionApi (which returns UTC): task note 1137261034
# was created 2026-10-10T16:19:08Z, shown in the report as 11:19:08. Across
# 25 Zapier hand-offs 2026-10-05..10 the report's "Created Date" was always
# fired_at minus 1h minus a few seconds. Reading it as Eastern put every row
# an hour before its own firing, so nothing ever matched (no task has ever
# been VERIFIED; every phone-watchdog task was flagged MISSING).
# ROBIE_TASK_REPORT_TZ overrides the zone (IANA name) if the report setting
# ever changes. Offset-bearing timestamps are used as they are.
DEFAULT_REPORT_TZ = "America/Chicago"


def _load_report_tz(name: str | None) -> ZoneInfo:
    wanted = (name or "").strip() or DEFAULT_REPORT_TZ
    try:
        return ZoneInfo(wanted)
    except Exception:
        logger.warning(
            "ROBIE_TASK_REPORT_TZ=%r is not a time zone; using %s", wanted, DEFAULT_REPORT_TZ
        )
        return ZoneInfo(DEFAULT_REPORT_TZ)


REPORT_TZ = _load_report_tz(os.environ.get("ROBIE_TASK_REPORT_TZ"))

# A report row may be stamped a little before our own "fired_at" (the producer
# records fired_at after its call returns; clock skew). 2026-10-07 Metro Trans:
# the task was created 56 s before the watchdog logged it.
FIRED_CLOCK_TOLERANCE = timedelta(minutes=2)


def _parse_ts(s: str) -> datetime:
    """Parse a timestamp string; naive values are box-local (LOCAL_TZ)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _parse_report_ts(s: str, tz: ZoneInfo | None = None) -> datetime:
    """Parse a report timestamp; naive values are in the report zone (REPORT_TZ)."""
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or REPORT_TZ)
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
    """Queue phone-watchdog handoffs whose EZLynx landing is still unproven.

    processed_calls.ezlynx_task_status is 'delivered' when the Zapier webhook
    returns HTTP 200, and 'sent_to_relay' when the watchdog handed the task
    to the relay. Both are ingested as PENDING so a later run can match them
    to a fresh EZLynx task report. 'sent_to_relay' is not itself evidence the
    task exists in EZLynx — verification still has to find it in the report.
    Other statuses are ignored.

    Returns the number of rows newly queued. A second ingest of the same
    applicant, title, and fired_at is a no-op (the pending-task unique key).
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
    status_marks = ", ".join("?" for _ in PHONE_WATCHDOG_TRACK_STATUSES)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""SELECT message_id, ams_account_id, account_name, assigned_user,
                       processed_at
                FROM processed_calls
                WHERE ezlynx_task_status IN ({status_marks})
                  AND processed_at >= ?
                ORDER BY processed_at""",
            (*PHONE_WATCHDOG_TRACK_STATUSES, coarse_cutoff),
        ).fetchall()
    finally:
        conn.close()

    n_new = 0
    n_seen = 0
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
        applicant_id = str(r["ams_account_id"] or "")
        fired_at = str(r["processed_at"] or "")
        already = _phone_task_queued(store, applicant_id, title, fired_at)
        store.record_pending(
            producer="phone-watchdog",
            applicant_id=applicant_id,
            title=title,
            assignee=str(r["assigned_user"] or ""),
            fired_at=fired_at,
        )
        n_seen += 1
        if not already:
            n_new += 1
    logger.info(
        "ingested %d new phone-watchdog tasks "
        "(%d delivered/sent_to_relay rows in window; "
        "sent_to_relay is tracked for report verification, not EZLynx proof)",
        n_new,
        n_seen,
    )
    return n_new


def _phone_task_queued(
    store: TaskVerificationStore, applicant_id: str, title: str, fired_at: str
) -> bool:
    """True when this handoff is already in the pending queue.

    fired_at is compared after the same UTC normalization record_pending uses,
    so a repeat ingest does not count as a new row.
    """
    if not fired_at:
        return False
    with store._connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM pending_tasks
               WHERE producer='phone-watchdog' AND applicant_id=?
                 AND title=? AND fired_at=?""",
            (applicant_id, title, _utc_iso(fired_at)),
        ).fetchone()
    return row is not None


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


# Title prefixes the phone watchdog writes. The watchdog picks the prefix
# (after-hours vs office hours) and the caller label (person vs company) at
# send time; processed_calls keeps neither, so the expected title rebuilt in
# ingest_phone_watchdog can differ from the real one in both. 2026-10-08:
# "[CALLBACK REQUIRED] George Conley" vs expected
# "[AFTER-HOURS CALLBACK] Conley Electric" raised a false MISSING alert.
PHONE_WATCHDOG_TITLE_PREFIXES = (
    "[callback required]",
    "[after-hours callback]",
    "[after hours callback]",
    "[quote request]",
)
_BRACKET_PREFIX = re.compile(r"^\s*\[[^\]]*\]\s*")


def _title_core(title: str) -> str:
    return _norm(_BRACKET_PREFIX.sub("", title or ""))


def _title_match(report_title: str, pending: "PendingTask") -> bool:
    """Same task title, allowing for prefix/suffix and watchdog label drift.

    Phone-watchdog tasks are pinned by applicant + assignee + time window;
    any report title carrying a watchdog prefix counts for them.
    """
    if not report_title:
        return False
    a, b = _norm(report_title), _norm(pending.title)
    if a in b or b in a:
        return True
    ca, cb = _title_core(report_title), _title_core(pending.title)
    if ca and cb and (ca in cb or cb in ca):
        return True
    if pending.producer == "phone-watchdog":
        return a.startswith(PHONE_WATCHDOG_TITLE_PREFIXES)
    return False


def _title_core_match(report_title: str, pending: "PendingTask") -> bool:
    """Strict title check: the task's own title text appears in the report text.

    Unlike _title_match this never accepts a row just for carrying a watchdog
    prefix, so it is safe to use when the assignee is not compared.
    """
    ca, cb = _title_core(report_title), _title_core(pending.title)
    return bool(ca and cb and (ca in cb or cb in ca))


def _created_in_window(row: dict[str, str], fired: datetime, window_end: datetime) -> bool | None:
    """True/False when the row's created time is (not) in the window; None if unknown."""
    created_raw = _row_field(row, "created date", "task created date", "created")
    if not created_raw:
        return None
    try:
        # Naive report timestamps are in the report's zone (REPORT_TZ, Central),
        # not the producers' Eastern wall clock.
        created = _parse_report_ts(created_raw)
    except ValueError:
        return None  # unparseable date: don't exclude on time
    return (fired - FIRED_CLOCK_TOLERANCE) <= created <= window_end


def match_task(
    pending: PendingTask, report_rows: list[dict[str, str]]
) -> dict[str, str] | None:
    """Find the report row proving this task exists. None = no match.

    Pass 1 (all must hold):
      - applicant/account ID matches (exact, string-compared)
      - assignee matches (case-insensitive substring either way)
      - title matches (case-insensitive substring either way — the report
        title and our expected title may differ in prefix/suffix)
      - report created timestamp (read in REPORT_TZ) is within
        [fired_at - 2min, fired_at + 40min + 15min grace]; the task cannot
        predate its firing beyond clock skew.

    Pass 2 (reassigned task): the same applicant, a strict title match and a
    parseable created time in the window, with a different assignee. People
    reassign a callback task right after it is created (KCG Logistics
    2026-10-09, Marucci 2026-10-05), and that must not make a task that
    exists look MISSING. The returned row is a copy carrying
    "_assignee_differs" = "1".
    """
    fired = _parse_ts(pending.fired_at)
    window_end = fired + timedelta(minutes=VERIFY_AFTER_MINUTES + 15)

    reassigned: dict[str, str] | None = None
    for row in report_rows:
        applicant = _row_field(row, "applicant id", "applicant", "account")
        if applicant != pending.applicant_id:
            continue
        title = _row_field(row, "title", "task title", "subject", "note")
        in_window = _created_in_window(row, fired, window_end)
        if in_window is False:
            continue
        assignee = _row_field(row, "task assigned to", "assignee", "assigned")
        if _assignee_match(assignee, pending.assignee):
            if _title_match(title, pending):
                return row
        elif (
            reassigned is None
            and in_window is True
            and _title_core_match(title, pending)
        ):
            reassigned = dict(row, _assignee_differs="1")
    return reassigned


# A report email must arrive this long after a task fired before a miss in
# it counts as MISSING; an older report cannot contain the task yet.
REPORT_LAG_MINUTES = int(os.environ.get("ROBIE_TASK_REPORT_LAG_MINUTES", "10"))
# Give up waiting for a new enough report after this long: UNVERIFIED.
# Measured 2026-10-08..10: the report's newest row trails the email by 1-3 h
# (Lori Radice: created 12:19 ET, first in the 14:00 ET report; Tammy Hughes:
# created 10:03 ET, first in the 12:00 ET report) and by ~6 h overnight while
# nobody creates tasks. 12 h keeps an overnight wait from raising an alert.
REPORT_WAIT_LIMIT_HOURS = int(os.environ.get("ROBIE_TASK_REPORT_WAIT_LIMIT_HOURS", "12"))
# A miss only counts when the report's newest row is at least this far past the
# task's firing: until then the report's data may simply not include it yet.
REPORT_COVERAGE_MARGIN_MINUTES = int(
    os.environ.get("ROBIE_TASK_REPORT_COVERAGE_MARGIN_MINUTES", "5")
)

# Zapier fallbacks the phone-watchdog already confirmed by reading the
# applicant's discussions through the Discussion API (fallback_confirm.py).
# The watchdog keeps those records (state "confirmed") for 24 h in
# zapier_fallback_confirm.json in its state directory. The verifier is the
# same user as the watchdog on the host.
FALLBACK_CONFIRM_FILE = "zapier_fallback_confirm.json"
FALLBACK_CONFIRM_MATCH_SECONDS = 600


def _fallback_confirm_paths() -> list[Path]:
    explicit = os.environ.get("ROBIE_FALLBACK_CONFIRM_STATE", "").strip()
    if explicit:
        return [Path(explicit)]
    dirs = [
        os.environ.get("EZLYNX_TASK_API_STATE_DIR", "").strip(),
        "/var/lib/streetsmart-phone-watchdog",
        os.path.expanduser("~/.streetsmart-phone-watchdog"),
    ]
    return [Path(d) / FALLBACK_CONFIRM_FILE for d in dirs if d]


def load_fallback_confirmations(paths: list[Path] | None = None) -> list[dict[str, Any]]:
    """Confirmed Zapier fallback records written by the phone-watchdog. [] if none."""
    import json

    rows: list[dict[str, Any]] = []
    for path in paths if paths is not None else _fallback_confirm_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in (data.get("pending") if isinstance(data, dict) else None) or []:
            if isinstance(rec, dict) and rec.get("state") == "confirmed":
                rows.append(rec)
    return rows


def watchdog_confirmation(
    task: "PendingTask", confirmations: list[dict[str, Any]]
) -> str | None:
    """Where the watchdog confirmed this task in EZLynx, or None.

    Same applicant, same title text, and the watchdog's hand-off time within
    10 minutes of our fired_at. The assignee is not compared (reassignment).
    """
    if task.producer != "phone-watchdog":
        return None
    fired = _parse_ts(task.fired_at).timestamp()
    for rec in confirmations:
        if str(rec.get("applicant_id") or "") != task.applicant_id:
            continue
        try:
            sent = float(rec.get("sent_at") or 0)
        except (TypeError, ValueError):
            continue
        if abs(sent - fired) > FALLBACK_CONFIRM_MATCH_SECONDS:
            continue
        ca, cb = _title_core(str(rec.get("title") or "")), _title_core(task.title)
        if ca and cb and (ca in cb or cb in ca):
            return str(rec.get("where") or "confirmed in EZLynx")
    return None


def report_coverage_end(report_rows: list[dict[str, str]]) -> datetime | None:
    """Newest created time in the report: the report holds nothing later than this."""
    newest: datetime | None = None
    for row in report_rows:
        raw = _row_field(row, "created date", "created")
        if not raw:
            continue
        try:
            created = _parse_report_ts(raw)
        except ValueError:
            continue
        if newest is None or created > newest:
            newest = created
    return newest


def verify_due_tasks(
    store: TaskVerificationStore,
    report_rows: list[dict[str, str]] | None,
    report_available: bool,
    report_received_at: datetime | None = None,
    now: datetime | None = None,
    confirmations: list[dict[str, Any]] | None = None,
) -> dict[str, list[PendingTask]]:
    """Verify every due PENDING task against the report.

    Returns {"verified": [...], "missing": [...], "unverified": [...]}.
    When the report is unavailable, due tasks become UNVERIFIED (never a
    silent pass, never a false MISSING).

    A task the phone-watchdog already confirmed in EZLynx (fallback_confirm)
    is VERIFIED without the report. A task absent from the report is MISSING
    only when the report demonstrably covers the time it was fired; otherwise
    it stays PENDING for a later report (the report trails real time).
    """
    result: dict[str, list[PendingTask]] = {
        "verified": [],
        "missing": [],
        "unverified": [],
    }
    due = store.due_for_verification()
    if not due:
        return result

    if confirmations is None:
        confirmations = load_fallback_confirmations()
    remaining: list[PendingTask] = []
    for task in due:
        where = watchdog_confirmation(task, confirmations)
        if where:
            store.resolve(task.id, "VERIFIED", f"confirmed in EZLynx by the phone-watchdog ({where})")
            result["verified"].append(task)
        else:
            remaining.append(task)
    due = remaining
    if not due:
        return result

    if not report_available or report_rows is None:
        for task in due:
            store.resolve(task.id, "UNVERIFIED", "task report unavailable")
            result["unverified"].append(task)
        return result

    coverage_end = report_coverage_end(report_rows)
    margin = timedelta(minutes=REPORT_COVERAGE_MARGIN_MINUTES)
    for task in due:
        hit = match_task(task, report_rows)
        fired = _parse_ts(task.fired_at)
        if hit:
            detail = f"matched report row: {(hit.get('title') or hit.get('task id') or '')[:80]}"
            if hit.get("_assignee_differs"):
                detail += (
                    f" (task now assigned to {hit.get('task assigned to', '?')}, "
                    f"not {task.assignee}: reassigned)"
                )
            store.resolve(task.id, "VERIFIED", detail)
            result["verified"].append(task)
            continue
        report_too_old = report_received_at is not None and report_received_at < (
            fired + timedelta(minutes=REPORT_LAG_MINUTES)
        )
        report_data_behind = coverage_end is not None and coverage_end < fired + margin
        if report_too_old or report_data_behind:
            # The newest report was sent before the task, or its data stops
            # before the task: it cannot show it yet. Stay PENDING for the next
            # report (2026-10-08 and 2026-10-10 false alarms).
            waited = (now or datetime.now(timezone.utc)) - fired
            if waited > timedelta(hours=REPORT_WAIT_LIMIT_HOURS):
                store.resolve(
                    task.id,
                    "UNVERIFIED",
                    f"no task report covering the task after {REPORT_WAIT_LIMIT_HOURS}h",
                )
                result["unverified"].append(task)
            continue
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
