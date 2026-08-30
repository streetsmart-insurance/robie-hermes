import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from robie_job_engine.accountability_jobs import AccountabilityReportVerifier, AccountabilityReportWorker
from robie_job_engine.accountability_schedule import install_accountability_schedules
from robie_job_engine.operations import OperationsStore


def test_report_worker_builds_and_verifies_fail_closed_artifact(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"output_dir": str(tmp_path / "reports"), "sources": {}}), encoding="utf-8")
    job = {"action_type": "accountability.weekly", "payload": {"manifest_path": str(manifest)}}
    result = AccountabilityReportWorker().perform(job, idempotency_key="weekly-1")
    assert result.succeeded
    report = Path(result.destination["artifact_path"])
    assert report.is_file()
    assert "Inbound Answer Rate: UNVERIFIED" in report.read_text(encoding="utf-8")
    verification = AccountabilityReportVerifier().verify(job, asdict(result))
    assert verification.verified


def test_schedule_installer_creates_three_verified_schedules(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"output_dir": str(tmp_path / "reports"), "sources": {}}), encoding="utf-8")
    db = tmp_path / "jobs.db"
    installed = install_accountability_schedules(
        str(db),
        str(manifest),
        now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
    )
    assert [item["action_type"] for item in installed] == [
        "accountability.daily",
        "accountability.weekly",
        "accountability.monthly",
    ]
    reread = OperationsStore(str(db), artifact_root=str(tmp_path / "artifacts")).list_recurring_jobs()
    assert len(reread) == 3
    assert all(item["enabled"] == 1 for item in reread)
