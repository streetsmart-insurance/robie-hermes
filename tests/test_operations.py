import hashlib
import sqlite3
from pathlib import Path

import pytest

from robie_job_engine.model_fallback import ModelTarget, execute_with_fallback
from robie_job_engine.operations import OperationsStore, ingest_chat_attachments


def seed_jobs(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE jobs (
          id TEXT PRIMARY KEY, action_type TEXT, status TEXT, attempt_count INTEGER DEFAULT 0,
          verification_count INTEGER DEFAULT 0, next_wakeup_at TEXT, created_at TEXT,
          updated_at TEXT, completed_at TEXT, last_error TEXT
        );
        CREATE TABLE verification_evidence (
          job_id TEXT, verified INTEGER, evidence_type TEXT, evidence_json TEXT, created_at TEXT
        );
        INSERT INTO jobs VALUES ('job-1','upload','RUNNING',0,0,NULL,'now','now',NULL,NULL);
        """)


def test_ingestion_is_durable_hashed_private_and_idempotent(tmp_path: Path):
    db, root = tmp_path / "jobs.db", tmp_path / "artifacts"
    seed_jobs(db)
    src = tmp_path / "renewal packet.pdf"
    src.write_bytes(b"%PDF durable test")
    first = ingest_chat_attachments(str(db), "job-1", "spaces/x/messages/y",
                                    [(str(src), "application/pdf")], artifact_root=str(root))
    second = ingest_chat_attachments(str(db), "job-1", "spaces/x/messages/y",
                                     [(str(src), "application/pdf")], artifact_root=str(root))
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    staged = Path(first[0]["stored_path"])
    assert staged.read_bytes() == src.read_bytes()
    assert staged.stat().st_mode & 0o777 == 0o600


def test_artifact_cannot_be_marked_uploaded_without_independent_evidence(tmp_path: Path):
    db, root = tmp_path / "jobs.db", tmp_path / "artifacts"
    seed_jobs(db)
    src = tmp_path / "x.pdf"; src.write_bytes(b"x")
    ops = OperationsStore(str(db), str(root))
    artifact = ops.ingest_cached_file(job_id="job-1", source_path=str(src),
        source_external_id="message:0", mime_type="application/pdf")
    with pytest.raises(ValueError):
        ops.mark_artifact_uploaded(artifact["id"], "EZLynx/Documents/x.pdf", {"verified": False})


def test_schedule_due_and_advance(tmp_path: Path):
    db = tmp_path / "jobs.db"; seed_jobs(db)
    ops = OperationsStore(str(db), str(tmp_path / "a"))
    schedule = ops.create_schedule("Hourly intake", "ezlynx.assignment_poll", {}, 60,
                                   next_run_at="2020-01-01T00:00:00+00:00")
    assert [x["id"] for x in ops.claim_due_schedules()] == [schedule["id"]]
    ops.advance_schedule(schedule["id"], "job-1", from_time="2026-01-01T00:00:00+00:00")
    assert ops.get_schedule(schedule["id"])["next_run_at"].startswith("2026-01-01T01:00:00")


def test_model_fallback_records_both_attempts():
    calls = []; records = []
    def call(target):
        calls.append(target.model)
        if len(calls) == 1: raise TimeoutError("primary unavailable")
        return "ok"
    result = execute_with_fallback(
        [ModelTarget("vertex", "gemini-primary"), ModelTarget("vertex", "gemini-fallback")],
        call, lambda target, ordinal, outcome, error: records.append((ordinal, outcome)))
    assert result == "ok"
    assert records == [(1, "RETRYABLE_FAILURE"), (2, "SUCCESS")]
