#!/usr/bin/env python3
"""Restore dual-writer ACLs after Magellan storage-state chmod 0600.

The dedicated extractor is not in this monorepo. On
streetsmart-accountability-prod, ``src/extractors/magellan_playwright.py``
writes ``data/magellan_storage_state.json`` and then ``os.chmod(..., 0o600)``.
That chmod clears the ACL mask, so the named entries for ubuntu and the
hermes-poc OS Login user (``sa_112650695780807418521``) stop being effective.

The observed save site is ``_ensure_authenticated``. Some copies name it
``_save_session``. This repair rewrites either exact site to:

    write temp in the same directory -> os.replace -> chmod 0600 -> setfacl

The inserted helpers are the source of ``scripts/magellan_session_acl.py``.
The daily service does not import this repo, so the patched extractor is
self-contained. The parent directory ACL is not touched.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import os
import shutil
from pathlib import Path


BLOCK_MKDIR_CONDITIONAL = (
    "    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)\n"
    "    context.storage_state(path=str(STORAGE_STATE_PATH))\n"
    "    if STORAGE_STATE_PATH.exists():\n"
    "        os.chmod(STORAGE_STATE_PATH, 0o600)\n"
)
BLOCK_CONDITIONAL = (
    "    context.storage_state(path=str(STORAGE_STATE_PATH))\n"
    "    if STORAGE_STATE_PATH.exists():\n"
    "        os.chmod(STORAGE_STATE_PATH, 0o600)\n"
)
BLOCK_MKDIR_DIRECT = (
    "    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)\n"
    "    context.storage_state(path=str(STORAGE_STATE_PATH))\n"
    "    os.chmod(STORAGE_STATE_PATH, 0o600)\n"
)
BLOCK_DIRECT = (
    "    context.storage_state(path=str(STORAGE_STATE_PATH))\n"
    "    os.chmod(STORAGE_STATE_PATH, 0o600)\n"
)
BLOCKS = (
    BLOCK_MKDIR_CONDITIONAL,
    BLOCK_CONDITIONAL,
    BLOCK_MKDIR_DIRECT,
    BLOCK_DIRECT,
)
CHMOD_MARK = "os.chmod(STORAGE_STATE_PATH, 0o600)"
HELPER_MARK = "def publish_private_storage_state("
SAVE_CALL = "    _save_session(context)\n"
PUBLISH_CALL = (
    "    publish_private_storage_state(\n"
    "        STORAGE_STATE_PATH,\n"
    "        lambda temporary: context.storage_state(path=str(temporary)),\n"
    "    )\n"
)
SAVE_SESSION = '''def _save_session(context) -> None:
    """Persist Magellan storage state without dropping dual-writer ACLs."""
    publish_private_storage_state(
        STORAGE_STATE_PATH,
        lambda temporary: context.storage_state(path=str(temporary)),
    )
'''


def _load_acl():
    path = Path(__file__).resolve().parent / "magellan_session_acl.py"
    spec = importlib.util.spec_from_file_location("magellan_session_acl", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Magellan session ACL helper is missing: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def helper_source() -> str:
    module = _load_acl()
    parts = [
        f"DUAL_WRITERS = {module.DUAL_WRITERS!r}\n",
        inspect.getsource(module._chown_ubuntu_if_root),
        inspect.getsource(module.restore_dual_writer_acls),
        inspect.getsource(module.publish_private_storage_state),
    ]
    return "\n".join(parts)


def _longest_blocks(source: str) -> list[str]:
    found = [block for block in BLOCKS if block in source]
    return [
        block
        for block in found
        if not any(block != other and block in other for other in found)
    ]


def _inside_save_session(source: str, index: int) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_save_session":
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        # ast lines are 1-based. Convert the source index to a line number.
        line = source.count("\n", 0, index) + 1
        if start <= line <= end:
            return True
    return False


def _insert_before_anchor(source: str, addition: str) -> str:
    anchors = []
    for name in (
        "def _ensure_authenticated(",
        "def _save_session(",
        "def fetch_live_magellan_data(",
    ):
        index = source.find(name)
        if index != -1:
            anchors.append(index)
    if not anchors:
        return source.rstrip() + "\n\n" + addition.rstrip() + "\n"
    index = min(anchors)
    return source[:index] + addition.rstrip() + "\n\n" + source[index:]


def rewrite_source(original: str) -> tuple[str, str]:
    if HELPER_MARK in original and CHMOD_MARK not in original:
        return "already_repaired", original
    if HELPER_MARK in original and CHMOD_MARK in original:
        raise RuntimeError("refusing partial Magellan session ACL repair")
    blocks = _longest_blocks(original)
    if not blocks:
        raise RuntimeError("refusing unexpected Magellan session save shape")
    if original.count(CHMOD_MARK) != sum(original.count(block) for block in blocks):
        raise RuntimeError("refusing unexpected Magellan session chmod shape")

    updated = original
    matches: list[tuple[int, str]] = []
    for block in blocks:
        start = 0
        while True:
            index = updated.find(block, start)
            if index < 0:
                break
            matches.append((index, block))
            start = index + len(block)
    if not matches:
        raise RuntimeError("refusing unexpected Magellan session save shape")

    for index, block in sorted(matches, reverse=True):
        replacement = PUBLISH_CALL if _inside_save_session(updated, index) else SAVE_CALL
        updated = updated[:index] + replacement + updated[index + len(block) :]

    addition = helper_source()
    if "def _save_session(" not in updated:
        addition = addition.rstrip() + "\n\n" + SAVE_SESSION
    updated = _insert_before_anchor(updated, addition)
    compile(updated, "<magellan_playwright>", "exec")
    if updated.count(HELPER_MARK) != 1 or updated.count("def _save_session(") != 1:
        raise RuntimeError("Magellan session ACL repair did not install one save helper")
    if CHMOD_MARK in updated:
        raise RuntimeError("Magellan session ACL repair left chmod 0600 in place")
    if "sa_112650695780807418521" not in updated:
        raise RuntimeError("Magellan session ACL repair did not retain the OS Login writer")
    return "repaired", updated


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    status, updated = rewrite_source(original)
    if status == "already_repaired":
        return status

    backup = path.with_suffix(path.suffix + ".pre-magellan-session-acl")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if verified != updated:
        raise RuntimeError("Magellan session ACL repair verification failed")
    compile(verified, str(path), "exec")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
