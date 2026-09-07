"""Tests for Quote PDF text and premium parsing."""

from decimal import Decimal
from pathlib import Path

from src.extractor.quote_parser import (
    QuoteDocumentParser,
    extract_policy_total_premium,
)

# Hyundai Benli HONJ2025100027-26 layout: Coverage A / Annual Premium column
# hits $1,116 first; Policy Total / Total Annual is $1,348.
BENLI_HYUNDAI_DEC_SNIPPET = """
HYUNDAI MARINE & FIRE INSURANCE
HOMEOWNERS DECLARATIONS
Named Insured: Hanim Benli
Policy Number: HONJ2025100027-26
Effective Date: 10/27/2026
Expiration Date: 10/27/2027

COVERAGE SECTION I
Coverages                      Limit of Liability    Annual Premium
Coverage A - Dwelling          $250,000              Annual Premium $1,116.00
Coverage B - Other Structures  $25,000               Annual Premium $112.00
Coverage C - Personal Property $125,000              Annual Premium $80.00
Coverage D - Loss of Use       $50,000               Annual Premium $40.00

Policy Total Premium                                 $1,348.00
Total Annual Premium                                 $1,348.00
"""


def test_quote_parser_text_extraction(tmp_path):
    parser = QuoteDocumentParser()
    sample_txt_pdf = tmp_path / "mock_quote.pdf"
    with open(sample_txt_pdf, "w") as f:
        f.write(
            "COMMERCIAL POLICY RENEWAL PROPOSAL\n"
            "Named Insured: Apex Logistics\n"
            "Policy Number: R2WC771037\n"
            "Effective Date: 10/15/2026\n"
            "Expiration Date: 10/15/2027\n"
            "Total Renewal Premium: $7,450.00\n"
        )

    res = parser.parse_pdf(sample_txt_pdf)
    assert res.renewal_premium == 7450.00
    assert str(res.effective_date) == "2026-10-15"
    assert str(res.expiration_date) == "2027-10-15"
    assert res.policy_number == "R2WC771037"


def test_benli_hyundai_prefers_policy_total_over_coverage_a():
    """Coverage A $1,116 must not beat Policy Total $1,348."""
    assert extract_policy_total_premium(BENLI_HYUNDAI_DEC_SNIPPET) == Decimal("1348.00")


def test_benli_hyundai_parse_pdf_returns_policy_total(tmp_path: Path):
    path = tmp_path / "benli_hyundai_dec.txt"
    path.write_text(BENLI_HYUNDAI_DEC_SNIPPET)
    parsed = QuoteDocumentParser().parse_pdf(path)
    assert parsed.renewal_premium == 1348.00
    assert parsed.renewal_premium != 1116.00


def test_extract_prefers_largest_labeled_total_over_first_dollar():
    text = (
        "Coverage A Dwelling Annual Premium $1,116.00\n"
        "Amount Due (renewal) $337.00\n"
        "Grand Total $1,348.00\n"
        "Policy Total Premium $1,348.00\n"
    )
    assert extract_policy_total_premium(text) == Decimal("1348.00")


def test_extract_wc_and_gl_policy_total_not_class_line():
    wc = (
        "WORKERS COMPENSATION RENEWAL OFFER\n"
        "Class 8810 Clerical Annual Premium $450.00\n"
        "Policy Total Premium $2,200.00\n"
    )
    gl = (
        "COMMERCIAL GENERAL LIABILITY DECLARATIONS\n"
        "Coverage A Bodily Injury Annual Premium $800.00\n"
        "Total Premium $1,950.00\n"
    )
    assert extract_policy_total_premium(wc) == Decimal("2200.00")
    assert extract_policy_total_premium(gl) == Decimal("1950.00")


def test_extract_returns_none_when_no_labeled_premium():
    assert extract_policy_total_premium("no money fields here") is None
    assert extract_policy_total_premium("") is None
