import hashlib
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from function_loader import load_function_tests
from robie_job_engine.model_fallback import (
    ModelTarget,
    execute_with_fallback,
    strip_gemini_thought_signatures,
)
from robie_job_engine.operations import (
    ModelBudgetExceeded,
    OperationsStore,
    ingest_chat_attachments,
)
from robie_job_engine.chat_guard import build_chat_execution_text, guard_chat_response
from robie_job_engine.store import JobStore
from robie_job_engine.models import JobStatus
from robie_job_engine.reporting import prepare_status_digest


def load_tests(loader, tests, pattern):
    return load_function_tests(globals())


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


def test_job_artifact_dir_is_exactly_full_job_id(tmp_path: Path):
    db, root = tmp_path / "jobs.db", tmp_path / "artifacts"
    seed_jobs(db)
    ops = OperationsStore(str(db), str(root))
    job_id = "eb96f620-f8c3-4006-8eb4-d938d2a44c73"
    artifact_id = "3a41af0e-ca7f-4a57-8cae-67d3ca55c1c5"
    folder = ops.job_artifact_dir(job_id)
    assert folder == Path(root) / job_id
    assert folder.name == job_id
    assert artifact_id not in str(folder)
    assert str(folder).endswith(job_id)
    src = tmp_path / "quote.pdf"
    src.write_bytes(b"%PDF test")
    record = ops.ingest_cached_file(
        job_id=job_id,
        source_path=str(src),
        source_external_id="message:quote",
        mime_type="application/pdf",
    )
    stored = Path(record["stored_path"])
    assert stored.parent == folder
    assert stored.parent.name == job_id


def test_artifact_cannot_be_marked_uploaded_without_independent_evidence(tmp_path: Path):
    db, root = tmp_path / "jobs.db", tmp_path / "artifacts"
    seed_jobs(db)
    src = tmp_path / "x.pdf"; src.write_bytes(b"x")
    ops = OperationsStore(str(db), str(root))
    artifact = ops.ingest_cached_file(job_id="job-1", source_path=str(src),
        source_external_id="message:0", mime_type="application/pdf")
    with unittest.TestCase().assertRaises(ValueError):
        ops.mark_artifact_uploaded(artifact["id"], "EZLynx/Documents/x.pdf", {"verified": False})


def test_schedule_due_and_advance(tmp_path: Path):
    db = tmp_path / "jobs.db"; seed_jobs(db)
    ops = OperationsStore(str(db), str(tmp_path / "a"))
    schedule = ops.create_schedule("Hourly intake", "ezlynx.assignment_poll", {}, 60,
                                   next_run_at="2020-01-01T00:00:00+00:00")
    assert [x["id"] for x in ops.claim_due_schedules()] == [schedule["id"]]
    ops.advance_schedule(schedule["id"], "job-1", from_time="2026-01-01T00:00:00+00:00")
    assert ops.get_schedule(schedule["id"])["next_run_at"].startswith("2026-01-01T01:00:00")


def test_schedule_occurrence_key_is_stable_across_restart_boundary(tmp_path: Path):
    db = tmp_path / "jobs.db"
    JobStore(db)
    ops = OperationsStore(str(db), str(tmp_path / "artifacts"))
    schedule = ops.create_schedule(
        "Two-hour status", "robie.status_report", {"destination": "spaces/robie-dm"},
        120, next_run_at="2020-01-01T00:59:59+00:00",
    )
    expected = f"schedule:{schedule['id']}:2020-01-01T00:59:59+00:00"
    first = JobStore(db).create_job(
        schedule["action_type"], schedule["payload"], idempotency_key=expected,
    )
    second = JobStore(db).create_job(
        schedule["action_type"], schedule["payload"], idempotency_key=expected,
    )
    assert first["id"] == second["id"]


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


def test_model_usage_is_durable_visible_and_budgeted(tmp_path: Path):
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    job = store.create_job("hermes.google_chat_task", {"text": "bounded request"})
    ops = OperationsStore(str(db), str(tmp_path / "artifacts"))
    ops.record_model_attempt(
        job_id=job["id"], provider="vertex", model="gemini-primary", ordinal=1,
        outcome="SUCCESS", latency_ms=3100, input_tokens=9898, output_tokens=390,
        cache_read_tokens=0, thinking_tokens=362, estimated_cost_usd=0.012345,
    )
    usage = ops.model_usage(job["id"])
    assert usage == {
        "input_tokens": 9898,
        "output_tokens": 390,
        "cache_read_tokens": 0,
        "thinking_tokens": 362,
        "model_attempt_count": 1,
        "total_tokens": 10288,
        "estimated_cost_usd": 0.012345,
    }
    dashboard_job = ops.dashboard_rows()["jobs"][0]
    assert dashboard_job["total_tokens"] == 10288
    with unittest.TestCase().assertRaises(ModelBudgetExceeded):
        ops.enforce_model_budget(
            job["id"], max_total_tokens=11_000, max_cost_usd=1.0,
            reserve_tokens=1_000,
        )
    with unittest.TestCase().assertRaises(ModelBudgetExceeded):
        ops.enforce_model_budget(
            job["id"], max_total_tokens=20_000, max_cost_usd=0.01,
        )


def test_budget_authorizer_stops_before_an_extra_model_call():
    calls = []

    def call(target):
        calls.append(target.model)
        raise TimeoutError("primary unavailable")

    def authorize(target, ordinal):
        if calls:
            raise ModelBudgetExceeded("bounded before fallback")

    with unittest.TestCase().assertRaises(ModelBudgetExceeded):
        execute_with_fallback(
            [ModelTarget("vertex", "primary"), ModelTarget("vertex", "fallback")],
            call,
            lambda *args: None,
            authorize_call=authorize,
        )
    assert calls == ["primary"]


def test_release_promotion_and_one_action_rollback_require_verified_digests(tmp_path: Path):
    db = tmp_path / "jobs.db"
    JobStore(db)
    ops = OperationsStore(str(db), str(tmp_path / "artifacts"))
    first = "1" * 64
    second = "2" * 64
    evidence = {"verified": True, "authoritative": True, "source": "Test readback"}
    ops.register_release(
        environment="TEST", digest=first, commit_sha="a" * 40,
        artifact_uri="gs://test-releases/first.tgz",
    )
    with unittest.TestCase().assertRaises(ValueError):
        ops.promote_verified_release("TEST", first, actor="Carlo", evidence=evidence)
    ops.verify_release("TEST", first, evidence)
    state = ops.promote_verified_release("TEST", first, actor="Carlo", evidence=evidence)
    assert state["current_digest"] == first and state["previous_digest"] is None
    ops.register_release(
        environment="TEST", digest=second, commit_sha="b" * 40,
        artifact_uri="gs://test-releases/second.tgz",
    )
    ops.verify_release("TEST", second, evidence)
    state = ops.promote_verified_release("TEST", second, actor="Carlo", evidence=evidence)
    assert state["current_digest"] == second and state["previous_digest"] == first
    rolled_back = ops.rollback_release("TEST", actor="Carlo", evidence=evidence)
    assert rolled_back["current_digest"] == first
    assert rolled_back["previous_digest"] == second


def test_production_release_state_is_isolated_from_test(tmp_path: Path):
    db = tmp_path / "jobs.db"
    JobStore(db)
    ops = OperationsStore(str(db), str(tmp_path / "artifacts"))
    digest = "3" * 64
    evidence = {"verified": True, "authoritative": True}
    ops.register_release(
        environment="TEST", digest=digest, commit_sha="c" * 40,
        artifact_uri="gs://test-releases/third.tgz",
    )
    ops.verify_release("TEST", digest, evidence)
    ops.promote_verified_release("TEST", digest, actor="Carlo", evidence=evidence)
    assert ops.release_state("PRODUCTION")["current_digest"] is None


def test_production_promotion_requires_explicit_approval_and_digest_is_immutable(tmp_path: Path):
    db = tmp_path / "jobs.db"
    JobStore(db)
    ops = OperationsStore(str(db), str(tmp_path / "artifacts"))
    digest = "4" * 64
    ops.register_release(
        environment="PRODUCTION", digest=digest, commit_sha="d" * 40,
        artifact_uri="gs://production-releases/fourth.tgz",
    )
    with unittest.TestCase().assertRaises(ValueError):
        ops.register_release(
            environment="PRODUCTION", digest=digest, commit_sha="e" * 40,
            artifact_uri="gs://production-releases/rebound.tgz",
        )
    evidence = {"verified": True, "authoritative": True}
    ops.verify_release("PRODUCTION", digest, evidence)
    with unittest.TestCase().assertRaises(ValueError):
        ops.promote_verified_release(
            "PRODUCTION", digest, actor="Carlo", evidence=evidence,
        )
    approved = {**evidence, "approved": True, "decision_id": "decision-1"}
    state = ops.promote_verified_release(
        "PRODUCTION", digest, actor="Carlo", evidence=approved,
    )
    assert state["current_digest"] == digest


def test_two_hour_report_is_plain_english_model_free_and_idempotent(tmp_path: Path):
    db = tmp_path / "jobs.db"
    jobs = JobStore(db)
    job = jobs.create_job("hermes.google_chat_task", {"text": "upload renewal"})
    jobs.transition(job["id"], JobStatus.UNVERIFIED, error="destination not verified")
    at = datetime.now(timezone.utc)
    first = prepare_status_digest(
        str(db), destination="spaces/robie-dm", window_hours=2, now=at,
        artifact_root=str(tmp_path / "artifacts"),
    )
    second = prepare_status_digest(
        str(db), destination="spaces/robie-dm", window_hours=2, now=at,
        artifact_root=str(tmp_path / "artifacts"),
    )
    assert first.report_run["id"] == second.report_run["id"]
    assert first.summary["needs_attention"] == 1
    assert first.summary["total_tokens"] == 0
    assert "Needs attention: 1" in first.text


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
    with unittest.TestCase().assertRaisesRegex(RuntimeError, "HTTP 400"):
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
    assert "Not verified." in result
    assert "suppressed" in result
