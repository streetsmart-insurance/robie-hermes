from datetime import date

from robie_job_engine.operational_trackers import (
    TRACKER_DEFINITIONS,
    audit_tracker_csv,
    voicemail_email_attestations,
)


def test_bor_tracker_preserves_owner_status_and_source_row():
    findings = audit_tracker_csv(
        """Ezlynx URL/Client Name,Company/Carrier,Date Submitted,Effective Date,Status,Additinal notes,Department,Owner
The Gutter Elite LLC,Geico,07/01/2026,08/01/2026,Pending Download,Monitoring download,Commercial,Taylor Cimei
Completed LLC,Geico,08/20/2026,08/25/2026,Completed,Policy downloaded,Commercial,Erika Palacios
""",
        TRACKER_DEFINITIONS["bor"],
        as_of=date(2026, 8, 30),
    )
    assert len(findings) == 1
    assert findings[0].account == "The Gutter Elite LLC"
    assert findings[0].opened_date == "2026-07-01"
    assert findings[0].owner == "Taylor Cimei"
    assert findings[0].source_row_number == 2
    assert "open for 60 days" in " ".join(findings[0].reasons)


def test_pending_payout_flags_missing_release_fields():
    findings = audit_tracker_csv(
        """Applicant,Policy No.,Invoice No.,Payable Due Date,Status,Owner,Notes
Costa 1 Cleaning,,INV-1,08/20/2026,Pending,Sandy Santana,Waiting
""",
        TRACKER_DEFINITIONS["pending_payouts"],
        as_of=date(2026, 8, 30),
    )
    assert len(findings) == 1
    assert "missing required field: Policy No." in findings[0].reasons
    assert findings[0].severity == "high"


def test_weekly_matrix_uses_latest_dated_count():
    findings = audit_tracker_csv(
        """Names (39) Members,Email,Initial Count,08/28/2026
Jackie Arriola,jackie@streetsmart.insurance,0,8
""",
        TRACKER_DEFINITIONS["voicemail_email"],
        as_of=date(2026, 8, 30),
    )
    assert len(findings) == 1
    assert findings[0].owner == "Jackie Arriola"
    assert findings[0].age_days == 2
    assert findings[0].severity == "medium"
    assert "unresolved count is 8" in " ".join(findings[0].reasons)


def test_weekly_matrix_does_not_flag_latest_zero():
    findings = audit_tracker_csv(
        """Names (39) Members,Email,Initial Count,08/21/2026,08/28/2026
Erika Palacios,erika@streetsmart.insurance,2,4,0
""",
        TRACKER_DEFINITIONS["voicemail_email"],
        as_of=date(2026, 8, 30),
    )
    assert findings == []


def test_policy_change_over_seven_days_classifies_carrier_and_assigns_owner():
    findings = audit_tracker_csv(
        """Account Name,Request Date,Status,Notes,CSR,EZLynx Account ID
Example LLC,08/20/2026,Pending Carrier,Underwriter reviewing request,Example CSR,A-123
""",
        TRACKER_DEFINITIONS["policy_changes"],
        as_of=date(2026, 8, 30),
    )
    assert len(findings) == 1
    assert findings[0].blocker_party == "carrier"
    assert findings[0].opened_date == "2026-08-20"
    assert findings[0].notification_target == "Example CSR"
    assert findings[0].ezlynx_reference == "A-123"
    assert "carrier response" in findings[0].next_action


def test_tracker_attestation_preserves_zero_for_authoritative_comparison():
    rows = voicemail_email_attestations(
        """Names (39) Members,Email,08/28/2026
Jackie Arriola,jackie@streetsmart.insurance,0
"""
    )
    assert rows[0]["reported_unresolved"] == 0
    assert rows[0]["email"] == "jackie@streetsmart.insurance"
