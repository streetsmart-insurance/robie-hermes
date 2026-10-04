"""Synthetic fixtures only. No EZLynx calls, no browser, no writes."""
import unittest

from applied_pay.ezlynx_receipt_lookup import lookup, ReceiptLookupError


def payout(ref="SYNREF001", net="250.00", invoice="INV-1001"):
    return {"ref": ref, "net": net, "payout_date": "2026-10-01",
            "invoice_number": invoice}


def receipt(number="R-1", amount="250.00", status="applied", invoice="INV-1001",
            memo=""):
    return {"receipt_number": number, "amount": amount,
            "applied_status": status, "invoice_number": invoice, "memo": memo,
            "source": "ezlynx_api"}


class ReceiptLookupTest(unittest.TestCase):
    def test_invoice_binding(self):
        r = lookup([payout()], [receipt()])
        self.assertEqual("receipt_bound", r["results"][0]["status"])
        self.assertEqual("R-1", r["results"][0]["receipt_number"])
        self.assertIn("invoice_number", r["results"][0]["binding"])

    def test_payout_reference_binding(self):
        r = lookup([payout(invoice="")], [receipt(invoice="", memo="PAYOUT SYNREF001 SETTLED")])
        self.assertEqual("receipt_bound", r["results"][0]["status"])
        self.assertIn("payout_reference", r["results"][0]["binding"])

    def test_amount_only_never_binds(self):
        r = lookup([payout()], [receipt(invoice="INV-9999", memo="unrelated")])
        self.assertEqual("needs_review", r["results"][0]["status"])
        self.assertIsNone(r["results"][0]["receipt_number"])

    def test_substring_reference_not_binding(self):
        r = lookup([payout(invoice="")],
                   [receipt(invoice="", memo="PAYOUT SYNREF001X EXTRA")])
        self.assertEqual("needs_review", r["results"][0]["status"])

    def test_unapplied_receipt_held(self):
        r = lookup([payout()], [receipt(status="unapplied")])
        self.assertEqual("needs_review", r["results"][0]["status"])
        self.assertTrue(any("not applied/posted" in x
                            for x in r["results"][0]["review_reasons"]))

    def test_void_receipt_held(self):
        r = lookup([payout()], [receipt(status="void")])
        self.assertEqual("needs_review", r["results"][0]["status"])

    def test_multiple_receipts_flagged(self):
        r = lookup([payout()], [receipt("R-1"), receipt("R-2")])
        self.assertEqual("needs_review", r["results"][0]["status"])
        self.assertTrue(any("multiple" in x
                            for x in r["results"][0]["review_reasons"]))

    def test_unmatched_receipts_listed(self):
        r = lookup([payout()], [receipt(), receipt("R-9", amount="999.00")])
        self.assertEqual(1, len(r["unmatched_receipts"]))
        self.assertEqual("R-9", r["unmatched_receipts"][0]["receipt_number"])

    def test_duplicate_payout_ref_fails_closed(self):
        with self.assertRaises(ReceiptLookupError):
            lookup([payout(), payout()], [receipt()])

    def test_duplicate_receipt_number_fails_closed(self):
        with self.assertRaises(ReceiptLookupError):
            lookup([payout()], [receipt(), receipt()])

    def test_no_write_surfaces(self):
        r = lookup([payout()], [receipt()])
        for key in ("ezlynx_reads", "ezlynx_writes", "bank_actions",
                    "qbo_posts", "notes_written"):
            self.assertEqual(0, r[key])

    def test_non_test_environment_refuses(self):
        with self.assertRaises(ReceiptLookupError):
            lookup([payout()], [receipt()], environment="PROD")


if __name__ == "__main__":
    unittest.main()
