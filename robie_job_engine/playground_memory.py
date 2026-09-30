"""Durable Playground memory beside jobs.db.

Preferences are per person or team-wide. Past jobs record what was asked,
for which client, the outcome, and the job id. A secret, password, token,
or payment detail is refused and is not written. Forgetting hides a row.
It does not change the undo log, and it does not change a guardrail.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .idempotency import assert_durable_path
from .secrets import redact_text

MEMORY_DB_NAME = "playground_memory.db"
RECALL_LIMIT = 8
# Bookkeeping for remember/forget/list. Kept in the file, not recited.
_META_OUTCOMES = frozenset({"remembered", "forgotten", "listed", "refused"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playground_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    client_name TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    forgotten_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS playground_memory_job
    ON playground_memory(job_id, kind);
"""

_FORBIDDEN = re.compile(
    r"\b(?:passwords?|passcodes?|passphrases?|api[_ ]?keys?|access tokens?|"
    r"refresh tokens?|bearer tokens?|secrets?|tokens?|ssns?|social security|"
    r"credit cards?|debit cards?|card numbers?|cvvs?|cvcs?|routing numbers?|"
    r"bank accounts?|ibans?|private keys?|checking accounts?|savings accounts?)\b",
    re.IGNORECASE,
)
_CARD = re.compile(r"\b\d{13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_ACCOUNT = re.compile(
    r"\b(?:account|routing)\s*(?:number|#|no\.?)?\s*[:#]?\s*\d{6,}\b",
    re.IGNORECASE,
)
_PIN = re.compile(r"\bpin\b.{0,16}\d{4,}", re.IGNORECASE)
_NAMED_PERSON = re.compile(r"^([A-Z][a-z]+) wants\b")
_ALWAYS_DISCUSSION = re.compile(
    r"\balways use the (.+?) discussion\b",
    re.IGNORECASE,
)
_SECRET_STORED = (
    "A request included a secret, password, token, or payment detail, "
    "so the words were not stored."
)


@dataclass(frozen=True)
class Memory:
    id: int
    scope: str
    owner: str
    kind: str
    subject: str
    body: str
    client_name: str
    job_id: str
    outcome: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Memory":
        return cls(
            id=int(row["id"]),
            scope=str(row["scope"]),
            owner=str(row["owner"]),
            kind=str(row["kind"]),
            subject=str(row["subject"]),
            body=str(row["body"]),
            client_name=str(row["client_name"] or ""),
            job_id=str(row["job_id"] or ""),
            outcome=str(row["outcome"] or ""),
            created_at=str(row["created_at"]),
        )


def memory_db_path(jobs_db: str) -> str:
    """Sibling of jobs.db. The jobs path is already a durable location."""
    source = Path(assert_durable_path(jobs_db))
    return str(assert_durable_path(source.with_name(MEMORY_DB_NAME)))


def memory_contains_secret(text: str) -> bool:
    """True when the words must not be stored or repeated."""
    raw = str(text or "")
    if not raw.strip():
        return False
    if redact_text(raw) != raw:
        return True
    if _FORBIDDEN.search(raw):
        return True
    if _CARD.search(raw):
        return True
    if _SSN.search(raw):
        return True
    if _ACCOUNT.search(raw):
        return True
    if _PIN.search(raw):
        return True
    return False


def safe_text(text: str) -> str:
    if memory_contains_secret(text):
        return _SECRET_STORED
    return " ".join(str(text or "").split())


def preference_scope(fact: str, *, force_team: bool = False) -> tuple[str, str, str]:
    """Return scope, subject, and the stored body.

    ``owner`` is filled by the caller: ``team`` or the person who asked.
    """
    body = " ".join(str(fact or "").split())
    folded = body.casefold()
    if force_team or re.search(r"\b(?:always|everyone|the team|we)\b", folded):
        named = _NAMED_PERSON.match(body)
        subject = named.group(1) if named else "team"
        return "team", subject, body
    named = _NAMED_PERSON.match(body)
    if named and named.group(1).casefold() != "i":
        return "team", named.group(1), body
    if re.match(r"(?:i|i'm|i'd|i’ll)\b", folded):
        return "person", "", body
    return "person", "", body


def team_discussion_title(memories: list[Memory]) -> str:
    """A stored 'always use the X discussion' rule. Not a guessed title."""
    for item in memories:
        if item.kind != "preference" or item.scope != "team":
            continue
        match = _ALWAYS_DISCUSSION.search(item.body)
        if not match:
            continue
        title = " ".join(match.group(1).split()).strip(" .\"'")
        if title and title.casefold() not in {"new", "untitled"}:
            return title
    return ""


def remember_preference(
    jobs_db: str,
    *,
    requested_by: str,
    fact: str,
    now: datetime,
    force_team: bool = False,
) -> Memory | None:
    """Store one preference. Returns None when the fact is a secret."""
    if memory_contains_secret(fact) or memory_contains_secret(requested_by):
        return None
    scope, subject, body = preference_scope(fact, force_team=force_team)
    owner = "team" if scope == "team" else (requested_by or "unknown")
    if scope == "person" and not subject:
        subject = owner
    with _connect(jobs_db) as conn:
        cursor = conn.execute(
            """INSERT INTO playground_memory (
                   scope, owner, kind, subject, body, client_name, job_id,
                   outcome, created_at, forgotten_at
               ) VALUES (?, ?, 'preference', ?, ?, '', '', '', ?, '')""",
            (scope, owner, subject, body, now.isoformat()),
        )
        row = conn.execute(
            "SELECT * FROM playground_memory WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
    return Memory.from_row(row)


def forget_matching(
    jobs_db: str,
    *,
    requested_by: str,
    query: str,
    now: datetime,
) -> list[Memory]:
    """Hide memories that match the query. Does not touch the undo log."""
    if memory_contains_secret(query):
        return []
    needle = " ".join(str(query or "").casefold().split())
    if len(needle) < 3:
        return []
    forgotten: list[Memory] = []
    with _connect(jobs_db) as conn:
        rows = conn.execute(
            """SELECT * FROM playground_memory
               WHERE forgotten_at=''
               ORDER BY id"""
        ).fetchall()
        for row in rows:
            item = Memory.from_row(row)
            if not _can_forget(item, requested_by):
                continue
            if not _query_hits(item, needle):
                continue
            conn.execute(
                "UPDATE playground_memory SET forgotten_at=? WHERE id=?",
                (now.isoformat(), item.id),
            )
            forgotten.append(item)
    return forgotten


def list_memories(
    jobs_db: str,
    *,
    requested_by: str,
    about: str = "",
    limit: int = 20,
) -> list[Memory]:
    needle = " ".join(str(about or "").casefold().split())
    found: list[Memory] = []
    with _connect(jobs_db) as conn:
        rows = conn.execute(
            """SELECT * FROM playground_memory
               WHERE forgotten_at=''
               ORDER BY id DESC"""
        ).fetchall()
    for row in rows:
        item = Memory.from_row(row)
        if _is_meta(item):
            continue
        if not _can_see(item, requested_by, about=needle):
            continue
        if needle and not _query_hits(item, needle):
            continue
        found.append(item)
        if len(found) >= limit:
            break
    return found


def recall_for_turn(
    jobs_db: str,
    *,
    requested_by: str,
    text: str,
    client: str = "",
) -> list[Memory]:
    """Team preferences, this person's memory, and jobs about this client."""
    text_fold = " ".join(str(text or "").casefold().split())
    client_fold = " ".join(str(client or "").casefold().split())
    prefs: list[Memory] = []
    jobs: list[Memory] = []
    with _connect(jobs_db) as conn:
        rows = conn.execute(
            """SELECT * FROM playground_memory
               WHERE forgotten_at=''
               ORDER BY id DESC"""
        ).fetchall()
    for row in rows:
        item = Memory.from_row(row)
        if item.kind == "preference":
            if item.scope == "team" or item.owner.casefold() == requested_by.casefold():
                prefs.append(item)
            continue
        if item.kind != "job" or _is_meta(item):
            continue
        same_person = item.owner.casefold() == requested_by.casefold()
        same_client = bool(client_fold) and client_fold == item.client_name.casefold()
        named = bool(item.client_name) and item.client_name.casefold() in text_fold
        if same_person or same_client or named:
            jobs.append(item)
    return (prefs + jobs)[:RECALL_LIMIT]


def record_job(
    jobs_db: str,
    *,
    job_id: str,
    requested_by: str,
    text: str,
    client_name: str,
    outcome: str,
    now: datetime,
) -> Memory | None:
    """One row per job. A later outcome updates that row. Secrets are not copied."""
    if not job_id:
        return None
    secret = memory_contains_secret(text) or memory_contains_secret(outcome)
    body = _SECRET_STORED if secret else " ".join(str(text or "").split())[:800]
    client = "" if secret else " ".join(str(client_name or "").split())
    owner = requested_by or "unknown"
    if memory_contains_secret(owner):
        owner = "unknown"
    stamp = now.isoformat()
    with _connect(jobs_db) as conn:
        existing = conn.execute(
            """SELECT id FROM playground_memory
               WHERE kind='job' AND job_id=? AND forgotten_at=''
               ORDER BY id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """UPDATE playground_memory
                   SET body=?, client_name=?, outcome=?, created_at=?, owner=?
                   WHERE id=?""",
                (body, client, outcome, stamp, owner, int(existing["id"])),
            )
            row_id = int(existing["id"])
        else:
            cursor = conn.execute(
                """INSERT INTO playground_memory (
                       scope, owner, kind, subject, body, client_name, job_id,
                       outcome, created_at, forgotten_at
                   ) VALUES ('person', ?, 'job', ?, ?, ?, ?, ?, ?, '')""",
                (owner, client or "job", body, client, job_id, outcome, stamp),
            )
            row_id = int(cursor.lastrowid)
        row = conn.execute(
            "SELECT * FROM playground_memory WHERE id=?",
            (row_id,),
        ).fetchone()
    return Memory.from_row(row)


def stored_text_contains(jobs_db: str, needle: str) -> bool:
    """Used by tests. True if any column still holds the forbidden words."""
    if not needle:
        return False
    with _connect(jobs_db) as conn:
        rows = conn.execute("SELECT * FROM playground_memory").fetchall()
    folded = needle.casefold()
    for row in rows:
        blob = " ".join(str(row[key] or "") for key in row.keys()).casefold()
        if folded in blob:
            return True
    return False


def _is_meta(item: Memory) -> bool:
    return item.kind == "job" and item.outcome in _META_OUTCOMES


def _same_owner(item: Memory, requested_by: str) -> bool:
    return item.owner.casefold() == str(requested_by or "").casefold()


def _can_see(item: Memory, requested_by: str, *, about: str) -> bool:
    if item.scope == "team" or _same_owner(item, requested_by):
        return True
    return bool(about) and item.kind == "job" and _query_hits(item, about)


def _can_forget(item: Memory, requested_by: str) -> bool:
    if item.scope == "team" or _same_owner(item, requested_by):
        return True
    return item.kind == "job"


def _query_hits(item: Memory, needle: str) -> bool:
    hay = " ".join(
        part
        for part in (item.body, item.subject, item.client_name, item.job_id, item.outcome)
        if part
    ).casefold()
    return needle in hay or (hay != "" and hay in needle)


@contextmanager
def _connect(jobs_db: str) -> Iterator[sqlite3.Connection]:
    path = memory_db_path(jobs_db)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()
