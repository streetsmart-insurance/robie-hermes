import json
from datetime import datetime
from pathlib import Path

from robie_job_engine.lost_customer_retention import (
    NO_MAGELLAN_MATCH, NO_MAGELLAN_SENTIMENT, REQUIRED_REVIEW_HEADERS,
    account_phones_by_applicant, build_messages, conservative_sop_audit,
    employee_directory, enrich_review_with_magellan, executive_email,
    load_magellan_source, magellan_cell_updates, magellan_write_back_enabled,
    records, resolve_department, review_sheet_rows, summarize, tenure_band,
    tenure_months, validate_monthly_source,
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


FIXTURE = Path(__file__).parent / "fixtures" / "magellan" / "phone_watchdog_calls.json"
SAD_SENTIMENT = (
    "SAD / at-risk / frustration; Sad; "
    "evidence: Cancellation, frustrated about premium (mag-100); 2026-06-12"
)


def _row(**overrides):
    item = {header: "" for header in HEADERS}
    item.update({
        "Month": "June 2026",
        "Applicant ID": "42",
        "Account Name": "Pat Example",
        "Policy Count": "1",
        "Department": "Personal Lines",
        "Evidence-Supported Cause": "Moved",
        "Magellan Match": "sheet match",
        "Magellan Sentiment": "sheet sentiment",
    })
    item.update(overrides)
    return item


def _load(path, **extra):
    config = {"enabled": True, "snapshot_path": str(path), "snapshot_dir": ""}
    config.update(extra)
    return load_magellan_source(config)


def test_phone_watchdog_fixture_matches_by_phone_without_inventing_direction():
    source = _load(FIXTURE)
    assert source.status == "available"
    assert source.sad_scoped is False
    assert source.summary()["calls_with_direction"] == 0
    item = _row(Phone="908-555-0142")
    summary = enrich_review_with_magellan([item], source)
    assert item["Magellan Match"] == "Matched by phone (1 call)"
    assert item["Magellan Sentiment"] == SAD_SENTIMENT
    assert "customer called" not in item["Magellan Sentiment"]
    assert "agency called" not in item["Magellan Sentiment"]
    assert item["Evidence-Supported Cause"] == "Moved"
    assert summary["rows"][0]["method"] == "phone"
    assert summarize([item])["groups"][0]["magellan_matches"] == 1


def test_phone_match_wins_over_a_different_account_name():
    source = _load(FIXTURE)
    item = _row(Phone="7325550199")
    enrich_review_with_magellan([item], source)
    assert item["Magellan Match"] == "Matched by phone (1 call)"
    assert item["Magellan Sentiment"] == "Neutral; 2026-06-02"
    assert "SAD" not in item["Magellan Sentiment"]


def test_name_fallback_requires_unique_two_word_name_and_no_phone():
    source = _load(FIXTURE)
    named = _row(**{"Account Name": "Example, Pat"})
    enrich_review_with_magellan([named], source)
    assert named["Magellan Match"] == "Matched by account name (1 call)"
    assert named["Magellan Sentiment"] == SAD_SENTIMENT

    ambiguous = [_row(**{"Applicant ID": "1"}), _row(**{"Applicant ID": "2"})]
    enrich_review_with_magellan(ambiguous, source)
    assert all(item["Magellan Match"] == NO_MAGELLAN_MATCH for item in ambiguous)

    single = _row(**{"Account Name": "Pat"})
    enrich_review_with_magellan([single], source)
    assert single["Magellan Match"] == NO_MAGELLAN_MATCH


def test_phone_on_file_blocks_name_fallback_when_it_does_not_match():
    source = _load(FIXTURE)
    item = _row(Phone="2015550100")
    enrich_review_with_magellan([item], source)
    assert item["Magellan Match"] == NO_MAGELLAN_MATCH
    assert item["Magellan Sentiment"] == NO_MAGELLAN_SENTIMENT


def test_uncovered_month_keeps_sheet_magellan_text():
    source = _load(FIXTURE)
    item = _row(Month="July 2026", Phone="9085550142")
    enrich_review_with_magellan([item], source)
    assert item["Magellan Match"] == "sheet match"
    assert item["Magellan Sentiment"] == "sheet sentiment"


def test_monthly_tab_phone_is_used_and_agency_did_is_not():
    phones = account_phones_by_applicant([[
        ["Applicant ID", "Account Name", "Phone - Cell", "Policy"],
        ["42", "Pat Example", "(908) 555-0142", "P1"],
        ["7", "Agency", "732-462-8343", "P2"],
    ]])
    assert phones == {"42": {"9085550142"}}
    source = _load(FIXTURE)
    item = _row()
    enrich_review_with_magellan([item], source, phones)
    assert item["Magellan Match"] == "Matched by phone (1 call)"
    assert "SAD / at-risk / frustration" in item["Magellan Sentiment"]


def test_shared_phone_does_not_match_either_account():
    source = _load(FIXTURE)
    first = _row(**{"Applicant ID": "1"}, Phone="9085550142")
    second = _row(**{"Applicant ID": "2", "Account Name": "Other Person"}, Phone="9085550142")
    enrich_review_with_magellan([first, second], source)
    assert first["Magellan Sentiment"] == NO_MAGELLAN_SENTIMENT
    assert second["Magellan Sentiment"] == NO_MAGELLAN_SENTIMENT


def test_accountability_snapshot_direction_and_applicant_id(tmp_path):
    payload = {
        "source_status": "available",
        "source": "Magellan authenticated dashboard",
        "target_date": "2026-06-04",
        "records": [
            {
                "call_id": "m-in",
                "occurred_at": "2026-06-03T15:00:00",
                "from_number": "9085550142",
                "to_number": "7324628343",
                "client_phone": "9085550142",
                "client_name": "Pat Example",
                "sentiment": "Sad",
                "tags": ["At-Risk"],
            },
            {
                "call_id": "m-out",
                "occurred_at": "2026-06-04T11:00:00",
                "from_number": "7324628343",
                "to_number": "9085550142",
                "client_phone": "9085550142",
                "client_name": "Pat Example",
                "sentiment": "Neutral",
                "tags": [],
            },
        ],
    }
    path = tmp_path / "magellan-2026-06-04-direction.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    source = _load(path)
    assert source.sad_scoped is True
    assert source.summary()["calls_with_direction"] == 2
    item = _row(Phone="9085550142")
    enrich_review_with_magellan([item], source)
    assert item["Magellan Match"] == "Matched by phone (2 calls)"
    assert item["Magellan Sentiment"] == (
        "customer called and agency called; SAD / at-risk; Sad; evidence: At-Risk (m-in); 2026-06-03"
    )

    applicant_only = {
        "source_status": "available",
        "records": [{
            "call_id": "m-app",
            "occurred_at": "2026-06-08T08:00:00",
            "applicant_id": "42",
            "customer_name": "Different Human",
            "sentiment_label": "Frustrated",
            "key_findings": [],
        }],
    }
    app_path = tmp_path / "applicant.json"
    app_path.write_text(json.dumps(applicant_only), encoding="utf-8")
    applicant_source = _load(app_path)
    applicant_row = _row(**{"Account Name": "Pat Example"})
    enrich_review_with_magellan([applicant_row], applicant_source)
    assert applicant_row["Magellan Match"] == "Matched by applicant ID (1 call)"
    assert applicant_row["Magellan Sentiment"].startswith("frustration; Frustrated;")
    assert "customer called" not in applicant_row["Magellan Sentiment"]


def test_negative_score_uses_phone_watchdog_at_risk_rule_without_direction(tmp_path):
    payload = [{
        "call_id": "mag-score",
        "timestamp": "2026-06-01T10:00:00",
        "phone_number": "9085550142",
        "customer_name": "Pat Example",
        "csr_name": "Ashley",
        "department": "Personal Lines",
        "sentiment_score": -0.2,
        "sentiment_label": "Neutral",
        "key_findings": [],
        "coaching_notes": None,
    }]
    path = tmp_path / "phone_watchdog_score.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    source = _load(path)
    item = _row(Phone="9085550142")
    enrich_review_with_magellan([item], source)
    assert item["Magellan Sentiment"] == "SAD / at-risk; Neutral; 2026-06-01"
    assert "customer called" not in item["Magellan Sentiment"]


def test_disabled_or_unverified_magellan_does_not_refresh_the_sheet_text(tmp_path):
    item = _row()
    enrich_review_with_magellan([item], load_magellan_source({"enabled": False}))
    assert item["Magellan Match"] == "sheet match"
    enrich_review_with_magellan([item], load_magellan_source(None))
    assert item["Magellan Sentiment"] == "sheet sentiment"

    missing = _load(tmp_path / "missing.json")
    assert missing.status == "unavailable"
    enrich_review_with_magellan([item], missing)
    assert item["Magellan Match"] == "sheet match"

    unverified = tmp_path / "magellan-2026-06-04-bad.json"
    unverified.write_text(json.dumps({
        "source_status": "UNVERIFIED",
        "records": [{"call_id": "x", "phone_number": "9085550142", "sentiment_label": "Sad", "timestamp": "2026-06-04T00:00:00"}],
    }), encoding="utf-8")
    blocked = load_magellan_source({"enabled": True, "snapshot_path": "", "snapshot_dir": str(tmp_path)})
    assert blocked.status == "unavailable"
    enrich_review_with_magellan([item], blocked)
    assert item["Magellan Sentiment"] == "sheet sentiment"

    incomplete = tmp_path / "cache.json"
    incomplete.write_text(json.dumps({
        "source_status": "available",
        "pages_complete": False,
        "target_date": "2026-06-04",
        "calls": [],
    }), encoding="utf-8")
    assert _load(incomplete).status == "unavailable"


def test_verified_empty_day_clears_only_the_covered_month(tmp_path):
    path = tmp_path / "magellan-2026-06-04-run.json"
    path.write_text(json.dumps({
        "source_status": "available",
        "target_date": "2026-06-04",
        "records": [],
    }), encoding="utf-8")
    source = _load(path)
    assert source.status == "available"
    assert source.summary()["covered_months"] == ["2026-06"]
    june = _row()
    july = _row(Month="July 2026")
    enrich_review_with_magellan([june, july], source)
    assert june["Magellan Match"] == NO_MAGELLAN_MATCH
    assert june["Magellan Sentiment"] == NO_MAGELLAN_SENTIMENT
    assert july["Magellan Match"] == "sheet match"


def test_magellan_columns_stay_on_the_approved_header_contract():
    assert REQUIRED_REVIEW_HEADERS[16:18] == ("Magellan Match", "Magellan Sentiment")
    values = [HEADERS, ["June 2026", "42", "Pat Example", "1", "", "", "Personal Lines", "", "", "", "", "Cause not found", "", "", "", "", "", "", "", "", ""]]
    row_number, item = review_sheet_rows(values)[0]
    assert row_number == 2
    assert item["Evidence-Supported Cause"] == "Unknown"
    updates = magellan_cell_updates([(row_number, {"Magellan Match": "Matched by phone (1 call)", "Magellan Sentiment": "Sad"})])
    assert updates[0]["range"] == "'3-Month Account Review'!Q2"
    assert updates[0]["values"] == [["Matched by phone (1 call)"]]
    assert updates[1]["range"] == "'3-Month Account Review'!R2"
    assert "Magellan is corroborating evidence only when an account match exists." in executive_email([item], run_id="run-1")


def test_example_config_leaves_magellan_disabled():
    config = json.loads((
        Path(__file__).resolve().parents[1] / "deploy/accountability/lost-customer-retention-config.example.json"
    ).read_text(encoding="utf-8"))
    assert config["magellan"]["enabled"] is False
    assert config["magellan"]["write_back"] is True
    assert load_magellan_source(config["magellan"]).status == "disabled"
    assert magellan_write_back_enabled(config, dry_run=True, status="available") is False
    assert magellan_write_back_enabled(config, dry_run=False, status="available") is False
    enabled = {"magellan": {"enabled": True, "write_back": True}}
    assert magellan_write_back_enabled(enabled, dry_run=False, status="available") is True
    assert magellan_write_back_enabled(enabled, dry_run=False, status="unavailable") is False
    assert magellan_write_back_enabled(
        {"magellan": {"enabled": True, "write_back": False}}, dry_run=False, status="available",
    ) is False
