import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext

from robie_job_engine.accountability_jobs import AccountabilityReportWorker, _previous_business_day
from robie_job_engine.accountability_schedule import install_accountability_schedules
from robie_job_engine.models import WorkerResult
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
    daily = next(item for item in reread if item["action_type"] == "accountability.daily")
    assert daily["cron_spec"] == "0 9 * * 1-5"
    assert daily["parameters"]["reporting_period"] == "previous_business_day"


def test_previous_business_day_uses_friday_for_monday():
    assert _previous_business_day(datetime(2026, 9, 7, 13, tzinfo=timezone.utc)).isoformat() == "2026-09-04"


def test_daily_worker_propagates_manifest_holiday_calendar_to_all_collectors(tmp_path):
    from datetime import date

    class LaborDayTuesday(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 8, 10, 25, tzinfo=timezone.utc)

    email = tmp_path / 'email.json'
    email.write_text('{"source_status":"available","by_employee":{}}')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({
        'output_dir': str(tmp_path / 'reports'),
        'rules': {'holiday_calendar': 'US-FEDERAL', 'require_complete_evidence': False},
        'sources': {'email_json': str(email)},
        'google_sheets': {'enabled': True},
        'google_sheet_trackers': {'enabled': True, 'trackers': {'policy_changes': {'range': '{previous_business_week}!A:R'}}},
        'collection': {'ringcentral_email': {'enabled': True}, 'magellan': {'enabled': True}},
        'delivery': {'enabled': False},
    }))

    def fake_build(arguments):
        Path(arguments[arguments.index('--output') + 1]).write_text('Test report\n')
        return 0

    with patch('robie_job_engine.accountability_jobs.datetime', LaborDayTuesday), \
         patch('robie_job_engine.accountability_jobs.build_report', side_effect=fake_build) as build, \
         patch('robie_job_engine.google_sheets_accountability.collect_allowlisted_tables', return_value={}) as sheets, \
         patch('robie_job_engine.google_sheets_accountability.write_allowlisted_table_csv'), \
         patch('robie_job_engine.ringcentral_email_sync.collect_scheduled_ringcentral_report', return_value=tmp_path / 'rc.json') as rc, \
         patch('robie_job_engine.magellan_collection.collect_magellan_snapshot', return_value=tmp_path / 'magellan.json') as magellan:
        result = AccountabilityReportWorker().perform(
            {'action_type': 'accountability.daily', 'payload': {'manifest_path': str(manifest)}},
            idempotency_key='holiday-regression',
        )
    assert result.succeeded, result.error
    assert rc.call_args.kwargs['target_date'] == date(2026, 9, 4)
    assert magellan.call_args.kwargs['target_date'] == date(2026, 9, 4)
    arguments = build.call_args.args[0]
    assert arguments[arguments.index('--report-date') + 1] == '2026-09-04'
    assert sheets.call_count == 2
    assert all(call.kwargs['holiday_calendar'] == 'US-FEDERAL' for call in sheets.call_args_list)


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


def test_weekly_worker_collects_fresh_read_only_submission_snapshot_without_email(tmp_path: Path):
    email = tmp_path / "email.json"
    email.write_text('{"source_status":"available","by_employee":{}}', encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"email_json": str(email)},
        "rules": {"require_complete_evidence": False},
        "collection": {"ezlynx_submission_center": {
            "enabled": True,
            "read_only": True,
            "email_delivery_enabled": False,
        }},
        "delivery": {"enabled": False},
    }), encoding="utf-8")
    observed = {
        "source_status": "available",
        "status_aria_sort": "ascending",
        "first_row_non_closed": True,
        "first_closed_row_inspected": True,
        "day_31_qualifies": True,
        "email_delivery_enabled": False,
        "open_over_30_count": 1,
        "qualifying_records": [{"applicant": "Example LLC"}],
    }

    def fake_build(arguments):
        output = Path(arguments[arguments.index("--output") + 1])
        output.write_text("🏆 *STREETSMART WEEKLY EXECUTIVE PERFORMANCE SCORECARD*\n", encoding="utf-8")
        return 0

    with patch(
        "robie_job_engine.submission_audit.EzlynxSubmissionAuditWorker.perform",
        return_value=WorkerResult(
            True,
            "ezlynx.submission_audit",
            {"expected_postcondition": observed},
            retryable=False,
        ),
    ) as collect, patch(
        "robie_job_engine.accountability_jobs.build_report", side_effect=fake_build
    ) as build:
        result = AccountabilityReportWorker().perform(
            {"action_type": "accountability.weekly", "payload": {"manifest_path": str(manifest)}},
            idempotency_key="weekly-live-submissions",
        )

    assert result.succeeded
    collect.assert_called_once()
    job = collect.call_args.args[0]
    assert job["payload"]["read_only"] is True
    assert job["payload"]["expected_postcondition"]["email_delivery_enabled"] is False
    arguments = build.call_args.args[0]
    snapshot_path = Path(arguments[arguments.index("--submissions-json") + 1])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["open_over_30_count"] == 1


def test_daily_worker_uses_every_approved_roster_mailbox_for_gmail_metadata(tmp_path: Path):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "source_status": "available",
        "employees": {
            "Alex Example": {"role": "CSR", "email": "alex@streetsmart.insurance"},
            "Blair Example": {"role": "Producer", "email": "blair@streetsmart.insurance"},
        },
    }), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"roles_json": str(roles)},
        "rules": {"require_complete_evidence": False},
        "collection": {"gmail_accountability": {
            "enabled": True,
            "approved_domain": "streetsmart.insurance",
        }},
        "delivery": {"enabled": False},
    }), encoding="utf-8")

    observed = {
        "source_status": "available",
        "scope": "https://www.googleapis.com/auth/gmail.metadata",
        "body_access": False,
        "by_employee": {},
    }

    def fake_build(arguments):
        output = Path(arguments[arguments.index("--output") + 1])
        output.write_text("accountability report\n", encoding="utf-8")
        return 0

    with patch(
        "robie_job_engine.gmail_accountability.collect_agency_summary",
        return_value=observed,
    ) as collect, patch(
        "robie_job_engine.accountability_jobs.build_report", side_effect=fake_build
    ):
        result = AccountabilityReportWorker().perform(
            {"action_type": "accountability.daily", "payload": {"manifest_path": str(manifest)}},
            idempotency_key="daily-gmail-roster",
        )

    assert result.succeeded
    assert collect.call_args.kwargs["approved_users"] == (
        "alex@streetsmart.insurance",
        "blair@streetsmart.insurance",
    )


def test_daily_worker_ingests_scheduled_ezlynx_reports_before_build(tmp_path: Path):
    tasks = tmp_path / "tasks.csv"
    tasks.write_text("Assigned To,Status\n", encoding="utf-8")
    email = tmp_path / "email.json"
    email.write_text('{"source_status":"available","by_employee":{}}', encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"email_json": str(email)},
        "rules": {"require_complete_evidence": False},
        "collection": {"scheduled_reports_email": {
            "enabled": True,
            "mailbox": "robie@example.test",
            "allowed_sender_domains": ["ezlynx.com"],
            "reports": {"tasks": {"label": "ROBIE_TASKS"}},
        }},
    }), encoding="utf-8")

    def fake_build(arguments):
        Path(arguments[arguments.index("--output") + 1]).write_text(
            "📋 *STREETSMART DAILY SERVICE & PHONE WATCHDOG*\n", encoding="utf-8"
        )
        return 0

    with patch.dict("os.environ", {
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "worker@example.test",
    }), patch(
        "robie_job_engine.ringcentral_email_sync.build_keyless_report_mailbox_service",
        return_value=object(),
    ), patch(
        "robie_job_engine.scheduled_report_email_sync.collect_scheduled_tabular_reports",
        return_value={"sources": {"tasks": str(tasks)}, "trackers": {}},
    ) as collect, patch(
        "robie_job_engine.accountability_jobs.build_report", side_effect=fake_build
    ) as build:
        result = AccountabilityReportWorker().perform(
            {"action_type": "accountability.daily", "payload": {"manifest_path": str(manifest)}},
            idempotency_key="scheduled-reports-daily",
        )

    assert result.succeeded, result.error
    collect.assert_called_once()
    arguments = build.call_args.args[0]
    assert arguments[arguments.index("--tasks") + 1] == str(tasks.resolve())
