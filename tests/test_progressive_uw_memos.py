"""Fixture tests for Progressive FAO underwriting memo capture. No live login."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.progressive_pending_cancellation import (
    CancellationRow,
    DocumentCapture,
    FaoCancellationLedger,
    FaoDocument,
    is_billing_document,
    is_renewal_document,
    is_underwriting_memo,
    run_pull,
)
from robie_job_engine.intake_core import IntakeHold, SourceArchive

LIST_URL = (
    "https://www.foragentsonly.com/managepolicies/reports/"
    "policiesneedservice/policiespendingcancellation/"
)

UW_ROW = CancellationRow(
    "970498127", "John Q Trucking LLC", date(2026, 10, 15),
    "$1,240.00", "UNDERWRITING", LIST_URL,
)
NP_ROW = CancellationRow(
    "876263535", "Acme Freight Inc", date(2026, 10, 20),
    "$845.10", "NON-PAYMENT", LIST_URL,
)

UW_DOC_ROWS = (
    FaoDocument("970498127", "Cancellation Notice - Non Payment", date(2026, 9, 28), "EMAIL", 0),
    FaoDocument("970498127", "Underwriting Memo", date(2026, 9, 29), "EMAIL", 1),
    FaoDocument("970498127", "Billing Statement", date(2026, 9, 30), "EMAIL", 2),
    FaoDocument("970498127", "Renewal Offer", date(2026, 9, 30), "EMAIL", 3),
)


class MemoClassificationTest(unittest.TestCase):
    def test_memo_terms_match(self):
        self.assertTrue(is_underwriting_memo("Underwriting Memo"))
        self.assertTrue(is_underwriting_memo("Additional Information Requested"))
        self.assertTrue(is_underwriting_memo("Agent Review Notice"))
        self.assertTrue(is_underwriting_memo("Documentation Required - Proof of Prior Insurance"))
        self.assertTrue(is_underwriting_memo("UW MEMO 10/01/2026"))

    def test_cancellation_docs_are_not_memos(self):
        # Cancellation documents belong to the cancellation flow, never memos.
        self.assertFalse(is_underwriting_memo("Cancellation Notice - Non Payment"))
        self.assertFalse(is_underwriting_memo("Notice of Cancellation"))
        self.assertFalse(is_underwriting_memo("Underwriting Cancellation Memo"))

    def test_billing_excluded(self):
        self.assertTrue(is_billing_document("Billing Statement"))
        self.assertTrue(is_billing_document("Premium Invoice"))
        self.assertFalse(is_underwriting_memo("Billing Memo"))
        self.assertFalse(is_underwriting_memo("Premium Billing Statement"))
        self.assertFalse(is_underwriting_memo("Billing Statement"))

    def test_renewal_excluded(self):
        self.assertTrue(is_renewal_document("Renewal Offer"))
        self.assertTrue(is_renewal_document("Renewal Reminder"))
        self.assertFalse(is_underwriting_memo("Renewal Offer"))
        self.assertFalse(is_underwriting_memo("Renewal Memo"))

    def test_plain_policy_docs_not_memos(self):
        self.assertFalse(is_underwriting_memo("Policy Declarations"))
        self.assertFalse(is_underwriting_memo("ID Cards"))
        self.assertFalse(is_underwriting_memo(""))


class MemoLedgerKeyTest(unittest.TestCase):
    def test_memo_document_id_format(self):
        doc = FaoDocument("970498127", "Underwriting Memo", date(2026, 9, 29), "EMAIL", 1)
        self.assertEqual(
            doc.memo_document_id,
            "progressive:970498127:memo:underwriting-memo",
        )

    def test_memo_document_id_distinct_from_cancellation_id(self):
        doc = FaoDocument("970498127", "Underwriting Memo", date(2026, 9, 29), "EMAIL", 1)
        self.assertNotEqual(doc.memo_document_id, doc.document_id)
        self.assertTrue(doc.document_id.startswith("fao:"))

    def test_memo_filename_tagged(self):
        doc = FaoDocument("970498127", "Underwriting Memo", date(2026, 9, 29), "EMAIL", 1)
        self.assertEqual(
            doc.memo_filename,
            "970498127 Underwriting_Memo UW Memo Progressive.pdf",
        )


class MemoPullFixtureTest(unittest.TestCase):
    """run_pull over a faked browser: UW row with one cancel doc + one memo."""

    class FakeBrowser:
        def __init__(self, *, docs=UW_DOC_ROWS, capture=DocumentCapture((), (b"%PDF-1.4 fake",))):
            self.docs = docs
            self.capture_result = capture
            self.captured = []
            self.report_loaded = False
            self.tabs_seen = []
            self.returned = 0

        def load_report(self):
            self.report_loaded = True

        def screenshot_report(self):
            return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

        def select_tab(self, label):
            self.tabs_seen.append(label)

        def load_current_tab(self, label):
            if label == "Pending Cancellation Due to Underwriting Reasons":
                return (UW_ROW,)
            return ()

        def open_policy_summary(self, policy_number):
            pass

        def open_documents_tab(self):
            pass

        def list_documents(self, policy_number):
            return self.docs

        def capture_document(self, doc):
            self.captured.append(doc.document_name)
            return self.capture_result

        def return_to_report(self):
            self.returned += 1

    def _pull(self, browser):
        root = Path(tempfile.mkdtemp(prefix="fao-memo-run-"))
        ledger = FaoCancellationLedger(root / "pack")
        archive = SourceArchive(root / "pack" / "sources")
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            receipt = run_pull(browser, ledger, archive, as_of=date(2026, 10, 5))
        return receipt, root

    def test_memo_pulled_alongside_cancellation(self):
        receipt, _ = self._pull(self.FakeBrowser())
        self.assertEqual(receipt["count"], 1)
        self.assertEqual(receipt["uw_memo_count"], 1)
        self.assertEqual(len(receipt["uw_memos"]), 1)
        memo = receipt["uw_memos"][0]
        self.assertEqual(memo["document_id"], "progressive:970498127:memo:underwriting-memo")
        self.assertEqual(memo["outcome"], "PULLED")
        self.assertEqual(memo["filename"], "970498127 Underwriting_Memo UW Memo Progressive.pdf")
        self.assertEqual(memo["sha256"], hashlib.sha256(b"%PDF-1.4 fake").hexdigest())
        self.assertTrue(Path(memo["path"]).exists())
        # Billing and renewal docs were never targeted.
        self.assertNotIn("Billing Statement", self.FakeBrowser().captured or [])

    def test_memo_capture_attempted_exactly_once(self):
        browser = self.FakeBrowser()
        self._pull(browser)
        self.assertIn("Underwriting Memo", browser.captured)
        self.assertIn("Cancellation Notice - Non Payment", browser.captured)
        self.assertNotIn("Billing Statement", browser.captured)
        self.assertNotIn("Renewal Offer", browser.captured)

    def test_memo_not_pulled_for_non_underwriting_row(self):
        class NonUwBrowser(self.FakeBrowser):
            def load_current_tab(self, label):
                if label == "Pending Cancellation Due to Non-Payment":
                    return (NP_ROW,)
                return ()

        browser = NonUwBrowser()
        receipt, _ = self._pull(browser)
        self.assertEqual(receipt["uw_memo_count"], 0)
        self.assertEqual(receipt["uw_memos"], [])
        self.assertNotIn("Underwriting Memo", browser.captured)

    def test_memo_second_run_skips_by_ledger(self):
        browser = self.FakeBrowser()
        _, root = self._pull(browser)
        ledger = FaoCancellationLedger(root / "pack")
        archive = SourceArchive(root / "pack" / "sources")

        class SkippingBrowser(self.FakeBrowser):
            def capture_document(self, doc):
                raise AssertionError("must not capture on a ledger hit")

        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            receipt = run_pull(SkippingBrowser(), ledger, archive, as_of=date(2026, 10, 5))
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(receipt["uw_memo_count"], 0)
        already = [m for m in receipt["uw_memos"] if m["outcome"] == "ALREADY_DELIVERED"]
        self.assertEqual(len(already), 1)
        self.assertEqual(already[0]["document_id"], "progressive:970498127:memo:underwriting-memo")

    def test_failed_memo_capture_holds(self):
        browser = self.FakeBrowser(capture=DocumentCapture((), ()))
        receipt, _ = self._pull(browser)
        holds = [h for h in receipt["held"] if "memo" in h.get("hold_reason", "").lower()]
        self.assertTrue(holds, f"expected a memo hold, got: {receipt['held']}")
        # Cancellation doc also failed to capture in this fixture; both held.
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(receipt["uw_memo_count"], 0)

    def test_unreadable_documents_tab_holds(self):
        class BrokenBrowser(self.FakeBrowser):
            def list_documents(self, policy_number):
                raise IntakeHold("Progressive FAO document table is missing or ambiguous")

        receipt, _ = self._pull(BrokenBrowser())
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(receipt["uw_memo_count"], 0)
        self.assertTrue(receipt["held"], "DOCUMENTS read failure must hold, not skip silently")

    def test_memo_ledger_conflict_holds(self):
        browser = self.FakeBrowser()
        receipt1, root = self._pull(browser)
        self.assertEqual(receipt1["uw_memo_count"], 1)
        # Corrupt the ledger entry so the second run conflicts instead of skipping.
        ledger_path = root / "pack" / "fao-cancellation-ledger.json"
        import json

        data = json.loads(ledger_path.read_text())
        memo_id = "progressive:970498127:memo:underwriting-memo"
        data["items"][memo_id]["sha256"] = "deadbeef"
        ledger_path.write_text(json.dumps(data))

        ledger = FaoCancellationLedger(root / "pack")
        archive = SourceArchive(root / "pack" / "sources")
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            receipt = run_pull(self.FakeBrowser(), ledger, archive, as_of=date(2026, 10, 5))
        holds = [h for h in receipt["held"] if "conflict" in h.get("hold_reason", "").lower()]
        self.assertTrue(holds, f"expected a ledger-conflict hold, got: {receipt['held']}")


if __name__ == "__main__":
    unittest.main()
