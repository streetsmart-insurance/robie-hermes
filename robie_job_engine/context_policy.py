from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import JobStatus, TERMINAL_STATUSES
from .store import JobStore, canonical_json


UTC = timezone.utc
RESET_COMMANDS = {"/new", "/reset", "new job", "start fresh"}
CONTINUATION_PREFIXES = (
    "continue ", "resume ", "pick up where we left off", "continue job ",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _as_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ContextDecision:
    action: str
    active_job_id: str | None
    reason: str


class JobContextManager:
    """Durable DM context isolation without deleting Job history.

    Context lifecycle is deliberately separate from Job completion. Expiring a
    prompt context pauses an unfinished Job; it can never turn the Job COMPLETE.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        inactivity_minutes: int = 120,
        context_char_budget: int = 12_000,
    ) -> None:
        if inactivity_minutes < 5:
            raise ValueError("inactivity timeout must be at least 5 minutes")
        if context_char_budget < 1_000:
            raise ValueError("context budget must be at least 1,000 characters")
        self.db_path = str(db_path)
        self.jobs = JobStore(self.db_path)
        self.inactivity_minutes = inactivity_minutes
        self.context_char_budget = context_char_budget
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_contexts (
                    conversation_id TEXT PRIMARY KEY,
                    active_job_id TEXT,
                    context_state TEXT NOT NULL DEFAULT 'ACTIVE',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    account_id TEXT,
                    policy_id TEXT,
                    submission_id TEXT,
                    last_activity_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_context_expiry
                    ON conversation_contexts(context_state, expires_at);
                """
            )

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_contexts WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        return result

    def decide(self, conversation_id: str, text: str, *, now: datetime | None = None) -> ContextDecision:
        at = now or _now()
        current = self.get(conversation_id)
        normalized = " ".join(text.casefold().split())
        if normalized in RESET_COMMANDS:
            self.archive(conversation_id, reason="explicit reset", now=at)
            return ContextDecision("NEW", None, "explicit reset")
        if current and _as_datetime(current["expires_at"]) <= at:
            old_job = current.get("active_job_id")
            self.archive(conversation_id, reason="two-hour inactivity expiration", now=at)
            return ContextDecision("NEW", None, f"expired context for {old_job or 'no job'}")
        explicit_continue = any(normalized.startswith(prefix) for prefix in CONTINUATION_PREFIXES)
        if explicit_continue and current and current.get("active_job_id"):
            job = self.jobs.get_job(current["active_job_id"])
            if job["status"] == JobStatus.PAUSED:
                self.jobs.resume(job["id"])
            elif job["status"] == JobStatus.UNVERIFIED:
                # UNVERIFIED is closed to automatic workers but remains open
                # for an explicit human-directed retry. Resume at verification
                # when an action checkpoint exists; otherwise perform again.
                target = (
                    JobStatus.VERIFYING
                    if self.jobs.get_checkpoint(job["id"], "action")
                    else JobStatus.PENDING
                )
                self.jobs.transition(
                    job["id"],
                    target,
                    expected={JobStatus.UNVERIFIED},
                    error=None,
                    release_lease=True,
                )
            self.touch(conversation_id, now=at)
            return ContextDecision("RESUME", job["id"], "explicit continuation")
        return ContextDecision("NEW", None, "new DM starts fresh by default")

    def bind_job(
        self,
        conversation_id: str,
        job_id: str,
        *,
        now: datetime | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        at = now or _now()
        expires = at + timedelta(minutes=self.inactivity_minutes)
        self.jobs.get_job(job_id)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conversation_contexts
                (conversation_id,active_job_id,context_state,summary_json,last_activity_at,
                 expires_at,updated_at) VALUES(?,?,'ACTIVE',?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    active_job_id=excluded.active_job_id,
                    context_state='ACTIVE',
                    summary_json=excluded.summary_json,
                    account_id=NULL,policy_id=NULL,submission_id=NULL,
                    last_activity_at=excluded.last_activity_at,
                    expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
                (conversation_id, job_id, canonical_json(summary or {}), at.isoformat(),
                 expires.isoformat(), at.isoformat()),
            )

    def touch(self, conversation_id: str, *, now: datetime | None = None) -> None:
        at = now or _now()
        expires = at + timedelta(minutes=self.inactivity_minutes)
        with self._connect() as conn:
            conn.execute(
                """UPDATE conversation_contexts SET last_activity_at=?,expires_at=?,updated_at=?
                   WHERE conversation_id=?""",
                (at.isoformat(), expires.isoformat(), at.isoformat(), conversation_id),
            )

    def archive(self, conversation_id: str, *, reason: str, now: datetime | None = None) -> None:
        at = now or _now()
        current = self.get(conversation_id)
        if not current:
            return
        job_id = current.get("active_job_id")
        if job_id:
            job = self.jobs.get_job(job_id)
            status = JobStatus(job["status"])
            self.jobs.checkpoint(job_id, "context_archive", {
                "reason": reason,
                "archived_at": at.isoformat(),
                "summary": current.get("summary") or {},
            })
            if status not in TERMINAL_STATUSES and status != JobStatus.PAUSED:
                self.jobs.pause(job_id)
        state = "EXPIRED" if "expiration" in reason else "ARCHIVED"
        with self._connect() as conn:
            conn.execute(
                """UPDATE conversation_contexts SET active_job_id=NULL,context_state=?,
                   account_id=NULL,policy_id=NULL,submission_id=NULL,updated_at=?
                   WHERE conversation_id=?""",
                (state, at.isoformat(), conversation_id),
            )

    def expire_due(self, *, now: datetime | None = None) -> list[str]:
        at = now or _now()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT conversation_id FROM conversation_contexts
                   WHERE context_state='ACTIVE' AND expires_at<=?""",
                (at.isoformat(),),
            ).fetchall()
        expired = [str(row[0]) for row in rows]
        for conversation_id in expired:
            self.archive(conversation_id, reason="two-hour inactivity expiration", now=at)
        return expired

    def set_entity_boundary(
        self,
        conversation_id: str,
        *,
        account_id: str | None,
        policy_id: str | None = None,
        submission_id: str | None = None,
    ) -> None:
        current = self.get(conversation_id)
        if not current:
            raise KeyError(conversation_id)
        changed_account = bool(current.get("account_id") and current.get("account_id") != account_id)
        with self._connect() as conn:
            conn.execute(
                """UPDATE conversation_contexts SET account_id=?,policy_id=?,submission_id=?,updated_at=?
                   WHERE conversation_id=?""",
                (
                    account_id,
                    None if changed_account else policy_id,
                    None if changed_account else submission_id,
                    _now().isoformat(),
                    conversation_id,
                ),
            )

    def compact(self, conversation_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "objective", "status", "important_facts", "decisions", "identifiers",
            "unresolved_issues", "next_action", "known", "remembered", "assumed",
        }
        compacted = {key: summary[key] for key in allowed if key in summary}
        encoded = canonical_json(compacted)
        if len(encoded) > self.context_char_budget:
            raise ValueError("structured job summary exceeds the context budget")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE conversation_contexts SET summary_json=?,updated_at=? WHERE conversation_id=?",
                (encoded, _now().isoformat(), conversation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(conversation_id)
        return compacted

    def build_prompt_context(
        self,
        conversation_id: str,
        *,
        permanent_rules: Iterable[str],
        relevant_skill: str = "",
        relevant_sop: str = "",
        current_tool_result: str = "",
    ) -> str:
        current = self.get(conversation_id)
        summary = current.get("summary") if current and current.get("active_job_id") else {}
        sections = [
            "SYSTEM RULES\n" + "\n".join(permanent_rules),
            "ACTIVE JOB SUMMARY\n" + canonical_json(summary or {}),
        ]
        if relevant_skill:
            sections.append("RELEVANT SKILL\n" + relevant_skill)
        if relevant_sop:
            sections.append("RELEVANT SOP\n" + relevant_sop)
        if current_tool_result:
            sections.append("CURRENT TOOL RESULT\n" + current_tool_result)
        prompt = "\n\n".join(sections)
        if len(prompt) > self.context_char_budget:
            raise ValueError("assembled context exceeds the per-job budget; compact first")
        return prompt
