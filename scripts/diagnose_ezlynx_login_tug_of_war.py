#!/usr/bin/env python3
"""Collect a narrow, read-only EZLynx login incident timeline.

The script is copied to a Hermes VM by a protected GitHub Actions workflow.
It never reads secret payloads and never drives Chrome.  Output is one JSON
document containing only service/auth events, sanitized EZLynx route classes,
and metadata for any ad-hoc re-auth script found in /tmp.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


UNITS = (
    "hermes-gateway",
    "robie-scheduler",
    "robie-ezlynx-session",
    "robie-ezlynx-browser",
    "robie-ezlynx-browser-test",
    "robie-ezlynx-keepalive-test",
)
INTERESTING = re.compile(
    r"(?i)(ezlynx|login|logout|auth|session|keepalive|bootstrap|re_auth|"
    r"session_preflight|modulenotfounderror)"
)
SECRETISH = re.compile(
    r"(?i)(password|passwd|secret|token|authorization|cookie)"
    r"\s*[:=]\s*([^\s,;]+)"
)
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
LONG_NUMBER = re.compile(r"\b\d{7,}\b")


def run(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 - evidence collection must continue
        return 127, f"{type(exc).__name__}: {exc}"


def sanitize(text: str) -> str:
    text = SECRETISH.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = IP.sub("[IP_REDACTED]", text)
    # Preserve timestamps/PIDs while hiding applicant, policy, phone, and other
    # long numeric identifiers that can appear in URLs or command arguments.
    return LONG_NUMBER.sub("[LONG_ID_REDACTED]", text)[:1000]


def journal_events(since: str, until: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for unit in UNITS:
        rc, out = run(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                since,
                "--until",
                until,
                "--no-pager",
                "-o",
                "short-iso",
            ]
        )
        for line in out.splitlines():
            if INTERESTING.search(line):
                events.append({"unit": unit, "line": sanitize(line)})
        if rc not in (0, 1):
            events.append({"unit": unit, "line": f"journal exit {rc}"})
    return events[-500:]


def auth_events(since: str, until: str) -> list[str]:
    # journalctl supplies the time bound.  Keep only login/sudo/command evidence
    # relevant to the incident and strip source IP addresses.
    rc, out = run(
        [
            "journalctl",
            "--since",
            since,
            "--until",
            until,
            "--no-pager",
            "-o",
            "short-iso",
        ]
    )
    if rc not in (0, 1):
        return [f"ssh journal exit {rc}"]
    wanted = re.compile(
        r"(?i)(accepted publickey|session opened|session closed|sudo|command|"
        r"re_auth_ezlynx|ezlynx_login_bootstrap)"
    )
    return [sanitize(line) for line in out.splitlines() if wanted.search(line)][-300:]


def systemd_inventory() -> dict[str, object]:
    units: dict[str, object] = {}
    for unit in UNITS:
        _, out = run(
            [
                "systemctl",
                "show",
                unit,
                "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStartTimestamp,"
                "ActiveEnterTimestamp,FragmentPath",
            ]
        )
        units[unit] = {
            key: sanitize(value)
            for key, _, value in (line.partition("=") for line in out.splitlines())
            if key
        }
    _, timers = run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
    timer_lines = [sanitize(line) for line in timers.splitlines() if INTERESTING.search(line)]
    return {"units": units, "timers": timer_lines}


def adhoc_script_metadata() -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in sorted(Path("/tmp").glob("*auth*ezlynx*.py")):
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            found.append({"path": str(path), "error": type(exc).__name__})
            continue
        found.append(
            {
                "path": str(path),
                "uid": stat.st_uid,
                "gid": stat.st_gid,
                "mode": oct(stat.st_mode & 0o777),
                "size": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "sha256": digest,
            }
        )
    return found


def chrome_time_to_iso(value: int) -> str:
    unix_micros = value - 11644473600000000
    return datetime.fromtimestamp(unix_micros / 1_000_000, tz=timezone.utc).isoformat()


def route_class(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return "unparseable"
    host = parsed.hostname or ""
    path = parsed.path.casefold()
    if not host.endswith("ezlynx.com"):
        return "non-ezlynx"
    if "/auth/account/login" in path:
        return "ezlynx-login"
    if "/auth/twofactorverification" in path:
        return "ezlynx-two-factor"
    if path.startswith("/web/"):
        return "ezlynx-authenticated-web"
    return "ezlynx-other"


def chrome_history(profile_root: Path, since: str, until: str) -> list[dict[str, object]]:
    history = profile_root / "Default" / "History"
    if not history.is_file():
        return [{"error": "History not found", "path": str(history)}]
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    end = datetime.fromisoformat(until.replace("Z", "+00:00"))
    chrome_epoch = 11644473600000000
    lower = int(start.timestamp() * 1_000_000) + chrome_epoch
    upper = int(end.timestamp() * 1_000_000) + chrome_epoch
    with tempfile.TemporaryDirectory(prefix="robie-history-") as td:
        copy = Path(td) / "History"
        shutil.copy2(history, copy)
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT v.visit_time, u.url, substr(coalesce(u.title,''),1,120) "
                "FROM visits v JOIN urls u ON u.id=v.url "
                "WHERE v.visit_time BETWEEN ? AND ? AND u.url LIKE '%ezlynx.com%' "
                "ORDER BY v.visit_time",
                (lower, upper),
            ).fetchall()
        finally:
            con.close()
    return [
        {
            "visited_at_utc": chrome_time_to_iso(int(visit_time)),
            "route": route_class(str(url)),
            "title": sanitize(str(title)),
        }
        for visit_time, url, title in rows
    ][-500:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", choices=("PRODUCTION", "TEST"), required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--profile-root", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "host": os.uname().nodename,
        "environment": args.environment,
        "window": {"since": args.since, "until": args.until},
        "systemd": systemd_inventory(),
        "journal_events": journal_events(args.since, args.until),
        "ssh_auth_events": auth_events(args.since, args.until),
        "adhoc_reauth_scripts": adhoc_script_metadata(),
        "chrome_history": chrome_history(args.profile_root, args.since, args.until),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
