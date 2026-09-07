"""Dry-run / idempotency tests for Robie Voice cron scripts."""

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from src.voice.processed_robie_notes import ProcessedRobieCallStore

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "scripts" / "install_robie_voice_crons.sh"
WATCH = REPO_ROOT / "scripts" / "run_robie_call_label_watch.sh"
REFRESH = REPO_ROOT / "scripts" / "run_voice_call_directory_refresh.sh"


def _run(cmd, env=None, cwd=None):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def test_install_script_is_executable_and_dry_run_prints_lines():
    assert INSTALL.exists()
    mode = INSTALL.stat().st_mode
    assert mode & stat.S_IXUSR
    proc = _run(
        ["bash", str(INSTALL), "--dry-run", "--app-dir", "/opt/renewal-automation-system"],
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "3,8,13,18,23,28,33,38,43,48,53,58 * * * *" in out
    assert "run_robie_call_label_watch.sh" in out
    assert "0 12 * * 0" in out
    assert "run_voice_call_directory_refresh.sh" in out
    assert "Dry-run: crontab not written." in out or "already present" in out
    assert "0 9 * * *" not in out


def test_install_script_adds_only_new_lines_and_is_idempotent(tmp_path):
    crontab = tmp_path / "user.cron"
    crontab.write_text(
        "0 9 * * * /opt/renewal-automation-system/scripts/run_daily_renewal_pipeline.sh\n"
        "20 * * * * /opt/renewal-automation-system/scripts/run_daily_robie_cleaner.sh\n",
        encoding="utf-8",
    )
    first = _run(
        [
            "bash",
            str(INSTALL),
            "--crontab-file",
            str(crontab),
            "--app-dir",
            "/opt/renewal-automation-system",
        ]
    )
    assert first.returncode == 0, first.stderr + first.stdout
    text = crontab.read_text(encoding="utf-8")
    assert "0 9 * * * /opt/renewal-automation-system/scripts/run_daily_renewal_pipeline.sh" in text
    assert "20 * * * * /opt/renewal-automation-system/scripts/run_daily_robie_cleaner.sh" in text
    assert text.count("run_robie_call_label_watch.sh") == 1
    assert text.count("run_voice_call_directory_refresh.sh") == 1

    second = _run(
        [
            "bash",
            str(INSTALL),
            "--crontab-file",
            str(crontab),
            "--app-dir",
            "/opt/renewal-automation-system",
        ]
    )
    assert second.returncode == 0, second.stderr + second.stdout
    assert "already present" in second.stdout
    again = crontab.read_text(encoding="utf-8")
    assert again.count("run_robie_call_label_watch.sh") == 1
    assert again.count("run_daily_renewal_pipeline.sh") == 1
    assert again.count("run_daily_robie_cleaner.sh") == 1


def test_watch_script_dry_run_invokes_scan_queue(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    fake_python = tmp_path / "fake-python"
    argv_log = tmp_path / "argv.txt"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > '{argv_log}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    lock = tmp_path / "watch.lock"
    proc = _run(
        ["bash", str(WATCH), "--dry-run"],
        env={
            "ROBIE_APP_DIR": str(tmp_path),
            "ROBIE_PYTHON": str(fake_python),
            "ROBIE_CALL_WATCH_LOCK": str(lock),
            "ROBIE_CALL_WATCH_TIMEOUT": "15",
        },
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    argv = argv_log.read_text(encoding="utf-8")
    assert "-m" in argv
    assert "src.voice.ezlynx_label_dispatcher" in argv
    assert "--scan-queue" in argv
    assert "--dry-run" in argv


def test_directory_refresh_script_dry_run(tmp_path):
    fake_python = tmp_path / "fake-python"
    argv_log = tmp_path / "argv.txt"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > '{argv_log}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    proc = _run(
        ["bash", str(REFRESH), "--dry-run"],
        env={
            "ROBIE_APP_DIR": str(tmp_path),
            "ROBIE_PYTHON": str(fake_python),
            "ROBIE_VOICE_DIR_LOCK": str(tmp_path / "dir.lock"),
            "ROBIE_VOICE_DIR_TIMEOUT": "15",
        },
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    argv = argv_log.read_text(encoding="utf-8")
    assert "src.voice.call_directory" in argv
    assert "--refresh" in argv
    assert "--dry-run" in argv


def test_scan_queue_dry_run_collects_watch_file_ids(tmp_path, monkeypatch):
    from src.voice.ezlynx_label_dispatcher import (
        EZLynxLabelCallDispatcher,
        collect_watch_applicant_ids,
    )

    watch = tmp_path / "watch.json"
    watch.write_text('{"applicant_ids": ["26356199", "21588091"]}\n', encoding="utf-8")
    monkeypatch.delenv("ROBIE_CALL_WATCH_APPLICANTS", raising=False)
    monkeypatch.setenv("ROBIE_CALL_WATCH_FILE", str(watch))

    ids = collect_watch_applicant_ids(watch_file=str(watch), extra_ids=["26356199"])
    assert "26356199" in ids
    assert "21588091" in ids
    assert ids.count("26356199") == 1

    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_ezlynx.get_applicant_discussions.return_value = []
    mock_ezlynx.get_applicant.return_value = {"status": "success", "applicant": {}}
    dispatcher = EZLynxLabelCallDispatcher(
        ezlynx_client=mock_ezlynx,
        voice_client=mock_voice,
        processed_store=ProcessedRobieCallStore(tmp_path / "notes.sqlite"),
    )
    result = dispatcher.process_watch_queue(
        dry_run=True,
        watch_file=str(watch),
        extra_ids=["999"],
    )
    assert result["dry_run"] is True
    assert "26356199" in result["applicant_ids"]
    assert result["dispatched"] == []
    mock_voice.dispatch_call.assert_not_called()
