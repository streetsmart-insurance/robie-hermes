import hashlib
import sqlite3
from pathlib import Path

import pytest

from robie_job_engine.model_fallback import (
    ModelTarget,
    execute_with_fallback,
    strip_gemini_thought_signatures,
)
from robie_job_engine.operations import OperationsStore, ingest_chat_attachments
from robie_job_engine.chat_guard import build_chat_execution_text, guard_chat_response
from robie_job_engine.store import JobStore
from robie_job_engine.models import JobStatus


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


def test_invalid_thought_signature_repairs_history_and_retries_same_model():
    calls = []
    records = []
    repaired = []

    def call(target):
        calls.append(target.model)
        if len(calls) == 1:
            raise RuntimeError("HTTP 400: Invalid thought signature.")
        return "ok"

    result = execute_with_fallback(
        [ModelTarget("vertex", "gemini-primary")],
        call,
        lambda target, ordinal, outcome, error: records.append(outcome),
        repair_invalid_thought=lambda target, error: repaired.append(target.model),
    )
    assert result == "ok"
    assert calls == ["gemini-primary", "gemini-primary"]
    assert repaired == ["gemini-primary"]
    assert records == ["REPAIRABLE_CONTEXT_FAILURE", "SUCCESS_AFTER_CONTEXT_REPAIR"]


def test_thought_signature_scrubber_preserves_business_content():
    history = [{
        "role": "assistant",
        "content": [{
            "type": "thinking",
            "thought": "private model state",
            "thought_signature": "endpoint-scoped-value",
        }, {"type": "text", "text": "Upload the quote"}],
    }]
    cleaned = strip_gemini_thought_signatures(history)
    assert "thought_signature" not in cleaned[0]["content"][0]
    assert cleaned[0]["content"][1]["text"] == "Upload the quote"


def test_http_503_falls_back_but_unrelated_400_does_not():
    calls = []

    def transient(target):
        calls.append(target.model)
        if len(calls) == 1:
            raise RuntimeError("HTTP 503: provider unavailable")
        return "ok"

    assert execute_with_fallback(
        [ModelTarget("vertex", "primary"), ModelTarget("vertex", "fallback")],
        transient,
        lambda *args: None,
    ) == "ok"
    with pytest.raises(RuntimeError, match="HTTP 400"):
        execute_with_fallback(
            [ModelTarget("vertex", "primary"), ModelTarget("vertex", "fallback")],
            lambda target: (_ for _ in ()).throw(RuntimeError("HTTP 400: malformed request")),
            lambda *args: None,
        )


def test_chat_execution_contract_exposes_durable_staged_path(tmp_path: Path):
    db, root = tmp_path / "jobs.db", tmp_path / "artifacts"
    store = JobStore(db)
    job = store.create_job("hermes.google_chat_task", {"message_id": "m1", "text": "upload it"})
    src = tmp_path / "quote.pdf"
    src.write_bytes(b"%PDF quote")
    artifact = ingest_chat_attachments(str(db), job["id"], "m1", [(str(src), "application/pdf")], artifact_root=str(root))[0]
    store.checkpoint(job["id"], "ingestion", {"artifacts": [{
        "name": artifact["original_name"], "mime_type": artifact["mime_type"],
        "sha256": artifact["sha256"], "stored_path": artifact["stored_path"],
    }]})
    prompt = build_chat_execution_text(str(db), job["id"], "upload it")
    assert artifact["stored_path"] in prompt
    assert "file chooser" in prompt
    assert "final completion authority" in prompt


def test_chat_execution_contract_adds_submission_routing(tmp_path: Path):
    db, root = tmp_path / "jobs.db", tmp_path / "artifacts"
    store = JobStore(db)
    job = store.create_job("hermes.google_chat_task", {"message_id": "m3", "text": "upload renewal quote"})
    src = tmp_path / "quote.pdf"
    src.write_bytes(b"%PDF quote")
    artifact = ingest_chat_attachments(str(db), job["id"], "m3", [(str(src), "application/pdf")], artifact_root=str(root))[0]
    store.checkpoint(job["id"], "ingestion", {"artifacts": [{
        "name": artifact["original_name"], "mime_type": artifact["mime_type"],
        "sha256": artifact["sha256"], "stored_path": artifact["stored_path"],
    }]})
    prompt = build_chat_execution_text(str(db), job["id"], "Upload this high-risk manual renewal quote to its submission")
    assert "exact Submission Center folder for this submission" in prompt
    assert "Submission Center activity discussion" in prompt
    assert "Start the High Risk Renewal workflow" in prompt
    assert "Never create or use an Untitled discussion" in prompt


def test_unverified_chat_response_suppresses_worker_success_claim(tmp_path: Path):
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    job = store.create_job("hermes.google_chat_task", {"message_id": "m2", "text": "do it"})
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    result = guard_chat_response(str(db), job["id"], "SUCCESS: I definitely uploaded it")
    assert "definitely uploaded" not in result
    assert "UNVERIFIED" in result
    assert "suppressed" in result
