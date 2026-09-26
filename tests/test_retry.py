"""Tests for retry scheduling, VERIFY_BEFORE_RETRY, and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from robie_job_engine import retry
from robie_job_engine.evidence import (
    Disposition,
    EvidenceOutcome,
    LockedPlan,
)
from robie_job_engine.retry import IdempotencyClaimed, IdempotencyViolation
from robie_job_engine.store import JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def _plan() -> LockedPlan:
    return LockedPlan(
        job_id="job-1",
        job_type="policy_change",
        fields={"writtenPremium": 2450.0},
        field_tiers={"writtenPremium": "A"},
        locked_by="carlo",
    )


# ---------------------------------------------------------------------------
# schedule_retry
# ---------------------------------------------------------------------------

def test_schedule_retry_persists_row(store):
    row_id = retry.schedule_retry(
        store,
        loop_job_id="loop-1",
        job_type="policy_change",
        disposition=Disposition.RETRY_ONCE,
        attempt_number=1,
        delay_seconds=60,
        now=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc),
    )
    assert row_id >= 1
    pending = retry.pending_retries(store, "loop-1")
    assert len(pending) == 1
    assert pending[0]["disposition"] == "RETRY_ONCE"


def test_schedule_retry_rejects_non_retryable(store):
    with pytest.raises(ValueError):
        retry.schedule_retry(
            store,
            loop_job_id="loop-1",
            job_type="policy_change",
            disposition=Disposition.HUMAN_QUEUE,
            attempt_number=1,
        )
    with pytest.raises(ValueError):
        retry.schedule_retry(
            store,
            loop_job_id="loop-1",
            job_type="policy_change",
            disposition=Disposition.CLOSE_CLEAN,
            attempt_number=1,
        )
    assert retry.pending_retries(store, "loop-1") == []


def test_schedule_retry_accepts_string_disposition(store):
    row_id = retry.schedule_retry(
        store,
        loop_job_id="loop-1",
        job_type="policy_change",
        disposition="RETRY_DELAYED",
        attempt_number=0,
        delay_seconds=10,
    )
    assert row_id >= 1


def test_schedule_retry_default_delay(store):
    retry.schedule_retry(
        store,
        loop_job_id="loop-1",
        job_type="policy_change",
        disposition=Disposition.RETRY_ONCE,
        attempt_number=1,
        now=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc),
    )
    pending = retry.pending_retries(store, "loop-1")
    not_before = datetime.fromisoformat(pending[0]["not_before"])
    assert not_before == datetime(
        2026, 9, 22, 0, 5, tzinfo=timezone.utc
    )  # DEFAULT_DELAYS[RETRY_ONCE] = 300


def test_schedule_retry_rejects_negative_delay(store):
    with pytest.raises(ValueError):
        retry.schedule_retry(
            store,
            loop_job_id="loop-1",
            job_type="policy_change",
            disposition=Disposition.RETRY_ONCE,
            attempt_number=1,
            delay_seconds=-5,
        )


# ---------------------------------------------------------------------------
# claim_due / complete / cancel
# ---------------------------------------------------------------------------

def test_claim_due_claims_exactly_once(store):
    base = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    retry.schedule_retry(
        store, loop_job_id="loop-1", job_type="policy_change",
        disposition=Disposition.RETRY_ONCE, attempt_number=1,
        delay_seconds=0, now=base,
    )
    first = retry.claim_due(store, now=base + timedelta(seconds=1))
    second = retry.claim_due(store, now=base + timedelta(seconds=1))
    assert len(first) == 1
    assert second == []  # already claimed


def test_claim_due_ignores_future_rows(store):
    base = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    retry.schedule_retry(
        store, loop_job_id="loop-1", job_type="policy_change",
        disposition=Disposition.RETRY_DELAYED, attempt_number=1,
        delay_seconds=3600, now=base,
    )
    assert retry.claim_due(store, now=base) == []
    due = retry.claim_due(store, now=base + timedelta(hours=2))
    assert len(due) == 1


def test_complete_and_cancel(store):
    base = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    row_id = retry.schedule_retry(
        store, loop_job_id="loop-1", job_type="policy_change",
        disposition=Disposition.RETRY_ONCE, attempt_number=1,
        delay_seconds=0, now=base,
    )
    retry.complete_retry(store, row_id)
    assert retry.pending_retries(store, "loop-1") == []

    retry.schedule_retry(
        store, loop_job_id="loop-2", job_type="policy_change",
        disposition=Disposition.RETRY_ONCE, attempt_number=1,
        delay_seconds=0, now=base,
    )
    assert retry.cancel_retries(store, "loop-2") == 1
    assert retry.pending_retries(store, "loop-2") == []


# ---------------------------------------------------------------------------
# verify_before_retry
# ---------------------------------------------------------------------------

def test_verify_before_retry_matched_means_no_rewrite():
    plan = _plan()
    result = retry.verify_before_retry(
        plan, lambda: {"writtenPremium": "2450.00"}
    )
    assert result.outcome is EvidenceOutcome.MATCHED
    # Caller closes clean here; the write must NOT be re-executed.


def test_verify_before_retry_mismatch_allows_retry():
    plan = _plan()
    result = retry.verify_before_retry(
        plan, lambda: {"writtenPremium": "1.0"}
    )
    assert result.outcome is EvidenceOutcome.MISMATCH


def test_verify_before_retry_fetch_failure_is_no_evidence():
    plan = _plan()

    def boom():
        raise RuntimeError("session dead")

    result = retry.verify_before_retry(plan, boom)
    assert result.outcome is EvidenceOutcome.NO_EVIDENCE


# ---------------------------------------------------------------------------
# execute_idempotent
# ---------------------------------------------------------------------------

def test_execute_idempotent_runs_once(store):
    calls = []
    out = retry.execute_idempotent(
        store, idempotency_key="key-1", loop_job_id="loop-1",
        fn=lambda: calls.append(1) or "ok",
    )
    assert out == "ok"
    with pytest.raises(IdempotencyViolation):
        retry.execute_idempotent(
            store, idempotency_key="key-1", loop_job_id="loop-1",
            fn=lambda: calls.append(1),
        )
    assert calls == [1]


def test_execute_idempotent_requires_key(store):
    with pytest.raises(ValueError):
        retry.execute_idempotent(
            store, idempotency_key="  ", loop_job_id="loop-1", fn=lambda: 1
        )


def test_execute_idempotent_fn_failure_records_unknown(store):
    """A raised fn is not proof the destination did not commit: the key is
    recorded UNKNOWN and a blind retry is refused."""
    def boom():
        raise RuntimeError("write failed")

    with pytest.raises(RuntimeError):
        retry.execute_idempotent(
            store, idempotency_key="key-2", loop_job_id="loop-1", fn=boom
        )
    # Blind re-run refused: the first attempt may have landed.
    with pytest.raises(IdempotencyClaimed):
        retry.execute_idempotent(
            store, idempotency_key="key-2", loop_job_id="loop-1",
            fn=lambda: "blind retry must not run",
        )


def test_execute_idempotent_unknown_reruns_only_after_reconcile(store):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection dropped after the write")
        return "ok"

    with pytest.raises(RuntimeError):
        retry.execute_idempotent(
            store, idempotency_key="key-3", loop_job_id="loop-1", fn=flaky
        )
    # Reconcile says the effect never landed -> re-run is allowed, once.
    out = retry.execute_idempotent(
        store, idempotency_key="key-3", loop_job_id="loop-1",
        fn=flaky, reconcile=lambda: False,
    )
    assert out == "ok"
    assert calls == [1, 1]
    with pytest.raises(IdempotencyViolation):
        retry.execute_idempotent(
            store, idempotency_key="key-3", loop_job_id="loop-1",
            fn=flaky, reconcile=lambda: False,
        )
    assert calls == [1, 1]


def test_execute_idempotent_reconcile_match_never_reruns(store):
    """UNKNOWN + destination already shows the effect -> seal, don't replay."""
    def boom():
        raise RuntimeError("response lost after commit")

    with pytest.raises(RuntimeError):
        retry.execute_idempotent(
            store, idempotency_key="key-4", loop_job_id="loop-1", fn=boom
        )
    with pytest.raises(IdempotencyViolation):
        retry.execute_idempotent(
            store, idempotency_key="key-4", loop_job_id="loop-1",
            fn=lambda: "must not run", reconcile=lambda: True,
        )


# ---------------------------------------------------------------------------
# Critical 3 regressions: atomic claim, crash window, reconciliation
# ---------------------------------------------------------------------------

def test_concurrent_workers_cannot_both_execute(store):
    """Two workers racing the same key: exactly one fn ever runs."""
    import threading

    ran = []
    errors = []

    def work():
        ran.append(1)

    def worker():
        try:
            retry.execute_idempotent(
                store, idempotency_key="race-key", loop_job_id="loop-1",
                fn=work,
            )
        except IdempotencyViolation as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ran == [1]  # the side effect ran exactly once
    assert len(errors) == 1  # the loser was refused


def test_crash_after_effect_blocks_blind_replay(store):
    """Simulate a crash: key left CLAIMED (fn ran, completion never
    recorded). A later execute must refuse, not replay."""
    import sqlite3

    retry.pending_retries(store, "setup")  # create the schema
    conn = store.connect()
    conn.execute(
        "INSERT INTO executed_idempotency_keys "
        "(idempotency_key, loop_job_id, status, executed_at, claimed_at) "
        "VALUES ('crash-key', 'loop-1', 'CLAIMED', ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    with pytest.raises(IdempotencyClaimed):
        retry.execute_idempotent(
            store, idempotency_key="crash-key", loop_job_id="loop-1",
            fn=lambda: "replay must not run",
        )


def test_reconcile_seals_stale_claimed_when_effect_landed(store):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    retry.pending_retries(store, "setup")  # create the schema
    conn = store.connect()
    conn.execute(
        "INSERT INTO executed_idempotency_keys "
        "(idempotency_key, loop_job_id, status, executed_at, claimed_at) "
        "VALUES ('stale-key', 'loop-1', 'CLAIMED', ?, ?)",
        (old, old),
    )
    conn.commit()
    conn.close()
    result = retry.reconcile_idempotency_key(
        store, idempotency_key="stale-key", reconcile=lambda: True
    )
    assert result == "COMPLETED"
    with pytest.raises(IdempotencyViolation):
        retry.execute_idempotent(
            store, idempotency_key="stale-key", loop_job_id="loop-1",
            fn=lambda: "must not run",
        )


def test_reconcile_clears_stale_claimed_when_effect_absent(store):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    retry.pending_retries(store, "setup")  # create the schema
    conn = store.connect()
    conn.execute(
        "INSERT INTO executed_idempotency_keys "
        "(idempotency_key, loop_job_id, status, executed_at, claimed_at) "
        "VALUES ('stale-key2', 'loop-1', 'CLAIMED', ?, ?)",
        (old, old),
    )
    conn.commit()
    conn.close()
    result = retry.reconcile_idempotency_key(
        store, idempotency_key="stale-key2", reconcile=lambda: False
    )
    assert result == "CLEARED"
    out = retry.execute_idempotent(
        store, idempotency_key="stale-key2", loop_job_id="loop-1",
        fn=lambda: "fresh run ok",
    )
    assert out == "fresh run ok"


def test_reconcile_leaves_live_claim_alone(store):
    fresh = datetime.now(timezone.utc).isoformat()
    retry.pending_retries(store, "setup")  # create the schema
    conn = store.connect()
    conn.execute(
        "INSERT INTO executed_idempotency_keys "
        "(idempotency_key, loop_job_id, status, executed_at, claimed_at) "
        "VALUES ('live-key', 'loop-1', 'CLAIMED', ?, ?)",
        (fresh, fresh),
    )
    conn.commit()
    conn.close()
    result = retry.reconcile_idempotency_key(
        store, idempotency_key="live-key", reconcile=lambda: False
    )
    assert result == "CLAIMED"
    with pytest.raises(IdempotencyClaimed):
        retry.execute_idempotent(
            store, idempotency_key="live-key", loop_job_id="loop-1",
            fn=lambda: "must not run",
        )


def test_reconcile_absent_key(store):
    assert retry.reconcile_idempotency_key(
        store, idempotency_key="never-seen", reconcile=lambda: False
    ) == "ABSENT"


def test_legacy_completed_rows_stay_completed(store):
    """Pre-claim-protocol rows (no status/claimed_at) migrate to COMPLETED
    and keep refusing re-execution."""
    retry.execute_idempotent(
        store, idempotency_key="legacy-key", loop_job_id="loop-1",
        fn=lambda: "done",
    )
    assert retry.reconcile_idempotency_key(
        store, idempotency_key="legacy-key", reconcile=lambda: False
    ) == "COMPLETED"
