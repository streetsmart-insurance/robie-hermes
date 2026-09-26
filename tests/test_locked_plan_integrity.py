"""H4: locked-plan idempotency/hash persistence, tamper guard, audit log.

Every test here FAILS on the pre-H4 tree and PASSES with the fix:
- idempotency was dropped by _plan_to_row (read back NON_IDEMPOTENT);
- no plan_hash column existed;
- raw UPDATE/DELETE on locked_plans succeeded silently;
- no transitions table existed;
- no integrity check ran on read.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from robie_job_engine import board, confirmations, plan_lock, retry
from robie_job_engine.evidence import EvidenceSpan, Idempotency, LockedPlan, span_source_hash
from robie_job_engine.grade_registry import GradeResult
from robie_job_engine.store import JobStore

# Referenced as plan_lock.LockedPlanTampered (attribute access at call time)
# so the module still imports on the pre-H4 tree, where the name does not
# exist and each tamper test fails honestly instead of erroring at import.


@pytest.fixture
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def _plan(job_id="job-1", **kwargs) -> LockedPlan:
    params = dict(
        job_id=job_id,
        job_type="policy_change",
        fields={"writtenPremium": 2450.0},
        field_tiers={"writtenPremium": "A"},
        field_notes={"writtenPremium": "PolicyApi read-back"},
        idempotency=Idempotency.IDEMPOTENT,
    )
    params.update(kwargs)
    return LockedPlan(**params)


# ---------------------------------------------------------------------------
# 1. idempotency round-trips through lock/read
# ---------------------------------------------------------------------------


def test_idempotency_round_trips_through_lock(store):
    plan_lock.lock_plan(store, _plan(), locked_by="planner")
    back = plan_lock.get_locked_plan(store, "job-1")
    assert back is not None
    assert back.idempotency is Idempotency.IDEMPOTENT


def test_non_idempotent_default_preserved(store):
    plan_lock.lock_plan(store, _plan(idempotency=Idempotency.NON_IDEMPOTENT))
    back = plan_lock.get_locked_plan(store, "job-1")
    assert back.idempotency is Idempotency.NON_IDEMPOTENT


# ---------------------------------------------------------------------------
# 2. plan_hash is stored and matches the stored plan_json
# ---------------------------------------------------------------------------


def test_plan_hash_matches_stored_plan_json(store):
    plan_lock.lock_plan(store, _plan(), locked_by="planner")
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT plan_json, idempotency_key, plan_hash FROM locked_plans"
            " WHERE job_id = 'job-1'"
        ).fetchone()
    finally:
        conn.close()
    plan_json, idempotency_key, plan_hash = row[0], row[1], row[2]
    assert idempotency_key == "IDEMPOTENT"
    assert plan_hash is not None
    assert hashlib.sha256(plan_json.encode("utf-8")).hexdigest() == plan_hash
    # The idempotency declaration is covered by the hash.
    assert json.loads(plan_json)["idempotency"] == "IDEMPOTENT"


# ---------------------------------------------------------------------------
# 3. raw UPDATE/DELETE on locked_plans are refused (trigger guard)
# ---------------------------------------------------------------------------


def test_raw_update_on_locked_plans_refused(store):
    plan_lock.lock_plan(store, _plan())
    conn = store.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE locked_plans SET plan_json = '{}' WHERE job_id = 'job-1'"
            )
    finally:
        conn.close()
    # The row is untouched.
    assert plan_lock.get_locked_plan(store, "job-1") is not None


def test_raw_delete_on_locked_plans_refused(store):
    plan_lock.lock_plan(store, _plan())
    conn = store.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM locked_plans WHERE job_id = 'job-1'")
    finally:
        conn.close()
    assert plan_lock.plan_is_locked(store, "job-1")


# ---------------------------------------------------------------------------
# 4. transitions are appended in order; the log is append-only
# ---------------------------------------------------------------------------


def test_transitions_appended_in_order_and_append_only(store):
    plan_lock.lock_plan(store, _plan("loop-9"), locked_by="planner")

    cid = confirmations.request_confirmation(
        store,
        loop_job_id="loop-9",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
        changes_json={"policy_number": "P1", "changes": {"writtenPremium": "2450.0"}},
        requested_by="Carlo Ferrara",
    )
    confirmations.approve(cid, "Carlo Ferrara", store=store)

    grade = GradeResult(
        job_type="policy_change",
        label="policy_change",
        grade="PASS",
        passed=True,
        reasons=["all fields matched"],
    )
    board.record_grade(store, "grade-row-1", loop_job_id="loop-9", result=grade)

    assert retry._claim_key(store, "idem-key-1", "loop-9") == "FRESH"

    transitions = plan_lock.get_transitions(store, "loop-9")
    assert [t["transition"] for t in transitions] == [
        "locked",
        "approved",
        "graded",
        "retry_claimed",
    ]
    assert transitions[0]["actor"] == "planner"
    assert transitions[1]["actor"] == "Carlo Ferrara"
    assert transitions[1]["detail"]["confirmation_id"] == cid
    assert transitions[2]["detail"]["passed"] is True
    assert transitions[3]["detail"]["idempotency_key"] == "idem-key-1"

    # The audit log itself has no update/delete path.
    conn = store.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE locked_plan_transitions SET transition = 'x'"
                " WHERE job_id = 'loop-9'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM locked_plan_transitions WHERE job_id = 'loop-9'")
    finally:
        conn.close()


def test_confirm_and_lock_appends_approved_then_locked(store):
    from robie_job_engine.plan_extraction import PlanDraft

    quote = "written premium to 2450.00"
    request = f"please increase the {quote} on the policy"
    start = request.index(quote)
    draft = PlanDraft(
        applicant_id="220250093",
        policy_number="TEST-HO-1",
        changes={"writtenPremium": "2450.0"},
        evidence_spans={
            "writtenPremium": EvidenceSpan(
                source_id="change_request_text",
                source_hash=span_source_hash(request),
                offset_start=start,
                offset_end=start + len(quote),
                quote=quote,
            )
        },
        uncertainties=[],
        needs_human_review=True,
        review_reasons=["test"],
        request_excerpt="increase premium",
        extracted_at="2026-09-21T00:00:00+00:00",
    )
    cid = confirmations.request_confirmation(
        store,
        loop_job_id="loop-7",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
        changes_json={"policy_number": "TEST-HO-1",
                      "changes": {"writtenPremium": "2450.0"}},
        requested_by="Carlo Ferrara",
        draft=draft,
    )
    confirmations.confirm_and_lock(store, cid, draft, "loop-7", "Carlo Ferrara")
    transitions = plan_lock.get_transitions(store, "loop-7")
    assert [t["transition"] for t in transitions] == ["approved", "locked"]


# ---------------------------------------------------------------------------
# 5. tampered rows fail closed on read
# ---------------------------------------------------------------------------


def test_tampered_row_fails_closed_on_read(store):
    conn = store.connect()
    try:
        plan_lock._ensure_schema(conn)
        conn.execute(
            "INSERT INTO locked_plans"
            "(job_id, plan_json, locked_at, locked_by, idempotency_key, plan_hash)"
            " VALUES (?, ?, '2026-09-22T00:00:00+00:00', 'planner',"
            " 'IDEMPOTENT', ?)",
            ("job-tamper", '{"job_id": "job-tamper"}', "0" * 64),
        )
    finally:
        conn.close()
    with pytest.raises(plan_lock.LockedPlanTampered):
        plan_lock.get_locked_plan(store, "job-tamper")


def test_missing_hash_fails_closed():
    # A row with no stored hash (e.g. written by a path that bypassed the
    # migration) must fail closed on verification, never read as valid.
    with pytest.raises(plan_lock.LockedPlanTampered):
        plan_lock._verify_plan_hash("job-x", '{"a": 1}', None)
    with pytest.raises(plan_lock.LockedPlanTampered):
        plan_lock._verify_plan_hash("job-x", '{"a": 1}', "")


# ---------------------------------------------------------------------------
# 6. pre-H4 schema migrates: columns added, values backfilled
# ---------------------------------------------------------------------------


def test_pre_h4_schema_migrates_with_backfill(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE locked_plans ("
        " job_id TEXT PRIMARY KEY,"
        " plan_json TEXT NOT NULL,"
        " locked_at TEXT NOT NULL,"
        " locked_by TEXT NOT NULL)"
    )
    legacy_payload = json.dumps(
        {"job_id": "legacy-1", "job_type": "policy_change",
         "fields": {"a": 1}},
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO locked_plans VALUES ('legacy-1', ?, 't', 'planner')",
        (legacy_payload,),
    )
    conn.close()

    legacy_store = JobStore(path)
    plan = plan_lock.get_locked_plan(legacy_store, "legacy-1")
    assert plan is not None
    # Old rows carry no idempotency declaration: fail closed to NON_IDEMPOTENT.
    assert plan.idempotency is Idempotency.NON_IDEMPOTENT

    conn = legacy_store.connect()
    try:
        row = conn.execute(
            "SELECT idempotency_key, plan_hash FROM locked_plans"
            " WHERE job_id = 'legacy-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "NON_IDEMPOTENT"
    # The backfilled hash covers the legacy bytes exactly as persisted.
    expected_hash = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()
    assert row[1] == expected_hash


def test_pre_h4_row_with_idempotency_in_json_backfills_key(tmp_path):
    path = str(tmp_path / "legacy2.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE locked_plans ("
        " job_id TEXT PRIMARY KEY,"
        " plan_json TEXT NOT NULL,"
        " locked_at TEXT NOT NULL,"
        " locked_by TEXT NOT NULL)"
    )
    legacy_payload = json.dumps(
        {"job_id": "legacy-2", "idempotency": "IDEMPOTENT", "fields": {"a": 1}},
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO locked_plans VALUES ('legacy-2', ?, 't', 'planner')",
        (legacy_payload,),
    )
    conn.close()

    legacy_store = JobStore(path)
    plan = plan_lock.get_locked_plan(legacy_store, "legacy-2")
    assert plan.idempotency is Idempotency.IDEMPOTENT
