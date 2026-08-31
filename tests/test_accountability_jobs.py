import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext

from robie_job_engine.accountability_jobs import AccountabilityReportVerifier, AccountabilityReportWorker
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
            "mailbox": "report-mailbox@example.test",
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


def test_weekly_worker_collects_live_submission_center_read_only(tmp_path: Path):
    email = tmp_path / "email.json"
    email.write_text('{"source_status":"available","by_employee":{}}', encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"email_json": str(email)},
        "rules": {"require_complete_evidence": False},
        "collection": {"ezlynx_submission_browser": {"enabled": True, "read_only": True}},
    }), encoding="utf-8")
    observed = {"pages_inspected": 2, "rows_inspected": 101, "pager_total": 101,
                "open_records_deduplicated": 1, "open_records": [{
                    "Submission Title": "Example", "Submission URL": "https://example.test/s/1",
                    "Applicant": "Example Applicant", "Assigned Producer": "Producer One", "Status": "Quoted",
                    "Quote Due Date": "2026-06-01", "Effective Date": "2026-09-01", "Overdue": "red",
                }]}

    def fake_build(arguments):
        output = Path(arguments[arguments.index("--output") + 1])
        output.write_text("STREETSMART WEEKLY EXECUTIVE PERFORMANCE SCORECARD\n", encoding="utf-8")
        return 0

    with patch("robie_job_engine.ezlynx_session_lock.exclusive_session", return_value=nullcontext()), \
         patch("robie_job_engine.submission_audit.ensure_ezlynx_login"), \
         patch("robie_job_engine.submission_audit.run_weekly_submission_read", return_value=observed) as read, \
         patch("robie_job_engine.accountability_jobs.build_report", side_effect=fake_build):
        result = AccountabilityReportWorker().perform(
            {"action_type": "accountability.weekly", "payload": {"manifest_path": str(manifest)}},
            idempotency_key="weekly-live",
        )
    assert result.succeeded
    read.assert_called_once_with(fresh=True)
    generated = list((tmp_path / "reports").glob("submission-center-*.csv"))
    assert len(generated) == 1
    assert "https://example.test/s/1" in generated[0].read_text(encoding="utf-8")


def test_verifier_independently_checks_source_attachment_digests(tmp_path: Path):
    report = tmp_path / "daily.md"
    report.write_text("STREETSMART DAILY SERVICE & PHONE WATCHDOG\n", encoding="utf-8")
    tasks = tmp_path / "tasks.csv"
    tasks.write_text("Assigned To,Status\nAlex Example,Open\n", encoding="utf-8")
    digest = hashlib.sha256(tasks.read_bytes()).hexdigest()
    direct_manifest = tmp_path / "direct-evidence.json"
    direct_manifest.write_text(json.dumps({
        "attachments": [{"source": "tasks", "path": str(tasks), "sha256": digest}],
    }), encoding="utf-8")
    scheduled_manifest = tmp_path / "scheduled-evidence.json"
    scheduled_manifest.write_text(json.dumps({
        "sources": {"tasks": str(tasks)},
        "trackers": {},
        "evidence": {"tasks": {"source_status": "available", "normalized_csv_sha256": digest}},
    }), encoding="utf-8")
    action = {
        "destination": {
            "artifact_path": str(report),
            "source_evidence_manifests": [str(direct_manifest), str(scheduled_manifest)],
        },
        "detail": {"sha256": hashlib.sha256(report.read_bytes()).hexdigest()},
    }
    job = {"action_type": "accountability.daily"}
    assert AccountabilityReportVerifier().verify(job, action).verified
    tasks.write_text("Assigned To,Status\nTampered,Closed\n", encoding="utf-8")
    assert not AccountabilityReportVerifier().verify(job, action).verified
