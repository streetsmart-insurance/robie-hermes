from datetime import datetime, timezone

from robie_job_engine.center_audits import (
    audit_sales_records,
    audit_retention_records,
    audit_submission_records,
    enrich_sales_last_touches,
    parse_retention_csv,
    parse_sales_csv,
    parse_submission_csv,
    vague_note_reasons,
)


AS_OF = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)


def test_retention_audit_flags_untouched_account_and_vague_note():
    records = parse_retention_csv(
        """Account ID,Account Name,Account Manager,Expiration Date,Last Activity Date,Last Note,Status
132780296,Costa 1 Cleaning Services,Jackie Arriola,09/15/2026,08/01/2026,Left voicemail,Active
200,Well Serviced LLC,Erika Palacios,10/15/2026,08/29/2026,Client confirmed renewal option and will sign proposal by 09/02,Active
"""
    )

    findings = audit_retention_records(records, as_of=AS_OF)

    assert len(findings) == 1
    assert findings[0].account_id == "132780296"
    assert findings[0].owner == "Jackie Arriola"
    assert findings[0].days_to_expiration == 16
    assert findings[0].days_since_touch == 29
    assert findings[0].severity == "high"
    assert "generic activity" in " ".join(findings[0].note_quality_reasons)


def test_retention_audit_ignores_accounts_outside_90_day_horizon():
    records = parse_retention_csv(
        """Account ID,Account Name,Owner,Expiration Date,Last Activity,Last Note,Status
300,Future Account,Jackie Arriola,01/15/2027,01/01/2026,Called client,Active
"""
    )
    assert audit_retention_records(records, as_of=AS_OF) == []


def test_submission_audit_only_flags_open_items_over_30_days():
    records = parse_submission_csv(
        """Submission ID,Account Name,Owner,Carrier,Created Date,Last Activity,Last Note,Status
S-1,Old Open Risk,Alexis Martinez,Carrier A,06/15/2026,07/01/2026,Follow up,Quoting
S-2,Recent Risk,Nelson Maldonado,Carrier B,08/15/2026,08/29/2026,Submitted complete application to Carrier B; follow up by 09/03,Submitted
S-3,Old Bound Risk,Alexis Martinez,Carrier C,06/01/2026,06/20/2026,Bound and handed off,Bound
"""
    )

    findings = audit_submission_records(records, as_of=AS_OF)

    assert len(findings) == 1
    assert findings[0].submission_id == "S-1"
    assert findings[0].age_days == 76
    assert findings[0].severity == "high"
    assert "submission open for 76 days" in findings[0].reasons


def test_sales_audit_flags_open_producer_opportunity_without_recent_touch():
    records = parse_sales_csv(
        "Opportunity ID,Account Name,Producer,Stage,Created Date,Last Activity,Last Note\n"
        "O-1,Inactive Risk,Alexis Martinez,Quoting,2026-07-01,2026-08-20,Followed up\n"
        "O-2,Active Risk,Nelson Maldonado,Proposed,2026-08-20,2026-08-28,Proposal sent; call again 09/01\n"
        "O-3,Bound Risk,Nelson Maldonado,Bound,2026-07-01,2026-07-10,Bound\n"
    )
    findings = audit_sales_records(records, as_of=AS_OF, untouched_days=5)
    assert len(findings) == 1
    assert findings[0].opportunity_id == "O-1"
    assert findings[0].producer == "Alexis Martinez"
    assert findings[0].days_since_touch == 10
    assert "threshold 5" in findings[0].reasons[0]


def test_sales_center_assigned_producer_and_lead_source_are_authoritative():
    records = parse_sales_csv(
        "Applicant ID,Account Name,Assigned Producer,Producer,Lead Channel,Department,Opportunity Status,Opportunity Created Date\n"
        "A-1,Example Risk,Sales Owner,Account Producer,Referral,Personal Lines,Quoting,08/01/2026\n"
    )
    assert records[0].producer == "Sales Owner"
    assert records[0].lead_source == "Referral"
    assert records[0].department == "Personal Lines"

    enriched = enrich_sales_last_touches(
        records,
        "Applicant ID,Created Date,Note\nA-1,08/29/2026 4:00 PM,Producer called client\n",
    )
    assert enriched[0].last_activity_at.date().isoformat() == "2026-08-29"
    assert "Applicant ID" in enriched[0].last_touch_evidence


def test_vague_note_is_a_signal_not_a_keyword_only_rule():
    assert vague_note_reasons("Called client")
    assert vague_note_reasons("Left voicemail")
    assert vague_note_reasons("Client selected option 2; producer will bind by 09/01") == []
