"""Outcome health check for the Ascend notice review runner: proven both ways."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ascend_notice_review_health.py"
MAILBOX = "robie@streetsmart.insurance"


def load():
    spec = importlib.util.spec_from_file_location("review_health", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def healthy_status(**kw):
    s = {
        "status": "ok",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "mode": "propose_only",
        "mailboxes": {
            MAILBOX: {
                "window": ["2026-10-01", "2026-10-05"],
                "complete": True,
                "pages": 2,
                "fetched": 10,
                "review": 3,
                "unrelated": 7,
                "errors": [],
                "checkpoint": "2026-10-04",
                "destination_writes": 0,
                "gmail_label_changes": 0,
            }
        },
        "queue_rows": 3,
    }
    s.update(kw)
    return s


@pytest.fixture()
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def write_state(d, status_obj, perms=0o600):
    (d / "status.json").write_text(json.dumps(status_obj))
    (d / "checkpoints.json").write_text("{}")
    (d / "review_queue.db").write_bytes(b"")
    for f in d.iterdir():
        os.chmod(f, perms)


def run_health(d, *extra):
    env = dict(os.environ, ROBIE_ASCEND_NOTICE_REVIEW_STATE_DIR=str(d))
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra], capture_output=True, text=True, env=env
    )


def test_healthy_is_quiet_exit_zero(state_dir):
    write_state(state_dir, healthy_status())
    r = run_health(state_dir)
    assert r.returncode == 0
    assert "OK" in r.stdout and "ALERT" not in r.stdout


def test_stale_run_alerts(state_dir):
    old = healthy_status(ran_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    write_state(state_dir, old)
    r = run_health(state_dir)
    assert r.returncode == 1
    assert "ALERT" in r.stdout and "stale" in r.stdout


def test_destination_write_breaks_propose_only_invariant(state_dir):
    s = healthy_status()
    s["mailboxes"][MAILBOX]["destination_writes"] = 1
    write_state(state_dir, s)
    r = run_health(state_dir)
    assert r.returncode == 1
    assert "destination_writes=1" in r.stdout


def test_incomplete_run_alerts(state_dir):
    write_state(state_dir, healthy_status(status="incomplete"))
    r = run_health(state_dir)
    assert r.returncode == 1
    assert "incomplete" in r.stdout


def test_missing_status_alerts(state_dir):
    r = run_health(state_dir)
    assert r.returncode == 1
    assert "ALERT" in r.stdout


def test_wrong_permissions_alert(state_dir):
    write_state(state_dir, healthy_status(), perms=0o644)
    r = run_health(state_dir)
    assert r.returncode == 1
    assert "0o644" in r.stdout


def test_run_errors_surface_as_alerts(state_dir):
    s = healthy_status()
    s["mailboxes"][MAILBOX]["errors"] = ["m:TimeoutError"]
    write_state(state_dir, s)
    r = run_health(state_dir)
    assert r.returncode == 1
    assert "m:TimeoutError" in r.stdout


def test_disabled_run_stays_quiet(state_dir):
    # Feature deliberately off: not an outage, no alert.
    write_state(state_dir, healthy_status(status="disabled"))
    r = run_health(state_dir)
    assert r.returncode == 0
