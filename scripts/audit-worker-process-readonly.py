#!/usr/bin/env python3
"""Read-only audit: is the live ROBIE worker actually running the deployed release?

Checks (never mutates, never prints secrets):
  1. /opt/streetsmart-hermes/current symlink target + activation time
     (symlink ctime = last pointer flip)
  2. sha256 of robie_job_engine/ezlynx_api.py in the live release and whether
     _urlopen uses http.client.HTTPSConnection (the #347 fix marker)
  3. every Python process whose cmdline mentions robie/hermes/ezlynx:
     pid, start time, cmdline (truncated), cwd, exe
  4. per-process verdict: started BEFORE the pointer flip -> STALE
     (predates the release); started AFTER -> CURRENT

Prints a single JSON document to stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

PROC_RE = re.compile(r"^[0-9]+$")
INTERESTING_RE = re.compile(r"robie|hermes|ezlynx", re.IGNORECASE)


def _read_proc_text(pid: str, name: str, binary: bool = False) -> str | None:
    try:
        data = Path(f"/proc/{pid}/{name}").read_bytes()
    except OSError:
        return None
    if binary:
        return data.decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


def _boot_time() -> float | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return None


def _clk_tck() -> int:
    try:
        return os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    except (AttributeError, KeyError, ValueError):
        return 100


def _proc_start_epoch(pid: str, boot: float, clk_tck: int) -> float | None:
    raw = _read_proc_text(pid, "stat")
    if not raw:
        return None
    # comm may contain spaces/parens; starttime is field 22 (1-based) after comm.
    m = re.search(r"\)\s*(.*)$", raw)
    if not m:
        return None
    fields = m.group(1).split()
    if len(fields) < 20:
        return None
    try:
        start_ticks = int(fields[19])
    except ValueError:
        return None
    return boot + start_ticks / clk_tck


def check_pointer(opt_root: str, expect_sha: str) -> dict:
    link = Path(opt_root) / "current"
    out: dict = {"link": str(link)}
    try:
        target = os.readlink(link)
    except OSError as exc:
        return {**out, "ok": False, "error": f"readlink: {exc}"}
    out["target"] = target
    try:
        st = os.lstat(link)
        activated = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
        out["pointer_activated_utc"] = activated.isoformat()
        out["pointer_activated_epoch"] = st.st_ctime
    except OSError as exc:
        out["pointer_activated_error"] = str(exc)
    out["matches_expect_sha"] = expect_sha in target
    out["ok"] = True
    return out


def check_engine_source(release_root: str) -> dict:
    path = Path(release_root) / "robie_job_engine" / "ezlynx_api.py"
    out: dict = {"path": str(path), "exists": path.is_file()}
    if not out["exists"]:
        return {**out, "ok": False}
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {**out, "ok": False, "error": str(exc)}
    out["sha256"] = hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8", errors="replace")
    m = re.search(r"def _urlopen\(.*?\n(?=def |\Z)", text, re.DOTALL)
    urlopen_src = m.group(0) if m else ""
    out["has_def_urlopen"] = bool(m)
    out["urlopen_uses_http_client"] = "http.client.HTTPSConnection" in urlopen_src
    out["urlopen_uses_urlopen"] = "urllib.request.urlopen" in urlopen_src
    out["ok"] = True
    return out


def scan_processes() -> list[dict]:
    boot = _boot_time()
    clk_tck = _clk_tck()
    found: list[dict] = []
    try:
        pids = [p for p in os.listdir("/proc") if PROC_RE.match(p)]
    except OSError as exc:
        return [{"error": f"cannot list /proc: {exc}"}]
    for pid in sorted(pids, key=int):
        raw = _read_proc_text(pid, "cmdline")
        if not raw:
            continue
        cmdline = raw.replace("\x00", " ").strip()
        if not cmdline or not INTERESTING_RE.search(cmdline):
            continue
        entry: dict = {"pid": int(pid), "cmdline": cmdline[:400]}
        try:
            entry["cwd"] = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        try:
            entry["exe"] = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            pass
        if boot is not None:
            start = _proc_start_epoch(pid, boot, clk_tck)
            if start is not None:
                entry["started_epoch"] = start
                entry["started_utc"] = datetime.fromtimestamp(
                    start, tz=timezone.utc
                ).isoformat()
        found.append(entry)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-sha", required=True)
    ap.add_argument("--opt-root", default="/opt/streetsmart-hermes")
    args = ap.parse_args()

    pointer = check_pointer(args.opt_root, args.expect_sha)
    release_root = str(Path(args.opt_root) / "current")
    engine = check_engine_source(release_root)
    processes = scan_processes()

    activated = pointer.get("pointer_activated_epoch")
    for proc in processes:
        started = proc.get("started_epoch")
        if activated is None or started is None:
            proc["release_verdict"] = "UNKNOWN"
        elif started < activated - 1:
            proc["release_verdict"] = "STALE (started before pointer flip)"
        else:
            proc["release_verdict"] = "CURRENT (started after pointer flip)"

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "expect_sha": args.expect_sha,
        "pointer": pointer,
        "engine_source": engine,
        "processes": processes,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
