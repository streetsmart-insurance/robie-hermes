"""Tests for the per-worker learnings store (robie_job_engine.learnings)."""

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.learnings import (  # noqa: E402
    WORKERS,
    all_learnings,
    get_learnings,
)

LEARNINGS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "robie_job_engine", "learnings"
)

EXPECTED_PREFIX = {
    "policy_changes": "pc-",
    "mortgagee": "mg-",
    "renewals": "rn-",
    "audits": "au-",
}


@pytest.mark.parametrize("worker", WORKERS)
def test_yaml_parses_and_has_learnings(worker):
    path = os.path.join(LEARNINGS_DIR, f"{worker}.yaml")
    assert os.path.exists(path), f"missing learnings file for {worker}"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data.get("learnings"), list)
    assert len(data["learnings"]) > 0, f"{worker} learnings file is empty"


@pytest.mark.parametrize("worker", WORKERS)
def test_loader_returns_validated_learnings(worker):
    learnings = get_learnings(worker)
    assert len(learnings) > 0
    ids = [e["id"] for e in learnings]
    assert len(ids) == len(set(ids)), f"{worker} has duplicate ids"
    for entry in learnings:
        assert entry["id"].startswith(EXPECTED_PREFIX[worker])
        for field in ("id", "rule", "learned", "source"):
            assert entry[field], f"{worker}/{entry['id']}: empty {field}"


def test_all_learnings_covers_four_workers():
    all_l = all_learnings()
    assert set(all_l.keys()) == set(WORKERS)
    assert all(len(v) > 0 for v in all_l.values())


def test_unknown_worker_rejected():
    with pytest.raises(ValueError):
        get_learnings("nonexistent")
