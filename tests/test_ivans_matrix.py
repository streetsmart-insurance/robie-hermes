"""Unit tests for the IVANS Download Matrix Matcher."""

import pytest
from src.ivans.matrix_matcher import ivans_matcher
from src.intake.report_ingestor import ReportIngestor

def test_ivans_matrix_hartford_lob_separation():
    """Hartford Workers Comp downloads via IVANS, but Commercial Inland Marine does not."""
    # 13WECAN7A7K - B&M All Inclusive LLC
    assert ivans_matcher.is_ivans_downloading("The Hartford", "Workers comp") is True
    # 13 MS BL8665 - Maier Solar LLC DBA Solar Me
    assert ivans_matcher.is_ivans_downloading("The Hartford", "Inland marine (comm)") is False

def test_ivans_matrix_progressive_downloads():
    """Progressive Auto and BOP download via IVANS."""
    assert ivans_matcher.is_ivans_downloading("Progressive Insurance", "Auto (Commercial)") is True
    assert ivans_matcher.is_ivans_downloading("Progressive Insurance", "Business Owners Policy") is True

def test_ivans_matrix_true_manual_markets():
    """MGAs, E&S, and Assigned Risk are not automated downloads."""
    assert ivans_matcher.is_ivans_downloading("AmWINS MGA", "General Liability") is False
    assert ivans_matcher.is_ivans_downloading("Trinity Underwriters", "Commercial Auto") is False
    assert ivans_matcher.is_ivans_downloading("NJCRIB - Hartford Assigned Risk", "Workers comp") is False
    assert ivans_matcher.is_ivans_downloading("CNA", "Cyber Liability") is False
    assert ivans_matcher.is_ivans_downloading("TAPCO Underwriters Inc.", "Commercial Pkg") is False

def test_report_ingestor_with_ivans_filtering():
    """Verify report ingestor applies IVANS filtering on Looker data."""
    ingestor = ReportIngestor()
    renewals = ingestor.parse_csv_file("data/downloads/inbox_attachments/1a061a18aa00d525_Manual_Renewal_Queue_-_ROBIE_2026-09-02T0605.csv")
    
    # Check that Maier Solar Inland Marine is preserved
    maier_solar = [r for r in renewals if "maier solar" in r.insured_name.lower() and "inland marine" in r.line_of_business.lower()]
    assert len(maier_solar) == 1
    assert maier_solar[0].policy_number == "13 MS BL8665"
    assert maier_solar[0].carrier_name == "The Hartford"

    # Check that Hartford WC (13WECAN7A7K) was excluded
    hartford_wc = [r for r in renewals if r.policy_number == "13WECAN7A7K"]
    assert len(hartford_wc) == 0

    # Check that Progressive Commercial Auto (02796124) was excluded
    prog_auto = [r for r in renewals if r.policy_number == "02796124"]
    assert len(prog_auto) == 0
