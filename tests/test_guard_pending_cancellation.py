"""Fixture tests for the Guard Pending Cancellation document pull. No live login."""
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

from robie_job_engine.guard_pending_cancellation import (
    CARRIER_QA_DRIVE_PARENT_ID,
    DEFAULT_OUTPUT_ROOT,
    DRIVE_UPLOAD_UNAVAILABLE,
    PROCESS,
    CancellationRow,
    DocumentOpenObservation,
    GuardDeliveryLedger,
    GuardDocument,
    PlaywrightGuardBrowser,
    _looks_like_eml_notification,
    collect_document_observation,
    is_cancellation_document,
    main,
    parse_cancellations_grid,
    parse_carrier_date,
    parse_document_links,
    parse_document_list,
    read_playwright_pdf_view,
    require_guard_url,
    run_pull,
    scribe_item_id_from_href,
    select_guard_page,
)
from robie_job_engine.intake_core import IntakeHold, SourceArchive, SourceItem


GUARD_HOME = "https://gigezrate.guard.com/agency/home"
GUARD_LIST = "https://gigezrate.guard.com/agency/bookofbusiness/cancellations"
LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
AS_OF = date(2026, 10, 2)

# Live Cancellations tab rows captured 2026-10-02.
LIVE_POLICIES = (
    ("PRAU716089", "Precision Builders", "09/21/2026", "Underwriting"),
    ("JMWC776835", "J&M Construction", "09/06/2026", "Billing"),
    ("NIHO762346", "Nicole Miller", "08/25/2026", "Billing"),
    ("R2WC794141", "IACONO CONTRACTING", "08/02/2026", "Billing"),
    ("CEWC785156", "C. Erb Construction", "07/29/2026", "Audit"),
    ("R2WC722429", "BSJ Contracting", "07/10/2026", "Audit"),
)
CANCEL_HEADERS = ("Policy", "Insured Name", "Cancel Date", "Reason")


def pdf_bytes(mark: bytes) -> bytes:
    return b"%PDF-1.4\n" + mark + b"\n%%EOF\n"


def eml_bytes() -> bytes:
    return (
        b"From: GUARD Automated Delivery System <noreply@guard.com>\r\n"
        b"Subject: Your GUARD Commercial Auto Policy\r\n"
        b"\r\n"
        b"Your document request has been processed and emailed.\r\n"
    )


class ParseTests(unittest.TestCase):
    def test_cancellations_grid_parses_six_live_policies(self):
        rows = tuple(
            (policy, insured, cancel, reason) for policy, insured, cancel, reason in LIVE_POLICIES
        )
        parsed = parse_cancellations_grid(CANCEL_HEADERS, rows, list_url=GUARD_LIST)
        self.assertEqual(len(parsed), 6)
        self.assertEqual(parsed[0].policy_number, "PRAU716089")
        self.assertEqual(parsed[0].insured_name, "Precision Builders")
        self.assertEqual(parsed[0].cancel_date, date(2026, 9, 21))
        self.assertEqual(parsed[0].reason, "Underwriting")
        self.assertEqual(parsed[5].policy_number, "R2WC722429")
        self.assertEqual(parsed[5].cancel_date, date(2026, 7, 10))
        self.assertTrue(all(isinstance(r, CancellationRow) for r in parsed))

    def test_cancellations_grid_header_aliases(self):
        headers = ("Policy Number", "Named Insured", "Cancellation Date", "Type")
        rows = (("PRAU716089", "Precision Builders", "09/21/2026", "Underwriting"),)
        parsed = parse_cancellations_grid(headers, rows, list_url=GUARD_LIST)
        self.assertEqual(parsed[0].policy_number, "PRAU716089")
        self.assertEqual(parsed[0].reason, "Underwriting")

    def test_cancellations_grid_rejects_bad_policy(self):
        rows = (("NOT-A-POLICY", "Someone", "09/21/2026", "Billing"),)
        with self.assertRaises(IntakeHold):
            parse_cancellations_grid(CANCEL_HEADERS, rows, list_url=GUARD_LIST)

    def test_cancellations_grid_rejects_missing_headers(self):
        with self.assertRaises(IntakeHold):
            parse_cancellations_grid(("Policy", "Name"), (("PRAU716089", "X"),), list_url=GUARD_LIST)

    def test_cancellations_grid_rejects_empty(self):
        with self.assertRaises(IntakeHold):
            parse_cancellations_grid(CANCEL_HEADERS, (), list_url=GUARD_LIST)

    def test_policy_number_format(self):
        from robie_job_engine.guard_pending_cancellation import require_policy_number

        self.assertEqual(require_policy_number("PRAU716089"), "PRAU716089")
        self.assertEqual(require_policy_number("prau716089"), "PRAU716089")
        for bad in ("PRAU71608", "PRAU7160891", "PRA716089", "1234567890", ""):
            with self.assertRaises(IntakeHold):
                require_policy_number(bad)

    def test_parse_carrier_date(self):
        self.assertEqual(parse_carrier_date("09/21/2026"), date(2026, 9, 21))
        self.assertEqual(parse_carrier_date("2026-09-21"), date(2026, 9, 21))
        with self.assertRaises(IntakeHold):
            parse_carrier_date("not a date")

    def test_document_list_parses_groups(self):
        groups = (
            (
                "Policy Documents",
                ("Description", "Form", "Issued Date", "Action"),
                (
                    ("Cancellation", "GUARD-CXL", "09/21/2026", "Download"),
                    ("Endorsement", "GUARD-END", "08/01/2026", "Download"),
                    ("Welcome Letter", "GUARD-WL", "07/01/2026", "Download"),
                ),
            ),
            (
                "Miscellaneous Documents",
                ("Description", "Form", "Issued Date", "Action"),
                (
                    ("Premium Billing Statements", "GUARD-BILL", "09/01/2026", "Download"),
                    ("ID Cards", "GUARD-ID", "07/01/2026", "Download"),
                ),
            ),
        )
        docs = parse_document_list(groups, policy_number="PRAU716089")
        self.assertEqual(len(docs), 5)
        self.assertEqual(docs[0].group, "Policy Documents")
        self.assertEqual(docs[0].description, "Cancellation")
        self.assertEqual(docs[0].issued, date(2026, 9, 21))
        self.assertEqual(docs[0].policy_number, "PRAU716089")
        self.assertEqual(docs[3].group, "Miscellaneous Documents")
        self.assertEqual(
            docs[0].document_id, "guard:PRAU716089:2026-09-21:cancellation"
        )
        self.assertEqual(docs[0].filename, "PRAU716089 Cancellation Guard.pdf")

    def test_document_list_rejects_empty(self):
        with self.assertRaises(IntakeHold):
            parse_document_list((), policy_number="PRAU716089")

    def test_is_cancellation_document(self):
        for desc in ("Cancellation", "Notice of Cancellation", "Pending Cancellation Notice"):
            self.assertTrue(is_cancellation_document(desc), desc)
        for desc in ("Endorsement", "Welcome Letter", "Premium Billing Statements", "ID Cards"):
            self.assertFalse(is_cancellation_document(desc), desc)

    def test_require_guard_url(self):
        self.assertEqual(require_guard_url(GUARD_HOME), GUARD_HOME)
        with self.assertRaises(IntakeHold):
            require_guard_url("https://guard.com/agency/home")
        with self.assertRaises(IntakeHold):
            require_guard_url("https://gigezrate.guard.com/auth")
        with self.assertRaises(IntakeHold):
            require_guard_url("http://gigezrate.guard.com/agency/home")


class QuirkTests(unittest.TestCase):
    def test_eml_notification_markers(self):
        self.assertTrue(_looks_like_eml_notification(eml_bytes()))
        self.assertFalse(_looks_like_eml_notification(pdf_bytes(b"x")))

    def test_emailed_document_is_not_claimed(self):
        page = EmailedPage()
        observation = collect_document_observation(page, lambda: None)
        self.assertTrue(observation.emailed)
        self.assertEqual(observation.downloads, ())
        self.assertEqual(observation.viewer_pdfs, ())

    def test_pdf_download_is_captured(self):
        token = pdf_bytes(b"download")
        page = DownloadPage(token)
        observation = collect_document_observation(page, lambda: None)
        self.assertFalse(observation.emailed)
        self.assertEqual(observation.downloads, (token,))

    def test_non_pdf_download_holds(self):
        page = DownloadPage(b"not a pdf at all")
        with self.assertRaisesRegex(IntakeHold, "not a PDF"):
            collect_document_observation(page, lambda: None)

    def test_eml_download_is_treated_as_emailed(self):
        page = DownloadPage(eml_bytes())
        observation = collect_document_observation(page, lambda: None)
        self.assertTrue(observation.emailed)
        self.assertEqual(observation.downloads, ())

    def test_viewer_pdf_is_captured(self):
        token = pdf_bytes(b"viewer")
        page = ViewerPage(token)
        observation = collect_document_observation(page, lambda: None)
        self.assertFalse(observation.emailed)
        self.assertEqual(observation.viewer_pdfs, (token,))

    def test_read_playwright_pdf_view_blob(self):
        token = pdf_bytes(b"blob")
        import base64

        encoded = base64.b64encode(token).decode()

        class BlobPage:
            url = "blob:https://gigezrate.guard.com/view"

            def evaluate(self, script, url):
                self.evaluated = url
                return encoded

        page = BlobPage()
        self.assertEqual(read_playwright_pdf_view(page), (token,))
        self.assertEqual(page.evaluated, page.url)


class FakeNode:
    def __init__(self, role, name="", text=None, children=None, attrs=None):
        self.role = role
        self.name = name
        self.text = name if text is None else text
        self.children = children or []
        self.attrs = attrs or {}
        self._following_table = None

    def find(self, selector):
        if selector.startswith("xpath=") and "following" in selector:
            return [self._following_table] if self._following_table is not None else []
        parts = selector.split()
        if len(parts) == 1:
            found = [self] if self.matches(parts[0]) else []
            for child in self.children:
                found.extend(child.find(selector))
            return found
        found = []
        for node in self.find(parts[0]):
            for child in node.children:
                found.extend(child.find(" ".join(parts[1:])))
        return found

    def _name_ok(self, name, exact):
        if name is None:
            return True
        if exact:
            return self.name == name
        return name in (self.name or "")

    def find_role(self, role, name, exact):
        found = []
        if self.role == role and self._name_ok(name, exact):
            found.append(self)
        for child in self.children:
            found.extend(child.find_role(role, name, exact))
        return found

    def matches(self, selector):
        # Attribute-substring selector, e.g. a[href*="scribeItemId=222"].
        # Mirrors the CSS semantics Playwright uses for the live anchor DOM.
        attr_match = re.match(r'^([a-zA-Z0-9]+)\[href\*\="([^"]*)"\]$', selector)
        if attr_match:
            tag, needle = attr_match.group(1), attr_match.group(2)
            return self.role == tag and needle in (self.attrs.get("href") or "")
        return {
            "table": self.role == "table",
            "thead": self.role == "thead",
            "tbody": self.role == "tbody",
            "th": self.role == "th",
            "td": self.role == "td",
            "tr": self.role == "tr",
        }.get(selector, False)

    def subtree_text(self):
        parts = [self.text or ""]
        for child in self.children:
            parts.append(child.subtree_text())
        return " ".join(p for p in parts if p)


class NodeLocator:
    def __init__(self, nodes, page):
        self.nodes = list(nodes)
        self.page = page

    def count(self):
        return len(self.nodes)

    @property
    def first(self):
        return NodeLocator(self.nodes[:1], self.page)

    def click(self):
        node = self.nodes[0]
        self.page.clicks.append(node.name)
        self.page.on_click(node)

    def inner_text(self, timeout=None):
        # Real inner_text() renders the subtree, including child links.
        return self.nodes[0].subtree_text()

    def get_attribute(self, name):
        return self.nodes[0].attrs.get(name)

    def all(self):
        return [NodeLocator([node], self.page) for node in self.nodes]

    def locator(self, selector):
        found = []
        for node in self.nodes:
            found.extend(node.find(selector))
        return NodeLocator(found, self.page)

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self.nodes:
            found.extend(node.find_role(role, name, exact))
        return NodeLocator(found, self.page)


def _doc_table(doc_rows):
    """Build a fake printable-documents table node from (description, form, issued) rows."""
    header = FakeNode("thead", children=[
        FakeNode("tr", children=[
            FakeNode("th", text="Description"),
            FakeNode("th", text="Form"),
            FakeNode("th", text="Issued Date"),
            FakeNode("th", text="Action"),
        ])
    ])
    body_rows = []
    for description, form, issued in doc_rows:
        action = FakeNode("link", name="Download", text="Download")
        body_rows.append(FakeNode("tr", children=[
            FakeNode("td", text=description),
            FakeNode("td", text=form),
            FakeNode("td", text=issued),
            FakeNode("td", children=[action]),
        ]))
    return FakeNode("table", children=[header, FakeNode("tbody", children=body_rows)])


def _cancellations_table():
    header = FakeNode("thead", children=[
        FakeNode("tr", children=[FakeNode("th", text=h) for h in CANCEL_HEADERS])
    ])
    body_rows = []
    for policy, insured, cancel, reason in LIVE_POLICIES:
        body_rows.append(FakeNode("tr", children=[
            FakeNode("td", children=[FakeNode("link", name=policy, text=policy)]),
            FakeNode("td", text=insured),
            FakeNode("td", text=cancel),
            FakeNode("td", text=reason),
        ]))
    return FakeNode("table", children=[header, FakeNode("tbody", children=body_rows)])


class FakeGuardPage:
    """Scripted Guard Agency Service Center page. No live browser."""

    def __init__(self, *, doc_outcomes=None, doc_rows=None, cancellations_table=True):
        # doc_outcomes: {policy_number: "download" | "emailed" | "viewer" | "nonpdf"}
        self.url = GUARD_HOME
        self.state = "home"
        self.policy = ""
        self.clicks = []
        self.doc_outcomes = doc_outcomes or {}
        self.doc_rows = doc_rows
        self.cancellations_table = cancellations_table
        self._download = None
        self._tmpdir = tempfile.TemporaryDirectory()
        self.listeners = []
        self.context = SimpleNamespace(on=self._on, remove_listener=self._off)

    def _on(self, event, fn):
        if event == "page":
            self.listeners.append(fn)

    def _off(self, event, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    def _docs_for_policy(self):
        if self.doc_rows is not None:
            return self.doc_rows
        return (
            ("Cancellation", "GUARD-CXL", "09/21/2026"),
            ("Endorsement", "GUARD-END", "08/01/2026"),
            ("Welcome Letter", "GUARD-WL", "07/01/2026"),
        )

    def roots(self):
        if self.state == "home":
            return [FakeNode("link", name="Book of Business")]
        if self.state == "book":
            return [FakeNode("tab", name="Cancellations")]
        if self.state == "cancellations":
            return [_cancellations_table()] if self.cancellations_table else []
        if self.state == "policy":
            return [
                FakeNode("heading", name="Policy Center"),
                FakeNode("link", name="printable documents"),
            ]
        if self.state == "documents":
            policy_docs = _doc_table(self._docs_for_policy())
            misc_docs = _doc_table((
                ("Premium Billing Statements", "GUARD-BILL", "09/01/2026"),
                ("ID Cards", "GUARD-ID", "07/01/2026"),
            ))
            policy_heading = FakeNode("heading", name="Policy Documents")
            policy_heading._following_table = policy_docs
            misc_heading = FakeNode("heading", name="Miscellaneous Documents")
            misc_heading._following_table = misc_docs
            return [policy_heading, policy_docs, misc_heading, misc_docs]
        return []

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self.roots():
            found.extend(node.find_role(role, name, exact))
        return NodeLocator(found, self)

    def locator(self, selector, has_text=None):
        found = []
        for node in self.roots():
            found.extend(node.find(selector))
        if has_text is not None:
            found = [n for n in found if has_text in n.subtree_text()]
        return NodeLocator(found, self)

    def wait_for_selector(self, selector, timeout=None):
        return None

    def screenshot(self, full_page=True, type="png", timeout=None):
        return LIST_PNG

    def goto(self, url, wait_until=None):
        self.url = url
        self.state = "cancellations"

    def expect_download(self, timeout=None):
        return _FakeExpect(self)

    def on_click(self, node):
        if node.name == "Book of Business" and self.state == "home":
            self.state = "book"
            return
        if node.name == "Cancellations" and self.state == "book":
            self.state = "cancellations"
            self.url = GUARD_LIST
            return
        if node.role == "link" and self.state == "cancellations":
            self.policy = node.name
            self.state = "policy"
            self.url = f"https://gigezrate.guard.com/agency/policy/{node.name}"
            return
        if node.name == "printable documents" and self.state == "policy":
            self.state = "documents"
            self.url = f"https://gigezrate.guard.com/agency/policy/{self.policy}/documents"
            return
        if node.name == "Download" and self.state == "documents":
            # Fresh download context per click, like Playwright's expect_download.
            self._download = None
            self._fire_doc_outcome()
            return

    def _fire_doc_outcome(self):
        outcome = self.doc_outcomes.get(self.policy, "download")
        if outcome == "download":
            path = Path(self._tmpdir.name) / f"{self.policy}.pdf"
            path.write_bytes(pdf_bytes(self.policy.encode()))
            self._download = SimpleNamespace(path=lambda: str(path))
        elif outcome == "nonpdf":
            path = Path(self._tmpdir.name) / f"{self.policy}.bin"
            path.write_bytes(b"not a pdf")
            self._download = SimpleNamespace(path=lambda: str(path))
        elif outcome == "viewer":
            token = pdf_bytes(b"viewer-" + self.policy.encode())
            viewer = SimpleNamespace(
                url=f"https://gigezrate.guard.com/viewer/{self.policy}.pdf",
                context=SimpleNamespace(
                    request=SimpleNamespace(
                        get=lambda url, timeout=None: SimpleNamespace(body=lambda: token)
                    )
                ),
            )
            for fn in list(self.listeners):
                fn(viewer)
        # "emailed": no download, no viewer — Guard emailed the document.


class _FakeExpect:
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


class EmailedPage:
    """Minimal page whose document actions never produce a download."""

    def __init__(self):
        self.listeners = []
        self.context = SimpleNamespace(on=self._on, remove_listener=self._off)
        self._download = None

    def _on(self, event, fn):
        self.listeners.append(fn)

    def _off(self, event, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    def expect_download(self, timeout=None):
        return _FakeExpect(self)


class DownloadPage(EmailedPage):
    def __init__(self, token: bytes):
        super().__init__()
        self._tmpdir = tempfile.TemporaryDirectory()
        path = Path(self._tmpdir.name) / "doc.bin"
        path.write_bytes(token)
        self._download = SimpleNamespace(path=lambda: str(path))


class ViewerPage(EmailedPage):
    def __init__(self, token: bytes):
        super().__init__()
        viewer = SimpleNamespace(
            url="https://gigezrate.guard.com/viewer/doc.pdf",
            context=SimpleNamespace(
                request=SimpleNamespace(
                    get=lambda url, timeout=None: SimpleNamespace(body=lambda: token)
                )
            ),
        )
        self._viewer = viewer

    def _on(self, event, fn):
        super()._on(event, fn)
        if event == "page":
            fn(self._viewer)


class LiveAnchorGuardPage:
    """Fake of the LIVE printable-documents DOM: flat document anchors.

    Anchors carry real hrefs like
    /dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem
    ?scribeItemId=...&Download=true. Two anchors may share IDENTICAL visible
    text with DIFFERENT scribeItemIds — the regression fixture.
    """

    def __init__(self, anchors):
        # anchors: list of (visible text, href)
        self.url = (
            "https://gigezrate.guard.com/dotNet/mvc/workflow/PrintableDocuments"
            "?linkid=356&MGACODE=PRAU716089&TABLABEL=undefined"
        )
        self.anchors = [
            FakeNode("a", text=text, attrs={"href": href}) for text, href in anchors
        ]
        self.clicked_hrefs = []
        self.clicks = []
        self._download = None
        self._tmpdir = tempfile.TemporaryDirectory()
        self.listeners = []
        self.context = SimpleNamespace(on=self._on, remove_listener=self._off)

    def _on(self, event, fn):
        if event == "page":
            self.listeners.append(fn)

    def _off(self, event, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    def locator(self, selector, has_text=None):
        found = [node for node in self.anchors if node.matches(selector)]
        return NodeLocator(found, self)

    def expect_download(self, timeout=None):
        return _FakeExpect(self)

    def on_click(self, node):
        href = node.attrs.get("href") or ""
        self.clicked_hrefs.append(href)
        scribe = scribe_item_id_from_href(href)
        path = Path(self._tmpdir.name) / f"scribe-{scribe or 'none'}.pdf"
        path.write_bytes(pdf_bytes(f"scribe-{scribe}".encode()))
        self._download = SimpleNamespace(path=lambda: str(path))


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"

    def _run(self, page):
        browser = PlaywrightGuardBrowser(page)
        ledger = GuardDeliveryLedger(self.output)
        archive = SourceArchive(self.output / "sources")
        return run_pull(browser, ledger, archive, as_of=AS_OF)

    def test_full_pull_downloads_six_cancellation_pdfs(self):
        page = FakeGuardPage()
        receipt = self._run(page)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["process"], "guard")
        self.assertEqual(receipt["cancellation_count"], 6)
        self.assertEqual(receipt["count"], 6)
        self.assertEqual(receipt["held"], [])
        self.assertEqual(receipt["ezlynx"], "not_run")
        downloaded = receipt["downloaded"]
        self.assertEqual(
            [d["policy_number"] for d in downloaded],
            [p for p, _, _, _ in LIVE_POLICIES],
        )
        for item in downloaded:
            path = Path(item["path"])
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_bytes().startswith(b"%PDF"))
            self.assertEqual(item["description"], "Cancellation")
        # QA screenshot of the Cancellations tab was saved.
        shots = list((self.output / AS_OF.isoformat()).glob("guard-cancellations-*.png"))
        self.assertEqual(len(shots), 1)
        self.assertTrue(shots[0].read_bytes().startswith(b"\x89PNG"))
        # Sources were archived.
        archived = list((self.output / "sources").glob("*.source"))
        self.assertEqual(len(archived), 6)

    def test_emailed_documents_are_held_never_claimed(self):
        page = FakeGuardPage(doc_outcomes={p: "emailed" for p, _, _, _ in LIVE_POLICIES})
        receipt = self._run(page)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(len(receipt["held"]), 6)
        for held in receipt["held"]:
            self.assertEqual(held["outcome"], "HELD")
            self.assertIn("Guard emails this document type", held["hold_reason"])
            self.assertIn("not claimed as downloaded", held["hold_reason"])
        self.assertEqual(receipt["downloaded"], [])
        # No PDFs were written for emailed documents.
        pdfs = list((self.output / AS_OF.isoformat()).glob("*.pdf"))
        self.assertEqual(pdfs, [])

    def test_mixed_download_and_emailed(self):
        outcomes = {
            "PRAU716089": "download",
            "JMWC776835": "emailed",
            "NIHO762346": "download",
            "R2WC794141": "emailed",
            "CEWC785156": "download",
            "R2WC722429": "emailed",
        }
        page = FakeGuardPage(doc_outcomes=outcomes)
        receipt = self._run(page)
        self.assertEqual(receipt["count"], 3)
        self.assertEqual(len(receipt["held"]), 3)
        self.assertEqual(
            sorted(d["policy_number"] for d in receipt["downloaded"]),
            ["CEWC785156", "NIHO762346", "PRAU716089"],
        )
        self.assertEqual(
            sorted(h["policy_number"] for h in receipt["held"]),
            ["JMWC776835", "R2WC722429", "R2WC794141"],
        )

    def test_viewer_pdf_is_captured(self):
        page = FakeGuardPage(doc_outcomes={"PRAU716089": "viewer"})
        receipt = self._run(page)
        # All six policies ran; only PRAU716089 used the viewer.
        by_policy = {d["policy_number"]: d for d in receipt["downloaded"]}
        self.assertIn("PRAU716089", by_policy)
        content = Path(by_policy["PRAU716089"]["path"]).read_bytes()
        self.assertIn(b"viewer-PRAU716089", content)

    def test_rerun_skips_already_delivered(self):
        page = FakeGuardPage()
        first = self._run(page)
        self.assertEqual(first["count"], 6)
        page2 = FakeGuardPage()
        second = self._run(page2)
        self.assertEqual(second["count"], 0)
        self.assertEqual(len(second["skipped_already_delivered"]), 6)
        self.assertEqual(second["held"], [])
        outcomes = [r["outcome"] for r in second["rows"]]
        self.assertTrue(all(o == "ALREADY_DELIVERED" for o in outcomes))

    def test_missing_cancellations_table_holds(self):
        page = FakeGuardPage(cancellations_table=False)
        with self.assertRaisesRegex(IntakeHold, "Cancellations table is missing or ambiguous"):
            self._run(page)

    def test_missing_cancellation_document_holds_policy(self):
        rows = (
            ("Endorsement", "GUARD-END", "08/01/2026"),
            ("Welcome Letter", "GUARD-WL", "07/01/2026"),
        )
        page = FakeGuardPage(doc_rows=rows)
        receipt = self._run(page)
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(len(receipt["held"]), 6)
        for held in receipt["held"]:
            self.assertIn("missing or ambiguous", held["hold_reason"])

    def test_ambiguous_cancellation_documents_hold_policy(self):
        rows = (
            ("Cancellation", "GUARD-CXL", "09/21/2026"),
            ("Cancellation Notice", "GUARD-CXL2", "09/20/2026"),
        )
        page = FakeGuardPage(doc_rows=rows)
        receipt = self._run(page)
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(len(receipt["held"]), 6)
        for held in receipt["held"]:
            self.assertIn("found 2", held["hold_reason"])
            # The hold names the colliding documents (disambiguation follow-up).
            self.assertIn("Cancellation [", held["hold_reason"])
            self.assertIn("Cancellation Notice [", held["hold_reason"])

    def test_non_pdf_download_holds(self):
        page = FakeGuardPage(doc_outcomes={"PRAU716089": "nonpdf"})
        with self.assertRaisesRegex(IntakeHold, "not a PDF"):
            self._run(page)

    def test_select_guard_page(self):
        good = SimpleNamespace(url="https://gigezrate.guard.com/agency/home")
        other = SimpleNamespace(url="https://example.com/")
        self.assertIs(select_guard_page([good]), good)
        with self.assertRaises(IntakeHold):
            select_guard_page([])
        with self.assertRaises(IntakeHold):
            select_guard_page([good, good])
        with self.assertRaises(IntakeHold):
            select_guard_page([other])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = GuardDeliveryLedger(Path(self.tmp.name) / "ledger")

    def _source(self, document_id, filename, content):
        return SourceItem(
            system=PROCESS,
            source_account="gigezrate.guard.com",
            source_id=document_id,
            source_url="https://gigezrate.guard.com/agency/policy/PRAU716089",
            received_at="2026-10-02T00:00:00+00:00",
            filename=filename,
            content=content,
        )

    def test_record_then_delivery_status(self):
        document_id = "guard:PRAU716089:2026-09-21:cancellation"
        filename = "PRAU716089 Cancellation Guard.pdf"
        source = self._source(document_id, filename, pdf_bytes(b"one"))
        self.assertFalse(
            self.ledger.delivery_status(
                document_id=document_id, filename=filename, issued_on=date(2026, 9, 21)
            )
        )
        saved = self.ledger.record(source, issued_on=date(2026, 9, 21))
        self.assertTrue(saved.is_file())
        self.assertTrue(
            self.ledger.delivery_status(
                document_id=document_id, filename=filename, issued_on=date(2026, 9, 21)
            )
        )

    def test_conflicting_file_holds(self):
        document_id = "guard:PRAU716089:2026-09-21:cancellation"
        filename = "PRAU716089 Cancellation Guard.pdf"
        source = self._source(document_id, filename, pdf_bytes(b"one"))
        self.ledger.record(source, issued_on=date(2026, 9, 21))
        # Corrupt the file on disk: ledger entry no longer matches.
        path = self.ledger.pdf_path(date(2026, 9, 21), filename)
        path.write_bytes(pdf_bytes(b"tampered"))
        with self.assertRaisesRegex(IntakeHold, "conflicts with the pull ledger"):
            self.ledger.delivery_status(
                document_id=document_id, filename=filename, issued_on=date(2026, 9, 21)
            )

    def test_output_directory_is_private(self):
        self.ledger.ensure_private()
        mode = self.ledger.root.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)


class SafetyTests(unittest.TestCase):
    def test_module_does_not_type_credentials_or_call_ezlynx(self):
        text = Path("robie_job_engine/guard_pending_cancellation.py").read_text(encoding="utf-8")
        for banned in (
            "upload_applicant_document",
            "add_note_to_discussion",
            "create_task_once",
            "DocumentApi",
            "DiscussionApi",
            "Status Sheet",
            ".fill(",
            "keyboard",
            "systemd",
            "googleapis",
            "googleapiclient",
            "MediaFileUpload",
            "google.auth",
        ):
            self.assertNotIn(banned, text)
        # "password" appears only in URL-credential refusal checks
        # (parsed.password), mirroring the GEICO module — no credential handling.
        self.assertNotIn("otp", text.casefold())
        # MFA is mentioned only as something the module does NOT automate.

    def test_drive_parent_matches_shared_qa_folder(self):
        self.assertEqual(CARRIER_QA_DRIVE_PARENT_ID, "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2")
        self.assertIn("not available", DRIVE_UPLOAD_UNAVAILABLE)

    def test_qa_pack_dir_is_private(self):
        from robie_job_engine.guard_pending_cancellation import qa_pack_dir

        with tempfile.TemporaryDirectory() as tmp:
            folder = qa_pack_dir(Path(tmp), AS_OF)
            self.assertEqual(folder.name, AS_OF.isoformat())
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)


class DuplicateScribeIdTests(unittest.TestCase):
    """Regression: identical display texts can be DISTINCT documents.

    Live DOM inspection (2026-10-05) found document anchors with identical
    visible text but different scribeItemId values. open_document() used to
    locate by description text, which could open the WRONG document. It now
    selects by the durable scribeItemId preserved on GuardDocument.
    """

    HREF_111 = (
        "/dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem"
        "?scribeItemId=111&Download=true"
    )
    HREF_222 = (
        "/dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem"
        "?scribeItemId=222&Download=true"
    )

    def _doc(self, scribe_id, href):
        return GuardDocument(
            group="Policy Documents",
            description="Cancellation",
            form="Cancellation",
            issued=date(2026, 9, 21),
            policy_number="PRAU716089",
            href=href,
            scribe_item_id=scribe_id,
        )

    def _dup_anchors(self):
        # Two anchors, IDENTICAL visible text, DIFFERENT scribeItemIds.
        return [
            ("Cancellation", self.HREF_111),
            ("Cancellation", self.HREF_222),
        ]

    def test_scribe_item_id_extracted_from_href(self):
        self.assertEqual(scribe_item_id_from_href(self.HREF_222), "222")
        self.assertEqual(
            scribe_item_id_from_href(
                "https://gigezrate.guard.com/dotNet/mvc/workflow/PrintableDocuments"
            ),
            "",
        )
        self.assertEqual(scribe_item_id_from_href(""), "")

    def test_parse_document_links_preserves_distinct_ids(self):
        docs = parse_document_links(
            (
                ("Policy Documents", "Cancellation", self.HREF_111),
                ("Policy Documents", "Cancellation", self.HREF_222),
            ),
            policy_number="PRAU716089",
        )
        self.assertEqual(len(docs), 2)
        self.assertEqual([d.scribe_item_id for d in docs], ["111", "222"])
        self.assertEqual([d.href for d in docs], [self.HREF_111, self.HREF_222])
        # The durable ledger key must differ: these are two documents.
        self.assertNotEqual(docs[0].document_id, docs[1].document_id)
        self.assertIn("scribe-111", docs[0].document_id)
        self.assertIn("scribe-222", docs[1].document_id)

    def test_document_id_unchanged_without_scribe_id(self):
        doc = GuardDocument(
            group="Policy Documents",
            description="Cancellation",
            form="GUARD-CXL",
            issued=date(2026, 9, 21),
            policy_number="PRAU716089",
        )
        self.assertEqual(doc.document_id, "guard:PRAU716089:2026-09-21:cancellation")

    def test_open_document_selects_second_duplicate_by_scribe_id(self):
        # The whole point: description text alone matches BOTH anchors.
        # open_document must click the one carrying scribeItemId=222.
        page = LiveAnchorGuardPage(self._dup_anchors())
        browser = PlaywrightGuardBrowser(page)
        observation = browser.open_document(self._doc("222", self.HREF_222))
        self.assertEqual(page.clicked_hrefs, [self.HREF_222])
        self.assertFalse(observation.emailed)
        self.assertEqual(len(observation.downloads), 1)
        self.assertIn(b"scribe-222", observation.downloads[0])

    def test_open_document_selects_first_duplicate_by_scribe_id(self):
        page = LiveAnchorGuardPage(self._dup_anchors())
        browser = PlaywrightGuardBrowser(page)
        observation = browser.open_document(self._doc("111", self.HREF_111))
        self.assertEqual(page.clicked_hrefs, [self.HREF_111])
        self.assertEqual(len(observation.downloads), 1)
        self.assertIn(b"scribe-111", observation.downloads[0])

    def test_open_document_duplicate_scribe_id_holds(self):
        # Two anchors carrying the SAME scribeItemId: ambiguous.
        # Fail closed — nothing is clicked, no document is claimed.
        page = LiveAnchorGuardPage(
            [
                ("Cancellation", self.HREF_222),
                ("Cancellation Notice", self.HREF_222),
            ]
        )
        browser = PlaywrightGuardBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
            browser.open_document(self._doc("222", self.HREF_222))
        self.assertEqual(page.clicked_hrefs, [])

    def test_open_document_missing_scribe_id_holds(self):
        page = LiveAnchorGuardPage(self._dup_anchors())
        browser = PlaywrightGuardBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
            browser.open_document(
                self._doc(
                    "999",
                    "/dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem"
                    "?scribeItemId=999&Download=true",
                )
            )
        self.assertEqual(page.clicked_hrefs, [])


if __name__ == "__main__":
    unittest.main()
