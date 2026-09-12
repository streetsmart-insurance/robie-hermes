#!/usr/bin/env python3
"""Hourly EZLynx monitor decision helper. Runs on the GitHub Actions runner.

The live check and login stay on hermes-poc-01. This script only reads the
check JSON, systemd chrome show text, and the last-check state file so each
remediation can record WHY the session looked logged out — tab list, Chrome
PID, systemd start timestamps, and whether the PID/start changed since the
last check — without claiming a cause.

Cap: at most one login attempt per check. If two consecutive checks both
find LOGGED_OUT (including after a failed bootstrap + re-check), do not
login again. A loop that keeps logging in a session something else is
killing would hide the thing we are trying to see.

No secrets. No passwords. No Chrome restart. Logout cause stays UNVERIFIED
unless a later operator records proof.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


STATE_VERSION = 1
LOGGED_OUT_EXIT = 2
UNVERIFIED = "UNVERIFIED"
CAP_EVALUATED = "EVALUATED"
CAP_UNEVALUATED = "UNEVALUATED"
REMOTE_STATE_PATH = "/var/tmp/robie-ezlynx-session-monitor/last-check.json"


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    raw = path.read_text(encoding="utf-8").strip()
    if not raw or raw == "{}":
        return dict(default or {})
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(default or {"exit_code": 1, "state": "UNDETERMINED", "pages": []})
    return data if isinstance(data, dict) else dict(default or {})


def load_last_state(path: Path) -> tuple[dict[str, Any], str]:
    """Read the prior-check file. Missing/empty/unreadable is UNEVALUATED.

    A cap that fails open without telling anyone is not a cap. The caller
    may still attempt one login, but must print cap_state=UNEVALUATED.
    """
    if not path.exists():
        return {}, CAP_UNEVALUATED
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}, CAP_UNEVALUATED
    if not raw or raw == "{}":
        return {}, CAP_UNEVALUATED
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, CAP_UNEVALUATED
    if not isinstance(data, dict):
        return {}, CAP_UNEVALUATED
    if data.get("exit_code") is None and not data.get("state"):
        return {}, CAP_UNEVALUATED
    return data, CAP_EVALUATED


def parse_chrome_show(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return {
        "chrome_pid": props.get("MainPID") or "",
        "chrome_active_enter": props.get("ActiveEnterTimestamp") or "",
        "chrome_exec_start": props.get("ExecMainStartTimestamp") or "",
    }


def tabs_from_check(check: dict[str, Any]) -> list[dict[str, Any]]:
    pages = check.get("pages") or []
    tabs = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        tabs.append(
            {
                "id": page.get("id"),
                "title": page.get("title"),
                "url": page.get("url"),
            }
        )
    return tabs


def is_logged_out(payload: dict[str, Any]) -> bool:
    try:
        exit_code = int(payload.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = None
    if exit_code == LOGGED_OUT_EXIT:
        return True
    return str(payload.get("state") or "") == "LOGGED_OUT"


def decide(
    check: dict[str, Any],
    chrome: dict[str, str],
    last: dict[str, Any] | None = None,
    *,
    cap_state: str = CAP_UNEVALUATED,
) -> dict[str, Any]:
    last = last or {}
    current_logged_out = is_logged_out(check)
    evaluated = cap_state == CAP_EVALUATED
    last_logged_out = bool(evaluated and is_logged_out(last))
    pid = str(chrome.get("chrome_pid") or "")
    last_pid = str(last.get("chrome_pid") or "") if evaluated else ""
    start = str(chrome.get("chrome_exec_start") or chrome.get("chrome_active_enter") or "")
    last_start = (
        str(last.get("chrome_exec_start") or last.get("chrome_active_enter") or "")
        if evaluated
        else ""
    )
    pid_changed = bool(last_pid) and pid != last_pid
    start_changed = bool(last_start) and start != last_start
    consecutive = (1 + (1 if last_logged_out else 0)) if current_logged_out else 0
    # One login attempt per check. Two consecutive LOGGED_OUT with a
    # readable prior state stops. Missing/unreadable prior state still
    # allows one login but is UNEVALUATED — never a silent fail-open.
    attempt_login = bool(current_logged_out and consecutive < 2)
    fail_consecutive = bool(evaluated and current_logged_out and consecutive >= 2)
    why = {
        "tabs_before": tabs_from_check(check),
        "check_state": check.get("state"),
        "check_exit_code": check.get("exit_code"),
        "check_reason": check.get("reason"),
        "probe_result": check.get("probe_result"),
        "chrome_pid": pid,
        "chrome_active_enter": chrome.get("chrome_active_enter") or "",
        "chrome_exec_start": chrome.get("chrome_exec_start") or "",
        "pid_changed_since_last": pid_changed,
        "start_time_changed_since_last": start_changed,
        "last_check_pid": last_pid,
        "last_check_start": last_start,
        "last_check_state": last.get("state"),
        "last_check_exit_code": last.get("exit_code"),
        "consecutive_logged_out": consecutive,
        "attempt_login": attempt_login,
        "fail_consecutive": fail_consecutive,
        "cap_state": cap_state,
        "logout_cause": UNVERIFIED,
    }
    next_state = {
        "version": STATE_VERSION,
        "exit_code": check.get("exit_code"),
        "state": check.get("state"),
        "chrome_pid": pid,
        "chrome_active_enter": chrome.get("chrome_active_enter") or "",
        "chrome_exec_start": chrome.get("chrome_exec_start") or "",
        "login_attempted": False,
        "consecutive_logged_out": consecutive,
        "cap_state": cap_state,
        "logout_cause": UNVERIFIED,
        "tabs": tabs_from_check(check),
    }
    return {
        "why": why,
        "attempt_login": attempt_login,
        "fail_consecutive": fail_consecutive,
        "next_state": next_state,
    }


def apply_after(
    next_state: dict[str, Any],
    after_check: dict[str, Any],
    *,
    login_attempted: bool,
) -> dict[str, Any]:
    updated = dict(next_state)
    updated["exit_code"] = after_check.get("exit_code")
    updated["state"] = after_check.get("state")
    updated["login_attempted"] = bool(login_attempted)
    updated["tabs_after"] = tabs_from_check(after_check)
    if is_logged_out(after_check):
        previous = 1 if is_logged_out(next_state) else 0
        updated["consecutive_logged_out"] = previous + 1
    else:
        updated["consecutive_logged_out"] = 0
    updated["logout_cause"] = UNVERIFIED
    return updated


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    decide_p = sub.add_parser("decide", help="record why + decide whether one login is allowed")
    decide_p.add_argument("--check-json", required=True, type=Path)
    decide_p.add_argument("--chrome-show", required=True, type=Path)
    decide_p.add_argument("--last-state", required=True, type=Path)
    decide_p.add_argument("--decision-out", required=True, type=Path)
    decide_p.add_argument("--next-state", required=True, type=Path)
    decide_p.add_argument("--why-out", required=True, type=Path)

    after_p = sub.add_parser("apply-after", help="fold the post-bootstrap re-check into next-state")
    after_p.add_argument("--next-state", required=True, type=Path)
    after_p.add_argument("--after-json", required=True, type=Path)
    after_p.add_argument("--login-attempted", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "decide":
        check = load_json(args.check_json, {"exit_code": 1, "state": "UNDETERMINED", "pages": []})
        last, cap_state = load_last_state(args.last_state)
        chrome = parse_chrome_show(
            args.chrome_show.read_text(encoding="utf-8") if args.chrome_show.exists() else ""
        )
        decision = decide(check, chrome, last, cap_state=cap_state)
        _write(args.decision_out, decision)
        _write(args.next_state, decision["next_state"])
        _write(args.why_out, decision["why"])
        print(json.dumps(decision, indent=2, sort_keys=True))
        return 0

    next_state = load_json(args.next_state, {})
    after_check = load_json(args.after_json, {"exit_code": 1, "state": "UNDETERMINED", "pages": []})
    updated = apply_after(next_state, after_check, login_attempted=args.login_attempted)
    _write(args.next_state, updated)
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
