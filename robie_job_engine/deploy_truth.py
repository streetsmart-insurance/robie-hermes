"""One source of truth for what Production Chat and Job Engine loaded.

Hermes still runs. Skills still live in ``.hermes``. The gap this module
closes is Chat adapter / Playwright tools silently disagreeing with the
zip SHA after a pointer flip.

A zip pointer match is not live. Official install is one script: copy every
Chat-loaded overlay (or a zip-load shim), flip both pointers, then refuse
``done`` until the running dest equals that zip. User-owned Loom skills
(including ``ascend-finance``) are not part of this proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


class DeployTruthError(RuntimeError):
    """Official install or Chat-runtime proof failed. Not done."""


ZIP_LOAD_MARKER = "ROBIE_ZIP_LOAD_PATH"
DEFAULT_OPT_ROOT = Path("/opt/streetsmart-hermes")
DEFAULT_HERMES_HOME = DEFAULT_OPT_ROOT / ".hermes"
USER_OWNED_SKILLS = ("ascend-finance",)
SKILL_DIR_NAME = "skills"
PROOF_FILENAME = "official-install-proof.json"
DONE_BANNER = "OFFICIAL INSTALL DONE"
NOT_DONE_BANNER = "OFFICIAL INSTALL NOT DONE"

@dataclass(frozen=True)
class ChatRuntimeFile:
    """One Hermes-loaded file that must equal the zip (or load it)."""

    name: str
    zip_relpath: str
    dest_relpath: str


# Chat / Playwright files Hermes loads from .hermes. Skills stay out.
CHAT_RUNTIME_FILES: tuple[ChatRuntimeFile, ...] = (
    ChatRuntimeFile(
        name="google-chat-adapter",
        zip_relpath="integrations/google_chat/adapter.py",
        dest_relpath="hermes-agent/plugins/platforms/google_chat/adapter.py",
    ),
    ChatRuntimeFile(
        name="google-chat-oauth",
        zip_relpath="integrations/google_chat/oauth.py",
        dest_relpath="hermes-agent/plugins/platforms/google_chat/oauth.py",
    ),
    ChatRuntimeFile(
        name="playwright-tool",
        zip_relpath="deploy/hermes/tools/playwright_tool.py",
        dest_relpath="hermes-agent/tools/playwright_tool.py",
    ),
    ChatRuntimeFile(
        name="playwright-write-guard",
        zip_relpath="deploy/hermes/tools/playwright_write_guard.py",
        dest_relpath="hermes-agent/tools/playwright_write_guard.py",
    ),
    ChatRuntimeFile(
        name="gemini-field-tool",
        zip_relpath="deploy/hermes/tools/gemini_field_tool.py",
        dest_relpath="hermes-agent/tools/gemini_field_tool.py",
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_path(value: str | Path | None, default: Path) -> Path:
    if value is None or str(value).strip() == "":
        return Path(default)
    return Path(value)


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _refuse_skill_path(relpath: str) -> None:
    parts = Path(relpath).parts
    lowered = {part.casefold() for part in parts}
    if SKILL_DIR_NAME in lowered or any(
        skill.casefold() in lowered for skill in USER_OWNED_SKILLS
    ):
        raise DeployTruthError(
            f"refusing skill path {relpath}; skills stay a separate .hermes install"
        )


def zip_load_shim_source(zip_relpath: str) -> str:
    """Hermes-facing stub that execs the zip file. Not Carlo's skill text."""
    _refuse_skill_path(zip_relpath)
    relpath_literal = json.dumps(zip_relpath)
    return (
        '"""Hermes load-path stub. Production code is the zip file."""\n'
        "from __future__ import annotations\n"
        "\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        f"{ZIP_LOAD_MARKER} = {relpath_literal}\n"
        "\n"
        "def _zip_source() -> Path:\n"
        "    roots: list[Path] = []\n"
        "    configured = os.environ.get("
        '"ROBIE_CANONICAL_JOB_ENGINE_ROOT", ""'
        ").strip()\n"
        "    if configured:\n"
        "        roots.append(Path(configured))\n"
        "    for item in os.environ.get("
        '"PYTHONPATH", ""'
        ").split(os.pathsep):\n"
        "        text = item.strip()\n"
        "        if text:\n"
        "            roots.append(Path(text))\n"
        "    roots.append(Path("
        '"/opt/streetsmart-hermes/releases/current"'
        "))\n"
        f"    relpath = {ZIP_LOAD_MARKER}\n"
        "    for root in roots:\n"
        "        path = root / relpath\n"
        "        if path.is_file():\n"
        "            return path\n"
        "    raise RuntimeError(\n"
        '        "zip source missing for " + relpath + '
        '"; refuse to run a stale .hermes copy"\n'
        "    )\n"
        "\n"
        "_src = _zip_source()\n"
        "_ns = globals()\n"
        '_ns["__file__"] = str(_src)\n'
        'exec(compile(_src.read_text(encoding="utf-8"), str(_src), "exec"), _ns)\n'
    )


def is_zip_load_shim(text: str, zip_relpath: str) -> bool:
    return (
        ZIP_LOAD_MARKER in text
        and zip_relpath in text
        and "exec(compile(" in text
        and "ROBIE_CANONICAL_JOB_ENGINE_ROOT" in text
        and "refuse to run a stale .hermes copy" in text
    )


def resolve_job_engine_root(
    *,
    env: Mapping[str, str] | None = None,
    here: Path | None = None,
    extra_candidates: Iterable[Path] | None = None,
) -> Path | None:
    """Prefer the zip PYTHONPATH / canonical root over a sibling .hermes tree."""
    environ = env if env is not None else os.environ
    candidates: list[Path] = []
    for key in ("ROBIE_CANONICAL_JOB_ENGINE_ROOT", "ROBIE_JOB_ENGINE_ROOT"):
        value = str(environ.get(key) or "").strip()
        if value:
            candidates.append(Path(value))
    for item in str(environ.get("PYTHONPATH") or "").split(os.pathsep):
        text = item.strip()
        if text:
            candidates.append(Path(text))
    if here is not None:
        resolved = Path(here).resolve()
        parents = resolved.parents
        if len(parents) >= 3:
            candidates.append(parents[2])
        if len(parents) >= 4:
            candidates.append(parents[3])
    candidates.append(Path("/opt/streetsmart-hermes/releases/current"))
    if extra_candidates:
        candidates.extend(Path(item) for item in extra_candidates)
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if (path / "robie_job_engine" / "__init__.py").is_file():
            return path
    return None


def resolve_write_guard_path(
    *,
    env: Mapping[str, str] | None = None,
    here: Path | None = None,
) -> Path:
    """Zip write-guard wins over a stale .hermes sibling copy."""
    root = resolve_job_engine_root(env=env, here=here)
    if root is not None:
        for relpath in (
            Path("robie_job_engine") / "playwright_write_guard.py",
            Path("deploy") / "hermes" / "tools" / "playwright_write_guard.py",
        ):
            path = root / relpath
            if path.is_file():
                return path
    if here is not None:
        sibling = Path(here).resolve().with_name("playwright_write_guard.py")
        if sibling.is_file():
            return sibling
    raise RuntimeError(
        "PLAYWRIGHT_BLOCKED: unique-write guard source is missing; "
        "refuse to run unconstrained Playwright writes"
    )


def resolve_gemini_helper_path(
    *,
    env: Mapping[str, str] | None = None,
    here: Path | None = None,
) -> Path:
    root = resolve_job_engine_root(env=env, here=here)
    if root is not None:
        path = root / "robie_job_engine" / "gemini_field_helper.py"
        if path.is_file():
            return path
    raise RuntimeError(
        "PLAYWRIGHT_BLOCKED: Gemini unique-field helper is missing; HITL Carlo"
    )


def _inode(path: Path) -> int | None:
    try:
        return int(path.stat().st_ino)
    except OSError:
        return None


def dest_matches_zip(
    dest: Path,
    zip_file: Path,
    zip_relpath: str,
) -> dict[str, Any]:
    """True when dest bytes equal the zip file, or dest is a zip-load shim."""
    payload: dict[str, Any] = {
        "dest": str(dest),
        "zip_file": str(zip_file),
        "zip_relpath": zip_relpath,
        "inode": _inode(dest) if dest.is_file() else None,
        "ok": False,
        "mode": "missing",
    }
    if not zip_file.is_file():
        payload["mode"] = "zip-missing"
        payload["evidence"] = f"zip source missing: {zip_file}"
        return payload
    payload["zip_md5"] = file_md5(zip_file)
    if not dest.is_file():
        payload["evidence"] = f"Chat-loaded dest missing: {dest}"
        return payload
    payload["md5"] = file_md5(dest)
    payload["inode"] = _inode(dest)
    if payload["md5"] == payload["zip_md5"]:
        payload["ok"] = True
        payload["mode"] = "bytes-match"
        payload["evidence"] = f"{dest.name} md5 equals zip {zip_relpath}"
        return payload
    text = dest.read_text(encoding="utf-8")
    if is_zip_load_shim(text, zip_relpath):
        payload["ok"] = True
        payload["mode"] = "zip-load-shim"
        payload["evidence"] = f"{dest.name} zip-load-shim -> {zip_relpath}"
        return payload
    payload["mode"] = "stale"
    payload["evidence"] = (
        f"{dest.name} stale vs zip {zip_relpath} "
        f"(dest md5={payload['md5']} zip md5={payload['zip_md5']}; "
        "not a zip-load shim)"
    )
    return payload


def pointer_targets(
    *,
    opt_root: str | Path | None = None,
) -> dict[str, Any]:
    root = _as_path(opt_root, DEFAULT_OPT_ROOT)
    current = root / "current"
    releases_current = root / "releases" / "current"
    current_target = current.resolve() if current.exists() else None
    releases_target = (
        releases_current.resolve() if releases_current.exists() else None
    )
    match = (
        current_target is not None
        and releases_target is not None
        and current_target == releases_target
    )
    return {
        "opt_root": str(root),
        "current": str(current),
        "releases_current": str(releases_current),
        "current_target": str(current_target) if current_target else None,
        "releases_current_target": str(releases_target) if releases_target else None,
        "match": match,
    }


def prove_chat_runtime_matches_zip(
    *,
    release_root: str | Path,
    hermes_home: str | Path | None = None,
    files: Iterable[ChatRuntimeFile] | None = None,
) -> dict[str, Any]:
    """Fail if a Chat-only dest can be stale while this zip is the pointer."""
    zip_root = Path(release_root)
    dest_root = _as_path(hermes_home, DEFAULT_HERMES_HOME)
    rows: list[dict[str, Any]] = []
    for item in files or CHAT_RUNTIME_FILES:
        _refuse_skill_path(item.zip_relpath)
        _refuse_skill_path(item.dest_relpath)
        row = dest_matches_zip(
            dest_root / item.dest_relpath,
            zip_root / item.zip_relpath,
            item.zip_relpath,
        )
        row["name"] = item.name
        rows.append(row)
    stale = [row for row in rows if not row["ok"]]
    ok = not stale
    evidence = (
        "; ".join(row["evidence"] for row in rows)
        if rows
        else "no Chat-runtime files declared"
    )
    return {
        "ok": ok,
        "kind": "chat-runtime",
        "release_root": str(zip_root),
        "hermes_home": str(dest_root),
        "files": rows,
        "stale": [row["name"] for row in stale],
        "evidence": evidence if ok else (stale[0]["evidence"] if stale else evidence),
        "skills": (
            "separate Drive/.hermes install; "
            "user-owned Loom ascend-finance is not proof"
        ),
    }


def prove_pointers_match_release(
    *,
    opt_root: str | Path,
    release_root: str | Path,
    sha: str | None = None,
) -> dict[str, Any]:
    pointers = pointer_targets(opt_root=opt_root)
    expected = Path(release_root).resolve()
    current = (
        Path(pointers["current_target"]).resolve()
        if pointers["current_target"]
        else None
    )
    releases = (
        Path(pointers["releases_current_target"]).resolve()
        if pointers["releases_current_target"]
        else None
    )
    ok = current == expected and releases == expected
    evidence = (
        f"both pointers -> {expected}"
        if ok
        else (
            "pointers do not both target the release "
            f"(current={current} releases/current={releases} expected={expected})"
        )
    )
    if ok and sha:
        text = str(expected)
        if sha not in text:
            ok = False
            evidence = f"release path {expected} does not contain SHA {sha}"
    return {
        "ok": ok,
        "kind": "pointers",
        "sha": sha,
        "release_root": str(expected),
        "pointers": pointers,
        "evidence": evidence,
    }


def prove_official_install(
    *,
    opt_root: str | Path,
    release_root: str | Path,
    hermes_home: str | Path | None = None,
    sha: str | None = None,
) -> dict[str, Any]:
    """Install is done only when pointers and Chat-loaded dests agree."""
    pointers = prove_pointers_match_release(
        opt_root=opt_root, release_root=release_root, sha=sha
    )
    chat = prove_chat_runtime_matches_zip(
        release_root=release_root, hermes_home=hermes_home
    )
    ok = bool(pointers["ok"] and chat["ok"])
    if not ok and not pointers["ok"]:
        evidence = pointers["evidence"]
    elif not ok:
        evidence = chat["evidence"]
    else:
        evidence = f"{pointers['evidence']}; {chat['evidence']}"
    return {
        "ok": ok,
        "done": ok,
        "pointers": pointers,
        "chat_runtime": chat,
        "evidence": evidence,
        "live": {
            "pointer_only": False,
            "requires": (
                "pointers + Chat load path equals that zip; "
                "gateway ActiveEnterTimestamp after the flip; "
                "gateway_progress on the next Chat job"
            ),
        },
    }


def is_pointer_only_live(
    *,
    opt_root: str | Path,
    release_root: str | Path,
    hermes_home: str | Path | None = None,
    sha: str | None = None,
) -> bool:
    """True only in the PR 17 failure mode: pointers match, Chat dest stale."""
    pointers = prove_pointers_match_release(
        opt_root=opt_root, release_root=release_root, sha=sha
    )
    chat = prove_chat_runtime_matches_zip(
        release_root=release_root, hermes_home=hermes_home
    )
    return bool(pointers["ok"] and not chat["ok"])


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f"{link.name}.robie-new")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(link)


def flip_release_pointers(
    *,
    opt_root: str | Path,
    release_root: str | Path,
) -> dict[str, Any]:
    root = Path(opt_root)
    target = Path(release_root).resolve()
    if not target.is_dir():
        raise DeployTruthError(f"release root is not a directory: {target}")
    current = root / "current"
    releases_current = root / "releases" / "current"
    _atomic_symlink(target, current)
    _atomic_symlink(target, releases_current)
    return pointer_targets(opt_root=root)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.robie-new")
    tmp.write_text(text, encoding="utf-8")
    backup = path.with_name(f"{path.name}.pre-deploy-truth")
    if path.exists() and not backup.exists():
        path.replace(backup)
    elif path.exists() or path.is_symlink():
        path.unlink()
    tmp.replace(path)


def install_chat_runtime_overlays(
    *,
    release_root: str | Path,
    hermes_home: str | Path,
    files: Iterable[ChatRuntimeFile] | None = None,
) -> list[dict[str, Any]]:
    """Install zip-load shims. Never writes skills or ascend-finance."""
    zip_root = Path(release_root)
    dest_root = Path(hermes_home)
    installed: list[dict[str, Any]] = []
    planned: list[tuple[ChatRuntimeFile, Path, str]] = []
    for item in files or CHAT_RUNTIME_FILES:
        _refuse_skill_path(item.zip_relpath)
        _refuse_skill_path(item.dest_relpath)
        zip_file = zip_root / item.zip_relpath
        if not zip_file.is_file():
            raise DeployTruthError(f"zip source missing: {zip_file}")
        dest = dest_root / item.dest_relpath
        if SKILL_DIR_NAME in dest.parts or any(
            skill in dest.parts for skill in USER_OWNED_SKILLS
        ):
            raise DeployTruthError(f"refusing to write skill dest {dest}")
        planned.append((item, dest, zip_load_shim_source(item.zip_relpath)))
    for item, dest, text in planned:
        _write_text_atomic(dest, text)
        installed.append(
            {
                "name": item.name,
                "dest": str(dest),
                "zip_relpath": item.zip_relpath,
                "mode": "zip-load-shim",
                "inode": _inode(dest),
                "md5": file_md5(dest),
            }
        )
    return installed


def write_install_proof(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    _write_text_atomic(path, text)
    return path


def official_install(
    *,
    opt_root: str | Path,
    release_root: str | Path,
    hermes_home: str | Path | None = None,
    sha: str | None = None,
    restart_gateway: bool = False,
) -> dict[str, Any]:
    """Flip pointers, install Chat-loaded overlays, refuse done without proof.

    Does not git pull. Does not bind. Does not print secrets. Does not
    overwrite user-owned Loom skills. Does not restart Hermes unless the
    caller passes ``restart_gateway`` (default false; this PR never deploys
    to hermes-poc-01).
    """
    if restart_gateway:
        raise DeployTruthError(
            "refusing to restart hermes-gateway from official install; "
            "operator restarts after Jake Approves / Carlo Confirms"
        )
    root = Path(opt_root)
    zip_root = Path(release_root).resolve()
    dest_root = Path(hermes_home) if hermes_home is not None else root / ".hermes"
    if not zip_root.is_dir():
        raise DeployTruthError(f"release root is not a directory: {zip_root}")
    if sha and sha not in str(zip_root) and sha not in zip_root.name:
        # SHA may live on the parent release dir (releases/<sha>/robie-hermes-<sha>).
        parent_name = zip_root.parent.name
        if sha not in parent_name:
            raise DeployTruthError(
                f"release root {zip_root} does not contain SHA {sha}"
            )
    overlays = install_chat_runtime_overlays(
        release_root=zip_root, hermes_home=dest_root
    )
    pointers = flip_release_pointers(opt_root=root, release_root=zip_root)
    proof = prove_official_install(
        opt_root=root,
        release_root=zip_root,
        hermes_home=dest_root,
        sha=sha,
    )
    payload = {
        "sha": sha,
        "opt_root": str(root),
        "release_root": str(zip_root),
        "hermes_home": str(dest_root),
        "overlays": overlays,
        "pointers": pointers,
        "proof": proof,
        "skills": (
            "not installed; Drive -> .hermes including user-owned "
            "Loom ascend-finance"
        ),
        "git_pull": False,
        "bind": False,
        "restart_gateway": False,
        "created_at": _utc_now(),
        "done": bool(proof["ok"]),
        "banner": DONE_BANNER if proof["ok"] else NOT_DONE_BANNER,
    }
    proof_path = zip_root / PROOF_FILENAME
    if proof["ok"]:
        write_install_proof(proof_path, payload)
        payload["proof_path"] = str(proof_path)
        return payload
    raise DeployTruthError(
        f"{NOT_DONE_BANNER}: {proof['evidence']}"
    )


def format_proof_report(proof: Mapping[str, Any]) -> str:
    banner = DONE_BANNER if proof.get("ok") else NOT_DONE_BANNER
    return f"{banner}: {proof.get('evidence') or 'no evidence'}"


def _public_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """JSON for operators. Never include environ or secret values."""
    return json.loads(json.dumps(payload))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Official zip install and Chat-runtime proof. "
            "Does not git pull, bind, or print secrets. "
            "Skills including ascend-finance are not installed here."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prove", "install"):
        item = sub.add_parser(name)
        item.add_argument("--opt-root", default="")
        item.add_argument("--release-root", required=True)
        item.add_argument("--hermes-home", default="")
        item.add_argument("--sha", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    opt_root = args.opt_root or str(DEFAULT_OPT_ROOT)
    hermes_home = args.hermes_home or None
    sha = args.sha or None
    if args.command == "prove":
        proof = prove_official_install(
            opt_root=opt_root,
            release_root=args.release_root,
            hermes_home=hermes_home,
            sha=sha,
        )
        print(json.dumps(_public_payload(proof), indent=2, sort_keys=True))
        print(format_proof_report(proof), flush=True)
        return 0 if proof["ok"] else 2
    try:
        payload = official_install(
            opt_root=opt_root,
            release_root=args.release_root,
            hermes_home=hermes_home,
            sha=sha,
        )
    except DeployTruthError as exc:
        print(json.dumps({"done": False, "error": str(exc)}, indent=2, sort_keys=True))
        print(str(exc), flush=True)
        return 2
    print(json.dumps(_public_payload(payload), indent=2, sort_keys=True))
    print(payload["banner"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
