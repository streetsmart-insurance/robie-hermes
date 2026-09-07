from datetime import datetime, timezone

from robie_job_engine.center_audits import (
    audit_sales_records,
    audit_retention_records,
    audit_submission_records,
    audit_overdue_submission_records,
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
100,Example Cleaning Services,CSR One,09/15/2026,08/01/2026,Left voicemail,Active
200,Well Serviced LLC,CSR Two,10/15/2026,08/29/2026,Client confirmed renewal option and will sign proposal by 09/02,Active
"""
    )

    findings = audit_retention_records(records, as_of=AS_OF)

    assert len(findings) == 1
    assert findings[0].account_id == "100"
    assert findings[0].owner == "CSR One"
    assert findings[0].days_to_expiration == 16
    assert findings[0].days_since_touch == 29
    assert findings[0].severity == "high"
    assert "generic activity" in " ".join(findings[0].note_quality_reasons)


def test_retention_audit_ignores_accounts_outside_90_day_horizon():
    records = parse_retention_csv(
        """Account ID,Account Name,Owner,Expiration Date,Last Activity,Last Note,Status
300,Future Account,CSR One,01/15/2027,01/01/2026,Called client,Active
"""
    )
    assert audit_retention_records(records, as_of=AS_OF) == []


def test_submission_audit_only_flags_open_items_over_30_days():
    records = parse_submission_csv(
        """Submission ID,Account Name,Owner,Carrier,Created Date,Last Activity,Last Note,Status
S-1,Old Open Risk,Producer One,Carrier A,06/15/2026,07/01/2026,Follow up,Quoting
S-2,Recent Risk,Producer Two,Carrier B,08/15/2026,08/29/2026,Submitted complete application to Carrier B; follow up by 09/03,Submitted
S-3,Old Bound Risk,Producer One,Carrier C,06/01/2026,06/20/2026,Bound and handed off,Bound
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
        "O-1,Inactive Risk,Producer One,Quoting,2026-07-01,2026-08-20,Followed up\n"
        "O-2,Active Risk,Producer Two,Proposed,2026-08-20,2026-08-28,Proposal sent; call again 09/01\n"
        "O-3,Bound Risk,Producer Two,Bound,2026-07-01,2026-07-10,Bound\n"
    )
    findings = audit_sales_records(records, as_of=AS_OF, untouched_days=5)
    assert len(findings) == 1
    assert findings[0].opportunity_id == "O-1"
    assert findings[0].producer == "Producer One"
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


def test_weekly_submission_rule_requires_red_and_day_31_and_exact_closed_statuses():
    records = parse_submission_csv(
        "Submission ID,Applicant,Assigned Producer,Status,Quote Due Date,Effective Date,Overdue,Submission URL\n"
        "S1,Example One,Producer One,Quoted,2026-07-30,2026-09-01,red,https://example.test/s/1\n"
        "S2,Example Two,Producer One,Quoted,2026-07-31,2026-09-01,red,https://example.test/s/2\n"
        "S3,Example Three,Producer Two,Closed - Bound,2026-06-01,2026-09-01,red,https://example.test/s/3\n"
    )
    findings = audit_overdue_submission_records(records, as_of=AS_OF)
    assert [item.submission_id for item in findings] == ["S1"]
    assert findings[0].overdue_days == 31


def test_weekly_submission_rule_deduplicates_url_and_fails_closed():
    records = parse_submission_csv(
        "Submission ID,Applicant,Assigned Producer,Status,Quote Due Date,Effective Date,Overdue,Submission URL\n"
        "S1,Example One,Producer One,Quoted,2026-06-01,2026-09-01,red,https://example.test/s/1\n"
        "S1-copy,Example One,Producer One,Quoted,2026-06-01,2026-09-01,red,https://example.test/s/1\n"
    )
    assert len(audit_overdue_submission_records(records, as_of=AS_OF)) == 1
    incomplete = parse_submission_csv(
        "Submission ID,Applicant,Assigned Producer,Status,Quote Due Date,Effective Date,Submission URL\n"
        "S2,Example Two,Producer Two,Quoted,2026-06-01,2026-09-01,https://example.test/s/2\n"
    )
    import pytest
    with pytest.raises(ValueError, match="red/overdue marker"):
        audit_overdue_submission_records(incomplete, as_of=AS_OF)
