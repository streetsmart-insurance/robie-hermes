import os
import stat
import tempfile
from pathlib import Path

from robie_job_engine.model_fallback import ModelTarget, execute_with_fallback
from robie_job_engine.operations import OperationsStore, ingest_chat_attachments
from robie_job_engine.store import JobStore


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    db = root / "jobs.db"
    jobs = JobStore(str(db))
    job = jobs.create_job("ezlynx.document_upload", {"account_id": "test"},
                          idempotency_key="smoke:doc")
    src = root / "renewal packet.pdf"
    src.write_bytes(b"%PDF ROBIE ingestion acceptance")
    one = ingest_chat_attachments(str(db), job["id"], "spaces/test/messages/test",
                                  [(str(src), "application/pdf")], artifact_root=str(root / "artifacts"))
    two = ingest_chat_attachments(str(db), job["id"], "spaces/test/messages/test",
                                  [(str(src), "application/pdf")], artifact_root=str(root / "artifacts"))
    assert one[0]["id"] == two[0]["id"]
    assert stat.S_IMODE(os.stat(one[0]["stored_path"]).st_mode) == 0o600
    ops = OperationsStore(str(db), str(root / "artifacts"))
    try:
        ops.mark_artifact_uploaded(one[0]["id"], "fake", {"verified": False})
        raise AssertionError("false verification was accepted")
    except ValueError:
        pass
    due = ops.create_schedule("Hourly", "ezlynx.assignment_poll", {}, 60,
                              next_run_at="2020-01-01T00:00:00+00:00")
    assert ops.claim_due_schedules()[0]["id"] == due["id"]
    attempts = []
    def call(target):
        if not attempts:
            attempts.append(target.model)
            raise TimeoutError("deliberate primary outage")
        attempts.append(target.model)
        return "ok"
    result = execute_with_fallback(
        [ModelTarget("vertex", "primary"), ModelTarget("vertex", "fallback")], call,
        lambda *args: None,
    )
    assert result == "ok" and attempts == ["primary", "fallback"]

    thought_calls = []
    repaired = []
    def thought_call(target):
        thought_calls.append(target.model)
        if len(thought_calls) == 1:
            raise RuntimeError("HTTP 400: Invalid thought signature.")
        return "recovered"
    assert execute_with_fallback(
        [ModelTarget("vertex", "primary")], thought_call, lambda *args: None,
        repair_invalid_thought=lambda target, error: repaired.append(target.model),
    ) == "recovered"
    assert thought_calls == ["primary", "primary"] and repaired == ["primary"]

print("PASS: durable ingestion, idempotency, verification gate, scheduling, model fallback, thought repair")
