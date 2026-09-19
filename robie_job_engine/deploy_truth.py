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
from typing import Any, Callable, Iterable, Mapping


class DeployTruthError(RuntimeError):
    """Official install or Chat-runtime proof failed. Not done."""


ZIP_LOAD_MARKER = "ROBIE_ZIP_LOAD_PATH"
DEFAULT_OPT_ROOT = Path("/opt/streetsmart-hermes")
DEFAULT_HERMES_HOME = DEFAULT_OPT_ROOT / ".hermes"
USER_OWNED_SKILLS = ("ascend-finance",)
SKILL_DIR_NAME = "skills"
PROOF_FILENAME = "official-install-proof.json"
FLIP_RECORD_FILENAME = "official-install-flip.json"
DONE_BANNER = "OFFICIAL INSTALL DONE"
NOT_DONE_BANNER = "OFFICIAL INSTALL NOT DONE"
INSTALL_PROOF_KIND = "install_proof"
INSTALL_PROOF_ACTION = "robie.official_install"
INSTALL_PROOF_KEY = "official-install-proof"
DEFAULT_JOBS_DB = Path("/opt/streetsmart-hermes/robie-job-engine/data/jobs.db")
GATEWAY_UNIT = "hermes-gateway"

@dataclass(frozen=True)
class ChatRuntimeFile:
    """One Hermes-loaded file that must equal the zip (or load it)."""

    name: str
    zip_relpath: str
    dest_relpath: str


# Chat / Playwright files Hermes loads from .hermes. Skills stay out.
CHAT_RUNTIME_FILES: tuple[ChatRuntimeFile, ...] = (
    ChatRuntimeFile(
        name="email-agent",
        zip_relpath="scripts/robie_email_agent.py",
        dest_relpath="scripts/robie_email_agent.py",
    ),
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
    ChatRuntimeFile(
        name="policy-setup-tool",
        zip_relpath="deploy/hermes/tools/policy_setup_tool.py",
        dest_relpath="hermes-agent/tools/policy_setup_tool.py",
    ),
    ChatRuntimeFile(
        name="document-upload-tool",
        zip_relpath="deploy/hermes/tools/ezlynx_document_tool.py",
        dest_relpath="hermes-agent/tools/ezlynx_document_tool.py",
    ),
    ChatRuntimeFile(
        name="discussion-note-tool",
        zip_relpath="deploy/hermes/tools/ezlynx_note_tool.py",
        dest_relpath="hermes-agent/tools/ezlynx_note_tool.py",
    ),
)

# Zip PYTHONPATH only — Hermes does not load these from .hermes.
ZIP_ONLY_FILES: tuple[ChatRuntimeFile, ...] = (
    ChatRuntimeFile(
        name="gemini-field-helper",
        zip_relpath="robie_job_engine/gemini_field_helper.py",
        dest_relpath="",
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
    if zip_relpath == "scripts/robie_email_agent.py":
        # The receiving .hermes location determines the environment even before
        # a newly deployed service has loaded its environment drop-in.
        return (
            'from pathlib import Path\n'
            f'{ZIP_LOAD_MARKER} = {relpath_literal}\n'
            '# ROBIE_CANONICAL_JOB_ENGINE_ROOT cannot redirect this launcher.\n'
            '_root = Path(__file__).resolve().parents[2]\n'
            '_src = _root / "releases/current" / ROBIE_ZIP_LOAD_PATH\n'
            'if not _src.is_file():\n'
            '    raise RuntimeError("refuse to run a stale .hermes copy")\n'
            'globals()["__file__"] = str(_src.resolve())\n'
            'exec(compile(_src.read_text(encoding="utf-8"), str(_src), "exec"), globals())\n'
        )
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
        "class _DummyRegistry:\n"
        "    def register(self, *args, **kwargs):\n"
        "        pass\n"
        'registry = globals().get("registry", _DummyRegistry())\n'
        'registry.register(name="robie_shim", toolset="robie", schema={}, handler=lambda *a, **k: None)\n'
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
    declared = tuple(files) if files is not None else CHAT_RUNTIME_FILES + ZIP_ONLY_FILES
    for item in declared:
        _refuse_skill_path(item.zip_relpath)
        if item.dest_relpath:
            _refuse_skill_path(item.dest_relpath)
            row = dest_matches_zip(
                dest_root / item.dest_relpath,
                zip_root / item.zip_relpath,
                item.zip_relpath,
            )
        else:
            zip_file = zip_root / item.zip_relpath
            present = zip_file.is_file()
            row = {
                "dest": None,
                "zip_file": str(zip_file),
                "zip_relpath": item.zip_relpath,
                "ok": present,
                "mode": "zip-path" if present else "zip-missing",
                "evidence": (
                    f"{item.name} on zip PYTHONPATH"
                    if present
                    else f"zip source missing: {zip_file}"
                ),
            }
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


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).strip()
    if not text or text.casefold() in {"n/a", "none", "0"}:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z"):
        try:
            wall_clock = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # systemctl prints ActiveEnterTimestamp in the SYSTEM LOCAL timezone
        # (e.g. "Thu 2026-09-10 12:02:06 EDT"), and strptime discards the zone
        # name. Re-attach the local zone explicitly: labeling local wall-clock
        # time as UTC made the Production verifier compare 12:02 EDT against
        # a true-UTC 16:02 flip and roll back good deploys ("pointer-only is
        # not live"). DST-boundary skew (service started under a different
        # offset than now) is out of scope for a seconds-after-flip check.
        local_tz = datetime.now().astimezone().tzinfo
        return wall_clock.replace(tzinfo=local_tz).astimezone(timezone.utc)
    return None


def probe_gateway_active_enter(
    *,
    gateway_unit: str = GATEWAY_UNIT,
    runner: Callable[[list[str]], Any] | None = None,
) -> datetime | None:
    """Read a gateway ActiveEnterTimestamp. Never restarts the unit."""
    if runner is None:
        import subprocess

        def runner(argv: list[str]) -> Any:
            return subprocess.run(
                argv, check=False, capture_output=True, text=True, timeout=5
            )

    try:
        proc = runner(
            [
                "systemctl",
                "show",
                gateway_unit,
                "-p",
                "ActiveEnterTimestamp",
                "--value",
                "--no-pager",
            ]
        )
    except OSError:
        return None
    text = (getattr(proc, "stdout", None) or "") if proc is not None else ""
    return parse_timestamp(str(text).strip())


def prove_gateway_after_flip(
    *,
    flip_at: str | datetime | None,
    active_enter: str | datetime | None = None,
    gateway_probe: Callable[[], datetime | None] | None = None,
    gateway_unit: str = GATEWAY_UNIT,
) -> dict[str, Any]:
    flipped = parse_timestamp(flip_at)
    entered = parse_timestamp(active_enter)
    if entered is None and gateway_probe is not None:
        entered = parse_timestamp(gateway_probe())
    if entered is None and active_enter is None and gateway_probe is None:
        entered = probe_gateway_active_enter(gateway_unit=gateway_unit)
    if flipped is None:
        return {
            "ok": False,
            "kind": "gateway-after-flip",
            "flip_at": None,
            "active_enter": entered.isoformat() if entered else None,
            "evidence": "missing pointer-flip timestamp; pointer-only is not live",
        }
    if entered is None:
        return {
            "ok": False,
            "kind": "gateway-after-flip",
            "flip_at": flipped.isoformat(),
            "active_enter": None,
            "evidence": (
                f"{gateway_unit} ActiveEnterTimestamp missing; "
                "restart after the flip is required; pointer-only is not live"
            ),
        }
    ok = entered > flipped
    return {
        "ok": ok,
        "kind": "gateway-after-flip",
        "flip_at": flipped.isoformat(),
        "active_enter": entered.isoformat(),
        "evidence": (
            f"{gateway_unit} ActiveEnterTimestamp {entered.isoformat()} "
            f"after flip {flipped.isoformat()}"
            if ok
            else (
                f"{gateway_unit} ActiveEnterTimestamp {entered.isoformat()} "
                f"is not after flip {flipped.isoformat()}; pointer-only is not live"
            )
        ),
    }


def write_flip_record(
    *,
    opt_root: str | Path,
    release_root: str | Path,
    sha: str | None,
    flip_at: str | datetime,
) -> dict[str, Any]:
    """Persist flip time so later prove does not need a remembered --flip-at."""
    parsed = parse_timestamp(flip_at)
    if parsed is None:
        raise DeployTruthError("cannot record flip without a timestamp")
    record = {
        "flip_at": parsed.isoformat(),
        "sha": sha,
        "release_root": str(Path(release_root).resolve()),
        "chat_busy_is_not_live": True,
        "authorizes_complete": False,
        "destination_verified": False,
    }
    for dest in (
        Path(release_root) / FLIP_RECORD_FILENAME,
        Path(opt_root) / FLIP_RECORD_FILENAME,
    ):
        write_install_proof(dest, record)
    return record


def load_recorded_flip(
    *,
    opt_root: str | Path | None = None,
    release_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the last official-install flip timestamp. Not live by itself."""
    candidates: list[Path] = []
    if release_root:
        root = Path(release_root)
        candidates.append(root / FLIP_RECORD_FILENAME)
        candidates.append(root / PROOF_FILENAME)
    if opt_root:
        candidates.append(Path(opt_root) / FLIP_RECORD_FILENAME)
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        flip_at = data.get("flip_at")
        if parse_timestamp(flip_at) is None:
            continue
        return {
            "flip_at": flip_at,
            "sha": data.get("sha"),
            "release_root": data.get("release_root"),
            "path": str(path),
        }
    return None


def persist_install_proof_row(
    db_path: str | Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Write an install_proof checkpoint. Never COMPLETE. Never secrets."""
    from .store import JobStore

    store = JobStore(str(db_path))
    sha = str(payload.get("sha") or "unknown")
    job = store.create_job(
        INSTALL_PROOF_ACTION,
        {
            "kind": INSTALL_PROOF_KIND,
            "sha": sha,
            "authorizes_complete": False,
            "destination_verified": False,
            "chat_busy_is_not_live": True,
        },
        idempotency_key=f"{INSTALL_PROOF_KEY}:{sha}",
    )
    from .models import JobStatus

    if job["status"] == JobStatus.COMPLETE.value:
        raise DeployTruthError("install proof job must not be COMPLETE")
    data = {
        "sha": sha,
        "live": bool(payload.get("live")),
        "pointer_only": False,
        "chat_busy_is_not_live": True,
        "authorizes_complete": False,
        "destination_verified": False,
        "destination_evidence_required_for_complete": True,
        "flip_at": payload.get("flip_at"),
        "gateway_active_enter": payload.get("gateway_active_enter"),
        "evidence": payload.get("evidence"),
    }
    store.checkpoint(job["id"], INSTALL_PROOF_KIND, data)
    stored = store.get_checkpoint(job["id"], INSTALL_PROOF_KIND) or {}
    return {
        "job_id": job["id"],
        "kind": INSTALL_PROOF_KIND,
        "status": job["status"],
        "data": stored,
    }


def prove_official_install(
    *,
    opt_root: str | Path,
    release_root: str | Path,
    hermes_home: str | Path | None = None,
    sha: str | None = None,
    flip_at: str | datetime | None = None,
    gateway_active_enter: str | datetime | None = None,
    gateway_probe: Callable[[], datetime | None] | None = None,
    gateway_unit: str = GATEWAY_UNIT,
    db_path: str | Path | None = None,
    persist_row: bool = False,
) -> dict[str, Any]:
    """Live/done only when pointers, Chat dests, and gateway-after-flip agree.

    Pointer-only is not live. Chat looking busy is not live. COMPLETE still
    requires destination-verified evidence > 0; this proof never authorizes it.
    """
    if flip_at is None:
        recorded = load_recorded_flip(opt_root=opt_root, release_root=release_root)
        if recorded is not None:
            flip_at = recorded.get("flip_at")
    pointers = prove_pointers_match_release(
        opt_root=opt_root, release_root=release_root, sha=sha
    )
    chat = prove_chat_runtime_matches_zip(
        release_root=release_root, hermes_home=hermes_home
    )
    gateway = prove_gateway_after_flip(
        flip_at=flip_at,
        active_enter=gateway_active_enter,
        gateway_probe=gateway_probe,
        gateway_unit=gateway_unit,
    )
    files_ok = bool(pointers["ok"] and chat["ok"])
    live = bool(files_ok and gateway["ok"])
    if not pointers["ok"]:
        evidence = pointers["evidence"]
    elif not chat["ok"]:
        evidence = chat["evidence"]
    elif not gateway["ok"]:
        evidence = gateway["evidence"]
    else:
        evidence = (
            f"{pointers['evidence']}; {chat['evidence']}; {gateway['evidence']}"
        )
    payload = {
        "ok": live,
        "done": live,
        "live": live,
        "pointer_only": bool(pointers["ok"] and not chat["ok"]),
        "chat_busy_is_not_live": True,
        "authorizes_complete": False,
        "destination_verified": False,
        "sha": sha,
        "flip_at": gateway.get("flip_at") or (parse_timestamp(flip_at).isoformat() if parse_timestamp(flip_at) else None),
        "gateway_active_enter": gateway.get("active_enter"),
        "pointers": pointers,
        "chat_runtime": chat,
        "gateway": gateway,
        "evidence": evidence,
        "install_proof_row": None,
    }
    if persist_row:
        db = db_path or os.environ.get("ROBIE_JOB_DB") or str(DEFAULT_JOBS_DB)
        can_write = Path(db).is_file() or db_path is not None
        if can_write:
            payload["install_proof_row"] = persist_install_proof_row(db, payload)
        elif live:
            payload["ok"] = False
            payload["done"] = False
            payload["live"] = False
            payload["evidence"] = (
                "install proof row missing; refuse to call live without a "
                f"{INSTALL_PROOF_KIND} checkpoint"
            )
            return payload
        if live and not payload.get("install_proof_row"):
            payload["ok"] = False
            payload["done"] = False
            payload["live"] = False
            payload["evidence"] = (
                "install proof row missing; refuse to call live without a "
                f"{INSTALL_PROOF_KIND} checkpoint"
            )
    return payload


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


def refresh_install_proof_file(
    path: Path,
    proof: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically replace stale pre-restart proof with the latest verdict.

    ``official_install`` intentionally writes a failed proof before the
    operator restarts the gateway.  A later ``prove`` must update that same
    durable artifact; otherwise operators and audits see ``live: false`` even
    after the database checkpoint and gateway timestamp prove the release is
    live.
    """
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            payload.update(existing)
    payload.update(
        {
            "sha": proof.get("sha") or payload.get("sha"),
            "proof": dict(proof),
            "done": bool(proof.get("ok")),
            "live": bool(proof.get("live")),
            "banner": DONE_BANNER if proof.get("ok") else NOT_DONE_BANNER,
            "verified_at": _utc_now(),
            "authorizes_complete": False,
        }
    )
    write_install_proof(path, payload)
    payload["proof_path"] = str(path)
    return payload


def official_install(
    *,
    opt_root: str | Path,
    release_root: str | Path,
    hermes_home: str | Path | None = None,
    sha: str | None = None,
    restart_gateway: bool = False,
    flip_at: str | datetime | None = None,
    gateway_active_enter: str | datetime | None = None,
    gateway_probe: Callable[[], datetime | None] | None = None,
    gateway_unit: str = GATEWAY_UNIT,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Flip pointers, install Chat-loaded overlays, refuse done without live proof.

    Done requires pointers + Chat dests equal that zip + hermes-gateway
    ActiveEnterTimestamp after the flip + an install_proof row. Pointer-only
    is not live. Chat looking busy is not live. Does not git pull, bind,
    print secrets, overwrite Loom ascend-finance, or restart Hermes.
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
        parent_name = zip_root.parent.name
        if sha not in parent_name:
            raise DeployTruthError(
                f"release root {zip_root} does not contain SHA {sha}"
            )
    flipped = parse_timestamp(flip_at) or datetime.now(timezone.utc)
    overlays = install_chat_runtime_overlays(
        release_root=zip_root, hermes_home=dest_root
    )
    pointers = flip_release_pointers(opt_root=root, release_root=zip_root)
    write_flip_record(
        opt_root=root, release_root=zip_root, sha=sha, flip_at=flipped
    )
    proof = prove_official_install(
        opt_root=root,
        release_root=zip_root,
        hermes_home=dest_root,
        sha=sha,
        flip_at=flipped,
        gateway_active_enter=gateway_active_enter,
        gateway_probe=gateway_probe,
        gateway_unit=gateway_unit,
        db_path=db_path,
        persist_row=True,
    )
    payload = {
        "sha": sha,
        "opt_root": str(root),
        "release_root": str(zip_root),
        "hermes_home": str(dest_root),
        "overlays": overlays,
        "pointers": pointers,
        "proof": proof,
        "flip_at": flipped.isoformat(),
        "skills": (
            "not installed; Drive -> .hermes including user-owned "
            "Loom ascend-finance"
        ),
        "git_pull": False,
        "bind": False,
        "restart_gateway": False,
        "authorizes_complete": False,
        "chat_busy_is_not_live": True,
        "created_at": _utc_now(),
        "done": bool(proof["ok"]),
        "live": bool(proof.get("live")),
        "banner": DONE_BANNER if proof["ok"] else NOT_DONE_BANNER,
    }
    proof_path = zip_root / PROOF_FILENAME
    write_install_proof(proof_path, payload)
    payload["proof_path"] = str(proof_path)
    if proof["ok"]:
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
        item.add_argument("--db", default="")
        item.add_argument("--flip-at", default="")
        item.add_argument("--gateway-active-enter", default="")
        item.add_argument("--gateway-unit", default=GATEWAY_UNIT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    opt_root = args.opt_root or str(DEFAULT_OPT_ROOT)
    hermes_home = args.hermes_home or None
    sha = args.sha or None
    db_path = args.db or None
    flip_at = args.flip_at or None
    gateway_active_enter = args.gateway_active_enter or None
    gateway_unit = args.gateway_unit
    if args.command == "prove":
        proof = prove_official_install(
            opt_root=opt_root,
            release_root=args.release_root,
            hermes_home=hermes_home,
            sha=sha,
            flip_at=flip_at,
            gateway_active_enter=gateway_active_enter,
            gateway_unit=gateway_unit,
            db_path=db_path,
            persist_row=True,
        )
        proof_path = Path(args.release_root) / PROOF_FILENAME
        refresh_install_proof_file(proof_path, proof)
        proof["proof_path"] = str(proof_path)
        print(json.dumps(_public_payload(proof), indent=2, sort_keys=True))
        print(format_proof_report(proof), flush=True)
        return 0 if proof["ok"] else 2
    try:
        payload = official_install(
            opt_root=opt_root,
            release_root=args.release_root,
            hermes_home=hermes_home,
            sha=sha,
            flip_at=flip_at,
            gateway_active_enter=gateway_active_enter,
            gateway_unit=gateway_unit,
            db_path=db_path,
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
