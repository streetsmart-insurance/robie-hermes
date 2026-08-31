import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.accountability_jobs import AccountabilityReportWorker
from robie_job_engine.accountability_schedule import install_accountability_schedules
from robie_job_engine.operations import OperationsStore


def test_report_worker_fails_closed_when_required_evidence_is_missing(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"output_dir": str(tmp_path / "reports"), "sources": {}}), encoding="utf-8")
    job = {"action_type": "accountability.weekly", "payload": {"manifest_path": str(manifest)}}
    result = AccountabilityReportWorker().perform(job, idempotency_key="weekly-1")
    assert not result.succeeded
    assert "missing, stale, partial" in result.error
    report = Path(result.destination["artifact_path"])
    assert report.is_file()
    assert "Inbound Answer Rate: UNVERIFIED" in report.read_text(encoding="utf-8")


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


def test_worker_uses_approved_role_registry_as_current_ringcentral_users(tmp_path: Path):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "source_status": "available",
        "employees": {
            "Alex Example": {"role": "CSR"},
            "Blair Example": {"role": "Producer"},
        },
    }), encoding="utf-8")
    email = tmp_path / "email.json"
    email.write_text('{"source_status":"available","by_employee":{}}', encoding="utf-8")
    missing_ringcentral = tmp_path / "collector-output.json"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"roles_json": str(roles), "email_json": str(email)},
        "rules": {"require_complete_evidence": False},
        "collection": {"ringcentral_email": {
            "enabled": True,
            "mailbox": "robie@streetsmart.insurance",
            "required_queues": ["Commercial Test"],
            "required_queue_members": {"Commercial Test": ["Alex Example"]},
        }},
    }), encoding="utf-8")
    job = {"action_type": "accountability.daily", "payload": {"manifest_path": str(manifest)}}
    with patch(
        "robie_job_engine.ringcentral_email_sync.collect_scheduled_ringcentral_report",
        return_value=missing_ringcentral,
    ) as collect:
        result = AccountabilityReportWorker().perform(job, idempotency_key="daily-users")
    assert result.succeeded
    assert collect.call_args.kwargs["required_users"] == ["Alex Example", "Blair Example"]
    assert collect.call_args.kwargs["required_queue_members"] == {"Commercial Test": ["Alex Example"]}
