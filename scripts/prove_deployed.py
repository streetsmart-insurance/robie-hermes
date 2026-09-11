#!/usr/bin/env python3
"""Did the deploy actually happen? Checks the running tree, not the report.

Separate from verify_engine_working.py on purpose. Two different questions:

    prove_deployed.py        did the files land and get registered?
    verify_engine_working.py did that change anything?

An agent saying "deployed" is a claim. This reads the release tree the
gateway is actually loading and prints facts with file hashes and
timestamps. Paste the whole output; a summary of it is worth nothing.

    python3 prove_deployed.py
    python3 prove_deployed.py --release /opt/streetsmart-hermes/releases/current
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_RELEASE = "/opt/streetsmart-hermes/releases/current"

REQUIRED = [
    "chat_destination_binding.py",
    "chat_ezlynx_destination_verifier.py",
    "ezlynx_api_read_port.py",
]

# What must be true in the deployed chat_guard.py for the verifier to exist.
REGISTRATION_MARKERS = [
    ("verifier class imported", "HermesChatEzlynxDestinationVerifier"),
    ("read port imported", "EzlynxApiClientReadPort"),
    ("registered for the Chat action type", '"hermes.google_chat_task"'),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def age(path: Path) -> str:
    mins = (time.time() - path.stat().st_mtime) / 60.0
    if mins < 90:
        return f"{mins:.0f} min ago"
    return f"{mins / 60:.1f} hours ago"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default=DEFAULT_RELEASE, type=Path)
    args = ap.parse_args(argv)

    root: Path = args.release
    print(f"release tree: {root}")
    if root.is_symlink():
        print(f"  -> {os.readlink(root)}")
    if not root.is_dir():
        print("FAIL  release tree does not exist")
        return 2

    failures: list[str] = []

    # -- 1. are the modules actually there? --------------------------- #
    print("\n1. MODULES IN THE RELEASE TREE")
    found: dict[str, Path] = {}
    for name in REQUIRED:
        hits = list(root.rglob(name))
        if not hits:
            print(f"   MISSING  {name}")
            failures.append(f"{name} is not in the release tree")
            continue
        p = hits[0]
        found[name] = p
        print(f"   present  {name}")
        print(f"            {p}")
        print(f"            sha256:{sha(p)}  modified {age(p)}")

    # -- 2. is the verifier actually registered? ---------------------- #
    print("\n2. REGISTRATION IN THE DEPLOYED chat_guard.py")
    guards = list(root.rglob("chat_guard.py"))
    if not guards:
        print("   FAIL  no chat_guard.py in the release tree")
        failures.append("chat_guard.py not found")
    else:
        guard = guards[0]
        text = guard.read_text(encoding="utf-8", errors="ignore")
        print(f"   file     {guard}")
        print(f"            sha256:{sha(guard)}  modified {age(guard)}")
        for label, marker in REGISTRATION_MARKERS:
            ok = marker in text
            print(f"   {'yes' if ok else 'NO ':<8} {label}")
            if not ok:
                failures.append(f"chat_guard.py: {label} — not present")

    # -- 3. is the gateway running this code? ------------------------- #
    # The unit is named differently across boxes: robie-gateway on Test,
    # hermes-gateway on Production. Checking one hardcoded name made this
    # print "NOT DEPLOYED - hermes-gateway is inactive" on a Test box where
    # that unit does not exist and robie-gateway was running fine. A
    # confident answer about the wrong unit is worse than no answer.
    print("\n3. IS THE GATEWAY RUNNING SINCE THE DEPLOY?")
    try:
        unit = None
        checked = []
        for candidate in ("robie-gateway", "hermes-gateway"):
            load = subprocess.run(
                ["systemctl", "show", candidate, "-p", "LoadState", "--value"],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            if load == "loaded":
                unit = candidate
                break
            checked.append(f"{candidate}={load or 'absent'}")
        if unit is None:
            print("   no gateway unit found - checked " + ", ".join(checked))
            failures.append(
                "no gateway unit found under any known name (checked "
                + ", ".join(checked)
                + ") - this says nothing about whether the deploy worked"
            )
        else:
            print(f"   {'unit':<22} {unit}")
            out = subprocess.run(
                ["systemctl", "show", unit,
                 "--property=ActiveState,ActiveEnterTimestamp,MainPID"],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            props = dict(
                line.split("=", 1) for line in out.splitlines() if "=" in line
            )
            for k, v in props.items():
                print(f"   {k:<22} {v}")
            if props.get("ActiveState") != "active":
                failures.append(f"{unit} is {props.get('ActiveState')}, not active")
            # A restart older than the newest deployed file means the running
            # process predates the change and has the OLD code in memory.
            newest = max((p.stat().st_mtime for p in found.values()), default=0)
            stamp = props.get("ActiveEnterTimestamp", "")
            if newest and stamp:
                print(f"   newest module written  {time.ctime(newest)}")
                print("   ^ if the gateway entered active BEFORE that line,")
                print("     it is still running the old code. Restart it.")
    except FileNotFoundError:
        print("   (systemctl not available here — check manually)")
    except Exception as exc:
        print(f"   could not read service state: {type(exc).__name__}: {exc}")

    # -- verdict ------------------------------------------------------ #
    print("\n" + "=" * 60)
    if failures:
        print("NOT DEPLOYED")
        for f in failures:
            print(f"  - {f}")
        print("\nThe deploy did not fully happen. Do not run a test job yet.")
        return 1
    print("DEPLOYED")
    print("  Modules present, verifier registered, gateway active.")
    print("  Next: verify_engine_working.py --baseline was needed BEFORE this.")
    print("  If you have a baseline, run a job then verify_engine_working.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
