"""PDF Parser for extracting renewal premiums, policy dates, and coverage terms.

Premium ranking (LOB-agnostic, Benli HO 2026-09-07):
the amount used for pending RWL + COMPLETE is the **Policy Total /
Total Annual / Grand Total** on the offer or declarations — never a
single coverage-line premium (Coverage A / Dwelling) when a higher
labeled policy total exists on the same page.
"""

from __future__ import annotations

import re
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import date, datetime
from pydantic import BaseModel, Field

logger = logging.getLogger("quote_parser")

# Dollars with cents — avoids HO/GL *limits* like $250,000 (no cents).
_MONEY = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\.[0-9]{2}"
_MONEY_RE = re.compile(_MONEY)

# Higher rank wins; same rank → largest amount.
# Do not put bare "Annual Premium" / "Renewal Premium" in the top band —
# those are the Coverage A / first-column hits on Hyundai-style decs.
_LABELED_TOTALS: Tuple[Tuple[int, str], ...] = (
    (100, r"Policy\s+Total\s+Premium"),
    (98, r"Policy\s+Total"),
    (95, r"Total\s+Annual\s+Premium"),
    (93, r"Total\s+Annual"),
    (90, r"Grand\s+Total\s+Premium"),
    (88, r"Grand\s+Total"),
    (85, r"Total\s+Renewal\s+Premium"),
    (83, r"Estimated\s+Total\s+Premium"),
    (80, r"Total\s+Premium"),
    (75, r"Term\s+Premium"),
    (70, r"Amount\s+Due(?:\s*\(\s*renewal\s*\))?"),
    (65, r"Premium\s+Due"),
    (20, r"Renewal\s+Premium"),
    (18, r"Annual\s+Premium"),
)

# Generic annual/renewal column on a single coverage row (Dwelling / Coverage A).
_GENERIC_RANK_MAX = 30
_COVERAGE_LINE_RE = re.compile(
    r"(?:"
    r"coverage\s+[a-f]\b|"
    r"\b(?:dwelling|other\s+structures|personal\s+property|loss\s+of\s+use|"
    r"medical\s+payments|section\s+i)\b"
    r")",
    re.I,
)


class ExtractedQuoteData(BaseModel):
    policy_number: Optional[str] = None
    named_insured: Optional[str] = None
    renewal_premium: Optional[float] = None
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    coverage_summary: Dict[str, str] = Field(default_factory=dict)
    raw_text_snippet: str = ""


def _parse_money_token(token: str) -> Optional[Decimal]:
    cleaned = (token or "").replace(",", "").replace("$", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _line_containing(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end < 0:
        end = len(text)
    return text[start:end]


def _amount_after_label(text: str, match: re.Match[str]) -> Optional[Decimal]:
    """Require the dollar amount to follow the label (whitespace / colon only).

    Skips a short parenthetical such as ``(including TRIA)`` or ``(renewal)``.
    Does **not** walk past other words — that would pick the first ``$`` under
    a Coverage Section I column header.
    """
    tail = text[match.end() :]
    tail_match = re.match(
        rf"(?:\s*\([^)]{{0,40}}\))?(?:\s*[:.\-–])?[\s]*\$?\s*({_MONEY})",
        tail,
    )
    if not tail_match:
        return None
    return _parse_money_token(tail_match.group(1))


def _amount_before_label(text: str, match: re.Match[str]) -> Optional[Decimal]:
    """``$1,348.00 Policy Total Premium`` on the same line."""
    line_start = text.rfind("\n", 0, match.start()) + 1
    prefix = text[line_start : match.start()]
    found = list(_MONEY_RE.finditer(prefix))
    if not found:
        return None
    # Immediate predecessor only (no other tokens between amount and label).
    last = found[-1]
    between = prefix[last.end() :]
    if not re.fullmatch(r"[\s$:]*", between):
        return None
    return _parse_money_token(last.group(0))


def extract_policy_total_premium(text: str) -> Optional[Decimal]:
    """Return the Policy Total / labeled grand total from offer or dec text.

    Ranking:
    1. Policy Total Premium / Policy Total / Total Annual / Grand Total /
       Total Premium / Amount Due (renewal)
    2. Largest amount among the winning rank band
    3. Generic Annual / Renewal Premium only when no policy-total label exists
    4. Coverage A / Dwelling / Section I line amounts are demoted and never
       beat a labeled policy total
    """
    if not text:
        return None

    hits: List[Tuple[int, Decimal]] = []
    for rank, label_pat in _LABELED_TOTALS:
        for match in re.finditer(label_pat, text, flags=re.IGNORECASE):
            amount = _amount_after_label(text, match) or _amount_before_label(text, match)
            if amount is None:
                continue
            effective_rank = rank
            if rank <= _GENERIC_RANK_MAX and _COVERAGE_LINE_RE.search(
                _line_containing(text, match.start())
            ):
                effective_rank = 0
            hits.append((effective_rank, amount))

    if not hits:
        return None

    best_rank = max(rank for rank, _ in hits)
    return max(amount for rank, amount in hits if rank == best_rank)


class QuoteDocumentParser:
    """Extracts structured renewal terms from carrier PDF documents."""

    # Kept for callers/tests that still inspect the historical first-match list.
    # Ranking now lives in extract_policy_total_premium (Policy Total first).
    PREMIUM_PATTERNS = [
        r"(?:Policy\s+Total\s+Premium|Total\s+Annual\s+Premium|Grand\s+Total|"
        r"Total\s+Renewal\s+Premium|Total\s+Premium|Estimated\s+Total\s+Premium|"
        r"Amount\s+Due)[\s:]*\$?([0-9,]+\.[0-9]{2})",
        r"(?:Premium\s+Due|Total\s+Annual|Term\s+Premium|Renewal\s+Premium|"
        r"Annual\s+Premium)[\s:]*\$?([0-9,]+\.[0-9]{2})",
        r"\$\s*([0-9,]+\.[0-9]{2})\s*(?:Renewal|Total)",
    ]

    DATE_PATTERNS = [
        r"(?:Effective\s+Date|Inception\s+Date|Policy\s+Period\s+From)[\s:]*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        r"(?:Expiration\s+Date|To)[\s:]*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
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

        extracted = extract_policy_total_premium(full_text)
        extracted_prem = float(extracted) if extracted is not None else None

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
            raw_text_snippet=full_text[:500] if full_text else "",
        )
