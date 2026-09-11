#!/usr/bin/env python3
"""Pre-flight for the completion milestone. Read-only, no side effects.

Three things can make a milestone run print a verdict that looks like a code
failure and is not one. Check them before seeding anything:

  1. Secret Manager reachable for the EZLynx PolicyApi credentials. The Chat
     destination verifier reads through it and fails CLOSED if absent.
  2. Slice 1's action checkpoint writer present in the deployed release. Without
     it the verification engine is never constructed and the job dies on the
     original Bond gap regardless of everything else.
  3. The Chat verifiers actually register. _default_chat_verifiers swallows
     import errors; since 0a4dd73 a failure registers a placeholder that names
     the cause instead of leaving the slot empty, and this reports either.

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


def _check_gateway_env(unit: str) -> tuple[str, str]:
    """What ROBIE_ENV does the gateway service itself run with?

    An ad-hoc ssh session does not inherit the unit's Environment=, so a
    preflight that sets its own ROBIE_ENV can pass while the real service has
    none. Read it from the unit rather than assuming.
    """
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        return OK, f"could not read unit {unit} ({type(exc).__name__}) - informational"
    text = (out.stdout or "").strip()
    if not text or text == "Environment=":
        return BAD, f"unit {unit} sets no Environment= at all"
    for item in text.removeprefix("Environment=").split():
        if item.startswith("ROBIE_ENV="):
            return OK, f"unit {unit} runs with {item}"
    return BAD, f"unit {unit} does not set ROBIE_ENV (has: {text[:120]})"


def _check_secret_manager() -> tuple[str, str]:
    try:
        from robie_job_engine.ezlynx_api import load_ezlynx_api_config
    except Exception as exc:
        return BAD, f"cannot import ezlynx_api: {type(exc).__name__}: {exc}"
    try:
        load_ezlynx_api_config()
    except Exception as exc:
        return BAD, f"Secret Manager unreachable: {type(exc).__name__}: {exc}"
    return OK, "EZLynx PolicyApi config loaded"


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
    ap.add_argument("--unit", default="hermes-gateway", help="gateway systemd unit to inspect")
    args = ap.parse_args(argv)

    problems = 0
    print(f"release:   {args.release.resolve()}")
    print(f"ROBIE_ENV: {os.environ.get('ROBIE_ENV') or '(unset in this shell)'}")
    print()

    for label, (state, detail) in (
        ("gateway unit ROBIE_ENV", _check_gateway_env(args.unit)),
        ("Secret Manager (EZLynx PolicyApi)", _check_secret_manager()),
        ("slice 1 checkpoint writer", _check_checkpoint_writer(args.release)),
    ):
        problems += state == BAD
        print(f"  [{state:>7}] {label}: {detail}")

    for action, state, detail in _check_chat_verifiers():
        problems += state == BAD
        print(f"  [{state:>7}] verifier {action}: {detail}")

    print()
    if problems:
        print(f"PRE-FLIGHT FAILED — {problems} problem(s). Do not seed a job yet.")
        return 1
    print("PRE-FLIGHT OK — safe to record a baseline and seed one Bond-like job.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
