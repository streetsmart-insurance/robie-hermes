import pytest
from robie_job_engine.reporting_suite import TaskAgingAuditor, ReportingSuite

def test_task_aging_auditor_detects_postponement():
    auditor = TaskAgingAuditor(age_warning_days=14, snooze_threshold=2)
    sample_tasks = [
        {
            "id": "T1",
            "title": "Update COI for Builder",
            "assigned_user": "Jackie Arriola",
            "age_days": 45,
            "postpone_count": 5,
        },
        {
            "id": "T2",
            "title": "Process Endorsement",
            "assigned_user": "Erika Palacios",
            "age_days": 3,
            "postpone_count": 0,
        },
        {
            "id": "T3",
            "title": "Renewal Followup",
            "assigned_user": "Maria Bara",
            "age_days": 10,
            "postpone_count": 3,
        }
    ]

    incidents = auditor.audit_task_aging(sample_tasks)
    assert len(incidents) == 2
    assert incidents[0].assigned_user == "Jackie Arriola"
    assert incidents[0].is_habitual is True
    assert "45 days" in incidents[0].flag_reason
    assert incidents[1].assigned_user == "Maria Bara"
    assert incidents[1].postpone_count == 3


def test_reporting_suite_generation():
    suite = ReportingSuite()
    daily = suite.build_daily_report(
        {"unreturned_calls": [{"name": "Piotr Gusciora", "phone": "9736528939", "rep": "Jackie", "time": "11:49 AM"}], "rep_stats": {"Jackie": {"inbound": 10, "answer_rate": "20.0%", "unreturned": 5}}},
        {"overdue_by_rep": {"Jackie": 0}}
    )
    assert "STREETSMART DAILY SERVICE & PHONE WATCHDOG" in daily
    assert "Piotr Gusciora" in daily

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
        [{"client_name": "Costa 1 Cleaning", "policy_type": "BOP", "root_cause": "17m hold + 4-rep handoff confusion"}]
    )
    assert "STREETSMART MONTHLY EXECUTIVE AUDIT & CHURN AUTOPSY" in monthly
    assert "Costa 1 Cleaning" in monthly
