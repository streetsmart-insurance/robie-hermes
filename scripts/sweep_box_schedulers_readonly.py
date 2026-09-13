#!/usr/bin/env python3
"""Sweep hermes-poc-01 for the hourly inbox processor. STRICTLY read-only.

Known state (2026-09-12): the ONLY inbox scheduler on this box is
/etc/systemd/system/hermes-email-watcher.timer -> hermes-email-watcher.service
("Poll robie@streetsmart.insurance inbox every 60 seconds"). Crontabs are
empty and there is NO separate hourly trigger. Yet "Robie Daily Mailbox Audit
& Cleanup Report" and "[URGENT / CSR ACTION] Carrier Response Received"
emails go out HOURLY at ~:00, so the hourly cadence lives inside whatever
hermes-email-watcher.service executes. This sweep captures that service's
full definition, its ExecStart target (with symlinks resolved), the first
200 lines of the target plus any hourly/time-gating logic, and a names-only
listing of the hermes home directories that may hold the audit logic.

Touches nothing:
- systemctl/journalctl/crontab/ls/readlink/file/sed/grep are read-only;
  no enable/start/stop, no file writes anywhere (stdout only).
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

WATCHER_UNIT = "hermes-email-watcher.service"
WATCHER_HOME = "/home/streetsmart-hermes/.hermes"

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


def _execstart_target(execstart_line: str) -> str:
    """Best-effort: extract the executed script/binary from an ExecStart.

    Handles both the plain `ExecStart=/path args...` form and the
    `systemctl show` structured form `{ path=/bin/x ; argv[]=/bin/x ; ... }`.
    Prefers a script file (.py/.sh) passed to an interpreter over the
    interpreter itself.
    """
    s = execstart_line.strip()
    argv_tokens = re.findall(r"argv\[\]=([^\s;}]+)", s)
    path_m = re.search(r"path=([^\s;}]+)", s)
    candidates: list[str] = []
    for tok in argv_tokens:
        t = tok.lstrip("-+!@:")
        if not t:
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) and "/" not in t.split("=", 1)[0]:
            continue
        candidates.append(t)
    for tok in candidates:
        if tok.endswith((".py", ".sh", ".pl", ".rb")):
            return tok
    if candidates:
        return candidates[0]
    if path_m:
        return path_m.group(1)
    if s.startswith("ExecStart="):
        s = s[len("ExecStart="):]
    plain: list[str] = []
    for tok in s.split():
        t = tok.lstrip("-+!@:")
        if not t:
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) and "/" not in t.split("=", 1)[0]:
            continue
        plain.append(t)
    for tok in plain:
        if tok.endswith((".py", ".sh", ".pl", ".rb")):
            return tok
    if plain:
        return plain[0]
    return ""


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

    section("8. Email-watcher service deep capture (read-only)")
    print(f"  --- systemctl cat {WATCHER_UNIT} ---")
    unit_body = _sh("systemctl", "cat", WATCHER_UNIT)
    print("  " + (unit_body.replace("\n", "\n  ")
                  if unit_body else "  <unit not found>"))
    print(f"  --- systemctl show {WATCHER_UNIT} ExecStart ---")
    execstart = _sh("systemctl", "show", WATCHER_UNIT, "-p", "ExecStart",
                    "--value")
    print("  " + (execstart.replace("\n", "\n  ") if execstart else "  <n/a>"))
    target = _execstart_target(execstart) if execstart else ""
    real = ""
    if target:
        resolved = _sh_shell(f"readlink -f '{target}'").splitlines()
        real = resolved[0] if resolved else ""
        print(f"  --- resolved target: {target} -> {real or '<unresolvable>'}")
        if real:
            ftype = _sh_shell(f"file -b '{real}'")
            print(f"  file: {ftype or '<unknown>'}")
            if "text" in (ftype or "").lower():
                head = _sh_shell(f"sed -n '1,200p' '{real}'")
                print("  --- first 200 lines of the target ---")
                print("  " + (head.replace("\n", "\n  ")
                              if head else "  <empty>"))
                gate = _sh_shell(
                    "grep -inE 'hour|3600|minute\\s*==\\s*0|schedule|audit|"
                    "URGENT|cleanup|mailbox|send.*report' "
                    f"'{real}' | head -60"
                )
                print("  --- hourly/time-gating keyword hits in the target ---")
                print("  " + (gate.replace("\n", "\n  ")
                              if gate else "  <none>"))
                if gate:
                    findings.append(
                        "The email-watcher's ExecStart target "
                        f"({real}) contains hourly/time-gating logic "
                        "(keyword hits shown in section 8). That is where "
                        "the hourly mailbox-audit/URGENT cadence lives — "
                        "there is no separate hourly timer on this box."
                    )
            else:
                print("  <binary file — contents not dumped>")
    _absent = ("not found", "no files found", "could not be found",
               "no such file", "<could not run")
    if unit_body and not any(m in unit_body.lower() for m in _absent):
        findings.append(
            f"{WATCHER_UNIT} captured in full (section 8). "
            f"ExecStart resolves to: {real or target or '<unresolvable>'}."
        )

    section("9. hermes home directory listing (names only)")
    print(f"  --- ls -la {WATCHER_HOME} ---")
    hermes_ls = _sh("ls", "-la", WATCHER_HOME)
    print("  " + (hermes_ls.replace("\n", "\n  ")
                  if hermes_ls else "  <not accessible>"))
    for sub in ("skills", "tasks"):
        subp = str(Path(WATCHER_HOME) / sub)
        print(f"  --- ls -la {subp} (names only) ---")
        out = _sh("ls", "-la", subp)
        print("  " + (out.replace("\n", "\n  ") if out else "  <absent>"))

    section("WHAT THIS MEANS")
    if not findings:
        print("  No systemd timer, unit file, crontab, or recent process start")
        print("  matched the inbox keywords, and the email-watcher service")
        print("  could not be captured. The hourly processor is therefore")
        print("  probably NOT driven from this box — next places to look:")
        print("  another VM (e.g. streetsmart-accountability-prod), a")
        print("  container/CI schedule, or an external scheduler (Zapier etc.).")
        return 1
    for n, item in enumerate(findings, 1):
        print(f"  {n}. {item}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
