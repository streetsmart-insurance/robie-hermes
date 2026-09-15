from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_daily_accountability_vm.sh"
BUSY_MESSAGE = "Accountability lock remained busy for 15 minutes"
FORBIDDEN_SUFFIX_WAIT = re.compile(r"flock\s+[^\n#]*-w\s+\d+[smhd]\b")


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _prepare_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True, exist_ok=True)
    (app / "data" / "run_state").mkdir(parents=True, exist_ok=True)
    return app


def _run_wrapper(
    tmp_path: Path,
    *,
    env_updates: dict[str, str] | None = None,
    extra_path: Path | None = None,
    timeout: float = 8,
) -> subprocess.CompletedProcess[str]:
    app = _prepare_app(tmp_path)
    fake_python_log = tmp_path / "python-args.log"
    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"${ACCOUNTABILITY_PYTHON_LOG}\"\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "ACCOUNTABILITY_APP_ROOT": str(app),
            "ACCOUNTABILITY_LOG_FILE": str(app / "daily_run.log"),
            "ACCOUNTABILITY_LOCK_FILE": str(app / "data" / "run_state" / "accountability.lock"),
            "ACCOUNTABILITY_PYTHON": str(fake_python),
            "ACCOUNTABILITY_PYTHON_LOG": str(fake_python_log),
            "ACCOUNTABILITY_FLOCK_WAIT_SECONDS": "2",
        }
    )
    if env_updates:
        env.update(env_updates)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    log_text = ""
    log_path = Path(env["ACCOUNTABILITY_LOG_FILE"])
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8")
    result.log_text = log_text  # type: ignore[attr-defined]
    result.python_log = (  # type: ignore[attr-defined]
        fake_python_log.read_text(encoding="utf-8") if fake_python_log.is_file() else ""
    )
    result.app_root = app  # type: ignore[attr-defined]
    return result


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


def test_wrapper_uses_integer_second_flock_wait() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'FLOCK_WAIT_SECONDS="${ACCOUNTABILITY_FLOCK_WAIT_SECONDS:-900}"' in text
    assert "flock -w \"${FLOCK_WAIT_SECONDS}\"" in text
    assert not re.search(r"flock\s+[^\n#]*-w\s+15m\b", text)
    assert "integer seconds" in text
    assert "util-linux" in text
    assert BUSY_MESSAGE in text
    assert "python -m src.production_main --publish --deliver" in text
    assert "flock_status" in text
    assert 'flock_status}" -eq 1' in text


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


def test_successful_lock_runs_production_main(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "-m src.production_main --publish --deliver" in result.python_log
    assert BUSY_MESSAGE not in result.stdout
    assert BUSY_MESSAGE not in result.log_text
    assert "production_main completed" in result.log_text


def test_busy_lock_exit_1_uses_busy_message(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _write_executable(
        bin_dir / "flock",
        "#!/bin/bash\necho 'simulated busy' >&2\nexit 1\n",
    )
    result = _run_wrapper(tmp_path, extra_path=bin_dir)
    assert result.returncode == 1
    assert BUSY_MESSAGE in result.log_text
    assert result.python_log == ""


def test_invalid_flock_args_are_not_reported_as_busy(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _write_executable(
        bin_dir / "flock",
        "#!/bin/bash\necho \"flock: invalid timeout value: '15m'\" >&2\nexit 75\n",
    )
    result = _run_wrapper(tmp_path, extra_path=bin_dir)
    assert result.returncode == 75
    combined = f"{result.stdout}\n{result.stderr}\n{result.log_text}"
    assert BUSY_MESSAGE not in combined
    assert "invalid timeout value" in combined
    assert "not a lock timeout" in result.log_text
    assert result.python_log == ""


def test_suffixed_wait_env_is_rejected_before_flock(tmp_path: Path) -> None:
    result = _run_wrapper(
        tmp_path,
        env_updates={"ACCOUNTABILITY_FLOCK_WAIT_SECONDS": "15m"},
    )
    assert result.returncode == 64
    combined = f"{result.stdout}\n{result.stderr}\n{result.log_text}"
    assert BUSY_MESSAGE not in combined
    assert "integer seconds" in combined
    assert result.python_log == ""


def test_held_lock_times_out_as_busy_not_usage_error(tmp_path: Path) -> None:
    app = _prepare_app(tmp_path)
    lock_file = app / "data" / "run_state" / "accountability.lock"
    lock_file.touch()
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>'{lock_file}'; flock 9; sleep 20"],
    )
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            busy = subprocess.run(
                ["flock", "-n", str(lock_file), "true"],
                check=False,
            )
            if busy.returncode != 0:
                break
            time.sleep(0.05)
        else:
            pytest.skip("could not observe a held lock")
        result = _run_wrapper(
            tmp_path,
            env_updates={
                "ACCOUNTABILITY_APP_ROOT": str(app),
                "ACCOUNTABILITY_LOG_FILE": str(app / "daily_run.log"),
                "ACCOUNTABILITY_LOCK_FILE": str(lock_file),
                "ACCOUNTABILITY_FLOCK_WAIT_SECONDS": "1",
            },
            timeout=6,
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    assert result.returncode == 1
    assert BUSY_MESSAGE in result.log_text
    assert "not a lock timeout" not in result.log_text
    assert result.python_log == ""
