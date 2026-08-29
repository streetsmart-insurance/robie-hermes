"""Tests for StreetSmart Productivity & Accountability Engine."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

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


def test_pipeline_execution():
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
