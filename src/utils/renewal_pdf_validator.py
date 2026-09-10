"""
Deterministic PDF Renewal Term Validator for StreetSmart Insurance Automation.
Enforces that uploaded/filed declaration pages strictly represent the upcoming renewal term
and blocks prior-term documents from slipping through.
"""

from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
import pypdf

logger = logging.getLogger(__name__)


class RenewalValidationError(Exception):
    """Base exception for renewal document validation failures."""
    pass


class PriorTermDetectedError(RenewalValidationError):
    """Raised when an inspected document belongs to an expiring or prior policy period."""
    pass


class RenewalTermNotFoundError(RenewalValidationError):
    """Raised when no discernible policy period dates can be extracted from the document."""
    pass


class PolicyNumberMismatchError(RenewalValidationError):
    """Raised when the expected policy number does not appear anywhere in the document."""
    pass


@dataclass
class RenewalValidationResult:
    is_valid: bool
    status: str  # VALID_RENEWAL, PRIOR_TERM_REJECTED, NO_DATES_FOUND, MISMATCH
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    detected_pairs: List[Tuple[date, date]] = None
    matched_pair: Optional[Tuple[date, date]] = None
    message: str = ""

    def __post_init__(self):
        if self.detected_pairs is None:
            self.detected_pairs = []


def _parse_date(s: str) -> Optional[date]:
    """Attempts to parse common date formats into a date object."""
    if not s:
        return None
    s = s.strip().rstrip(",")
    formats = [
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%b %d %Y",
        "%d %B %Y",
        "%d %b %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def extract_policy_periods(pdf_path: str | Path, max_pages: int = 25) -> List[Tuple[date, date, int, str]]:
    """
    Extracts candidate (effective_date, expiration_date) pairs from the PDF.
    Returns list of tuples: (effective_date, expiration_date, page_number, matched_text)
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")

    reader = pypdf.PdfReader(str(path))
    pairs: List[Tuple[date, date, int, str]] = []

    date_regex = (
        r"(?:\d{1,2}/\d{1,2}/(?:19|20)?\d{2}|"
        r"(?:19|20)\d{2}-\d{2}-\d{2}|"
        r"[A-Za-z]+ \d{1,2},? (?:19|20)\d{2})"
    )

    num_pages_to_check = min(len(reader.pages), max_pages)

    for p_idx in range(num_pages_to_check):
        raw_text = reader.pages[p_idx].extract_text() or ""
        if not raw_text.strip():
            continue

        # 1. Normalize line whitespace but preserve newlines
        lines = [re.sub(r"[ \t]+", " ", l.strip()) for l in raw_text.splitlines() if l.strip()]

        # Check multi-line patterns where effective and expiration dates are on adjacent or nearby lines
        for i in range(len(lines)):
            chunk = " ".join(lines[i:min(i + 5, len(lines))])
            # Match "Effective: <d1> Expiration: <d2>" or "Term Effective Date: <d1> Term Expiration Date: <d2>"
            m_eff_exp = re.search(
                rf"(?:effective|from|begins|inception).*?({date_regex}).*?(?:expiration|expires|to|ends|until).*?({date_regex})",
                chunk,
                re.IGNORECASE
            )
            if m_eff_exp:
                d1 = _parse_date(m_eff_exp.group(1))
                d2 = _parse_date(m_eff_exp.group(2))
                if d1 and d2 and d2 > d1:
                    delta = (d2 - d1).days
                    if 150 <= delta <= 400:
                        pairs.append((d1, d2, p_idx, m_eff_exp.group(0)))

        # 2. Check collapsed single-line text
        collapsed = re.sub(r"\s+", " ", raw_text)
        single_line_patterns = [
            rf"(?:policy\s*period|period|term)?\s*[:#]?\s*({date_regex})\s*(?:to|through|until|-)\s*({date_regex})",
            rf"({date_regex})\s*(?:to|through|until|-)\s*({date_regex})",
            rf"({date_regex})\s+({date_regex})",
        ]

        for pat in single_line_patterns:
            for m in re.finditer(pat, collapsed, re.IGNORECASE):
                d1 = _parse_date(m.group(1))
                d2 = _parse_date(m.group(2))
                if d1 and d2 and d2 > d1:
                    delta = (d2 - d1).days
                    if 150 <= delta <= 400:
                        # Avoid duplicate pairs on same page
                        if not any(p[0] == d1 and p[1] == d2 and p[2] == p_idx for p in pairs):
                            pairs.append((d1, d2, p_idx, m.group(0)))

    return pairs


def validate_renewal_pdf(
    pdf_path: str | Path,
    expected_renewal_effective: str | date,
    policy_number: Optional[str] = None,
    tolerance_days: int = 14,
    raise_on_error: bool = True
) -> RenewalValidationResult:
    """
    Validates that the PDF document represents the upcoming renewal term for the target policy.

    Args:
        pdf_path: Path to the PDF file.
        expected_renewal_effective: Target renewal effective date (usually policy.expiration_date).
        policy_number: Optional policy number to confirm document matches account.
        tolerance_days: Allowed difference in days between expected date and doc date (default 14).
        raise_on_error: If True, raises exceptions; if False, returns RenewalValidationResult.
    """
    path = Path(pdf_path)
    if not path.is_file():
        err_msg = f"File not found: {path}"
        if raise_on_error:
            raise FileNotFoundError(err_msg)
        return RenewalValidationResult(is_valid=False, status="FILE_NOT_FOUND", message=err_msg)

    # Parse expected effective date
    if isinstance(expected_renewal_effective, str):
        target_date = _parse_date(expected_renewal_effective)
        if not target_date:
            raise ValueError(f"Could not parse expected_renewal_effective date: {expected_renewal_effective}")
    else:
        target_date = expected_renewal_effective

    # 1. Verify policy number if provided
    if policy_number:
        # Strip common prefixes/suffixes for loose matching
        clean_pnum = re.sub(r"[^A-Za-z0-9]", "", policy_number).upper()
        reader = pypdf.PdfReader(str(path))
        full_text = " ".join([(p.extract_text() or "") for p in reader.pages[:10]])
        clean_text = re.sub(r"[^A-Za-z0-9]", "", full_text).upper()
        if clean_pnum not in clean_text:
            msg = f"Policy number mismatch: {policy_number} not found in document text."
            if raise_on_error:
                raise PolicyNumberMismatchError(msg)
            return RenewalValidationResult(is_valid=False, status="POLICY_MISMATCH", message=msg)

    # 2. Extract policy period date pairs
    detected_tuples = extract_policy_periods(path)
    if not detected_tuples:
        msg = f"No recognizable policy period dates found in {path.name}."
        if raise_on_error:
            raise RenewalTermNotFoundError(msg)
        return RenewalValidationResult(is_valid=False, status="NO_DATES_FOUND", message=msg)

    pairs = [(t[0], t[1]) for t in detected_tuples]

    # 3. Check for matching renewal term
    valid_pair = None
    for eff, exp in pairs:
        day_diff = abs((eff - target_date).days)
        if day_diff <= tolerance_days and exp > eff:
            valid_pair = (eff, exp)
            break

    if valid_pair:
        msg = f"Verified upcoming renewal term: {valid_pair[0]} to {valid_pair[1]}."
        logger.info(f"{path.name} validated: {msg}")
        return RenewalValidationResult(
            is_valid=True,
            status="VALID_RENEWAL",
            effective_date=valid_pair[0],
            expiration_date=valid_pair[1],
            detected_pairs=pairs,
            matched_pair=valid_pair,
            message=msg
        )

    # 4. If no renewal term matched, check if all or most dates belong to a PRIOR term
    prior_pairs = [p for p in pairs if p[0] < target_date - timedelta(days=30)]
    if prior_pairs:
        latest_prior = max(prior_pairs, key=lambda x: x[0])
        msg = (
            f"PRIOR TERM DETECTED in {path.name}: Document effective date {latest_prior[0]} to {latest_prior[1]} "
            f"is prior to expected renewal effective date {target_date}."
        )
        logger.error(msg)
        if raise_on_error:
            raise PriorTermDetectedError(msg)
        return RenewalValidationResult(
            is_valid=False,
            status="PRIOR_TERM_REJECTED",
            effective_date=latest_prior[0],
            expiration_date=latest_prior[1],
            detected_pairs=pairs,
            message=msg
        )

    # 5. Future or unaligned dates
    msg = f"Detected term dates {pairs} did not align with expected renewal effective {target_date}."
    logger.error(msg)
    if raise_on_error:
        raise RenewalValidationError(msg)
    return RenewalValidationResult(
        is_valid=False,
        status="MISMATCH",
        detected_pairs=pairs,
        message=msg
    )
