"""M1+M3 hardening tests: empty-plan NO_EVIDENCE and typed-value hygiene.

Every test here must FAIL on the pre-fix tree and PASS with the fix.
"""

from __future__ import annotations

from decimal import Decimal

from robie_job_engine.evidence import (
    Disposition,
    EvidenceOutcome,
    LockedPlan,
    compare_plan_to_evidence,
    next_disposition,
    values_equal,
)
from robie_job_engine.plan_extraction import _sane_value


# ---------------------------------------------------------------------------
# M1: empty LockedPlan.fields must return NO_EVIDENCE, not MATCHED
# ---------------------------------------------------------------------------


def _empty_plan():
    return LockedPlan(job_id="j-empty", job_type="policy_change", fields={})


def test_m1_empty_plan_is_no_evidence():
    result = compare_plan_to_evidence(_empty_plan(), {"anything": 1})
    assert result.outcome is EvidenceOutcome.NO_EVIDENCE
    assert result.checks == ()
    assert "refusing to grade an empty checklist" in result.detail


def test_m1_empty_plan_never_dispositions_clean():
    result = compare_plan_to_evidence(_empty_plan(), {})
    assert next_disposition(result.outcome, 0) is Disposition.ALERT_AND_RETRY_DELAYED
    assert next_disposition(result.outcome, 0) is not Disposition.CLOSE_CLEAN


def test_m1_nonempty_plan_still_grades_normally():
    plan = LockedPlan(
        job_id="j-full",
        job_type="policy_change",
        fields={"writtenPremium": 1284.00, "policyStatus": "Active"},
        settle_delay_seconds=600,
        locked_at="2026-09-21T20:00:00+00:00",
    )
    result = compare_plan_to_evidence(
        plan,
        {"writtenPremium": "1284.00", "policyStatus": "active"},
        now="2026-09-21T20:30:00+00:00",
    )
    assert result.outcome is EvidenceOutcome.MATCHED


# ---------------------------------------------------------------------------
# M3: typed-value hygiene
# ---------------------------------------------------------------------------


def test_m3_identifiers_compare_as_strings():
    # "007" vs "7" were equal through the float branch; identifiers must not be.
    assert not values_equal("007", "7", field_name="policyNumber")
    assert values_equal("007", "007", field_name="policyNumber")
    assert values_equal("HO12345", "ho12345", field_name="policyNumber")  # casefold kept


def test_m3_long_identifiers_do_not_lose_precision():
    # 20+ digit IDs rounded to the same float under the old code.
    assert not values_equal(
        "12345678901234567890123",
        "12345678901234567890124",
        field_name="loan_number",
    )


def test_m3_invalid_dates_fail_closed():
    assert not values_equal("2026-02-30", "2026-02-30", field_name="effectiveDate")
    assert not values_equal("2026-13-01", "2026-13-01", field_name="effectiveDate")
    # A real date still matches its datetime rendering from the destination.
    assert values_equal("2026-09-01", "2026-09-01T00:00:00", field_name="effectiveDate")
    assert values_equal("2026-09-01", "2026-09-01", field_name="expirationDate")
    assert not values_equal("2026-09-01", "2026-10-01", field_name="expirationDate")


def test_m3_money_uses_decimal_not_float():
    assert values_equal(Decimal("1284.10"), "1284.1", field_name="writtenPremium")
    assert values_equal("1284.00", 1284.00, field_name="writtenPremium")
    assert values_equal("$1,284.00", "1284", field_name="fullTermPremium")
    # No tolerance: a one-cent difference is a real difference in money.
    assert not values_equal("1284.00", "1284.01", field_name="writtenPremium")
    assert not values_equal("1284.005", "1284.00", field_name="writtenPremium")


def test_m3_sane_value_rejects_impossible_dates():
    assert _sane_value("effectiveDate", "2026-13-01") is not None
    assert _sane_value("effectiveDate", "2026-02-30") is not None
    assert _sane_value("expirationDate", "2026-09-01") is None
