#!/usr/bin/env python3
"""Keep Magellan storage-state private without defeating dual-writer ACLs.

``chmod 0600`` sets the ACL mask to the group bits (``---``). Named entries
for ``ubuntu`` and the hermes-poc OS Login user stay in the ACL list, but
their effective rights become none. The next writer then hits
``PermissionError``.

``os`` and ``pathlib`` cannot name those users. After the private chmod,
``setfacl`` re-applies both ``rw`` entries and an ``rw`` mask. The parent
directory is not modified, so its default ACL stays as the host left it.
A missing ``setfacl`` binary is ignored so unit tests and hosts without the
tool still finish the private write. A present ``setfacl`` that fails raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


# ubuntu runs the 9am digest. sa_112650695780807418521 is the hermes-poc
# OS Login identity that also refreshes this file.
DUAL_WRITERS = ("ubuntu", "sa_112650695780807418521")


def _chown_ubuntu_if_root(path) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    import pwd

    try:
        user = pwd.getpwnam("ubuntu")
    except KeyError:
        return
    os.chown(os.fspath(path), user.pw_uid, user.pw_gid)


def _effective_acl(path) -> dict[str, str]:
    getfacl = shutil.which("getfacl")
    if not getfacl:
        return {}
    completed = subprocess.run(
        [getfacl, "-e", "-c", os.fspath(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {}
    entries: dict[str, str] = {}
    for raw in completed.stdout.splitlines():
        body, _, comment = raw.partition("#")
        body = body.strip()
        if not body or ":" not in body:
            continue
        tag, perms = body.rsplit(":", 1)
        effective = perms.strip()
        if "effective:" in comment:
            effective = comment.split("effective:", 1)[1].strip()
        entries[tag] = effective
    return entries


def storage_state_is_dual_writer_private(path, writers=None) -> bool:
    """True when both writers can read/write and other users cannot.

    A ``chmod 0600`` that left named ACL entries behind a ``---`` mask is
    not private-for-both: the entries exist and their effective rights do
    not. Linux stores the ACL mask in the group mode bits, so a restored
    file may stat as ``0660`` while ``group::`` and ``other::`` stay ``---``.
    """
    if writers is None:
        writers = DUAL_WRITERS
    mode = Path(path).stat().st_mode & 0o777
    if mode & 0o007:
        return False
    entries = _effective_acl(path)
    if not entries:
        return mode == 0o600
    named = [entries.get(f"user:{user}") for user in writers]
    if all(value is None for value in named):
        return mode == 0o600
    mask = entries.get("mask:")
    if mask is None or "r" not in mask or "w" not in mask:
        return False
    if any(value is None or "r" not in value or "w" not in value for value in named):
        return False
    other = entries.get("other:", "---")
    return "r" not in other and "w" not in other and "x" not in other


def restore_dual_writer_acls(path, writers=None) -> bool:
    """Re-apply named rw ACLs after chmod cleared the mask.

    Returns True when setfacl succeeded. Returns False when setfacl is not
    installed. Raises RuntimeError when setfacl runs and fails.
    Imports stay inside the function so the dedicated-extractor repair can
    paste this source into a module that does not import shutil itself.
    """
    import shutil
    import subprocess

    if writers is None:
        writers = DUAL_WRITERS
    setfacl = shutil.which("setfacl")
    if not setfacl:
        return False
    spec = ",".join(f"u:{user}:rw" for user in writers) + ",m::rw"
    completed = subprocess.run(
        [setfacl, "-m", spec, os.fspath(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"setfacl failed to restore Magellan session ACLs: {detail}")
    return True


def publish_private_storage_state(destination, write, writers=None) -> None:
    """Write storage state in the same directory, then chmod and restore ACLs.

    ``write`` receives the temporary path and must create that file. The
    temporary name is replaced onto ``destination`` before the private chmod,
    so a failed write leaves the previous session file in place.
    """
    if writers is None:
        writers = DUAL_WRITERS
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        write(temporary)
        if not temporary.is_file():
            raise RuntimeError("Magellan storage state write did not create a file")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    os.chmod(destination, 0o600)
    _chown_ubuntu_if_root(destination)
    restore_dual_writer_acls(destination, writers)
