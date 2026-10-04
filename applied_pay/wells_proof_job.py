"""Scheduled Wells captured-page proof job. Read-only. No network, no posts, no money.

WELLS_ACCESS_MODE (environment variable):
  captured (default) - ingest operator-supplied captured Wells guest pages for
      Trust 3021 + Operating 3018, extract posted transactions with stable IDs
      (sha256 of account|date|amount|direction|descriptor), keep a durable
      append-only store, and match rows against Applied Pay payouts by literal
      transfer-reference binding only. Amount/date alone never matches.
  live - reserved. Refuses to run until Carlo's read-only Wells access is
      provisioned. Fail closed: nothing runs, nothing is claimed.

Until live access exists, every bank-side claim is labeled UNVERIFIED.

This module never authenticates a capture, never converts "posted" to
"cleared", and never writes to QBO, EZLynx, or the bank. Counters for all
write surfaces stay at zero by construction.
"""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone

from applied_pay.wells_guest_reader import read_capture, CaptureError

UNVERIFIED = "UNVERIFIED"
SUPPORTED_ACCOUNTS = ("3021", "3018")


class ProofError(ValueError):
    pass


def access_mode() -> str:
    mode = os.environ.get("WELLS_ACCESS_MODE", "captured").strip().lower()
    if mode == "live":
        # Carlo's read-only Wells access has not landed. Refuse rather than
        # claim live bank reads we cannot make.
        raise ProofError("WELLS_ACCESS_MODE=live is not provisioned; refusing to run")
    if mode != "captured":
        raise ProofError("WELLS_ACCESS_MODE must be 'captured' or 'live'")
    return mode


def stable_id(row: dict) -> str:
    """Stable transaction identity: sha256(account|date|amount|direction|descriptor)."""
    key = "|".join(str(row.get(k, "")) for k in
                   ("account_last4", "bank_date", "amount", "direction", "descriptor"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {"mode": "captured", "captures": [], "seen": {}}
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict) or not isinstance(state.get("seen"), dict):
        raise ProofError("proof state file is corrupt; refusing to continue")
    return state


def _save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def run(captures, payouts, *, state_path, now, environment="TEST", quick=False):
    """Ingest captured Wells pages and match rows to Applied Pay payouts.

    captures: list of {"capture": <reader capture dict>, "artifact_bytes": bytes}.
    payouts: list of {"ref": str, ...} Applied Pay payout dicts.
    Returns a review-only report; every bank-side claim labeled UNVERIFIED.
    """
    mode = access_mode()
    if environment != "TEST":
        raise ProofError("Test only")
    if now.tzinfo is None:
        raise ProofError("timezone-aware now required")
    state = _load_state(state_path)
    seen = state.setdefault("seen", {})
    ingested = set(state.setdefault("captures", []))

    report_rows = []
    findings = []
    for entry in captures:
        capture = entry["capture"]
        artifact = entry["artifact_bytes"]
        sha = capture.get("artifact_sha256", "")
        if quick and sha in ingested:
            findings.append({"capture": sha[:12], "status": "skipped",
                             "reason": "already ingested; --quick skips re-ingest"})
            continue
        try:
            result = read_capture(capture, artifact_bytes=artifact, now=now,
                                  environment=environment)
        except CaptureError as exc:
            findings.append({"capture": sha[:12], "status": "stop",
                             "reason": "capture rejected: %s" % exc})
            continue
        account = result["account_last4"]
        for row in result["rows"]:
            sid = stable_id(row)
            first = seen.get(sid, {}).get("first_seen")
            record = seen.setdefault(sid, {"first_seen": now.isoformat(),
                                           "account_last4": account,
                                           "capture_refs": []})
            if first is None:
                record["first_seen"] = now.isoformat()
            record["last_seen"] = now.isoformat()
            if sha not in record["capture_refs"]:
                record["capture_refs"].append(sha)
            report_rows.append({**row, "stable_id": sid,
                                "verification_status": UNVERIFIED})
        for issue in result["issues"]:
            findings.append({"capture": sha[:12], "status": "needs_review",
                             "reason": "reader issue: %s" % json.dumps(issue, sort_keys=True)})
        if not result["complete"]:
            findings.append({"capture": sha[:12], "status": "needs_review",
                             "reason": "capture incomplete; no completeness claim"})
        ingested.add(sha)
        findings.append({"capture": sha[:12], "status": "ingested",
                         "reason": "%d rows from account %s" % (len(result["rows"]), account)})

    state["captures"] = sorted(ingested)
    state["mode"] = mode
    if state_path:
        _save_state(state_path, state)

    # Match payouts to bank rows by literal transfer-reference binding only.
    payout_refs = [p.get("ref") for p in payouts]
    if any(not isinstance(r, str) or not r for r in payout_refs) or \
            len(set(payout_refs)) != len(payout_refs):
        raise ProofError("duplicate or missing payout reference")
    matches, unmatched, used = [], [], set()
    for payout in payouts:
        ref = payout["ref"]
        bound = [r for r in report_rows
                 if ref in (r.get("bank_reference_candidates") or [])]
        if len(bound) == 1:
            row = bound[0]
            used.add(row["stable_id"])
            matches.append({"payout_ref": ref, "stable_id": row["stable_id"],
                            "account_last4": row["account_last4"],
                            "bank_date": row["bank_date"], "amount": row["amount"],
                            "direction": row["direction"],
                            "bank_status": row["bank_status"],
                            "descriptor": row["descriptor"],
                            "source_location": row["source_location"],
                            "binding": "literal transfer reference",
                            "verification_status": UNVERIFIED,
                            "clears_funds": False,
                            "note": "candidate only; posted is not clearing proof"})
        elif len(bound) > 1:
            unmatched.append({"payout_ref": ref, "status": "needs_review",
                              "reason": "multiple bank rows share this transfer reference"})
        else:
            unmatched.append({"payout_ref": ref, "status": "needs_review",
                              "reason": "no literal transfer-reference match; amount/date alone not proof"})
    unbound = [{"stable_id": r["stable_id"], "account_last4": r["account_last4"],
                "bank_date": r["bank_date"], "amount": r["amount"],
                "direction": r["direction"], "bank_status": r["bank_status"]}
               for r in report_rows if r["stable_id"] not in used]
    return {"mode": "scheduled_proof_%s" % mode,
            "verification_label": UNVERIFIED,
            "environment": environment,
            "as_of": now.isoformat(),
            "accounts": {a: {"rows": sum(1 for r in report_rows if r["account_last4"] == a)}
                         for a in SUPPORTED_ACCOUNTS},
            "rows": report_rows,
            "matches": matches,
            "unmatched_payouts": unmatched,
            "unbound_bank_rows": unbound,
            "findings": findings,
            "state_path": state_path,
            "bank_actions": 0, "qbo_posts": 0, "ezlynx_writes": 0,
            "notes_written": 0, "transfers": 0}


def main():
    """CLI: --capture-dir holds <name>.capture.json + <name>.artifact.json pairs."""
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", required=True, type=Path)
    parser.add_argument("--payouts", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--quick", action="store_true",
                        help="skip captures already ingested (for the hourly desk sweep)")
    args = parser.parse_args()
    captures = []
    for cap_file in sorted(args.capture_dir.glob("*.capture.json")):
        art_file = cap_file.with_name(cap_file.name.replace(".capture.json", ".artifact.json"))
        if not art_file.exists():
            raise ProofError("missing artifact for %s" % cap_file.name)
        captures.append({"capture": json.loads(cap_file.read_text(encoding="utf-8")),
                         "artifact_bytes": art_file.read_bytes()})
    if not captures:
        raise ProofError("no captures in %s" % args.capture_dir)
    result = run(captures, json.loads(args.payouts.read_text(encoding="utf-8")),
                 state_path=str(args.state), now=datetime.now(timezone.utc),
                 quick=args.quick)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({"mode": result["mode"],
                      "verification_label": result["verification_label"],
                      "matches": len(result["matches"]),
                      "unmatched_payouts": len(result["unmatched_payouts"]),
                      "unbound_bank_rows": len(result["unbound_bank_rows"]),
                      "findings": len(result["findings"]),
                      "out": str(args.out)}, indent=2))


if __name__ == "__main__":
    main()
