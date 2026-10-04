"""Synthetic fixtures only. Never touches a bank, network, QBO or EZLynx."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from applied_pay.wells_proof_job import (
    run, stable_id, access_mode, ProofError, UNVERIFIED,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
REF = "SYNTRANSFER12345"


def make_capture(account="3021", rows=None, descriptor=None):
    descriptor = descriptor or "APPLIED SYSTEMS PAYOUT TRN*1*%s" % REF
    rows = rows if rows is not None else [
        ["Posted Transactions"],
        ["1", "10/03/26", descriptor, "100.00", ""],
    ]
    pages = [{"page_number": 1, "account_last4": account,
              "has_next": False, "rows": rows}]
    artifact = json.dumps({"account_last4": account, "pages": pages}).encode()
    capture = {
        "source_url": "https://www.wellsfargo.com/synthetic",
        "account_last4": account,
        "access_mode": "view_only",
        "identity_review_reference": "SYN-REVIEW",
        "captured_at": (NOW - timedelta(hours=1)).isoformat(),
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "pages": pages,
    }
    return {"capture": capture, "artifact_bytes": artifact}


def payout(ref=REF):
    return {"ref": ref, "net": "100.00", "payout_date": "2026-10-03"}


class ProofJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.json")
        self.old_mode = os.environ.get("WELLS_ACCESS_MODE")
        os.environ.pop("WELLS_ACCESS_MODE", None)

    def tearDown(self):
        if self.old_mode is None:
            os.environ.pop("WELLS_ACCESS_MODE", None)
        else:
            os.environ["WELLS_ACCESS_MODE"] = self.old_mode

    def test_literal_reference_match(self):
        r = run([make_capture()], [payout()], state_path=self.state, now=NOW)
        self.assertEqual(1, len(r["matches"]))
        m = r["matches"][0]
        self.assertEqual(REF, m["payout_ref"])
        self.assertEqual("literal transfer reference", m["binding"])
        self.assertFalse(m["clears_funds"])
        self.assertEqual(UNVERIFIED, m["verification_status"])
        self.assertEqual(0, len(r["unmatched_payouts"]))

    def test_everything_labeled_unverified(self):
        r = run([make_capture()], [payout()], state_path=self.state, now=NOW)
        self.assertEqual(UNVERIFIED, r["verification_label"])
        for row in r["rows"]:
            self.assertEqual(UNVERIFIED, row["verification_status"])

    def test_amount_only_never_matches(self):
        cap = make_capture(descriptor="SOME OTHER DESCRIPTOR NO REF")
        r = run([cap], [payout()], state_path=self.state, now=NOW)
        self.assertEqual(0, len(r["matches"]))
        self.assertEqual(1, len(r["unmatched_payouts"]))
        self.assertIn("amount/date alone not proof",
                      r["unmatched_payouts"][0]["reason"])

    def test_stable_id_deterministic(self):
        row = {"account_last4": "3021", "bank_date": "2026-10-03",
               "amount": "100.00", "direction": "credit",
               "descriptor": "APPLIED SYSTEMS PAYOUT TRN*1*" + REF}
        self.assertEqual(stable_id(row), stable_id(copy.deepcopy(row)))
        other = dict(row, amount="100.01")
        self.assertNotEqual(stable_id(row), stable_id(other))

    def test_durable_store_appends(self):
        cap = make_capture()
        run([cap], [payout()], state_path=self.state, now=NOW)
        with open(self.state, encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(1, len(state["seen"]))
        sid = next(iter(state["seen"]))
        first_seen = state["seen"][sid]["first_seen"]
        later = NOW + timedelta(hours=2)
        run([cap], [payout()], state_path=self.state, now=later)
        with open(self.state, encoding="utf-8") as fh:
            state2 = json.load(fh)
        self.assertEqual(1, len(state2["seen"]))
        self.assertEqual(first_seen, state2["seen"][sid]["first_seen"])
        self.assertEqual(later.isoformat(), state2["seen"][sid]["last_seen"])

    def test_quick_skips_reingest(self):
        cap = make_capture()
        run([cap], [payout()], state_path=self.state, now=NOW)
        r = run([cap], [payout()], state_path=self.state, now=NOW, quick=True)
        self.assertEqual(0, len(r["rows"]))
        self.assertTrue(any(f["status"] == "skipped" for f in r["findings"]))

    def test_live_mode_refuses(self):
        os.environ["WELLS_ACCESS_MODE"] = "live"
        with self.assertRaises(ProofError):
            access_mode()
        with self.assertRaises(ProofError):
            run([make_capture()], [payout()], state_path=self.state, now=NOW)

    def test_bad_mode_refuses(self):
        os.environ["WELLS_ACCESS_MODE"] = "scrape"
        with self.assertRaises(ProofError):
            run([make_capture()], [payout()], state_path=self.state, now=NOW)

    def test_both_accounts_ingested(self):
        caps = [make_capture("3021"),
                make_capture("3018", descriptor="OPERATING ITEM TRN*1*SYNTRANSFER99999")]
        r = run(caps, [payout()], state_path=self.state, now=NOW)
        self.assertEqual(1, r["accounts"]["3021"]["rows"])
        self.assertEqual(1, r["accounts"]["3018"]["rows"])
        self.assertEqual(1, len(r["matches"]))  # only the 3021 row binds REF

    def test_no_write_surfaces(self):
        r = run([make_capture()], [payout()], state_path=self.state, now=NOW)
        self.assertEqual(0, r["bank_actions"])
        self.assertEqual(0, r["qbo_posts"])
        self.assertEqual(0, r["ezlynx_writes"])
        self.assertEqual(0, r["notes_written"])
        self.assertEqual(0, r["transfers"])

    def test_rejected_capture_is_finding_not_crash(self):
        bad = make_capture()
        bad["capture"]["account_last4"] = "9999"
        r = run([bad], [payout()], state_path=self.state, now=NOW)
        self.assertEqual(0, len(r["rows"]))
        self.assertTrue(any(f["status"] == "stop" for f in r["findings"]))

    def test_duplicate_payout_ref_fails_closed(self):
        with self.assertRaises(ProofError):
            run([make_capture()], [payout(), payout()],
                state_path=self.state, now=NOW)


if __name__ == "__main__":
    unittest.main()
