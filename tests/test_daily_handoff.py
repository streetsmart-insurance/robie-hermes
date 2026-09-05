from datetime import date
from src.reporting.daily_handoff import DailyHandoffReporter

def test_daily_handoff_rolling_pipeline_table():
    ref_date = date(2026, 9, 5)
    active_processed = [
        {
            "applicant_id": "99055770",
            "insured_name": "ABC TRANSPIRATION LLC",
            "policy_number": "02TRM066190-01",
            "carrier_name": "Trinity Underwriters",
            "line_of_business": "Commercial Auto",
            "expiration_date": "2026-10-15",
            "expiring_premium": 8200.0,
            "assigned_csr": "Gabriela C.",
            "status": "Quote Received"
        }
    ]
    rolling_pipeline = [
        {
            "applicant_id": "99055770",
            "insured_name": "ABC TRANSPIRATION LLC",
            "policy_number": "02TRM066190-01",
            "carrier_name": "Trinity Underwriters",
            "line_of_business": "Commercial Auto",
            "expiration_date": "2026-10-15",
            "days_to_exp": 40,
            "expiring_premium": 8200.0,
            "renewal_premium": 8850.0,
            "delta_pct": 7.9,
            "channel": "EMAIL",
            "status": "READY_FOR_AGENT_REVIEW",
            "doc_info": "`02TRM066190-01 Renewal Offer.pdf` (Renewal Offers)",
            "assigned_csr": "Gabriela C.",
            "next_action": "AM to review renewal quote & present to client"
        },
        {
            "applicant_id": "99055888",
            "insured_name": "LE SHAWN SNEED",
            "policy_number": "CUS062900594",
            "carrier_name": "Canal Insurance",
            "line_of_business": "Non-Trucking Liability",
            "expiration_date": "2026-10-20",
            "days_to_exp": 45,
            "expiring_premium": 1450.0,
            "renewal_premium": None,
            "delta_pct": None,
            "channel": "EMAIL",
            "status": "NON_RENEWAL_DECLINED",
            "doc_info": "`CUS062900594 Non Renewal.pdf` (Cancellations)",
            "assigned_csr": "Sandy Y.",
            "next_action": "CRITICAL: Re-market to secondary carriers"
        }
    ]

    report_md = DailyHandoffReporter.generate_report_markdown(
        report_date=ref_date,
        active_processed=active_processed,
        excluded_inactive=[],
        portal_logins_needed=[],
        rolling_pipeline=rolling_pipeline
    )

    # Verify Executive Summary
    assert "Executive Summary" in report_md
    assert "Active Manual Accounts Processed:** 1" in report_md

    # Verify Rolling Pipeline section
    assert "Rolling 50-Day Renewal Pipeline Matrix" in report_md
    assert "https://app.ezlynx.com/web/account/99055770/overview" in report_md
    assert "https://app.ezlynx.com/web/account/99055770/documents" in report_md
    assert "T-40d" in report_md
    assert "$8,200.00" in report_md
    assert "$8,850.00" in report_md
    assert "(+7.9%)" in report_md
    assert "Renewal Offers" in report_md
    assert "Cancellations" in report_md
    assert "Pending Terms" in report_md
    assert "CRITICAL: Re-market to secondary carriers" in report_md
