"""Evening collect targets today in America/New_York; morning stays prior weekday."""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.engine.collection_target import (
    eastern_calendar_day,
    evening_collection_target,
    main,
    morning_report_target,
    skip_if_prepared_reuses,
)


ROOT = Path(__file__).resolve().parents[1]
EVENING = ROOT / "scripts" / "run_source_collection_vm.sh"
MORNING = ROOT / "scripts" / "run_daily_accountability_vm.sh"
EASTERN = ZoneInfo("America/New_York")

# 2026-09-25 17:00 Eastern, the production miss: collect prepared 2026-09-24.
FRIDAY_EVENING = datetime(2026, 9, 25, 17, 0, tzinfo=EASTERN)
FRIDAY_MORNING = datetime(2026, 9, 25, 9, 0, tzinfo=EASTERN)
MONDAY_MORNING = datetime(2026, 9, 28, 9, 0, tzinfo=EASTERN)
MONDAY_EVENING = datetime(2026, 9, 28, 17, 0, tzinfo=EASTERN)

FAKE_PYTHON = """#!/bin/bash
printf '%s\\n' "$*" >> "${FAKE_PYTHON_LOG}"
if [ "$1" = "-m" ] && [ "$2" = "src.engine.collection_target" ]; then
  if [ "${FAKE_DATE_EXIT:-0}" != "0" ]; then
    exit "${FAKE_DATE_EXIT}"
  fi
  printf '%s\\n' "${FAKE_EASTERN_DATE:-2026-09-25}"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "src.production_main" ]; then
  exit "${FAKE_MAIN_EXIT:-0}"
fi
echo "unexpected: $*" >&2
exit 99
"""


def test_friday_evening_targets_same_eastern_day_not_prior_business_day():
    assert evening_collection_target(FRIDAY_EVENING) == date(2026, 9, 25)
    assert morning_report_target(FRIDAY_MORNING) == date(2026, 9, 24)
    assert morning_report_target(FRIDAY_EVENING) == date(2026, 9, 24)


def test_monday_morning_is_friday_and_monday_evening_is_monday():
    assert morning_report_target(MONDAY_MORNING) == date(2026, 9, 25)
    assert evening_collection_target(MONDAY_EVENING) == date(2026, 9, 28)
    assert morning_report_target(MONDAY_EVENING) == date(2026, 9, 25)


def test_eastern_day_holds_across_utc_midnight():
    # 21:30 Eastern is 01:30 UTC the next calendar day.
    late = datetime(2026, 9, 26, 1, 30, tzinfo=timezone.utc)
    assert late.astimezone(EASTERN).hour == 21
    assert evening_collection_target(late) == date(2026, 9, 25)
    assert eastern_calendar_day(late) == date(2026, 9, 25)


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        evening_collection_target(datetime(2026, 9, 25, 17, 0))


def test_skip_if_prepared_does_not_reuse_prior_day_for_today():
    """2026-09-25 evidence: yesterday ready must not satisfy today's collect."""
    ready = {date(2026, 9, 24)}
    today = evening_collection_target(FRIDAY_EVENING)
    prior = morning_report_target(FRIDAY_EVENING)
    assert today == date(2026, 9, 25)
    assert prior == date(2026, 9, 24)
    assert skip_if_prepared_reuses(ready, prior) is True
    assert skip_if_prepared_reuses(ready, today) is False
    assert skip_if_prepared_reuses(ready | {today}, today) is True


def test_cli_evening_prints_iso_date(capsys):
    assert main(["--evening"]) == 0
    printed = capsys.readouterr().out.strip()
    assert date.fromisoformat(printed) == datetime.now(EASTERN).date()


def test_morning_wrapper_still_omits_explicit_date():
    text = MORNING.read_text(encoding="utf-8")
    assert "-m src.production_main --publish --deliver" in text
    assert "--prefer-prepared --source-wait-minutes 30" in text
    assert "--date" not in text
    assert "--skip-if-prepared" not in text
    assert "collection_target" not in text
    assert "src.engine.collection_target" not in text


def test_evening_wrapper_passes_same_day_and_does_not_publish(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    log = tmp_path / "python-args.log"
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update(
        {
            "ACCOUNTABILITY_APP_ROOT": str(app),
            "ACCOUNTABILITY_PYTHON": str(fake),
            "SOURCE_COLLECTION_LOCK_FILE": str(tmp_path / "collect.lock"),
            "FAKE_PYTHON_LOG": str(log),
            "FAKE_EASTERN_DATE": "2026-09-25",
        }
    )
    result = subprocess.run(
        ["bash", str(EVENING)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = log.read_text(encoding="utf-8")
    assert "-m src.engine.collection_target --evening" in args
    assert "-m src.production_main --skip-if-prepared --date 2026-09-25" in args
    assert "--publish" not in args
    assert "--deliver" not in args
    assert "--prefer-prepared" not in args
    collection_log = (app / "data" / "logs" / "source_collection.log").read_text(encoding="utf-8")
    assert "Eastern day 2026-09-25" in collection_log
    script = EVENING.read_text(encoding="utf-8")
    assert "MAGELLAN_ALLOW_EMPTY" not in script
    assert "get_previous_business_day" not in script
    assert "flock -w 900" in script
    assert "15m" not in script


def test_evening_wrapper_refuses_a_non_iso_date_without_collecting(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    log = tmp_path / "python-args.log"
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update(
        {
            "ACCOUNTABILITY_APP_ROOT": str(app),
            "ACCOUNTABILITY_PYTHON": str(fake),
            "SOURCE_COLLECTION_LOCK_FILE": str(tmp_path / "collect.lock"),
            "FAKE_PYTHON_LOG": str(log),
            "FAKE_EASTERN_DATE": "yesterday",
        }
    )
    result = subprocess.run(
        ["bash", str(EVENING)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2
    assert "src.production_main" not in log.read_text(encoding="utf-8")


def test_evening_wrapper_does_not_collect_when_the_date_helper_fails(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    log = tmp_path / "python-args.log"
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update(
        {
            "ACCOUNTABILITY_APP_ROOT": str(app),
            "ACCOUNTABILITY_PYTHON": str(fake),
            "SOURCE_COLLECTION_LOCK_FILE": str(tmp_path / "collect.lock"),
            "FAKE_PYTHON_LOG": str(log),
            "FAKE_DATE_EXIT": "1",
        }
    )
    result = subprocess.run(
        ["bash", str(EVENING)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    args = log.read_text(encoding="utf-8")
    assert "src.production_main" not in args
