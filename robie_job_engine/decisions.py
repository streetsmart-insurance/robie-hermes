from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .models import JobStatus
from .store import JobStore, canonical_json, utc_now


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    status: str
    choice: str | None
    handled_by: str | None
    handled_at: str | None
    resumed: bool
    message: str


@dataclass(frozen=True)
class ChatDecisionInteraction:
    action: str
    decision_id: str
    choice: str
    actor: str
    custom_text: str | None
    session_scope: str | None


class DecisionError(RuntimeError):
    pass


class DecisionStore:
    """Durable, auditable human decisions tied to a Job checkpoint."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        JobStore(db_path)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    checkpoint_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    choices_json TEXT NOT NULL,
                    authorized_users_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    selected_choice TEXT,
                    custom_text TEXT,
                    session_scope TEXT,
                    handled_by TEXT,
                    handled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, checkpoint_id)
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_pending
                    ON decisions(status, expires_at);
                CREATE TABLE IF NOT EXISTS session_approvals (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    source_decision_id TEXT NOT NULL REFERENCES decisions(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(scope, approved_by)
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
            if "session_scope" not in columns:
                conn.execute("ALTER TABLE decisions ADD COLUMN session_scope TEXT")

    @staticmethod
    def _clean_users(users: Iterable[str]) -> list[str]:
        return sorted({str(user).strip().lower() for user in users if str(user).strip()})

    @staticmethod
    def _clean_choices(choices: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in choices:
            value = str(item.get("value") or "").strip()
            label = str(item.get("label") or value).strip()
            if not value or not label or value in seen:
                continue
            normalized_label = label.casefold().rstrip(" .:!?/")
            if normalized_label in {
                "other",
                "other / type answer",
                "something else",
                "something else (type it out)",
            }:
                value = "__other__"
                label = "Something else"
                if value in seen:
                    continue
            seen.add(value)
            cleaned = {"value": value, "label": label[:80]}
            description = str(item.get("description") or "").strip()
            if description:
                cleaned["description"] = description[:240]
            result.append(cleaned)
        if not result:
            raise ValueError("at least one decision choice is required")
        if len(result) > 6:
            raise ValueError("a decision card may contain at most six choices")
        return result

    def create(
        self,
        *,
        job_id: str,
        checkpoint_id: str,
        prompt: str,
        choices: Iterable[dict[str, Any]],
        authorized_users: Iterable[str],
        ttl_minutes: int = 60,
        session_scope: str | None = None,
    ) -> dict[str, Any]:
        if ttl_minutes < 1 or ttl_minutes > 24 * 7:
            raise ValueError("decision TTL must be between 1 minute and 7 days")
        clean_choices = self._clean_choices(choices)
        users = self._clean_users(authorized_users)
        if not users:
            raise ValueError("at least one authorized user is required")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(minutes=ttl_minutes)).isoformat()
        decision_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM decisions WHERE job_id=? AND checkpoint_id=?",
                    (job_id, checkpoint_id),
                ).fetchone()
                if existing:
                    conn.commit()
                    return self._decode(existing)
                job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not job:
                    raise KeyError(job_id)
                current = JobStatus(job["status"])
                if current in {JobStatus.COMPLETE, JobStatus.FAILED}:
                    raise DecisionError(f"cannot pause terminal job in {current.value}")
                resume_status = (
                    JobStatus.VERIFYING.value
                    if conn.execute(
                        "SELECT 1 FROM checkpoints WHERE job_id=? AND kind='action'", (job_id,)
                    ).fetchone()
                    else JobStatus.PENDING.value
                )
                conn.execute(
                    """INSERT INTO decisions
                    (id,job_id,checkpoint_id,prompt,choices_json,authorized_users_json,
                     status,expires_at,session_scope,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'PENDING',?,?,?,?)""",
                    (
                        decision_id,
                        job_id,
                        checkpoint_id,
                        prompt.strip(),
                        canonical_json(clean_choices),
                        canonical_json(users),
                        expires,
                        (session_scope or "").strip() or None,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """UPDATE jobs SET status=?,resume_status=?,lease_owner=NULL,
                    lease_expires_at=NULL,updated_at=? WHERE id=?""",
                    (JobStatus.PAUSED.value, resume_status, now, job_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get(decision_id)

    def get(self, decision_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if not row:
            raise KeyError(decision_id)
        return self._decode(row)

    def resolve(
        self,
        decision_id: str,
        *,
        actor: str,
        choice: str,
        custom_text: str | None = None,
        session_scope: str | None = None,
        session_ttl_minutes: int = 120,
    ) -> DecisionResult:
        actor_key = actor.strip().lower()
        choice = choice.strip()
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
                if not row:
                    raise KeyError(decision_id)
                decision = self._decode(row)
                if decision["status"] == "RESOLVED":
                    same = decision["handled_by"] == actor_key and decision["selected_choice"] == choice
                    conn.commit()
                    return DecisionResult(
                        decision_id,
                        "RESOLVED" if same else "CONFLICT",
                        decision["selected_choice"],
                        decision["handled_by"],
                        decision["handled_at"],
                        False,
                        "This choice was already recorded." if same else "This request was already answered.",
                    )
                if decision["status"] != "PENDING" or decision["expires_at"] <= now:
                    conn.execute(
                        "UPDATE decisions SET status='EXPIRED',updated_at=? WHERE id=?",
                        (now, decision_id),
                    )
                    conn.commit()
                    return DecisionResult(decision_id, "EXPIRED", None, None, None, False, "This request has expired.")
                if actor_key not in decision["authorized_users"]:
                    conn.commit()
                    return DecisionResult(decision_id, "UNAUTHORIZED", None, None, None, False, "You are not authorized to answer this request.")
                valid = {item["value"] for item in decision["choices"]}
                if choice not in valid:
                    raise DecisionError("invalid decision choice")
                clean_custom = (custom_text or "").strip() or None
                if choice == "__other__" and not clean_custom:
                    conn.commit()
                    return DecisionResult(decision_id, "NEEDS_TEXT", None, None, None, False, "Type your answer and include the request ID.")
                updated = conn.execute(
                    """UPDATE decisions SET status='RESOLVED',selected_choice=?,custom_text=?,
                    handled_by=?,handled_at=?,updated_at=? WHERE id=? AND status='PENDING'""",
                    (choice, clean_custom, actor_key, now, now, decision_id),
                )
                if updated.rowcount != 1:
                    raise DecisionError("decision changed while it was being resolved")
                conn.execute(
                    """INSERT INTO checkpoints(job_id,kind,data_json,created_at)
                    VALUES (?,?,?,?) ON CONFLICT(job_id,kind) DO UPDATE SET
                    data_json=excluded.data_json,created_at=excluded.created_at""",
                    (
                        decision["job_id"],
                        f"decision:{decision_id}",
                        canonical_json({
                            "decision_id": decision_id,
                            "choice": choice,
                            "custom_text": clean_custom,
                            "handled_by": actor_key,
                            "handled_at": now,
                        }),
                        now,
                    ),
                )
                resumed = choice not in {"deny", "cancel"}
                if resumed:
                    job = conn.execute(
                        "SELECT status,resume_status FROM jobs WHERE id=?", (decision["job_id"],)
                    ).fetchone()
                    if job and job["status"] == JobStatus.PAUSED.value:
                        conn.execute(
                            """UPDATE jobs SET status=?,resume_status=NULL,next_wakeup_at=NULL,
                            lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                            (job["resume_status"] or JobStatus.PENDING.value, now, decision["job_id"]),
                        )
                else:
                    conn.execute(
                        """UPDATE jobs SET status=?,resume_status=NULL,last_error=?,next_wakeup_at=NULL,
                        lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?""",
                        (JobStatus.FAILED.value, f"denied by {actor_key}", now, decision["job_id"]),
                    )
                # The approval boundary is server-owned.  Never trust a scope
                # echoed by a client-side button because a caller can alter
                # action parameters before submitting the event.
                stored_scope = decision.get("session_scope")
                if choice == "approve_session" and stored_scope:
                    expiry = (now_dt + timedelta(minutes=session_ttl_minutes)).isoformat()
                    conn.execute(
                        """INSERT INTO session_approvals
                        (id,scope,approved_by,expires_at,source_decision_id,created_at)
                        VALUES (?,?,?,?,?,?) ON CONFLICT(scope,approved_by) DO UPDATE SET
                        expires_at=excluded.expires_at,source_decision_id=excluded.source_decision_id,
                        created_at=excluded.created_at""",
                        (str(uuid.uuid4()), stored_scope, actor_key, expiry, decision_id, now),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return DecisionResult(decision_id, "RESOLVED", choice, actor_key, now, resumed, "Your choice was recorded.")

    def session_is_approved(self, scope: str, actor: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM session_approvals WHERE scope=? AND approved_by=?",
                (scope, actor.strip().lower()),
            ).fetchone()
        return bool(row and row["expires_at"] > utc_now())

    def expire_due(self) -> list[str]:
        now = utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM decisions WHERE status='PENDING' AND expires_at<=?",
                (now,),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                conn.executemany(
                    "UPDATE decisions SET status='EXPIRED',updated_at=? WHERE id=?",
                    [(now, decision_id) for decision_id in ids],
                )
        return ids

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["choices"] = json.loads(result.pop("choices_json"))
        result["authorized_users"] = json.loads(result.pop("authorized_users_json"))
        return result


def decision_card(decision: dict[str, Any]) -> dict[str, Any]:
    normal_choices = [item for item in decision["choices"] if item["value"] != "__other__"]
    buttons = [
        {
            "text": item["label"],
            "action": "robie_decision",
            "parameters": {
                "decision_id": decision["id"],
                "choice": item["value"],
                "session_scope": decision.get("session_scope") or "",
            },
        }
        for item in normal_choices
    ]
    widgets: list[dict[str, Any]] = [
        {"type": "text", "text": decision["prompt"]},
        {"type": "decorated_text", "top_label": "Job", "text": decision["job_id"]},
    ]
    descriptions = [
        f"<b>{item['label']}</b>: {item['description']}"
        for item in normal_choices
        if item.get("description")
    ]
    if descriptions:
        widgets.append({"type": "text", "text": "<br>".join(descriptions)})
    if buttons:
        widgets.append({"type": "buttons", "buttons": buttons})
    if any(item["value"] == "__other__" for item in decision["choices"]):
        widgets.extend([
            {
                "type": "text_input",
                "name": "custom_text",
                "label": "Something else",
                "hint": "Tell ROBIE what to do instead.",
                "multiline": True,
            },
            {
                "type": "buttons",
                "buttons": [{
                    "text": "Submit",
                    "action": "robie_decision",
                    "parameters": {
                        "decision_id": decision["id"],
                        "choice": "__other__",
                        "session_scope": decision.get("session_scope") or "",
                    },
                }],
            },
        ])
    return {
        "card_id": f"robie-decision-{decision['id']}",
        "header": {"title": "ROBIE needs your decision"},
        "sections": [{"widgets": widgets}],
    }


def _parameter_map(payload: dict[str, Any]) -> dict[str, str]:
    common = payload.get("common") or {}
    direct = common.get("parameters") or payload.get("parameters") or {}
    if isinstance(direct, dict):
        return {str(key): str(value) for key, value in direct.items()}
    action = payload.get("action") or {}
    result: dict[str, str] = {}
    for item in action.get("parameters") or []:
        if isinstance(item, dict) and item.get("key") is not None:
            result[str(item["key"])] = str(item.get("value") or "")
    return result


def _form_text(payload: dict[str, Any], name: str) -> str | None:
    common = payload.get("common") or {}
    inputs = common.get("formInputs") or payload.get("formInputs") or {}
    item = inputs.get(name) if isinstance(inputs, dict) else None
    if not isinstance(item, dict):
        return None
    value = item.get("stringInputs") or item.get("string_inputs") or {}
    values = value.get("value") if isinstance(value, dict) else None
    if isinstance(values, list) and values:
        return str(values[0]).strip() or None
    if isinstance(values, str):
        return values.strip() or None
    return None


def parse_google_chat_interaction(payload: dict[str, Any]) -> ChatDecisionInteraction:
    common = payload.get("common") or {}
    action = str(
        common.get("invokedFunction")
        or (payload.get("action") or {}).get("actionMethodName")
        or payload.get("actionMethodName")
        or ""
    ).strip()
    if action != "robie_decision":
        raise DecisionError("unsupported Google Chat action")
    parameters = _parameter_map(payload)
    decision_id = parameters.get("decision_id", "").strip()
    choice = parameters.get("choice", "").strip()
    user = payload.get("user") or common.get("user") or {}
    actor = str(user.get("email") or user.get("name") or "").strip().lower()
    if not decision_id or not choice or not actor:
        raise DecisionError("decision interaction is missing identity or parameters")
    return ChatDecisionInteraction(
        action=action,
        decision_id=decision_id,
        choice=choice,
        actor=actor,
        custom_text=_form_text(payload, "custom_text"),
        session_scope=parameters.get("session_scope") or None,
    )


def resolve_google_chat_interaction(
    db_path: str, payload: dict[str, Any]
) -> DecisionResult:
    interaction = parse_google_chat_interaction(payload)
    return DecisionStore(db_path).resolve(
        interaction.decision_id,
        actor=interaction.actor,
        choice=interaction.choice,
        custom_text=interaction.custom_text,
        session_scope=interaction.session_scope,
    )


def google_chat_decision_response(result: DecisionResult) -> dict[str, Any]:
    """Return a fail-closed Chat interaction response for an answered card."""
    if result.status == "RESOLVED":
        if result.choice in {"deny", "cancel"}:
            text = "ROBIE recorded the denial. The Job is stopped and marked Failed."
        else:
            text = "ROBIE recorded your decision. The exact paused Job may now continue."
    elif result.status == "NEEDS_TEXT":
        text = "Type your alternative in Something else, then select Submit."
    elif result.status == "UNAUTHORIZED":
        text = "This approval request is restricted to its authorized reviewers."
    elif result.status == "EXPIRED":
        text = "This approval request expired. ROBIE did not continue the Job."
    elif result.status == "CONFLICT":
        text = "This approval request was already answered. ROBIE did not change it."
    else:
        text = "ROBIE could not safely apply this decision. The Job remains paused."
    return {
        "actionResponse": {"type": "UPDATE_MESSAGE"},
        "text": text,
    }


def handle_google_chat_decision(
    db_path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a signed Chat interaction and produce the card update payload."""
    try:
        result = resolve_google_chat_interaction(db_path, payload)
    except (DecisionError, KeyError, ValueError):
        result = DecisionResult(
            decision_id="",
            status="REJECTED",
            choice=None,
            handled_by=None,
            handled_at=None,
            resumed=False,
            message="The interaction could not be validated.",
        )
    return google_chat_decision_response(result)
