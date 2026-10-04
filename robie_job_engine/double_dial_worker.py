"""Durable double-dial worker: polling-first call outcome -> single redial.

Polling-first per the draft working constraints: no webhook ingress for the
first Test proof. Bounded call-detail polling with durable state, capped
backoff, one idempotency key per attempt, and a call-detail status re-read
immediately before dispatching the redial. A webhook path can later feed the
same ``record_outcome``/``maybe_redial`` interface without policy changes.

Durability and idempotency:
- SQLite store; /tmp is never a persistent store (assert_durable_path).
- attempts PRIMARY KEY (policy_version, target, attempt_seq): the redial is
  an INSERT-once row created BEFORE dispatch, so a crash or a duplicate
  trigger can never place a second redial. call_id is UNIQUE.
- events: append-only poll/transcript evidence, deduped by content
  fingerprint so re-reading the same call detail writes nothing twice.
- The dispatch port and clock are injected; the real Bland wiring is a
  separate, secret-backed adapter (config.bland_secret_ref names the
  Test-scoped secret - which does not exist yet - and nothing here reads
  any secret).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .bland_double_dial import (
    AGENT_IDENTITY,
    CallOutcome,
    DoubleDialConfig,
    OutcomeClassification,
    build_attempt2_voicemail_script,
    classify_outcome,
    decide_redial,
    utc_from_epoch,
    validate_target,
)
from .idempotency import assert_durable_path
from .secrets import redact_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    policy_version TEXT NOT NULL,
    target TEXT NOT NULL,
    attempt_seq INTEGER NOT NULL,
    call_id TEXT UNIQUE,
    caller_id TEXT NOT NULL,
    dispatch_started_at TEXT NOT NULL,
    outcome TEXT,
    conclusive INTEGER,
    reason TEXT,
    ended_at TEXT,
    duration_minutes REAL,
    price REAL,
    transcript TEXT,
    error TEXT,
    PRIMARY KEY (policy_version, target, attempt_seq)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (call_id, fingerprint)
);
"""


class CallDetailPort(Protocol):
    """Outbound dispatch + call-detail reads. Bland-backed in production."""

    def dispatch_call(
        self, *, target: str, task: str, caller_id: str, metadata: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Place one call. Returns a receipt mapping including ``call_id``."""
        ...

    def get_call_detail(self, call_id: str) -> Mapping[str, Any]:
        """Read the current call detail for classification."""
        ...


@dataclass(frozen=True)
class AttemptRecord:
    attempt_seq: int
    call_id: Optional[str]
    outcome: Optional[CallOutcome]
    conclusive: bool
    reason: str


def _fingerprint(detail: Mapping[str, Any]) -> str:
    """Stable content fingerprint for deduping repeated call-detail reads."""
    relevant = {
        k: detail.get(k)
        for k in (
            "queue_status", "status", "completed", "answered_by",
            "call_length", "price", "error_message", "concatenated_transcript",
        )
    }
    blob = json.dumps(relevant, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class DoubleDialWorker:
    def __init__(
        self,
        *,
        db_path: str,
        port: CallDetailPort,
        config: DoubleDialConfig = DoubleDialConfig(),
        clock: Callable[[], float],
        bland_secret_ref: Optional[str] = None,
    ) -> None:
        self.db_path = str(assert_durable_path(db_path))
        self.port = port
        self.config = config
        self.clock = clock
        # Names the Test-scoped secret the eventual Bland adapter will read.
        # Config label only: this worker never reads secrets, and the
        # Production secret must never be referenced here.
        self.bland_secret_ref = bland_secret_ref
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _now(self) -> datetime:
        return utc_from_epoch(self.clock())

    # -- attempt lifecycle -------------------------------------------------

    def start_campaign(
        self,
        *,
        target: str,
        task: str,
        number_source: str,
        client_phones: Sequence[str] = (),
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> AttemptRecord:
        """Dispatch attempt 1 for a target, or return the existing one.

        Idempotent on (policy_version, target, attempt_seq=1): a repeated
        trigger never places a duplicate first call.
        """
        number = validate_target(
            target_number=target,
            number_source=number_source,
            client_phones=client_phones,
        )
        meta = dict(metadata or {})
        meta.update(
            {"policy_version": self.config.policy_version, "attempt_seq": 1,
             "agent_identity": AGENT_IDENTITY,
             # Attempt 1 reaching voicemail (or screening with no pickup):
             # hang up, NO message.
             "voicemail_action": "no_message"}
        )
        # Atomic claim: the INSERT itself decides who dispatches. The row is
        # committed BEFORE dispatch, so two racing workers can never both
        # place the first call - the loser sees the committed row and stands
        # down. A row left with call_id NULL means the owner crashed between
        # claim and dispatch: never redispatch blindly; surface it instead.
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO attempts (policy_version, target, attempt_seq,"
                    " caller_id, dispatch_started_at) VALUES (?,?,?,?,?)",
                    (
                        self.config.policy_version, number, 1,
                        self.config.caller_id, self._now().isoformat(),
                    ),
                )
                claimed = True
            except sqlite3.IntegrityError:
                claimed = False
        if not claimed:
            existing = self._get_attempt(number, 1)
            if existing is not None and existing.call_id is None:
                return AttemptRecord(
                    1, None, existing.outcome, existing.conclusive,
                    "attempt 1 claimed but has no call_id - owner may have "
                    "crashed before dispatch; investigate, do not redispatch",
                )
            return existing
        receipt = self.port.dispatch_call(
            target=number, task=task,
            caller_id=self.config.caller_id, metadata=meta,
        )
        call_id = str(receipt.get("call_id") or "") or None
        with self._connect() as conn:
            conn.execute(
                "UPDATE attempts SET call_id=? WHERE policy_version=? AND"
                " target=? AND attempt_seq=1 AND call_id IS NULL",
                (call_id, self.config.policy_version, number),
            )
        return AttemptRecord(1, call_id, None, False, "attempt 1 dispatched")

    def record_outcome(self, call_id: str, detail: Mapping[str, Any]) -> OutcomeClassification:
        """Record one call-detail read and classify it.

        Idempotent: identical re-reads (same fingerprint) write no new
        event row. A conclusive classification finalizes the attempt row.
        """
        classification = classify_outcome(detail, config=self.config)
        fp = _fingerprint(detail)
        payload = redact_text(json.dumps(dict(detail), default=str))
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO events (call_id, fingerprint,"
                " recorded_at, payload_json) VALUES (?,?,?,?)",
                (call_id, fp, self._now().isoformat(), payload),
            )
            if classification.conclusive:
                conn.execute(
                    "UPDATE attempts SET outcome=?, conclusive=1, reason=?,"
                    " ended_at=?, duration_minutes=?, price=?, transcript=?,"
                    " error=? WHERE call_id=? AND conclusive IS NULL",
                    (
                        classification.outcome.value, classification.reason,
                        self._now().isoformat(),
                        _as_float(detail.get("call_length")),
                        _as_float(detail.get("price")),
                        redact_text(_transcript_of(detail)) or None,
                        str(detail.get("error_message") or "")[:300] or None,
                        call_id,
                    ),
                )
        return classification

    def poll_until_conclusive(self, call_id: str) -> OutcomeClassification:
        """Bounded poll with capped backoff; timeout -> UNKNOWN (conclusive).

        Never treats continued PENDING as a conversation, and never dials on
        inconclusive evidence.
        """
        started = self.clock()
        last = OutcomeClassification(CallOutcome.PENDING, False, "not polled")
        for interval in self.config.poll_intervals_seconds:
            if self.clock() - started > self.config.max_poll_seconds:
                break
            last = self.record_outcome(call_id, self.port.get_call_detail(call_id))
            if last.conclusive:
                return last
            self._sleep(interval)
        timeout = OutcomeClassification(
            CallOutcome.UNKNOWN, True,
            f"poll timeout after {self.config.max_poll_seconds}s - inconclusive",
        )
        self.record_outcome(call_id, {
            "call_id": call_id, "queue_status": "timeout",
            "status": "unknown", "completed": False,
            "error_message": "local poll timeout",
        })
        with self._connect() as conn:
            conn.execute(
                "UPDATE attempts SET outcome=?, conclusive=1, reason=?,"
                " ended_at=? WHERE call_id=? AND conclusive IS NULL",
                (CallOutcome.UNKNOWN.value, timeout.reason,
                 self._now().isoformat(), call_id),
            )
        return timeout

    def _sleep(self, seconds: float) -> None:
        # Real sleep in production wiring; tests use zero-length poll
        # intervals with an injected clock, so this is a no-op there.
        import time

        time.sleep(seconds)

    # -- redial ------------------------------------------------------------

    def maybe_redial(self, *, target: str, task: str,
                     number_source: str = "carrier_directory",
                     client_phones: Sequence[str] = (),
                     call_reason: Optional[str] = None) -> AttemptRecord:
        """Fire the single redial if policy and window allow it.

        Re-reads the attempt-1 call detail immediately before dispatching;
        if the outcome is no longer conclusive voicemail-no-message, or the
        window has closed, nothing is dialed and the reason is recorded.
        """
        number = validate_target(
            target_number=target, number_source=number_source,
            client_phones=client_phones,
        )
        if self._get_attempt(number, 2) is not None:
            return AttemptRecord(2, self._get_attempt(number, 2).call_id,
                                 None, False, "attempt 2 already recorded - no third call")
        first = self._get_attempt(number, 1)
        if first is None or not first.call_id:
            return AttemptRecord(1, None, None, False, "no attempt 1 on record")
        # Status re-read immediately before dispatch.
        classification = self.record_outcome(number and first.call_id,
                                             self.port.get_call_detail(first.call_id))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT dispatch_started_at, ended_at FROM attempts WHERE"
                " policy_version=? AND target=? AND attempt_seq=1",
                (self.config.policy_version, number),
            ).fetchone()
            outcomes = [
                CallOutcome(r["outcome"]) for r in conn.execute(
                    "SELECT outcome FROM attempts WHERE policy_version=? AND"
                    " target=? AND outcome IS NOT NULL ORDER BY attempt_seq",
                    (self.config.policy_version, number),
                )
            ]
        first_dispatch_at = datetime.fromisoformat(row["dispatch_started_at"])
        first_outcome_at = (
            datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
        )
        decision = decide_redial(
            attempt_outcomes=outcomes,
            classification=classification,
            first_dispatch_at=first_dispatch_at,
            now=self._now(),
            caller_id=self.config.caller_id,
            config=self.config,
            first_outcome_at=first_outcome_at,
        )
        if not decision.redial:
            if decision.missed_window:
                self._mark_superseded(number, CallOutcome.MISSED_WINDOW, decision.reason)
            return AttemptRecord(1, first.call_id, first.outcome,
                                 first.conclusive, decision.reason)
        # INSERT-once guard BEFORE dispatch: a crash after dispatch with the
        # row committed still blocks any further redial for this target.
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO attempts (policy_version, target, attempt_seq,"
                    " caller_id, dispatch_started_at) VALUES (?,?,2,?,?)",
                    (self.config.policy_version, number,
                     self.config.caller_id, self._now().isoformat()),
                )
            except sqlite3.IntegrityError:
                return AttemptRecord(2, None, None, False,
                                     "attempt 2 guard already held - no duplicate redial")
        receipt = self.port.dispatch_call(
            target=number, task=task, caller_id=self.config.caller_id,
            metadata={"policy_version": self.config.policy_version,
                      "attempt_seq": 2, "redial_of": first.call_id,
                      "agent_identity": AGENT_IDENTITY,
                      # Attempt 2 LEAVES the voicemail; attempt 1 leaves none.
                      "voicemail_action": "leave_message",
                      "voicemail_script": build_attempt2_voicemail_script(
                          (call_reason or task)[:120])},
        )
        call_id = str(receipt.get("call_id") or "") or None
        with self._connect() as conn:
            conn.execute(
                "UPDATE attempts SET call_id=? WHERE policy_version=? AND"
                " target=? AND attempt_seq=2 AND call_id IS NULL",
                (call_id, self.config.policy_version, number),
            )
        return AttemptRecord(2, call_id, None, False, decision.reason)

    # -- reads -------------------------------------------------------------

    def _get_attempt(self, target: str, seq: int) -> Optional[AttemptRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE policy_version=? AND target=?"
                " AND attempt_seq=?",
                (self.config.policy_version, target, seq),
            ).fetchone()
        if row is None:
            return None
        return AttemptRecord(
            seq, row["call_id"],
            CallOutcome(row["outcome"]) if row["outcome"] else None,
            bool(row["conclusive"]), row["reason"] or "",
        )

    def _mark_superseded(self, target: str, outcome: CallOutcome, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE attempts SET outcome=?, reason=? WHERE policy_version=?"
                " AND target=? AND attempt_seq=1",
                (outcome.value, reason, self.config.policy_version, target),
            )

    def attempts_for(self, target: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM attempts WHERE policy_version=? AND target=?"
                " ORDER BY attempt_seq",
                (self.config.policy_version, target),
            )]

    def event_count(self, call_id: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) c FROM events WHERE call_id=?", (call_id,),
            ).fetchone()["c"]


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _transcript_of(detail: Mapping[str, Any]) -> str:
    for key in ("concatenated_transcript", "transcript", "summary"):
        text = detail.get(key)
        if isinstance(text, str) and text.strip():
            return text
    return ""
