from datetime import datetime

from robie_job_engine.lost_customer_retention import (
    build_messages, conservative_sop_audit, employee_directory, records,
    resolve_department, tenure_band, tenure_months, validate_monthly_source,
)


HEADERS = [
    "Month", "Applicant ID", "Account Name", "Policy Count", "Lines of Business",
    "Policy Numbers", "Department", "CSR", "Assigned Agent", "Annualized Premium",
    "Account Status Classification", "Evidence-Supported Cause", "Evidence Summary",
    "Responsibility Lane", "Preventable?", "Confidence", "Magellan Match",
    "Magellan Sentiment", "Recovery Opportunity", "Recommended Account Action",
    "Systemic Prevention",
]


def test_unknown_and_department_routing():
    values = [HEADERS, ["June 2026", "1", "A", "1", "Auto (Personal)", "P1", "Personal Lines", "X", "Y", "$10", "Needs verification", "Cause not found in latest visible activity", "", "", "", "Low", "No matched record", "Not available", "", "Review", ""]]
    items = records(values)
    assert items[0]["Evidence-Supported Cause"] == "Unknown"
    recipients = {"commercial":"sandy@example.com","personal":"ashley@example.com","trucking":"gabby@example.com","carlo":"carlo@example.com","jake":"jake@example.com"}
    messages = build_messages(items, recipients, "run-1")
    assert [m.to for m in messages] == [("ashley@example.com",), ("sandy@example.com",), ("gabby@example.com",), ("carlo@example.com", "jake@example.com")]
    assert "Applicant 1" in messages[0].body
    assert "Applicant 1" not in messages[1].body


def test_appsheet_department_mapping_never_folds_trucking_into_commercial():
    directory = employee_directory([
        ["Name", "Email", "Department", "Employment Status", "Position"],
        ["Gabriela Chutin", "gabrielac@example.com", "Trucking and Transportation", "Active", "Manager"],
        ["Carlo Ferrara", "carlo@example.com", "Executive Team", "Active", "Executive"],
        ["Diana Cabrera", "diana@example.com", "Trucking and Transportation", "Active", "Technician"],
    ])
    department, source, exception = resolve_department(
        {"Assigned Agent": "Carlo Ferrara", "CSR": "Cabrera, Diana", "Lines of Business": "Commercial Auto"},
        directory,
    )
    assert department == "Trucking and Transportation"
    assert source == "AppSheet Employees: Diana Cabrera"
    assert exception is False


def test_unmatched_employee_uses_flagged_lob_fallback():
    department, source, exception = resolve_department(
        {"Assigned Agent": "Unknown", "CSR": "Unknown", "Lines of Business": "Truckers General Liability"},
        {},
    )
    assert department == "Trucking and Transportation"
    assert "fallback" in source
    assert exception is True


def test_tenure_bands_and_sop_are_conservative():
    assert tenure_months(datetime(2026, 9, 1), datetime(2025, 9, 2)) == 11
    assert tenure_band(11) == "<1 year"
    assert tenure_band(12) == "1–3 years"
    assert tenure_band(36) == "3–5 years"
    assert tenure_band(60) == "5+ years"
    audit = conservative_sop_audit({
        "Month": "June 2026", "Applicant ID": "1",
        "Evidence Summary": "Allstate agent supplied the signed cancellation request.",
        "Evidence-Supported Cause": "Moved",
    })
    assert audit["Written Authorization"] == "Followed"
    assert audit["SPLICE Call"] == "Cannot Verify"
    assert audit["Overall SOP Result"] == "Partially Followed"


def test_duplicate_policy_key_refused():
    header = ["ApplicantID", "Account Name", "Reason", "CSR", "Branch", "Agent", "LOB", "Carrier", "Personal", "Policy", "Effective", "Expiration", "06/01/26"]
    with __import__("pytest").raises(ValueError, match="duplicate"):
        validate_monthly_source([header, ["1","A","","","","","Auto","C","Personal","P","","","06/01/26"], ["1","A","","","","","Auto","C","Personal","P","","","06/01/26"]], "June 2026")
