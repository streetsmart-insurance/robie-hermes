"""Synthetic compatibility smoke check; never a real-data hand-match."""
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from match import Matcher

def test():
    snap = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "snapshot_sept.json").read_text())
    findings = {f.payout_ref: f for f in Matcher(snap).run()}
    assert findings["SYN-PAYOUT-A"].bucket == "unmatched"  # no EZLynx note check yet
    assert findings["SYN-PAYOUT-B"].bucket == "unmatched"
    assert all(f.proposed_deposit is None for f in findings.values())
    return True

if __name__ == "__main__":
    print("PASS: synthetic safety fixture" if test() else "FAIL")
