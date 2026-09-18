"""Regression tests: engine lease heartbeat writes gateway_progress checkpoints.

Guards against a recurrence of the 2026-09-18 Test failure mode, where the
lease-maintenance thread only wrote ``gateway_progress`` for Chat jobs.
``post_job_audit.audit_heartbeat`` requires that checkpoint, so every
verification worker (manual renewal, audit, mortgagee, policy change)
permanently failed its post-job audit on Test.

The fix writes the checkpoint for ALL action types from the lease heartbeat,
with ``source: job_engine_lease`` and first_at/last_at stamps.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

import robie_job_engine.engine as engine_mod
import robie_job_engine.post_job_audit as pja
from robie_job_engine.store import JobStore, canonical_json


def _maintain_leases_source() -> str:
    src = inspect.getsource(engine_mod)
    start = src.index("def maintain_leases")
    end = src.index("heartbeat = threading.Thread", start)
    return src[start:end]


def test_maintain_leases_writes_gateway_progress():
    src = _maintain_leases_source()
    assert "gateway_progress" in src, (
        "lease heartbeat must write gateway_progress checkpoints"
    )
    assert "job_engine_lease" in src, (
        "checkpoint source must be job_engine_lease"
    )


def test_maintain_leases_writes_on_first_beat_not_only_on_renew():
    """The first write must happen before the renew loop, so short jobs
    still get a heartbeat even if they finish before the first interval."""
    src = _maintain_leases_source()
    first_write = src.index("_write_gateway_progress()")
    loop_start = src.index("while not heartbeat_stop.wait(interval)")
    assert first_write < loop_start, (
        "gateway_progress must be written before entering the renew loop"
    )


def _make_job(store: JobStore) -> str:
    return store.create_job("test.noop", {})["id"]


def test_writer_output_shape_satisfies_audit_heartbeat(tmp_path):
    """The checkpoint shape the writer produces must make audit_heartbeat
    PASS — this is the producer/consumer contract the audits depend on."""
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    job_id = _make_job(store)
    store.checkpoint(
        job_id,
        "gateway_progress",
        {
            "source": "job_engine_lease",
            "first_at": "2026-09-18T01:49:40.680233+00:00",
            "last_at": "2026-09-18T01:50:10.775618+00:00",
        },
    )
    result = pja.audit_heartbeat(store, job_id)
    assert result["result"] == "PASS", result
    assert result["present"] is True
    assert result["source"] == "job_engine_lease"


def test_audit_heartbeat_still_fails_without_checkpoint(tmp_path):
    """The audit must stay fail-closed: no checkpoint -> FAIL, not PASS."""
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    result = pja.audit_heartbeat(store, _make_job(store))
    assert result["result"] == "FAIL", result
    assert result["present"] is False


def test_gateway_progress_upsert_keeps_first_at(tmp_path):
    """Replicates the writer's upsert: first_at is preserved across beats,
    last_at advances. Guards the ON CONFLICT logic the writer relies on."""
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    job_id = _make_job(store)

    def write(first_at: str, last_at: str) -> None:
        with store.transaction() as conn:
            existing = conn.execute(
                "SELECT data_json, created_at FROM checkpoints "
                "WHERE job_id=? AND kind='gateway_progress'",
                (job_id,),
            ).fetchone()
            preserved = first_at
            if existing is not None:
                previous = json.loads(existing["data_json"] or "{}")
                preserved = (
                    str(previous.get("first_at") or "").strip()
                    or str(existing["created_at"] or "").strip()
                    or first_at
                )
            conn.execute(
                "INSERT INTO checkpoints(job_id,kind,data_json,created_at) "
                "VALUES (?, 'gateway_progress', ?, ?) "
                "ON CONFLICT(job_id,kind) DO UPDATE SET "
                "data_json=excluded.data_json,created_at=excluded.created_at",
                (
                    job_id,
                    canonical_json(
                        {
                            "source": "job_engine_lease",
                            "first_at": preserved,
                            "last_at": last_at,
                        }
                    ),
                    last_at,
                ),
            )

    write("2026-09-18T01:49:40+00:00", "2026-09-18T01:49:40+00:00")
    write("2026-09-18T01:49:40+00:00", "2026-09-18T01:50:10+00:00")
    record = store.get_checkpoint_record(job_id, "gateway_progress")
    data = record["data"]
    assert data["first_at"] == "2026-09-18T01:49:40+00:00", data
    assert data["last_at"] == "2026-09-18T01:50:10+00:00", data
    assert data["source"] == "job_engine_lease", data
