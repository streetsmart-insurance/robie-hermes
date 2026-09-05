"""PDF Parser for extracting renewal premiums, policy dates, and coverage terms."""

import re
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import date, datetime
from pydantic import BaseModel

logger = logging.getLogger("quote_parser")

class ExtractedQuoteData(BaseModel):
    policy_number: Optional[str] = None
    named_insured: Optional[str] = None
    renewal_premium: Optional[float] = None
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    coverage_summary: Dict[str, str] = {}
    raw_text_snippet: str = ""

class QuoteDocumentParser:
    """Extracts structured renewal terms from carrier PDF documents."""

    PREMIUM_PATTERNS = [
        r"(?:Total\s+Renewal\s+Premium|Renewal\s+Premium|Total\s+Premium|Estimated\s+Total\s+Premium|Annual\s+Premium)[\s:]*\$?([0-9,]+\.[0-9]{2})",
        r"(?:Premium\s+Due|Total\s+Annual|Term\s+Premium)[\s:]*\$?([0-9,]+\.[0-9]{2})",
        r"\$\s*([0-9,]+\.[0-9]{2})\s*(?:Renewal|Total)"
    ]

    DATE_PATTERNS = [
        r"(?:Effective\s+Date|Inception\s+Date|Policy\s+Period\s+From)[\s:]*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        r"(?:Expiration\s+Date|To)[\s:]*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})"
    ]

    POLICY_NUMBER_PATTERNS = [
        r"Policy\s*(?:Number|No\.?|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]*\d[A-Z0-9._/-]{2,40})",
        r"Pol\s*#\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]*\d[A-Z0-9._/-]{2,40})",
    ]

    def _parse_date_str(self, s: str) -> Optional[date]:
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.strip(), fmt).date()
            except ValueError:
                continue
        return None

    def parse_pdf(self, file_path: Path) -> ExtractedQuoteData:
        """Extracts text from PDF and parses key insurance fields."""
        if not file_path.exists():
            return ExtractedQuoteData(raw_text_snippet="File not found")

        full_text = ""
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages[:5]:  # Inspect first 5 pages
                    text = page.extract_text()
                    if text:
                        full_text += text + "\n"
        except Exception as e:
            logger.warning(f"pdfplumber extraction failed ({e}), falling back to raw text search.")
            try:
                with open(file_path, "r", errors="ignore") as f:
                    full_text = f.read()
            except Exception:
                pass

        # Extract Renewal Premium
        extracted_prem: Optional[float] = None
        for pattern in self.PREMIUM_PATTERNS:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                try:
                    num_str = match.group(1).replace(",", "")
                    extracted_prem = float(num_str)
                    break
                except ValueError:
                    continue

        # Extract Dates
        eff_date: Optional[date] = None
        exp_date: Optional[date] = None
        eff_match = re.search(self.DATE_PATTERNS[0], full_text, re.IGNORECASE)
        if eff_match:
            eff_date = self._parse_date_str(eff_match.group(1))

        exp_match = re.search(self.DATE_PATTERNS[1], full_text, re.IGNORECASE)
        if exp_match:
            exp_date = self._parse_date_str(exp_match.group(1))

        extracted_pol: Optional[str] = None
        for pattern in self.POLICY_NUMBER_PATTERNS:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                extracted_pol = match.group(1).strip().rstrip(".,;")
                break

        return ExtractedQuoteData(
            policy_number=extracted_pol,
            renewal_premium=extracted_prem,
            effective_date=eff_date,
            expiration_date=exp_date,
            raw_text_snippet=full_text[:500] if full_text else ""
        )
