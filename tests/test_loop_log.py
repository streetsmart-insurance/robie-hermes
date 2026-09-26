"""Tests for the persistent evidence-loop run log."""

from __future__ import annotations

import pytest

from robie_job_engine import loop_log
from robie_job_engine.store import JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def test_record_run_returns_row_id(store):
    row_id = loop_log.record_run(
        store, loop_job_id="loop-1", job_type="policy_change",
        phase="EXTRACT", outcome="DRAFT_OK",
        detail={"fields": ["writtenPremium"]},
    )
    assert row_id >= 1


def test_runs_for_returns_chronological_detail(store):
    loop_log.record_run(
        store, loop_job_id="loop-1", job_type="policy_change",
        phase="EXTRACT", outcome="DRAFT_OK",
    )
    loop_log.record_run(
        store, loop_job_id="loop-1", job_type="policy_change",
        phase="LOCK", outcome="NEEDS_HUMAN",
        detail={"reason": "1 uncertainty"},
    )
    rows = loop_log.runs_for(store, "loop-1")
    assert [r["phase"] for r in rows] == ["EXTRACT", "LOCK"]
    assert rows[1]["detail"] == {"reason": "1 uncertainty"}


def test_runs_for_isolated_per_job(store):
    loop_log.record_run(
        store, loop_job_id="loop-1", job_type="policy_change",
        phase="EXTRACT", outcome="DRAFT_OK",
    )
    loop_log.record_run(
        store, loop_job_id="loop-2", job_type="carrier_call",
        phase="EVIDENCE", outcome="MATCHED",
    )
    assert len(loop_log.runs_for(store, "loop-1")) == 1
    assert len(loop_log.runs_for(store, "loop-2")) == 1


def test_unknown_phase_does_not_lose_row(store):
    row_id = loop_log.record_run(
        store, loop_job_id="loop-1", job_type="policy_change",
        phase="brand_new_phase", outcome="OK",
    )
    assert row_id >= 1
    assert loop_log.runs_for(store, "loop-1")[0]["phase"] == "BRAND_NEW_PHASE"


def test_loop_summary_lists_timeline(store):
    loop_log.record_run(
        store, loop_job_id="loop-9", job_type="policy_change",
        phase="EVIDENCE", outcome="MISMATCH",
    )
    summary = loop_log.loop_summary(store, "loop-9")
    assert "loop-9" in summary
    assert "EVIDENCE" in summary
    assert "MISMATCH" in summary


def test_loop_summary_empty_job(store):
    assert "no loop runs recorded" in loop_log.loop_summary(store, "nope")
