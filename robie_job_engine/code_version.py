"""Honest running-code identity for job reports and health checks.

CODE_VERSION used to be a hand-edited SHA. That marker stayed at PR 370
(712cdb0a) after 375 (c166a9bb) was live, so a later zip could lie.

This module derives the version from, in order:

1. The loaded file path, if it lives under a release directory named with
   the commit (``.../releases/<sha>/robie-hermes-<sha>/...``).
2. The deployed release pointer (``ROBIE_CANONICAL_JOB_ENGINE_ROOT``,
   then ``releases/current`` / ``current`` on Test and Production).
3. ``git rev-parse HEAD`` of the source tree (dev / CI). Git is not
   allowed to walk out of a release extract into some other repo.
4. ``unknown`` — never a leftover hardcoded SHA.

A zip extracted as ``releases/c166a9bbb8b7/robie-hermes-c166a9bbb8b7``
therefore reports ``c166a9bbb8b7``. The hourly health check compares
``CODE_VERSION[:8]`` to that release dir; a derived value cannot stay at
370 while 375 is live.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path


UNKNOWN_CODE_VERSION = "unknown"

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_RELEASE_DIR_RE = re.compile(
    r"(?:^|/)releases/([0-9a-f]{7,40})(?:/|$)",
    re.IGNORECASE,
)
_HERMES_PREFIX_RE = re.compile(
    r"(?:^|/)robie-hermes-([0-9a-f]{7,40})(?:/|$)",
    re.IGNORECASE,
)

DEFAULT_RELEASE_POINTERS: tuple[str, ...] = (
    "/opt/streetsmart-hermes/releases/current",
    "/opt/streetsmart-hermes/current",
    "/opt/streetsmart-hermes-test/releases/current",
    "/opt/streetsmart-hermes-test/current",
)

GitRunner = Callable[[Path], str | None]


def _normalize_sha(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text or not _SHA_RE.fullmatch(text):
        return None
    return text


def sha_from_release_path(path: str | Path) -> str | None:
    """Extract the release commit from a deployed zip path."""
    text = str(path).replace("\\", "/")
    found: list[str] = []
    match = _RELEASE_DIR_RE.search(text)
    if match:
        found.append(match.group(1).lower())
    match = _HERMES_PREFIX_RE.search(text)
    if match:
        found.append(match.group(1).lower())
    if not found:
        return None
    return max(found, key=len)


def _resolved(path: str | Path) -> Path | None:
    try:
        return Path(path).resolve()
    except OSError:
        return None


def _sha_from_pointer(pointer: str | Path) -> str | None:
    raw = Path(pointer)
    try:
        exists = raw.is_symlink() or raw.exists()
    except OSError:
        return None
    if not exists:
        return None
    resolved = _resolved(raw)
    if resolved is None:
        return None
    return sha_from_release_path(resolved)


def _find_git_root(start: Path) -> Path | None:
    """Find .git for this tree. Do not walk out of a release extract."""
    for parent in (start, *start.parents):
        name = parent.name
        if name.startswith("robie-hermes-") and _SHA_RE.fullmatch(
            name.removeprefix("robie-hermes-")
        ):
            return None
        if parent.name == "releases" or parent.parent.name == "releases":
            return None
        if (parent / ".git").exists():
            return parent
    return None


def _git_head(cwd: Path, run_git: GitRunner | None = None) -> str | None:
    if run_git is not None:
        return _normalize_sha(run_git(cwd))
    root = _find_git_root(cwd)
    if root is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _normalize_sha(proc.stdout)


def resolve_code_version(
    *,
    source_file: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    pointer_paths: Sequence[str | Path] | None = None,
    run_git: GitRunner | None = None,
) -> str:
    """Return the SHA of the code that is actually loaded.

    Never returns a leftover hardcoded marker. Prefer the deployed
    release path / pointer; fall back to git; otherwise ``unknown``.
    """
    path = _resolved(source_file or __file__)
    if path is not None:
        from_file = sha_from_release_path(path)
        if from_file:
            return from_file

    environ = env if env is not None else os.environ
    env_pointers: list[str | Path] = []
    for key in ("ROBIE_CANONICAL_JOB_ENGINE_ROOT", "ROBIE_JOB_ENGINE_ROOT"):
        value = str(environ.get(key) or "").strip()
        if value:
            env_pointers.append(value)

    pointers: list[str | Path] = list(env_pointers)
    if pointer_paths is None:
        pointers.extend(DEFAULT_RELEASE_POINTERS)
    else:
        pointers.extend(pointer_paths)

    seen: set[str] = set()
    for pointer in pointers:
        key = str(pointer)
        if key in seen:
            continue
        seen.add(key)
        from_pointer = _sha_from_pointer(pointer)
        if from_pointer:
            return from_pointer

    git_cwd = path if path is not None else Path(__file__).resolve()
    from_git = _git_head(git_cwd, run_git)
    if from_git:
        return from_git

    return UNKNOWN_CODE_VERSION
