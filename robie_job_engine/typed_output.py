"""Structured outputs and append-only audit with evidence lineage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .store import utc_now


class TypedOutputError(RuntimeError):
    pass


COT_KEYS = frozenset(
    {
        "chain_of_thought",
        "chain-of-thought",
        "reasoning",
        "thoughts",
        "scratchpad",
        "hidden_reasoning",
    }
)


class TypedOutputStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS typed_outputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    evidence_ref TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def emit(
        self,
        *,
        run_id: str,
        schema_name: str,
        payload: dict[str, Any],
        enums: dict[str, set[str]] | None = None,
        cardinality: dict[str, int] | None = None,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        if any(key in COT_KEYS for key in payload):
            raise TypedOutputError("chain-of-thought is not allowed in user output")
        for key, allowed in (enums or {}).items():
            value = payload.get(key)
            if value is None:
                raise TypedOutputError(f"enum field {key} is required")
            if value == "***":
                raise TypedOutputError("enum values cannot be collapsed to ***")
            if value not in allowed:
                raise TypedOutputError(f"invalid enum for {key}: {value}")
        for key, expected in (cardinality or {}).items():
            value = payload.get(key)
            if not isinstance(value, list) or len(value) != expected:
                raise TypedOutputError(
                    f"field {key} must contain exactly {expected} items"
                )
        record = {
            "run_id": run_id,
            "schema_name": schema_name,
            "payload": payload,
            "created_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO typed_outputs(run_id,schema_name,payload_json,created_at)
                   VALUES (?,?,?,?)""",
                (run_id, schema_name, json.dumps(payload, sort_keys=True), record["created_at"]),
            )
        self.append_audit(
            run_id=run_id,
            event_type="typed_output",
            payload={"schema_name": schema_name, "payload": payload},
            evidence_ref=evidence_ref,
        )
        return record

    def append_audit(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        created = utc_now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO audit_events(run_id,event_type,evidence_ref,payload_json,created_at)
                   VALUES (?,?,?,?,?)""",
                (run_id, event_type, evidence_ref, json.dumps(payload, sort_keys=True), created),
            )
        return {
            "run_id": run_id,
            "event_type": event_type,
            "evidence_ref": evidence_ref,
            "payload": payload,
            "created_at": created,
        }

    def rewrite_audit(self, run_id: str) -> None:
        raise TypedOutputError("audit log is append-only")

    def outputs(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT schema_name, payload_json, created_at FROM typed_outputs
                   WHERE run_id=? ORDER BY id""",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def audit(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT event_type, evidence_ref, payload_json, created_at FROM audit_events
                   WHERE run_id=? ORDER BY id""",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result


def user_visible_text(structured: dict[str, Any]) -> str:
    """Chat text is derived from structured output, never the reverse."""
    return json.dumps(structured, sort_keys=True)
