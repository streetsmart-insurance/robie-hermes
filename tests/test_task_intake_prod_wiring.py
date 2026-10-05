"""Production intake wiring, unit files, and the installer."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from robie_job_engine.bland_call_port import BlandTransportCallPort
from robie_job_engine.bland_prod_wiring import (
    build_call_dependencies,
    fetch_ezlynx_applicant,
    production_secret_reader,
)
from robie_job_engine.bland_transport import KILL_SWITCH_SECRET, PROD_HOST
from robie_job_engine.ezlynx_applicant_phone import EzlynxApplicantPhoneLookup
from robie_job_engine.ringcentral_transfer_lookup import load_staff_directory

ROOT = Path(__file__).resolve().parent.parent
UNIT = (ROOT / "deploy/systemd/robie-task-intake.service").read_text(encoding="utf-8")
TIMER = (ROOT / "deploy/systemd/robie-task-intake.timer").read_text(encoding="utf-8")
HEALTH_UNIT = (ROOT / "deploy/systemd/robie-task-intake-health.service").read_text(encoding="utf-8")
HEALTH_TIMER = (ROOT / "deploy/systemd/robie-task-intake-health.timer").read_text(encoding="utf-8")
DROPIN = (
    ROOT / "deploy/systemd/robie-task-intake.service.d/20-bland-prod.conf.example"
).read_text(encoding="utf-8")
INSTALLER = ROOT / "scripts/install-robie-task-intake.sh"
TEST_UNIT = (ROOT / "deploy/systemd/robie-task-intake-test.service").read_text(encoding="utf-8")
TEST_TIMER = (ROOT / "deploy/systemd/robie-task-intake-test.timer").read_text(encoding="utf-8")

PROD_ENV = {
    "ROBIE_ENV": "PRODUCTION",
    "ROBIE_PHONE_LIVE_CALLS": "1",
    "ROBIE_PHONE_REAL_CLIENTS": "1",
    "ROBIE_BLAND_ALLOWED_HOSTS": "hermes-poc-01",
    "ROBIE_BLAND_ALLOWED_ENVS": "PRODUCTION",
    "ROBIE_BLAND_MAX_DURATION_MINUTES": "10",
}


def test_production_intake_wires_secret_manager_applicant_api_and_staff_dials():
    phone, bland, transfer, dry_run = build_call_dependencies(
        env={"ROBIE_ENV": "PRODUCTION"},
    )
    assert phone._fetch is fetch_ezlynx_applicant
    assert bland.secret_reader is production_secret_reader
    assert bland.execute is False
    assert dry_run is True
    directory = load_staff_directory()
    assert transfer.get_transfer_number("ashley huntley") == directory["ashley huntley"]["did"]

    other_phone, other_bland, _transfer, other_dry = build_call_dependencies(env={})
    assert other_bland.secret_reader is None
    assert other_phone.get_phone("900100001") is None
    assert other_dry is True


def test_missing_secret_and_unreadable_kill_switch_place_zero_dials():
    def boom(*_args, **_kwargs):
        raise AssertionError("urlopen must not be called")

    def missing(name: str) -> str:
        if name == KILL_SWITCH_SECRET:
            return "0"
        raise RuntimeError("secret missing")

    port = BlandTransportCallPort(
        env=PROD_ENV, hostname=PROD_HOST, secret_reader=missing,
        urlopen=boom, execute=True,
    )
    refused = port.place_call_with_double_dial("+17325550142", "task", "hi", "vm")
    assert refused["success"] is False
    assert refused["call_ids"] == []
    assert "unreadable" in refused["error"]

    no_reader = BlandTransportCallPort(
        env=PROD_ENV, hostname=PROD_HOST, api_key="SYN-KEY",
        urlopen=boom, execute=True,
    )
    halted = no_reader.place_call_with_double_dial("+17325550142", "task", "hi", "vm")
    assert halted["success"] is False
    assert halted["call_ids"] == []
    assert halted["error"] == "kill switch unreadable"


def test_ambiguous_applicant_phones_fail_closed_without_digits():
    lookup = EzlynxApplicantPhoneLookup(lambda _applicant_id: {
        "CellPhone": "(732) 555-0142",
        "HomePhone": "908-555-0199",
        "BusinessPhone": "",
    })
    result = lookup.get_phone("900100001")
    assert result["phone"] is None
    assert result["ambiguous"] is True
    labels = [item["label"] for item in result["candidates"]]
    assert labels == ["Cell", "Home"]
    rendered = str(result)
    assert "732" not in rendered
    assert "908" not in rendered
    assert "0142" not in rendered

    same = EzlynxApplicantPhoneLookup(lambda _applicant_id: {
        "CellPhone": "7325550142",
        "HomePhone": "732-555-0142",
    })
    assert same.get_phone("900100001") == "+17325550142"


def test_unit_files_use_the_venv_python_and_the_requested_cadence():
    assert "User=streetsmart-hermes" in UNIT
    assert "WorkingDirectory=/" in UNIT
    assert "ExecStart=/opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.ezlynx_task_intake" in UNIT
    assert ".hermes" not in UNIT
    assert "TimeoutStartSec=120min" in UNIT
    assert "25" in UNIT
    assert "140" in UNIT
    assert "60" in UNIT
    assert "EnvironmentFile= overrides Environment=" in UNIT
    assert "a file cannot clear them" not in UNIT
    assert "ROBIE_EZLYNX_WRITE_SCOPE=all" in UNIT
    assert "ROBIE_PLAYGROUND=1" in UNIT
    assert "ROBIE_EZLYNX_DRIVER_GATE_REQUIRED=1" in HEALTH_UNIT
    assert "ROBIE_EZLYNX_DRIVER_HOLDER=PRODUCTION" in HEALTH_UNIT
    assert "ROBIE_TASK_INTAKE_CHECK_EFFECTIVE_ENV=1" in HEALTH_UNIT
    assert "Environment=ROBIE_ENV=PRODUCTION" in UNIT
    assert "ROBIE_PHONE_LIVE_CALLS" not in UNIT
    assert "OnUnitActiveSec=5min" in TIMER
    assert "ExecStart=/opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.task_intake_health" in HEALTH_UNIT
    assert "WorkingDirectory=/" in HEALTH_UNIT
    assert "OnUnitActiveSec=30min" in HEALTH_TIMER
    for key in (
        "ROBIE_PHONE_LIVE_CALLS=1",
        "ROBIE_PHONE_REAL_CLIENTS=1",
        "ROBIE_BLAND_ALLOWED_ENVS=PRODUCTION",
        "ROBIE_BLAND_ALLOWED_HOSTS=hermes-poc-01",
        "ROBIE_BLAND_MAX_DURATION_MINUTES=10",
    ):
        assert key in DROPIN
    assert "ROBIE_CALL_SMS_CONFIGURED" not in DROPIN.replace(
        "# ROBIE_CALL_SMS_CONFIGURED is left unset.", ""
    )


def test_test_intake_unit_targets_jake_ferrara_and_leaves_task_ids_empty():
    assert "User=streetsmart-hermes-test" in TEST_UNIT
    assert "Group=streetsmart-hermes-test" in TEST_UNIT
    assert "/opt/streetsmart-hermes-test" in TEST_UNIT
    assert "Environment=ROBIE_ENV=TEST" in TEST_UNIT
    assert "Environment=ROBIE_PHONE_LIVE_CALLS=1" in TEST_UNIT
    assert "Environment=ROBIE_PHONE_REAL_CLIENTS" not in TEST_UNIT
    assert "Environment=ROBIE_SPLICE_TEST_APPLICANT_ID=25486692" in TEST_UNIT
    assert "Environment=ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS=" in TEST_UNIT
    assert "Environment=ROBIE_EZLYNX_DISCUSSION_API=live" in TEST_UNIT
    assert "Environment=ROBIE_EZLYNX_WRITE_APPLICANT_IDS=25486692" in TEST_UNIT
    assert "Environment=ROBIE_EZLYNX_API_PROD_SECRET=projects/751771086524/secrets/ezlynx-api-prod/versions/latest" in TEST_UNIT
    assert "EnvironmentFile=/etc/streetsmart-hermes-test/robie-message-runtime.env" in TEST_UNIT
    assert "EnvironmentFile=/etc/streetsmart-hermes-test/robie-accountability.env" in TEST_UNIT
    assert "robie-ezlynx.env" not in TEST_UNIT
    assert "ezlynx-api-prod" in TEST_UNIT
    assert "robie-test-jake-cell" in TEST_UNIT
    assert "bland-dispatcher-kill-switch" in TEST_UNIT
    assert "robie-test-bland-api-key" in TEST_UNIT
    assert "Unit=robie-task-intake-test.service" in TEST_TIMER
    assert "OnUnitActiveSec=5min" in TEST_TIMER
    assert "25486692" not in UNIT
    assert "ROBIE_PHONE_LIVE_CALLS" not in UNIT


def test_test_intake_unit_never_sets_write_scope_all():
    """Test talks to the Production EZLynx tenant. All-clients would cover every real client."""
    for text in (TEST_UNIT, TEST_TIMER):
        assert "ROBIE_EZLYNX_WRITE_SCOPE=all" not in text
        for line in text.splitlines():
            assignment = line.split("#", 1)[0]
            assert "ROBIE_EZLYNX_WRITE_SCOPE" not in assignment
            assert "ROBIE_PLAYGROUND" not in assignment
    assert "Environment=ROBIE_EZLYNX_WRITE_APPLICANT_IDS=25486692" in TEST_UNIT
    assert "Environment=ROBIE_EZLYNX_WRITE_APPLICANT_IDS=*" not in TEST_UNIT
    assert "220250093" not in TEST_UNIT
    assert "26356199" not in TEST_UNIT


def _hostname_env(env: dict[str, str], directory: Path, name: str) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "hostname"
    script.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' '{name}'\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    updated = dict(env)
    updated["PATH"] = str(directory) + os.pathsep + updated.get("PATH", "")
    return updated


def test_install_test_renders_the_test_unit_without_touching_production(tmp_path):
    log = tmp_path / "systemctl.log"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    env = _hostname_env(os.environ.copy(), tmp_path / "bin", "hermes-test-01")
    env.update({
        "ROBIE_UNIT_DEST": str(tmp_path / "systemd"),
        "ROBIE_RELEASE_ROOT": str(ROOT),
        "ROBIE_SYSTEMCTL": str(systemctl),
        "ROBIE_SYSTEMCTL_LOG": str(log),
        "ROBIE_BACKUP_ROOT": str(tmp_path / "backups"),
    })
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--install-test"],
        check=False, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TEST_INTAKE_INSTALLED" in proc.stdout
    installed = (tmp_path / "systemd" / "robie-task-intake-test.service").read_text(encoding="utf-8")
    assert "Environment=ROBIE_SPLICE_TEST_APPLICANT_ID=25486692" in installed
    assert "Environment=ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS=" in installed
    assert not (tmp_path / "systemd" / "robie-task-intake.service").exists()
    assert "enable --now robie-task-intake-test.timer" in log.read_text(encoding="utf-8")
    rolled = _run("--rollback", tmp_path, log)
    assert rolled.returncode == 0, rolled.stderr
    assert not (tmp_path / "systemd" / "robie-task-intake-test.service").exists()
    assert not (tmp_path / "systemd" / "robie-task-intake-test.timer").exists()
    assert "robie-task-intake-test.timer" in log.read_text(encoding="utf-8")


def test_install_test_refuses_unless_the_host_is_hermes_test_01(tmp_path):
    env = _hostname_env(os.environ.copy(), tmp_path / "bin", "hermes-poc-01")
    env.update({
        "ROBIE_UNIT_DEST": str(tmp_path / "systemd"),
        "ROBIE_RELEASE_ROOT": str(ROOT),
        "ROBIE_SYSTEMCTL": "/bin/true",
    })
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--install-test"],
        check=False, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "hermes-test-01 only" in proc.stderr
    assert not (tmp_path / "systemd" / "robie-task-intake-test.service").exists()


def test_install_test_defaults_to_the_test_release_root(tmp_path):
    env = _hostname_env(os.environ.copy(), tmp_path / "bin", "hermes-test-01")
    env.update({
        "ROBIE_UNIT_DEST": str(tmp_path / "systemd"),
        "ROBIE_SYSTEMCTL": "/bin/true",
    })
    env.pop("ROBIE_RELEASE_ROOT", None)
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--install-test"],
        check=False, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert (
        "/opt/streetsmart-hermes-test/releases/current/deploy/systemd/"
        "robie-task-intake-test.service"
    ) in proc.stderr


def test_test_intake_discussion_client_uses_the_prod_tenant(monkeypatch):
    from robie_job_engine.ezlynx_discussions import DiscussionApiConfig
    from robie_job_engine.ezlynx_task_intake import _build_discussion_client

    seen = {}

    def load_discussion(**kwargs):
        seen["environ"] = dict(kwargs.get("environ") or {})
        return DiscussionApiConfig(
            discussion_base_url="https://app.ezlynx.com/DiscussionApi/",
            token_endpoint="https://app.ezlynx.com/auth/connect/token",
            client_id="id",
            client_secret="secret",
            username="SSRobie",
            integration_group_id="group",
        )

    def refuse_uat(*_args, **_kwargs):
        raise AssertionError("Test intake must not load the UAT EZLynx secret")

    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.delenv("ROBIE_EZLYNX_DISCUSSION_API", raising=False)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_api_only_writes.load_discussion_api_config",
        load_discussion,
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_api.load_ezlynx_api_config",
        refuse_uat,
    )
    client = _build_discussion_client()
    assert client._config.discussion_base_url.startswith("https://app.ezlynx.com/")
    assert seen["environ"]["ROBIE_EZLYNX_DISCUSSION_API"] == "live"
    assert seen["environ"]["ROBIE_ENV"] == "TEST"


def test_installer_requires_root_for_the_real_systemd_dir():
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--enable-timer"],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "sudo" in proc.stderr


def _run(mode: str, root: Path, log: Path) -> subprocess.CompletedProcess[str]:
    systemctl = root / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "if [[ \"$*\" == *show* && \"$*\" == *-p\\ EnvironmentFiles* ]]; then\n"
        "  printf '%s\\n' 'EnvironmentFiles='\n"
        "elif [[ \"$*\" == *show* && \"$*\" == *-p\\ Environment* ]]; then\n"
        "  if [[ -f \"$ROBIE_UNIT_DEST/robie-task-intake.service.d/20-bland-prod.conf\" ]]; then\n"
        "    printf '%s\\n' 'LIVE_ALREADY_PRESENT' >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "  fi\n"
        "  printf '%s\\n' 'Environment=ROBIE_EZLYNX_WRITE_SCOPE=all ROBIE_PLAYGROUND=1'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update({
        "ROBIE_UNIT_DEST": str(root / "systemd"),
        "ROBIE_RELEASE_ROOT": str(ROOT),
        "ROBIE_SYSTEMCTL": str(systemctl),
        "ROBIE_SYSTEMCTL_LOG": str(log),
        "ROBIE_BACKUP_ROOT": str(root / "backups"),
        "ROBIE_TASK_INTAKE_ENV_CHECK": str(root / "env-check.json"),
        "ROBIE_PHONE_LIVE_CALLS": "1",
        "ROBIE_SUPER_SECRET": "super-secret-value-xyz",
    })
    return subprocess.run(
        ["bash", str(INSTALLER), mode],
        check=False, capture_output=True, text=True, env=env,
    )


def test_installer_dry_run_enable_live_and_rollback(tmp_path):
    log = tmp_path / "systemctl.log"
    dry = _run("--dry-run-once", tmp_path, log)
    assert dry.returncode == 0, dry.stderr
    assert "DRY_RUN_ONCE_DONE" in dry.stdout
    assert "super-secret-value-xyz" not in dry.stdout + dry.stderr
    dest = tmp_path / "systemd"
    assert (dest / "robie-task-intake.service").is_file()
    assert (dest / "robie-task-intake-health.timer").is_file()
    assert not (dest / "robie-task-intake.service.d" / "20-bland-prod.conf").exists()
    commands = log.read_text(encoding="utf-8")
    assert "daemon-reload" in commands
    assert "show robie-task-intake.service -p Environment" in commands
    assert "start robie-task-intake.service" in commands
    assert "enable --now" not in commands
    assert not (dest / "robie-task-intake.service.d" / "10-dry-run.conf").exists()
    assert "ROBIE_TASK_INTAKE_DRY_RUN=1" in INSTALLER.read_text(encoding="utf-8")

    log.write_text("", encoding="utf-8")
    enabled = _run("--enable-timer", tmp_path, log)
    assert enabled.returncode == 0, enabled.stderr
    assert "TIMERS_ENABLED" in enabled.stdout
    enabled_commands = log.read_text(encoding="utf-8")
    assert "enable --now robie-task-intake.timer robie-task-intake-health.timer" in enabled_commands
    backups = list((tmp_path / "backups").iterdir())
    assert backups
    assert any((path / "robie-task-intake.service").is_file() for path in backups)

    again = _run("--enable-timer", tmp_path, log)
    assert again.returncode == 0, again.stderr

    live = _run("--live", tmp_path, log)
    assert live.returncode == 0, live.stderr
    assert "LIVE_ALREADY_PRESENT" not in log.read_text(encoding="utf-8")
    assert "show robie-task-intake.service -p EnvironmentFiles" in log.read_text(encoding="utf-8")
    dropin = (dest / "robie-task-intake.service.d" / "20-bland-prod.conf").read_text(encoding="utf-8")
    assert "ROBIE_PHONE_LIVE_CALLS=1" in dropin
    assert "super-secret-value-xyz" not in live.stdout + live.stderr
    refused = _run("--dry-run-once", tmp_path, log)
    assert refused.returncode == 2
    assert "live drop-in" in refused.stderr

    rolled = _run("--rollback", tmp_path, log)
    assert rolled.returncode == 0, rolled.stderr
    assert "ROLLBACK_DONE" in rolled.stdout
    assert not (dest / "robie-task-intake.service").exists()
    assert not (dest / "robie-task-intake.service.d" / "20-bland-prod.conf").exists()
    assert any((path / "robie-task-intake.service").is_file() for path in (tmp_path / "backups").iterdir())


def test_installer_refuses_when_the_effective_env_lacks_write_scope(tmp_path):
    log = tmp_path / "systemctl.log"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "if [[ \"$*\" == *show* ]]; then\n"
        "  printf '%s\\n' 'Environment=ROBIE_ENV=PRODUCTION'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update({
        "ROBIE_UNIT_DEST": str(tmp_path / "systemd"),
        "ROBIE_RELEASE_ROOT": str(ROOT),
        "ROBIE_SYSTEMCTL": str(systemctl),
        "ROBIE_SYSTEMCTL_LOG": str(log),
        "ROBIE_BACKUP_ROOT": str(tmp_path / "backups"),
        "ROBIE_TASK_INTAKE_ENV_CHECK": str(tmp_path / "env-check.json"),
    })
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--enable-timer"],
        check=False, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "ROBIE_EZLYNX_WRITE_SCOPE=all" in proc.stderr
    assert "show robie-task-intake.service -p Environment" in log.read_text(encoding="utf-8")


def _live_systemctl(root: Path, log: Path, *, environment: str, environment_files: str) -> Path:
    systemctl = root / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "if [[ \"$*\" == *show* && \"$*\" == *-p\\ EnvironmentFiles* ]]; then\n"
        f"  printf '%s\\n' 'EnvironmentFiles={environment_files}'\n"
        "elif [[ \"$*\" == *show* && \"$*\" == *-p\\ Environment* ]]; then\n"
        f"  printf '%s\\n' '{environment}'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    return systemctl


def _live_env(root: Path, systemctl: Path, log: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ROBIE_UNIT_DEST": str(root / "systemd"),
        "ROBIE_RELEASE_ROOT": str(ROOT),
        "ROBIE_SYSTEMCTL": str(systemctl),
        "ROBIE_SYSTEMCTL_LOG": str(log),
        "ROBIE_BACKUP_ROOT": str(root / "backups"),
        "ROBIE_TASK_INTAKE_ENV_CHECK": str(root / "env-check.json"),
    })
    return env


def test_live_refuses_before_installing_the_dropin_when_scope_is_missing(tmp_path):
    log = tmp_path / "systemctl.log"
    systemctl = _live_systemctl(
        tmp_path, log,
        environment="Environment=ROBIE_ENV=PRODUCTION",
        environment_files="",
    )
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--live"],
        check=False, capture_output=True, text=True,
        env=_live_env(tmp_path, systemctl, log),
    )
    assert proc.returncode == 2
    assert "ROBIE_EZLYNX_WRITE_SCOPE=all" in proc.stderr
    assert not (tmp_path / "systemd" / "robie-task-intake.service.d" / "20-bland-prod.conf").exists()


def test_live_refuses_when_an_environment_file_sets_scope_or_playground(tmp_path):
    log = tmp_path / "systemctl.log"
    env_file = tmp_path / "override.env"
    env_file.write_text("export ROBIE_PLAYGROUND=1\n", encoding="utf-8")
    systemctl = _live_systemctl(
        tmp_path, log,
        environment="Environment=ROBIE_EZLYNX_WRITE_SCOPE=all",
        environment_files=f"{env_file} (ignore_errors)",
    )
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--live"],
        check=False, capture_output=True, text=True,
        env=_live_env(tmp_path, systemctl, log),
    )
    assert proc.returncode == 2
    assert "an EnvironmentFile sets ROBIE_EZLYNX_WRITE_SCOPE or ROBIE_PLAYGROUND" in proc.stderr
    assert not (tmp_path / "systemd" / "robie-task-intake.service.d" / "20-bland-prod.conf").exists()

    env_file.write_text(
        "# ROBIE_PLAYGROUND=1\n# export ROBIE_EZLYNX_WRITE_SCOPE=\n",
        encoding="utf-8",
    )
    commented = subprocess.run(
        ["bash", str(INSTALLER), "--live"],
        check=False, capture_output=True, text=True,
        env=_live_env(tmp_path, systemctl, log),
    )
    assert commented.returncode == 0, commented.stderr
    assert (tmp_path / "systemd" / "robie-task-intake.service.d" / "20-bland-prod.conf").is_file()


def test_enable_live_and_rollback_remove_a_leftover_dry_run_dropin(tmp_path):
    log = tmp_path / "systemctl.log"
    dest = tmp_path / "systemd" / "robie-task-intake.service.d"
    dest.mkdir(parents=True)
    leftover = dest / "10-dry-run.conf"
    leftover.write_text(
        "[Service]\nEnvironment=ROBIE_TASK_INTAKE_DRY_RUN=1\n", encoding="utf-8",
    )
    enabled = _run("--enable-timer", tmp_path, log)
    assert enabled.returncode == 0, enabled.stderr
    assert not leftover.exists()

    dest.mkdir(parents=True, exist_ok=True)
    leftover.write_text("still here\n", encoding="utf-8")
    live = _run("--live", tmp_path, log)
    assert live.returncode == 0, live.stderr
    assert not leftover.exists()
    assert (dest / "20-bland-prod.conf").is_file()

    dest.mkdir(parents=True, exist_ok=True)
    leftover.write_text("still here\n", encoding="utf-8")
    rolled = _run("--rollback", tmp_path, log)
    assert rolled.returncode == 0, rolled.stderr
    assert not leftover.exists()
    assert not (dest / "20-bland-prod.conf").exists()


def test_installer_notes_say_a_dry_run_needs_a_report_from_the_last_90_minutes():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "within the last 90 minutes" in text
    assert "Re-run the installer after any env-file change" in text


def test_live_refuses_when_the_second_environment_file_sets_playground(tmp_path):
    """Real systemctl output is one prefixed line per file. The second one counts."""
    log = tmp_path / "systemctl.log"
    first = tmp_path / "recording.env"
    second = tmp_path / "accountability.env"
    first.write_text("OTHER=1\n", encoding="utf-8")
    second.write_text("ROBIE_PLAYGROUND=0\n", encoding="utf-8")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "if [[ \"$*\" == *show* && \"$*\" == *-p\\ EnvironmentFiles* ]]; then\n"
        "  printf '%s\\n' 'EnvironmentFiles=/etc/streetsmart-hermes/robie-recording.env (ignore_errors=yes)'\n"
        f"  printf '%s\\n' 'EnvironmentFiles={first} (ignore_errors=yes)'\n"
        f"  printf '%s\\n' 'EnvironmentFiles={second} (ignore_errors=yes)'\n"
        "elif [[ \"$*\" == *show* && \"$*\" == *-p\\ Environment* ]]; then\n"
        "  printf '%s\\n' 'Environment=ROBIE_EZLYNX_WRITE_SCOPE=all'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--live"],
        check=False, capture_output=True, text=True,
        env=_live_env(tmp_path, systemctl, log),
    )
    assert proc.returncode == 2, proc.stderr
    assert "an EnvironmentFile sets ROBIE_EZLYNX_WRITE_SCOPE or ROBIE_PLAYGROUND" in proc.stderr
    assert "an EnvironmentFile is missing" not in proc.stderr
    assert not (tmp_path / "systemd" / "robie-task-intake.service.d" / "20-bland-prod.conf").exists()
    state = json.loads((tmp_path / "env-check.json").read_text(encoding="utf-8"))
    recorded = {item["path"]: item for item in state["files"]}
    assert recorded[str(second)]["assigns"] is True
    assert recorded[str(first)]["assigns"] is False
    assert state["ok"] is False


def test_live_refuses_when_an_environment_file_is_unreadable(tmp_path):
    log = tmp_path / "systemctl.log"
    blocked = tmp_path / "accountability.env"
    blocked.write_text("OTHER=1\n", encoding="utf-8")
    blocked.chmod(0)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "if [[ \"$*\" == *show* && \"$*\" == *-p\\ EnvironmentFiles* ]]; then\n"
        "  printf '%s\\n' 'EnvironmentFiles=/etc/streetsmart-hermes/robie-recording.env (ignore_errors=yes)'\n"
        f"  printf '%s\\n' 'EnvironmentFiles={blocked} (ignore_errors=yes)'\n"
        "elif [[ \"$*\" == *show* && \"$*\" == *-p\\ Environment* ]]; then\n"
        "  printf '%s\\n' 'Environment=ROBIE_EZLYNX_WRITE_SCOPE=all'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    try:
        proc = subprocess.run(
            ["bash", str(INSTALLER), "--live"],
            check=False, capture_output=True, text=True,
            env=_live_env(tmp_path, systemctl, log),
        )
    finally:
        blocked.chmod(0o644)
    assert proc.returncode == 2, proc.stderr
    assert "unreadable" in proc.stderr
    assert "an EnvironmentFile is missing" not in proc.stderr
    assert not (tmp_path / "systemd" / "robie-task-intake.service.d" / "20-bland-prod.conf").exists()
    state = json.loads((tmp_path / "env-check.json").read_text(encoding="utf-8"))
    recorded = {item["path"]: item for item in state["files"]}
    assert recorded[str(blocked)]["readable"] is False
    assert recorded["/etc/streetsmart-hermes/robie-recording.env"]["optional"] is True
    assert state["ok"] is False


def test_live_skips_a_missing_optional_file_and_refuses_a_missing_required_one(tmp_path):
    """systemd 255: optional files have no dash, only (ignore_errors=yes)."""
    log = tmp_path / "systemctl.log"
    present = tmp_path / "accountability.env"
    present.write_text("OTHER=1\n", encoding="utf-8")
    systemctl = tmp_path / "systemctl"
    optional_missing = "/etc/streetsmart-hermes/robie-recording.env"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "if [[ \"$*\" == *show* && \"$*\" == *-p\\ EnvironmentFiles* ]]; then\n"
        f"  printf '%s\\n' 'EnvironmentFiles={optional_missing} (ignore_errors=yes)'\n"
        "  printf '%s\\n' 'EnvironmentFiles=-/etc/streetsmart-hermes/also-optional.env'\n"
        f"  printf '%s\\n' 'EnvironmentFiles={present}'\n"
        "elif [[ \"$*\" == *show* && \"$*\" == *-p\\ Environment* ]]; then\n"
        "  printf '%s\\n' 'Environment=ROBIE_EZLYNX_WRITE_SCOPE=all'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--live"],
        check=False, capture_output=True, text=True,
        env=_live_env(tmp_path, systemctl, log),
    )
    assert proc.returncode == 0, proc.stderr
    assert "an EnvironmentFile is missing" not in proc.stderr
    assert (tmp_path / "systemd" / "robie-task-intake.service.d" / "20-bland-prod.conf").is_file()
    state = json.loads((tmp_path / "env-check.json").read_text(encoding="utf-8"))
    recorded = {item["path"]: item for item in state["files"]}
    assert recorded[optional_missing]["optional"] is True
    assert recorded[optional_missing]["readable"] is False
    assert state["ok"] is True

    dropin = tmp_path / "systemd" / "robie-task-intake.service.d" / "20-bland-prod.conf"
    dropin.unlink()
    required_missing = "/etc/streetsmart-hermes/robie-required.env"
    systemctl.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ROBIE_SYSTEMCTL_LOG\"\n"
        "if [[ \"$*\" == *show* && \"$*\" == *-p\\ EnvironmentFiles* ]]; then\n"
        f"  printf '%s\\n' 'EnvironmentFiles={required_missing}'\n"
        "elif [[ \"$*\" == *show* && \"$*\" == *-p\\ Environment* ]]; then\n"
        "  printf '%s\\n' 'Environment=ROBIE_EZLYNX_WRITE_SCOPE=all'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)
    refused = subprocess.run(
        ["bash", str(INSTALLER), "--live"],
        check=False, capture_output=True, text=True,
        env=_live_env(tmp_path, systemctl, log),
    )
    assert refused.returncode == 2, refused.stderr
    assert f"an EnvironmentFile is missing: {required_missing}" in refused.stderr
    assert not dropin.exists()
