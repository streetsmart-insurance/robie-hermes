from datetime import date

import pytest
from robie_job_engine.reporting_suite import TaskAgingAuditor, ReportingSuite

def test_task_aging_auditor_detects_postponement():
    auditor = TaskAgingAuditor(age_warning_days=14, snooze_threshold=2)
    sample_tasks = [
        {
            "id": "T1",
            "title": "Update COI for Builder",
            "assigned_user": "Alex Example",
            "age_days": 45,
            "postpone_count": 5,
        },
        {
            "id": "T2",
            "title": "Process Endorsement",
            "assigned_user": "Blair Example",
            "age_days": 3,
            "postpone_count": 0,
        },
        {
            "id": "T3",
            "title": "Renewal Followup",
            "assigned_user": "Casey Example",
            "age_days": 10,
            "postpone_count": 3,
        }
    ]

    incidents = auditor.audit_task_aging(sample_tasks)
    assert len(incidents) == 2
    assert incidents[0].assigned_user == "Alex Example"
    assert incidents[0].is_habitual is True
    assert "45 days" in incidents[0].flag_reason
    assert incidents[1].assigned_user == "Casey Example"
    assert incidents[1].postpone_count == 3


def test_reporting_suite_generation():
    suite = ReportingSuite()
    daily = suite.build_daily_report(
        {"unreturned_calls": [{"name": "Example Caller", "phone": "2025550101", "rep": "Alex", "time": "11:49 AM"}], "rep_stats": {"Alex": {"inbound": 10, "answer_rate": "20.0%", "unreturned": 5}}},
        {"overdue_by_rep": {"Alex": 0}}
    )
    assert "STREETSMART DAILY SERVICE & PHONE WATCHDOG" in daily
    assert "Example Caller" in daily

    weekly = suite.build_weekly_report(
        {"answer_rate": "51.8%", "unreturned_total": 249},
        {"total_overdue": 98},
        {"quotes_created": 42},
        {"reviews_completed": 18},
        {"stalled_inboxes": 3}
    )
    assert "STREETSMART WEEKLY EXECUTIVE PERFORMANCE SCORECARD" in weekly
    assert "Quotes Created: 42" in weekly

    monthly = suite.build_monthly_report(
        {"total_calls": 4500, "policies_serviced": 1200},
        [{"client_name": "Example Cleaning LLC", "policy_type": "BOP", "root_cause": "17m hold + 4-rep handoff confusion"}]
    )
    assert "STREETSMART MONTHLY EXECUTIVE AUDIT & CHURN AUTOPSY" in monthly
    assert "Example Cleaning LLC" in monthly
    assert "UNVERIFIED claim" in monthly
    assert "DATA LIMITATIONS" in monthly


def test_daily_report_displays_explicit_prior_business_date():
    report = ReportingSuite().build_daily_report(
        {"source_status": "available", "unreturned_calls": [], "rep_stats": {}},
        {"source_status": "available", "overdue_by_rep": {}},
        report_date=date(2026, 9, 4),
    )
    assert "Date: Friday, September 04, 2026" in report


def test_weekly_report_never_invents_missing_sources():
    suite = ReportingSuite()
    weekly = suite.build_weekly_report(
        {"source_status": "missing"},
        {"source_status": "missing"},
        {"source_status": "missing"},
        {"source_status": "missing"},
        {"source_status": "missing"},
    )
    assert "51.8%" not in weekly
    assert "249" not in weekly
    assert "UNVERIFIED" in weekly
    assert "DATA LIMITATIONS" in weekly


def test_weekly_report_includes_retention_and_submission_ledgers():
    suite = ReportingSuite()
    weekly = suite.build_weekly_report(
        {"answer_rate": "80.0%", "unreturned_total": 1, "employee_rows": []},
        {"total_overdue": 2},
        {"quotes_created": 3},
        {
            "reviews_completed": 4,
            "exception_count": 1,
            "exceptions": [
                {
                    "account_name": "Example Cleaning LLC",
                    "owner": "Alex Example",
                    "days_to_expiration": 16,
                    "reasons": ["no recorded touch for 29 days"],
                    "source_row_number": 2,
                }
            ],
        },
        {"stalled_threads": 0},
        submission_data={
            "open_over_30_count": 1,
            "exceptions": [
                {
                    "account_name": "Old Open Risk",
                    "owner": "Blair Example",
                    "age_days": 76,
                    "status": "Quoting",
                    "reasons": ["submission open for 76 days"],
                    "source_row_number": 4,
                }
            ],
        },
    )
    assert "RETENTION CENTER EXCEPTIONS" in weekly
    assert "Example Cleaning LLC" in weekly
    assert "SUBMISSION CENTER EXCEPTIONS" in weekly
    assert "Old Open Risk" in weekly

def test_daily_report_never_marks_missing_call_source_resolved():
    suite = ReportingSuite()
    daily = suite.build_daily_report(
        {"source_status": "not supplied"},
        {"source_status": "not supplied"},
    )
    assert "UNVERIFIED — RingCentral evidence is unavailable." in daily
    assert "All client voicemails and missed calls resolved!" not in daily


def test_daily_report_shows_cross_channel_and_metadata_only_email_evidence():
    suite = ReportingSuite()
    daily = suite.build_daily_report(
        {
            "source_status": "available",
            "unreturned_calls": [],
            "service_reconciliation": [{
                "caller_phone_masked": "***-***-0101", "status": "UNRESOLVED", "resolution": "UNRESOLVED",
                "failed_destination": "Service Queue", "assigned_producer": "Producer A", "assigned_csr": "CSR A",
                "resolved_by": None, "evidence_source": "RingCentral", "parent_call_id": "parent-1",
            }],
        },
        {"source_status": "available", "overdue_by_rep": {}},
        email_data={
            "source_status": "available",
            "by_employee": {"csr@example.com": {"awaiting_employee": 2, "stalled_threads": 1, "awaiting_customer": 3}},
        },
    )
    assert "CROSS-CHANNEL SERVICE RESOLUTION" in daily
    assert "EMAIL RESPONSE METADATA (NO MESSAGE CONTENT)" in daily
    assert "csr@example.com" in daily


def test_daily_report_lists_magellan_sad_calls_without_transcript_content():
    suite = ReportingSuite()
    daily = suite.build_daily_report(
        {"source_status": "available", "unreturned_calls": []},
        {"source_status": "available", "overdue_by_rep": {}},
        magellan_data={
            "source_status": "available",
            "sad_calls": [{
                "account_name": "Example Transfer Account",
                "caller_phone_masked": "***-***-2984",
                "occurred_at": "2:39 PM",
                "tags": ["Policy Transfer", "Delay"],
                "answered_by": "Diana / Kyoungnam",
                "assigned_producer": "Ricardo",
                "assigned_csr": "UNVERIFIED",
                "callback_status": "Carrier policy number pending",
                "transcript": "This private transcript must never appear in the digest.",
            }],
        },
    )
    assert "MAGELLAN SAD / AT-RISK CUSTOMER CALLS" in daily
    assert "***-***-2984" in daily
    assert "Policy Transfer, Delay" in daily
    assert "producer: Ricardo" in daily
    assert "private transcript" not in daily
