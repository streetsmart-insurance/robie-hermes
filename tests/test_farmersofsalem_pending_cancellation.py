"""Fixture tests for the Farmers of Salem pending-cancellation pull. No live login."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine import farmersofsalem_pending_cancellation as fos
from robie_job_engine.farmersofsalem_pending_cancellation import (
    FinysFoSBrowser,
    FoSDocument,
    LocalDeliveryLedger,
    PullHeld,
    classify_notice,
    extract_documents,
    extract_pending_items,
    notice_document_id,
    notice_filename,
    open_finys_from_portal,
    parse_carrier_date,
    parse_policy_number,
    run_pull,
    select_target_document,
)
from robie_job_engine.intake_core import IntakeHold, SourceArchive


LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
AS_OF = date(2026, 10, 4)
FINYS_TASKS_URL = "https://fos.finys.com/tasks"

HONJ = "HONJ038633"
HODJ = "HODJ024421"
HOMJ3 = "HOMJ045433"

PENDING_ROWS = (
    (HONJ, "Maria Lua", "FINYS_HOMEOWNERS", "10/8/2026"),
    (HODJ, "Robert Diaz", "FINYS_HOMEOWNERS", "10/30/2026"),
    (HOMJ3, "Susan Park", "FINYS_HOMEOWNERS", "10/6/2026"),
    ("HOMJ045430", "James Park", "FINYS_HOMEOWNERS", "10/5/2026"),
    ("HOUJ019521", "Ana Ruiz", "FINYS_HOMEOWNERS", "10/2/2026"),
)


def pdf_bytes(mark: bytes) -> bytes:
    return b"%PDF-1.4\n" + mark + b"\n%%EOF\n"


def _name_matches(node_name: str, wanted: str, exact: bool) -> bool:
    node_name = " ".join(str(node_name or "").split()).casefold()
    wanted = " ".join(str(wanted or "").split()).casefold()
    if exact:
        return node_name == wanted
    return wanted in node_name


class FakeNode:
    def __init__(self, *, role=None, name="", text=None, attrs=None, children=(), tag=None, action=None):
        self.role = role
        self._name = name
        self._text = name if text is None else text
        self.attrs = dict(attrs or {})
        self.children = list(children)
        self.tag = tag
        self.action = action
        self.page = None
        self.token = None

    @property
    def name(self):
        return self._name

    def inner_text(self) -> str:
        parts = [self._text] if self._text else []
        for child in self.children:
            parts.append(child.inner_text())
        return " ".join(part for part in parts if part)

    def get_attribute(self, name):
        return self.attrs.get(name)

    def click(self):
        self.page.on_click(self)

    def fill(self, value):
        self.page.on_fill(self, value)

    def press(self, key):
        self.page.on_press(self, key)

    def find_role(self, role, name=None, exact=True):
        found = []
        if self.role == role and (name is None or _name_matches(self._name, name, exact)):
            found.append(self)
        for child in self.children:
            found.extend(child.find_role(role, name, exact))
        return found

    def _descendants(self, tag):
        found = []
        for child in self.children:
            if child.tag == tag:
                found.append(child)
            found.extend(child._descendants(tag))
        return found

    def find_selector(self, selector):
        parts = selector.split()
        if len(parts) == 1:
            mine = [self] if self.tag == parts[0] else []
            return mine + self._descendants(parts[0])
        if len(parts) == 2:
            parents = ([self] if self.tag == parts[0] else []) + self._descendants(parts[0])
            found = []
            for parent in parents:
                found.extend(parent._descendants(parts[1]))
            return found
        raise ValueError(f"unsupported selector: {selector!r}")

    def locator(self, selector):
        return FakeLocator(self.find_selector(selector), self.page)

    def get_by_role(self, role, name=None, exact=True):
        return FakeLocator(self.find_role(role, name, exact), self.page)


class FakeLocator:
    def __init__(self, nodes, page):
        self.nodes = list(nodes)
        self.page = page

    def count(self):
        return len(self.nodes)

    def all(self):
        return [FakeLocator([node], self.page) for node in self.nodes]

    @property
    def first(self):
        return FakeLocator(self.nodes[:1], self.page)

    def inner_text(self):
        return self.nodes[0].inner_text()

    def get_attribute(self, name):
        return self.nodes[0].get_attribute(name)

    def click(self):
        self.nodes[0].click()

    def fill(self, value):
        self.nodes[0].fill(value)

    def press(self, key):
        self.nodes[0].press(key)

    def locator(self, selector):
        found = []
        for node in self.nodes:
            found.extend(node.find_selector(selector))
        return FakeLocator(found, self.page)

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self.nodes:
            found.extend(node.find_role(role, name, exact))
        return FakeLocator(found, self.page)


class _DownloadExpect:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        if self.page._download is None:
            raise TimeoutError("Timeout exceeded while waiting for download")
        return False

    @property
    def value(self):
        return self.page._download


class _PopupExpect:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def value(self):
        return self.page._popup_value


def _table(headers, rows):
    """Build a table node. Row cells may be str, a FakeNode, or a list of FakeNodes."""
    head = FakeNode(tag="thead", children=[
        FakeNode(tag="tr", children=[FakeNode(tag="th", name=h) for h in headers])
    ])
    body_rows = []
    for cells in rows:
        tds = []
        for cell in cells:
            if isinstance(cell, (list, tuple)):
                tds.append(FakeNode(tag="td", children=list(cell)))
            elif isinstance(cell, FakeNode):
                cell.tag = "td"
                tds.append(cell)
            else:
                tds.append(FakeNode(tag="td", name=str(cell)))
        body_rows.append(FakeNode(tag="tr", children=tds))
    body = FakeNode(tag="tbody", children=body_rows)
    return FakeNode(tag="table", children=[head, body])


def _view_link(token, *, name="View"):
    node = FakeNode(role="link", name=name, action="view")
    node.token = token
    return node

class FakeFinysPage:
    """Mock Finys tab with tasks -> policy -> docs states."""

    def __init__(
        self,
        *,
        pending_rows=PENDING_ROWS,
        docs_by_policy=None,
        policy_shown=None,
        url=FINYS_TASKS_URL,
    ):
        self.url = url
        self.state = "tasks"
        self.pending_rows = pending_rows
        self.docs_by_policy = docs_by_policy or {}
        self.policy_shown = policy_shown  # policy displayed on the Policy Summary; None => searched policy
        self.clicks = []
        self.searched_policy = None
        self._download = None
        self._popup_value = None
        self.context = SimpleNamespace(pages=[self], request=SimpleNamespace(get=self._http_get))

    # -- plumbing ---------------------------------------------------------
    def _http_get(self, url, timeout=None):
        raise AssertionError("unexpected viewer-tab fetch in this fixture")

    def _adopt(self, node):
        node.page = self
        for child in node.children:
            self._adopt(child)
        return node

    def _tasks_table(self):
        return _table(
            ("Policy Number", "Insured Name", "Product", "Due Date"),
            [tuple(row) for row in self.pending_rows],
        )

    def _docs_table(self, policy):
        rows = self.docs_by_policy.get(policy, ())
        return _table(("Description", "Date", "Action"), [tuple(row) for row in rows])

    def roots(self):
        if self.state == "tasks":
            nodes = [
                FakeNode(role="heading", name="My Open Tasks - pending items"),
                self._tasks_table(),
                FakeNode(role="textbox", name="Policy Search", action="search-box"),
                FakeNode(role="button", name="Search", action="search"),
                FakeNode(role="link", name="My Pending Cancellation Items"),
            ]
        elif self.state == "policy":
            shown = self.policy_shown if self.policy_shown is not None else self.searched_policy
            nodes = [
                FakeNode(role="heading", name=f"Policy Summary - {shown}"),
                FakeNode(role="link", name="Document Summary", action="doc-summary"),
                FakeNode(role="link", name="My Open Tasks", action="my-open-tasks"),
            ]
        elif self.state == "docs":
            nodes = [
                FakeNode(role="heading", name=f"Document Summary - {self.searched_policy}"),
                self._docs_table(self.searched_policy),
                FakeNode(role="link", name="My Open Tasks", action="my-open-tasks"),
            ]
        else:
            nodes = []
        return [self._adopt(node) for node in nodes]

    def locator(self, selector):
        found = []
        for node in self.roots():
            found.extend(node.find_selector(selector))
        return FakeLocator(found, self)

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self.roots():
            found.extend(node.find_role(role, name, exact))
        return FakeLocator(found, self)

    def screenshot(self, full_page=True, type="png"):
        if full_page and type == "png" and self.state == "tasks":
            return LIST_PNG
        return b""

    def expect_download(self, timeout=None):
        return _DownloadExpect(self)

    def goto(self, url):
        self.url = url
        self.state = "tasks"

    # -- interactions ------------------------------------------------------
    def on_click(self, node):
        self.clicks.append(node.name)
        action = node.action
        if action == "search" and self.state == "tasks":
            self.state = "policy"
            self.url = f"https://fos.finys.com/policy/{self.searched_policy}"
        elif action == "doc-summary" and self.state == "policy":
            self.state = "docs"
            self.url = f"https://fos.finys.com/policy/{self.searched_policy}/documents"
        elif action == "my-open-tasks":
            self.state = "tasks"
            self.url = FINYS_TASKS_URL
        elif action == "view" and self.state == "docs":
            token = node.token
            if token is not None:
                self._download = SimpleNamespace(save_as=lambda path, token=token: Path(path).write_bytes(token))

    def on_fill(self, node, value):
        if node.action == "search-box":
            self.searched_policy = value

    def on_press(self, node, key):
        if node.action == "search-box" and key == "Enter" and self.state == "tasks":
            self.state = "policy"
            self.url = f"https://fos.finys.com/policy/{self.searched_policy}"


class FakePortalPage:
    """Mock farmersofsalem.com tab for the FOS PORTAL handoff."""

    def __init__(self, *, opens_tab=True, finys_url=FINYS_TASKS_URL):
        self.url = "https://www.farmersofsalem.com/agent_home.aspx"
        self._opens_tab = opens_tab
        self._finys_url = finys_url
        self._popup_value = None
        self.context = SimpleNamespace(pages=[self])
        link = FakeNode(role="link", name="FOS PORTAL", action="fos-portal")
        link.page = self
        self._link = link

    def get_by_role(self, role, name=None, exact=True):
        if role == "link" and _name_matches("FOS PORTAL", name or "", exact):
            return FakeLocator([self._link], self)
        return FakeLocator([], self)

    def locator(self, selector):
        return FakeLocator([], self)

    def expect_popup(self, timeout=None):
        return _PopupExpect(self)

    def on_click(self, node):
        if node.action == "fos-portal" and self._opens_tab:
            finys = FakeFinysPage(url=self._finys_url)
            self.context.pages.append(finys)
            self._popup_value = finys

    def on_fill(self, node, value):
        pass

    def on_press(self, node, key):
        pass


def _docs_for(policy, token):
    return [
        ("Policy Declarations", "09/01/2026", None),
        ("Intent to Cancel Notice", "09/28/2026", _view_link(token)),
        ("Billing Statement", "09/15/2026", _view_link(pdf_bytes(b"billing"))),
    ]


def _browser(policy=HONJ, token=b"notice"):
    page = FakeFinysPage(docs_by_policy={policy: _docs_for(policy, pdf_bytes(token))})
    return FinysFoSBrowser(page), page


class UnitTests(unittest.TestCase):
    def test_policy_number_format(self):
        self.assertEqual(parse_policy_number("HONJ038633"), "HONJ038633")
        self.assertEqual(parse_policy_number("honj038633"), "HONJ038633")
        for bad in ("", "HONJ03863", "HONJ0386334", "1234567890", "HONJ-038633", "HONJ 038633X"):
            with self.assertRaises(IntakeHold):
                parse_policy_number(bad)

    def test_carrier_date(self):
        self.assertEqual(parse_carrier_date("10/8/2026"), date(2026, 10, 8))
        self.assertEqual(parse_carrier_date("2026-10-08"), date(2026, 10, 8))
        with self.assertRaises(IntakeHold):
            parse_carrier_date("not a date")

    def test_classify_notice(self):
        self.assertEqual(classify_notice("Intent to Cancel Notice"), "intent-to-cancel")
        self.assertEqual(classify_notice("NOTICE OF CANCELLATION"), "cancellation-notice")
        self.assertEqual(classify_notice("Underwriting Memo - inspection"), "underwriting-memo")
        self.assertEqual(classify_notice("Billing Memo"), "billing-memo")
        self.assertIsNone(classify_notice("Policy Declarations"))
        self.assertIsNone(classify_notice("Correspondence"))
        self.assertIsNone(classify_notice(""))

    def test_document_id_and_filename(self):
        document_id = notice_document_id(HONJ, date(2026, 9, 28), "intent-to-cancel")
        self.assertEqual(document_id, "farmersofsalem:HONJ038633:2026-09-28:intent-to-cancel")
        self.assertEqual(
            notice_filename(HONJ, "intent-to-cancel"),
            "HONJ038633 Intent to Cancel Notice FarmersofSalem.pdf",
        )
        with self.assertRaises(IntakeHold):
            notice_document_id(HONJ, date(2026, 9, 28), "nope")

    def test_select_target_document_prefers_most_recent(self):
        view = _view_link(pdf_bytes(b"x"))
        docs = (
            FoSDocument("Billing Memo", date(2026, 9, 20), "billing-memo", view),
            FoSDocument("Intent to Cancel Notice", date(2026, 9, 28), "intent-to-cancel", view),
            FoSDocument("Cancellation Notice", date(2026, 9, 25), "cancellation-notice", view),
            FoSDocument("Policy Declarations", date(2026, 9, 30), None, None),
        )
        target = select_target_document(docs)
        self.assertEqual(target.description, "Intent to Cancel Notice")
        self.assertIsNone(select_target_document(()))
        undated = (FoSDocument("Intent to Cancel Notice", None, "intent-to-cancel", view),)
        self.assertIsNone(select_target_document(undated))
        noview = (FoSDocument("Intent to Cancel Notice", date(2026, 9, 28), "intent-to-cancel", None),)
        self.assertIsNone(select_target_document(noview))

class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.nav = patch.object(fos, "_NAV_TIMEOUT_MS", 300)
        self.nav.start()
        self.addCleanup(self.nav.stop)

    def test_pending_items_parse_five_policies(self):
        page = FakeFinysPage()
        items = extract_pending_items(page)
        self.assertEqual(len(items), 5)
        self.assertEqual(
            [(item.policy_number, item.due_on) for item in items],
            [
                (HONJ, date(2026, 10, 8)),
                (HODJ, date(2026, 10, 30)),
                (HOMJ3, date(2026, 10, 6)),
                ("HOMJ045430", date(2026, 10, 5)),
                ("HOUJ019521", date(2026, 10, 2)),
            ],
        )
        self.assertEqual(items[0].insured_name, "Maria Lua")
        self.assertEqual(items[0].product, "FINYS_HOMEOWNERS")

    def test_missing_pending_table_holds(self):
        page = FakeFinysPage(pending_rows=())
        # Empty table body still parses; a page with no table at all holds.
        page.state = "policy"
        with self.assertRaisesRegex(IntakeHold, "Finys table is missing or ambiguous"):
            extract_pending_items(page)

    def test_policy_search_to_document_summary_navigation(self):
        browser, page = _browser()
        browser.open_policy(HONJ)
        self.assertEqual(page.state, "policy")
        browser.open_document_summary()
        self.assertEqual(page.state, "docs")
        documents = browser.list_documents()
        self.assertEqual(len(documents), 3)
        target = select_target_document(documents)
        self.assertIsNotNone(target)
        self.assertEqual(target.notice_key, "intent-to-cancel")

    def test_policy_search_mismatch_holds(self):
        page = FakeFinysPage(policy_shown="HONJ999999")
        browser = FinysFoSBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "Policy Summary for HONJ038633 is missing or ambiguous"):
            browser.open_policy(HONJ)

    def test_ambiguous_view_links_hold(self):
        ambiguous_rows = [
            (
                "Intent to Cancel Notice",
                "09/28/2026",
                [_view_link(pdf_bytes(b"a")), _view_link(pdf_bytes(b"b"))],
            ),
        ]
        page = FakeFinysPage(docs_by_policy={HONJ: ambiguous_rows})
        page.state = "docs"
        page.searched_policy = HONJ
        with self.assertRaisesRegex(IntakeHold, "Document View link .* is missing or ambiguous"):
            extract_documents(page)

    def test_portal_handoff_opens_finys_tab(self):
        portal = FakePortalPage()
        finys = open_finys_from_portal(portal)
        self.assertEqual(finys.url, FINYS_TASKS_URL)
        items = extract_pending_items(finys)
        self.assertEqual(len(items), 5)

    def test_portal_handoff_without_finys_tab_holds(self):
        portal = FakePortalPage(opens_tab=False)
        with self.assertRaisesRegex(IntakeHold, "FOS PORTAL did not open the Finys tab"):
            open_finys_from_portal(portal)

    def test_portal_handoff_wrong_host_holds(self):
        portal = FakePortalPage()
        portal.url = "https://example.com/"
        with self.assertRaisesRegex(IntakeHold, "portal tab is missing or ambiguous"):
            open_finys_from_portal(portal)

    def test_finys_url_guard(self):
        page = FakeFinysPage()
        page.url = "https://fos.finys.com/login.aspx"
        with self.assertRaisesRegex(IntakeHold, "not authenticated"):
            extract_pending_items(page)
        page.url = "https://evil.example/tasks"
        with self.assertRaisesRegex(IntakeHold, "Finys URL is missing or ambiguous"):
            extract_pending_items(page)


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.nav = patch.object(fos, "_NAV_TIMEOUT_MS", 300)
        self.nav.start()
        self.addCleanup(self.nav.stop)
        self.output = Path(self.tmp.name) / "pull"

    def _run(self, page):
        browser = FinysFoSBrowser(page)
        ledger = LocalDeliveryLedger(self.output)
        archive = SourceArchive(self.output / "sources")
        return run_pull(browser, ledger, archive, as_of=AS_OF), ledger

    def test_pull_downloads_target_notices(self):
        token = pdf_bytes(b"honj-notice")
        page = FakeFinysPage(
            pending_rows=PENDING_ROWS[:2],
            docs_by_policy={
                HONJ: _docs_for(HONJ, token),
                HODJ: _docs_for(HODJ, pdf_bytes(b"hodj-notice")),
            },
        )
        receipt, _ = self._run(page)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 2)
        self.assertEqual(receipt["held"], [])
        self.assertEqual(receipt["skipped"], [])
        names = {item["filename"] for item in receipt["downloaded"]}
        self.assertEqual(
            names,
            {
                "HONJ038633 Intent to Cancel Notice FarmersofSalem.pdf",
                "HODJ024421 Intent to Cancel Notice FarmersofSalem.pdf",
            },
        )
        saved = self.output / "2026-09-28" / "HONJ038633 Intent to Cancel Notice FarmersofSalem.pdf"
        self.assertEqual(saved.read_bytes(), token)
        shot = self.output / AS_OF.isoformat() / f"pending-items-{AS_OF.isoformat()}.png"
        self.assertEqual(shot.read_bytes(), LIST_PNG)
        # Ledger recorded both documents.
        ledger_data = json.loads((self.output / "farmersofsalem-noc-ledger.json").read_text())
        self.assertEqual(len(ledger_data["items"]), 2)

    def test_dedup_second_pull_skipped(self):
        token = pdf_bytes(b"honj-notice")

        def fresh_page():
            return FakeFinysPage(
                pending_rows=PENDING_ROWS[:1],
                docs_by_policy={HONJ: _docs_for(HONJ, token)},
            )

        receipt, _ = self._run(fresh_page())
        self.assertEqual(receipt["count"], 1)
        document_id = "farmersofsalem:HONJ038633:2026-09-28:intent-to-cancel"

        receipt2, _ = self._run(fresh_page())
        self.assertEqual(receipt2["status"], "PULLED")
        self.assertEqual(receipt2["count"], 0)
        self.assertEqual(receipt2["skipped"], [document_id])
        self.assertEqual(receipt2["held"], [])
        ledger_data = json.loads((self.output / "farmersofsalem-noc-ledger.json").read_text())
        self.assertEqual(len(ledger_data["items"]), 1)

    def test_policy_without_target_document_is_held(self):
        page = FakeFinysPage(
            pending_rows=PENDING_ROWS[:1],
            docs_by_policy={HONJ: [("Policy Declarations", "09/01/2026", None), ("Photos", "09/02/2026", None)]},
        )
        receipt, _ = self._run(page)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(len(receipt["held"]), 1)
        self.assertEqual(receipt["held"][0]["policy_number"], HONJ)
        self.assertIn("no dated Intent to Cancel", receipt["held"][0]["reason"])

    def test_non_pdf_download_holds_pull(self):
        page = FakeFinysPage(
            pending_rows=PENDING_ROWS[:1],
            docs_by_policy={HONJ: _docs_for(HONJ, b"<html>not a pdf</html>")},
        )
        with self.assertRaisesRegex(PullHeld, "Document View download is not a PDF"):
            self._run(page)

    def test_non_test_env_blocks_pull(self):
        # farmersofsalem is not in the production filing allowlist, so any
        # non-TEST env holds before any browser work.
        with patch.dict(os.environ, {"ROBIE_ENV": "PROD"}):
            page = FakeFinysPage(pending_rows=PENDING_ROWS[:1])
            browser = FinysFoSBrowser(page)
            ledger = LocalDeliveryLedger(self.output)
            archive = SourceArchive(self.output / "sources")
            with self.assertRaises(IntakeHold):
                run_pull(browser, ledger, archive, as_of=AS_OF)


if __name__ == "__main__":
    unittest.main()
