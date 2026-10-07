"""Fixture tests for the Progressive FAO Pending Cancellation pull. No live login."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.progressive_pending_cancellation import (
    CARRIER_QA_DRIVE_PARENT_ID,
    DEFAULT_OUTPUT_ROOT,
    DRIVE_UPLOAD_UNAVAILABLE,
    FAO_HOST,
    PROCESS,
    REPORT_URL,
    CancellationRow,
    DocumentCapture,
    FaoCancellationLedger,
    FaoDocument,
    PlaywrightFaoCancellationBrowser,
    _REPORT_TABS,
    collect_document_capture,
    is_cancellation_document,
    main,
    parse_cancellations_report,
    parse_carrier_date,
    parse_policy_documents,
    require_fao_url,
    require_policy_number,
    run_pull,
    select_fao_page,
)
from robie_job_engine.intake_core import IntakeHold, SourceArchive, SourceItem


FAO_HOME = f"https://{FAO_HOST}/home/?Welcome=474"
LIST_URL = (
    "https://www.foragentsonly.com/managepolicies/reports/"
    "policiesneedservice/policiespendingcancellation/"
)

HEADERS = (
    "Primary Named Insured",
    "Policy Number",
    "Product",
    "State",
    "Agent Code",
    "Producer",
    "Cancel Effective Date",
    "Amount Due",
)

ROWS = (
    ("John Q Trucking LLC", "970498127", "Commercial Auto", "NJ", "33617", "Carlo", "10/15/2026", "$1,240.00"),
    ("Acme Freight Inc", "876263535", "Commercial Auto", "PA", "33617", "Carlo", "10/20/2026", "$845.10"),
)

DOC_HEADERS = ("", "Date", "Delivery", "Document name")
DOC_ROWS = (
    ("", "09/28/2026", "EMAIL", "Cancellation Notice - Non Payment"),
    ("", "09/28/2026", "EMAIL", "Billing Statement"),
    ("", "09/29/2026", "USPS", "Notice of Cancellation"),
)


class FakeCell:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class FakeRow:
    def __init__(self, cells):
        self._cells = [FakeCell(c) for c in cells]

    def locator(self, selector):
        assert selector == "td"
        return FakeNodes(self._cells)


class FakeSection:
    def __init__(self, cells):
        self._cells = [FakeCell(c) for c in cells]

    def all(self):
        return list(self._cells)


class FakeNodes:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class FakeTable:
    def __init__(self, headers, rows):
        self._headers = headers
        self._rows = [FakeRow(cells) for cells in rows]

    def locator(self, selector):
        if selector == "thead th":
            return FakeSection(self._headers)
        if selector == "tbody tr":
            return FakeNodes(self._rows)
        raise AssertionError(selector)


class FakeTableLocator:
    def __init__(self, tables):
        self._tables = tables

    def count(self):
        return len(self._tables)

    @property
    def first(self):
        return self._tables[0]


class FakeControl:
    """get_by_role() result."""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def count(self):
        return len(self._nodes)

    @property
    def first(self):
        return FakeControl(self._nodes[:1])

    def click(self):
        self._nodes[0]["clicked"] = True


class FakeFaoPage:
    """Minimal FAO page: one report table, tab + policy links as roles."""

    def __init__(self, *, url=LIST_URL, headers=HEADERS, rows=ROWS, tabs=None, policies=None):
        self.url = url
        self._headers = headers
        self._rows = rows
        self._tabs = tabs if tabs is not None else [label for label, _ in _REPORT_TABS]
        self._policies = policies if policies is not None else [r[1] for r in rows]
        self._selected_tab = None
        self._goto_calls = []

    def goto(self, url, wait_until=None):
        self._goto_calls.append(url)
        self.url = url

    def wait_for_selector(self, selector, timeout=None):
        pass

    def wait_for_url(self, pattern, timeout=None):
        pass

    def screenshot(self, full_page=None, type=None):
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    def get_by_role(self, role, name=None, exact=True):
        if role == "tab":
            nodes = [{"label": t} for t in self._tabs if t == name]
            if exact is False:
                nodes = [{"label": t} for t in self._tabs if name in t]
            return FakeControl(nodes)
        if role == "link":
            matches = [p for p in self._policies if p == name]
            return FakeControl([{"policy": p} for p in matches])
        if role == "button":
            return FakeControl([])
        raise AssertionError(role)

    def locator(self, selector):
        if selector == "table":
            return FakeTableLocator([FakeTable(self._headers, self._rows)])
        raise AssertionError(selector)

    # -- inner_text for agent context --
    def _agent_context_locator(self):
        return SimpleNamespace(inner_text=lambda: "Hello, Carlo Ferrara, Streetsmart Risk Mgr (33617)")


class ParseReportTest(unittest.TestCase):
    def test_live_columns_parse(self):
        rows = parse_cancellations_report(
            "Pending Cancellation Due to Non-Payment", HEADERS, ROWS, list_url=LIST_URL
        )
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first.policy_number, "970498127")
        self.assertEqual(first.insured_name, "John Q Trucking LLC")
        self.assertEqual(first.cancel_date, date(2026, 10, 15))
        self.assertEqual(first.amount_due, "$1,240.00")
        self.assertEqual(first.reason, "NON-PAYMENT")

    def test_tab_reason_underwriting(self):
        rows = parse_cancellations_report(
            "Pending Cancellation Due to Underwriting Reasons", HEADERS, ROWS, list_url=LIST_URL
        )
        self.assertTrue(all(r.reason == "UNDERWRITING" for r in rows))

    def test_tab_reason_renewals(self):
        rows = parse_cancellations_report("Pending Renewals", HEADERS, ROWS, list_url=LIST_URL)
        self.assertTrue(all(r.reason == "RENEWAL" for r in rows))

    def test_header_order_not_load_bearing(self):
        shuffled = (
            "Policy #", "Insured", "Cancel Date", "Product", "State",
            "Agent Code", "Producer", "Amount Due",
        )
        rows_in = (
            ("970498127", "John Q Trucking LLC", "10/15/2026", "Commercial Auto",
             "NJ", "33617", "Carlo", "$1,240.00"),
        )
        rows = parse_cancellations_report(
            "Pending Cancellation Due to Non-Payment", shuffled, rows_in, list_url=LIST_URL
        )
        self.assertEqual(rows[0].insured_name, "John Q Trucking LLC")
        self.assertEqual(rows[0].amount_due, "$1,240.00")

    def test_unknown_tab_holds(self):
        with self.assertRaises(IntakeHold):
            parse_cancellations_report("Billing", HEADERS, ROWS, list_url=LIST_URL)

    def test_missing_header_holds(self):
        # Cancel Effective Date is required; omitting it must raise IntakeHold
        headers_missing_required = tuple(h for h in HEADERS if h != "Cancel Effective Date")
        with self.assertRaises(IntakeHold):
            parse_cancellations_report(
                "Pending Cancellation Due to Non-Payment", headers_missing_required, ROWS, list_url=LIST_URL
            )
        # Amount Due is optional (e.g. Underwriting tab); omitting it does not hold
        headers_without_amount = tuple(h for h in HEADERS if h != "Amount Due")
        rows_without_amount = tuple(r[:-1] for r in ROWS)
        parsed = parse_cancellations_report(
            "Pending Cancellation Due to Non-Payment", headers_without_amount, rows_without_amount, list_url=LIST_URL
        )
        self.assertEqual(parsed[0].amount_due, "")

    def test_short_row_holds(self):
        with self.assertRaises(IntakeHold):
            parse_cancellations_report(
                "Pending Cancellation Due to Non-Payment",
                HEADERS,
                (("John Q", "970498127"),),
                list_url=LIST_URL,
            )

    def test_empty_rows_hold(self):
        with self.assertRaises(IntakeHold):
            parse_cancellations_report(
                "Pending Cancellation Due to Non-Payment", HEADERS, (), list_url=LIST_URL
            )

    def test_bad_policy_number_holds(self):
        with self.assertRaises(IntakeHold):
            parse_cancellations_report(
                "Pending Cancellation Due to Non-Payment",
                HEADERS,
                (("John Q", "ABC-123", "Commercial Auto", "NJ", "33617", "Carlo", "10/15/2026", "$0"),),
                list_url=LIST_URL,
            )

    def test_blank_insured_holds(self):
        with self.assertRaises(IntakeHold):
            parse_cancellations_report(
                "Pending Cancellation Due to Non-Payment",
                HEADERS,
                (("", "970498127", "Commercial Auto", "NJ", "33617", "Carlo", "10/15/2026", "$0"),),
                list_url=LIST_URL,
            )


class PolicyNumberTest(unittest.TestCase):
    def test_numeric_ok(self):
        self.assertEqual(require_policy_number("970498127"), "970498127")
        self.assertEqual(require_policy_number("  876263535 "), "876263535")

    def test_alpha_holds(self):
        with self.assertRaises(IntakeHold):
            require_policy_number("PRAU716089")

    def test_short_holds(self):
        with self.assertRaises(IntakeHold):
            require_policy_number("12345")


class DateTest(unittest.TestCase):
    def test_mm_dd_yyyy(self):
        self.assertEqual(parse_carrier_date("10/15/2026"), date(2026, 10, 15))

    def test_iso(self):
        self.assertEqual(parse_carrier_date("2026-10-15"), date(2026, 10, 15))

    def test_iso_with_time(self):
        # Live PDFHandler Timestamp: 2026-09-28T04:29:43-04:00
        self.assertEqual(parse_carrier_date("2026-09-28T04:29:43-04:00"), date(2026, 9, 28))

    def test_bad_holds(self):
        with self.assertRaises(IntakeHold):
            parse_carrier_date("October 15th")


class CancellationTermsTest(unittest.TestCase):
    def test_terms(self):
        self.assertTrue(is_cancellation_document("Cancellation Notice - Non Payment"))
        self.assertTrue(is_cancellation_document("Notice of Cancellation"))
        self.assertTrue(is_cancellation_document("Intent to Cancel"))
        self.assertTrue(is_cancellation_document("pending cancellation"))

    def test_out_of_scope(self):
        self.assertFalse(is_cancellation_document("Billing Statement"))
        self.assertFalse(is_cancellation_document("Renewal Offer"))
        self.assertFalse(is_cancellation_document("Policy Declarations"))

    def test_no_false_positive_on_cancelled_word_fragments(self):
        self.assertFalse(is_cancellation_document("ID Cards"))


class ParseDocumentsTest(unittest.TestCase):
    def test_live_table_parse(self):
        docs = parse_policy_documents(DOC_HEADERS, DOC_ROWS, policy_number="970498127")
        self.assertEqual(len(docs), 3)
        self.assertEqual(docs[0].document_name, "Cancellation Notice - Non Payment")
        self.assertEqual(docs[0].document_date, date(2026, 9, 28))
        self.assertEqual(docs[0].delivery, "EMAIL")
        self.assertEqual(docs[0].row_index, 0)
        self.assertEqual(docs[2].row_index, 2)

    def test_filter_targets_cancellation_only(self):
        docs = parse_policy_documents(DOC_HEADERS, DOC_ROWS, policy_number="970498127")
        targets = [d for d in docs if is_cancellation_document(d.document_name)]
        self.assertEqual([d.document_name for d in targets],
                         ["Cancellation Notice - Non Payment", "Notice of Cancellation"])

    def test_empty_name_holds(self):
        with self.assertRaises(IntakeHold):
            parse_policy_documents(DOC_HEADERS, (("", "09/28/2026", "EMAIL", ""),), policy_number="970498127")

    def test_empty_list_holds(self):
        with self.assertRaises(IntakeHold):
            parse_policy_documents(DOC_HEADERS, (), policy_number="970498127")

    def test_short_row_holds(self):
        with self.assertRaises(IntakeHold):
            parse_policy_documents(DOC_HEADERS, (("", "09/28/2026"),), policy_number="970498127")


class IdentityTest(unittest.TestCase):
    def test_document_id_differs_by_tab_reason(self):
        np = CancellationRow("970498127", "A", date(2026, 10, 15), "$1", "NON-PAYMENT", LIST_URL)
        uw = CancellationRow("970498127", "A", date(2026, 10, 15), "$1", "UNDERWRITING", LIST_URL)
        self.assertNotEqual(np.document_id, uw.document_id)
        self.assertTrue(np.document_id.startswith("fao:970498127:2026-10-15:"))

    def test_fao_document_filename(self):
        doc = FaoDocument("970498127", "Cancellation Notice - Non Payment",
                          date(2026, 9, 28), "EMAIL", 0)
        self.assertEqual(doc.filename, "970498127 Cancellation_Notice_-_Non_Payment Progressive.pdf")
        self.assertTrue(doc.document_id.startswith("fao:970498127:2026-09-28:"))

    def test_row_index_in_document_id(self):
        a = FaoDocument("970498127", "Notice of Cancellation", date(2026, 9, 28), "EMAIL", 0)
        b = FaoDocument("970498127", "Notice of Cancellation", date(2026, 9, 28), "USPS", 1)
        # Same name/date slugs: distinct ledger identity is enforced by exact
        # document_id + filename + sha match in the ledger.
        self.assertEqual(a.document_id, b.document_id)


class UrlGateTest(unittest.TestCase):
    def test_fao_report_ok(self):
        self.assertEqual(require_fao_url(REPORT_URL), REPORT_URL)

    def test_cl_policy_ok(self):
        url = "https://clpolicy.foragentsonly.com/Express/Default.aspx?pageName=PolicySummary"
        self.assertEqual(require_fao_url(url), url)

    def test_login_holds(self):
        with self.assertRaises(IntakeHold):
            require_fao_url("https://www.foragentsonly.com/login")

    def test_wrong_host_holds(self):
        with self.assertRaises(IntakeHold):
            require_fao_url("https://example.com/x")

    def test_http_holds(self):
        with self.assertRaises(IntakeHold):
            require_fao_url("http://www.foragentsonly.com/x")

    def test_blank_holds(self):
        with self.assertRaises(IntakeHold):
            require_fao_url("")


class FakeBrowserTabTest(unittest.TestCase):
    def test_select_tab_and_extract(self):
        page = FakeFaoPage()
        browser = PlaywrightFaoCancellationBrowser(page)
        browser._list_url = LIST_URL
        browser.select_tab("Pending Cancellation Due to Non-Payment")
        rows = browser.load_current_tab("Pending Cancellation Due to Non-Payment")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].policy_number, "970498127")

    def test_select_unknown_tab_holds(self):
        page = FakeFaoPage()
        browser = PlaywrightFaoCancellationBrowser(page)
        browser._list_url = LIST_URL
        with self.assertRaises(IntakeHold):
            browser.select_tab("Billing")


class LedgerTest(unittest.TestCase):
    def _ledger(self):
        root = Path(tempfile.mkdtemp(prefix="fao-canc-test-"))
        return FaoCancellationLedger(root), root

    def _source(self):
        return SourceItem(
            system="fao",
            source_account="www.foragentsonly.com",
            source_id="fao:970498127:2026-09-28:cancellation-notice-non-payment",
            source_url=LIST_URL,
            received_at="2026-10-05T00:00:00+00:00",
            filename="970498127 Cancellation_Notice Progressive.pdf",
            content=b"%PDF-1.4 fake cancellation notice",
        )

    def test_record_then_delivered(self):
        ledger, _ = self._ledger()
        source = self._source()
        saved = ledger.record(source, issued_on=date(2026, 9, 28))
        self.assertTrue(saved.exists())
        self.assertTrue(
            ledger.delivery_status(
                document_id=source.source_id,
                filename=source.filename,
                issued_on=date(2026, 9, 28),
            )
        )

    def test_conflicting_content_holds(self):
        ledger, _ = self._ledger()
        source = self._source()
        ledger.record(source, issued_on=date(2026, 9, 28))
        with self.assertRaises(IntakeHold):
            ledger.delivery_status(
                document_id=source.source_id,
                filename="970498127 Different_Name Progressive.pdf",
                issued_on=date(2026, 9, 28),
            )


class RunPullFixtureTest(unittest.TestCase):
    """run_pull over a fully faked browser: one row, one document, PDF bytes."""

    class FakeBrowser:
        def __init__(self):
            self.report_loaded = False
            self.tabs_seen = []
            self.opened = []
            self.returned = 0
            self.captured = []

        def load_report(self):
            self.report_loaded = True

        def screenshot_report(self):
            return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

        def select_tab(self, label):
            self.tabs_seen.append(label)

        def load_current_tab(self, label):
            if label == "Pending Cancellation Due to Non-Payment":
                return (
                    CancellationRow("970498127", "John Q Trucking LLC", date(2026, 10, 15),
                                    "$1,240.00", "NON-PAYMENT", LIST_URL),
                )
            return ()

        def open_policy_summary(self, policy_number):
            self.opened.append(policy_number)

        def open_documents_tab(self):
            pass

        def list_documents(self, policy_number):
            return (
                FaoDocument("970498127", "Cancellation Notice - Non Payment",
                            date(2026, 9, 28), "EMAIL", 0),
            )

        def capture_document(self, doc):
            self.captured.append(doc.document_id)
            return DocumentCapture((), (b"%PDF-1.4 fake",))

        def return_to_report(self):
            self.returned += 1

    def _pull(self):
        root = Path(tempfile.mkdtemp(prefix="fao-canc-run-"))
        ledger = FaoCancellationLedger(root / "pack")
        archive = SourceArchive(root / "pack" / "sources")
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            receipt = run_pull(self.FakeBrowser(), ledger, archive, as_of=date(2026, 10, 5))
        return receipt, root

    def test_pulled_receipt(self):
        receipt, root = self._pull()
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["process"], "fao")
        self.assertEqual(receipt["count"], 1)
        self.assertEqual(receipt["cancellation_count"], 1)
        self.assertEqual(receipt["held"], [])
        downloaded = receipt["downloaded"][0]
        self.assertEqual(downloaded["policy_number"], "970498127")
        self.assertEqual(downloaded["document_name"], "Cancellation Notice - Non Payment")
        self.assertTrue(Path(downloaded["path"]).exists())
        self.assertEqual(downloaded["sha256"], hashlib.sha256(b"%PDF-1.4 fake").hexdigest())

    def test_second_run_skips_by_ledger(self):
        _, root = self._pull()
        # Re-run against the same pack dir: the document must skip.
        ledger = FaoCancellationLedger(root / "pack")
        archive = SourceArchive(root / "pack" / "sources")

        class SkippingBrowser(self.FakeBrowser):
            def capture_document(self, doc):  # pragma: no cover
                raise AssertionError("must not capture on a ledger hit")

        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            receipt = run_pull(SkippingBrowser(), ledger, archive, as_of=date(2026, 10, 5))
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(receipt["skipped_already_delivered"],
                         ["fao:970498127:2026-09-28:cancellation-notice-non-payment"])


class UploadDriveTest(unittest.TestCase):
    def test_upload_drive_fails_closed(self):
        with self.assertRaises(IntakeHold) as ctx:
            main(["--upload-drive"])
        self.assertIn("not available", str(ctx.exception))


class SelectPageTest(unittest.TestCase):
    def test_exactly_one_fao_tab(self):
        good = SimpleNamespace(url=FAO_HOME)
        other = SimpleNamespace(url="https://example.com")
        self.assertIs(select_fao_page([other, good]), good)
        with self.assertRaises(IntakeHold):
            select_fao_page([other])
        with self.assertRaises(IntakeHold):
            select_fao_page([good, good])


if __name__ == "__main__":
    unittest.main()
