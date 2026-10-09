"""Shell installer for the robie-filer unit, timer, live drop-in, and rollback."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-robie-filer.sh"
UNIT = "robie-filer.service"
TIMER = "robie-filer.timer"

STUB = textwrap.dedent(
    """\
    #!/bin/bash
    printf '%s\\n' "$(basename "$0") $*" >> "$STUB_LOG"
    case "$1" in
      is-active|is-enabled) printf '%s\\n' inactive ;;
    esac
    if [[ "$(basename "$0")" == systemctl && "$1" == start ]]; then
      d="$STUB_PREFIX/etc/systemd/system/robie-filer.service.d"
      if [[ -f "$d/30-live.conf" || -f "$d/40-auto-any-applicant.conf" ]]; then
        printf '%s\\n' LIVE_AT_START >> "$STUB_LOG"
      else
        printf '%s\\n' DRY_AT_START >> "$STUB_LOG"
      fi
      exit "${STUB_START_EXIT:-0}"
    fi
    exit 0
    """
)


def _run(tmp_path: Path, *args: str, start_exit: int = 0) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    log = tmp_path / "log.txt"
    log.touch()
    for name in ("systemctl", "systemd-analyze", "journalctl"):
        stub = tmp_path / name
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, STUB_LOG=str(log), STUB_PREFIX=str(tmp_path), STUB_START_EXIT=str(start_exit))
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--prefix", str(tmp_path), "--systemctl", str(tmp_path / "systemctl"),
         "--systemd-analyze", str(tmp_path / "systemd-analyze"), "--journalctl", str(tmp_path / "journalctl"), *args],
        capture_output=True, text=True, env=env, check=False,
    )
    return proc, log.read_text().splitlines()


def _etc(tmp_path: Path) -> Path:
    return tmp_path / "etc" / "systemd" / "system"


def test_default_install_is_dry_with_timer_stopped(tmp_path):
    proc, log = _run(tmp_path, "--release-dir", str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert (_etc(tmp_path) / UNIT).read_text() == (ROOT / "deploy/systemd" / UNIT).read_text()
    assert (_etc(tmp_path) / TIMER).exists()
    assert not (_etc(tmp_path) / f"{UNIT}.d" / "30-live.conf").exists()
    assert "DRY_AT_START" in log
    assert f"systemctl disable --now {TIMER}" in log
    assert f"systemctl enable --now {TIMER}" not in log
    assert "ROLLBACK_TARGET=" in proc.stdout


def test_live_drop_in_lands_only_after_the_dry_run(tmp_path):
    dropins = _etc(tmp_path) / f"{UNIT}.d"
    dropins.mkdir(parents=True)
    (dropins / "40-auto-any-applicant.conf").write_text("[Service]\n")
    proc, log = _run(tmp_path, "--release-dir", str(ROOT), "--live", "--enable-timer")
    assert proc.returncode == 0, proc.stderr
    assert "DRY_AT_START" in log and "LIVE_AT_START" not in log
    assert (dropins / "30-live.conf").exists()
    assert not (dropins / "40-auto-any-applicant.conf").exists()
    assert f"systemctl enable --now {TIMER}" in log


def test_failed_dry_run_installs_no_live_and_keeps_timer_stopped(tmp_path):
    proc, log = _run(tmp_path, "--release-dir", str(ROOT), "--live", "--enable-timer", start_exit=1)
    assert proc.returncode == 2
    assert "verification dry run failed" in proc.stderr
    assert not (_etc(tmp_path) / f"{UNIT}.d" / "30-live.conf").exists()
    assert f"systemctl enable --now {TIMER}" not in log
    # No failed unit is left behind for systemctl --failed or health checks.
    assert f"systemctl reset-failed {UNIT}" in log
    assert "failed state cleared" in proc.stderr


def test_successful_install_does_not_reset_or_enable_by_default(tmp_path):
    proc, log = _run(tmp_path, "--release-dir", str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert f"systemctl reset-failed {UNIT}" not in log
    assert f"systemctl enable --now {TIMER}" not in log


def test_rollback_restores_previous_files_and_stops_timer(tmp_path):
    etc = _etc(tmp_path)
    etc.mkdir(parents=True)
    (etc / UNIT).write_text("old unit\n")
    proc, _ = _run(tmp_path, "--release-dir", str(ROOT))
    backup = next(line.split("=", 1)[1] for line in proc.stdout.splitlines() if line.startswith("BACKUP="))
    assert (etc / UNIT).read_text() != "old unit\n"
    proc, log = _run(tmp_path, "--rollback", backup)
    assert proc.returncode == 0, proc.stderr
    assert (etc / UNIT).read_text() == "old unit\n"
    assert not (etc / TIMER).exists()
    assert f"systemctl disable --now {TIMER}" in log


def test_preview_writes_nothing(tmp_path):
    proc, log = _run(tmp_path, "--release-dir", str(ROOT), "--dry-run", "--live")
    assert proc.returncode == 0, proc.stderr
    assert not (_etc(tmp_path) / UNIT).exists()
    assert log == []
