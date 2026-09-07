from robie_job_engine.lost_customer_retention import build_messages, records, validate_monthly_source


HEADERS = [
    "Month", "Applicant ID", "Account Name", "Policy Count", "Lines of Business",
    "Policy Numbers", "Department", "CSR", "Assigned Agent", "Annualized Premium",
    "Account Status Classification", "Evidence-Supported Cause", "Evidence Summary",
    "Responsibility Lane", "Preventable?", "Confidence", "Magellan Match",
    "Magellan Sentiment", "Recovery Opportunity", "Recommended Account Action",
    "Systemic Prevention",
]


def test_unknown_and_department_routing():
    values = [HEADERS, ["June 2026", "1", "A", "1", "Auto", "P1", "Personal", "X", "Y", "$10", "Needs verification", "Cause not found in latest visible activity", "", "", "", "Low", "No matched record", "Not available", "", "Review", ""]]
    items = records(values)
    assert items[0]["Evidence-Supported Cause"] == "Unknown"
    recipients = {"commercial":"sandy@example.com","personal":"ashley@example.com","trucking":"gabby@example.com","carlo":"carlo@example.com","jake":"jake@example.com"}
    messages = build_messages(items, recipients, "run-1")
    assert [m.to for m in messages] == [("sandy@example.com",), ("ashley@example.com",), ("gabby@example.com",), ("carlo@example.com", "jake@example.com")]
    assert "Applicant 1" not in messages[0].body
    assert "Applicant 1" in messages[1].body


def test_duplicate_policy_key_refused():
    header = ["ApplicantID", "Account Name", "Reason", "CSR", "Branch", "Agent", "LOB", "Carrier", "Personal", "Policy", "Effective", "Expiration", "06/01/26"]
    with __import__("pytest").raises(ValueError, match="duplicate"):
        validate_monthly_source([header, ["1","A","","","","","Auto","C","Personal","P","","","06/01/26"], ["1","A","","","","","Auto","C","Personal","P","","","06/01/26"]], "June 2026")

