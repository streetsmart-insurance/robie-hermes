from datetime import date

from robie_job_engine.operational_trackers import TRACKER_DEFINITIONS, audit_tracker_csv


def test_live_coi_tracker_headers_map_to_owner_account_date_notes_and_link():
    csv_text = """Date,Date COI was Requested,Profile,Link,Requirements/Endo,Agent assigned to the task.,Status,Date Completed
08/25/2026,08/25/2026,Example LLC,https://app.ezlynx.com/web/account/123/activity,Awaiting endorsement,Example CSR,Pending,
"""
    findings = audit_tracker_csv(
        csv_text,
        TRACKER_DEFINITIONS["coi_endorsements"],
        as_of=date(2026, 8, 31),
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.account == "Example LLC"
    assert finding.owner == "Example CSR"
    assert finding.age_days == 6
    assert finding.ezlynx_reference.endswith("/123/activity")
