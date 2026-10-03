#!/usr/bin/env python3
"""Daily outcome health check for the certificate edge-case fixes.

Reads the latest cert-sweep summary (written by ``cert_sweep.main`` to
``~/.cert-sweep/latest-summary.json``) and verifies the edge cases are
being handled, not silently dropped:

  1. The summary is fresh (the sweep actually ran recently).
  2. The edge-case counters are present (the new code is deployed):
     tie_broken, fuzzy_matched, multi_insured_records, filename_sourced.
  3. Near-miss signals: a held request that came within one typo of a real
     applicant is surfaced in plain English (the next EPHE-class miss
     becomes visible within a day, not months).
  4. AMBIGUOUS holds name BOTH applicant ids, so the human can act.
  5. No held entry still carries an un-split "A and B" insured blob (that
     would mean the multi-insured fan-out didn't fire and a company may
     have been silently dropped).

Usage:
    python -m robie_job_engine.cert_edge_case_health [--alert] [--verbose]

Exit 0 = healthy (silent unless --verbose). Exit 1 = broken, with
plain-English detail on stdout. With --alert, the detail is also posted
to the ROBIE health Chat (deduplicated: one alert per distinct problem,
repeat at most every 4 hours; a recovery posts once).

This is the named health check for the edge-case deploy. Schedule it to
run after the sweep's daily window (or every 30 minutes alongside the
sweep); it checks OUTCOMES, not just that the process is alive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

SUMMARY_ENV = "CERT_SWEEP_SUMMARY_PATH"
SPACE_ENV = "ROBIE_HEALTH_CHAT_SPACE"
ALERT_STATE_ENV = "CERT_SWEEP_DATA_DIR"

EDGE_COUNTERS = ("tie_broken", "fuzzy_matched", "multi_insured_records",
                 "filename_sourced")

ALERT_REPEAT_SECONDS = 4 * 3600


def _default_summary_path() -> str:
    return os.path.expanduser(os.environ.get(
        SUMMARY_ENV,
        os.path.join(os.environ.get("CERT_SWEEP_DATA_DIR", "~/.cert-sweep"),
                     "latest-summary.json")))


def _alert_state_path() -> str:
    d = os.path.expanduser(os.environ.get(ALERT_STATE_ENV, "~/.cert-sweep"))
    return os.path.join(d, "edge-case-health-alert.json")


def _parse_time(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check(summary_path: str, max_age_minutes: int) -> list[str]:
    """Return a list of plain-English problems. Empty = healthy."""
    problems: list[str] = []
    if not os.path.exists(summary_path):
        return [f"No sweep summary at {summary_path} — the sweep may not "
                "be writing it (is the new code deployed?)"]
    try:
        with open(summary_path) as fh:
            summary = json.load(fh)
    except (OSError, ValueError) as exc:
        return [f"Could not read sweep summary at {summary_path}: {exc}"]

    sweep_at = _parse_time(summary.get("sweep_at", ""))
    if sweep_at is None:
        problems.append("Sweep summary has no readable sweep_at timestamp.")
    else:
        age_min = (datetime.now(timezone.utc) - sweep_at).total_seconds() / 60
        if age_min > max_age_minutes:
            problems.append(
                f"Last sweep ran at {summary.get('sweep_at')} "
                f"({age_min:.0f} minutes ago) — older than the "
                f"{max_age_minutes}-minute freshness window.")

    stats = summary.get("stats") or {}
    missing = [c for c in EDGE_COUNTERS if c not in stats]
    if missing:
        problems.append(
            "Sweep summary is missing edge-case counters "
            f"({', '.join(missing)}) — the sweep is running old code "
            "without the edge-case fixes.")

    unverified = summary.get("unverified") or []
    for entry in unverified:
        subject = entry.get("subject") or entry.get("gmail_id", "?")
        insured = entry.get("insured") or "?"
        near = entry.get("near_miss_applicant_ids") or []
        if near:
            ids = ", ".join(map(str, near))
            problems.append(
                f"Near miss: held request '{subject}' for insured "
                f"'{insured}' came within one typo of applicant(s) {ids}. "
                "Worth a human look — this is how the EPHE miss started.")
        reason = entry.get("reason") or ""
        if "matches" in reason and "records" in reason:
            id_hits = re.findall(r"\d{5,}", reason)
            if len(id_hits) < 2:
                problems.append(
                    f"Ambiguous hold for '{subject}' does not name both "
                    f"applicant ids (reason: {reason!r}) — the human "
                    "can't act on it.")
        blob = str(entry.get("insured") or "")
        if " and " in blob.lower():
            problems.append(
                f"Held entry '{subject}' still carries an un-split insured "
                f"blob {blob!r} — the multi-insured fan-out may not have "
                "fired and a second company may have been silently dropped.")
    return problems


def _alert_dedupe_key(problems: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(problems)).encode()).hexdigest()


def maybe_alert(problems: list[str]) -> str:
    """Post to the ROBIE health Chat unless this exact alert already fired
    within the repeat window. Returns a short status string."""
    space = os.environ.get(SPACE_ENV, "").strip()
    if not space:
        return (f"--alert given but {SPACE_ENV} is not set; "
                "not posting (set it to the ROBIE health Chat space, "
                "e.g. spaces/AAAA...).")
    key = _alert_dedupe_key(problems)
    state_path = _alert_state_path()
    now = datetime.now(timezone.utc).timestamp()
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    last = state.get(key)
    if last and now - float(last) < ALERT_REPEAT_SECONDS:
        return "alert already posted for this problem within 4h; skipping."
    try:
        from robie_job_engine.chat_app_post import post_as_chat_app
        text = ("Certificate edge-case health check found problems:\n" +
                "\n".join(f"- {p}" for p in problems))
        post_as_chat_app(space, text)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the check
        return f"Chat alert failed: {type(exc).__name__}: {exc}"
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        state[key] = now
        with open(state_path, "w") as fh:
            json.dump(state, fh)
    except OSError:
        pass
    return "alert posted to ROBIE health Chat."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Outcome health check for cert edge-case handling.")
    ap.add_argument("--summary-path", default="",
                    help="path to latest sweep summary JSON")
    ap.add_argument("--max-age-minutes", type=int, default=30,
                    help="freshness window for the sweep summary")
    ap.add_argument("--alert", action="store_true",
                    help="post problems to the ROBIE health Chat")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    summary_path = args.summary_path or _default_summary_path()
    problems = check(summary_path, args.max_age_minutes)
    if not problems:
        if args.verbose:
            stats = {}
            try:
                with open(summary_path) as fh:
                    stats = json.load(fh).get("stats", {})
            except (OSError, ValueError):
                pass
            edge = {c: stats.get(c) for c in EDGE_COUNTERS}
            print(f"healthy: summary fresh, edge-case counters {edge}")
        return 0
    detail = "\n".join(f"- {p}" for p in problems)
    print("UNHEALTHY:\n" + detail)
    if args.alert:
        print(maybe_alert(problems))
    return 1


if __name__ == "__main__":
    sys.exit(main())
