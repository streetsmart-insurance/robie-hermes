"""Tests for the ROBIE verification pattern: Plan -> Execute -> Evidence -> Outcome."""

from __future__ import annotations

import pytest

from robie_job_engine.evidence import (
    Disposition,
    EvidenceOutcome,
    EvidenceUnavailable,
    Idempotency,
    LockedPlan,
    compare_plan_to_evidence,
    next_disposition,
    run_evidence_check,
    values_equal,
)
from robie_job_engine.evidence_fetchers import (
    fetch_discussion_evidence,
    fetch_document_evidence,
    fetch_policy_evidence,
)
from robie_job_engine.plan_lock import (
    PlanAlreadyLocked,
    get_locked_plan,
    lock_plan,
    plan_is_locked,
)
from robie_job_engine.policy_change_plan import (
    JOB_TYPE_POLICY_CHANGE,
    POLICY_CHANGE_SETTLE_DELAY_SECONDS,
    extract_policy_change_plan,
    plan_summary_text,
    tier_a_fields,
    tier_b_fields,
)
from robie_job_engine.store import JobStore


def _plan(**overrides):
    base = {
        "job_id": "job-001",
        "job_type": JOB_TYPE_POLICY_CHANGE,
        "fields": {"writtenPremium": 1284.00, "policyStatus": "Active"},
        "field_tiers": {"writtenPremium": "A", "policyStatus": "A"},
        "settle_delay_seconds": 600,
        "locked_at": "2026-09-21T20:00:00+00:00",
        "locked_by": "planner",
    }
    base.update(overrides)
    return LockedPlan(**base)


def _store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


# ---------------------------------------------------------------------------
# values_equal
# ---------------------------------------------------------------------------


def test_values_equal_numerics():
    assert values_equal(1284.00, "1284")
    assert values_equal("1,284.00", 1284)
    assert values_equal("$1284.00", "1284")
    assert not values_equal(1284.00, 1285.00)


def test_values_equal_strings_case_insensitive():
    assert values_equal("Active", "active")
    assert values_equal("  Active ", "active")
    assert not values_equal("Active", "Cancelled")


def test_values_equal_dates():
    assert values_equal("2026-09-01", "2026-09-01T00:00:00")
    assert not values_equal("2026-09-01", "2026-10-01")


def test_values_equal_none():
    assert values_equal(None, None)
    assert not values_equal(None, "x")
    assert not values_equal("x", None)


# ---------------------------------------------------------------------------
# compare_plan_to_evidence
# ---------------------------------------------------------------------------


def test_compare_all_matched():
    result = compare_plan_to_evidence(
        _plan(),
        {"writtenPremium": "1284.00", "policyStatus": "active"},
        now="2026-09-21T20:30:00+00:00",
    )
    assert result.outcome is EvidenceOutcome.MATCHED
    assert all(c.matched for c in result.checks)
    assert result.plan_job_id == "job-001"


def test_compare_mismatch_reports_expected_vs_actual():
    result = compare_plan_to_evidence(
        _plan(),
        {"writtenPremium": "999.00", "policyStatus": "Active"},
        now="2026-09-21T20:30:00+00:00",
    )
    assert result.outcome is EvidenceOutcome.MISMATCH
    bad = [c for c in result.checks if not c.matched]
    assert len(bad) == 1 and bad[0].field == "writtenPremium"
    assert bad[0].expected == 1284.00
    assert "999.00" in result.detail


def test_compare_missing_field_within_settle_is_pending():
    result = compare_plan_to_evidence(
        _plan(),
        {"policyStatus": "Active"},  # premium not visible yet
        now="2026-09-21T20:05:00+00:00",  # 5 min after lock, 600s settle
    )
    assert result.outcome is EvidenceOutcome.PENDING_SETTLEMENT
    pending = [c for c in result.checks if c.pending_settle]
    assert [c.field for c in pending] == ["writtenPremium"]


def test_compare_missing_field_after_settle_is_mismatch():
    result = compare_plan_to_evidence(
        _plan(),
        {"policyStatus": "Active"},
        now="2026-09-21T20:30:00+00:00",  # 30 min after lock, settle elapsed
    )
    assert result.outcome is EvidenceOutcome.MISMATCH


def test_compare_mismatch_beats_pending():
    plan = _plan(
        fields={"writtenPremium": 1284.00, "policyStatus": "Active"},
        locked_at="2026-09-21T20:00:00+00:00",
    )
    result = compare_plan_to_evidence(
        plan,
        {"policyStatus": "Cancelled"},  # premium missing (pending), status wrong
        now="2026-09-21T20:05:00+00:00",
    )
    assert result.outcome is EvidenceOutcome.MISMATCH


# ---------------------------------------------------------------------------
# run_evidence_check
# ---------------------------------------------------------------------------


def test_run_evidence_check_fetch_failure_is_no_evidence():
    def fetch():
        raise EvidenceUnavailable("PolicyApi timed out")

    result = run_evidence_check(_plan(), fetch)
    assert result.outcome is EvidenceOutcome.NO_EVIDENCE
    assert "timed out" in result.detail


def test_run_evidence_check_unexpected_exception_is_no_evidence_never_fake_pass():
    def fetch():
        raise RuntimeError("something exploded")

    result = run_evidence_check(_plan(), fetch)
    assert result.outcome is EvidenceOutcome.NO_EVIDENCE
    assert "RuntimeError" in result.detail


def test_run_evidence_check_bad_shape_is_no_evidence():
    result = run_evidence_check(_plan(), lambda: ["not", "a", "mapping"])
    assert result.outcome is EvidenceOutcome.NO_EVIDENCE


def test_run_evidence_check_happy_path():
    result = run_evidence_check(
        _plan(),
        lambda: {"writtenPremium": 1284, "policyStatus": "ACTIVE"},
        now="2026-09-21T20:30:00+00:00",
    )
    assert result.outcome is EvidenceOutcome.MATCHED


# ---------------------------------------------------------------------------
# next_disposition
# ---------------------------------------------------------------------------


def test_next_disposition_matrix():
    assert next_disposition(EvidenceOutcome.MATCHED, 0) is Disposition.CLOSE_CLEAN
    # MISMATCH retry is gated on the job type's idempotency declaration.
    # Undeclared job types default to NON_IDEMPOTENT: no blind retry --
    # straight to a human, so a retried note job can't post twice.
    assert next_disposition(EvidenceOutcome.MISMATCH, 0) is Disposition.HUMAN_QUEUE
    assert (
        next_disposition(EvidenceOutcome.MISMATCH, 0, Idempotency.NON_IDEMPOTENT)
        is Disposition.HUMAN_QUEUE
    )
    assert (
        next_disposition(EvidenceOutcome.MISMATCH, 0, Idempotency.IDEMPOTENT)
        is Disposition.RETRY_ONCE
    )
    assert (
        next_disposition(EvidenceOutcome.MISMATCH, 1, Idempotency.IDEMPOTENT)
        is Disposition.HUMAN_QUEUE
    )
    assert (
        next_disposition(EvidenceOutcome.MISMATCH, 0, Idempotency.VERIFY_BEFORE_RETRY)
        is Disposition.RETRY_ONCE
    )
    assert (
        next_disposition(EvidenceOutcome.MISMATCH, 1, Idempotency.VERIFY_BEFORE_RETRY)
        is Disposition.HUMAN_QUEUE
    )
    assert (
        next_disposition(EvidenceOutcome.NO_EVIDENCE, 0)
        is Disposition.ALERT_AND_RETRY_DELAYED
    )
    assert next_disposition(EvidenceOutcome.NO_EVIDENCE, 1) is Disposition.HUMAN_QUEUE
    assert (
        next_disposition(EvidenceOutcome.PENDING_SETTLEMENT, 0)
        is Disposition.RETRY_DELAYED
    )


def test_locked_plan_defaults_to_non_idempotent():
    # Fail closed: a plan that declares nothing never auto-retries.
    plan = _plan()
    assert plan.idempotency is Idempotency.NON_IDEMPOTENT
    assert (
        next_disposition(EvidenceOutcome.MISMATCH, 0, plan.idempotency)
        is Disposition.HUMAN_QUEUE
    )


def test_policy_change_plan_declares_idempotent():
    # Policy-change steps are field sets to exact values: re-running sets
    # the same value again, so the pilot permits one retry on MISMATCH.
    plan = extract_policy_change_plan(
        "job-1",
        applicant_id="123",
        policy_number="P-1",
        changes={"writtenPremium": 1284.00},
    )
    assert plan.idempotency is Idempotency.IDEMPOTENT
    assert (
        next_disposition(EvidenceOutcome.MISMATCH, 0, plan.idempotency)
        is Disposition.RETRY_ONCE
    )


# ---------------------------------------------------------------------------
# plan_lock: write-once
# ---------------------------------------------------------------------------


def test_lock_plan_write_once(tmp_path):
    store = _store(tmp_path)
    plan = _plan()
    locked = lock_plan(store, plan)
    assert locked.job_id == "job-001"
    assert plan_is_locked(store, "job-001")

    fetched = get_locked_plan(store, "job-001")
    assert fetched is not None
    assert fetched.fields == plan.fields
    assert fetched.job_type == JOB_TYPE_POLICY_CHANGE

    with pytest.raises(PlanAlreadyLocked):
        lock_plan(store, _plan(fields={"writtenPremium": 1.00}))

    # The original lock survived the overwrite attempt untouched.
    fetched_again = get_locked_plan(store, "job-001")
    assert fetched_again is not None
    assert fetched_again.fields == {"writtenPremium": 1284.00, "policyStatus": "Active"}


def test_lock_plan_rejects_empty(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        lock_plan(store, _plan(job_id=""))
    with pytest.raises(ValueError):
        lock_plan(store, _plan(fields={}))
    assert get_locked_plan(store, "nope") is None
    assert not plan_is_locked(store, "nope")


# ---------------------------------------------------------------------------
# policy_change_plan: the pilot extractor
# ---------------------------------------------------------------------------


def test_extract_policy_change_plan_tiers():
    plan = extract_policy_change_plan(
        "job-123",
        applicant_id="220250093",
        policy_number="HO12345",
        changes={"writtenPremium": 1284.00, "coverageA": 500000},
    )
    assert plan.job_type == JOB_TYPE_POLICY_CHANGE
    assert plan.fields["writtenPremium"] == 1284.00
    assert plan.fields["policyNumber"] == "HO12345"
    assert plan.field_tiers["writtenPremium"] == "A"
    assert plan.field_tiers["coverageA"] == "B"  # not API-visible: Tier B
    assert plan.settle_delay_seconds == POLICY_CHANGE_SETTLE_DELAY_SECONDS
    assert tier_a_fields(plan) == {
        "writtenPremium": 1284.00,
        "policyNumber": "HO12345",
    }
    assert "coverageA" in tier_b_fields(plan)
    summary = plan_summary_text(plan)
    assert "HO12345" in summary and "coverageA" in summary


def test_extract_policy_change_plan_rejects_bad_input():
    with pytest.raises(ValueError):
        extract_policy_change_plan("", applicant_id="1", policy_number="P", changes={"a": 1})
    with pytest.raises(ValueError):
        extract_policy_change_plan("j", applicant_id="", policy_number="P", changes={"a": 1})
    with pytest.raises(ValueError):
        extract_policy_change_plan("j", applicant_id="1", policy_number="", changes={"a": 1})
    with pytest.raises(ValueError):
        extract_policy_change_plan("j", applicant_id="1", policy_number="P", changes={})


# ---------------------------------------------------------------------------
# evidence_fetchers with fakes
# ---------------------------------------------------------------------------


def test_fetch_policy_evidence_found():
    def fake_search(number):
        assert number == "HO12345"
        return {
            "status": "success",
            "data": [
                {
                    "PolicyNumber": "HO12345",
                    "WrittenPremium": "1284.00",
                    "PolicyStatus": "Active",
                }
            ],
        }

    observed = fetch_policy_evidence(
        fake_search, "HO12345", ["writtenPremium", "policyStatus", "expirationDate"]
    )
    assert observed["writtenPremium"] == "1284.00"
    assert observed["policyStatus"] == "Active"
    assert observed["expirationDate"] is None  # record lacks it: None, not KeyError


def test_fetch_policy_evidence_not_found_is_no_evidence():
    def fake_search(number):
        return {"status": "success", "data": []}

    with pytest.raises(EvidenceUnavailable):
        fetch_policy_evidence(fake_search, "NOPE123", ["writtenPremium"])


def test_fetch_policy_evidence_search_error_is_no_evidence():
    def fake_search(number):
        raise ConnectionError("api down")

    with pytest.raises(EvidenceUnavailable):
        fetch_policy_evidence(fake_search, "HO12345", ["writtenPremium"])


def test_fetch_discussion_evidence_found():
    def fake_list(applicant_id):
        return [
            {"id": "111", "title": "Other thread", "noteCount": 3,
             "mostRecentNoteId": "1"},
            {"id": "222", "title": "Policy Change Request", "noteCount": 6,
             "mostRecentNoteId": "1128876526"},
        ]

    observed = fetch_discussion_evidence(fake_list, "220250093", "222")
    assert observed["note_count"] == "6"
    assert observed["most_recent_note_id"] == "1128876526"
    assert observed["title"] == "Policy Change Request"


def test_fetch_discussion_evidence_missing_thread_is_no_evidence():
    with pytest.raises(EvidenceUnavailable):
        fetch_discussion_evidence(lambda app_id: [], "220250093", "999")


def test_fetch_document_evidence_found_and_missing():
    def fake_search(applicant_id):
        return {
            "results": [
                {"id": "820700654", "documentName": "Quote Letter.pdf"},
                {"id": "820700655", "documentName": "Dec Page.pdf"},
            ]
        }

    found = fetch_document_evidence(fake_search, "31897605", "quote letter.pdf")
    assert found["found"] is True
    assert found["document_id"] == "820700654"

    missing = fetch_document_evidence(fake_search, "31897605", "audit.pdf")
    # Missing document is observed absence (MISMATCH material), not NO_EVIDENCE.
    assert missing["found"] is False


def test_fetch_document_evidence_search_error_is_no_evidence():
    def fake_search(applicant_id):
        raise TimeoutError("down")

    with pytest.raises(EvidenceUnavailable):
        fetch_document_evidence(fake_search, "31897605", "x.pdf")
