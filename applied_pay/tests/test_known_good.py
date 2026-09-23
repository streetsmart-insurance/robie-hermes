"""Known-good: must reproduce the September hand-match (QBO deposits 75184-75192, 75194, 75195; 9/04 = 75158)."""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from match import Matcher
EXPECT_READY = {   # payout ref -> receipts grouped in the posted deposit
 "38EABS6C7KD0BO5W": {"015093", "015081-R"},
 "38EBWI6C7YNDVRA6": {"015094"},
 "38E9MW6C9JT158XD": {"015098", "015099", "015100", "015101", "015102", "015103"},
 "38E9MW6C9Y3ENBLO": {"015104", "015105", "015106"},
 "38EEZC6CCQ48X6VI": {"015116"},
 "38EEZC6CD4EQCUCN": {"015119", "015120", "015121", "015122", "015089-R"},
 "38E9MG6CDWZFCWFJ": {"015126", "015127", "015136"},
 "38EC0I6CDIP1AYE2": {"015125"},       # waiting: bank-feed 07/13 suggestion must not be accepted
}
EXPECT_STOP = {  # payouts the hand-match needed a person for
 "38EF106C762KR7K9": "no source JE",   # Segura -2800 chargeback -> Carlo approved Unapplied Cash line
 "38EBWI6C8CXSI5GR": "legacy Trust",   # Accurate Leak 015096 JE 74687 still on legacy Trust (open item)
 "38EBVI6CAQO7BBDU": "group",          # 9/10 + 9/14 tie only as a pair
 "38E9LL6CCBTT9MIA": "group",
}
def test():
    snap = json.load(open(pathlib.Path(__file__).resolve().parents[1] / "fixtures" / (sys.argv[1] if len(sys.argv)>1 else "snapshot_sept.json")))
    fs = {f.payout_ref: f for f in Matcher(snap).run()}
    bad = []
    for ref, want in EXPECT_READY.items():
        f = fs[ref]
        got = {s["receipt_no"] for s in (f.proposed_deposit or {}).get("selected", [])}
        if f.bucket not in ("ready", "waiting_approval") or got != want or not f.proposed_deposit["ties"]:
            bad.append(f"{ref}: bucket={f.bucket} got={sorted(got)} want={sorted(want)}")
    for ref, why in EXPECT_STOP.items():
        f = fs[ref]
        if f.bucket != "unmatched" or not any(why in r for r in f.reasons): bad.append(f"{ref}: expected stop '{why}', got {f.bucket} {f.reasons}")
    for ref in ("38EAB86CF3UO7EVW", "38E9L66CFI521IBD"):
        if fs[ref].bucket != "past_cutoff": bad.append(f"{ref}: expected past_cutoff")
    split = [r for r in fs["38EBVI6CAQO7BBDU"].reasons if r.startswith("suggested split")]
    want_split = {"J Swat Contracting LLC 400.00", "EG Smart Home LLC 500.00", "DJ Movers LLC -190.00", "DJ Movers LLC -149.49", "ODELL LOGISTICS LLC 5.67"}
    if not split or {x.strip() for x in split[0].split(":",1)[1].split(";")[0].split(",")} != want_split: bad.append(f"9/10 split hint wrong: {split}")
    in_period = sum(float(f.proposed_deposit["amount"]) if f.proposed_deposit else 0 for f in fs.values())
    print("FAIL\n" + "\n".join(bad) if bad else "PASS: reproduces hand-match")
    return not bad
if __name__ == "__main__": sys.exit(0 if test() else 1)
