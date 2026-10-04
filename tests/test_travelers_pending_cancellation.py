"""Fixture tests for the Travelers pre-cancellation alert pull. No live login."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.travelers_pending_cancellation import (
    AlertGrid,
    LocalDeliveryLedger,
    PlaywrightTravelersAlertBrowser,
    PullHeld,
    alert_document_id,
    alert_filename,
    parse_alert_grid,
    parse_notice_date,
    refuse_production_host,
    run_pull,
)
from robie_job_engine.intake_core import IntakeHold, SourceArchive


LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
AS_OF = date(2026, 10, 2)
DASHBOARD_URL = "https://foragents.travelers.com/dashboard"
DETAILS_URL = "https://foragents.travelers.com/reports/direct-bill-activity-details"

POLICIES = (
    ("UB-B326957A", "D SERVICE LANDSCAPING LLC", "$864.84", "10/12/2026"),
    ("UB-0S764907", "CRYSTAL WOOD FLOORS LLC", "$5,938.00", "10/12/2026"),
    ("UB-C8291492", "EMPOWER GROUP, LLC", "$556.16", "10/12/2026"),
)
HEADERS = ("Policy Number", "Insured Name", "Amount Due", "Cancellation Notice Date")


def pdf_bytes(mark: bytes) -> bytes:
    return b"%PDF-1.4\n" + mark + b"\n%%EOF\n"


def prove_grid(rows=POLICIES, headers=HEADERS) -> AlertGrid:
    return AlertGrid(DETAILS_URL, headers, tuple(rows))


class FakeNode:
    def __init__(self, tag="", role="", text="", children=(), attrs=None, on_click=None):
        self.tag = tag
        self.role = role
        self.text = text
        self.children = list(children)
        self.attrs = dict(attrs or {})
        self._on_click = on_click

    def matches_role(self, role, name, exact):
        if self.role != role:
            return False
        if name is None:
            return True
        return self.text == name if exact else name.lower() in self.text.lower()

    def find_role(self, role, name, exact):
        found = [self] if self.matches_role(role, name, exact) else []
        for child in self.children:
            found.extend(child.find_role(role, name, exact))
        return found

    def select(self, parts):
        """Match a simple descendant tag selector like ['table', 'tbody', 'tr']."""
        if not parts:
            return [self]
        found = []
        if self.tag == parts[0]:
            if len(parts) == 1:
                found.append(self)
            else:
                for child in self.children:
                    found.extend(child.select(parts[1:]))
        for child in self.children:
            found.extend(child.select(parts))
        # Deduplicate while preserving order.
        seen = []
        for node in found:
            if not any(node is other for other in seen):
                seen.append(node)
        return seen

    def inner_text(self):
        parts = [self.text]
        for child in self.children:
            parts.append(child.inner_text())
        return " ".join(p for p in parts if p)

    def get_attribute(self, name):
        return self.attrs.get(name)


class FakeLocator:
    def __init__(self, page, nodes):
        self._page = page
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    @property
    def first(self):
        return FakeLocator(self._page, self.nodes[:1])

    def all(self):
        return [FakeLocator(self._page, [node]) for node in self.nodes]

    def click(self):
        if len(self.nodes) != 1:
            raise AssertionError("click requires exactly one node")
        node = self.nodes[0]
        self._page.clicks.append(node.text)
        if node._on_click is not None:
            node._on_click()

    def fill(self, value):
        if len(self.nodes) != 1:
            raise AssertionError("fill requires exactly one node")
        self.nodes[0].text = value
        self._page.fills.append(value)

    def inner_text(self):
        if len(self.nodes) != 1:
            raise AssertionError("inner_text requires exactly one node")
        return self.nodes[0].inner_text()

    def get_attribute(self, name):
        if len(self.nodes) != 1:
            raise AssertionError("get_attribute requires exactly one node")
        return self.nodes[0].get_attribute(name)

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self.nodes:
            found.extend(node.find_role(role, name, exact))
        return FakeLocator(self._page, found)

    def locator(self, selector):
        parts = selector.split()
        found = []
        for node in self.nodes:
            found.extend(node.select(parts))
        return FakeLocator(self._page, found)


class _Download:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path


class _ExpectDownload:
    def __init__(self, page):
        self._page = page

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def value(self):
        if self._page.staged_download is None:
            raise TimeoutError("Timeout exceeded while waiting for download")
        return _Download(self._page.staged_download)


class MockTravelersPage:
    """Fake foragents.travelers.com page driving the real page object."""

    def __init__(
        self,
        *,
        policies=POLICIES,
        missing=(),
        agency_rows=("STREETSMART RISK MGR|Agent Code 0X4688",),
        no_save_document=False,
        start="dashboard",
    ):
        self.url = DASHBOARD_URL
        self.state = start
        self.policies = list(policies)
        self.missing = set(missing)
        self.agency_rows = list(agency_rows)
        self.no_save_document = no_save_document
        self.clicks: list[str] = []
        self.fills: list[str] = []
        self.staged_download: str | None = None
        self.policy: str | None = None
        self.visited_snapshot = False

    # -- DOM construction -------------------------------------------------
    def _roots(self):
        if self.state == "dashboard":
            nodes = []
            if "agency_reports" not in self.missing:
                nodes.append(FakeNode(tag="button", role="tab", text="Agency Reports",
                                      on_click=self._to_reports))
            return nodes
        if self.state == "reports":
            nodes = []
            if "report_link" not in self.missing:
                nodes.append(FakeNode(tag="a", role="link",
                                      text="Direct Bill Activity Cancellation & Reinstatement Notices",
                                      on_click=self._to_form))
            return nodes
        if self.state == "form":
            nodes = [FakeNode(tag="input", role="textbox", text="")]
            if "submit" not in self.missing:
                nodes.append(FakeNode(tag="button", role="button", text="View Report",
                                      on_click=self._to_results))
            return nodes
        if self.state == "results":
            rows = []
            for agency in self.agency_rows:
                link = FakeNode(tag="a", role="link", text="view", on_click=self._to_details)
                rows.append(FakeNode(tag="tr", children=[
                    FakeNode(tag="td", text=agency),
                    FakeNode(tag="td", children=[link]),
                ]))
            table = FakeNode(tag="table", children=[
                FakeNode(tag="tbody", children=rows),
            ])
            return [table]
        if self.state == "details":
            header = [FakeNode(tag="th", text=h) for h in HEADERS]
            rows = []
            for policy, insured, amount, notice in self.policies:
                link = FakeNode(
                    tag="a", role="link", text=policy,
                    on_click=lambda p=policy: self._to_alert(p),
                )
                rows.append(FakeNode(tag="tr", children=[
                    FakeNode(tag="td", children=[link]),
                    FakeNode(tag="td", text=insured),
                    FakeNode(tag="td", text=amount),
                    FakeNode(tag="td", text=notice),
                ]))
            table = FakeNode(tag="table", children=[
                FakeNode(tag="thead", children=[FakeNode(tag="tr", children=header)]),
                FakeNode(tag="tbody", children=rows),
            ])
            heading = FakeNode(tag="h1", role="heading", text="Direct Bill Activity Details")
            return [heading, table]
        if self.state == "alert":
            nodes = [
                FakeNode(tag="h1", role="heading", text=f"Pre-cancellation Alert {self.policy}"),
                FakeNode(tag="a", role="link", text=self.policy or ""),
            ]
            if not self.no_save_document:
                nodes.append(FakeNode(tag="button", role="button", text="Save Document",
                                      on_click=self._stage_download))
            return nodes
        if self.state == "snapshot":
            # Customer Snapshot: the refused path. The worker must never land here.
            self.visited_snapshot = True
            return [FakeNode(tag="h1", role="heading", text="Customer Snapshot")]
        return []

    # -- transitions ------------------------------------------------------
    def _to_reports(self):
        self.state = "reports"

    def _to_form(self):
        self.state = "form"

    def _to_results(self):
        self.state = "results"

    def _to_details(self):
        self.state = "details"
        self.url = DETAILS_URL

    def _to_alert(self, policy):
        self.state = "alert"
        self.policy = policy

    def _stage_download(self):
        blob = pdf_bytes((self.policy or "alert").encode())
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp.write(blob)
        tmp.close()
        self.staged_download = tmp.name

    # -- Playwright surface ------------------------------------------------
    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self._roots():
            found.extend(node.find_role(role, name, exact))
        return FakeLocator(self, found)

    def locator(self, selector):
        parts = selector.split()
        found = []
        for node in self._roots():
            found.extend(node.select(parts))
        return FakeLocator(self, found)

    def screenshot(self, full_page=True, type="png"):
        return LIST_PNG

    def go_back(self):
        self.state = "details"
        self.policy = None
        self.staged_download = None

    def expect_download(self, timeout):
        return _ExpectDownload(self)


class ParseTests(unittest.TestCase):
    def test_three_policies_parse(self):
        rows = parse_alert_grid(prove_grid())
        self.assertEqual([r.policy_number for r in rows],
                         ["UB-B326957A", "UB-0S764907", "UB-C8291492"])
        self.assertEqual(rows[0].insured_name, "D SERVICE LANDSCAPING LLC")
        self.assertEqual(rows[0].amount_due, "$864.84")
        self.assertEqual(rows[0].notice_on, date(2026, 10, 12))
        self.assertEqual(rows[0].document_id,
                         "travelers-pre-cancel-alert:UB-B326957A:2026-10-12")
        self.assertEqual(rows[0].filename, "UB-B326957A Pre-Cancellation Alert Travelers.pdf")

    def test_bad_policy_number_holds(self):
        bad = (("UBB326957A", "D SERVICE LANDSCAPING LLC", "$864.84", "10/12/2026"),)
        with self.assertRaises(IntakeHold):
            parse_alert_grid(prove_grid(rows=bad))

    def test_missing_header_holds(self):
        with self.assertRaises(IntakeHold):
            parse_alert_grid(prove_grid(headers=("Policy Number", "Insured Name")))

    def test_duplicate_policy_holds(self):
        dup = (POLICIES[0], POLICIES[0])
        with self.assertRaises(IntakeHold):
            parse_alert_grid(prove_grid(rows=dup))

    def test_notice_date_formats(self):
        self.assertEqual(parse_notice_date("10/12/2026"), date(2026, 10, 12))
        self.assertEqual(parse_notice_date("2026-10-12"), date(2026, 10, 12))
        with self.assertRaises(IntakeHold):
            parse_notice_date("not a date")

    def test_document_id_and_filename(self):
        self.assertEqual(alert_document_id("UB-0S764907", date(2026, 10, 12)),
                         "travelers-pre-cancel-alert:UB-0S764907:2026-10-12")
        self.assertEqual(alert_filename("UB-C8291492"),
                         "UB-C8291492 Pre-Cancellation Alert Travelers.pdf")
        with self.assertRaises(IntakeHold):
            alert_filename("bogus")


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_open_alert_list_click_chain(self):
        page = MockTravelersPage()
        browser = PlaywrightTravelersAlertBrowser(page)
        browser.open_alert_list(AS_OF)
        self.assertEqual(
            page.clicks,
            ["Agency Reports",
             "Direct Bill Activity Cancellation & Reinstatement Notices",
             "View Report",
             "view"],
        )
        self.assertEqual(page.fills, ["10/02/2026"])
        self.assertEqual(page.url, DETAILS_URL)

    def test_load_alert_list_parses_details_table(self):
        page = MockTravelersPage()
        browser = PlaywrightTravelersAlertBrowser(page)
        browser.open_alert_list(AS_OF)
        grid = browser.load_alert_list()
        rows = parse_alert_grid(grid)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].policy_number, "UB-C8291492")

    def test_missing_agency_reports_tab_holds(self):
        page = MockTravelersPage(missing=("agency_reports",))
        browser = PlaywrightTravelersAlertBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "Agency Reports tab"):
            browser.open_alert_list(AS_OF)

    def test_missing_report_link_holds(self):
        page = MockTravelersPage(missing=("report_link",))
        browser = PlaywrightTravelersAlertBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "Direct Bill Activity report link"):
            browser.open_alert_list(AS_OF)

    def test_missing_submit_holds(self):
        page = MockTravelersPage(missing=("submit",))
        browser = PlaywrightTravelersAlertBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "report submit"):
            browser.open_alert_list(AS_OF)

    def test_ambiguous_view_link_without_streetsmart_holds(self):
        page = MockTravelersPage(agency_rows=("OTHER AGENCY|Agent Code 1A1111",
                                              "ANOTHER AGENCY|Agent Code 2B2222"))
        browser = PlaywrightTravelersAlertBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "view link"):
            browser.open_alert_list(AS_OF)

    def test_wrong_host_holds(self):
        page = MockTravelersPage()
        page.url = "https://example.com/dashboard"
        browser = PlaywrightTravelersAlertBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "dashboard URL"):
            browser.open_alert_list(AS_OF)

    def test_snapshot_page_cannot_open_alert_list(self):
        # The Customer Snapshot page has no Agency Reports flow; the worker
        # must hold rather than improvise a transaction Download path.
        page = MockTravelersPage(start="snapshot")
        browser = PlaywrightTravelersAlertBrowser(page)
        with self.assertRaises(IntakeHold):
            browser.open_alert_list(AS_OF)


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict("os.environ", {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"
        self.ledger = LocalDeliveryLedger(self.output)
        self.archive = SourceArchive(self.output / "sources")

    def _run(self, page):
        browser = PlaywrightTravelersAlertBrowser(page)
        return run_pull(browser, self.ledger, self.archive, as_of=AS_OF)

    def test_full_pull_downloads_three_alerts(self):
        page = MockTravelersPage()
        receipt = self._run(page)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 3)
        self.assertEqual(receipt["held"], [])
        for policy, _, _, _ in POLICIES:
            filename = f"{policy} Pre-Cancellation Alert Travelers.pdf"
            content = (self.output / filename).read_bytes()
            self.assertTrue(content.startswith(b"%PDF-1.4"), filename)
            self.assertIn(policy.encode(), content)
        # The only download control the worker touches is Save Document.
        self.assertIn("Save Document", page.clicks)
        self.assertNotIn("Download", page.clicks)
        self.assertFalse(page.visited_snapshot)
        self.assertTrue((self.output / "travelers-pre-cancellation-alert-2026-10-02.png").exists())

    def test_quirk_transaction_download_never_used(self):
        # The verified quirk: Cancel Notice transaction Download returned the
        # Welcome Letter ZIP. The worker must never click a transaction
        # Download and never visit Customer Snapshot for cancellation notices.
        page = MockTravelersPage()
        self._run(page)
        self.assertNotIn("Download", page.clicks)
        self.assertFalse(page.visited_snapshot)
        self.assertEqual(page.url, DETAILS_URL)

    def test_dedup_second_pull_skips_downloads(self):
        page = MockTravelersPage()
        first = self._run(page)
        self.assertEqual(first["count"], 3)
        saves_after_first = page.clicks.count("Save Document")
        self.assertEqual(saves_after_first, 3)
        # Second pull with a fresh page: everything is already in the ledger.
        page2 = MockTravelersPage()
        second = self._run(page2)
        self.assertEqual(second["status"], "PULLED")
        self.assertEqual(second["count"], 0)
        self.assertEqual(page2.clicks.count("Save Document"), 0)
        outcomes = [row["outcome"] for row in second["alerts"]]
        self.assertEqual(outcomes, ["SKIPPED", "SKIPPED", "SKIPPED"])

    def test_missing_save_document_holds(self):
        page = MockTravelersPage(no_save_document=True)
        with self.assertRaises(PullHeld) as ctx:
            self._run(page)
        held = ctx.exception.details["held"]
        self.assertEqual(len(held), 3)
        self.assertTrue(all("Save Document" in row["reason"] for row in held))
        # No partial downloads were recorded.
        self.assertEqual(ctx.exception.details["downloaded"], [])

    def test_kill_switch_requires_test_env(self):
        with patch.dict("os.environ", {}, clear=True):
            page = MockTravelersPage()
            browser = PlaywrightTravelersAlertBrowser(page)
            with self.assertRaises(IntakeHold):
                run_pull(browser, self.ledger, self.archive, as_of=AS_OF)

    def test_ledger_conflict_holds(self):
        page = MockTravelersPage()
        self._run(page)
        # Corrupt the ledger entry: the file no longer matches the recorded digest.
        victim = self.output / "UB-B326957A Pre-Cancellation Alert Travelers.pdf"
        victim.write_bytes(b"tampered")
        page2 = MockTravelersPage()
        browser2 = PlaywrightTravelersAlertBrowser(page2)
        with self.assertRaises(PullHeld) as ctx:
            run_pull(browser2, self.ledger, self.archive, as_of=AS_OF)
        held = ctx.exception.details["held"]
        self.assertEqual(len(held), 1)
        self.assertIn("conflicts", held[0]["reason"])

    def test_production_host_refused(self):
        with patch("socket.gethostname", return_value="hermes-poc-01"), \
             patch("socket.getfqdn", return_value="hermes-poc-01"):
            with self.assertRaisesRegex(IntakeHold, "hermes-poc-01"):
                refuse_production_host()


if __name__ == "__main__":
    unittest.main()
