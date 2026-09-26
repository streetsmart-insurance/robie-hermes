from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_daily_accountability_vm.sh"
UNIT_DIR = ROOT / "deploy" / "systemd" / "streetsmart-accountability-prod"
SERVICE = UNIT_DIR / "streetsmart-accountability.service"
SERVICE_DROPINS = UNIT_DIR / "streetsmart-accountability.service.d"
ALERT = UNIT_DIR / "streetsmart-accountability-alert@.service"
BUSY_MESSAGE = "Accountability lock remained busy for 15 minutes."
FORBIDDEN_SUFFIX_WAIT = re.compile(r"flock\s+[^\n#]*-w\s+\d+[smhd]\b")
STOPPED_EXIT = 80

# Fake venv python. The wrapper calls it twice: `-c` for the holiday check
# (exit 0 = holiday) and `-m src.production_main ...` for the report.
FAKE_PYTHON = """#!/bin/bash
if [ "$1" = "-c" ]; then
  exit "${FAKE_HOLIDAY_EXIT:-1}"
fi
printf '%s\\n' "$*" >> "${FAKE_PYTHON_LOG}"
touch "${FAKE_PYTHON_STARTED}"
case "${FAKE_MAIN:-ok}" in
  ok) exit 0 ;;
  fail) echo boom >&2; exit 3 ;;
  sleep) sleep 30 ;;
  self-term) kill -TERM $$ ;;
esac
"""


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Wrapper:
    def __init__(self, tmp_path: Path, *, extra_path: Path | None = None, **fake: str):
        self.app = tmp_path / "app"
        self.app.mkdir(parents=True, exist_ok=True)
        self.lock_file = tmp_path / "accountability.lock"
        self.log_file = self.app / "data" / "logs" / "daily_run.log"
        self.python_log = tmp_path / "python-args.log"
        self.started = tmp_path / "python-started"
        fake_python = tmp_path / "fake-python"
        _write_executable(fake_python, FAKE_PYTHON)
        self.env = os.environ.copy()
        self.env.update(
            {
                "ACCOUNTABILITY_APP_ROOT": str(self.app),
                "ACCOUNTABILITY_PYTHON": str(fake_python),
                "ACCOUNTABILITY_LOCK_FILE": str(self.lock_file),
                "FAKE_PYTHON_LOG": str(self.python_log),
                "FAKE_PYTHON_STARTED": str(self.started),
                **fake,
            }
        )
        if extra_path is not None:
            self.env["PATH"] = f"{extra_path}{os.pathsep}{self.env['PATH']}"

    def run(self, timeout: float = 10) -> int:
        return subprocess.run(
            ["bash", str(SCRIPT)], env=self.env, capture_output=True, timeout=timeout, check=False
        ).returncode

    def start(self) -> subprocess.Popen[bytes]:
        # Own process group, like the unit cgroup that `systemctl stop` signals.
        return subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @property
    def log(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.is_file() else ""

    @property
    def main_args(self) -> str:
        return self.python_log.read_text(encoding="utf-8") if self.python_log.is_file() else ""


def _process_tree(root_pid: int) -> list[int]:
    """root_pid and every descendant, from /proc (no psutil dependency)."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text()
        except OSError:
            continue
        ppid = int(stat_text.rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(entry.name))
    tree, queue = [], [root_pid]
    while queue:
        pid = queue.pop()
        tree.append(pid)
        queue.extend(children.get(pid, []))
    return tree


def _signal_tree(root_pid: int, sig: signal.Signals) -> None:
    for pid in _process_tree(root_pid):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _stop_like_systemctl(proc: subprocess.Popen[bytes], *, after: Path | None = None, delay: float = 0.5) -> int:
    # systemctl stop (KillMode=control-group) signals every process in the unit
    # cgroup. A process group is not enough: GNU timeout moves itself and python
    # into their own group, so signal the whole wrapper process tree instead.
    try:
        if after is not None:
            deadline = time.time() + 5
            while not after.exists():
                if time.time() > deadline:
                    pytest.fail(f"timed out waiting for {after}")
                time.sleep(0.05)
        else:
            time.sleep(delay)
        _signal_tree(proc.pid, signal.SIGTERM)
        return proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            _signal_tree(proc.pid, signal.SIGKILL)


def _unit_values(*paths: Path) -> dict[str, list[str]]:
    """Collect Key=Value lines across a unit and its drop-ins, in order."""
    values: dict[str, list[str]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";", "[")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), []).append(value.strip())
    return values


def _service_values() -> dict[str, list[str]]:
    return _unit_values(SERVICE, *sorted(SERVICE_DROPINS.glob("*.conf")))


# --- flock guards (repo-wide) -------------------------------------------------


def test_repo_has_no_util_linux_invalid_flock_timeout_suffix() -> None:
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if FORBIDDEN_SUFFIX_WAIT.search(text):
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []


def test_util_linux_flock_rejects_suffixed_timeout() -> None:
    probe = subprocess.run(
        ["flock", "-w", "15m", "/tmp/robie-flock-probe", "true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if "invalid timeout" not in (probe.stderr or ""):
        pytest.skip("util-linux flock is not what rejected 15m on this host")
    assert probe.returncode not in (0, 1)
    assert probe.returncode in {64, 75}


def test_wrapper_keeps_live_flock_holiday_err_trap_and_inner_timeout() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    # run-accountability-now.yml greps the installed wrapper for `flock .*900`.
    assert re.search(r"flock .*900", text)
    assert "flock -w 900 9" in text
    assert "federal_holidays" in text
    assert "trap 'code=$?;" in text and "' ERR" in text
    assert "timeout --signal=TERM --kill-after=30s 55m nice -n 10" in text
    assert f"STOPPED_EXIT={STOPPED_EXIT}" in text
    assert "trap on_stop TERM" in text


# --- live wrapper behavior ----------------------------------------------------


def test_successful_run_invokes_production_main_and_exits_0(tmp_path: Path) -> None:
    w = Wrapper(tmp_path)
    assert w.run() == 0
    assert "-m src.production_main --publish --deliver --prefer-prepared --source-wait-minutes 30" in w.main_args
    assert "Run Complete Successfully" in w.log
    assert "FAILED" not in w.log


def test_holiday_skips_the_report_and_exits_0(tmp_path: Path) -> None:
    w = Wrapper(tmp_path, FAKE_HOLIDAY_EXIT="0")
    assert w.run() == 0
    assert "Agency closed for a federal holiday" in w.log
    assert w.main_args == ""


def test_busy_lock_exits_75(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _write_executable(bin_dir / "flock", "#!/bin/bash\nexit 1\n")
    w = Wrapper(tmp_path, extra_path=bin_dir)
    assert w.run() == 75
    assert BUSY_MESSAGE in w.log
    assert w.main_args == ""


# --- intentional stop: exit 80, no alert --------------------------------------


def test_systemctl_stop_during_report_exits_stopped_code(tmp_path: Path) -> None:
    w = Wrapper(tmp_path, FAKE_MAIN="sleep")
    rc = _stop_like_systemctl(w.start(), after=w.started)
    assert rc == STOPPED_EXIT
    assert "intentional stop" in w.log
    assert "FAILED" not in w.log
    assert "Run Complete Successfully" not in w.log


def test_systemctl_stop_while_waiting_for_lock_exits_stopped_code(tmp_path: Path) -> None:
    w = Wrapper(tmp_path, FAKE_MAIN="sleep")
    w.lock_file.touch()
    holder = subprocess.Popen(["bash", "-c", f"exec 9>'{w.lock_file}'; flock 9; sleep 20"])
    try:
        deadline = time.time() + 3
        while subprocess.run(["flock", "-n", str(w.lock_file), "true"], check=False).returncode == 0:
            if time.time() > deadline:
                pytest.skip("could not observe a held lock")
            time.sleep(0.05)
        rc = _stop_like_systemctl(w.start())
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    assert rc == STOPPED_EXIT
    assert "intentional stop" in w.log
    assert w.main_args == ""


# --- real failures: nonzero, not 80, still alert ------------------------------


def test_report_failure_keeps_its_exit_code_and_logs_failed(tmp_path: Path) -> None:
    w = Wrapper(tmp_path, FAKE_MAIN="fail")
    assert w.run() == 3
    assert "Accountability run FAILED (exit 3)" in w.log
    assert "intentional stop" not in w.log


def test_inner_timeout_expiry_is_a_failure_not_a_clean_stop(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _write_executable(bin_dir / "timeout", "#!/bin/bash\nexit 124\n")
    w = Wrapper(tmp_path, extra_path=bin_dir)
    assert w.run() == 124
    assert "Accountability run FAILED (exit 124)" in w.log
    assert "intentional stop" not in w.log


def test_python_killed_alone_is_a_failure_not_a_clean_stop(tmp_path: Path) -> None:
    w = Wrapper(tmp_path, FAKE_MAIN="self-term")
    assert w.run() == 143
    assert "Accountability run FAILED (exit 143)" in w.log
    assert "intentional stop" not in w.log


# --- dedicated VM units (streetsmart-accountability-prod) ---------------------


def test_prod_unit_treats_only_stopped_exit_as_extra_success() -> None:
    values = _service_values()
    assert values["SuccessExitStatus"] == [str(STOPPED_EXIT)]
    for failure in ("1", "3", "75", "124", "143", "SIGTERM"):
        assert failure not in values["SuccessExitStatus"][0].split()


def test_prod_unit_still_alerts_on_failure_and_timeout() -> None:
    values = _service_values()
    assert values["OnFailure"] == ["streetsmart-accountability-alert@%n.service"]
    assert values["Type"] == ["oneshot"]
    # Expiry is Result=timeout, which alerts even when the wrapper exits 80.
    assert values["TimeoutStartSec"] == ["65min"]
    assert values["ExecStart"] == [
        "/opt/streetsmart-daily-accountability/scripts/run_daily_accountability_vm.sh"
    ]


def test_prod_alert_unit_sends_failure_alert() -> None:
    values = _unit_values(ALERT)
    assert values["ExecStart"] == [
        "/opt/streetsmart-daily-accountability/venv/bin/python -m src.alerts failure %i"
    ]


def test_prod_units_do_not_replace_hermes_poc_unit() -> None:
    hermes_unit = (ROOT / "deploy" / "systemd" / "streetsmart-accountability.service").read_text(
        encoding="utf-8"
    )
    assert "/opt/streetsmart-hermes/releases/current/scripts/run_accountability_job.py" in hermes_unit
    assert "SuccessExitStatus" not in hermes_unit
