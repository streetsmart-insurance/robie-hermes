"""Tests for the HITL plan-confirmation workflow backend."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from robie_job_engine import confirmations
from robie_job_engine.confirmations import (
    approve,
    confirm_and_lock,
    confirmation_summary,
    expire_old,
    get,
    list_pending,
    reject,
    request_confirmation,
    rows_for_sheet,
)
from robie_job_engine.plan_extraction import PlanDraft, PlanNeedsHumanReview
from robie_job_engine.store import JobStore


@pytest.fixture()
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def _changes(policy="TEST-HO-1"):
    return {
        "policy_number": policy,
        "changes": {"writtenPremium": "2450.0", "effectiveDate": "2026-10-01"},
    }


def _request(store, loop_job_id="loop-1", **kwargs):
    params = dict(
        loop_job_id=loop_job_id,
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
        changes_json=_changes(),
        requested_by="Carlo Ferrara",
    )
    params.update(kwargs)
    return request_confirmation(store, **params)


def _review_draft():
    return PlanDraft(
        applicant_id="220250093",
        policy_number="TEST-HO-1",
        changes={"writtenPremium": "2450.0"},
        evidence_spans={"writtenPremium": "written premium to 2450.00"},
        uncertainties=["driver add cannot be mapped"],
        needs_human_review=True,
        review_reasons=["model reported 1 uncertainty"],
        request_excerpt="increase premium",
        extracted_at="2026-09-21T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# request_confirmation
# ---------------------------------------------------------------------------

def test_request_creates_pending_confirmation(store):
    cid = _request(store)
    record = get(cid, store=store)
    assert record is not None
    assert record["status"] == "PENDING"
    assert record["loop_job_id"] == "loop-1"
    assert record["job_type"] == "policy_change"
    assert record["requested_by"] == "Carlo Ferrara"
    assert record["decided_by"] is None
    assert json.loads(record["changes_json"])["policy_number"] == "TEST-HO-1"


def test_request_is_idempotent_while_pending(store):
    first = _request(store)
    second = _request(store)
    assert first == second
    assert len(list_pending(store)) == 1


def test_request_creates_new_after_decided(store):
    first = _request(store)
    approve(first, "Carlo Ferrara", store=store)
    second = _request(store)
    assert second != first
    assert get(second, store=store)["status"] == "PENDING"


def test_request_rejects_empty_loop_job_id(store):
    with pytest.raises(ValueError):
        request_confirmation(
            store, loop_job_id="  ", job_type="policy_change",
            requested_by="Carlo Ferrara",
        )


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------

def test_approve_happy_path(store):
    cid = _request(store)
    record = approve(cid, "Carlo Ferrara", store=store)
    assert record["status"] == "APPROVED"
    assert record["decided_by"] == "Carlo Ferrara"
    assert record["decided_at"]
    assert get(cid, store=store)["status"] == "APPROVED"


def test_reject_happy_path_with_reason(store):
    cid = _request(store)
    record = reject(cid, "Carlo Ferrara", "wrong policy number", store=store)
    assert record["status"] == "REJECTED"
    assert record["decision_reason"] == "wrong policy number"


def test_double_decide_raises(store):
    cid = _request(store)
    approve(cid, "Carlo Ferrara", store=store)
    with pytest.raises(ValueError):
        approve(cid, "Carlo Ferrara", store=store)
    with pytest.raises(ValueError):
        reject(cid, "Carlo Ferrara", "too late", store=store)


def test_decide_unknown_id_raises_and_get_returns_none(store):
    with pytest.raises(ValueError):
        approve("does-not-exist", "Carlo Ferrara", store=store)
    with pytest.raises(ValueError):
        reject("does-not-exist", "Carlo Ferrara", store=store)
    assert get("does-not-exist", store=store) is None


def test_decide_blank_decided_by_raises(store):
    cid = _request(store)
    with pytest.raises(ValueError):
        approve(cid, "   ", store=store)


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------

def test_expire_old_marks_stale_pending(store):
    cid = _request(store)
    old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    conn = store.connect()
    conn.execute(
        "UPDATE plan_confirmations SET created_at = ? WHERE id = ?", (old, cid)
    )
    conn.commit()
    conn.close()
    assert expire_old(store, max_age_hours=72) == 1
    record = get(cid, store=store)
    assert record["status"] == "EXPIRED"
    assert record["decided_by"] == "system"


def test_expire_old_keeps_fresh_pending(store):
    _request(store)
    assert expire_old(store, max_age_hours=72) == 0
    assert len(list_pending(store)) == 1


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------

def test_list_pending_oldest_first(store):
    c1 = _request(store, loop_job_id="loop-1")
    c2 = _request(store, loop_job_id="loop-2")
    c3 = _request(store, loop_job_id="loop-3")
    pending = list_pending(store)
    assert [r["id"] for r in pending] == [c1, c2, c3]
    approve(c2, "Carlo Ferrara", store=store)
    assert [r["id"] for r in list_pending(store)] == [c1, c3]


# ---------------------------------------------------------------------------
# confirm_and_lock
# ---------------------------------------------------------------------------

def test_confirm_and_lock_approves_and_locks_review_draft(store):
    cid = _request(store)
    locked = confirm_and_lock(store, cid, _review_draft(), "job-1", "Carlo Ferrara")
    assert locked is not None
    record = get(cid, store=store)
    assert record["status"] == "APPROVED"
    assert record["decided_by"] == "Carlo Ferrara"


def test_confirm_and_lock_raises_on_already_decided(store):
    cid = _request(store)
    approve(cid, "Carlo Ferrara", store=store)
    with pytest.raises(ValueError):
        confirm_and_lock(store, cid, _review_draft(), "job-1", "Carlo Ferrara")


def test_confirm_and_lock_unknown_id_raises(store):
    with pytest.raises(ValueError):
        confirm_and_lock(store, "nope", _review_draft(), "job-1", "Carlo Ferrara")


def test_lock_still_refuses_without_approval():
    # Sanity: the underlying gate is unchanged -- no confirmation, no lock.
    from robie_job_engine.plan_extraction import draft_to_locked_plan

    with pytest.raises(PlanNeedsHumanReview):
        draft_to_locked_plan(_review_draft(), "job-1")


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def test_confirmation_summary_pending(store):
    cid = _request(store)
    line = confirmation_summary(get(cid, store=store))
    assert "waiting for approval" in line
    assert "Policy change" in line
    assert "TEST-HO-1" in line
    assert "Carlo Ferrara" in line
    assert "HITL" not in line and "draft_to_locked" not in line


def test_confirmation_summary_approved_and_rejected(store):
    cid = _request(store)
    approve(cid, "Carlo Ferrara", store=store)
    assert "approved by Carlo Ferrara" in confirmation_summary(
        get(cid, store=store)
    )
    cid2 = _request(store, loop_job_id="loop-2")
    reject(cid2, "Carlo Ferrara", "not now", store=store)
    line = confirmation_summary(get(cid2, store=store))
    assert "rejected by Carlo Ferrara" in line
    assert "not now" in line


def test_rows_for_sheet_shape_pending_first(store):
    c1 = _request(store, loop_job_id="loop-1")
    c2 = _request(store, loop_job_id="loop-2")
    approve(c2, "Carlo Ferrara", store=store)
    c3 = _request(store, loop_job_id="loop-3")
    headers, rows = rows_for_sheet(store)
    assert headers == [
        "Confirmation ID", "Job type", "Draft summary", "Status",
        "Requested by", "Decided by", "Decided at", "Created at",
    ]
    assert len(rows) == 3
    # Pending first (oldest first), then decided.
    assert [r[3] for r in rows] == ["PENDING", "PENDING", "APPROVED"]
    assert rows[0][0] == c1
    assert rows[1][0] == c3
    assert rows[2][0] == c2
    # Plain values only.
    assert all(isinstance(cell, str) for row in rows for cell in row)
    assert rows[0][5] == ""  # no decider yet


def test_store_is_required_everywhere(store):
    # No implicit "last used store": every call must name its store.
    cid = _request(store)
    with pytest.raises(ValueError):
        approve(cid, "Carlo Ferrara", store=None)
    with pytest.raises(ValueError):
        get(cid, store=None)
    with pytest.raises(ValueError):
        rows_for_sheet(None)
    # The confirmation is untouched by the refused calls.
    assert get(cid, store=store)["status"] == "PENDING"
