#!/usr/bin/env python3
"""Pre-flight for the completion milestone. Read-only, no side effects.

Four things can make a milestone run print a verdict that looks like a code
failure and is not one. Check them before seeding anything:

  1. The gateway service runs with ROBIE_ENV set. Every Secret Manager read
     fails closed without it.
  2. Secret Manager reachable for the EZLynx PolicyApi credentials, using the
     environment the gateway itself runs with. The Chat destination verifier
     reads through it and fails CLOSED if absent.
  3. The running service has actually picked up the configuration on disk.
     Writing an EnvironmentFile changes nothing until the unit restarts.
  4. Slice 1's action checkpoint writer present in the deployed release, and the
     Chat verifiers actually register.

This script runs over an ad-hoc ssh session, which inherits none of the unit's
Environment= or EnvironmentFile=. Rather than inventing values (which would
report on a box that does not exist) or ignoring them (which reports a failure
the service does not have), it reads the unit's own environment out of systemd
and uses exactly that.

Exit 0 when everything needed for the milestone is in place, 1 otherwise.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

OK = "ok"
BAD = "PROBLEM"


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
    for item in _unit_property(unit, "Environment").split():
        if "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    if _unit_property(unit, "Environment"):
        sources.append(f"{unit} Environment=")
    return env, sources


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


def _check_secret_manager(overlay: dict[str, str], sources: list[str]) -> tuple[str, str]:
    label = ", ".join(sources) if sources else "this shell only"
    try:
        from robie_job_engine.ezlynx_api import load_ezlynx_api_config
    except Exception as exc:
        return BAD, f"cannot import ezlynx_api: {type(exc).__name__}: {exc}"
    saved = {key: os.environ.get(key) for key in overlay}
    os.environ.update(overlay)
    try:
        load_ezlynx_api_config()
    except Exception as exc:
        return BAD, (
            f"Secret Manager unreachable using the gateway's own environment "
            f"({label}): {type(exc).__name__}: {exc}"
        )
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
    return OK, f"EZLynx PolicyApi config loaded using {label}"


def _check_checkpoint_writer(release: Path) -> tuple[str, str]:
    target = release / "robie_job_engine" / "chat_destination_binding.py"
    if not target.is_file():
        return BAD, f"missing {target} — the engine is never constructed without it"
    return OK, str(target)


def _check_chat_verifiers() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    try:
        from robie_job_engine.chat_guard import _default_chat_verifiers, _UnavailableVerifier
    except Exception as exc:
        return [("chat_guard", BAD, f"import failed: {type(exc).__name__}: {exc}")]
    try:
        registry = _default_chat_verifiers()
    except Exception as exc:
        return [("_default_chat_verifiers", BAD, f"raised: {type(exc).__name__}: {exc}")]
    for action in ("hermes.google_chat_task", "browser.read"):
        got = registry.get(action)
        if got is None:
            rows.append((action, BAD, "not registered at all"))
        elif isinstance(got, _UnavailableVerifier):
            rows.append((action, BAD, f"failed to register: {got.reason}"))
        else:
            rows.append((action, OK, type(got).__name__))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release", required=True, type=Path, help="deployed release root")
    ap.add_argument("--unit", action="append", default=None,
                    help="gateway unit to probe; repeatable. Default: robie-gateway, hermes-gateway")
    args = ap.parse_args(argv)

    units = args.unit or ["robie-gateway", "hermes-gateway"]
    unit, seen = _loaded_gateway_unit(units)
    unit_env, env_sources = _unit_environment(unit)

    print(f"release:   {args.release.resolve()}")
    print(f"ROBIE_ENV: {os.environ.get('ROBIE_ENV') or '(unset in this shell)'} in this shell; "
          f"{unit_env.get('ROBIE_ENV') or '(unset)'} in {unit or 'no gateway unit'}")
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
            "Everything below that depends on the unit's environment is reading an empty "
            "one, so treat those results as unknown rather than failing."
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

    state, detail = _check_secret_manager(unit_env, env_sources)
    results.append((
        "Secret Manager (EZLynx PolicyApi)", state, detail,
        "The box cannot load EZLynx API credentials, so the Chat destination verifier "
        "can register but every read it makes will fail. This check uses the gateway "
        "unit's own Environment= and EnvironmentFile= values, so a failure here is a "
        "failure the service really has. Fix: check that the EnvironmentFile names "
        "ROBIE_EZLYNX_API_UAT_SECRET as a full resource name "
        "(projects/NNN/secrets/NAME/versions/latest), and that this VM's service "
        "account holds roles/secretmanager.secretAccessor on that secret.",
    ))

    state, detail = _check_config_is_live(unit)
    results.append((
        "running service has the current config", state, detail,
        "The configuration on disk is right but the running process predates it. "
        "systemd reads an EnvironmentFile once, at start, so an edit does nothing until "
        "the unit restarts. Everything else here can be green while the live service "
        "still fails. Fix: restart the gateway unit through the normal deploy path, then "
        "re-run this pre-flight.",
    ))

    state, detail = _check_checkpoint_writer(args.release)
    results.append((
        "slice 1 checkpoint writer", state, detail,
        "Without chat_destination_binding.py in the deployed release the worker never "
        "writes a structured action checkpoint, and the verification engine is never "
        "constructed. The job then dies on the original Bond gap no matter what else is "
        "correct. Fix: deploy a release that contains it.",
    ))

    for action, state, detail in _check_chat_verifiers():
        if action in ("chat_guard", "_default_chat_verifiers"):
            results.append((
                f"chat verifier registry ({action})", state, detail,
                "The registry itself could not be built, so no Chat action has a verifier "
                "and every Chat job will terminate UNVERIFIED. The quoted exception is the "
                "whole story — this is a code or dependency problem in the deployed "
                "release, not a configuration one.",
            ))
            continue
        results.append((
            f"verifier {action}", state, detail,
            f"No verifier is reachable for {action}, so every such job terminates "
            "UNVERIFIED however well the worker performed. If the detail says "
            "'failed to register', an import raised at startup and the reason is quoted "
            "— that is a code or dependency problem. If it says 'not registered at all', "
            "nothing ever wired it.",
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
