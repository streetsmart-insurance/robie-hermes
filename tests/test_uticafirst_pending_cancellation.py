"""Tests for the Test-only Utica First Pending Cancellation worker.

All browser interaction runs against FakeUticaFirstPage: no live browser,
no network, no credentials.
"""
from __future__ import annotations

import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.intake_core import IntakeHold, SourceArchive
from robie_job_engine.uticafirst_pending_cancellation import (
    LocalDeliveryLedger,
    PlaywrightUticaFirstNocBrowser,
    find_cancellation_document,
    is_cancellation_family,
    is_reversal_or_renewal,
    noc_document_id,
    noc_filename,
    parse_carrier_date,
    parse_document_grid,
    parse_transaction_grid,
    run_pull,
)

AS_OF = date(2026, 10, 4)
LIST_URL = "https://ufirstnow.uticafirst.com/Policy/TransactionList"
PDF_BYTES = b"%PDF-1.7 fake utica first notice"
LIST_PNG = b"\x89PNG\r\n\x1a\nfake-list-shot"

TXN_HEADERS = (
    "Policy Number",
    "Insured",
    "Transaction Type",
    "Transaction Date",
    "Status",
)
TXN_ROWS = (
    ("ART3001958910", "Sunflower Maid Services LLC", "Pending Cancellation(NOC)", "10/02/2026", "Pending"),
    ("ART3001958911", "Test Insured LLC", "Cancellation", "10/01/2026", "Pending"),
    ("ART3001958912", "Renewal Insured Inc", "Renewal", "10/01/2026", "Active"),
    ("ART3001958913", "Reinstated Co", "Reinstatement", "09/30/2026", "Active"),
    ("ART3001958914", "NonRenew Co", "Non-Renewal", "10/03/2026", "Pending"),
)
DOC_HEADERS = ("ID", "NAME", "TYPE", "SOURCE", "RENDERING STATUS")
DOC_ROWS = (
    ("DOC001", "ADDITIONAL INSURED WAIVER OF SUBROGATION", "Policy Form", "UF Document Delivery", "Completed"),
    ("DOC002", "NOTICE OF CANCELLATION", "Notice", "UF Document Delivery", "Completed"),
    ("DOC003", "DECLARATIONS", "Policy Form", "UF Document Delivery", "Completed"),
)


class FakeNode:
    def __init__(self, role, name="", text="", attrs=None, children=()):
        self.role = role
        self.name = name
        self.text = text or name
        self.attrs = dict(attrs or {})
        self.children = list(children)
        self.page = None

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_checked(self):
        return bool(self.attrs.get("checked"))

    def find_role(self, role, name=None, exact=True):
        found = []
        if self.role == role:
            if name is None:
                found.append(self)
            else:
                target = str(name).casefold()
                mine = str(self.name).casefold()
                if (exact and mine == target) or (not exact and target in mine):
                    found.append(self)
        for child in self.children:
            found.extend(child.find_role(role, name, exact))
        return found

    def find_text(self, text, exact=False):
        found = []
        target = str(text).casefold()
        mine = str(self.text).casefold()
        if (exact and mine == target) or (not exact and target in mine):
            found.append(self)
        for child in self.children:
            found.extend(child.find_text(text, exact))
        return found


class FakeLocator:
    def __init__(self, nodes, page=None):
        self.nodes = list(nodes)
        self.page = page

    def count(self):
        return len(self.nodes)

    def all(self):
        return [FakeLocator([n], self.page) for n in self.nodes]

    def first(self):
        return FakeLocator(self.nodes[:1], self.page)

    def get_attribute(self, name):
        return self.nodes[0].get_attribute(name)

    def inner_text(self):
        return "\n".join(n.inner_text() for n in self.nodes)

    def is_checked(self):
        return self.nodes[0].is_checked()

    def click(self):
        if self.page is not None:
            self.page.on_click(self.nodes[0])

    def dblclick(self):
        if self.page is not None:
            self.page.on_dblclick(self.nodes[0])

    def check(self):
        node = self.nodes[0]
        node.attrs["checked"] = True
        if self.page is not None:
            self.page.on_click(node)

    def locator(self, selector):
        # Only used for table cell traversal in these tests.
        if selector == "td":
            cells = []
            for node in self.nodes:
                cells.extend([c for c in node.children if c.role == "td"])
            return FakeLocator(cells, self.page)
        raise AssertionError(f"unsupported selector {selector!r}")


class FakeResponse:
    def __init__(self, url, headers, body):
        self.url = url
        self.headers = headers
        self._body = body

    def body(self):
        return self._body


class _ExpectResponse:
    """Mimics page.expect_response: the viewer open triggers the response."""

    def __init__(self, page, predicate):
        self.page = page
        self.predicate = predicate
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        for response in self.page.pending_responses:
            try:
                if self.predicate(response):
                    self.value = response
                    return False
            except Exception:
                continue
        raise TimeoutError("Timeout exceeded while waiting for response")


class FakeUticaFirstPage:
    """State machine mock of the UFIRST Now cancellation pull."""

    def __init__(
        self,
        *,
        txn_rows=TXN_ROWS,
        doc_rows=DOC_ROWS,
        viewer_pdf=True,
        viewer_bytes=PDF_BYTES,
        with_filter_button=True,
        txn_headers=TXN_HEADERS,
        doc_headers=DOC_HEADERS,
    ):
        self.url = LIST_URL
        self.txn_rows = txn_rows
        self.doc_rows = doc_rows
        self.viewer_pdf = viewer_pdf
        self.viewer_bytes = viewer_bytes
        self.with_filter_button = with_filter_button
        self.txn_headers = txn_headers
        self.doc_headers = doc_headers
        self.state = "transactions"
        self.current_policy = ""
        self.clicks = []
        self.pending_responses = []

    # -- page API used by the worker -------------------------------------
    def get_by_role(self, role, name=None, exact=True):
        return FakeLocator(self._roots_role(role, name, exact), self)

    def get_by_text(self, text, exact=False):
        found = []
        for node in self._roots():
            found.extend(node.find_text(text, exact))
        return FakeLocator(found, self)

    def locator(self, selector):
        if selector == "body":
            return FakeLocator([FakeNode("body", text=self._body_text())], self)
        if selector == "table thead th":
            headers = self.txn_headers if self.state == "transactions" else self.doc_headers
            return FakeLocator([FakeNode("th", text=h) for h in headers], self)
        if selector == "table tbody tr":
            rows = self.txn_rows if self.state == "transactions" else self.doc_rows
            trs = []
            for cells in rows:
                tds = [FakeNode("td", text=c) for c in cells]
                trs.append(FakeNode("tr", children=tds))
            # Wire child page refs for locator("td") traversal.
            for tr in trs:
                for td in tr.children:
                    td.page = self
            return FakeLocator(trs, self)
        raise AssertionError(f"unsupported selector {selector!r}")

    def wait_for_selector(self, selector, timeout=None):
        marker = selector.split("text=", 1)[1] if selector.startswith("text=") else selector
        if marker.casefold() not in self._body_text().casefold():
            raise TimeoutError(f"selector not found: {selector}")

    def screenshot(self, full_page=True, type="png"):
        if type == "png" and self.state == "transactions":
            return LIST_PNG
        return b""

    def go_back(self):
        self.state = "transactions"
        self.current_policy = ""
        self.url = LIST_URL

    def expect_response(self, predicate, timeout=None):
        return _ExpectResponse(self, predicate)

    # -- mock DOM ---------------------------------------------------------
    def _body_text(self):
        if self.state == "transactions":
            return "Welcome Carlo Ferrara AGENCY ADMIN | HOME POLICY | TRANSACTION LIST TRANSACTION TYPE"
        if self.state == "policy":
            return f"POLICY | CURRENT SUMMARY {self.current_policy} DOCUMENTS"
        if self.state == "documents":
            return "POLICY | CURRENT SUMMARY Following is a list of documents for this policy."
        if self.state == "viewer":
            return "Document Viewer NOTICE OF CANCELLATION"
        return ""

    def _roots(self):
        if self.state == "transactions":
            nodes = [
                FakeNode("link", name="POLICY TRANSACTIONS"),
                FakeNode("radio", name="All"),
                FakeNode("radio", name="Today"),
            ]
            for label in (
                "Cancellation",
                "Cancellation - Insured",
                "Non-Renewal",
                "Pending Cancellation(NOC)",
                "Intent to Non-Renew",
                "PreRenewal Notice",
            ):
                nodes.append(FakeNode("checkbox", name=label))
            if self.with_filter_button:
                nodes.append(FakeNode("button", name="FILTER LIST"))
            for policy, _insured, _ttype, _tdate, _status in self.txn_rows:
                nodes.append(FakeNode("link", name=policy))
            return nodes
        if self.state == "policy":
            return [FakeNode("tab", name="DOCUMENTS")]
        if self.state == "documents":
            return [FakeNode("link", name=row[1]) for row in self.doc_rows]
        return []

    def _roots_role(self, role, name=None, exact=True):
        found = []
        for node in self._roots():
            found.extend(node.find_role(role, name, exact))
        return found

    # -- interactions ------------------------------------------------------
    def on_click(self, node):
        self.clicks.append(node.name)
        if node.role == "link" and node.name.startswith("ART"):
            self.current_policy = node.name
            self.state = "policy"
            self.url = f"https://ufirstnow.uticafirst.com/Policy/Summary/{node.name}"
        elif node.name == "DOCUMENTS":
            self.state = "documents"
        elif node.name == "FILTER LIST":
            self.state = "transactions"

    def on_dblclick(self, node):
        self.clicks.append(f"dbl:{node.name}")
        self.state = "viewer"
        self.pending_responses = []
        if self.viewer_pdf:
            self.pending_responses.append(FakeResponse(
                url="https://ufirstnow.uticafirst.com/Document/Render/DOC002.pdf",
                headers={"content-type": "application/pdf"},
                body=self.viewer_bytes,
            ))


class UticaFirstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict("os.environ", {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"
        self.ledger = LocalDeliveryLedger(self.output)
        self.archive = SourceArchive(self.output / "sources")

    def _browser(self, **kwargs):
        return PlaywrightUticaFirstNocBrowser(FakeUticaFirstPage(**kwargs))

    # -- classification ----------------------------------------------------
    def test_cancellation_family_types(self):
        for label in (
            "Cancellation",
            "Cancellation - Insured",
            "Non-Renewal",
            "Pending Cancellation(NOC)",
            "Pending Cancellation (NOC)",
            "Intent to Non-Renew",
            "PreRenewal Notice",
        ):
            self.assertTrue(is_cancellation_family(label), label)

    def test_reversal_and_renewal_types_excluded(self):
        for label in ("Reinstatement", "Rescind Pending Cancellation", "Renewal"):
            self.assertFalse(is_cancellation_family(label), label)
            self.assertTrue(is_reversal_or_renewal(label), label)

    def test_transaction_grid_parsing_keeps_only_cancellation_family(self):
        grid = parse_transaction_grid(TXN_HEADERS, TXN_ROWS, list_url=LIST_URL)
        policies = [r.policy_number for r in grid.rows]
        self.assertEqual(policies, ["ART3001958910", "ART3001958911", "ART3001958914"])
        self.assertEqual(grid.rows[0].transaction_type, "Pending Cancellation(NOC)")

    def test_transaction_grid_bad_policy_number_holds(self):
        rows = (("NOT-A-POLICY", "X", "Cancellation", "10/01/2026", "Pending"),)
        with self.assertRaises(IntakeHold):
            parse_transaction_grid(TXN_HEADERS, rows, list_url=LIST_URL)

    def test_transaction_grid_ambiguous_header_holds(self):
        headers = ("Policy Number", "Type", "Transaction Type", "Transaction Date", "Status")
        with self.assertRaises(IntakeHold):
            parse_transaction_grid(headers, TXN_ROWS, list_url=LIST_URL)

    def test_document_grid_parsing(self):
        grid = parse_document_grid(DOC_HEADERS, DOC_ROWS, policy_number="ART3001958910")
        self.assertEqual(len(grid.rows), 3)
        self.assertEqual(grid.rows[1].name, "NOTICE OF CANCELLATION")
        self.assertEqual(grid.rows[1].rendering_status, "Completed")

    def test_find_cancellation_document(self):
        grid = parse_document_grid(DOC_HEADERS, DOC_ROWS, policy_number="ART3001958910")
        doc = find_cancellation_document(grid)
        self.assertEqual(doc.document_id, "DOC002")

    def test_ambiguous_cancellation_document_holds(self):
        rows = DOC_ROWS + (
            ("DOC004", "CANCELLATION NOTICE", "Notice", "UF Document Delivery", "Completed"),
        )
        grid = parse_document_grid(DOC_HEADERS, rows, policy_number="ART3001958910")
        with self.assertRaises(IntakeHold):
            find_cancellation_document(grid)

    def test_document_id_and_filename(self):
        day = date(2026, 10, 2)
        self.assertEqual(
            noc_document_id("ART3001958910", "Pending Cancellation(NOC)", day),
            "uticafirst-noc:ART3001958910:pending cancellation(noc):2026-10-02",
        )
        self.assertEqual(
            noc_filename("ART3001958910", "Pending Cancellation(NOC)"),
            "ART3001958910 PendingCancellationNoc UticaFirst.pdf",
        )
        with self.assertRaises(IntakeHold):
            noc_document_id("BAD", "Cancellation", day)

    def test_parse_carrier_date(self):
        self.assertEqual(parse_carrier_date("10/02/2026"), date(2026, 10, 2))
        with self.assertRaises(IntakeHold):
            parse_carrier_date("not a date")

    # -- pull flow ----------------------------------------------------------
    def test_full_pull_downloads_cancellation_notices(self):
        browser = self._browser()
        receipt = run_pull(browser, self.ledger, self.archive, as_of=AS_OF)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 3)
        self.assertEqual(receipt["held"], [])
        for item in receipt["downloaded"]:
            self.assertTrue(item["filename"].endswith("UticaFirst.pdf"))
            day = parse_carrier_date(item["transaction_date"]).isoformat()
            path = self.output / day / item["filename"]
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
        # Filter + per-policy navigation happened.
        self.assertIn("FILTER LIST", browser.page.clicks)
        self.assertIn("ART3001958910", browser.page.clicks)
        self.assertIn("DOCUMENTS", browser.page.clicks)

    def test_rerun_is_deduped(self):
        browser = self._browser()
        first = run_pull(browser, self.ledger, self.archive, as_of=AS_OF)
        self.assertEqual(first["count"], 3)
        browser2 = self._browser()
        second = run_pull(browser2, self.ledger, self.archive, as_of=AS_OF)
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["held"], [])

    def test_framed_viewer_without_bytes_holds(self):
        browser = self._browser(viewer_pdf=False)
        receipt = run_pull(browser, self.ledger, self.archive, as_of=AS_OF)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(len(receipt["held"]), 3)
        for held in receipt["held"]:
            self.assertIn("no PDF byte access", held["reason"])
        # Nothing was recorded: no download was claimed.
        self.assertEqual(self.ledger._load()["items"], {})

    def test_non_pdf_viewer_content_holds(self):
        browser = self._browser(viewer_bytes=b"<html>not a pdf</html>")
        receipt = run_pull(browser, self.ledger, self.archive, as_of=AS_OF)
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(len(receipt["held"]), 3)
        for held in receipt["held"]:
            self.assertIn("did not produce PDF bytes", held["reason"])

    def test_missing_filter_button_holds(self):
        browser = self._browser(with_filter_button=False)
        with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
            run_pull(browser, self.ledger, self.archive, as_of=AS_OF)

    def test_ledger_conflict_holds(self):
        browser = self._browser()
        run_pull(browser, self.ledger, self.archive, as_of=AS_OF)
        # Corrupt the recorded file: the next pull must hold, not overwrite.
        item = self.ledger._load()["items"]
        first_id = next(iter(item))
        filename = item[first_id]["filename"]
        processed = item[first_id]["processed_date"]
        path = self.output / processed / filename
        path.write_bytes(b"%PDF- tampered")
        browser2 = self._browser()
        with self.assertRaisesRegex(IntakeHold, "conflicts with the pull ledger"):
            run_pull(browser2, self.ledger, self.archive, as_of=AS_OF)

    # -- guards --------------------------------------------------------------
    def test_kill_switch_off_holds(self):
        with patch.dict("os.environ", {"ROBIE_ENV": ""}):
            browser = self._browser()
            with self.assertRaisesRegex(IntakeHold, "TEST"):
                run_pull(browser, self.ledger, self.archive, as_of=AS_OF)

    def test_production_host_refused(self):
        with patch.object(socket, "gethostname", return_value="hermes-poc-01"), \
             patch.object(socket, "getfqdn", return_value="hermes-poc-01"):
            browser = self._browser()
            with self.assertRaisesRegex(IntakeHold, "refuses Production host"):
                run_pull(browser, self.ledger, self.archive, as_of=AS_OF)

    def test_unauthenticated_tab_holds(self):
        page = FakeUticaFirstPage()
        page.url = "https://example.com/"
        browser = PlaywrightUticaFirstNocBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "Utica First tab"):
            run_pull(browser, self.ledger, self.archive, as_of=AS_OF)


if __name__ == "__main__":
    unittest.main()
