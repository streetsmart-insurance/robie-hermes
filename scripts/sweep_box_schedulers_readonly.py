#!/usr/bin/env python3
"""Sweep hermes-poc-01 for the hourly inbox processor. STRICTLY read-only.

Something on this box fires every hour at ~:00:16-:00:28 ET: it sends
"Robie Daily Mailbox Audit & Cleanup Report" and "[URGENT / CSR ACTION]
Carrier Response Received" emails as robie@streetsmart.insurance and logs
notes to EZLynx via REST API. It is NOT in the job engine's scheduled_jobs
table, NOT in the repo, and NOT a repo systemd timer. This sweep looks for
it in the remaining scheduler surfaces: systemd timers/units, user and
system crontabs, and recent process starts.

Touches nothing:
- systemctl/journalctl/crontab queries are read-only; no enable/start/stop.
- No file is written anywhere (output goes to stdout only).
- Anything that looks like a secret value is redacted before printing.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

KEYWORDS = ("mailbox", "audit", "urgent", "cleanup", "cleanup_report",
            "carrier response", "carrier_response", "inbox")

USERS = ("streetsmart-hermes", "ubuntu", "root")

CRON_DIRS = ("/etc/cron.d", "/etc/cron.hourly", "/etc/cron.daily")

SYSTEMD_UNIT_GLOB = "/etc/systemd/system"

# Redact assignment-style secrets and JWT-ish tokens before printing.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key|bearer|"
               r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*\S+"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)


def _redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: re.sub(r"\s*[:=]\s*\S+$", "=<redacted>",
                                        m.group(0))
                       if "=" in m.group(0) or ":" in m.group(0)
                       else "<redacted-token>", text)
    return text


def _sh(*args: str, timeout: int = 25) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - probe must not crash
        return f"<could not run {' '.join(args)}: {type(exc).__name__}: {exc}>"
    combined = ((out.stdout or "") + (out.stderr or "")).strip()
    return _redact(combined)


def _sh_shell(cmd: str, timeout: int = 25) -> str:
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - probe must not crash
        return f"<could not run shell: {type(exc).__name__}: {exc}>"
    return _redact(((out.stdout or "") + (out.stderr or "")).strip())


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _matches_keywords(text: str) -> list[str]:
    low = text.lower()
    return sorted({kw for kw in KEYWORDS if kw in low})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=3,
                    help="how far back to look for hourly process starts")
    args = ap.parse_args(argv)

    findings: list[str] = []

    section("1. All systemd timers (enabled, disabled, and static)")
    timers = _sh("systemctl", "list-timers", "--all", "--no-pager")
    print(timers or "  <no timers listed>")
    hits = _matches_keywords(timers)
    if hits:
        findings.append(
            "A systemd timer name/description matches keywords "
            f"{hits}. The timer list above shows its schedule and unit."
        )

    section("2. All installed timer and service unit files")
    unit_files = _sh("systemctl", "list-unit-files",
                     "--type=timer,service", "--no-pager")
    print(unit_files or "  <no unit files listed>")
    hits = _matches_keywords(unit_files)
    if hits:
        findings.append(
            "A unit file name matches keywords "
            f"{hits}. Section 3 shows the matching unit definitions."
        )

    section("3. Unit file contents matching inbox keywords")
    matched_units: list[str] = []
    for line in unit_files.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.endswith((".timer", ".service")) and _matches_keywords(name):
            matched_units.append(name)
    # Also grep the raw unit files on disk for keyword content.
    grep_out = _sh_shell(
        "grep -rilE 'mailbox|urgent|cleanup|carrier.response|inbox' "
        f"{SYSTEMD_UNIT_GLOB}/*.timer {SYSTEMD_UNIT_GLOB}/*.service "
        "2>/dev/null | head -20"
    )
    for path in grep_out.splitlines():
        path = path.strip()
        if path and path not in matched_units:
            matched_units.append(path)
    if not matched_units:
        print("  <no unit files match the inbox keywords>")
    for unit in matched_units:
        print(f"  --- {unit} ---")
        if unit.startswith("/"):
            body = _sh_shell(f"sed -n '1,60p' '{unit}'")
        else:
            body = _sh("systemctl", "cat", unit)
        print("  " + body.replace("\n", "\n  ") if body else "  <empty>")
        timer_show = ""
        if unit.endswith(".timer") and not unit.startswith("/"):
            timer_show = _sh("systemctl", "show", unit, "-p",
                             "OnCalendar", "-p", "NextElapseUSecRealtime",
                             "--value")
            print("  OnCalendar/Next: " + (timer_show or "<n/a>"))
    if matched_units:
        findings.append(
            f"{len(matched_units)} unit file(s) matched the inbox keywords "
            "(listed above with their definitions). Check the ExecStart= line "
            "and the timer's OnCalendar= for the hourly trigger."
        )

    section("4. User crontabs (streetsmart-hermes, ubuntu, root)")
    for user in USERS:
        print(f"  --- crontab -l -u {user} ---")
        out = _sh("crontab", "-l", "-u", user)
        print("  " + (out.replace("\n", "\n  ") if out else "<empty>"))
        if _matches_keywords(out):
            findings.append(
                f"The crontab for user {user} contains an inbox keyword. "
                "The entry above is the hourly trigger candidate."
            )

    section("5. System cron directories")
    for d in CRON_DIRS:
        print(f"  --- {d} ---")
        listing = _sh("ls", "-la", d)
        print("  " + (listing.replace("\n", "\n  ") if listing else "<empty>"))
    cron_d = _sh_shell(
        "for f in /etc/cron.d/*; do [ -f \"$f\" ] && "
        "{ echo \"--- $f ---\"; sed -n '1,40p' \"$f\"; }; done 2>/dev/null"
    )
    print(cron_d or "  <no /etc/cron.d files>")
    if _matches_keywords(cron_d):
        findings.append(
            "A file under /etc/cron.d matches the inbox keywords. "
            "The entry above is the hourly trigger candidate."
        )

    section(f"6. Process starts in the last {args.hours} hour(s) "
            "(cron/systemd, bounded)")
    starts = _sh_shell(
        "journalctl --since '" + str(args.hours) + " hours ago' --no-pager "
        "-o short 2>/dev/null | grep -iE 'CRON|started|starting' | "
        "grep -viE 'session|user@|sshd' | tail -40"
    )
    print(starts or "  <no matching journal entries>")
    kw_starts = [l for l in starts.splitlines()
                 if _matches_keywords(l)]
    if kw_starts:
        findings.append(
            "Recent process starts match the inbox keywords:\n    " +
            "\n    ".join(kw_starts[:10])
        )

    section("7. Anything running right now that looks like the processor")
    ps = _sh_shell(
        "ps -eo pid,etime,cmd --no-headers 2>/dev/null | "
        "grep -iE 'mailbox|audit|cleanup|urgent|inbox' | grep -v grep | head -20"
    )
    print(ps or "  <no matching processes right now>")

    section("WHAT THIS MEANS")
    if not findings:
        print("  No systemd timer, unit file, crontab, or recent process start")
        print("  matched the inbox keywords. The hourly processor is therefore")
        print("  probably NOT scheduled on this box via systemd or cron — next")
        print("  places to look: another VM (e.g. streetsmart-accountability-prod),")
        print("  a container/CI schedule, or an external scheduler (Zapier etc.).")
        return 1
    for n, item in enumerate(findings, 1):
        print(f"  {n}. {item}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
