"""Non-Ascend release contract. No network, browser, or credentials."""

import json
import subprocess
import sys
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.job_schema import get_bounded_job_schema, get_executable_skill_contract
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION, classify_request
from robie_job_engine.release_profile import ASCEND_REQUEST_FIXTURES
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_runtime_engine


def test_ascend_requests_route_to_unavailable_and_never_complete():
    requests = ASCEND_REQUEST_FIXTURES
    with durable_temporary_directory() as tmp:
        db = str(Path(tmp) / "jobs.db")
        for index, text in enumerate(requests):
            classification = classify_request(text)
            assert classification.action_type == "hermes.unavailable"
            assert classification.hold_status == JobStatus.FAILED.value
            job_id = open_chat_job(db, f"ascend-unavailable-{index}", text)
            guarded = guard_chat_response(db, job_id, "COMPLETE — program created")
            job = JobStore(db).get_job(job_id)
            assert job["status"] == JobStatus.FAILED.value
            assert "ASCEND_UNAVAILABLE" in (job["last_error"] or "")
            assert "— FAILED" in guarded
            assert "No success claims" in guarded
            assert "does not authorize COMPLETE" in guarded


def test_ascend_has_no_worker_schema_or_executable_contract():
    assert not any(key.startswith("ascend.") for key in WORKER_FOR_ACTION)
    assert not any(key.startswith("ascend.") for key in BOUNDED_ENGINE_ACTIONS)
    assert get_bounded_job_schema("ascend.create_program") is None
    assert get_executable_skill_contract("ascend.create_program") is None
    with durable_temporary_directory() as tmp:
        engine = build_runtime_engine(JobStore(Path(tmp) / "jobs.db"))
        assert not any(name.startswith("ascend") for name in engine.workers)


def test_release_export_rules_exclude_ascend_execution_surfaces():
    rules = Path(".gitattributes").read_text(encoding="utf-8")
    for required in (
        "robie_job_engine/ascend*.py export-ignore",
        "robie_job_engine/locators/ascend.json export-ignore",
        "deploy/hermes/skills/ascend-* export-ignore",
        "skills/ascend-* export-ignore",
        "scripts/*ascend* export-ignore",
    ):
        assert required in rules


def test_readonly_test_job_audit_is_redacted_and_unique():
    with durable_temporary_directory() as tmp:
        db = Path(tmp) / "jobs.db"
        store = JobStore(db)
        job = store.create_job(
            "ezlynx.commercial_auto",
            {
                "text": "Commercial Auto for ROBIE Test LLC",
                "applicant_id": "220250093",
            },
        )
        store.add_playwright_exec(
            job["id"],
            tool="playwright_exec",
            status="PASS",
            code_preview="page.goto('sensitive-destination')",
            result={"private": "value"},
        )
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/audit-test-job-readonly.py",
                "--db",
                str(db),
                "--job-prefix",
                job["id"][:8],
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        report = json.loads(completed.stdout)
        assert report["job"]["expected_test_identity"] == {
            "applicant_220250093": True,
            "commercial_auto": True,
            "robie_test_llc": True,
        }
        assert report["completion_authorization"] == "NOT_FOUND"
        assert "sensitive-destination" not in completed.stdout
        assert '"private"' not in completed.stdout
