"""Live known-good: live portal payouts + live QBO snapshot -> matcher, then compare every proposed
deposit's selected JEs with the deposit accounting actually posted (same date + amount)."""
import json, sys
from match import Matcher, report
from qbo_snapshot import build
payouts = json.load(open("fixtures/payouts_email_sept.json"))
qs = build("2026-07-25", "2026-09-22", "2026-09-01", "2026-09-22")
json.dump(qs, open("fixtures/qbo_live_snapshot.json", "w"), indent=1)
snap = {"cutoff": "2026-09-18", "payouts": payouts, "ledger": qs["ledger"], "bank_deposits": qs["bank_deposits"], "aliases": {}}
fs = Matcher(snap).run()
print(report(fs)); print()
posted = {b["id"]: b for b in qs["bank_deposits"]}
ok = bad = 0
for f in fs:
    if not f.proposed_deposit: continue
    b = posted[f.bank["id"]]
    want = sorted(x for x in b["groups"])
    got = sorted(s["je_id"] for s in f.proposed_deposit["selected"])
    if want == got: ok += 1
    else: bad += 1; print("MISMATCH", f.payout_ref, "engine", got, "posted", b["id"], want)
print(f"proposed deposits identical to posted: {ok}; mismatched: {bad}")
print("skipped JEs:", len(qs["skipped_jes"]), "ledger rows:", len(qs["ledger"]), "3021 deposits:", len(qs["bank_deposits"]))
