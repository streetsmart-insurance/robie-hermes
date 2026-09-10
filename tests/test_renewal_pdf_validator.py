"""Unit tests for deterministic renewal PDF validator."""

from datetime import date
from pathlib import Path
import pytest

from src.utils.renewal_pdf_validator import (
    validate_renewal_pdf,
    PriorTermDetectedError,
    RenewalTermNotFoundError,
    PolicyNumberMismatchError,
    RenewalValidationResult
)


def test_reject_prior_term_upcic():
    pdf_path = Path("/opt/busy-borg/data/policy_pdfs/83644603/UPCIC_4701-2000-4650_CURRENT_DEC_PAGE.pdf")
    if not pdf_path.exists():
        pytest.skip("Test fixture not present on disk")
    
    with pytest.raises(PriorTermDetectedError) as exc_info:
        validate_renewal_pdf(
            pdf_path=pdf_path,
            expected_renewal_effective="2026-10-12",
            policy_number="4701-2000-4650",
            raise_on_error=True
        )
    assert "PRIOR TERM DETECTED" in str(exc_info.value)
    assert "2025-10-12" in str(exc_info.value)


def test_reject_prior_term_tipan():
    pdf_path = Path("/opt/busy-borg/data/policy_pdfs/21586719/HONJ026092_CURRENT_TERM_2025-10-12_to_2026-10-12.pdf")
    if not pdf_path.exists():
        pytest.skip("Test fixture not present on disk")
    
    with pytest.raises(PriorTermDetectedError):
        validate_renewal_pdf(
            pdf_path=pdf_path,
            expected_renewal_effective="2026-10-12",
            policy_number="HONJ026092",
            raise_on_error=True
        )


def test_accept_authentic_renewal_tipan():
    pdf_path = Path("/opt/busy-borg/data/policy_pdfs/21586719/HONJ026092_CURRENT_blob.pdf")
    if not pdf_path.exists():
        pytest.skip("Test fixture not present on disk")
    
    res = validate_renewal_pdf(
        pdf_path=pdf_path,
        expected_renewal_effective="2026-10-12",
        policy_number="HONJ026092",
        raise_on_error=True
    )
    assert res.is_valid is True
    assert res.status == "VALID_RENEWAL"
    assert res.effective_date == date(2026, 10, 12)
    assert res.expiration_date == date(2027, 10, 12)


def test_accept_authentic_renewal_abdin():
    pdf_path = Path("/opt/busy-borg/data/policy_pdfs/49338990/FMI_4204205_Renewal_2026-2027.pdf")
    if not pdf_path.exists():
        pytest.skip("Test fixture not present on disk")
    
    res = validate_renewal_pdf(
        pdf_path=pdf_path,
        expected_renewal_effective="2026-10-12",
        policy_number="4204205",
        raise_on_error=True
    )
    assert res.is_valid is True
    assert res.status == "VALID_RENEWAL"
    assert res.effective_date == date(2026, 10, 12)


def test_accept_authentic_renewal_chubb_lacorte():
    pdf_path = Path("/opt/busy-borg/data/policy_pdfs/58768907/Chubb_Renewal_13332766-03_2026-2027.pdf")
    if not pdf_path.exists():
        pytest.skip("Test fixture not present on disk")
    
    res = validate_renewal_pdf(
        pdf_path=pdf_path,
        expected_renewal_effective="2026-10-11",
        policy_number="13332766-03",
        raise_on_error=True
    )
    assert res.is_valid is True
    assert res.status == "VALID_RENEWAL"
    assert res.effective_date == date(2026, 10, 11)


def test_non_raising_mode_returns_structured_result():
    pdf_path = Path("/opt/busy-borg/data/policy_pdfs/83644603/UPCIC_4701-2000-4650_CURRENT_DEC_PAGE.pdf")
    if not pdf_path.exists():
        pytest.skip("Test fixture not present on disk")
    
    res = validate_renewal_pdf(
        pdf_path=pdf_path,
        expected_renewal_effective="2026-10-12",
        policy_number="4701-2000-4650",
        raise_on_error=False
    )
    assert res.is_valid is False
    assert res.status == "PRIOR_TERM_REJECTED"
    assert res.effective_date == date(2025, 10, 12)


def test_policy_number_mismatch_raises():
    pdf_path = Path("/opt/busy-borg/data/policy_pdfs/21586719/HONJ026092_CURRENT_blob.pdf")
    if not pdf_path.exists():
        pytest.skip("Test fixture not present on disk")
    
    with pytest.raises(PolicyNumberMismatchError):
        validate_renewal_pdf(
            pdf_path=pdf_path,
            expected_renewal_effective="2026-10-12",
            policy_number="WRONG_POL_99999",
            raise_on_error=True
        )
