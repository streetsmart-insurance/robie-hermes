"""Durable Playground memory beside jobs.db.

One SQLite file per server, next to that server's jobs.db, mode 0640,
created by the service user. Test and Production stay separate because
they are different machines and different files.

Each row is a person, team, client, or agency memory. Retrieval is a
lookup: this person's preferences, their team's preferences, agency
preferences, and the last N jobs and client notes for the client named
in the request. A secret is refused and is not written. Forgetting hides
a row. It does not change the undo log, and it does not change a guardrail.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .idempotency import assert_durable_path
from .secrets import redact_text

MEMORY_DB_NAME = "playground_memory.db"
MEMORY_DB_MODE = 0o640
TEAM_NAMES = ("Commercial", "Personal", "Trucking")
TEAM_MEMBERS_ENV = "ROBIE_PLAYGROUND_TEAM_MEMBERS"
JOB_RECALL_ENV = "ROBIE_PLAYGROUND_MEMORY_JOB_LIMIT"
JOB_RECALL_DEFAULT = 5
PREFERENCE_RECALL_CAP = 20
# Bookkeeping for remember/forget/list. Kept in the file, not recited.
_META_OUTCOMES = frozenset({"remembered", "forgotten", "listed", "refused"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playground_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    author TEXT NOT NULL,
    team TEXT NOT NULL DEFAULT '',
    applicant_id TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_job_id TEXT,
    expires_at TEXT,
    kind TEXT NOT NULL DEFAULT 'preference',
    client_name TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    forgotten_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS playground_memory_author
    ON playground_memory(author, scope);
CREATE INDEX IF NOT EXISTS playground_memory_client
    ON playground_memory(applicant_id, client_name);
CREATE INDEX IF NOT EXISTS playground_memory_job
    ON playground_memory(source_job_id, kind);
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
_NAMED_PERSON = re.compile(r"^([A-Za-z][A-Za-z'’.-]*) wants\b")
_ALWAYS_DISCUSSION = re.compile(
    r"\balways use the (.+?) discussion\b",
    re.IGNORECASE,
)
_FIRST_PERSON = re.compile(r"^(?:i|i'm|i'd|i’ll)\b", re.IGNORECASE)
_AGENCY_WORD = re.compile(r"\b(?:always|everyone|agency-wide|the agency)\b", re.IGNORECASE)
_FOR_DAYS = re.compile(r"\bfor (\d{1,4}) days?\b", re.IGNORECASE)
_UNTIL = re.compile(r"\buntil (\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_SECRET_STORED = (
    "A request included a secret, password, token, or payment detail, "
    "so the words were not stored."
)
_TEAM_QUESTION = (
    "Which team should I save that for: Commercial, Personal, or Trucking?"
)


@dataclass(frozen=True)
class Memory:
    id: int
    scope: str
    author: str
    team: str
    applicant_id: str
    text: str
    created_at: str
    source_job_id: str
    expires_at: str
    kind: str = "preference"
    client_name: str = ""
    outcome: str = ""

    @property
    def owner(self) -> str:
        return self.author

    @property
    def body(self) -> str:
        return self.text

    @property
    def job_id(self) -> str:
        return self.source_job_id

    @property
    def subject(self) -> str:
        return self.client_name or self.team or self.author

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Memory":
        return cls(
            id=int(row["id"]),
            scope=str(row["scope"]),
            author=str(row["author"]),
            team=str(row["team"] or ""),
            applicant_id=str(row["applicant_id"] or ""),
            text=str(row["text"]),
            created_at=str(row["created_at"]),
            source_job_id=str(row["source_job_id"] or ""),
            expires_at=str(row["expires_at"] or ""),
            kind=str(row["kind"] or "preference"),
            client_name=str(row["client_name"] or ""),
            outcome=str(row["outcome"] or ""),
        )


@dataclass(frozen=True)
class PlannedMemory:
    scope: str = ""
    team: str = ""
    applicant_id: str = ""
    client_name: str = ""
    text: str = ""
    expires_at: str = ""
    question: str = ""


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


def canonical_team(name: str) -> str:
    folded = " ".join(str(name or "").split()).casefold()
    for team in TEAM_NAMES:
        if team.casefold() == folded:
            return team
    return ""


def team_roster() -> dict[str, str]:
    """Map a Chat or email identity to Commercial, Personal, or Trucking.

    ``ROBIE_PLAYGROUND_TEAM_MEMBERS`` looks like
    ``Commercial=Casey,Maria;Personal=Alex;Trucking=Jordan``.
    The identity must match the whole name or address, ignoring case.
    """
    raw = os.environ.get(TEAM_MEMBERS_ENV, "")
    roster: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" not in part:
            continue
        team_name, people = part.split("=", 1)
        team = canonical_team(team_name)
        if not team:
            continue
        for person in people.split(","):
            identity = person.strip()
            if identity:
                roster[identity.casefold()] = team
    return roster


def team_for(identity: str) -> str:
    return team_roster().get(" ".join(str(identity or "").split()).casefold(), "")


def job_recall_limit() -> int:
    raw = os.environ.get(JOB_RECALL_ENV, "").strip()
    if not raw:
        return JOB_RECALL_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return JOB_RECALL_DEFAULT
    return value if value > 0 else JOB_RECALL_DEFAULT


def plan_preference(
    fact: str,
    *,
    requested_by: str,
    now: datetime,
    force_team: bool = False,
    scope_hint: str = "",
    team_hint: str = "",
    applicant_id: str = "",
    client_name: str = "",
) -> PlannedMemory | None:
    """Choose a scope. None means the fact is a secret and must not be stored.

    A team is never guessed. If the words need a team and nobody in the
    config matches, the result is one question.
    """
    if memory_contains_secret(fact) or memory_contains_secret(requested_by):
        return None
    body = " ".join(str(fact or "").split())
    expires = _expires_at(body, now)
    author_team = team_for(requested_by)
    hinted = canonical_team(team_hint)
    hint = scope_hint.strip().casefold()
    scope = ""
    team = ""
    applicant = " ".join(str(applicant_id or "").split())
    client = " ".join(str(client_name or "").split())
    question = ""
    if hint == "agency" or (not hint and not force_team and _agency_fact(body)):
        scope = "agency"
    elif hint == "client" or applicant or client:
        scope = "client"
        team = ""
    elif hint == "person":
        scope = "person"
        team = author_team
    elif hint == "team" or force_team:
        scope = "team"
        team = hinted or author_team
        if not team:
            question = _TEAM_QUESTION
    elif _FIRST_PERSON.match(body):
        scope = "person"
        team = author_team
    else:
        named = _NAMED_PERSON.match(body)
        if named:
            scope = "team"
            team = team_for(named.group(1)) or author_team
            if not team:
                question = _TEAM_QUESTION
        else:
            scope = "person"
            team = author_team
    return PlannedMemory(
        scope=scope,
        team=team,
        applicant_id=applicant,
        client_name=client,
        text=body,
        expires_at=expires,
        question=question,
    )


def preference_scope(fact: str, *, force_team: bool = False) -> tuple[str, str, str]:
    """Older helper. Prefer ``plan_preference`` for new calls."""
    planned = plan_preference(
        fact,
        requested_by="",
        now=datetime.now(timezone.utc),
        force_team=force_team,
    )
    if planned is None:
        return "person", "", ""
    if planned.question:
        return "team", "", planned.text
    subject = planned.team or planned.client_name
    return planned.scope, subject, planned.text


def team_discussion_title(memories: list[Memory]) -> str:
    """A stored 'always use the X discussion' rule. Not a guessed title."""
    for item in memories:
        if item.kind not in {"preference", "note"}:
            continue
        if item.scope not in {"team", "agency"}:
            continue
        match = _ALWAYS_DISCUSSION.search(item.text)
        if not match:
            continue
        title = " ".join(match.group(1).split()).strip(" .\"'")
        if title and title.casefold() not in {"new", "untitled"}:
            return title
    return ""


def store_preference(
    jobs_db: str,
    planned: PlannedMemory,
    *,
    requested_by: str,
    now: datetime,
    source_job_id: str = "",
) -> Memory:
    """Write one planned preference or client note. Caller already checked secrets."""
    if planned.question or not planned.scope:
        raise ValueError("preference is not ready to store")
    kind = "note" if planned.scope == "client" else "preference"
    author = requested_by or "unknown"
    with _connect(jobs_db) as conn:
        cursor = conn.execute(
            """INSERT INTO playground_memory (
                   scope, author, team, applicant_id, text, created_at,
                   source_job_id, expires_at, kind, client_name, outcome,
                   forgotten_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')""",
            (
                planned.scope,
                author,
                planned.team,
                planned.applicant_id or None,
                planned.text,
                now.isoformat(),
                source_job_id or None,
                planned.expires_at or None,
                kind,
                planned.client_name,
            ),
        )
        row = conn.execute(
            "SELECT * FROM playground_memory WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
    return Memory.from_row(row)


def remember_preference(
    jobs_db: str,
    *,
    requested_by: str,
    fact: str,
    now: datetime,
    force_team: bool = False,
    scope_hint: str = "",
    team_hint: str = "",
    applicant_id: str = "",
    client_name: str = "",
    source_job_id: str = "",
) -> Memory | None:
    """Store one preference. None when the fact is a secret or the team is unknown."""
    planned = plan_preference(
        fact,
        requested_by=requested_by,
        now=now,
        force_team=force_team,
        scope_hint=scope_hint,
        team_hint=team_hint,
        applicant_id=applicant_id,
        client_name=client_name,
    )
    if planned is None or planned.question:
        return None
    return store_preference(
        jobs_db,
        planned,
        requested_by=requested_by,
        now=now,
        source_job_id=source_job_id,
    )


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
    moment = _aware(now)
    forgotten: list[Memory] = []
    with _connect(jobs_db) as conn:
        rows = conn.execute(
            """SELECT * FROM playground_memory
               WHERE forgotten_at=''
               ORDER BY id"""
        ).fetchall()
        for row in rows:
            item = Memory.from_row(row)
            if _expired(item, moment) or _is_meta(item):
                continue
            if not _can_forget(item, requested_by):
                continue
            if not _query_hits(item, needle):
                continue
            conn.execute(
                "UPDATE playground_memory SET forgotten_at=? WHERE id=?",
                (moment.isoformat(), item.id),
            )
            forgotten.append(item)
    return forgotten


def list_memories(
    jobs_db: str,
    *,
    requested_by: str,
    about: str = "",
    limit: int = 20,
    now: datetime | None = None,
) -> list[Memory]:
    needle = " ".join(str(about or "").casefold().split())
    moment = _aware(now)
    found: list[Memory] = []
    with _connect(jobs_db) as conn:
        rows = conn.execute(
            """SELECT * FROM playground_memory
               WHERE forgotten_at=''
               ORDER BY id DESC"""
        ).fetchall()
    for row in rows:
        item = Memory.from_row(row)
        if _expired(item, moment) or _is_meta(item):
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
    applicant_id: str = "",
    now: datetime | None = None,
) -> list[Memory]:
    """Person prefs, this team's prefs, agency prefs, and this client's recent notes.

    Lookup only. Another person's preferences and another team's preferences
    are left out. Client notes and job history are included only when this
    request names that client, and then they are shared across the agency.
    """
    moment = _aware(now)
    text_fold = " ".join(str(text or "").casefold().split())
    client_fold = " ".join(str(client or "").casefold().split())
    applicant = " ".join(str(applicant_id or "").split())
    prefs: list[Memory] = []
    notes: list[Memory] = []
    jobs: list[Memory] = []
    with _connect(jobs_db) as conn:
        rows = conn.execute(
            """SELECT * FROM playground_memory
               WHERE forgotten_at=''
               ORDER BY id DESC"""
        ).fetchall()
    for row in rows:
        item = Memory.from_row(row)
        if _expired(item, moment):
            continue
        if item.kind == "preference" and item.scope in {"person", "team", "agency"}:
            if _scope_visible(item, requested_by):
                prefs.append(item)
            continue
        if item.kind == "note" or (item.scope == "client" and item.kind != "job"):
            if _client_requested(item, text_fold, client_fold, applicant):
                notes.append(item)
            continue
        if item.kind != "job" or _is_meta(item):
            continue
        if _client_requested(item, text_fold, client_fold, applicant):
            jobs.append(item)
    limit = job_recall_limit()
    return prefs[:PREFERENCE_RECALL_CAP] + notes[:limit] + jobs[:limit]


def record_job(
    jobs_db: str,
    *,
    job_id: str,
    requested_by: str,
    text: str,
    client_name: str,
    outcome: str,
    now: datetime,
    applicant_id: str = "",
) -> Memory | None:
    """One client-history row per job. A later outcome updates that row.

    Job history is agency-wide once a later request names the client.
    Secrets are not copied.
    """
    if not job_id:
        return None
    secret = memory_contains_secret(text) or memory_contains_secret(outcome)
    body = _SECRET_STORED if secret else " ".join(str(text or "").split())[:800]
    client = "" if secret else " ".join(str(client_name or "").split())
    applicant = "" if secret else " ".join(str(applicant_id or "").split())
    author = requested_by or "unknown"
    if memory_contains_secret(author):
        author = "unknown"
    stamp = now.isoformat()
    team = team_for(author)
    with _connect(jobs_db) as conn:
        existing = conn.execute(
            """SELECT id FROM playground_memory
               WHERE kind='job' AND source_job_id=? AND forgotten_at=''
               ORDER BY id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """UPDATE playground_memory
                   SET text=?, client_name=?, applicant_id=?, outcome=?,
                       created_at=?, author=?, team=?
                   WHERE id=?""",
                (
                    body,
                    client,
                    applicant or None,
                    outcome,
                    stamp,
                    author,
                    team,
                    int(existing["id"]),
                ),
            )
            row_id = int(existing["id"])
        else:
            cursor = conn.execute(
                """INSERT INTO playground_memory (
                       scope, author, team, applicant_id, text, created_at,
                       source_job_id, expires_at, kind, client_name, outcome,
                       forgotten_at
                   ) VALUES ('client', ?, ?, ?, ?, ?, ?, NULL, 'job', ?, ?, '')""",
                (author, team, applicant or None, body, stamp, job_id, client, outcome),
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


def _agency_fact(body: str) -> bool:
    if _FIRST_PERSON.match(body):
        return False
    return _AGENCY_WORD.search(body) is not None


def _expires_at(text: str, now: datetime) -> str:
    days = _FOR_DAYS.search(text)
    if days:
        return (_aware(now) + timedelta(days=int(days.group(1)))).isoformat()
    until = _UNTIL.search(text)
    if until:
        return datetime.fromisoformat(until.group(1)).replace(tzinfo=timezone.utc).isoformat()
    return ""


def _aware(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _expired(item: Memory, now: datetime) -> bool:
    if not item.expires_at:
        return False
    try:
        when = datetime.fromisoformat(item.expires_at)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= _aware(now)


def _is_meta(item: Memory) -> bool:
    return item.kind == "job" and item.outcome in _META_OUTCOMES


def _same_author(item: Memory, requested_by: str) -> bool:
    return item.author.casefold() == " ".join(str(requested_by or "").split()).casefold()


def _scope_visible(item: Memory, requested_by: str) -> bool:
    if item.scope == "person":
        return _same_author(item, requested_by)
    if item.scope == "team":
        team = team_for(requested_by)
        return bool(team) and team == item.team
    if item.scope == "agency":
        return True
    return False


def _client_requested(item: Memory, text_fold: str, client_fold: str, applicant_id: str) -> bool:
    if applicant_id and item.applicant_id and applicant_id == item.applicant_id:
        return True
    name = item.client_name.casefold()
    if client_fold and name and client_fold == name:
        return True
    if name and name in text_fold:
        return True
    if item.applicant_id and item.applicant_id in text_fold:
        return True
    return False


def _can_see(item: Memory, requested_by: str, *, about: str) -> bool:
    if item.kind == "job" or item.scope == "client" or item.kind == "note":
        return bool(about) and _client_requested(item, about, about, about)
    return _scope_visible(item, requested_by)


def _can_forget(item: Memory, requested_by: str) -> bool:
    if item.scope == "person" and item.kind != "job":
        return _same_author(item, requested_by)
    if item.scope == "team":
        team = team_for(requested_by)
        return bool(team) and team == item.team
    if item.scope in {"agency", "client"}:
        return True
    return False


def _query_hits(item: Memory, needle: str) -> bool:
    hay = " ".join(
        part
        for part in (
            item.text,
            item.client_name,
            item.team,
            item.applicant_id,
            item.source_job_id,
            item.outcome,
            item.author,
        )
        if part
    ).casefold()
    return needle in hay or (hay != "" and hay in needle)


def _restrict_memory_files(path: str) -> None:
    """0640, so the service user can write and the service group can read."""
    base = Path(path)
    for candidate in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
        if candidate.exists():
            os.chmod(candidate, MEMORY_DB_MODE)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    found = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='playground_memory'"
    ).fetchone()
    if found is None:
        conn.executescript(_SCHEMA)
        return
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(playground_memory)")}
    if "author" in cols and "text" in cols:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS playground_memory_author
                ON playground_memory(author, scope);
            CREATE INDEX IF NOT EXISTS playground_memory_client
                ON playground_memory(applicant_id, client_name);
            CREATE INDEX IF NOT EXISTS playground_memory_job
                ON playground_memory(source_job_id, kind);
            """
        )
        return
    if "owner" not in cols or "body" not in cols:
        conn.executescript(_SCHEMA)
        return
    conn.execute("ALTER TABLE playground_memory RENAME TO playground_memory_legacy")
    conn.executescript(_SCHEMA)
    conn.execute(
        """INSERT INTO playground_memory (
               scope, author, team, applicant_id, text, created_at, source_job_id,
               expires_at, kind, client_name, outcome, forgotten_at
           )
           SELECT scope, owner, '', NULL, body, created_at,
                  NULLIF(job_id, ''), NULL, kind, client_name, outcome, forgotten_at
           FROM playground_memory_legacy"""
    )
    conn.execute("DROP TABLE playground_memory_legacy")


@contextmanager
def _connect(jobs_db: str) -> Iterator[sqlite3.Connection]:
    path = memory_db_path(jobs_db)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()
        _restrict_memory_files(path)
