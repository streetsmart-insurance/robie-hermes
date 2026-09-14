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

The deployed watcher script turned out to be a 9-line ROBIE_ZIP_LOAD_PATH
launcher that exec()s the real agent from releases/current — section 8
follows that indirection and captures the REAL file, since the hourly
"Mailbox Audit & Cleanup" trigger and the duplicate-note bug live in the
deployed code, not in the repo's scripts/robie_email_agent.py.

Section 10 covers the remaining suspect for the hourly report: the Hermes
agent framework's own scheduler (the .hermes/cron/ directories on the box).
It lists cron job definitions (JSON contents, redacted) and hooks, plus
any long-running hermes/cron/scheduler processes.

Section 11 dumps the FULL definitions (systemctl cat) of every robie-*
/ hermes-* unit (services AND timers) regardless of keyword matches —
the keyword filter missed hourly timers that fire at :00
(robie-production-preflight.timer, robie-ascend-sync.timer,
robie-health-check.timer), which remain audit-trigger suspects.

Section 12 covers the last unchecked surface: the ROBIE Job Engine's own
scheduled-jobs SQLite DB ($ROBIE_JOB_DB or the engine default path). The
engine scheduler ticks every 60s via robie-scheduler.timer and reads the
`schedules` and `scheduled_jobs` tables — an hourly row there would fire
the audit without leaving any systemd/cron/Hermes-cron trace. Read-only:
opens the DB with sqlite3 mode=ro + PRAGMA query_only=ON, dumps tables
and schedule rows (name/title, cron/interval, enabled, run timestamps),
hourly-first, everything through _redact.

Section 14 covers the surfaces sections 1-13 missed: carlo_streetsmart_insurance's crontab (its 5-min call-label cron proves
that user has jobs the sweep never listed), /opt/renewal-automation-system
(a whole second Robie system), the engine DB's report_run_configs /
report_runs tables, and the deployed robie_health_check.py content
(robie-health-check.timer fires hourly at :00).

Touches nothing:
- systemctl/journalctl/crontab/ls/readlink/file/sed/grep are read-only;
  no enable/start/stop, no file writes anywhere (stdout only).
- Anything that looks like a secret value is redacted before printing.
"""
from __future__ import annotations

import argparse
import re
import shlex
import sqlite3
import subprocess
import sys
import urllib.parse
from pathlib import Path

KEYWORDS = ("mailbox", "audit", "urgent", "cleanup", "cleanup_report",
            "carrier response", "carrier_response", "inbox")

WATCHER_UNIT = "hermes-email-watcher.service"
WATCHER_HOME = "/home/streetsmart-hermes/.hermes"

USERS = ("streetsmart-hermes", "ubuntu", "root")

CRON_DIRS = ("/etc/cron.d", "/etc/cron.hourly", "/etc/cron.daily")

SYSTEMD_UNIT_GLOB = "/etc/systemd/system"

# Redact assignment-style secrets and JWT-ish tokens before printing.
#
# The line pattern below masks the VALUE of any NAME=value / NAME: value
# assignment whose NAME merely *contains* key/secret/token/passwd/password
# (case-insensitive) — e.g. Environment=ROBIE_ASCEND_API_KEY=<redacted>.
# The older \b word-boundary pattern missed exactly that case (no boundary
# between "_" and "API"), which leaked a live key in run 34756330819.
_LINE_SECRET_RE = re.compile(
    r"(?i)([\"']?[A-Za-z_][A-Za-z0-9_.]*"
    r"(?:key|secret|token|passwd|password)[A-Za-z0-9_.]*[\"']?"
    r"\s*[:=]\s*)"
    r"(?:\"[^\"\n]*\"|\'[^\'\n]*\'|[^\s\"\']+)"
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key|bearer|"
               r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*\S+"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)


def _redact(text: str) -> str:
    # Line-anchored NAME=value masking first (fail closed on secrets).
    text = _LINE_SECRET_RE.sub(r"\g<1><redacted>", text)
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
    """Best-effort: extract the executed script from an ExecStart line.

    Prefers the LAST argument ending in .py/.sh/.pl/.rb (the script) over
    argv[0] (the interpreter). Handles the `systemctl show` structured
    form `{ path=... ; argv[]=... ; ... }` where a single argv[] token can
    hold the whole command line (this was the run-34756330819 bug: the
    old `[^\\s;}]+` capture stopped at the first space and returned the
    python interpreter instead of the script), and the plain
    `ExecStart=/path args...` form. Skips the interpreter and any -X
    flags; falls back to the first non-flag argument, then argv[0].
    """
    s = execstart_line.strip()
    # In the structured form each argv[] payload runs to the next ';' and
    # may itself contain spaces (the whole command line in one token).
    argv_payloads = re.findall(r"argv\[\]=([^;]+)", s)
    if argv_payloads:
        cmdline = " ".join(part.strip() for part in argv_payloads)
    elif s.startswith("ExecStart="):
        cmdline = s[len("ExecStart="):].strip()
    else:
        cmdline = s
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()
    args = [
        t for t in tokens
        if not (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)
                and "/" not in t.split("=", 1)[0])
    ]
    if not args:
        path_m = re.search(r"path=([^\s;}]+)", s)
        return path_m.group(1) if path_m else ""
    for tok in reversed(args[1:]):
        if tok.endswith((".py", ".sh", ".pl", ".rb")):
            return tok
    for tok in args[1:]:
        if not tok.startswith("-"):
            return tok
    return args[0]


_LOADER_LOAD_PATH_RE = re.compile(
    r"""ROBIE_ZIP_LOAD_PATH\s*=\s*["']([^"']+)["']""")


def _resolve_loader_target(launcher_text: str, launcher_real_path: str,
                           parents_up: int = 2) -> str:
    """Follow the ROBIE_ZIP_LOAD_PATH launcher indirection (pure, testable).

    The deployed watcher script is a tiny launcher (<50 lines) that exec()s
    the real agent from ``<_root>/releases/current/<load_path>`` where
    ``_root`` is ``parents_up`` levels above the launcher file itself
    (the launcher's own ``Path(__file__).resolve().parents[2]``).
    Returns the resolved real path, or "" when the text is not such a
    launcher (50+ lines, or no ROBIE_ZIP_LOAD_PATH assignment).
    """
    lines = launcher_text.splitlines()
    if len(lines) >= 50:
        return ""
    m = _LOADER_LOAD_PATH_RE.search(launcher_text)
    if not m:
        return ""
    load_path = m.group(1).lstrip("/")
    try:
        root = Path(launcher_real_path).parents[parents_up]
    except IndexError:
        return ""
    return str(root / "releases" / "current" / load_path)


HERMES_CRON_DIRS = (
    "/home/streetsmart-hermes/.hermes/cron",
    "/opt/streetsmart-hermes/.hermes/cron",
)
HERMES_HOOKS_DIR = "/opt/streetsmart-hermes/.hermes/hooks"


def _json_files_in_listing(lines: list[str]) -> list[str]:
    """Pure: pull .json file paths out of `find -printf` listing lines.

    Each line is "<path>\\t<size>\\t<mtime>"; paths may contain spaces,
    so the path is everything before the first tab.
    """
    paths = []
    for line in lines:
        path = line.split("\t")[0].strip()
        if path.endswith(".json"):
            paths.append(path)
    return paths


_ROBIE_HERMES_UNIT_RE = re.compile(r"^(robie-|hermes-).*\.(?:timer|service)$")


def _robie_hermes_units(unit_files_text: str) -> list[str]:
    """Pure: robie-* / hermes-* unit names from list-unit-files output.

    Each row is "<unit name>  <state> ...". The header row ("UNIT FILE")
    does not match the pattern, so it is naturally skipped.
    """
    units: list[str] = []
    for line in unit_files_text.splitlines():
        name = line.split()[0] if line.split() else ""
        if _ROBIE_HERMES_UNIT_RE.match(name) and name not in units:
            units.append(name)
    return units


JOB_DB_DEFAULT = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"


def _resolve_job_db_path(env_value: str) -> str:
    """Pure: $ROBIE_JOB_DB value, else the engine default DB path."""
    value = (env_value or "").strip()
    return value if value else JOB_DB_DEFAULT


def _dump_job_tables(conn: "sqlite3.Connection") -> str:
    """Read-only dump of the job-engine SQLite store (tables + schedules).

    Expects `conn` opened read-only (mode=ro URI); also sets
    PRAGMA query_only=ON. Lists tables, then dumps from `schedules`
    and `scheduled_jobs`: name/title, cron/interval, enabled flag,
    next/last run timestamps — hourly rows first. Never raises:
    returns an explanatory line on any failure.
    """
    lines: list[str] = []
    try:
        conn.execute("PRAGMA query_only=ON")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    except Exception as exc:  # noqa: BLE001 - probe must not crash
        return f"  <could not list tables: {type(exc).__name__}: {exc}>"
    lines.append("  tables: " + (", ".join(tables) if tables else "<none>"))
    for table in ("schedules", "scheduled_jobs"):
        if table not in tables:
            lines.append(f"  --- {table}: <absent>")
            continue
        try:
            cols = [r[1] for r in
                    conn.execute(f'PRAGMA table_info("{table}")')]
            want = ("name", "task_name", "action_type", "interval_minutes",
                    "cron_spec", "enabled", "next_run_at", "last_run_at")
            sel = [c for c in want if c in cols]
            if not sel:
                lines.append(f"  --- {table}: <no recognized columns>")
                continue
            qcols = ", ".join(f'"{c}"' for c in sel)
            rows = conn.execute(
                f'SELECT {qcols} FROM "{table}"').fetchall()
        except Exception as exc:  # noqa: BLE001 - probe must not crash
            lines.append(
                f"  --- {table}: <read failed: {type(exc).__name__}>")
            continue
        lines.append(f"  --- {table} ({len(rows)} row(s)) ---")

        def _hourly_first(row: tuple) -> int:
            d = dict(zip(sel, row))
            if d.get("interval_minutes") == 60:
                return 0
            cron = str(d.get("cron_spec") or "")
            # minute field "0" (e.g. "0 * * * *") fires at the top of the hour
            if cron.split()[:1] == ["0"]:
                return 0
            return 1

        for row in sorted(rows, key=_hourly_first):
            d = dict(zip(sel, row))
            label = d.get("name") or d.get("task_name") or "?"
            if d.get("cron_spec"):
                sched = str(d["cron_spec"])
            elif d.get("interval_minutes") is not None:
                sched = f"every {d['interval_minutes']}m"
            else:
                sched = "?"
            lines.append(
                f"    name={label} action={d.get('action_type')} "
                f"schedule={sched} enabled={d.get('enabled')} "
                f"next_run_at={d.get('next_run_at')} "
                f"last_run_at={d.get('last_run_at')}")
    return "\n".join(lines)


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

    def _dump_script_file(path: str, label: str) -> tuple[str, str]:
        """Read-only capture of one script file.

        Prints wc/md5/first-200-lines plus the hourly-cadence/dedupe
        keyword grep (with line numbers) and a names-only sibling listing.
        Returns (head_text, keyword_hit_text).
        """
        print(f"  --- {label}: {path} ---")
        wc = _sh_shell(f"wc -l '{path}'")
        print(f"  wc -l: {wc or '<n/a>'}")
        md5 = _sh_shell(f"md5sum '{path}'")
        print(f"  md5sum: {md5 or '<n/a>'}")
        head = _sh_shell(f"sed -n '1,200p' '{path}'")
        print("  --- first 200 lines ---")
        print("  " + (head.replace("\n", "\n  ")
                      if head else "  <empty>"))
        gate = _sh_shell(
            "grep -inE 'hour|3600|minute\\s*==\\s*0|timedelta|schedule|"
            "audit|URGENT|cleanup|mailbox|Mailbox Audit|ACTIONABLE|"
            "NOISE PURGED|send_message|dedupe|dedup|processed_ids|"
            "already|note' "
            f"'{path}' | head -100"
        )
        print("  --- keyword hits (hourly cadence / dedupe logic) ---")
        print("  " + (gate.replace("\n", "\n  ")
                      if gate else "  <none>"))
        script_dir = str(Path(path).parent)
        sibs = _sh_shell(f"ls -1 '{script_dir}' 2>/dev/null")
        print(f"  --- sibling files in {script_dir} (names only) ---")
        print("  " + (sibs.replace("\n", "\n  ")
                      if sibs else "  <not listable>"))
        return head, gate

    if target:
        resolved = _sh_shell(f"readlink -f '{target}'").splitlines()
        real = resolved[0] if resolved else ""
        print(f"  --- resolved script target: {target} -> "
              f"{real or '<unresolvable>'}")
        if real and real.endswith((".py", ".sh", ".pl", ".rb")):
            head, gate = _dump_script_file(real, "ExecStart script")
            if gate:
                findings.append(
                    "The email-watcher's ExecStart script "
                    f"({real}) contains hourly/dedupe keyword hits "
                    "(shown in section 8). That is where the hourly "
                    "mailbox-audit/URGENT cadence and the duplicate-note "
                    "logic live — there is no separate hourly timer on "
                    "this box."
                )
            # The deployed watcher is a tiny ROBIE_ZIP_LOAD_PATH launcher
            # (<50 lines) that exec()s the real agent from
            # <root>/releases/current/<load_path>. The hourly "Mailbox
            # Audit & Cleanup" trigger and the duplicate-note bug live in
            # the deployed code, so follow the indirection and capture the
            # REAL file too.
            loader_primary = _resolve_loader_target(head, real)
            if loader_primary:
                loader_alt = _resolve_loader_target(head, real,
                                                    parents_up=1)
                print("  --- ROBIE_ZIP_LOAD_PATH launcher detected ---")
                print(f"  primary candidate (parents[2]): {loader_primary}")
                print(f"  fallback candidate (parents[1]): {loader_alt}")
                real2 = ""
                for cand in (loader_primary, loader_alt):
                    ok = _sh_shell(
                        f"test -f '{cand}' && echo YES || echo NO").strip()
                    print(f"  exists: {cand} -> {ok}")
                    if ok == "YES":
                        real2 = cand
                        break
                if real2:
                    _, gate2 = _dump_script_file(
                        real2, "REAL agent script (via ROBIE_ZIP_LOAD_PATH)")
                    if gate2:
                        findings.append(
                            "The watcher's real agent code "
                            f"({real2}, loaded via ROBIE_ZIP_LOAD_PATH) "
                            "contains hourly/dedupe keyword hits (shown in "
                            "section 8). The hourly mailbox-audit/URGENT "
                            "cadence and the duplicate-note logic live there."
                        )
                else:
                    findings.append(
                        "The watcher script is a ROBIE_ZIP_LOAD_PATH "
                        "launcher but neither candidate real path exists "
                        f"({loader_primary} / {loader_alt}). The deployed "
                        "releases layout differs from the launcher's "
                        "assumption — inspect releases/current manually."
                    )
        else:
            print("  <target is not a script file — contents not dumped>")
    # Filenames-only hunt for the report generator under releases/current,
    # in case the audit email is built by a different deployed file.
    rel_current = "/opt/streetsmart-hermes/.hermes/releases/current"
    print(f"  --- files under {rel_current} containing 'Mailbox Audit' "
          "(names only) ---")
    audit_hits = _sh_shell(
        f"grep -rl 'Mailbox Audit' '{rel_current}' 2>/dev/null | head -30"
    )
    print("  " + (audit_hits.replace("\n", "\n  ")
                  if audit_hits else "  <none>"))
    if audit_hits:
        findings.append(
            "Files under releases/current containing the string "
            "'Mailbox Audit' (names only, section 8):\n    " +
            "\n    ".join(audit_hits.splitlines()[:10])
        )
    _absent = ("not found", "no files found", "could not be found",
               "no such file", "<could not run")
    if unit_body and not any(m in unit_body.lower() for m in _absent):
        findings.append(
            f"{WATCHER_UNIT} captured in full (section 8). "
            f"ExecStart script resolves to: {real or target or '<unresolvable>'}."
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

    section("10. Hermes framework cron dirs, hooks, and agent processes "
            "(read-only)")
    # The hourly "Mailbox Audit & Cleanup" report is not a systemd timer,
    # not a cron, and not in the watcher's script — the Hermes agent
    # framework's own scheduler (.hermes/cron/) is the remaining suspect.
    cron_json_total = 0
    for d in HERMES_CRON_DIRS:
        print(f"  --- recursive names-only listing of {d} ---")
        listing = _sh_shell(
            f"find '{d}' -type f "
            r"-printf '%p\t%s bytes\t%TY-%Tm-%Td %TH:%TM\n' "
            "2>/dev/null | sort"
        )
        print("  " + (listing.replace("\n", "\n  ")
                      if listing else "  <absent or empty>"))
        json_files = _json_files_in_listing(listing.splitlines())
        cron_json_total += len(json_files)
        for jf in json_files[:20]:
            print(f"  --- cron job definition: {jf} (redacted) ---")
            body = _sh("cat", jf)
            print("  " + (body.replace("\n", "\n  ")
                          if body else "  <empty>"))
            if _matches_keywords(body):
                findings.append(
                    f"Hermes cron job definition {jf} matches the inbox "
                    "keywords (section 10). Its schedule/prompt above is "
                    "the hourly mailbox-audit trigger candidate."
                )
    if cron_json_total:
        findings.append(
            f"{cron_json_total} Hermes cron JSON definition(s) printed in "
            "section 10 (contents redacted). Check their schedule fields "
            "for the hourly mailbox-audit cadence."
        )
    print(f"  --- names-only listing of {HERMES_HOOKS_DIR} ---")
    hooks = _sh("ls", "-la", HERMES_HOOKS_DIR)
    print("  " + (hooks.replace("\n", "\n  ")
                  if hooks else "  <absent>"))
    if _matches_keywords(hooks):
        findings.append(
            "A Hermes hook name matches the inbox keywords (section 10). "
            "The listing above shows it."
        )
    print("  --- long-running hermes/cron/scheduler processes "
          "(names/args only) ---")
    procs = _sh_shell(
        "ps aux 2>/dev/null | grep -i -E 'hermes|cron|schedul' | "
        "grep -v grep | head -30"
    )
    print("  " + (procs.replace("\n", "\n  ")
                  if procs else "  <no matching processes>"))

    section("11. FULL definitions of every robie-* / hermes-* unit "
            "(read-only)")
    # The keyword filter missed hourly timers that fire at :00
    # (robie-production-preflight.timer, robie-ascend-sync.timer,
    # robie-health-check.timer). Dump every robie-* / hermes-* service
    # and timer in full so the XX:00:30 audit firing time can be matched
    # against a schedule and each ExecStart= inspected for the report
    # generator.
    rh_units = _robie_hermes_units(unit_files)
    if not rh_units:
        print("  <no robie-* or hermes-* units found>")
    for unit in rh_units:
        print(f"  --- systemctl cat {unit} ---")
        body = _sh("systemctl", "cat", unit)
        print("  " + (body.replace("\n", "\n  ") if body else "  <empty>"))
        if unit.endswith(".timer"):
            sched = _sh("systemctl", "show", unit, "-p", "OnCalendar",
                        "-p", "OnUnitActiveSec", "-p",
                        "NextElapseUSecRealtime", "--value")
            print("  schedule: " + (sched.replace("\n", "; ")
                                    if sched else "<n/a>"))
    if rh_units:
        findings.append(
            f"{len(rh_units)} robie-*/hermes-* unit(s) dumped in full "
            "(section 11): " + ", ".join(rh_units[:12]) +
            (", ..." if len(rh_units) > 12 else "") +
            ". Match each timer's OnCalendar= against the XX:00:30 audit "
            "firing time, and each service's ExecStart= for the report "
            "generator."
        )

    section("12. ROBIE Job Engine scheduled-jobs DB (read-only)")
    # The engine scheduler ticks every 60s via robie-scheduler.timer and
    # reads `schedules` + `scheduled_jobs` from $ROBIE_JOB_DB (default
    # /opt/streetsmart-hermes/robie-job-engine/data/jobs.db). An hourly
    # row here would fire the audit with no systemd/cron/Hermes-cron trace.
    job_db_env = _sh("printenv", "ROBIE_JOB_DB")
    job_db_path = _resolve_job_db_path(job_db_env)
    print(f"  ROBIE_JOB_DB env: {job_db_env or '<unset>'}")
    print(f"  resolved path: {job_db_path}")
    ls_out = _sh("ls", "-la", "--", job_db_path)
    print("  file: " + (ls_out or "<absent or unreadable>"))
    if "No such file" in ls_out or not ls_out or ls_out.startswith("<"):
        print("  <DB absent — nothing to dump>")
    else:
        dump = ""
        try:
            uri = ("file:" + urllib.parse.quote(job_db_path) + "?mode=ro")
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            try:
                dump = _dump_job_tables(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - probe must not crash
            dump = (f"  <could not open DB read-only: "
                    f"{type(exc).__name__}: {exc}>")
        print(_redact(dump))
        if "name=" in dump:
            findings.append(
                "ROBIE Job Engine scheduled-jobs DB dumped in section 12. "
                "Match any hourly row (interval 60m or cron minute '0') "
                "against the XX:00:30 audit firing time.")

    section("13. FULL deployed email-agent script capture (read-only)")
    # The hourly "Mailbox Audit & Cleanup" report and the hourly
    # "[URGENT / CSR ACTION]" re-alerts are generated by the deployed
    # agent script, which is NOT in the repo. Capture it here in full
    # (redacted, line-numbered) so the cadence/dedupe fix can be written
    # against the real code. Read-only: prints the file, changes nothing.
    _agent_cands = [
        "/opt/streetsmart-hermes/releases/current/scripts/"
        "robie_email_agent.py",
        "/opt/streetsmart-hermes/.hermes/releases/current/scripts/"
        "robie_email_agent.py",
    ]
    _agent_path = ""
    for _cand in _agent_cands:
        _r = _sh_shell(f"readlink -f '{_cand}'").splitlines()
        _r = _r[0] if _r else ""
        _ok = _sh_shell(f"test -f '{_r}' && echo YES || echo NO").strip()
        print(f"  candidate: {_cand} -> {_r or '<unresolvable>'} "
              f"[{_ok}]")
        if _ok == "YES":
            _agent_path = _r
            break
    if _agent_path:
        _full = _sh_shell(f"sed -n '1,2000p' '{_agent_path}'")
        if _full:
            _numbered = "\n".join(
                f"{_i + 1:4d}: {_line}"
                for _i, _line in enumerate(_full.splitlines())
            )
            print("  --- FULL FILE (redacted, line-numbered) ---")
            print("  " + _numbered.replace("\n", "\n  "))
            findings.append(
                "Full deployed email-agent script "
                f"({_agent_path}) captured in section 13. The hourly "
                "mailbox-audit/URGENT cadence and re-alert logic live in "
                "that listing — write the cadence/dedupe fix against it."
            )
        else:
            print("  <file exists but is empty or unreadable>")
    else:
        print("  <no candidate agent script found>")


    section("14. Remaining hourly-trigger suspects (read-only)")
    # Sections 1-13 ruled out: systemd timers, the three checked crontabs,
    # Hermes cron jobs.json, the engine's schedules/scheduled_jobs tables,
    # and the deployed email-agent script (no hourly/report strings in any
    # of them). The report STILL fires hourly at ~:00:30 ET, so the trigger
    # is on a surface the sweep never looked at:
    #   (a) carlo_streetsmart_insurance's crontab — process starts PROVE this
    #       user has cron jobs (run_robie_call_label_watch.sh every 5 min)
    #       but section 4 only checked streetsmart-hermes, ubuntu, root.
    #   (b) /opt/renewal-automation-system — a whole second Robie system the
    #       sweep never listed (backup, browser bridge, health watchdog,
    #       call-label watch).
    #   (c) the engine DB's report_run_configs / report_runs tables — the
    #       audit email is literally titled a "Report"; section 12 only
    #       dumped schedules/scheduled_jobs.
    #   (d) the DEPLOYED /opt/streetsmart-hermes/scripts/robie_health_check.py
    #       (robie-health-check.timer fires hourly at :00) — section 11
    #       dumped the unit but never the script's content.
    # Everything below is read-only: list, cat, grep, sqlite3 mode=ro.

    print("  --- 14a. carlo_streetsmart_insurance crontab ---")
    out = _sh("crontab", "-l", "-u", "carlo_streetsmart_insurance")
    print("  " + (out.replace("\n", "\n  ") if out else "<empty>"))
    if _matches_keywords(out) or "0 *" in out or "0  * " in out:
        findings.append(
            "carlo_streetsmart_insurance's crontab (section 14a) has an "
            "inbox-keyword or hourly ('0 *') entry — prime trigger suspect.")
    print("  --- /var/spool/cron/crontabs listing (names only) ---")
    print("  " + _sh("ls", "-la", "/var/spool/cron/crontabs").replace(
        "\n", "\n  "))

    print("  --- 14b. /opt/renewal-automation-system (names only, top 120) ---")
    listing = _sh_shell(
        "find /opt/renewal-automation-system -maxdepth 3 "
        "| head -120")
    print("  " + (listing.replace("\n", "\n  ") if listing else "<absent>"))
    print("  --- /srv/robie (names only, top 40) ---")
    srv = _sh_shell("find /srv/robie -maxdepth 2 2>/dev/null | head -40")
    print("  " + (srv.replace("\n", "\n  ") if srv else "<absent>"))

    print("  --- 14c. content grep for the report/urgent subject phrases ---")
    # Exact phrases from the emails themselves. Filenames + line numbers
    # only; bodies go through _redact.
    _grep_roots = ("/opt/renewal-automation-system",
                   "/opt/streetsmart-hermes/scripts",
                   "/srv/robie")
    _grep_pat = ("Mailbox Audit & Cleanup Report|URGENT / CSR ACTION|"
                 "Carrier Response Received")
    for _root in _grep_roots:
        hits = _sh_shell(
            f"grep -rl --include='*.py' --include='*.sh' --include='*.json' "
            f"-e '{_grep_pat}' '{_root}' 2>/dev/null | head -20")
        print(f"  [{_root}] files containing the phrases:")
        print("  " + (hits.replace("\n", "\n  ") if hits else "<none>"))
        if hits and "<none>" not in hits:
            findings.append(
                f"A file under {_root} (section 14c) contains the hourly "
                "report/urgent subject phrases — this is the generator. "
                "Write the twice-daily + dedupe fix against it.")

    print("  --- 14d. engine DB report_run_configs / report_runs ---")
    try:
        uri = ("file:" + urllib.parse.quote(job_db_path) + "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            conn.execute("PRAGMA query_only=ON")
            for _table in ("report_run_configs", "report_runs"):
                try:
                    _cols = [r[1] for r in
                             conn.execute(f'PRAGMA table_info("{_table}")')]
                except Exception:
                    print(f"  --- {_table}: <unreadable>")
                    continue
                if not _cols:
                    print(f"  --- {_table}: <absent>")
                    continue
                _rows = conn.execute(
                    f'SELECT * FROM "{_table}" LIMIT 25').fetchall()
                print(f"  --- {_table} ({len(_rows)} row(s), "
                      f"cols={_cols}) ---")
                for _r in _rows:
                    print("  " + _redact(" | ".join(
                        str(_c)[:120] for _c in _r)))
                if _rows:
                    findings.append(
                        f"Engine DB table {_table} has rows (section 14d) — "
                        "check cadence columns for the hourly audit trigger.")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - probe must not crash
        print(f"  <could not open DB read-only: "
              f"{type(exc).__name__}: {exc}>")

    print("  --- 14e. deployed robie_health_check.py identity ---")
    _hc = "/opt/streetsmart-hermes/scripts/robie_health_check.py"
    print("  md5: " + _sh("md5sum", "--", _hc))
    print("  wc: " + _sh("wc", "-l", "--", _hc))
    _hc_head = _sh_shell(f"sed -n '1,40p' '{_hc}'")
    print("  --- first 40 lines (redacted) ---")
    print("  " + _redact(_hc_head).replace("\n", "\n  "))
    if _matches_keywords(_hc_head):
        findings.append(
            "Deployed robie_health_check.py (section 14e, fired hourly at "
            ":00 by robie-health-check.timer) contains inbox keywords — "
            "the hourly report generator.")

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
