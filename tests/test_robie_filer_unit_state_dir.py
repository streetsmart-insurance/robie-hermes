"""robie-filer.service must start on a fresh host with no /var/lib/robie-filer.

2026-10-09 08:34 ET, hermes-poc-01: ``ReadWritePaths=/var/lib/robie-filer``
named a folder that did not exist yet, so systemd failed the unit with
226/NAMESPACE before the ``ExecStartPre`` that would have created it ran.
The unit now uses ``StateDirectory=robie-filer``: systemd creates the folder
(owner ``User=``, mode 0700) before it builds the sandbox and makes it
writable under ``ProtectSystem=strict``.

The static tests run everywhere. ``test_fresh_host_starts_under_real_systemd``
runs the unit's own sandbox settings under real systemd. It needs root
systemd (passwordless sudo, as on GitHub's ubuntu runners) and runs only
with ROBIE_SYSTEMD_TESTS=1. It never touches /var/lib/robie-filer: the state
folder name is rewritten to a throwaway one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy" / "systemd" / "robie-filer.service"
STATE = "/var/lib/robie-filer"


def _lines() -> list[str]:
    return [line.strip() for line in UNIT_PATH.read_text().splitlines()]


def _values(key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in _lines() if line.startswith(f"{key}=")]


def test_state_directory_is_managed_by_systemd():
    assert _values("StateDirectory") == ["robie-filer"]
    assert _values("StateDirectoryMode") == ["0700"]
    assert _values("User") == ["streetsmart-hermes"]


def test_no_mandatory_path_that_a_fresh_host_lacks():
    # A plain ReadWritePaths/ReadOnlyPaths entry must already exist when the
    # namespace is built; the state folder does not on a fresh host.
    for key in ("ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "BindPaths"):
        for value in _values(key):
            for path in value.split():
                assert not (path.startswith(STATE) and not path.startswith("-")), (key, path)


def test_sandbox_is_kept():
    assert _values("ProtectSystem") == ["strict"]
    assert _values("ProtectHome") == ["read-only"]
    assert _values("PrivateTmp") == ["true"]
    assert _values("NoNewPrivileges") == ["true"]
    assert _values("UMask") == ["0077"]


def test_no_exec_start_pre_creates_the_state_folder():
    for value in _values("ExecStartPre"):
        assert not ("install -d" in value and STATE in value), value


def test_paths_the_job_uses_live_in_the_state_directory():
    env = dict(v.split("=", 1) for v in _values("Environment") if "=" in v)
    assert env["ROBIE_FILER_STATE_DIR"] == STATE
    assert env["ROBIE_FILER_INDEX_CSV"].startswith(STATE + "/")


def _ci_unit(name: str, state: str) -> str:
    """The real unit with only host-specific lines swapped for the CI host."""
    out = []
    for raw in UNIT_PATH.read_text().splitlines():
        line = raw.strip()
        if line.startswith(("User=", "Group=")):
            out.append(line.split("=", 1)[0] + "=nobody" if line.startswith("User=") else "Group=nogroup")
        elif line.startswith("WorkingDirectory="):
            out.append("WorkingDirectory=/")
        elif line.startswith("ExecStart="):
            out.append(
                # The state folder exists and is writable; the rest of the
                # file system stays read-only (ProtectSystem=strict).
                "ExecStart=/bin/sh -c 'test -d /var/lib/%s && ! touch /usr/robie-filer-ci-escape 2>/dev/null "
                "&& echo ok > /var/lib/%s/probe'" % (state, state)
            )
        elif line.startswith("StateDirectory="):
            out.append(f"StateDirectory={state}")
        else:
            out.append(raw.replace(STATE, f"/var/lib/{state}"))
    return "\n".join(out) + "\n"


@pytest.mark.skipif(os.environ.get("ROBIE_SYSTEMD_TESTS") != "1", reason="needs root systemd (CI sets ROBIE_SYSTEMD_TESTS=1)")
def test_fresh_host_starts_under_real_systemd(tmp_path):
    tag = uuid.uuid4().hex[:8]
    name = f"robie-filer-ci-{tag}.service"
    state = f"robie-filer-ci-{tag}"
    unit_file = Path("/run/systemd/system") / name
    sudo = ["sudo", "-n"]

    def run(*args, check=True):
        return subprocess.run([*sudo, *args], capture_output=True, text=True, check=check)

    assert run("test", "!", "-e", f"/var/lib/{state}", check=False).returncode == 0
    src = tmp_path / name
    src.write_text(_ci_unit(name, state))
    try:
        run("install", "-m", "0644", str(src), str(unit_file))
        run("systemctl", "daemon-reload")
        started = run("systemctl", "start", name, check=False)
        journal = run("journalctl", "-u", name, "--no-pager", "-o", "cat", check=False).stdout
        assert started.returncode == 0, f"{started.stderr}\n{journal}"
        stat = run("stat", "-c", "%U %a", f"/var/lib/{state}").stdout.split()
        assert stat == ["nobody", "700"], stat
        assert run("cat", f"/var/lib/{state}/probe").stdout.strip() == "ok"
    finally:
        run("systemctl", "stop", name, check=False)
        run("rm", "-f", str(unit_file), check=False)
        run("systemctl", "daemon-reload", check=False)
        run("systemctl", "reset-failed", name, check=False)
        run("rm", "-rf", f"/var/lib/{state}", f"/var/lib/private/{state}", check=False)


@pytest.mark.skipif(os.environ.get("ROBIE_SYSTEMD_TESTS") != "1", reason="needs root systemd (CI sets ROBIE_SYSTEMD_TESTS=1)")
def test_old_readwritepaths_form_fails_on_fresh_host(tmp_path):
    """Control: the 9c6701e8 form reproduces the Production 226/NAMESPACE failure."""
    tag = uuid.uuid4().hex[:8]
    name = f"robie-filer-ci-old-{tag}.service"
    state = f"robie-filer-ci-old-{tag}"
    text = _ci_unit(name, state)
    text = text.replace(f"StateDirectory={state}\n", "").replace("StateDirectoryMode=0700\n", "")
    text = text.replace("ProtectSystem=strict\n", f"ProtectSystem=strict\nReadWritePaths=/var/lib/{state}\n")
    unit_file = Path("/run/systemd/system") / name
    src = tmp_path / name
    src.write_text(text)

    def run(*args, check=True):
        return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, check=check)

    try:
        run("install", "-m", "0644", str(src), str(unit_file))
        run("systemctl", "daemon-reload")
        assert run("systemctl", "start", name, check=False).returncode != 0
        status = run("systemctl", "show", name, "-p", "ExecMainStatus", "-p", "Result", check=False).stdout
        journal = run("journalctl", "-u", name, "--no-pager", "-o", "cat", check=False).stdout
        assert "226" in journal or "NAMESPACE" in journal or "namespac" in journal.lower(), status + journal
    finally:
        run("rm", "-f", str(unit_file), check=False)
        run("systemctl", "daemon-reload", check=False)
        run("systemctl", "reset-failed", name, check=False)
        run("rm", "-rf", f"/var/lib/{state}", check=False)
