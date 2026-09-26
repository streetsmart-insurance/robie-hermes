import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.magellan_session_acl import (
    DUAL_WRITERS,
    _effective_acl,
    publish_private_storage_state,
    restore_dual_writer_acls,
    storage_state_is_dual_writer_private,
)
from scripts.repair_dedicated_accountability_magellan_session_acl import (
    BLOCK_MKDIR_CONDITIONAL,
    BLOCK_MKDIR_DIRECT,
    repair,
)


def _resolvable_writers() -> tuple[str, str]:
    import pwd

    names: list[str] = []
    current = pwd.getpwuid(os.geteuid()).pw_name
    for candidate in (current, "ubuntu", "root"):
        try:
            pwd.getpwnam(candidate)
        except KeyError:
            continue
        if candidate not in names:
            names.append(candidate)
        if len(names) == 2:
            return names[0], names[1]
    pytest.skip("need two resolvable users to exercise setfacl")


def _parent_acl(path: Path) -> str:
    return subprocess.check_output(["getfacl", "-c", os.fspath(path)], text=True)


def _write_json(temporary: Path) -> None:
    Path(temporary).write_text('{"cookies":[]}', encoding="utf-8")


@pytest.mark.skipif(shutil.which("setfacl") is None or shutil.which("getfacl") is None, reason="setfacl not installed")
def test_chmod_600_does_not_leave_acl_mask_empty(tmp_path: Path):
    writers = _resolvable_writers()
    parent = tmp_path / "data"
    parent.mkdir()
    spec = ",".join(f"u:{user}:rw" for user in writers) + ",m::rw"
    subprocess.check_call(["setfacl", "-d", "-m", spec, os.fspath(parent)])
    parent_before = _parent_acl(parent)

    broken = parent / "chmod-only.json"
    broken.write_text("{}", encoding="utf-8")
    subprocess.check_call(["setfacl", "-m", spec, os.fspath(broken)])
    os.chmod(broken, 0o600)
    defeated = _effective_acl(broken)
    assert defeated["mask:"] == "---"
    for user in writers:
        assert defeated[f"user:{user}"] == "---"
    assert storage_state_is_dual_writer_private(broken, writers) is False

    destination = parent / "magellan_storage_state.json"
    seen: dict[str, Path] = {}

    def write(temporary: Path) -> None:
        seen["path"] = Path(temporary)
        _write_json(temporary)

    publish_private_storage_state(destination, write, writers=writers)

    assert seen["path"].parent == parent
    assert seen["path"].name == "magellan_storage_state.json.tmp"
    assert not seen["path"].exists()
    assert destination.read_text(encoding="utf-8") == '{"cookies":[]}'
    assert destination.stat().st_mode & 0o007 == 0
    restored = _effective_acl(destination)
    assert "r" in restored["mask:"] and "w" in restored["mask:"]
    for user in writers:
        assert "r" in restored[f"user:{user}"] and "w" in restored[f"user:{user}"]
    assert "r" not in restored["other:"] and "w" not in restored["other:"]
    assert storage_state_is_dual_writer_private(destination, writers) is True
    assert _parent_acl(parent) == parent_before


def test_publish_invokes_setfacl_for_ubuntu_and_oslogin_after_chmod(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "setfacl.log"
    script = bin_dir / "setfacl"
    script.write_text(
        "#!/bin/sh\n"
        "last=\n"
        "for arg in \"$@\"; do last=\"$arg\"; done\n"
        "mode=$(stat -c %a \"$last\")\n"
        "printf '%s mode=%s\\n' \"$*\" \"$mode\" >> \"$SETFACL_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("SETFACL_LOG", str(log))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    destination = tmp_path / "magellan_storage_state.json"
    chmod_paths: list[Path] = []
    real_chmod = os.chmod

    def tracking_chmod(path, mode):
        chmod_paths.append(Path(path))
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", tracking_chmod)
    publish_private_storage_state(destination, _write_json)

    recorded = log.read_text(encoding="utf-8")
    assert "u:ubuntu:rw" in recorded
    assert "u:sa_112650695780807418521:rw" in recorded
    assert "m::rw" in recorded
    assert "mode=600" in recorded
    assert destination in chmod_paths
    assert tmp_path not in chmod_paths
    assert DUAL_WRITERS == ("ubuntu", "sa_112650695780807418521")


def test_missing_setfacl_keeps_owner_only_mode(tmp_path: Path, monkeypatch):
    real_which = shutil.which

    def which(name):
        if name == "setfacl":
            return None
        return real_which(name)

    monkeypatch.setattr(shutil, "which", which)
    destination = tmp_path / "magellan_storage_state.json"
    publish_private_storage_state(destination, _write_json)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert restore_dual_writer_acls(destination) is False
    assert storage_state_is_dual_writer_private(destination) is True


def test_setfacl_failure_raises_without_touching_parent(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "setfacl"
    script.write_text("#!/bin/sh\necho 'acl denied' >&2\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    destination = tmp_path / "magellan_storage_state.json"
    with pytest.raises(RuntimeError, match="setfacl failed to restore Magellan session ACLs"):
        publish_private_storage_state(destination, _write_json)
    assert destination.is_file()
    assert not (tmp_path / "magellan_storage_state.json.tmp").exists()


def test_failed_write_preserves_previous_session(tmp_path: Path):
    destination = tmp_path / "magellan_storage_state.json"
    destination.write_text("old", encoding="utf-8")
    destination.chmod(0o600)

    def write(temporary: Path) -> None:
        Path(temporary).write_text("new", encoding="utf-8")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        publish_private_storage_state(destination, write)
    assert destination.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob("*.tmp")) == []


def test_root_chowns_ubuntu_before_setfacl(tmp_path: Path, monkeypatch):
    import pwd

    events: list[tuple] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def chown(path, uid, gid):
        events.append(("chown", uid, gid))

    monkeypatch.setattr(os, "chown", chown)
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: type("User", (), {"pw_uid": 111, "pw_gid": 222})()
        if name == "ubuntu"
        else (_ for _ in ()).throw(KeyError(name)),
    )

    def fake_run(cmd, **kwargs):
        events.append(("setfacl", tuple(cmd)))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    publish_private_storage_state(tmp_path / "magellan_storage_state.json", _write_json)
    assert ("chown", 111, 222) in events
    assert any(item[0] == "setfacl" for item in events)
    assert events.index(("chown", 111, 222)) < next(
        index for index, item in enumerate(events) if item[0] == "setfacl"
    )


def _live_authenticated_source() -> str:
    return (
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "STORAGE_STATE_PATH = Path('data/magellan_storage_state.json')\n"
        "\n"
        "def _ensure_authenticated(page, context) -> None:\n"
        "    page.goto('https://app.magellan.insure/dashboard')\n"
        "    if '/login' not in page.url.lower():\n"
        "        return\n"
        + BLOCK_MKDIR_CONDITIONAL
        + "\n"
        "def fetch_live_magellan_data(target_date: str):\n"
        "    return target_date\n"
    )


def _save_session_source() -> str:
    return (
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "STORAGE_STATE_PATH = Path('data/magellan_storage_state.json')\n"
        "\n"
        "def _save_session(context) -> None:\n"
        + BLOCK_MKDIR_DIRECT
        + "\n"
        "def fetch_live_magellan_data(target_date: str):\n"
        "    return target_date\n"
    )


def _exec_save(source: str, destination: Path, writers: tuple[str, ...] | None):
    namespace: dict = {}
    exec(compile(source, "<magellan>", "exec"), namespace)
    namespace["STORAGE_STATE_PATH"] = destination
    if writers is not None:
        namespace["DUAL_WRITERS"] = writers
    seen: dict[str, Path] = {}

    class Context:
        def storage_state(self, path):
            seen["path"] = Path(path)
            Path(path).write_text('{"cookies":[]}', encoding="utf-8")

    namespace["_save_session"](Context())
    return seen


def test_repair_live_authenticated_save_restores_acl(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text(_live_authenticated_source(), encoding="utf-8")
    target.chmod(0o644)

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert "os.chmod(STORAGE_STATE_PATH, 0o600)" not in repaired
    assert repaired.count("def _save_session(") == 1
    assert "if '/login' not in page.url.lower():\n        return\n    _save_session(context)" in repaired
    assert "sa_112650695780807418521" in repaired
    assert target.with_suffix(".py.pre-magellan-session-acl").is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644

    destination = tmp_path / "data" / "magellan_storage_state.json"
    writers = None
    if shutil.which("setfacl") and shutil.which("getfacl"):
        writers = _resolvable_writers()
    seen = _exec_save(repaired, destination, writers)
    assert seen["path"].parent == destination.parent
    assert destination.is_file()
    assert not list(destination.parent.glob("*.tmp"))
    if writers is not None:
        assert storage_state_is_dual_writer_private(destination, writers) is True
        assert _effective_acl(destination)["mask:"] != "---"
    else:
        assert destination.stat().st_mode & 0o777 == 0o600

    assert repair(target) == "already_repaired"


def test_repair_existing_save_session_and_is_idempotent(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text(_save_session_source(), encoding="utf-8")

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert repaired.count("def _save_session(") == 1
    assert "publish_private_storage_state(" in repaired
    assert "os.chmod(STORAGE_STATE_PATH, 0o600)" not in repaired
    assert repair(target) == "already_repaired"


def test_repair_refuses_unknown_and_partial_shapes(tmp_path: Path):
    unknown = tmp_path / "unknown.py"
    unknown.write_text("def fetch_live_magellan_data():\n    return None\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected Magellan session save shape"):
        repair(unknown)

    partial = tmp_path / "partial.py"
    partial.write_text(
        "def publish_private_storage_state(destination, write, writers=None):\n"
        "    return None\n"
        "os.chmod(STORAGE_STATE_PATH, 0o600)\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="partial Magellan session ACL repair"):
        repair(partial)
