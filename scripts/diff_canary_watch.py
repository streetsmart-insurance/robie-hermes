#!/usr/bin/env python3
"""L3 canary-watch diff.

Compares today's verifier outputs (E01 + D01 runs) against the previous
snapshot and emits a verdict: changed true/false plus literal changes.

Read-only, stdlib only. Exit code is always 0 — a detected change is data,
not a failure. The caller (Ralph's daily review) decides what wakes Carlo.

Usage:
    diff_canary_watch.py --current-e01 E01.json --current-d01 D01.json
        [--previous snapshot.json]
        --verdict-out verdict.json --snapshot-out snapshot.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone

FAILURE_MARKERS = {"PLAYWRIGHT_BLOCKED", "TimeoutError"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def watched_state(e01: dict, d01: dict) -> dict:
    """Extract the small set of assertions L3 watches from two verifier outputs."""
    np_e01 = e01.get("named_policy", {}) or {}
    np_d01 = d01.get("named_policy", {}) or {}
    ap = d01.get("applicant_policies", {}) or {}
    job = e01.get("job", {}) or {}
    d01_matches = np_d01.get("matches", []) or []
    d01_first = d01_matches[0] if d01_matches else {}
    markers = set(job.get("last_error_markers", []) or [])
    return {
        "e01_found": bool(np_e01.get("found")),
        "e01_http": np_e01.get("http_status"),
        "d01_found": bool(np_d01.get("found")),
        "d01_policy_id": str(d01_first.get("policy_id", "")),
        "d01_status": str(d01_first.get("status", "")),
        "d01_carrier": str(d01_first.get("carrier", "")),
        "d01_effective": str(d01_first.get("effective", "")),
        "d01_expiration": str(d01_first.get("expiration", "")),
        "applicant_filter_honored": ap.get("applicant_filter_honored"),
        "policies_matching_applicant": ap.get("policies_matching_applicant"),
        "applicant_total_count": ap.get("total_count"),
        "not_checked": sorted(e01.get("not_checked", []) or []),
        "job_id": str(job.get("job_id_requested", "")),
        "job_found": bool(job.get("found")),
        "job_status": str(job.get("status", "")),
        "job_failure_markers": sorted(markers & FAILURE_MARKERS),
    }


def diff_states(prev: dict, cur: dict) -> list:
    changes = []

    def changed(code: str, detail: str):
        changes.append({"code": code, "detail": detail})

    # E01 must stay absent until Phase 5 runs.
    if cur["e01_found"] and not prev["e01_found"]:
        changed("E01_APPEARED",
                "TEST-HO-20260911-E01 is now returned by PolicyApi search; "
                "it was absent in the previous snapshot.")
    if cur["e01_http"] != 200:
        changed("E01_SEARCH_ERROR",
                f"E01 PolicyApi search HTTP {cur['e01_http']} (was {prev['e01_http']}).")

    # D01 must stay present with unchanged fields.
    if not cur["d01_found"]:
        changed("D01_MISSING",
                "TEST-HO-20260912-D01 no longer returned by PolicyApi search.")
    else:
        for field in ("d01_policy_id", "d01_status", "d01_carrier",
                      "d01_effective", "d01_expiration"):
            if cur[field] != prev[field]:
                changed("D01_FIELD_CHANGED",
                        f"{field}: {prev[field]!r} -> {cur[field]!r}.")
    if cur.get("d01_found") and prev.get("d01_found") is False:
        # covered by D01_MISSING above in reverse; keep symmetric
        pass

    # Endpoint behavior flips are changes in either direction.
    if cur["applicant_filter_honored"] != prev["applicant_filter_honored"]:
        changed("FILTER_FLIP",
                "applicant_filter_honored: "
                f"{prev['applicant_filter_honored']} -> {cur['applicant_filter_honored']}.")

    # The verifier's blind spots must not silently grow or shrink.
    if cur["not_checked"] != prev["not_checked"]:
        changed("NOT_CHECKED_CHANGED",
                f"not_checked changed: {prev['not_checked']} -> {cur['not_checked']}.")

    # A new job that failed is a change. A new clean job is noted, not a wake.
    if cur["job_id"] != prev["job_id"]:
        if cur["job_failure_markers"] or cur["job_status"] == "UNVERIFIED":
            changed("NEW_FAILED_JOB",
                    f"latest job changed {prev['job_id']} -> {cur['job_id']}; "
                    f"status={cur['job_status']}, "
                    f"failure_markers={cur['job_failure_markers']}.")
        # else: new clean job — recorded in state, not a change.

    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description="L3 canary-watch diff")
    parser.add_argument("--current-e01", required=True)
    parser.add_argument("--current-d01", required=True)
    parser.add_argument("--previous", default=None,
                        help="previous snapshot.json (omit on first run)")
    parser.add_argument("--verdict-out", required=True)
    parser.add_argument("--snapshot-out", required=True)
    args = parser.parse_args()

    e01 = load(args.current_e01)
    d01 = load(args.current_d01)
    cur_state = watched_state(e01, d01)

    snapshot = {
        "watch": "canary-watch",
        "checked_at": utc_now(),
        "e01": e01,
        "d01": d01,
        "state": cur_state,
    }
    with open(args.snapshot_out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, default=str)

    verdict = {
        "watch": "canary-watch",
        "checked_at": snapshot["checked_at"],
        "baseline": args.previous is None,
        "changed": False,
        "changes": [],
        "state": cur_state,
    }
    if args.previous is not None:
        prev_state = load(args.previous)["state"]
        verdict["changes"] = diff_states(prev_state, cur_state)
        verdict["changed"] = bool(verdict["changes"])
    else:
        verdict["changes"] = [{"code": "BASELINE_ESTABLISHED",
                               "detail": "No previous snapshot; this run "
                                         "establishes the baseline."}]

    with open(args.verdict_out, "w", encoding="utf-8") as fh:
        json.dump(verdict, fh, indent=2, default=str)
    print(json.dumps({"changed": verdict["changed"],
                      "baseline": verdict["baseline"],
                      "change_codes": [c["code"] for c in verdict["changes"]
                                       if c["code"] != "BASELINE_ESTABLISHED"]},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
