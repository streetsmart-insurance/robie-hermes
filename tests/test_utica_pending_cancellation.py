"""Tests for robie_job_engine/utica_pending_cancellation.py.

Fixture-level proof only. The live DocGenServlet download against the real
UFirst Now portal is UNVERIFIED and needs a live run.
"""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.intake_core import IntakeHold, SourceArchive
from robie_job_engine.utica_pending_cancellation import (
    UticaDeliveryLedger,
    UticaDocument,
    TransactionRow,
    docgen_pdf_url,
    is_cancellation_document,
    is_cancellation_transaction,
    parse_carrier_date,
    parse_document_grid,
    parse_transactions_grid,
    require_policy_number,
    run_pull,
    session_guid_from_url,
    transaction_id_from_page,
    PlaywrightUticaCancellationBrowser,
)

AS_OF = date(2026, 10, 5)
LIST_URL = "https://ufirstnow.uticafirst.com/oneshield/sso?osst=TOKEN123"
_FAKE_PDF = b"%PDF-1.7 fake utica notice\n"
_FAKE_PNG = b"\x89PNG\r\n\x1a\nfakepng"

TXN_HEADERS = (
    "POLICY NUMBER", "TRANSACTION TYPE", "INSURED NAME", "EFFECTIVE",
    "PROCESSED", "AGENT", "BUSINESS INTRODUCER", "PRODUCER",
    "PREMIUM BEFORE", "CHANGE", "PREMIUM AFTER", "STATUS", "DOCUMENTS",
)
TXN_ROWS = (
    ("ART3000926870", "Pending Cancellation(NOC)", "MIJ Landscaping LLC",
     "10/01/2026", "10/04/2026", "A1", "B1", "P1",
     "1000.00", "0.00", "1000.00", "Pending", "Documents"),
    ("ART3000926999", "Cancellation", "Acme Corp",
     "09/15/2026", "09/28/2026", "A1", "B1", "P1",
     "2000.00", "0.00", "2000.00", "Processed", "Documents"),
    ("ART3000927000", "Endorsement", "Beta LLC",
     "09/10/2026", "09/12/2026", "A1", "B1", "P1",
     "500.00", "50.00", "550.00", "Processed", "Documents"),
    ("ART3000927111", "Non-Renewal", "Gamma Inc",
     "08/01/2026", "09/20/2026", "A1", "B1", "P1",
     "3000.00", "0.00", "3000.00", "Pending", "Documents"),
)
DOC_HEADERS = (
    "ID", "NAME", "CONTENT TYPE", "DESCRIPTION",
    "ADDED DATE", "SOURCE", "RENDERING STATUS",
)
DOC_ROWS = (
    ("504387230999", "NonPay Notice-Insured", "Document Package",
     "Pending cancellation notice", "10/04/2026 02:08 AM",
     "UF Document Delivery", "Completed"),
    ("504387231099", "NonPay Notice-Agent", "Document Package",
     "Pending cancellation notice", "10/04/2026 02:08 AM",
     "UF Document Delivery", "Completed"),
)


class ParseTests(unittest.TestCase):
    def test_parse_transactions_grid(self):
        rows = parse_transactions_grid(TXN_HEADERS, TXN_ROWS, list_url=LIST_URL)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0].policy_number, "ART3000926870")
        self.assertEqual(rows[0].transaction_type, "Pending Cancellation(NOC)")
        self.assertEqual(rows[0].insured_name, "MIJ Landscaping LLC")
        self.assertEqual(rows[0].effective_date, date(2026, 10, 1))

    def test_parse_transactions_grid_missing_headers_hold(self):
        with self.assertRaises(IntakeHold):
            parse_transactions_grid(("POLICY NUMBER", "TYPE"), TXN_ROWS, list_url=LIST_URL)

    def test_parse_transactions_grid_empty_rows_hold(self):
        with self.assertRaises(IntakeHold):
            parse_transactions_grid(TXN_HEADERS, (), list_url=LIST_URL)

    def test_parse_transactions_grid_bad_policy_hold(self):
        bad = (("!!!", "Cancellation", "X", "10/01/2026"),)
        with self.assertRaises(IntakeHold):
            parse_transactions_grid(
                ("POLICY NUMBER", "TRANSACTION TYPE", "INSURED NAME", "EFFECTIVE"),
                bad, list_url=LIST_URL,
            )

    def test_cancellation_transaction_types(self):
        for t in ("Pending Cancellation(NOC)", "Cancellation",
                  "Rescind Pending Cancellation", "Non-Renewal",
                  "PreRenewal Notice", "pending cancellation(noc)"):
            self.assertTrue(is_cancellation_transaction(t), t)
        for t in ("Endorsement", "Reinstatement", "Change Payment Plan",
                  "New Business", ""):
            self.assertFalse(is_cancellation_transaction(t), t)

    def test_parse_document_grid(self):
        docs = parse_document_grid(DOC_HEADERS, DOC_ROWS, policy_number="ART3000926870")
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0].doc_id, "504387230999")
        self.assertEqual(docs[0].name, "NonPay Notice-Insured")
        self.assertEqual(docs[0].added_date, date(2026, 10, 4))
        # Durable ledger key uses the document ID, never the name.
        self.assertEqual(docs[0].document_id, "utica:ART3000926870:504387230999")
        self.assertNotEqual(docs[0].document_id, docs[1].document_id)

    def test_parse_document_grid_empty_hold(self):
        with self.assertRaises(IntakeHold):
            parse_document_grid(DOC_HEADERS, (), policy_number="ART3000926870")

    def test_parse_document_grid_bad_doc_id_hold(self):
        bad = (("!!!", "X", "Y", "Z", "10/04/2026", "S", "Completed"),)
        with self.assertRaises(IntakeHold):
            parse_document_grid(DOC_HEADERS, bad, policy_number="ART3000926870")

    def test_cancellation_document_terms(self):
        self.assertTrue(is_cancellation_document("NonPay Notice-Insured"))
        self.assertTrue(is_cancellation_document("Notice of Cancellation"))
        self.assertTrue(is_cancellation_document("Cancellation", "pending cancel"))
        # Billing terms are excluded even when cancellation-adjacent.
        self.assertFalse(is_cancellation_document("Premium Invoice"))
        self.assertFalse(is_cancellation_document("Cancellation Invoice"))

    def test_require_policy_number(self):
        self.assertEqual(require_policy_number("art3000926870"), "ART3000926870")
        with self.assertRaises(IntakeHold):
            require_policy_number("abc")
        with self.assertRaises(IntakeHold):
            require_policy_number("")

    def test_parse_carrier_date(self):
        self.assertEqual(parse_carrier_date("10/04/2026"), date(2026, 10, 4))
        self.assertEqual(parse_carrier_date("10/04/2026 02:08 AM"), date(2026, 10, 4))
        with self.assertRaises(IntakeHold):
            parse_carrier_date("not a date")


class UrlHelperTests(unittest.TestCase):
    def test_session_guid_prefers_param(self):
        url = "https://ufirstnow.uticafirst.com/oneshield/sso?USER_SESSION_GUID=G1&osst=O1"
        self.assertEqual(session_guid_from_url(url), "G1")

    def test_session_guid_falls_back_to_osst(self):
        url = "https://ufirstnow.uticafirst.com/oneshield/sso?osst=O1"
        self.assertEqual(session_guid_from_url(url), "O1")

    def test_session_guid_missing_holds(self):
        with self.assertRaises(IntakeHold):
            session_guid_from_url("https://ufirstnow.uticafirst.com/oneshield/sso")

    def test_docgen_pdf_url(self):
        url = docgen_pdf_url(doc_id="504387230999", session_guid="G1", transaction_id="1246149193")
        self.assertTrue(url.startswith("https://ufirstnow.uticafirst.com/oneshield/DocGenServlet?"))
        self.assertIn("docId=504387230999", url)
        self.assertIn("USER_SESSION_GUID=G1", url)
        self.assertIn("DRAGON_TRANSACTION_ID=1246149193", url)

    def test_docgen_pdf_url_missing_holds(self):
        with self.assertRaises(IntakeHold):
            docgen_pdf_url(doc_id="", session_guid="G1", transaction_id="1")

    def test_transaction_id_from_page(self):
        html = '<input type="hidden" name="x" value="1"> DRAGON_TRANSACTION_ID=1246149193 <b>done</b>'
        self.assertEqual(transaction_id_from_page(html), "1246149193")

    def test_transaction_id_from_page_ambiguous_holds(self):
        with self.assertRaises(IntakeHold):
            transaction_id_from_page("<html>no id here</html>")
        with self.assertRaises(IntakeHold):
            transaction_id_from_page("DRAGON_TRANSACTION_ID=1 DRAGON_TRANSACTION_ID=2")


class FakeUticaBrowser(PlaywrightUticaCancellationBrowser):
    """Fixture browser implementing the pull interface (no Playwright)."""

    def __init__(self, *, txn_rows=TXN_ROWS, doc_rows=DOC_ROWS,
                 pdf_bytes=_FAKE_PDF, txn_id="1246149193",
                 doc_outcomes=None):
        self._txn_rows = txn_rows
        self._doc_rows = doc_rows
        self._pdf_bytes = pdf_bytes
        self._txn_id = txn_id
        self._doc_outcomes = doc_outcomes or {}
        self._current_policy = None
        self._transaction_id = ""
        self.downloaded_urls = []

    def open_transactions(self):
        self._current_policy = None

    def select_filter_all(self):
        pass

    def screenshot_transactions(self):
        return _FAKE_PNG

    def load_transactions(self):
        return parse_transactions_grid(TXN_HEADERS, self._txn_rows, list_url=LIST_URL)

    def open_documents(self, row):
        self._current_policy = row.policy_number
        self._transaction_id = self._txn_id

    def list_documents(self, policy_number):
        return parse_document_grid(DOC_HEADERS, self._doc_rows, policy_number=policy_number)

    def download_document(self, doc):
        outcome = self._doc_outcomes.get(doc.doc_id, "ok")
        if outcome == "not_pdf":
            raise IntakeHold(f"Utica First document {doc.name!r} download is not a PDF")
        self.downloaded_urls.append(docgen_pdf_url(
            doc_id=doc.doc_id, session_guid="G1", transaction_id=self._transaction_id))
        return self._pdf_bytes

    def return_to_transactions(self):
        self._current_policy = None
        self._transaction_id = ""


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"

    def _run(self, browser):
        ledger = UticaDeliveryLedger(self.output)
        archive = SourceArchive(self.output / "sources")
        return run_pull(browser, ledger, archive, as_of=AS_OF)

    def test_full_pull_downloads_notice_pdfs(self):
        browser = FakeUticaBrowser()
        receipt = self._run(browser)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["process"], "utica")
        self.assertEqual(receipt["transaction_count"], 4)
        # 3 of 4 rows are cancellation types (Endorsement excluded).
        self.assertEqual(receipt["cancellation_transaction_count"], 3)
        # 3 rows x 2 notice docs each = 6 PDFs.
        self.assertEqual(receipt["count"], 6)
        self.assertEqual(receipt["held"], [])
        self.assertEqual(receipt["ezlynx"], "not_run")
        for item in receipt["downloaded"]:
            path = Path(item["path"])
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        # DocGenServlet URLs were built with docId + session + txn id.
        self.assertEqual(len(browser.downloaded_urls), 6)
        self.assertTrue(all("DocGenServlet" in u for u in browser.downloaded_urls))
        self.assertTrue(all("DRAGON_TRANSACTION_ID=1246149193" in u for u in browser.downloaded_urls))
        # QA screenshot saved.
        shots = list((self.output / AS_OF.isoformat()).glob("utica-transactions-*.png"))
        self.assertEqual(len(shots), 1)

    def test_second_run_skips_by_ledger(self):
        browser = FakeUticaBrowser()
        first = self._run(browser)
        self.assertEqual(first["count"], 6)
        browser2 = FakeUticaBrowser()
        second = self._run(browser2)
        self.assertEqual(second["count"], 0)
        self.assertEqual(len(second["skipped_already_delivered"]), 6)
        self.assertEqual(second["held"], [])
        # No additional downloads on the second run.
        self.assertEqual(browser2.downloaded_urls, [])

    def test_non_pdf_download_is_held(self):
        browser = FakeUticaBrowser(doc_outcomes={"504387230999": "not_pdf"})
        receipt = self._run(browser)
        # 3 held (one per row for the bad doc), 3 downloaded (the good docs).
        self.assertEqual(receipt["count"], 3)
        self.assertEqual(len(receipt["held"]), 3)
        for held in receipt["held"]:
            self.assertEqual(held["outcome"], "HELD")
            self.assertIn("not a PDF", held["hold_reason"])

    def test_no_notice_documents_hold(self):
        other_docs = (
            ("999", "Premium Invoice", "Document", "billing",
             "10/04/2026", "UF Document Delivery", "Completed"),
        )
        browser = FakeUticaBrowser(doc_rows=other_docs)
        receipt = self._run(browser)
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(len(receipt["held"]), 3)
        for held in receipt["held"]:
            self.assertIn("no notice document", held["hold_reason"])

    def test_ledger_conflict_holds(self):
        browser = FakeUticaBrowser()
        receipt = self._run(browser)
        self.assertEqual(receipt["count"], 6)
        # Corrupt one delivered file: the next run must hold, not re-record.
        first_pdf = Path(receipt["downloaded"][0]["path"])
        first_pdf.write_bytes(b"corrupted")
        browser2 = FakeUticaBrowser()
        second = self._run(browser2)
        self.assertTrue(any(h["outcome"] == "HELD" for h in second["held"]))


class DuplicateNameTests(unittest.TestCase):
    """Identical document names with different IDs are distinct documents."""

    def test_same_name_different_id_distinct_ledger_keys(self):
        rows = (
            ("111", "NonPay Notice-Insured", "Document Package", "notice",
             "10/04/2026", "UF Document Delivery", "Completed"),
            ("222", "NonPay Notice-Insured", "Document Package", "notice",
             "10/04/2026", "UF Document Delivery", "Completed"),
        )
        docs = parse_document_grid(DOC_HEADERS, rows, policy_number="ART3000926870")
        self.assertEqual(docs[0].name, docs[1].name)
        self.assertNotEqual(docs[0].document_id, docs[1].document_id)
        self.assertEqual(docs[0].document_id, "utica:ART3000926870:111")
        self.assertEqual(docs[1].document_id, "utica:ART3000926870:222")


if __name__ == "__main__":
    unittest.main()
