"""Fixture tests for the Geico Pending Cancellation NOC pull. No live login."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.geico_pending_cancellation_noc import (
    CARRIER_QA_DRIVE_PARENT_ID,
    DEFAULT_OUTPUT_ROOT,
    DRIVE_UPLOAD_UNAVAILABLE,
    GEICO_QA_DRIVE_FOLDER_ID,
    PROCESS,
    AlertGrid,
    LocalDeliveryLedger,
    NoticeOpenObservation,
    NoticePath,
    PagePdfView,
    PlaywrightGeicoNocBrowser,
    classify_line,
    classify_policy_documents,
    collect_notice_observation,
    extract_document_id,
    extract_viewer_document_id,
    fetch_notice_pdf_via_viewer,
    is_consolidated_viewer_url,
    main,
    noc_document_id,
    noc_filename,
    parse_alert_grid,
    pdf_bytes_from_observation,
    qa_pack_dir,
    read_playwright_pdf_view,
    require_hermes_test_host,
    require_loopback_cdp,
    require_noc_pdf_parity,
    run_pull,
    select_gateway_page,
)
from robie_job_engine.intake_core import IntakeHold, SourceArchive
from robie_job_engine.locator_registry import LocatorRegistry
from robie_job_engine.playwright_write_guard import locator_is_positional_guess


LIST_URL = "https://gateway2.geico.com/client-alerts"
HOME_URL = "https://gateway2.geico.com/home"
LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
AS_OF = date(2026, 9, 26)
HEADERS = (
    "Severity",
    "Policy Number",
    "Insured Name",
    "Product",
    "State",
    "Cancel Date",
    "Premium",
    "Due Date",
    "Phone",
    "Email",
    "Action",
)
COMMERCIAL = "9300116248"
GUEVARA = "6253395526"
PANELLA = "6260043796"
EXTRA = "6111111111"
PERSONAL_NAMES = (
    "6253395526 NOC Geico.pdf",
    "6260043796 NOC Geico.pdf",
)


def pdf_bytes(mark: bytes) -> bytes:
    return b"%PDF-1.4\n" + mark + b"\n%%EOF\n"


def row(policy: str, insured: str, product: str, due: str, *, severity: str = "High", cancel: str = "9/1/2026") -> tuple[str, ...]:
    return (severity, policy, insured, product, "LA", cancel, "", due, "", "", "Open Alert")


PROVE_ROWS = (
    row(COMMERCIAL, "BYOND TRANSPORTATION LLC", "Commercial Auto", "9/21/2026", cancel="9/11/2026"),
    row(GUEVARA, "Charlemagne Guevara", "Private Passenger Auto", "10/6/2026"),
    row(PANELLA, "TIMOTHY PANELLA", "Private Passenger Auto", "9/26/2026"),
)


def prove_grid(*, extra=(), more_pages=False, headers=HEADERS) -> AlertGrid:
    return AlertGrid(LIST_URL, headers, PROVE_ROWS + tuple(extra), more_pages)


def _test_doc_uuid(policy_number: str) -> str:
    return f"00000000-0000-4000-8000-{int(policy_number):012d}"


def prove_paths(**overrides) -> dict[str, NoticePath]:
    paths = {
        COMMERCIAL: NoticePath("billing_only"),
        GUEVARA: NoticePath("noc", "Pending Cancellation Notice", document_id=_test_doc_uuid(GUEVARA)),
        PANELLA: NoticePath("noc", "CANCELLATION NOTICE", document_id=_test_doc_uuid(PANELLA)),
    }
    paths.update(overrides)
    return paths


class ScriptedBrowser:
    def __init__(self, grid: AlertGrid, paths: dict[str, NoticePath], pdfs: dict[str, bytes]):
        self.grid = grid
        self.paths = paths
        self.pdfs = pdfs
        self.loads = 0
        self.shots = 0
        self.screenshot_bytes = LIST_PNG
        self.inspections: list[str] = []
        self.downloads: list[str] = []
        self.returns = 0

    def load_pending_cancellations(self):
        self.loads += 1
        return self.grid

    def screenshot_pending_cancellations(self):
        self.shots += 1
        return self.screenshot_bytes

    def inspect_notice_path(self, policy_number):
        self.inspections.append(policy_number)
        return self.paths[policy_number]

    def download_notice(self, policy_number):
        self.downloads.append(policy_number)
        blob = self.pdfs[policy_number]
        if not blob:
            raise IntakeHold("NOC PDF capture is missing or ambiguous")
        # Return (document_id, pdf_bytes) per the direct-fetch contract.
        return _test_doc_uuid(policy_number), blob

    def return_to_pending_list(self):
        self.returns += 1


class ParseTests(unittest.TestCase):
    def test_prove_rows_name_personal_pdfs_and_hold_commercial_shape(self):
        alerts = parse_alert_grid(prove_grid())
        self.assertEqual([alert.policy_number for alert in alerts], [COMMERCIAL, GUEVARA, PANELLA])
        self.assertEqual(alerts[0].line, "commercial")
        self.assertEqual(alerts[0].due_on, date(2026, 9, 21))
        self.assertEqual(alerts[0].status, "High")
        self.assertEqual(alerts[1].filename, PERSONAL_NAMES[0])
        self.assertEqual(alerts[1].due_on, date(2026, 10, 6))
        self.assertEqual(alerts[2].filename, PERSONAL_NAMES[1])
        self.assertEqual(alerts[2].due_on, AS_OF)
        self.assertNotEqual(alerts[0].due_on.isoformat(), "2026-09-11")

    def test_another_personal_policy_is_not_a_hardcoded_prove_list(self):
        extra = (row(EXTRA, "Other Insured", "Private Passenger Auto", "2026-09-26"),)
        alerts = parse_alert_grid(prove_grid(extra=extra))
        self.assertEqual(alerts[-1].policy_number, EXTRA)
        self.assertEqual(alerts[-1].filename, f"{EXTRA} NOC Geico.pdf")
        self.assertEqual(noc_filename(EXTRA), f"{EXTRA} NOC Geico.pdf")

    def test_non_high_alert_is_skipped_not_held(self):
        # hermes-test-01 hand patch (2026-10-07): a Medium/Low alert on the
        # Pending Cancellations list no longer holds the whole run.
        grid = prove_grid(extra=(row("6000000001", "Low Row", "Private Passenger Auto", "9/26/2026", severity="Medium"),))
        policies = [alert.policy_number for alert in parse_alert_grid(grid)]
        self.assertNotIn("6000000001", policies)

    def test_unknown_product_duplicate_and_ragged_rows_hold(self):
        bad = (
            AlertGrid(LIST_URL, HEADERS, (row("6000000001", "Home", "Homeowners", "9/26/2026"),), False),
            prove_grid(extra=(PROVE_ROWS[1],)),
            AlertGrid(LIST_URL, HEADERS, (("9300116248", "only"),), False),
            AlertGrid("http://gateway2.geico.com/client-alerts", HEADERS, (), False),
            AlertGrid(LIST_URL, ("Severity", "Policy Number", "Insured Name", "Product"), (), False),
        )
        for grid in bad:
            with self.subTest(rows=grid.rows[:1]):
                with self.assertRaises(IntakeHold):
                    parse_alert_grid(grid)

    def test_line_and_filename_are_exact(self):
        self.assertEqual(classify_line("Private Passenger Auto"), "personal")
        self.assertEqual(classify_line("commercial auto"), "commercial")
        with self.assertRaises(IntakeHold):
            classify_line("Commercial Property")
        with self.assertRaises(IntakeHold):
            noc_filename("123")
        with self.assertRaises(IntakeHold):
            noc_filename("../9300116248")


class ViewerUrlTests(unittest.TestCase):
    """UUID discovery from the consolidated-viewer URL.

    Live 2026-10-05: the notice link href is "#" (Angular). Clicking opens:
      /documents/consolidated-document-viewer?documentId={uuid}&token=...&visitAppId=E01&convToken=
    The UUID in that URL is the durable document identity.
    """

    VIEWER = (
        "https://edgeextended.geico.com/documents/consolidated-document-viewer"
        "?documentId=eaf9b197-7471-3c36-2f23-3ed54027b6d6"
        "&token=abc123&visitAppId=E01&convToken="
    )

    def test_extract_viewer_document_id(self):
        self.assertEqual(
            extract_viewer_document_id(self.VIEWER),
            "eaf9b197-7471-3c36-2f23-3ed54027b6d6",
        )

    def test_extract_viewer_document_id_case_insensitive(self):
        url = (
            "https://edgeextended.geico.com/documents/consolidated-document-viewer"
            "?documentId=EAF9B197-7471-3C36-2F23-3ED54027B6D6&token=x&visitAppId=E01&convToken="
        )
        self.assertEqual(
            extract_viewer_document_id(url),
            "eaf9b197-7471-3c36-2f23-3ed54027b6d6",
        )

    def test_is_consolidated_viewer_url(self):
        self.assertTrue(is_consolidated_viewer_url(self.VIEWER))
        self.assertFalse(is_consolidated_viewer_url("https://edgeextended.geico.com/view-document?documentId=eaf9b197-7471-3c36-2f23-3ed54027b6d6"))
        self.assertFalse(is_consolidated_viewer_url("https://gateway2.geico.com/client-alerts"))
        self.assertFalse(is_consolidated_viewer_url(""))
        self.assertFalse(is_consolidated_viewer_url("http://edgeextended.geico.com/documents/consolidated-document-viewer?documentId=eaf9b197-7471-3c36-2f23-3ed54027b6d6"))

    def test_extract_viewer_document_id_wrong_path_holds(self):
        with self.assertRaisesRegex(IntakeHold, "viewer URL"):
            extract_viewer_document_id("https://edgeextended.geico.com/view-document?documentId=eaf9b197-7471-3c36-2f23-3ed54027b6d6")
        with self.assertRaisesRegex(IntakeHold, "viewer URL"):
            extract_viewer_document_id("https://gateway2.geico.com/client-alerts")

    def test_extract_viewer_document_id_missing_holds(self):
        with self.assertRaisesRegex(IntakeHold, "document ID"):
            extract_viewer_document_id(
                "https://edgeextended.geico.com/documents/consolidated-document-viewer?token=x&visitAppId=E01"
            )
        with self.assertRaisesRegex(IntakeHold, "viewer URL"):
            extract_viewer_document_id("")

    def test_extract_viewer_document_id_malformed_holds(self):
        with self.assertRaisesRegex(IntakeHold, "document ID"):
            extract_viewer_document_id(
                "https://edgeextended.geico.com/documents/consolidated-document-viewer?documentId=not-a-uuid"
            )

    def test_extract_document_id_still_works_for_viewer_url(self):
        # The generic extractor is reused by the viewer-specific one.
        url = "https://edgeextended.geico.com/documents/consolidated-document-viewer?documentId=eaf9b197-7471-3c36-2f23-3ed54027b6d6"
        self.assertEqual(extract_document_id(url), "eaf9b197-7471-3c36-2f23-3ed54027b6d6")

    def test_extract_document_id_missing_holds(self):
        with self.assertRaisesRegex(IntakeHold, "document ID"):
            extract_document_id("https://edgeextended.geico.com/view-document")
        with self.assertRaisesRegex(IntakeHold, "document ID"):
            extract_document_id("")

    def test_extract_document_id_malformed_holds(self):
        with self.assertRaisesRegex(IntakeHold, "document ID"):
            extract_document_id("https://edgeextended.geico.com/view?documentId=not-a-uuid")
        with self.assertRaisesRegex(IntakeHold, "document ID"):
            extract_document_id("https://edgeextended.geico.com/view?documentId=123")

    def test_noc_document_id_with_uuid(self):
        did = noc_document_id("6253395526", date(2026, 10, 6), "eaf9b197-7471-3c36-2f23-3ed54027b6d6")
        self.assertEqual(did, "geico-noc:6253395526:2026-10-06:doc-eaf9b197-7471-3c36-2f23-3ed54027b6d6")

    def test_noc_document_id_without_uuid_unchanged(self):
        did = noc_document_id("6253395526", date(2026, 10, 6))
        self.assertEqual(did, "geico-noc:6253395526:2026-10-06")

    def test_noc_document_id_bad_uuid_holds(self):
        with self.assertRaisesRegex(IntakeHold, "document ID"):
            noc_document_id("6253395526", date(2026, 10, 6), "not-a-uuid")


class NetworkInterceptionTests(unittest.TestCase):
    """The PDF XHR response handler: capture PDF, ignore everything else.

    Live 2026-10-05: the viewer URL returns an HTML shell (Angular + PDF.js).
    The PDF bytes arrive via XHR. The handler must capture only real PDFs and
    fail closed (via the caller) when nothing is captured.
    """

    def _handler(self):
        import robie_job_engine.geico_pending_cancellation_noc as mod
        captured: list[bytes] = []
        return captured, mod._pdf_response_handler(captured)

    def _response(self, *, url="", content_type="", body=b""):
        return SimpleNamespace(url=url, headers={"content-type": content_type}, body=lambda: body)

    def test_pdf_xhr_captured(self):
        captured, on_response = self._handler()
        token = pdf_bytes(b"xhr-pdf")
        on_response(self._response(
            url="https://edgeextended.geico.com/api/documents/stream?id=123",
            content_type="application/pdf",
            body=token,
        ))
        self.assertEqual(captured, [token])

    def test_pdf_url_captured_without_content_type(self):
        captured, on_response = self._handler()
        token = pdf_bytes(b"url-pdf")
        on_response(self._response(
            url="https://edgeextended.geico.com/documents/file.pdf?token=x",
            content_type="text/html",
            body=token,
        ))
        self.assertEqual(captured, [token])

    def test_html_shell_ignored(self):
        """The viewer HTML shell must NOT be captured as a PDF."""
        captured, on_response = self._handler()
        on_response(self._response(
            url="https://edgeextended.geico.com/documents/consolidated-document-viewer?documentId=abc",
            content_type="text/html",
            body=b"<html><body>Angular app</body></html>",
        ))
        self.assertEqual(captured, [])

    def test_non_pdf_body_ignored(self):
        captured, on_response = self._handler()
        on_response(self._response(
            url="https://edgeextended.geico.com/api/data",
            content_type="application/pdf",
            body=b"not actually a pdf",
        ))
        self.assertEqual(captured, [])

    def test_json_xhr_ignored(self):
        captured, on_response = self._handler()
        on_response(self._response(
            url="https://edgeextended.geico.com/api/documents/meta",
            content_type="application/json",
            body=b'{"documentId": "abc"}',
        ))
        self.assertEqual(captured, [])

    def test_multiple_pdfs_all_captured(self):
        """The caller holds on multiple captures (ambiguous)."""
        captured, on_response = self._handler()
        first = pdf_bytes(b"one")
        second = pdf_bytes(b"two")
        on_response(self._response(url="https://x/y.pdf", content_type="application/pdf", body=first))
        on_response(self._response(url="https://x/z.pdf", content_type="application/pdf", body=second))
        self.assertEqual(captured, [first, second])

    def test_broken_response_ignored(self):
        captured, on_response = self._handler()
        on_response(SimpleNamespace())  # no url/headers/body
        on_response(None)
        self.assertEqual(captured, [])


class LedgerInsuredNameTests(unittest.TestCase):
    """record() must persist the insured name so the daily sheet can show it."""

    def test_record_persists_insured_name(self):
        from robie_job_engine.intake_core import SourceItem

        with tempfile.TemporaryDirectory() as tmp:
            ledger = LocalDeliveryLedger(Path(tmp) / "pull")
            source = SourceItem(
                system=PROCESS,
                source_account="gateway.geico.com",
                source_id="geico:6253395526:test-doc",
                source_url="https://gateway.geico.com/#noc=test-doc",
                received_at="2026-10-06T00:00:00+00:00",
                filename="6253395526 Cancel_Notice.pdf",
                content=pdf_bytes(b"test"),
            )
            ledger.record(
                source,
                due_on=date(2026, 10, 6),
                policy_number="6253395526",
                insured_name="Charlemagne Guevara",
            )
            data = json.loads((Path(tmp) / "pull" / "geico-noc-ledger.json").read_text())
        entry = data["items"]["geico:6253395526:test-doc"]
        self.assertEqual(entry["insured_name"], "Charlemagne Guevara")
        self.assertEqual(entry["policy_number"], "6253395526")

    def test_record_defaults_insured_name_blank(self):
        from robie_job_engine.intake_core import SourceItem

        with tempfile.TemporaryDirectory() as tmp:
            ledger = LocalDeliveryLedger(Path(tmp) / "pull")
            source = SourceItem(
                system=PROCESS,
                source_account="gateway.geico.com",
                source_id="geico:6253395526:test-doc2",
                source_url="https://gateway.geico.com/#noc=test-doc2",
                received_at="2026-10-06T00:00:00+00:00",
                filename="6253395526 Cancel_Notice.pdf",
                content=pdf_bytes(b"test"),
            )
            ledger.record(source, due_on=date(2026, 10, 6), policy_number="6253395526")
            data = json.loads((Path(tmp) / "pull" / "geico-noc-ledger.json").read_text())
        self.assertEqual(data["items"]["geico:6253395526:test-doc2"]["insured_name"], "")


class ViewerFlowTests(unittest.TestCase):
    """End-to-end viewer flow with mocked Playwright objects (no live portal).

    Flow: click notice link (href="#") -> viewer opens with documentId in URL
    -> PDF XHR intercepted -> (document_id, pdf_bytes). Fallback: viewer's
    Download button via frames. Fail-closed on every ambiguity.
    """

    DOC_ID = "eaf9b197-7471-3c36-2f23-3ed54027b6d6"
    VIEWER_URL = (
        "https://edgeextended.geico.com/documents/consolidated-document-viewer"
        f"?documentId={DOC_ID}&token=sess123&visitAppId=E01&convToken="
    )

    def _locator(self, *, clicks=None):
        clicks = clicks if clicks is not None else []
        class FakeLocator:
            def get_attribute(self, name):
                return "#" if name == "href" else None
            def click(self, *a, **k):
                clicks.append("clicked")
        return FakeLocator(), clicks

    def _context(self, *, responses=(), pages=()):
        listeners: dict[str, list] = {}
        class FakeContext:
            def on(self, event, handler):
                listeners.setdefault(event, []).append(handler)
            def remove_listener(self, event, handler):
                if handler in listeners.get(event, []):
                    listeners[event].remove(handler)
        ctx = FakeContext()
        # Fire the scripted responses as if the viewer XHR completed.
        for resp in responses:
            for handler in list(listeners.get("response", [])):
                handler(resp)
        return ctx, listeners

    def _page(self, *, url="", context=None, frames=(), download_bytes=None):
        page_url = url
        class FakePage:
            def __init__(self):
                self.url = page_url
                self.context = context
                self._frames = list(frames)
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
            @property
            def frames(self):
                return list(self._frames)
            def get_by_role(self, role, name=None, exact=True):
                raise AssertionError("unexpected get_by_role on page")
        return FakePage()

    def _viewer_page(self, *, context=None, download_bytes=None):
        """A viewer page whose Download button yields download_bytes (or none)."""
        outer = self
        class FakeButton:
            def click(self, *a, **k):
                return None
        class FakeExpectDownload:
            def __init__(self, blob):
                self.blob = blob
            def __enter__(self):
                return SimpleNamespace(value=SimpleNamespace())
            def __exit__(self, *a):
                return False
        # Simpler: the frame returns a button; the page's expect_download
        # is stubbed via _download_bytes patch below.
        class FakeFrame:
            def get_by_role(self, role, name=None, exact=True):
                btn = FakeButton()
                btn.count = lambda: 1
                return btn
        class FakeViewerPage:
            def __init__(self):
                self.url = outer.VIEWER_URL
                self.context = context
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
            @property
            def frames(self):
                return [FakeFrame()]
            def get_by_role(self, role, name=None, exact=True):
                class Empty:
                    def count(self):
                        return 0
                return Empty()
        return FakeViewerPage()

    def _run(self, page, locator, *, patch_notice=True):
        import robie_job_engine.geico_pending_cancellation_noc as mod
        orig = mod._notice_match
        try:
            if patch_notice:
                mod._notice_match = lambda p: ("CANCELLATION NOTICE", locator)
            return fetch_notice_pdf_via_viewer(page)
        finally:
            mod._notice_match = orig

    def test_viewer_flow_captures_pdf_xhr(self):
        """Click -> viewer URL with documentId -> XHR PDF captured."""
        import robie_job_engine.geico_pending_cancellation_noc as mod
        token = pdf_bytes(b"viewer-xhr")
        locator, clicks = self._locator()

        listeners: dict[str, list] = {}
        opened: list = []

        class FakeContext:
            def on(self, event, handler):
                listeners.setdefault(event, []).append(handler)
                if event == "response":
                    # Simulate the viewer's PDF XHR completing after attach.
                    handler(SimpleNamespace(
                        url="https://edgeextended.geico.com/api/doc/stream",
                        headers={"content-type": "application/pdf"},
                        body=lambda: token,
                    ))
            def remove_listener(self, event, handler):
                if handler in listeners.get(event, []):
                    listeners[event].remove(handler)

        class FakeViewer:
            url = ViewerFlowTests.VIEWER_URL
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None

        class FakePage:
            url = "https://gateway2.geico.com/policy"
            def __init__(self):
                self.context = FakeContext()
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None

        page = FakePage()
        # The click opens the viewer in a new tab.
        orig_on = FakeContext.on
        def patched_on(self, event, handler):
            orig_on(self, event, handler)
            if event == "page":
                handler(FakeViewer())
        FakeContext.on = patched_on
        try:
            got_id, got_bytes = self._run(page, locator)
        finally:
            FakeContext.on = orig_on
        self.assertEqual(got_id, self.DOC_ID)
        self.assertEqual(got_bytes, token)
        self.assertEqual(clicks, ["clicked"])
        # Listeners are cleaned up.
        self.assertEqual(listeners.get("response", []), [])
        self.assertEqual(listeners.get("page", []), [])

    def test_viewer_flow_same_tab_navigation(self):
        """The viewer may load in the same tab (no popup)."""
        token = pdf_bytes(b"same-tab")
        locator, _ = self._locator()
        listeners: dict[str, list] = {}

        class FakeContext:
            def on(self, event, handler):
                listeners.setdefault(event, []).append(handler)
                if event == "response":
                    handler(SimpleNamespace(
                        url="https://edgeextended.geico.com/x.pdf",
                        headers={"content-type": "application/pdf"},
                        body=lambda: token,
                    ))
            def remove_listener(self, event, handler):
                if handler in listeners.get(event, []):
                    listeners[event].remove(handler)

        class FakePage:
            def __init__(self):
                self.context = FakeContext()
                self.url = "https://gateway2.geico.com/policy"
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None

        page = FakePage()
        # Simulate same-tab navigation: after click, page.url becomes viewer URL.
        orig_click = locator.click
        def nav_click(*a, **k):
            orig_click(*a, **k)
            page.url = ViewerFlowTests.VIEWER_URL
        locator.click = nav_click

        got_id, got_bytes = self._run(page, locator)
        self.assertEqual(got_id, self.DOC_ID)
        self.assertEqual(got_bytes, token)

    def test_viewer_flow_no_notice_holds(self):
        import robie_job_engine.geico_pending_cancellation_noc as mod
        orig = mod._notice_match
        class FakePage:
            context = SimpleNamespace(on=lambda e, h: None, remove_listener=lambda e, h: None)
        try:
            mod._notice_match = lambda p: None
            with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
                fetch_notice_pdf_via_viewer(FakePage())
        finally:
            mod._notice_match = orig

    def test_viewer_flow_viewer_never_opens_holds(self):
        locator, _ = self._locator()
        listeners: dict[str, list] = {}
        class FakeContext:
            def on(self, event, handler):
                listeners.setdefault(event, []).append(handler)
            def remove_listener(self, event, handler):
                pass
        class FakePage:
            url = "https://gateway2.geico.com/policy"
            def __init__(self):
                self.context = FakeContext()
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
        with self.assertRaisesRegex(IntakeHold, "viewer did not open"):
            self._run(FakePage(), locator)

    def test_viewer_flow_html_shell_only_holds(self):
        """Viewer HTML loads but no PDF XHR and no Download button -> hold."""
        locator, _ = self._locator()
        listeners: dict[str, list] = {}
        class FakeContext:
            def on(self, event, handler):
                listeners.setdefault(event, []).append(handler)
                if event == "response":
                    # Only the HTML shell arrives; no PDF XHR.
                    handler(SimpleNamespace(
                        url=ViewerFlowTests.VIEWER_URL,
                        headers={"content-type": "text/html"},
                        body=b"<html>viewer shell</html>",
                    ))
            def remove_listener(self, event, handler):
                pass
        class FakeViewer:
            url = ViewerFlowTests.VIEWER_URL
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
            @property
            def frames(self):
                return []
            def get_by_role(self, role, name=None, exact=True):
                class Empty:
                    def count(self):
                        return 0
                return Empty()
        opened: list = []
        orig_on = FakeContext.on
        def patched_on(self, event, handler):
            orig_on(self, event, handler)
            if event == "page":
                handler(FakeViewer())
        FakeContext.on = patched_on
        class FakePage:
            url = "https://gateway2.geico.com/policy"
            def __init__(self):
                self.context = FakeContext()
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
        try:
            with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
                self._run(FakePage(), locator)
        finally:
            FakeContext.on = orig_on

    def test_viewer_flow_multiple_pdfs_hold(self):
        """Two PDF XHRs is ambiguous -> hold."""
        locator, _ = self._locator()
        listeners: dict[str, list] = {}
        class FakeContext:
            def on(self, event, handler):
                listeners.setdefault(event, []).append(handler)
                if event == "response":
                    handler(SimpleNamespace(
                        url="https://e/a.pdf", headers={"content-type": "application/pdf"},
                        body=lambda: pdf_bytes(b"one")))
                    handler(SimpleNamespace(
                        url="https://e/b.pdf", headers={"content-type": "application/pdf"},
                        body=lambda: pdf_bytes(b"two")))
            def remove_listener(self, event, handler):
                pass
        class FakeViewer:
            url = ViewerFlowTests.VIEWER_URL
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
        opened: list = []
        orig_on = FakeContext.on
        def patched_on(self, event, handler):
            orig_on(self, event, handler)
            if event == "page":
                handler(FakeViewer())
        FakeContext.on = patched_on
        class FakePage:
            url = "https://gateway2.geico.com/policy"
            def __init__(self):
                self.context = FakeContext()
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
        try:
            with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
                self._run(FakePage(), locator)
        finally:
            FakeContext.on = orig_on

    def test_viewer_flow_download_button_fallback(self):
        """No XHR, but the viewer's Download button yields the PDF."""
        import robie_job_engine.geico_pending_cancellation_noc as mod
        token = pdf_bytes(b"download-button")
        locator, _ = self._locator()
        listeners: dict[str, list] = {}

        class FakeButton:
            def click(self, *a, **k):
                return None
            def count(self):
                return 1

        class FakeFrame:
            def get_by_role(self, role, name=None, exact=True):
                return FakeButton()

        class FakeViewer:
            url = ViewerFlowTests.VIEWER_URL
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None
            @property
            def frames(self):
                return [FakeFrame()]
            def get_by_role(self, role, name=None, exact=True):
                class Empty:
                    def count(self):
                        return 0
                return Empty()
            def expect_download(self, timeout=None):
                outer_token = token
                class Ctx:
                    def __enter__(self):
                        return SimpleNamespace(value=SimpleNamespace())
                    def __exit__(self, *a):
                        return False
                return Ctx()

        opened: list = []
        class FakeContext:
            def on(self, event, handler):
                listeners.setdefault(event, []).append(handler)
            def remove_listener(self, event, handler):
                pass
        orig_on = FakeContext.on
        def patched_on(self, event, handler):
            orig_on(self, event, handler)
            if event == "page":
                handler(FakeViewer())
        FakeContext.on = patched_on

        class FakePage:
            url = "https://gateway2.geico.com/policy"
            def __init__(self):
                self.context = FakeContext()
            def wait_for_load_state(self, *a, **k):
                return None
            def wait_for_timeout(self, ms):
                return None

        orig_dl = mod._download_bytes
        try:
            mod._download_bytes = lambda download: token
            got_id, got_bytes = self._run(FakePage(), locator)
        finally:
            FakeContext.on = orig_on
            mod._download_bytes = orig_dl
        self.assertEqual(got_id, self.DOC_ID)
        self.assertEqual(got_bytes, token)


class PdfCaptureTests(unittest.TestCase):
    def test_matching_download_and_tab_are_one_pdf(self):
        blob = pdf_bytes(b"same")
        chosen = pdf_bytes_from_observation(NoticeOpenObservation(
            downloads=(blob,),
            pages=(PagePdfView("https://gateway2.geico.com/notice.pdf", (blob,)),),
        ))
        self.assertEqual(chosen, blob)

    def test_non_pdf_and_disagreeing_bytes_hold(self):
        with self.assertRaisesRegex(IntakeHold, "not a PDF"):
            pdf_bytes_from_observation(NoticeOpenObservation(
                downloads=(b"<!DOCTYPE html>",),
                pages=(PagePdfView("blob:noc", (pdf_bytes(b"ok"),)),),
            ))
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            pdf_bytes_from_observation(NoticeOpenObservation(
                downloads=(pdf_bytes(b"one"),),
                pages=(PagePdfView("blob:noc", (pdf_bytes(b"two"),)),),
            ))
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            pdf_bytes_from_observation(NoticeOpenObservation(downloads=(), pages=()))

    def test_blob_and_geico_host_are_read_cdn_and_login_are_not(self):
        token = pdf_bytes(b"viewer")
        encoded = base64.b64encode(token).decode("ascii")
        calls = []

        class Response:
            def body(self):
                return token

        class Request:
            def get(self, url, timeout):
                calls.append(url)
                return Response()

        class Page:
            def __init__(self, url, embeds):
                self.url = url
                self.context = SimpleNamespace(request=Request())
                self._embeds = embeds

            def evaluate(self, script, url):
                self.evaluated = url
                return encoded

            def locator(self, selector):
                return FakeLocator(self._embeds.get(selector, []))

        blob_page = Page("blob:https://gateway2.geico.com/noc", {})
        self.assertEqual(read_playwright_pdf_view(blob_page).pdfs, (token,))
        self.assertEqual(blob_page.evaluated, blob_page.url)

        viewer = Page("https://gateway2.geico.com/notices/6253395526.pdf", {})
        self.assertEqual(read_playwright_pdf_view(viewer).pdfs, (token,))
        self.assertEqual(calls, ["https://gateway2.geico.com/notices/6253395526.pdf"])

        html = Page("https://gateway2.geico.com/print", {
            "a": [SimpleNamespace(get_attribute=lambda name: "https://cdn.example/noc.pdf" if name == "href" else None)],
            "iframe": [SimpleNamespace(get_attribute=lambda name: "https://geicoextendprod.b2clogin.com/noc.pdf" if name == "src" else None)],
        })
        before = len(calls)
        self.assertEqual(read_playwright_pdf_view(html).pdfs, ())
        self.assertEqual(len(calls), before)

    def test_new_tab_is_closed_without_closing_the_list(self):
        token = pdf_bytes(b"tab")
        page = DownloadPage(token)
        observation = collect_notice_observation(page, page.open_tab, read_page=lambda item: item.view)
        self.assertEqual(pdf_bytes_from_observation(observation), token)
        self.assertEqual(page.closed, False)
        self.assertEqual(page.context.closed, [True])


class FakeLocator:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def all(self):
        return [FakeLocator([node]) for node in self.nodes]

    def get_attribute(self, name):
        return self.nodes[0].get_attribute(name)


class DownloadPage:
    def __init__(self, token: bytes):
        self.url = LIST_URL
        self.token = token
        self.closed = False
        self.listeners = []
        self.context = SimpleNamespace(on=self._on, remove_listener=self._off, closed=[])
        self._download = None

    def _on(self, event, fn):
        self.listeners.append(fn)

    def _off(self, event, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    def open_tab(self):
        tab = SimpleNamespace(
            view=PagePdfView("blob:noc", (self.token,)),
            close=lambda: self.context.closed.append(True),
            wait_for_load_state=lambda *args, **kwargs: None,
        )
        for fn in list(self.listeners):
            fn(tab)

    def expect_download(self, timeout):
        return _Expect(self)


class _Expect:
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


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"
        self.ledger = LocalDeliveryLedger(self.output)
        self.archive = SourceArchive(self.output / "sources")
        self.grid = prove_grid()
        self.alerts = parse_alert_grid(self.grid)
        self.pdfs = {alert.policy_number: pdf_bytes(alert.policy_number.encode()) for alert in self.alerts if alert.line == "personal"}
        self.browser = ScriptedBrowser(self.grid, prove_paths(), self.pdfs)

    def pull(self, browser=None, ledger=None):
        return run_pull(browser or self.browser, ledger or self.ledger, self.archive, as_of=AS_OF)

    def test_prove_pull_saves_two_pdfs_and_holds_commercial_separately(self):
        receipt = self.pull()
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["process"], PROCESS)
        self.assertEqual(receipt["ezlynx"], "not_run")
        self.assertNotIn("COMPLETE", json.dumps(receipt))
        self.assertEqual(receipt["targeted"], 2)
        self.assertEqual(receipt["count"], 2)
        self.assertEqual([item["filename"] for item in receipt["downloaded"]], list(PERSONAL_NAMES))
        self.assertEqual(self.browser.downloads, [GUEVARA, PANELLA])
        self.assertEqual(self.browser.inspections, [COMMERCIAL, GUEVARA, PANELLA])
        self.assertEqual(receipt["held"], [{
            "policy_number": COMMERCIAL,
            "insured_name": "BYOND TRANSPORTATION LLC",
            "due_date": "2026-09-21",
            "status": "High",
            "product": "Commercial Auto",
            "line": "commercial",
            "outcome": "HELD",
            "reason": "commercial policy: Documents path missing; Billing only",
        }])
        self.assertEqual(receipt["verification"]["targeted"], 2)
        self.assertEqual(receipt["verification"]["pdfs"], 2)
        shot = self.output / "geico-pending-cancellations-2026-09-26.png"
        self.assertEqual(shot.read_bytes(), LIST_PNG)
        self.assertEqual(shot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_mode & 0o077, 0)
        for name in PERSONAL_NAMES:
            saved = self.output / name
            self.assertTrue(saved.read_bytes().startswith(b"%PDF"))
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.output / f"{COMMERCIAL} NOC Geico.pdf").exists())

    def test_replay_skips_saved_personal_pdfs_and_still_holds_commercial(self):
        self.pull()
        replay = ScriptedBrowser(self.grid, prove_paths(), self.pdfs)
        second = self.pull(replay)
        self.assertEqual(second["status"], "PULLED")
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["targeted"], 2)
        self.assertEqual(replay.downloads, [])
        self.assertEqual(replay.inspections, [COMMERCIAL])
        self.assertEqual(second["held"][0]["outcome"], "HELD")
        self.assertEqual(list(self.output.glob("geico-pending-cancellations-*.png")), [
            self.output / "geico-pending-cancellations-2026-09-26.png",
        ])

    def test_commercial_notice_is_not_downloaded_as_success(self):
        browser = ScriptedBrowser(
            self.grid,
            prove_paths(**{COMMERCIAL: NoticePath("noc", "Pending Cancellation Notice")}),
            self.pdfs,
        )
        receipt = self.pull(browser)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertNotIn(COMMERCIAL, browser.downloads)
        self.assertIn("not downloaded", receipt["held"][0]["reason"])

    def test_personal_path_missing_holds_the_job_and_keeps_partial_pdf(self):
        browser = ScriptedBrowser(self.grid, prove_paths(**{PANELLA: NoticePath("absent")}), self.pdfs)
        with self.assertRaisesRegex(IntakeHold, "6260043796"):
            self.pull(browser)
        self.assertEqual(browser.downloads, [GUEVARA])
        self.assertTrue((self.output / PERSONAL_NAMES[0]).is_file())
        self.assertTrue((self.output / "geico-pending-cancellations-2026-09-26.png").is_file())
        self.assertNotIn(COMMERCIAL, browser.downloads)

    def test_count_mismatch_does_not_save_the_screenshot_or_claim_success(self):
        class ShortLedger(LocalDeliveryLedger):
            def verified_ids(self, document_ids):
                found = super().verified_ids(document_ids)
                return set(list(found)[:-1]) if found else found

        ledger = ShortLedger(self.output)
        with self.assertRaisesRegex(IntakeHold, "2 targeted and 1 PDFs"):
            self.pull(ledger=ledger)
        self.assertTrue((self.output / "geico-pending-cancellations-2026-09-26.png").is_file())
        self.assertTrue((self.output / PERSONAL_NAMES[0]).is_file())

    def test_enabled_next_and_bad_screenshot_hold_before_download(self):
        paged = ScriptedBrowser(prove_grid(more_pages=True), prove_paths(), self.pdfs)
        with self.assertRaisesRegex(IntakeHold, "incomplete or ambiguous"):
            self.pull(paged)
        self.assertEqual(paged.shots, 1)
        self.assertEqual(paged.inspections, [])
        shot = self.output / "geico-pending-cancellations-2026-09-26.png"
        self.assertEqual(shot.read_bytes(), LIST_PNG)

        self.browser.screenshot_bytes = b"GIF89a-not-a-png"
        with self.assertRaisesRegex(IntakeHold, "not a PNG"):
            self.pull()
        self.assertEqual(self.browser.inspections, [])
        self.assertEqual(shot.read_bytes(), LIST_PNG)
        self.assertEqual(list(self.output.glob("*.png")), [shot])

    def test_conflicting_personal_file_and_commercial_file_are_kept(self):
        self.ledger.ensure_private()
        personal = self.output / PERSONAL_NAMES[0]
        personal.write_bytes(pdf_bytes(b"different"))
        with self.assertRaisesRegex(IntakeHold, "conflicts"):
            self.pull()
        self.assertEqual(personal.read_bytes(), pdf_bytes(b"different"))
        self.assertEqual(self.browser.downloads, [])
        self.assertEqual(self.browser.inspections, [COMMERCIAL])

        commercial = self.output / f"{COMMERCIAL} NOC Geico.pdf"
        personal.unlink()
        commercial.write_bytes(pdf_bytes(b"commercial"))
        fresh = ScriptedBrowser(self.grid, prove_paths(), self.pdfs)
        with self.assertRaisesRegex(IntakeHold, "local NOC file"):
            self.pull(fresh)
        self.assertEqual(commercial.read_bytes(), pdf_bytes(b"commercial"))
        self.assertEqual(fresh.inspections, [])

    def test_zero_personal_rows_can_pass_the_gate_with_commercial_held(self):
        grid = AlertGrid(LIST_URL, HEADERS, (PROVE_ROWS[0],), False)
        browser = ScriptedBrowser(grid, {COMMERCIAL: NoticePath("billing_only")}, {})
        receipt = self.pull(browser)
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["targeted"], 0)
        self.assertEqual(receipt["count"], 0)
        self.assertEqual(receipt["verification"]["pdfs"], 0)
        self.assertEqual(receipt["held"][0]["policy_number"], COMMERCIAL)
        self.assertTrue((self.output / "geico-pending-cancellations-2026-09-26.png").is_file())

    def test_production_unset_and_poc_host_do_not_attach(self):
        calls = []

        def factory(args):
            calls.append(args)
            return self.browser

        stdout = io.StringIO()
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}), patch("sys.stdout", stdout):
            code = main(["--as-of", "2026-09-26", "--output-root", str(self.output)], browser_factory=factory)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(stdout.getvalue())["status"], "HELD")
        self.assertFalse(self.output.exists())

        stdout.seek(0)
        stdout.truncate()
        with patch.dict(os.environ, {"ROBIE_ENV": ""}), patch("sys.stdout", stdout):
            code = main(["--as-of", "2026-09-26", "--output-root", str(self.output)], browser_factory=factory)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("TEST", json.loads(stdout.getvalue())["reason"])

        stdout.seek(0)
        stdout.truncate()
        with patch("robie_job_engine.geico_pending_cancellation_noc.socket.gethostname", return_value="hermes-poc-01"), \
             patch("robie_job_engine.geico_pending_cancellation_noc.socket.getfqdn", return_value="hermes-poc-01.c.streetsmart-hermes-poc.internal"), \
             patch("sys.stdout", stdout):
            code = main(["--as-of", "2026-09-26", "--output-root", str(self.output)], browser_factory=factory)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("hermes-poc-01", json.loads(stdout.getvalue())["reason"])

    def test_cli_prints_pulled_receipt_with_separate_held_rows(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--as-of", "2026-09-26", "--output-root", str(self.output)],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "PULLED")
        self.assertEqual(payload["ezlynx"], "not_run")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["held"][0]["policy_number"], COMMERCIAL)
        self.assertTrue(payload["verification"]["screenshot"].endswith("geico-pending-cancellations-2026-09-26.png"))
        pack = self.output / "2026-09-26"
        self.assertEqual(payload["pack"], str(pack))
        manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["carrier"], "geico")
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["screenshot"], str(pack / "geico-pending-cancellations-2026-09-26.png"))
        self.assertEqual([item["filename"] for item in manifest["downloaded"]], list(PERSONAL_NAMES))
        self.assertEqual([row["policy_number"] for row in manifest["rows"]], [COMMERCIAL, GUEVARA, PANELLA])
        self.assertEqual(manifest["held"][0]["policy_number"], COMMERCIAL)
        self.assertEqual(manifest["drive"]["status"], "not_run")
        self.assertEqual(manifest["drive"]["folder_id"], GEICO_QA_DRIVE_FOLDER_ID)
        self.assertEqual(manifest["drive"]["parent_id"], CARRIER_QA_DRIVE_PARENT_ID)
        self.assertEqual(
            manifest["drive"]["path"],
            "Robie Carrier Pull QA (Nicole)/Geico/2026-09-26/",
        )
        readme = (pack / "README.md").read_text(encoding="utf-8")
        self.assertIn("BYOND TRANSPORTATION LLC", readme)
        self.assertIn("Billing only", readme)
        self.assertIn(PERSONAL_NAMES[0], readme)
        self.assertIn(PERSONAL_NAMES[1], readme)
        self.assertIn("2026-09-26", readme)
        self.assertIn("Drive: not_run", readme)
        self.assertIn(GEICO_QA_DRIVE_FOLDER_ID, readme)
        self.assertNotIn("geico-noc-ledger.json", readme)

        stdout.seek(0)
        stdout.truncate()
        broken = ScriptedBrowser(self.grid, prove_paths(**{PANELLA: NoticePath("absent")}), self.pdfs)
        fresh = Path(self.tmp.name) / "held"
        with patch("sys.stdout", stdout):
            code = main(
                ["--as-of", "2026-09-26", "--output-root", str(fresh)],
                browser_factory=lambda args: broken,
                run_ts="2026-09-26T12:00:00-04:00",
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "HELD")
        self.assertEqual(payload["held"][0]["outcome"], "HELD")
        held_pack = fresh / "2026-09-26"
        self.assertTrue((held_pack / PERSONAL_NAMES[0]).is_file())
        self.assertTrue((held_pack / "geico-pending-cancellations-2026-09-26.png").is_file())
        held_manifest = json.loads((held_pack / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(held_manifest["status"], "HELD")
        self.assertEqual(held_manifest["carrier"], "geico")
        self.assertIn("6260043796", held_manifest["reason"])
        self.assertIn("Billing only", (held_pack / "README.md").read_text(encoding="utf-8"))

    def test_default_pack_is_the_hermes_geico_date_folder(self):
        self.assertEqual(
            DEFAULT_OUTPUT_ROOT,
            Path("/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/geico"),
        )
        self.assertEqual(qa_pack_dir(DEFAULT_OUTPUT_ROOT, AS_OF), DEFAULT_OUTPUT_ROOT / "2026-09-26")
        self.assertEqual(qa_pack_dir(self.output, AS_OF), self.output / "2026-09-26")
        with self.assertRaisesRegex(IntakeHold, "absolute"):
            qa_pack_dir(Path("relative-root"), AS_OF)

    def test_upload_drive_keeps_the_local_pack_and_does_not_call_google(self):
        before = set(sys.modules)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--as-of", "2026-09-26", "--output-root", str(self.output), "--upload-drive"],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "HELD")
        self.assertEqual(payload["portal_gate"], "PULLED")
        self.assertEqual(payload["reason"], DRIVE_UPLOAD_UNAVAILABLE)
        self.assertEqual(payload["ezlynx"], "not_run")
        pack = self.output / "2026-09-26"
        self.assertTrue((pack / PERSONAL_NAMES[0]).is_file())
        self.assertTrue((pack / PERSONAL_NAMES[1]).is_file())
        self.assertTrue((pack / "geico-pending-cancellations-2026-09-26.png").is_file())
        self.assertTrue((pack / "README.md").is_file())
        manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["carrier"], "geico")
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["drive"]["status"], "HELD")
        self.assertEqual(manifest["drive"]["reason"], DRIVE_UPLOAD_UNAVAILABLE)
        self.assertEqual(manifest["drive"]["parent_id"], CARRIER_QA_DRIVE_PARENT_ID)
        self.assertEqual(manifest["drive"]["folder_id"], GEICO_QA_DRIVE_FOLDER_ID)
        readme = (pack / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Drive: HELD — {DRIVE_UPLOAD_UNAVAILABLE}", readme)
        added = set(sys.modules) - before
        self.assertFalse(any(name == "google" or name.startswith(("google.", "googleapiclient")) for name in added))

    def test_cli_unexpected_error_is_unverified_without_the_message(self):
        stdout = io.StringIO()

        def explode(args):
            raise RuntimeError("secret-boom")

        with patch("sys.stdout", stdout):
            code = main(["--as-of", "2026-09-26", "--output-root", str(self.output)], browser_factory=explode)
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "UNVERIFIED")
        self.assertIn("RuntimeError", payload["reason"])
        self.assertNotIn("secret-boom", payload["reason"])

    def test_parity_rejects_a_short_or_extra_set(self):
        with self.assertRaisesRegex(IntakeHold, "2 targeted and 1 PDFs"):
            require_noc_pdf_parity(targeted_ids={"a", "b"}, verified_ids={"a"})
        with self.assertRaisesRegex(IntakeHold, "2 targeted and 3 PDFs"):
            require_noc_pdf_parity(targeted_ids={"a", "b"}, verified_ids={"a", "b", "c"})
        evidence = require_noc_pdf_parity(targeted_ids=set(), verified_ids=set())
        self.assertEqual(evidence["pdfs"], 0)


class LocatorContractTests(unittest.TestCase):
    def test_registered_names_are_exact_and_not_positional(self):
        page = LocatorRegistry().get_page("geico_gateway", "pending_cancellations")
        self.assertIsNotNone(page)
        selectors = []
        for field in page.fields.values():
            field.validate()
            selectors.append(field.primary_selector)
            if field.fallback_selector:
                selectors.append(field.fallback_selector)
        self.assertIn("link:Client Alerts", selectors)
        self.assertIn("option:Pending Cancellations", selectors)
        self.assertIn("link:Documents", selectors)
        self.assertIn("link:Billing", selectors)
        self.assertIn("link:Pending Cancellation Notice", selectors)
        self.assertIn("link:CANCELLATION NOTICE", selectors)
        for selector in selectors:
            self.assertFalse(locator_is_positional_guess(selector))

    def test_one_gateway_tab_and_loopback_cdp_are_required(self):
        app = SimpleNamespace(url=LIST_URL)
        login = SimpleNamespace(url="https://geicoextendprod.b2clogin.com/authorize")
        other = SimpleNamespace(url="https://ezlynx.com/web/")
        self.assertIs(select_gateway_page([login, other, app]), app)
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_gateway_page([app, SimpleNamespace(url="https://gateway2.geico.com/policy/1")])
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_gateway_page([login])
        with self.assertRaises(IntakeHold):
            require_loopback_cdp("http://10.0.0.5:9222")
        with self.assertRaises(IntakeHold):
            require_loopback_cdp("http://user:pass@127.0.0.1:9222")
        self.assertEqual(require_loopback_cdp("http://127.0.0.1:9222"), "http://127.0.0.1:9222")

    def test_module_does_not_type_credentials_or_call_ezlynx(self):
        text = Path("robie_job_engine/geico_pending_cancellation_noc.py").read_text(encoding="utf-8")
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
        self.assertNotIn("otp", text.casefold())


class FakeNode:
    def __init__(self, role, name="", text=None, children=None, attrs=None, disabled=False):
        self.role = role
        self.name = name
        self.text = name if text is None else text
        self.children = children or []
        self.attrs = attrs or {}
        self.disabled = disabled

    def find(self, selector):
        found = [self] if self.matches(selector) else []
        for child in self.children:
            found.extend(child.find(selector))
        return found

    def _name_ok(self, name, exact):
        if name is None:
            return True
        if isinstance(name, re.Pattern):
            return name.search(self.name or "") is not None
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
        return {
            "table": self.role == "table",
            "thead": self.role == "thead",
            "tbody": self.role == "tbody",
            "th": self.role == "th",
            "td": self.role == "td",
            "tr": self.role == "tr",
            "iframe": self.role == "iframe",
            "a": self.role == "link",
            "embed[type='application/pdf']": self.role == "embed" and self.attrs.get("type") == "application/pdf",
            "input[type='password']": self.attrs.get("type") == "password",
        }.get(selector, False)


class NodeLocator:
    def __init__(self, nodes, page):
        self.nodes = list(nodes)
        self.page = page

    def count(self):
        return len(self.nodes)

    def click(self, *args, **kwargs):
        node = self.nodes[0]
        self.page.clicks.append(node.name)
        self.page.on_click(node)

    def inner_text(self, timeout=None):
        return self.nodes[0].text

    def get_attribute(self, name):
        return self.nodes[0].attrs.get(name)

    def is_disabled(self):
        return self.nodes[0].disabled

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


class NavPage:
    def __init__(self, *, start="list", password=False, url=None, duplicate_alerts=False,
                 next_mode="none", go_back_restores=True, chip_mode=None):
        self.password = password
        self.url = url or (LIST_URL if start == "list" else HOME_URL)
        self.duplicate_alerts = duplicate_alerts
        self.next_mode = next_mode
        self.go_back_restores = go_back_restores
        self.clicks = []
        self.state = "list" if start == "list" else "home"
        self.combo_text = "Pending Cancellations" if start == "list" else ""
        self.table_visible = start == "list"
        self.chip_mode = chip_mode
        if chip_mode:
            self.url = url or LIST_URL
            self.state = "chips"
            self.combo_text = ""
            self.table_visible = chip_mode != "bare-empty"
        self.policy = ""
        self.docs_open = False
        self.billing_open = False
        self.screenshot_calls = 0
        self._download = None
        self.context = NavContext(self)
        self._build_table()

    def _build_table(self):
        body_rows = []
        for cells in PROVE_ROWS:
            policy = cells[1]
            link = FakeNode("link", policy)
            tds = []
            for index, value in enumerate(cells):
                children = [link] if index == 1 else []
                tds.append(FakeNode("td", text=value, children=children))
            body_rows.append(FakeNode("tr", children=tds))
        headers = [FakeNode("th", text=header) for header in HEADERS]
        self.table = FakeNode("table", children=[
            FakeNode("thead", children=[FakeNode("tr", children=headers)]),
            FakeNode("tbody", children=body_rows),
        ])

    def roots(self):
        nodes = []
        if self.password:
            nodes.append(FakeNode("input", attrs={"type": "password"}))
        if self.state == "chips":
            if self.chip_mode == "pressed":
                attrs = {"aria-pressed": "true"}
            elif self.chip_mode in {"open", "duplicate"}:
                attrs = {"aria-pressed": "false"}
            else:
                attrs = {}
            nodes.append(FakeNode("button", "Pending Cancellations (3)", attrs=attrs))
            if self.chip_mode == "duplicate":
                nodes.append(FakeNode("link", "Pending Cancellations (3)", attrs=dict(attrs)))
            if self.chip_mode == "among":
                nodes.append(FakeNode("button", "All Alerts (50)"))
            if self.table_visible:
                nodes.append(self.table)
            return nodes
        if self.state == "home":
            nodes.append(FakeNode("link", "Client Alerts"))
            if self.duplicate_alerts:
                nodes.append(FakeNode("link", "Client Alerts"))
            return nodes
        if self.state == "choose":
            nodes.append(FakeNode("combobox", text=self.combo_text))
            nodes.append(FakeNode("option", "Pending Cancellations"))
            return nodes
        if self.state == "policy":
            if self.policy == COMMERCIAL:
                nodes.append(FakeNode("link", "Billing"))
            else:
                nodes.append(FakeNode("link", "Documents"))
                if self.docs_open:
                    nodes.append(FakeNode("link", "Billing"))
                if self.billing_open:
                    notice = "CANCELLATION NOTICE" if self.policy == PANELLA else "Pending Cancellation Notice"
                    # Live 2026-10-05: href is "#" (Angular). The documentId
                    # comes from the viewer URL after clicking.
                    nodes.append(FakeNode("link", notice, attrs={"href": "#"}))
            return nodes
        nodes.append(FakeNode("combobox", text=self.combo_text))
        if self.table_visible:
            nodes.append(self.table)
        if self.next_mode == "disabled":
            nodes.append(FakeNode("button", "Next", attrs={"aria-disabled": "true"}, disabled=True))
        elif self.next_mode == "enabled":
            nodes.append(FakeNode("button", "Next", attrs={"aria-disabled": "false"}, disabled=False))
        elif self.next_mode == "ambiguous":
            nodes.append(FakeNode("button", "Next", disabled=False))
            nodes.append(FakeNode("link", "Next", disabled=False))
        return nodes

    def locator(self, selector):
        found = []
        for node in self.roots():
            found.extend(node.find(selector))
        return NodeLocator(found, self)

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self.roots():
            found.extend(node.find_role(role, name, exact))
        return NodeLocator(found, self)

    def screenshot(self, full_page=True, type="png", timeout=None):
        self.screenshot_calls += 1
        if type == "png" and self.table_visible and self.state in {"list", "chips"}:
            return LIST_PNG
        return b""

    def expect_download(self, timeout):
        return _Expect(self)

    def go_back(self):
        if not self.go_back_restores:
            return
        self.state = "list"
        self.url = LIST_URL
        self.combo_text = "Pending Cancellations"
        self.table_visible = True
        self.policy = ""
        self.docs_open = False
        self.billing_open = False
        self._download = None

    def on_click(self, node):
        if node.name == "Client Alerts" and self.state == "home":
            self.state = "choose"
            self.url = LIST_URL
            self.combo_text = "All Alerts"
        elif self.state == "chips" and str(node.name).startswith("Pending Cancellations"):
            if self.chip_mode == "open":
                self.chip_mode = "pressed"
            elif self.chip_mode == "bare-empty":
                self.chip_mode = "bare"
                self.table_visible = True
        elif node.name == "Pending Cancellations" and self.state == "choose":
            self.state = "list"
            self.combo_text = "Pending Cancellations"
            self.table_visible = True
        elif node.name in {COMMERCIAL, GUEVARA, PANELLA} and self.state in {"list", "chips"}:
            self.policy = node.name
            self.state = "policy"
            self.url = f"https://gateway2.geico.com/policy/{node.name}"
            self.table_visible = False
            self.docs_open = False
            self.billing_open = False
        elif node.name == "Documents":
            self.docs_open = True
        elif node.name == "Billing":
            self.billing_open = True
        elif node.name in {"Pending Cancellation Notice", "CANCELLATION NOTICE"}:
            # Viewer flow (live 2026-10-05): clicking the notice (href="#")
            # opens the consolidated viewer in a new tab with documentId in
            # the URL; the PDF arrives via XHR.
            doc_id = f"00000000-0000-4000-8000-{int(self.policy or '0'):012d}"
            viewer_url = (
                "https://edgeextended.geico.com/documents/consolidated-document-viewer"
                f"?documentId={doc_id}&token=test&visitAppId=E01&convToken="
            )
            viewer = NavViewerPage(viewer_url)
            for fn in list(self.context.listeners):
                fn(viewer)
            token = pdf_bytes(self.policy.encode())
            resp = SimpleNamespace(
                url="https://edgeextended.geico.com/api/documents/stream",
                headers={"content-type": "application/pdf"},
                body=lambda: token,
            )
            for fn in list(self.context.response_listeners):
                fn(resp)


class NavViewerPage:
    """Mock for the consolidated document viewer popup."""
    def __init__(self, url):
        self.url = url
    def wait_for_load_state(self, *a, **k):
        return None
    def wait_for_timeout(self, ms):
        return None
    @property
    def frames(self):
        return []
    def get_by_role(self, role, name=None, exact=True):
        class Empty:
            def count(self):
                return 0
        return Empty()
    def close(self):
        return None


class NavContext:
    def __init__(self, page):
        self.page = page
        self.listeners = []
        self.response_listeners = []
        self.request = SimpleNamespace(get=self._get)

    def on(self, event, fn):
        if event == "page":
            self.listeners.append(fn)
        elif event == "response":
            self.response_listeners.append(fn)

    def remove_listener(self, event, fn):
        if event == "page" and fn in self.listeners:
            self.listeners.remove(fn)
        elif event == "response" and fn in self.response_listeners:
            self.response_listeners.remove(fn)

    def _get(self, url, timeout):
        # Direct-fetch: return PDF bytes for document URLs.
        # The documentId encodes the policy number in the last 12 digits.
        import re
        m = re.search(r"documentId=00000000-0000-4000-8000-(\d{12})", str(url or ""))
        if m:
            policy = str(int(m.group(1)))
            return SimpleNamespace(ok=True, body=lambda: pdf_bytes(policy.encode()))
        return SimpleNamespace(ok=True, body=lambda: b"")


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"

    def test_chip_navigation_downloads_personal_notices_and_holds_billing_only(self):
        # bda3df83 removed the Client Alerts fallback: the live Gateway exposes
        # Pending Cancellations as a count-suffixed filter chip, clicked directly.
        page = NavPage(chip_mode="open")
        browser = PlaywrightGeicoNocBrowser(page)
        receipt = run_pull(
            browser,
            LocalDeliveryLedger(self.output),
            SourceArchive(self.output / "sources"),
            as_of=AS_OF,
        )
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 2)
        self.assertEqual(receipt["held"][0]["policy_number"], COMMERCIAL)
        self.assertEqual((self.output / PERSONAL_NAMES[0]).read_bytes(), pdf_bytes(GUEVARA.encode()))
        self.assertEqual((self.output / PERSONAL_NAMES[1]).read_bytes(), pdf_bytes(PANELLA.encode()))
        self.assertFalse((self.output / f"{COMMERCIAL} NOC Geico.pdf").exists())
        self.assertEqual(page.clicks[0], "Pending Cancellations (3)")
        self.assertNotIn("Client Alerts", page.clicks)
        self.assertIn("Documents", page.clicks)
        # Viewer flow (live 2026-10-05): the notice link (href="#") IS clicked;
        # the viewer opens with documentId in the URL and the PDF is captured
        # via XHR interception.
        self.assertIn("Pending Cancellation Notice", page.clicks)
        self.assertIn("CANCELLATION NOTICE", page.clicks)
        self.assertEqual(page.url, LIST_URL)
        self.assertEqual(page.screenshot_calls, 1)
        # Viewer flow: Documents and Billing are clicked to reach the notice
        # list, then the notice link itself is clicked to open the viewer.
        self.assertIn("Documents", page.clicks)
        self.assertIn("Billing", page.clicks)

    def test_filter_chip_selects_pending_cancellations_without_client_alerts(self):
        page = NavPage(chip_mode="open")
        browser = PlaywrightGeicoNocBrowser(page)
        receipt = run_pull(
            browser,
            LocalDeliveryLedger(self.output / "chips"),
            SourceArchive(self.output / "chips" / "sources"),
            as_of=AS_OF,
        )
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["count"], 2)
        self.assertEqual(page.clicks[0], "Pending Cancellations (3)")
        self.assertNotIn("Client Alerts", page.clicks)
        self.assertTrue((self.output / "chips" / PERSONAL_NAMES[0]).is_file())
        self.assertFalse((self.output / "chips" / f"{COMMERCIAL} NOC Geico.pdf").exists())

    def test_pressed_or_bare_chip_with_the_list_does_not_reclick(self):
        for mode in ("pressed", "bare"):
            page = NavPage(chip_mode=mode)
            browser = PlaywrightGeicoNocBrowser(page)
            grid = browser.load_pending_cancellations()
            self.assertEqual(
                [alert.policy_number for alert in parse_alert_grid(grid)],
                [COMMERCIAL, GUEVARA, PANELLA],
            )
            self.assertEqual(page.clicks, [])

    def test_unselected_chip_without_a_table_is_clicked_before_the_list(self):
        page = NavPage(chip_mode="bare-empty")
        browser = PlaywrightGeicoNocBrowser(page)
        grid = browser.load_pending_cancellations()
        self.assertEqual(len(parse_alert_grid(grid)), 3)
        self.assertEqual(page.clicks, ["Pending Cancellations (3)"])
        self.assertNotIn("Client Alerts", page.clicks)

    def test_pending_chip_among_all_alerts_is_clicked_without_client_alerts(self):
        page = NavPage(chip_mode="among")
        browser = PlaywrightGeicoNocBrowser(page)
        grid = browser.load_pending_cancellations()
        self.assertEqual(
            [alert.policy_number for alert in parse_alert_grid(grid)],
            [COMMERCIAL, GUEVARA, PANELLA],
        )
        self.assertEqual(page.clicks, ["Pending Cancellations (3)"])
        self.assertNotIn("Client Alerts", page.clicks)

    def test_duplicate_pending_chips_hold_before_a_policy_opens(self):
        page = NavPage(chip_mode="duplicate")
        with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
            PlaywrightGeicoNocBrowser(page).load_pending_cancellations()
        self.assertEqual(page.clicks, [])

    def test_already_on_the_list_does_not_reclick_client_alerts(self):
        page = NavPage(start="list", next_mode="disabled")
        browser = PlaywrightGeicoNocBrowser(page)
        grid = browser.load_pending_cancellations()
        alerts = parse_alert_grid(grid)
        self.assertEqual([alert.policy_number for alert in alerts], [COMMERCIAL, GUEVARA, PANELLA])
        self.assertEqual(page.clicks, [])
        self.assertEqual(browser.screenshot_pending_cancellations(), LIST_PNG)

    def test_login_password_and_ambiguous_alerts_do_not_open_a_policy(self):
        cases = (
            NavPage(url="https://geicoextendprod.b2clogin.com/authorize", start="home"),
            NavPage(password=True, start="list"),
            NavPage(start="home", duplicate_alerts=True),
        )
        for page in cases:
            with self.subTest(url=page.url, password=page.password):
                with self.assertRaises(IntakeHold):
                    PlaywrightGeicoNocBrowser(page).load_pending_cancellations()
                self.assertFalse(any(click.isdigit() for click in page.clicks))

    def test_enabled_or_ambiguous_next_holds_before_open(self):
        for mode in ("enabled", "ambiguous"):
            page = NavPage(start="list", next_mode=mode)
            with self.assertRaisesRegex(IntakeHold, "incomplete or ambiguous"):
                run_pull(
                    PlaywrightGeicoNocBrowser(page),
                    LocalDeliveryLedger(self.output / mode),
                    SourceArchive(self.output / mode / "sources"),
                    as_of=AS_OF,
                )
            self.assertFalse(any(click.isdigit() for click in page.clicks))
            self.assertEqual(page.screenshot_calls, 1)
            self.assertTrue((self.output / mode / "geico-pending-cancellations-2026-09-26.png").is_file())

    def test_billing_only_classifier_does_not_invent_a_notice(self):
        page = NavPage(start="list")
        browser = PlaywrightGeicoNocBrowser(page)
        browser.load_pending_cancellations()
        path = browser.inspect_notice_path(COMMERCIAL)
        self.assertEqual(path.kind, "billing_only")
        self.assertNotIn("Documents", page.clicks)
        self.assertNotIn("Pending Cancellation Notice", page.clicks)
        browser.return_to_pending_list()
        personal = browser.inspect_notice_path(GUEVARA)
        self.assertEqual(personal.kind, "noc")
        self.assertEqual(personal.notice_name, "Pending Cancellation Notice")

    def test_return_failure_keeps_the_list_screenshot_and_does_not_claim_pulled(self):
        page = NavPage(start="list", go_back_restores=False)
        with self.assertRaisesRegex(IntakeHold, "left the Pending Cancellations list"):
            run_pull(
                PlaywrightGeicoNocBrowser(page),
                LocalDeliveryLedger(self.output),
                SourceArchive(self.output / "sources"),
                as_of=AS_OF,
            )
        self.assertTrue((self.output / "geico-pending-cancellations-2026-09-26.png").is_file())
        self.assertEqual(list(self.output.glob("* NOC Geico.pdf")), [])


class DocumentClassifyTests(unittest.TestCase):
    def test_ambiguous_documents_and_two_notices_hold(self):
        class Page:
            def __init__(self, roles):
                self.roles = roles
                self.clicks = []

            def get_by_role(self, role, name=None, exact=True):
                nodes = [name for item_role, item_name in self.roles if item_role == role and (name is None or item_name == name)]
                return _RoleList(nodes, self)

        ambiguous = Page([("link", "Documents"), ("button", "Documents"), ("link", "Billing")])
        self.assertEqual(classify_policy_documents(ambiguous).kind, "ambiguous")
        self.assertEqual(ambiguous.clicks, [])

        two_notices = Page([
            ("link", "Documents"),
            ("link", "Billing"),
            ("link", "Pending Cancellation Notice"),
            ("link", "CANCELLATION NOTICE"),
        ])
        self.assertEqual(classify_policy_documents(two_notices).kind, "ambiguous")


class _RoleList:
    def __init__(self, names, page):
        self.names = list(names)
        self.page = page

    def count(self):
        return len(self.names)

    def click(self):
        self.page.clicks.append(self.names[0])



class _Toggle:
    def __init__(self, count, pressed):
        self._count = count
        self.pressed = pressed

    def count(self):
        return self._count

    def get_attribute(self, name):
        return self.pressed if name == "aria-pressed" else None


class _TogglePage:
    def __init__(self, count, pressed="true"):
        self.toggle = _Toggle(count, pressed)
        self.role_queries = 0

    def locator(self, selector, has_text=None):
        assert selector == "gds-toggle-button"
        return self.toggle

    def get_by_role(self, role, name=None, exact=False):
        self.role_queries += 1
        return _Toggle(2, None)


class PendingToggleChipTests(unittest.TestCase):
    def test_single_gds_toggle_is_the_one_chip_and_reads_aria_pressed(self):
        from robie_job_engine.geico_pending_cancellation_noc import _pending_chip_view

        page = _TogglePage(1, "true")
        self.assertEqual(_pending_chip_view(page), "selected")
        self.assertEqual(page.role_queries, 0)
        self.assertEqual(_pending_chip_view(_TogglePage(1, "false")), "unselected")
        self.assertEqual(_pending_chip_view(_TogglePage(2, "true")), "ambiguous")


class GatewayLivePageTests(unittest.TestCase):
    def test_gds_grid_splits_client_and_policy(self):
        from robie_job_engine.geico_pending_cancellation_noc import normalize_gds_grid

        headers, rows = normalize_gds_grid(
            ("", "Client/Policy#", "Product/Description", "Due Date"),
            (("", "Charlemagne Guevara\n6253395526", "Personal Auto\nPending cancellation", "10/06/2026"),),
        )
        self.assertEqual(headers, ("", "Insured", "Policy", "Product/Description", "Due Date"))
        self.assertEqual(rows[0], ("", "Charlemagne Guevara", "6253395526", "Personal Auto", "10/06/2026"))

    def test_client_cell_without_policy_holds(self):
        from robie_job_engine.geico_pending_cancellation_noc import split_client_cell

        with self.assertRaises(IntakeHold):
            split_client_cell("Only A Name")

    def test_old_notice_is_stale(self):
        from robie_job_engine.geico_pending_cancellation_noc import notice_is_stale, notice_issued_on

        issued = notice_issued_on("Cancellation Notice Issued 06/24/2026")
        self.assertEqual(issued, date(2026, 6, 24))
        self.assertTrue(notice_is_stale(issued, date(2026, 10, 6)))
        self.assertFalse(notice_is_stale(date(2026, 9, 20), date(2026, 10, 6)))
        self.assertFalse(notice_is_stale(None, date(2026, 10, 6)))
        self.assertIsNone(notice_issued_on("Issued 06/24/2026 and Issued 07/01/2026"))


if __name__ == "__main__":
    unittest.main()
