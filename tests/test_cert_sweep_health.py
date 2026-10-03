"""Tests for the certificate sweep buddy (cert_sweep_health).

The buddy is the probe + alert Carlo requires with every PR: it must
fail (exit 1) when the sweep is not Green, and Green is defined as
timer active + last run fresh + last run clean. All offline, fakes only.

Covers:
- record_sweep_run writes/prunes the sweep_runs table and never raises
- probe Green: timer active, fresh clean run -> green, exit 0
- probe red: timer inactive / stale run / errored run / no runs yet
- UNVERIFIED-heavy but error-free runs stay Green (fail-closed by design)
- probe is read-only: no Gmail/EZLynx/Zapier touched, no sweep state
  mutated (only the alert-state file when --alert fires)
- alert dedup: one alert per red episode, repeat after the window,
  recovery notice on green-after-red, never raises
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine import cert_sweep_health as health


@pytest.fixture()
def datadir(tmp_path):
    d = str(tmp_path / "sweep")
    os.makedirs(d, exist_ok=True)
    return d


def _record(datadir, *, exit_code=0, filed=0, unverified=0, errors=0,
            age_s=60, elapsed_s=12.5):
    run_at = (datetime.now(timezone.utc) - timedelta(seconds=age_s))
    conn = sqlite3.connect(os.path.join(datadir, health.DB_FILENAME))
    try:
        conn.execute(health._RUNS_SCHEMA)
        conn.execute(
            "INSERT INTO sweep_runs (run_at, exit_code, filed, unverified,"
            " errors, elapsed_s) VALUES (?, ?, ?, ?, ?, ?)",
            (run_at.isoformat(timespec="seconds"), exit_code, filed,
             unverified, errors, elapsed_s),
        )
        conn.commit()
    finally:
        conn.close()


def _timer(active=True):
    return lambda: active


# --- record_sweep_run -------------------------------------------------------

def test_record_writes_row_and_prunes(datadir):
    for _ in range(health.SWEEP_RUNS_KEEP + 5):
        res = health.record_sweep_run(exit_code=0, filed=1, directory=datadir)
        assert res["recorded"] is True
    conn = sqlite3.connect(os.path.join(datadir, health.DB_FILENAME))
    try:
        n = conn.execute("SELECT COUNT(*) FROM sweep_runs").fetchone()[0]
    finally:
        conn.close()
    assert n == health.SWEEP_RUNS_KEEP


def test_record_never_raises_on_broken_dir(tmp_path):
    # A path that cannot become a sqlite DB: recording must not raise.
    bad = str(tmp_path / "nope")
    os.makedirs(bad, exist_ok=True)
    os.chmod(bad, 0o500)
    try:
        res = health.record_sweep_run(exit_code=0, directory=bad)
    finally:
        os.chmod(bad, 0o700)
    assert res["recorded"] in (True, False)  # either way, no raise


# --- probe ------------------------------------------------------------------

def test_probe_green(datadir):
    _record(datadir, exit_code=0, filed=2, unverified=1, errors=0, age_s=120)
    report = health.probe(directory=datadir, timer_check=_timer(True))
    assert report["green"] is True
    assert report["checks"]["timer_active"] is True
    assert report["checks"]["run_fresh"] is True
    assert report["checks"]["run_clean"] is True


def test_probe_red_timer_inactive(datadir):
    _record(datadir, age_s=60)
    report = health.probe(directory=datadir, timer_check=_timer(False))
    assert report["green"] is False
    assert report["checks"]["timer_active"] is False


def test_probe_red_stale_run(datadir):
    _record(datadir, age_s=3600)
    report = health.probe(directory=datadir, max_age_s=900,
                          timer_check=_timer(True))
    assert report["green"] is False
    assert report["checks"]["run_fresh"] is False
    assert "3600" in (report["checks"].get("run_fresh_reason") or "")


def test_probe_red_last_run_errored(datadir):
    _record(datadir, exit_code=1, errors=2, age_s=60)
    report = health.probe(directory=datadir, timer_check=_timer(True))
    assert report["green"] is False
    assert report["checks"]["run_clean"] is False


def test_probe_red_no_runs_yet(datadir):
    report = health.probe(directory=datadir, timer_check=_timer(True))
    assert report["green"] is False
    assert report["checks"]["last_run"] is None


def test_probe_red_timer_check_raises(datadir):
    _record(datadir, age_s=60)

    def boom():
        raise RuntimeError("no systemctl here")

    report = health.probe(directory=datadir, timer_check=boom)
    assert report["green"] is False
    assert report["checks"]["timer_active"] is False


def test_unverified_does_not_break_green(datadir):
    # UNVERIFIED = fail-closed by design (mail held unread for human
    # eyes). It must not turn the buddy red.
    _record(datadir, exit_code=0, unverified=9, errors=0, age_s=60)
    report = health.probe(directory=datadir, timer_check=_timer(True))
    assert report["green"] is True


def test_probe_is_read_only(datadir):
    _record(datadir, age_s=60)
    before = set(os.listdir(datadir))
    db = os.path.join(datadir, health.DB_FILENAME)
    before_size = os.path.getsize(db)
    report = health.probe(directory=datadir, timer_check=_timer(True))
    assert report["green"] is True
    assert set(os.listdir(datadir)) == before
    assert os.path.getsize(db) == before_size


def test_cli_exit_codes(datadir, capsys, monkeypatch):
    monkeypatch.setattr(health, "_default_timer_check", lambda: True)
    _record(datadir, age_s=60)
    rc = health.main(["--data-dir", datadir])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["probe"]["green"] is True

    stale = os.path.join(datadir, "stale")
    os.makedirs(stale, exist_ok=True)
    rc = health.main(["--data-dir", stale])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["probe"]["green"] is False


# --- alert ------------------------------------------------------------------

class FakePoster:
    def __init__(self):
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)
        return {"ok": True}


def _red_report(datadir):
    return health.probe(directory=datadir, timer_check=_timer(False))


def test_alert_fires_once_per_episode(datadir):
    poster = FakePoster()
    report = _red_report(datadir)
    r1 = health.maybe_alert(report, directory=datadir, poster=poster,
                            alert_every_s=3600)
    assert r1["alerted"] is True
    assert len(poster.texts) == 1
    assert "NOT GREEN" in poster.texts[0]
    assert "timer" in poster.texts[0].lower()

    # Second probe while still red and inside the window: quiet.
    r2 = health.maybe_alert(report, directory=datadir, poster=poster,
                            alert_every_s=3600)
    assert r2["alerted"] is False
    assert r2["state"] == "red-quiet"
    assert len(poster.texts) == 1


def test_alert_repeats_after_window(datadir):
    poster = FakePoster()
    report = _red_report(datadir)
    health.maybe_alert(report, directory=datadir, poster=poster,
                       alert_every_s=3600)
    # Window of 0 forces the repeat.
    r = health.maybe_alert(report, directory=datadir, poster=poster,
                           alert_every_s=0)
    assert r["alerted"] is True
    assert len(poster.texts) == 2


def test_alert_recovery_notice(datadir):
    poster = FakePoster()
    red = _red_report(datadir)
    health.maybe_alert(red, directory=datadir, poster=poster)
    assert len(poster.texts) == 1

    _record(datadir, exit_code=0, age_s=30)
    green = health.probe(directory=datadir, timer_check=_timer(True))
    assert green["green"] is True
    r = health.maybe_alert(green, directory=datadir, poster=poster)
    assert len(poster.texts) == 2
    assert "GREEN again" in poster.texts[1]

    # After recovery the state resets: a later green stays quiet.
    r2 = health.maybe_alert(green, directory=datadir, poster=poster)
    assert len(poster.texts) == 2


def test_alert_never_raises_when_poster_blows_up(datadir):
    def boom(text):
        raise RuntimeError("chat is down")

    report = _red_report(datadir)
    result = health.maybe_alert(report, directory=datadir, poster=boom)
    assert result["alerted"] is False  # recorded, not raised


def test_alert_without_poster_and_no_identity(datadir, monkeypatch):
    # No Chat identity configured: the alert records posted=False
    # (skipped or failed), never raises, never claims delivery.
    monkeypatch.delenv("ROBIE_CHAT_SA_KEY_FILE", raising=False)
    monkeypatch.delenv("CERT_SWEEP_CHAT_SPACE", raising=False)
    monkeypatch.delenv("ROBIE_CHAT_HOME_SPACE", raising=False)
    report = _red_report(datadir)
    result = health.maybe_alert(report, directory=datadir, poster=None)
    assert result["state"] == "red-alerted"
    assert result["alerted"] is False
    assert result["post"]["posted"] is False
    assert ("skipped" in result["post"]) != ("failed" in result["post"])
