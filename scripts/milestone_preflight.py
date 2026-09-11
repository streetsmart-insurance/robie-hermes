#!/usr/bin/env python3
"""Pre-flight for the completion milestone. Read-only, no side effects.

Five things can make a milestone run print a verdict that looks like a code
failure and is not one. Check them before seeding anything:

  1. The gateway service runs with ROBIE_ENV set. Every Secret Manager read
     fails closed without it.
  2. Secret Manager reachable for the EZLynx PolicyApi credentials. The Chat
     destination verifier reads through it and fails CLOSED if absent.
  3. The running service has actually picked up the configuration on disk.
     Writing an EnvironmentFile changes nothing until the unit restarts.
  4. Slice 1's action checkpoint writer present in the deployed release.
  5. The Chat verifiers actually register.

This script runs over an ad-hoc ssh session. That session is not the service:
it inherits none of the unit's Environment= or EnvironmentFile=, and python3
on PATH is not necessarily the interpreter the unit runs, which means it need
not have the same libraries installed. Answering from this session would
describe a box that does not exist. So checks 2 and 5 — the ones that import
robie_job_engine — are run as a child process using the interpreter out of the
unit's ExecStart, with the unit's own environment, and the report names both so
the answer can be traced. Checks 1, 3 and 4 read systemd and the filesystem and
need no interpreter.

Exit 0 when everything needed for the milestone is in place, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

OK = "ok"
BAD = "PROBLEM"

# Run inside the service's own interpreter. Everything it reports is about that
# runtime, not ours. It prints one JSON line so a noisy import cannot be
# mistaken for a result.
PROBE = r'''
import json, sys
out = {"python": sys.executable, "secret": None, "verifiers": []}
try:
    from robie_job_engine.ezlynx_api import load_ezlynx_api_config
except Exception as exc:
    out["secret"] = ["PROBLEM", "cannot import ezlynx_api: %s: %s" % (type(exc).__name__, exc)]
else:
    try:
        load_ezlynx_api_config()
    except Exception as exc:
        out["secret"] = ["PROBLEM", "%s: %s" % (type(exc).__name__, exc)]
    else:
        out["secret"] = ["ok", "EZLynx PolicyApi config loaded"]
try:
    from robie_job_engine.chat_guard import _default_chat_verifiers, _UnavailableVerifier
except Exception as exc:
    out["verifiers"].append(["chat_guard", "PROBLEM",
                             "import failed: %s: %s" % (type(exc).__name__, exc)])
else:
    try:
        registry = _default_chat_verifiers()
    except Exception as exc:
        out["verifiers"].append(["_default_chat_verifiers", "PROBLEM",
                                 "raised: %s: %s" % (type(exc).__name__, exc)])
    else:
        for action in ("hermes.google_chat_task", "browser.read"):
            got = registry.get(action)
            if got is None:
                out["verifiers"].append([action, "PROBLEM", "not registered at all"])
            elif isinstance(got, _UnavailableVerifier):
                out["verifiers"].append([action, "PROBLEM",
                                         "failed to register: %s" % (got.reason,)])
            else:
                out["verifiers"].append([action, "ok", type(got).__name__])
sys.stdout.write("PROBE_JSON:" + json.dumps(out) + "\n")
'''


def _unit_property(unit: str, prop: str) -> str:
    try:
        return subprocess.run(
            ["systemctl", "show", unit, "-p", prop, "--value"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception:
        return ""


def _loaded_gateway_unit(units: list[str]) -> tuple[str | None, list[str]]:
    """First unit that actually exists on this box, plus what was checked.

    The unit is named differently across boxes (robie-gateway on Test,
    hermes-gateway elsewhere). An earlier version checked one hardcoded name and
    reported "sets no Environment= at all" for a unit that simply was not there
    — a confident answer about the wrong thing, which is worse than no answer.
    """
    seen: list[str] = []
    for unit in units:
        loaded = _unit_property(unit, "LoadState")
        if loaded == "loaded":
            return unit, seen
        seen.append(f"{unit}={loaded or 'absent'}")
    return None, seen


def _unit_environment_files(unit: str) -> list[Path]:
    """Paths systemd loads into the unit's environment, in order."""
    raw = _unit_property(unit, "EnvironmentFiles")
    paths: list[Path] = []
    for token in raw.split():
        if token.startswith("("):  # trailing "(ignore_errors=no)"
            continue
        paths.append(Path(token.lstrip("-")))
    return paths


def _unit_environment(unit: str | None) -> tuple[dict[str, str], list[str]]:
    """The environment the service itself runs with, and where it came from."""
    if unit is None:
        return {}, []
    env: dict[str, str] = {}
    sources: list[str] = []
    for item in shlex.split(_unit_property(unit, "Environment")):
        if "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    if _unit_property(unit, "Environment"):
        sources.append(f"{unit} Environment=")
    # EnvironmentFile= overrides Environment=; later files override earlier ones.
    for path in _unit_environment_files(unit):
        if not path.is_file():
            sources.append(f"{path} (missing)")
            continue
        try:
            text = path.read_text()
        except Exception as exc:
            sources.append(f"{path} (unreadable: {type(exc).__name__})")
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
        sources.append(str(path))
    return env, sources


def _looks_like_python(token: str) -> bool:
    name = Path(token).name
    return name.startswith("python") or name.startswith("pypy")


def _service_python(unit: str | None) -> tuple[str, str]:
    """The interpreter the unit runs, and how it was determined.

    A service that runs out of a virtualenv has libraries this ssh session's
    python3 does not. Reporting "google-cloud-secret-manager is required" from
    the wrong interpreter says nothing about whether the service can read the
    secret. Falls back to this process's interpreter, and says so, rather than
    guessing a venv path.
    """
    if unit is None:
        return sys.executable, f"{sys.executable} (no gateway unit; this ssh session's python)"
    raw = _unit_property(unit, "ExecStart")
    if not raw:
        return sys.executable, f"{sys.executable} ({unit} reports no ExecStart; this ssh session's python)"
    # systemd prints: { path=/x/python ; argv[]=/x/python -m y ; ... }
    for field in raw.replace("{", " ").replace("}", " ").split(";"):
        field = field.strip()
        if not field.startswith("path="):
            continue
        candidate = field[len("path="):].strip()
        if candidate and _looks_like_python(candidate) and Path(candidate).exists():
            return candidate, f"{candidate} (from {unit} ExecStart)"
        break
    for field in raw.replace("{", " ").replace("}", " ").split(";"):
        field = field.strip()
        if not field.startswith("argv[]="):
            continue
        try:
            tokens = shlex.split(field[len("argv[]="):])
        except ValueError:
            tokens = field[len("argv[]="):].split()
        for token in tokens:
            if _looks_like_python(token) and Path(token).exists():
                return token, f"{token} (from {unit} argv)"
        break
    return sys.executable, (
        f"{sys.executable} (no interpreter found in {unit} ExecStart; this ssh session's "
        "python — a missing library below may be this session's, not the service's)"
    )


def _service_pythonpath(env: dict[str, str], release: Path) -> str:
    """The unit's PYTHONPATH, with the release appended if it is not already on it.

    deploy-test-release.sh installs the gateway's libraries into
    releases/current/.gateway-runtime and puts that directory on the unit's
    PYTHONPATH through a drop-in. Replacing PYTHONPATH with the release root
    alone would hide exactly the libraries the service can import, and the probe
    would report them missing from a box where they are present.
    """
    existing = env.get("PYTHONPATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if str(release) not in parts:
        parts.append(str(release))
    return os.pathsep.join(parts)


def _probe_service_runtime(interpreter: str, env: dict[str, str], release: Path):
    """Run PROBE inside the service's interpreter with the service's environment."""
    child_env = dict(os.environ)
    child_env.update(env)
    child_env["PYTHONPATH"] = _service_pythonpath(env, release)
    try:
        proc = subprocess.run(
            [interpreter, "-c", PROBE], capture_output=True, text=True,
            timeout=120, cwd=str(release) if release.is_dir() else None, env=child_env,
        )
    except Exception as exc:
        return None, f"could not run {interpreter}: {type(exc).__name__}: {exc}"
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_JSON:"):
            try:
                return json.loads(line[len("PROBE_JSON:"):]), ""
            except Exception as exc:
                return None, f"probe printed unreadable JSON: {type(exc).__name__}: {exc}"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return None, (
        f"{interpreter} exited {proc.returncode} without a result"
        + (f": {tail[-1]}" if tail else "")
    )


def _check_gateway_env(unit: str | None, seen: list[str], env: dict[str, str]) -> tuple[str, str]:
    if unit is None:
        return BAD, (
            "no gateway unit found under any known name — checked "
            + ", ".join(seen)
            + ". Either the service is named something else on this box or it is not "
            "installed; this check cannot tell you about ROBIE_ENV either way."
        )
    if "ROBIE_ENV" in env:
        return OK, f"{unit} runs with ROBIE_ENV={env['ROBIE_ENV']}"
    return BAD, f"{unit} is loaded but sets no ROBIE_ENV in Environment= or any EnvironmentFile="


def _to_epoch(stamp: str) -> float | None:
    if not stamp:
        return None
    try:
        out = subprocess.run(["date", "-d", stamp, "+%s"],
                             capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def _check_config_is_live(unit: str | None) -> tuple[str, str]:
    """Has the service restarted since its config files were last written?"""
    if unit is None:
        return OK, "no gateway unit — nothing to compare (informational)"
    present = [p for p in _unit_environment_files(unit) if p.is_file()]
    if not present:
        return OK, f"{unit} loads no EnvironmentFile — nothing to compare"
    newest_path = max(present, key=lambda p: p.stat().st_mtime)
    newest = newest_path.stat().st_mtime
    started_raw = _unit_property(unit, "ExecMainStartTimestamp")
    started = _to_epoch(started_raw)
    if started is None:
        return OK, (
            f"{unit} start time is {started_raw or 'unavailable'} — cannot compare "
            f"against {newest_path} (informational)"
        )
    if started < newest:
        return BAD, (
            f"{unit} started {started_raw}, but {newest_path} was written after that. "
            "The running service still has the old values."
        )
    return OK, f"{unit} started {started_raw}, after {newest_path} was last written"


def _check_checkpoint_writer(release: Path) -> tuple[str, str]:
    target = release / "robie_job_engine" / "chat_destination_binding.py"
    if not target.is_file():
        return BAD, f"missing {target} — the engine is never constructed without it"
    return OK, str(target)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release", required=True, type=Path, help="deployed release root")
    ap.add_argument("--unit", action="append", default=None,
                    help="gateway unit to probe; repeatable. Default: robie-gateway, hermes-gateway")
    args = ap.parse_args(argv)

    units = args.unit or ["robie-gateway", "hermes-gateway"]
    unit, seen = _loaded_gateway_unit(units)
    unit_env, env_sources = _unit_environment(unit)
    interpreter, interpreter_note = _service_python(unit)
    pythonpath = _service_pythonpath(unit_env, args.release)
    probe, probe_error = _probe_service_runtime(interpreter, unit_env, args.release)

    env_label = ", ".join(env_sources) if env_sources else "no EnvironmentFile or Environment="
    print(f"release:     {args.release.resolve()}")
    print(f"ROBIE_ENV:   {os.environ.get('ROBIE_ENV') or '(unset in this shell)'} in this shell; "
          f"{unit_env.get('ROBIE_ENV') or '(unset)'} in {unit or 'no gateway unit'}")
    print(f"interpreter: {interpreter_note}")
    print(f"environment: {env_label}")
    print(f"PYTHONPATH:  {pythonpath}")
    print()

    # Each entry: (label, state, detail, what it means in plain English)
    results: list[tuple[str, str, str, str]] = []

    state, detail = _check_gateway_env(unit, seen, unit_env)
    if unit is None:
        why = (
            f"None of the names this script knows ({'/'.join(units)}) exist on this box, "
            "so it cannot tell you anything about the gateway's configuration — and it "
            "should not pretend otherwise. Either the unit is named something else here, "
            "in which case pass --unit <name>, or the service is not installed. "
            "Everything below that depends on the unit's environment or interpreter is "
            "reading this ssh session instead, so treat those results as unknown."
        )
    else:
        why = (
            f"The gateway service ({unit}) runs with no ROBIE_ENV. Every Secret Manager "
            "read fails closed without it, so nothing that needs EZLynx credentials can "
            "work — including the Chat destination verifier the milestone depends on. "
            "This is the service's own configuration, not ours: setting ROBIE_ENV in our "
            "ssh session would hide it rather than fix it. Fix: add "
            "Environment=ROBIE_ENV=TEST to the unit and reload."
        )
    results.append(("gateway unit ROBIE_ENV", state, detail, why))

    if probe is None:
        results.append((
            "service runtime probe", BAD, probe_error,
            f"Nothing could be learned about what {interpreter} can import, so the Secret "
            "Manager and verifier checks below are missing rather than passing. Read the "
            "error literally: it is about starting the interpreter, not about the engine. "
            "Fix: confirm the interpreter path exists and is executable by this session, "
            "and that the release root is importable from it.",
        ))
    else:
        state, detail = probe["secret"]
        results.append((
            "Secret Manager (EZLynx PolicyApi)", state,
            f"{detail} [as {probe['python']}, env from {env_label}]",
            "The service cannot load EZLynx API credentials, so the Chat destination "
            "verifier can register but every read it makes will fail. This ran inside the "
            "service's own interpreter with the service's own environment, so it is a "
            "failure the service really has. Fix: if the message names a missing library, "
            "it is missing from that interpreter on the PYTHONPATH printed above, and "
            "belongs in the release's gateway runtime requirements "
            "(deploy/requirements-test-gateway-playwright.txt, installed into "
            ".gateway-runtime by deploy-test-release.sh); if it names a variable, check "
            "the EnvironmentFile spells "
            "ROBIE_EZLYNX_API_UAT_SECRET as a full resource name "
            "(projects/NNN/secrets/NAME/versions/latest); if it is a permission error, "
            "this VM's service account needs roles/secretmanager.secretAccessor on it.",
        ))
        for action, state, detail in probe["verifiers"]:
            if action in ("chat_guard", "_default_chat_verifiers"):
                results.append((
                    f"chat verifier registry ({action})", state, detail,
                    "The registry itself could not be built, so no Chat action has a "
                    "verifier and every Chat job will terminate UNVERIFIED. The quoted "
                    "exception is the whole story — a code or dependency problem in the "
                    "deployed release, not a configuration one.",
                ))
                continue
            results.append((
                f"verifier {action}", state, detail,
                f"No verifier is reachable for {action}, so every such job terminates "
                "UNVERIFIED however well the worker performed. If the detail says "
                "'failed to register', an import raised at startup and the reason is "
                "quoted — that is a code or dependency problem. If it says 'not registered "
                "at all', nothing ever wired it.",
            ))

    state, detail = _check_config_is_live(unit)
    results.append((
        "running service has the current config", state, detail,
        "The configuration on disk is right but the running process predates it. "
        "systemd reads an EnvironmentFile once, at start, so an edit does nothing until "
        "the unit restarts. Every other check here can be green while the live service "
        "still fails, because those checks read the file and the service does not. Fix: "
        "restart the gateway unit through the normal deploy path, then re-run this "
        "pre-flight.",
    ))

    state, detail = _check_checkpoint_writer(args.release)
    results.append((
        "slice 1 checkpoint writer", state, detail,
        "Without chat_destination_binding.py in the deployed release the worker never "
        "writes a structured action checkpoint, and the verification engine is never "
        "constructed. The job then dies on the original Bond gap no matter what else is "
        "correct. Fix: deploy a release that contains it.",
    ))

    for label, state, detail, _why in results:
        print(f"  [{state:>7}] {label}: {detail}")

    problems = [(label, why) for label, state, _d, why in results if state == BAD]

    print()
    print("WHAT THIS MEANS")
    print("---------------")
    if not problems:
        print("  Everything the milestone depends on is in place. Record a baseline, seed")
        print("  ONE Bond-like Chat job against test applicant 220250093, then evaluate.")
        return 0

    for n, (label, why) in enumerate(problems, 1):
        print(f"  {n}. {label}")
        for line in why.split(". "):
            line = line.strip()
            if line:
                print(f"     {line}{'' if line.endswith('.') else '.'}")
        print()
    print(f"  {len(problems)} problem(s). Do not seed a job yet — a run now would fail for")
    print("  these reasons and the verdict would look like a code fault instead.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
