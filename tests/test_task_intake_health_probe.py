"""Task intake health: stall, identical reports, and quiet hours."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from robie_job_engine.task_intake_health import (
    LEASE_RECOVERY_LINE,
    RECOVERY_LINE,
    check_task_intake,
    main,
)

NY = ZoneInfo("America/New_York")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


def _beat(**overrides) -> dict:
    base = {
        "created_at": "2026-10-05T14:25:00+00:00",  # 10:25 AM ET
        "status": "ok",
        "digest": "digest-a",
        "newest_created_et": "2026-10-05T10:15:00-04:00",
        "row_count": 12,
        "error": "",
    }
    base.update(overrides)
    return base


def test_quiet_outside_business_hours_even_when_stalled():
    stalled = [_beat(status="failed", error="inbox")]
    assert check_task_intake(now=_at(4, 12), heartbeats=stalled) == []  # Sunday
    assert check_task_intake(now=_at(3, 12), heartbeats=stalled) == []  # Saturday
    assert check_task_intake(now=_at(5, 8, 59), heartbeats=stalled) == []
    assert check_task_intake(now=_at(5, 18), heartbeats=stalled) == []


def test_missing_email_and_non_zero_and_stale_run_alert_during_hours():
    missing = check_task_intake(
        now=_at(5, 10),
        heartbeats=[_beat(status="no_email", created_at="2026-10-05T14:00:00+00:00")],
    )
    assert missing == ["the Task Check-In email is missing"]

    failed = check_task_intake(
        now=_at(5, 10),
        heartbeats=[_beat(
            status="failed", error="inbox down",
            created_at="2026-10-05T13:55:00+00:00",
        )],
    )
    assert any("non-zero" in item for item in failed)

    stale = check_task_intake(
        now=_at(5, 11),
        heartbeats=[_beat(created_at="2026-10-05T13:30:00+00:00")],
        fresh_limit=20,
    )
    assert any("has not succeeded" in item for item in stale)

    assert any(
        "has not recorded a run" in item
        for item in check_task_intake(now=_at(5, 9), heartbeats=[])
    )


def test_newest_created_date_stall_uses_eastern_time():
    # 10:15 AM ET, checked at 12:00 PM ET, is 105 minutes and over the default 90.
    problems = check_task_intake(now=_at(5, 12), heartbeats=[_beat()])
    assert any("newest Created Date" in item for item in problems)

    fresh_task = check_task_intake(
        now=_at(5, 12),
        heartbeats=[_beat(
            created_at="2026-10-05T15:55:00+00:00",
            newest_created_et="2026-10-05T11:30:00-04:00",
            digest="moving",
        )],
    )
    assert fresh_task == []


def test_identical_report_hashes_alert_even_when_the_task_is_fresh():
    rows = [
        _beat(message_id="m1", digest="same", created_at="2026-10-05T15:55:00+00:00",
              newest_created_et="2026-10-05T11:40:00-04:00"),
        _beat(message_id="m2", digest="same", created_at="2026-10-05T15:25:00+00:00",
              newest_created_et="2026-10-05T11:40:00-04:00"),
        _beat(message_id="m3", digest="same", created_at="2026-10-05T14:55:00+00:00",
              newest_created_et="2026-10-05T11:40:00-04:00"),
    ]
    problems = check_task_intake(now=_at(5, 12), heartbeats=rows, digest_limit=3)
    assert problems == ["3 consecutive Task Check-In reports hashed the same"]

    two = check_task_intake(now=_at(5, 12), heartbeats=rows[:2], digest_limit=3)
    assert two == []


def test_repeated_ticks_of_one_email_do_not_alert():
    ticks = [
        _beat(message_id="same-mail", digest="same",
              created_at="2026-10-05T15:55:00+00:00",
              newest_created_et="2026-10-05T11:40:00-04:00"),
        _beat(message_id="same-mail", digest="same",
              created_at="2026-10-05T15:50:00+00:00",
              newest_created_et="2026-10-05T11:40:00-04:00"),
        _beat(message_id="same-mail", digest="same",
              created_at="2026-10-05T15:45:00+00:00",
              newest_created_et="2026-10-05T11:40:00-04:00"),
    ]
    assert check_task_intake(now=_at(5, 12), heartbeats=ticks, digest_limit=3) == []


def test_normal_day_is_quiet_and_a_stalled_feed_alerts_once():
    rows = []
    for minute in (55, 50, 45, 40, 35, 30):
        rows.append(_beat(
            created_at=f"2026-10-05T18:{minute:02d}:00+00:00",
            message_id="m-latest",
            digest="d-latest",
            newest_created_et="2026-10-05T14:40:00-04:00",
        ))
    for index, hour in enumerate((14, 13, 12, 11, 10, 9)):
        rows.append(_beat(
            created_at=f"2026-10-05T{hour + 4:02d}:05:00+00:00",
            message_id=f"m-{index}",
            digest=f"d-{index}",
            newest_created_et=f"2026-10-05T{hour:02d}:00:00-04:00",
        ))
    assert check_task_intake(
        now=_at(5, 15), heartbeats=rows, fresh_limit=20, stall_limit=90,
    ) == []

    stalled = [
        _beat(message_id="a", digest="same", created_at="2026-10-05T15:55:00+00:00",
              newest_created_et="2026-10-05T10:15:00-04:00"),
        _beat(message_id="a", digest="same", created_at="2026-10-05T15:50:00+00:00",
              newest_created_et="2026-10-05T10:15:00-04:00"),
        _beat(message_id="b", digest="same", created_at="2026-10-05T15:25:00+00:00",
              newest_created_et="2026-10-05T10:15:00-04:00"),
        _beat(message_id="c", digest="same", created_at="2026-10-05T14:55:00+00:00",
              newest_created_et="2026-10-05T10:15:00-04:00"),
    ]
    problems = check_task_intake(
        now=_at(5, 12), heartbeats=stalled, digest_limit=3, stall_limit=90,
    )
    assert problems.count("3 consecutive Task Check-In reports hashed the same") == 1
    assert sum("newest Created Date" in item for item in problems) == 1


def test_newest_row_stall_stays_quiet_before_ten_thirty():
    old = [_beat(
        created_at="2026-10-05T14:20:00+00:00",
        newest_created_et="2026-10-02T16:00:00-04:00",
    )]
    for moment in (_at(5, 9), _at(5, 10), _at(5, 10, 29)):
        problems = check_task_intake(now=moment, heartbeats=old)
        assert not any("newest Created Date" in item for item in problems)
    opened = check_task_intake(now=_at(5, 10, 30), heartbeats=old)
    assert any("newest Created Date has not moved" in item for item in opened)


def test_stall_alerts_once_then_one_recovery_line():
    episode: dict = {}
    old = [_beat(
        created_at="2026-10-05T15:55:00+00:00",
        newest_created_et="2026-10-05T10:15:00-04:00",
    )]
    first = check_task_intake(now=_at(5, 12), heartbeats=old, episode=episode)
    assert any("newest Created Date has not moved" in item for item in first)
    assert episode["stall_open"] is True
    second = check_task_intake(now=_at(5, 12, 10), heartbeats=old, episode=episode)
    assert not any("newest Created Date" in item for item in second)
    fresh = [_beat(
        created_at="2026-10-05T16:00:00+00:00",
        newest_created_et="2026-10-05T12:00:00-04:00",
    )]
    recovery = check_task_intake(now=_at(5, 12, 5), heartbeats=fresh, episode=episode)
    assert recovery == [RECOVERY_LINE]
    assert episode["stall_open"] is False
    quiet = check_task_intake(now=_at(5, 12, 10), heartbeats=fresh, episode=episode)
    assert quiet == []


def test_stall_episode_is_remembered_across_probe_runs(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE ezlynx_task_intake_heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, status TEXT, message_id TEXT, digest TEXT,
            newest_created_et TEXT, row_count INTEGER, error TEXT
        )"""
    )
    conn.execute(
        """INSERT INTO ezlynx_task_intake_heartbeats
           (created_at, status, message_id, digest, newest_created_et, row_count, error)
           VALUES (?,?,?,?,?,?,?)""",
        ("2026-10-05T16:00:00+00:00", "ok", "m", "d", "2026-10-05T10:15:00-04:00", 1, ""),
    )
    conn.commit()
    conn.close()
    first = check_task_intake(now=_at(5, 12), db_path=str(db))
    assert any("newest Created Date has not moved" in item for item in first)
    second = check_task_intake(now=_at(5, 12, 10), db_path=str(db))
    assert not any("newest Created Date" in item for item in second)


def test_dry_run_dropin_is_reported_while_live(tmp_path, monkeypatch):
    fresh = [_beat(
        created_at="2026-10-05T15:55:00+00:00",
        newest_created_et="2026-10-05T11:40:00-04:00",
    )]
    dropin = tmp_path / "live"
    dropin.mkdir()
    (dropin / "10-dry-run.conf").write_text(
        "[Service]\nEnvironment=ROBIE_TASK_INTAKE_DRY_RUN=1\n", encoding="utf-8",
    )
    (dropin / "20-bland-prod.conf").write_text(
        "[Service]\nEnvironment=ROBIE_PHONE_LIVE_CALLS=1\n", encoding="utf-8",
    )
    problems = check_task_intake(now=_at(5, 12), heartbeats=fresh, dropin_dir=str(dropin))
    assert any("10-dry-run.conf" in item for item in problems)

    dry_only = tmp_path / "dry"
    dry_only.mkdir()
    (dry_only / "10-dry-run.conf").write_text("leftover\n", encoding="utf-8")
    monkeypatch.delenv("ROBIE_PHONE_LIVE_CALLS", raising=False)
    quiet = check_task_intake(now=_at(5, 12), heartbeats=fresh, dropin_dir=str(dry_only))
    assert not any("10-dry-run.conf" in item for item in quiet)

    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    paged = check_task_intake(now=_at(5, 12), heartbeats=fresh, dropin_dir=str(dry_only))
    assert any("10-dry-run.conf" in item for item in paged)
    weekend = check_task_intake(now=_at(4, 12), heartbeats=fresh, dropin_dir=str(dry_only))
    assert any("10-dry-run.conf" in item for item in weekend)


def test_driver_lease_not_with_production_is_red_during_business_hours(monkeypatch):
    import json

    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED", "1")
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_HOLDER", "PRODUCTION")
    fresh = _beat(
        created_at="2026-10-05T14:00:00+00:00",
        newest_created_et="2026-10-05T09:50:00-04:00",
    )

    def reader(holder: str):
        def _read() -> str:
            return json.dumps({
                "version": 1,
                "state": "IN",
                "holder": holder,
                "expires_at": "2027-01-01T00:00:00+00:00",
            })
        return _read

    held_by_test = check_task_intake(
        now=_at(5, 10), heartbeats=[fresh], driver_reader=reader("TEST"),
    )
    assert "driver lease not with PRODUCTION" in held_by_test
    sunday = check_task_intake(
        now=_at(4, 12), heartbeats=[fresh], driver_reader=reader("TEST"),
    )
    assert sunday == []
    held_by_production = check_task_intake(
        now=_at(5, 10), heartbeats=[fresh], driver_reader=reader("PRODUCTION"),
    )
    assert held_by_production == []


def test_driver_lease_alerts_once_then_one_recovery_line(monkeypatch):
    import json

    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED", "1")
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_HOLDER", "PRODUCTION")
    fresh = _beat(
        created_at="2026-10-05T14:00:00+00:00",
        newest_created_et="2026-10-05T09:50:00-04:00",
    )
    episode: dict = {}

    def reader(holder: str):
        def _read() -> str:
            return json.dumps({
                "version": 1,
                "state": "IN",
                "holder": holder,
                "expires_at": "2027-01-01T00:00:00+00:00",
            })
        return _read

    moment = _at(5, 10)
    first = check_task_intake(
        now=moment, heartbeats=[fresh], driver_reader=reader("TEST"), episode=episode,
    )
    assert first == ["driver lease not with PRODUCTION"]
    assert episode["lease_open"] is True
    second = check_task_intake(
        now=moment, heartbeats=[fresh], driver_reader=reader("TEST"), episode=episode,
    )
    assert second == []
    sunday = check_task_intake(
        now=_at(4, 12), heartbeats=[fresh], driver_reader=reader("TEST"), episode=episode,
    )
    assert sunday == []
    assert episode["lease_open"] is True
    back = check_task_intake(
        now=moment, heartbeats=[fresh], driver_reader=reader("PRODUCTION"), episode=episode,
    )
    assert back == [LEASE_RECOVERY_LINE]
    assert episode["lease_open"] is False
    quiet = check_task_intake(
        now=moment, heartbeats=[fresh],
        driver_reader=reader("PRODUCTION"), episode=episode,
    )
    assert quiet == []


def test_lease_episode_is_remembered_across_probe_runs(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED", "1")
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_HOLDER", "PRODUCTION")
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE ezlynx_task_intake_heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, status TEXT, message_id TEXT, digest TEXT,
            newest_created_et TEXT, row_count INTEGER, error TEXT
        )"""
    )
    conn.execute(
        """INSERT INTO ezlynx_task_intake_heartbeats
           (created_at, status, message_id, digest, newest_created_et, row_count, error)
           VALUES (?,?,?,?,?,?,?)""",
        ("2026-10-05T14:00:00+00:00", "ok", "m", "d", "2026-10-05T09:50:00-04:00", 1, ""),
    )
    conn.commit()
    conn.close()

    def reader(holder: str):
        def _read() -> str:
            return json.dumps({
                "version": 1,
                "state": "IN",
                "holder": holder,
                "expires_at": "2027-01-01T00:00:00+00:00",
            })
        return _read

    moment = _at(5, 10)
    first = check_task_intake(now=moment, db_path=str(db), driver_reader=reader("TEST"))
    assert first == ["driver lease not with PRODUCTION"]
    second = check_task_intake(now=moment, db_path=str(db), driver_reader=reader("TEST"))
    assert second == []
    back = check_task_intake(now=moment, db_path=str(db), driver_reader=reader("PRODUCTION"))
    assert back == [LEASE_RECOVERY_LINE]
    quiet = check_task_intake(now=moment, db_path=str(db), driver_reader=reader("PRODUCTION"))
    assert quiet == []


def test_environment_file_that_sets_scope_or_playground_is_red(tmp_path):
    fresh = _beat(
        created_at="2026-10-05T14:00:00+00:00",
        newest_created_et="2026-10-05T09:50:00-04:00",
    )
    env_file = tmp_path / "override.env"
    env_file.write_text(
        "# ROBIE_PLAYGROUND=1\n# ROBIE_EZLYNX_WRITE_SCOPE=all\nOTHER=1\n",
        encoding="utf-8",
    )
    shown = f"EnvironmentFiles={env_file} (ignore_errors)"
    quiet = check_task_intake(
        now=_at(5, 10),
        heartbeats=[fresh],
        effective_environment="Environment=ROBIE_EZLYNX_WRITE_SCOPE=all",
        environment_files=shown,
    )
    assert quiet == []
    env_file.write_text("export ROBIE_PLAYGROUND=0\n", encoding="utf-8")
    red = check_task_intake(
        now=_at(5, 10),
        heartbeats=[fresh],
        effective_environment="Environment=ROBIE_EZLYNX_WRITE_SCOPE=all",
        environment_files=shown,
    )
    assert any(
        "an EnvironmentFile sets ROBIE_EZLYNX_WRITE_SCOPE or ROBIE_PLAYGROUND" in item
        for item in red
    )


def test_effective_environment_must_name_the_all_clients_scope():
    fresh = _beat(
        created_at="2026-10-05T14:00:00+00:00",
        newest_created_et="2026-10-05T09:50:00-04:00",
    )
    missing = check_task_intake(
        now=_at(5, 10),
        heartbeats=[fresh],
        effective_environment="Environment=ROBIE_ENV=PRODUCTION",
    )
    assert any("ROBIE_EZLYNX_WRITE_SCOPE=all" in item for item in missing)
    present = check_task_intake(
        now=_at(5, 10),
        heartbeats=[fresh],
        effective_environment="Environment=ROBIE_ENV=PRODUCTION ROBIE_EZLYNX_WRITE_SCOPE=all",
    )
    assert present == []


def test_healthy_probe_is_quiet_and_simulate_failure_does_not_post(monkeypatch, capsys):
    assert check_task_intake(
        now=_at(5, 10, 30),
        heartbeats=[_beat(
            created_at="2026-10-05T14:25:00+00:00",
            newest_created_et="2026-10-05T10:20:00-04:00",
        )],
    ) == []

    def refuse_post(*_args, **_kwargs):
        raise AssertionError("healthy or simulated probe must not post")

    monkeypatch.setattr(
        "robie_job_engine.task_intake_health.post_as_chat_app", refuse_post,
    )

    def refuse_check(**_kwargs):
        raise AssertionError("simulate-failure must not read heartbeats")

    monkeypatch.setattr(
        "robie_job_engine.task_intake_health.check_task_intake", refuse_check,
    )
    assert main(["--simulate-failure", "--no-chat"]) == 2
    captured = capsys.readouterr()
    assert "simulated task-intake health failure" in captured.out

    monkeypatch.setattr(
        "robie_job_engine.task_intake_health.check_task_intake", lambda **_kwargs: [],
    )
    assert main([]) == 0
    assert "simulated" not in capsys.readouterr().out
