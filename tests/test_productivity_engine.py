"""Tests for StreetSmart Productivity & Accountability Engine."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from robie_job_engine.ezlynx_productivity_sync import EZLynxProductivityParser
from robie_job_engine.productivity import (
    EZLynxActivityMetric,
    EZLynxTaskMetric,
    ProductivityAuditor,
    RingCentralCall,
    format_phone,
    normalize_phone,
)
from robie_job_engine.productivity_pipeline import ProductivityPipeline
from robie_job_engine.ringcentral_client import RingCentralClient


def test_normalize_phone():
    assert normalize_phone("+1 (555) 123-4567") == "5551234567"
    assert normalize_phone("15551234567") == "5551234567"
    assert normalize_phone("5551234567") == "5551234567"
    assert format_phone("5551234567") == "(555) 123-4567"


def test_missed_call_reconciliation_resolved():
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    base_time = datetime(2026, 8, 29, 14, 0, 0, tzinfo=timezone.utc)

    calls = [
        # Customer calls at 2:00 PM, missed
        RingCentralCall(
            call_id="call-01",
            direction="Inbound",
            from_number="5551234567",
            to_number="5559990001",
            result="Missed",
            duration_seconds=0,
            start_time=base_time,
            extension="101",
            employee_name="Sarah",
        ),
        # Sarah calls customer back at 2:15 PM (15 mins later)
        RingCentralCall(
            call_id="call-02",
            direction="Outbound",
            from_number="5559990001",
            to_number="5551234567",
            result="Call connected",
            duration_seconds=180,
            start_time=base_time + timedelta(minutes=15),
            extension="101",
            employee_name="Sarah",
        ),
    ]

    incidents = auditor.reconcile_missed_calls(calls, reference_time=base_time + timedelta(hours=2))
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.status == "RESOLVED"
    assert inc.response_time_minutes == 15.0
    assert inc.returned_by == "Sarah"


def test_jackie_orphaned_missed_call_scenario():
    """Test the failure mode where a rep misses a call, leaves it unreturned, causing lost account risk."""
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    missed_time = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
    ref_time = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone.utc)  # 5 hours later

    calls = [
        # Inbound call from high-value client to Jackie -> Missed / Voicemail
        RingCentralCall(
            call_id="call-jackie-missed",
            direction="Inbound",
            from_number="5552345678",
            to_number="5559990002",
            result="Voicemail",
            duration_seconds=45,
            start_time=missed_time,
            extension="102",
            employee_name="Jackie",
        ),
        # Jackie made some other outbound call to a different number
        RingCentralCall(
            call_id="call-jackie-other",
            direction="Outbound",
            from_number="5559990002",
            to_number="5559998888",
            result="Call connected",
            duration_seconds=120,
            start_time=missed_time + timedelta(minutes=60),
            extension="102",
            employee_name="Jackie",
        ),
    ]

    tasks = [
        EZLynxTaskMetric(employee_name="Jackie", completed_today=1, overdue=4, open_total=5)
    ]
    activities = [
        EZLynxActivityMetric(employee_name="Jackie", notes_count=2)
    ]

    audit = auditor.generate_audit(calls, tasks=tasks, activities=activities, reference_time=ref_time)
    
    assert len(audit["critical_alerts"]) == 1
    assert "Jackie" in audit["critical_alerts"][0]
    assert "(555) 234-5678" in audit["critical_alerts"][0]

    jackie_report = audit["employee_reports"]["Jackie"]
    assert jackie_report["missed_calls_orphaned"] == 1
    assert jackie_report["missed_calls_resolved"] == 0
    assert jackie_report["ezlynx_tasks_overdue"] == 4
    # Score is penalized severely for orphaned missed call (25 pts) and 4 overdue tasks (20 pts)
    assert jackie_report["productivity_score"] <= 60.0

    # Format Google Chat card
    chat_card = auditor.format_google_chat_card(audit)
    assert "CRITICAL ACCOUNT RISK" in chat_card
    assert "Jackie" in chat_card
    assert "Unreturned 🚨" in chat_card


def test_cross_employee_callback_resolution():
    """If Jackie misses a call but Sarah returns the call to the client, it is marked resolved."""
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    base_time = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)

    calls = [
        # Customer calls Jackie, goes to voicemail
        RingCentralCall(
            call_id="call-01",
            direction="Inbound",
            from_number="5554443322",
            to_number="5559990002",
            result="Voicemail",
            duration_seconds=30,
            start_time=base_time,
            extension="102",
            employee_name="Jackie",
        ),
        # Sarah notices and calls the client back 20 mins later
        RingCentralCall(
            call_id="call-02",
            direction="Outbound",
            from_number="5559990001",
            to_number="5554443322",
            result="Call connected",
            duration_seconds=300,
            start_time=base_time + timedelta(minutes=20),
            extension="101",
            employee_name="Sarah",
        ),
    ]

    incidents = auditor.reconcile_missed_calls(calls, reference_time=base_time + timedelta(hours=1))
    assert len(incidents) == 1
    assert incidents[0].status == "RESOLVED"
    assert incidents[0].returned_by == "Sarah"
    assert incidents[0].response_time_minutes == 20.0


def test_ezlynx_csv_parsing():
    tasks_csv = """Assigned To,Task Type,Priority,Due Date,Status
Jackie,Certificate of Insurance,High,08/15/2026,Overdue
Jackie,Endorsement Request,Normal,08/20/2026,Overdue
Jackie,Renewal Review,Low,08/29/2026,Completed
Sarah,COI Request,High,08/29/2026,Completed
Sarah,Billing Inquiry,Normal,08/29/2026,Completed
"""
    task_metrics = EZLynxProductivityParser.parse_tasks_csv(tasks_csv)
    by_user = {t.employee_name: t for t in task_metrics}

    assert "Jackie" in by_user
    assert by_user["Jackie"].overdue == 2
    assert by_user["Jackie"].completed_today == 1
    assert by_user["Jackie"].high_priority_overdue == 1

    assert "Sarah" in by_user
    assert by_user["Sarah"].overdue == 0
    assert by_user["Sarah"].completed_today == 2

    activities_csv = """Created By,Activity Type,Date Created
Jackie,Discussion Note,08/29/2026 10:00
Jackie,Discussion Note,08/29/2026 11:30
Sarah,Quote Created,08/29/2026 09:15
Sarah,Policy Change,08/29/2026 13:00
Sarah,Discussion Note,08/29/2026 14:00
"""
    act_metrics = EZLynxProductivityParser.parse_activities_csv(activities_csv)
    act_by_user = {a.employee_name: a for a in act_metrics}

    assert act_by_user["Jackie"].notes_count == 2
    assert act_by_user["Sarah"].notes_count == 3
    assert act_by_user["Sarah"].quotes_created == 1


def test_non_client_lead_audit_and_magellan_sentiment():
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    base_time = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)

    calls = [
        # Call 1: New Prospect / Non-client calls in, leaves 200s hold/voicemail (frustrated)
        RingCentralCall(
            call_id="call-prospect-1",
            direction="Inbound",
            from_number="7325550199",
            to_number="7324628343",
            result="Voicemail",
            duration_seconds=210,
            start_time=base_time,
            extension="9006",
            employee_name="Commercial Queue",
        ),
        # Call 2: Existing client calls in
        RingCentralCall(
            call_id="call-client-1",
            direction="Inbound",
            from_number="7329007987",
            to_number="7324628343",
            result="Voicemail",
            duration_seconds=90,
            start_time=base_time + timedelta(minutes=5),
            extension="9042",
            employee_name="Jackie",
        ),
    ]

    known_clients = {"7329007987"}  # Costa 1 Cleaning is known client

    # Audit non-clients
    non_client_audits = auditor.audit_non_clients(calls, known_client_phones=known_clients)
    assert len(non_client_audits) == 1
    assert non_client_audits[0].phone_number == "7325550199"
    assert non_client_audits[0].was_returned is False

    # Audit Magellan sentiment
    sentiment_flags = auditor.analyze_magellan_sentiment(calls, hold_time_threshold_seconds=180)
    assert len(sentiment_flags) == 1
    assert sentiment_flags[0]["phone"] == "7325550199"
    assert sentiment_flags[0]["sentiment"] == "FRUSTRATED_HIGH_RISK"

    pipeline = ProductivityPipeline(chat_space=None)
    calls = [
        RingCentralCall(
            call_id="c1",
            direction="Inbound",
            from_number="5551112222",
            to_number="5559990001",
            result="Call connected",
            duration_seconds=180,
            start_time=datetime.now(timezone.utc),
            extension="101",
            employee_name="Sarah",
        )
    ]
    audit = pipeline.run(calls=calls, post_to_chat=False)
    assert "employee_reports" in audit
    assert "Sarah" in audit["employee_reports"]


def test_unassigned_orphaned_call_reconciliation():
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    base_time = datetime(2026, 8, 29, 14, 0, 0, tzinfo=timezone.utc)

    calls = [
        # Inbound call on main line with no direct employee / Unassigned
        RingCentralCall(
            call_id="call-unassigned",
            direction="Inbound",
            from_number="5557776666",
            to_number="5559990000",
            result="Voicemail",
            duration_seconds=45,
            start_time=base_time,
            extension="",
            employee_name="Unassigned",
        )
    ]

    audit = auditor.generate_audit(calls, reference_time=base_time + timedelta(hours=1))
    
    # Verify unassigned orphaned call is successfully captured in critical alerts
    assert len(audit["critical_alerts"]) == 1
    assert "Unassigned / General Queue" in audit["critical_alerts"][0]
    assert "(555) 777-6666" in audit["critical_alerts"][0]


def test_split_messages_google_chat_delivery():
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    base_time = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
    ref_time = base_time + timedelta(hours=2)

    calls = [
        RingCentralCall(
            call_id="call-orphaned",
            direction="Inbound",
            from_number="5559876543",
            to_number="5559990002",
            result="Voicemail",
            duration_seconds=30,
            start_time=base_time,
            extension="102",
            employee_name="Jackie",
        )
    ]

    pipeline = ProductivityPipeline(
        auditor=auditor,
        chat_space="spaces/AAQAZbLJO78",
        alert_thread_name="productivity-sla-alerts",
        scorecard_thread_name="productivity-daily-digest",
    )

    with patch("robie_job_engine.productivity_pipeline.post_as_chat_app") as mock_post:
        # Run with split_messages=True
        audit = pipeline.run(calls=calls, post_to_chat=True, split_messages=True)
        assert len(mock_post.call_args_list) == 2

        # First call: Critical Alerts to alert thread
        alert_args, alert_kwargs = mock_post.call_args_list[0]
        assert alert_args[0] == "spaces/AAQAZbLJO78"
        assert "CRITICAL ACCOUNT RISK" in alert_args[1]
        assert "(555) 987-6543" in alert_args[1]
        assert alert_kwargs.get("thread_name") == "productivity-sla-alerts"

        # Second call: Scorecard to daily digest thread (without redundant critical block)
        score_args, score_kwargs = mock_post.call_args_list[1]
        assert score_args[0] == "spaces/AAQAZbLJO78"
        assert "Daily Agency Productivity" in score_args[1]
        assert "CRITICAL ACCOUNT RISK" not in score_args[1]
        assert score_kwargs.get("thread_name") == "productivity-daily-digest"


def test_poll_and_ingest_reports_intake_dir(tmp_path):
    intake = tmp_path / "intake"
    archive = tmp_path / "archive"
    intake.mkdir()
    
    # Write sample tasks CSV
    task_csv = intake / "Task_Aging_Report_20260830.csv"
    task_csv.write_text("Assigned To,Subject,Due Date,Status,Customer Name\nJackie,Renew policy,08/20/2026,Open,John Doe\nJackie,Send ID card,08/30/2026,Completed,Jane Smith\n")

    # Write sample activities CSV
    act_csv = intake / "Activity_Summary_20260830.csv"
    act_csv.write_text("Created By,Activity Type,Action Date,Details\nJackie,Note Added,08/30/2026,Called client\n")

    tasks, acts = EZLynxProductivityParser.poll_and_ingest_reports(intake_dir=intake, archive_dir=archive)
    assert len(tasks) == 1
    assert tasks[0].employee_name == "Jackie"
    assert tasks[0].overdue == 1
    assert tasks[0].completed_today == 1

    assert len(acts) == 1
    assert acts[0].employee_name == "Jackie"
    assert acts[0].notes_count == 1

    # Verify archived
    assert (archive / task_csv.name).exists()
    assert (archive / act_csv.name).exists()


def test_ringcentral_client_from_env_and_polling():
    with patch.dict("os.environ", {
        "RINGCENTRAL_CLIENT_ID": "mock_id",
        "RINGCENTRAL_CLIENT_SECRET": "mock_secret",
        "RINGCENTRAL_JWT": "mock_jwt",
    }):
        rc = RingCentralClient.from_env()
        assert rc.is_configured() is True
        assert rc.client_id == "mock_id"

    with patch.object(RingCentralClient, "fetch_call_logs") as mock_fetch:
        now = datetime(2026, 8, 30, 15, 0, 0, tzinfo=timezone.utc)
        mock_fetch.return_value = [
            RingCentralCall(
                call_id="call-orphaned-1",
                direction="Inbound",
                from_number="5559876543",
                to_number="5559990002",
                result="Voicemail",
                duration_seconds=30,
                start_time=now - timedelta(minutes=45),
                extension="102",
                employee_name="Jackie",
            )
        ]
        rc = RingCentralClient(access_token="mock_token")
        with patch("robie_job_engine.ringcentral_client.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.strptime = datetime.strptime
            
            # Since datetime arithmetic is needed
            orphaned = rc.poll_unreturned_missed_calls(lookback_minutes=60, sla_minutes=30)
            assert len(orphaned) == 1
            assert orphaned[0]["caller_phone"] == "5559876543"
            assert orphaned[0]["status"] == "ORPHANED_ALERT"


