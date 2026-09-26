"""Tests for the Tier B browser evidence driver (lock discipline)."""

from __future__ import annotations

import json
import time

import pytest

from robie_job_engine import evidence_tier_b
from robie_job_engine.evidence_tier_b import (
    TierBDriver,
    TierBLockError,
    TierBReadError,
    read_for_evidence,
)

FAKE_LOCK_SCRIPT = """\
#!/usr/bin/env bash
# Fake ezlynx-lock.sh for tests: acquire/heartbeat/release/status
# against a JSON state file next to the script.
set -u
STATE="$(dirname "$0")/fake-state.json"
[ -f "$STATE" ] || echo '{"holder":null,"heartbeats":[]}' > "$STATE"
case "$1" in
  acquire)
    if [ "${FAIL_ACQUIRE:-0}" = "1" ]; then echo "no slot" >&2; exit 1; fi
    python3 -c "
import json
s=json.load(open('$STATE')); s['holder']='$2'; json.dump(s,open('$STATE','w'))"
    echo "acquired: $2";;
  heartbeat)
    python3 -c "
import json,time
s=json.load(open('$STATE')); s['heartbeats'].append(time.time()); json.dump(s,open('$STATE','w'))"
    echo "heartbeat ok";;
  release)
    python3 -c "
import json
s=json.load(open('$STATE')); s['holder']=None; json.dump(s,open('$STATE','w'))"
    echo "released";;
  status)
    cat "$STATE";;
esac
"""


@pytest.fixture
def lock_script(tmp_path):
    script = tmp_path / "ezlynx-lock.sh"
    script.write_text(FAKE_LOCK_SCRIPT)
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "fake-state.json"


def _state(state_file):
    return json.loads(state_file.read_text())


def _reader(applicant_id, policy_number, fields):
    assert applicant_id == "220250093"
    return {f: "seen" for f in fields}


def test_acquire_on_enter_release_on_exit(lock_script, state_file):
    with TierBDriver(login="SSRobie", reader=_reader,
                     lock_script=lock_script) as driver:
        assert driver.lock_held
        assert _state(state_file)["holder"].startswith("tier-b evidence driver")
    assert _state(state_file)["holder"] is None
    assert not driver.lock_held


def test_holder_names_the_login(lock_script, state_file):
    with TierBDriver(login="SSRobie", reader=_reader,
                     lock_script=lock_script):
        holder = _state(state_file)["holder"]
    assert "SSRobie" in holder


def test_heartbeat_fires_while_held(lock_script, state_file):
    with TierBDriver(login="SSRobie", reader=_reader, lock_script=lock_script,
                     heartbeat_interval=0.2):
        time.sleep(1.5)
        beats = _state(state_file)["heartbeats"]
        assert len(beats) >= 2
    # No heartbeats after release.
    beats_after = _state(state_file)["heartbeats"]
    time.sleep(0.4)
    assert _state(state_file)["heartbeats"] == beats_after


def test_release_happens_when_reader_raises(lock_script, state_file):
    def boom(a, p, f):
        raise RuntimeError("page exploded")

    with pytest.raises(RuntimeError):
        with TierBDriver(login="SSRobie", reader=boom,
                         lock_script=lock_script) as driver:
            driver.fetch("220250093", "POL", ["writtenPremium"])
    assert _state(state_file)["holder"] is None


def test_fetch_returns_reader_mapping(lock_script):
    with TierBDriver(login="SSRobie", reader=_reader,
                     lock_script=lock_script) as driver:
        observed = driver.fetch("220250093", "POL", ["writtenPremium"])
    assert observed == {"writtenPremium": "seen"}


def test_fetch_rejects_non_mapping_reader(lock_script):
    def bad(a, p, f):
        return ["not", "a", "mapping"]

    with TierBDriver(login="SSRobie", reader=bad,
                     lock_script=lock_script) as driver:
        with pytest.raises(TierBReadError):
            driver.fetch("220250093", "POL", ["writtenPremium"])


def test_fetch_without_held_lock_raises(lock_script):
    driver = TierBDriver(login="SSRobie", reader=_reader,
                        lock_script=lock_script)
    with pytest.raises(TierBLockError):
        driver.fetch("220250093", "POL", ["writtenPremium"])


def test_acquire_failure_raises_and_holds_nothing(lock_script, state_file,
                                                  monkeypatch):
    monkeypatch.setenv("FAIL_ACQUIRE", "1")
    driver = TierBDriver(login="SSRobie", reader=_reader,
                         lock_script=lock_script)
    with pytest.raises(TierBLockError):
        driver.__enter__()
    assert not driver.lock_held


def test_login_required():
    with pytest.raises(ValueError):
        TierBDriver(login="  ", reader=_reader)


def test_read_for_evidence_shape(lock_script):
    with TierBDriver(login="SSRobie", reader=_reader,
                     lock_script=lock_script) as driver:
        observed = read_for_evidence(driver, "220250093", "POL",
                                     ["writtenPremium"])
    assert isinstance(observed, dict)
    assert observed["writtenPremium"] == "seen"
