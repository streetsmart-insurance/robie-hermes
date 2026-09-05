"""Tests for Quote PDF text and premium parsing."""

from pathlib import Path
from src.extractor.quote_parser import QuoteDocumentParser

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
