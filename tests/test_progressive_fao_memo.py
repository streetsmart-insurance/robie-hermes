"""Fixture tests for Progressive FAO Communications memo pull. No live login."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.gemini_ui_rescue import is_rescuable_control_failure
from robie_job_engine.intake_core import IntakeHold, ReadResult, SourceArchive
from robie_job_engine.locator_registry import LocatorRegistry
from robie_job_engine.playwright_write_guard import locator_is_positional_guess
from robie_job_engine.progressive_fao_memo import (
    DEFAULT_AGENT_CODE,
    DEFAULT_QA_ROOT,
    DRIVE_PROGRESSIVE_FOLDER_ID,
    DRIVE_QA_PARENT_ID,
    FaoCommunicationsMemoPortal,
    LocalDeliveryLedger,
    MemoGrid,
    MemoOpenObservation,
    PagePdfView,
    END_DATE_CSS,
    END_DATE_LABEL,
    COMMUNICATIONS_LINK_CSS,
    COMMUNICATIONS_LINK_LABEL,
    COMMUNICATIONS_SECTION_HOLD,
    GET_POLICY_ACTIVITY_CSS,
    GET_POLICY_ACTIVITY_LABEL,
    AGENCY_ADMIN_NAME,
    HEADER_DRAWER_OPEN_CSS,
    MAIN_NAV_DRAWER_HOLD,
    MAIN_NAVIGATION_NAME,
    MANAGE_POLICIES_CSS,
    MANAGE_POLICIES_NAME,
    POLICY_ACTIVITY_NAMES,
    CUSTOM_DATE_RANGE_LABEL,
    PROCESSED_DATE_OPTION_CSS,
    PROCESSED_DATE_OPTION_LABEL,
    PROCESSED_DATE_OPTION_VALUE,
    PROCESSED_DATE_RANGE_CSS,
    PROCESSED_DATE_RANGE_LABEL,
    START_DATE_CSS,
    START_DATE_LABEL,
    VIEW_ACTIVITY_BY_CSS,
    VIEW_ACTIVITY_BY_LABEL,
    PlaywrightFaoMemoBrowser,
    assert_agent_context,
    build_parser,
    classify_memo_row,
    collect_memo_observation,
    main,
    memo_document_id,
    memo_filename,
    PROCESSED_DATE_RESULTS_HOLD,
    parse_memo_grid,
    require_communications_section,
    pdf_bytes_from_observation,
    require_processed_date_results,
    read_playwright_pdf_view,
    require_loopback_cdp,
    require_memo_pdf_parity,
    resolve_processed_window,
    select_fao_page,
)
from robie_job_engine.progressive_retrieval import ProgressiveRetrieval


LIST_URL = "https://www.foragentsonly.com/managepolicies/policyactivity"
RESULTS_CANCELS_URL = (
    "https://www.foragentsonly.com/managepolicies/policyactivity/"
    "processeddateresults/cancels/"
)
RESULTS_UNDERWRITING_URL = (
    "https://www.foragentsonly.com/managepolicies/policyactivity/"
    "processeddateresults/underwriting/"
)
LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
PROVE_DAY = date(2026, 9, 25)
# Stand-ins for the live preset values. Production reads the value from the
# Select Date Range option; these numbers are not that contract.
FIXTURE_PRESET_YESTERDAY = "1"
FIXTURE_PRESET_LAST_30 = "2"
FIXTURE_CUSTOM_RANGE_VALUE = "4"
PROVE_ROWS = (
    ("860521214", "3JR Contracting LLC", "General", "Memo", "09/25/2026"),
    ("879512352", "ALTI TRANSPORT LLC", "Policy Verific.", "Memo", "09/25/2026"),
    ("993334183", "Yolanda Concepcion", "Signature", "Memo", "09/25/2026"),
    ("983754955", "MHS LLC", "General", "Memo", "09/25/2026"),
)
HEADERS = ("Policy Number", "Insured", "Reason", "Type", "Processed Date", "Open")
EXPECTED_NAMES = (
    "860521214 Progressive Memo General.pdf",
    "879512352 Progressive Memo Policy Verific.pdf",
    "993334183 Progressive Memo Signature.pdf",
    "983754955 Progressive Memo General.pdf",
)


def pdf_bytes(mark: bytes) -> bytes:
    return b"%PDF-1.4\n" + mark + b"\n%%EOF\n"


def prove_grid(*, extra=(), controls=None, more_pages=False, headers=HEADERS) -> MemoGrid:
    rows = tuple(tuple(row) + ("Memo",) for row in PROVE_ROWS) + tuple(extra)
    counts = controls if controls is not None else tuple(1 for _ in rows)
    return MemoGrid(LIST_URL, headers, rows, counts, more_pages)


class ScriptedBrowser:
    def __init__(self, grid: MemoGrid, pdfs: dict[str, bytes]):
        self.grid = grid
        self.pdfs = pdfs
        self.loads = 0
        self.shots = 0
        self.screenshot_bytes = LIST_PNG
        self.captures: list[str] = []

    def load_communications(self, *, start, end, agent_code):
        self.loads += 1
        self.window = (start, end, agent_code)
        return self.grid

    def screenshot_communications(self):
        self.shots += 1
        return self.screenshot_bytes

    def capture_memo(self, document_id):
        self.captures.append(document_id)
        return MemoOpenObservation(downloads=(self.pdfs[document_id],), pages=())


class ParseTests(unittest.TestCase):
    def test_prove_rows_use_live_filenames_and_distinct_ids(self):
        memos = parse_memo_grid(prove_grid(), agent_code=DEFAULT_AGENT_CODE)
        self.assertEqual(tuple(memo.filename for memo in memos), EXPECTED_NAMES)
        self.assertEqual(len({memo.document_id for memo in memos}), 4)
        verific = memos[1]
        self.assertEqual(verific.reason, "Policy Verific")
        self.assertEqual(verific.processed_on, PROVE_DAY)
        self.assertEqual(
            verific.document_id,
            memo_document_id(DEFAULT_AGENT_CODE, "879512352", PROVE_DAY, "Policy Verific."),
        )

    def test_unpadded_processed_date_is_accepted(self):
        grid = prove_grid()
        rows = [tuple(row) for row in grid.rows]
        rows[0] = ("860521214", "3JR Contracting LLC", "General", "Memo", "9/25/2026", "Memo")
        parsed = parse_memo_grid(
            MemoGrid(grid.list_url, grid.headers, tuple(rows), grid.memo_controls, False),
            agent_code=DEFAULT_AGENT_CODE,
        )
        self.assertEqual(parsed[0].processed_on, PROVE_DAY)

    def test_explicit_non_memo_without_a_memo_control_is_not_downloaded(self):
        letter = (("111111111", "Other Insured", "Billing", "Letter", "09/25/2026", "Open"),)
        memos = parse_memo_grid(
            prove_grid(extra=letter, controls=(1, 1, 1, 1, 0)),
            agent_code=DEFAULT_AGENT_CODE,
        )
        self.assertEqual(len(memos), 4)

    def test_letter_row_with_a_memo_control_holds_the_list(self):
        letter = (("111111111", "Other Insured", "Billing", "Letter", "09/25/2026", "Memo"),)
        with self.assertRaises(IntakeHold):
            parse_memo_grid(prove_grid(extra=letter, controls=(1, 1, 1, 1, 1)), agent_code=DEFAULT_AGENT_CODE)

    def test_duplicate_policy_reason_and_date_holds(self):
        duplicate = (PROVE_ROWS[0] + ("Memo",),)
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            parse_memo_grid(prove_grid(extra=duplicate, controls=(1, 1, 1, 1, 1)), agent_code=DEFAULT_AGENT_CODE)

    def test_missing_type_with_one_memo_control_is_a_memo(self):
        headers = ("Policy", "Insured Name", "FAO Reason", "Processed Date", "Open")
        row = (("860521214", "3JR Contracting LLC", "General", "2026-09-25", "Memo"),)
        memos = parse_memo_grid(
            MemoGrid(LIST_URL, headers, row, (1,), False),
            agent_code="ca33617",
        )
        self.assertEqual(memos[0].agent_code, "CA33617")
        self.assertEqual(memos[0].filename, EXPECTED_NAMES[0])

    def test_policy_row_without_type_or_memo_control_holds(self):
        headers = ("Policy", "Insured", "Reason", "Processed Date")
        row = (("860521214", "3JR Contracting LLC", "General", "2026-09-25"),)
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            parse_memo_grid(MemoGrid(LIST_URL, headers, row, (0,), False), agent_code=DEFAULT_AGENT_CODE)

    def test_ragged_row_blank_insured_and_bad_policy_hold(self):
        bad_grids = (
            MemoGrid(LIST_URL, HEADERS, (("860521214", "Only two"),), (1,), False),
            MemoGrid(LIST_URL, HEADERS, (("860521214", "", "General", "Memo", "09/25/2026", "Memo"),), (1,), False),
            MemoGrid(LIST_URL, HEADERS, (("ABC", "Named", "General", "Memo", "09/25/2026", "Memo"),), (1,), False),
            MemoGrid("http://www.foragentsonly.com/activity", HEADERS, (), (), False),
        )
        for grid in bad_grids:
            with self.subTest(grid=grid.rows):
                with self.assertRaises(IntakeHold):
                    parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)

    def test_classify_requires_exactly_one_memo_control(self):
        self.assertEqual(classify_memo_row("Memo", 1), "take")
        self.assertEqual(classify_memo_row("Memo", 0), "ambiguous")
        self.assertEqual(classify_memo_row("Memo", 2), "ambiguous")
        self.assertEqual(classify_memo_row("Letter", 0), "skip")
        self.assertEqual(classify_memo_row(None, 1), "take")
        self.assertEqual(classify_memo_row(None, 0), "ambiguous")

    def test_filename_strips_trailing_period_and_rejects_path_pieces(self):
        self.assertEqual(memo_filename("879512352", "Policy Verific."), EXPECTED_NAMES[1])
        with self.assertRaises(IntakeHold):
            memo_filename("860521214", "../General")
        with self.assertRaises(IntakeHold):
            memo_filename("12", "General")


class PdfCaptureTests(unittest.TestCase):
    def test_matching_download_and_tab_are_one_pdf(self):
        blob = pdf_bytes(b"same")
        chosen = pdf_bytes_from_observation(MemoOpenObservation(
            downloads=(blob,),
            pages=(PagePdfView("https://www.foragentsonly.com/memo.pdf", (blob,)),),
        ))
        self.assertEqual(chosen, blob)

    def test_non_pdf_download_holds_even_when_a_tab_has_a_pdf(self):
        with self.assertRaisesRegex(IntakeHold, "not a PDF"):
            pdf_bytes_from_observation(MemoOpenObservation(
                downloads=(b"<!DOCTYPE html>",),
                pages=(PagePdfView("blob:memo", (pdf_bytes(b"ok"),)),),
            ))

    def test_disagreeing_pdfs_and_empty_capture_hold(self):
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            pdf_bytes_from_observation(MemoOpenObservation(
                downloads=(pdf_bytes(b"one"),),
                pages=(PagePdfView("blob:memo", (pdf_bytes(b"two"),)),),
            ))
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            pdf_bytes_from_observation(MemoOpenObservation(downloads=(), pages=()))

    def test_blob_embed_and_viewer_query_are_read_without_printing_html(self):
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

        blob_page = Page("blob:https://www.foragentsonly.com/memo", {})
        view = read_playwright_pdf_view(blob_page)
        self.assertEqual(view.pdfs, (token,))
        self.assertEqual(blob_page.evaluated, blob_page.url)
        self.assertEqual(calls, [])

        viewer = Page(
            "chrome-extension://viewer/index.html?https://www.foragentsonly.com/memos/860521214.pdf",
            {},
        )
        self.assertEqual(read_playwright_pdf_view(viewer).pdfs, (token,))
        self.assertEqual(calls, ["https://www.foragentsonly.com/memos/860521214.pdf"])

        embed = Page("https://www.foragentsonly.com/memo/view", {
            "iframe": [SimpleNamespace(get_attribute=lambda name: "https://www.progressive.com/memos/a.pdf" if name == "src" else None)],
        })
        self.assertEqual(read_playwright_pdf_view(embed).pdfs, (token,))
        self.assertIn("https://www.progressive.com/memos/a.pdf", calls)

        html = Page("https://www.foragentsonly.com/memo/print", {
            "a": [SimpleNamespace(get_attribute=lambda name: "https://cdn.example/memo.pdf" if name == "href" else None)],
        })
        before = len(calls)
        self.assertEqual(read_playwright_pdf_view(html).pdfs, ())
        self.assertEqual(len(calls), before)

    def test_download_event_and_new_tab_close_without_closing_the_list(self):
        token = pdf_bytes(b"tab")
        page = DownloadPage(token)
        observation = collect_memo_observation(page, page.open_tab, read_page=lambda item: item.view)
        self.assertEqual(pdf_bytes_from_observation(observation), token)
        self.assertEqual(page.closed, False)
        self.assertEqual(page.context.closed, [True])

        saved = DownloadPage(token)
        observation = collect_memo_observation(saved, saved.open_download, read_page=lambda item: PagePdfView("", ()))
        self.assertEqual(observation.downloads, (token,))


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

    def open_download(self):
        self._download = SimpleNamespace(save_as=lambda path: Path(path).write_bytes(self.token))

    def open_tab(self):
        tab = SimpleNamespace(
            view=PagePdfView("blob:memo", (self.token,)),
            close=lambda: self.context.closed.append(True),
            wait_for_load_state=lambda *args, **kwargs: None,
        )
        for fn in list(self.listeners):
            fn(tab)

    def expect_download(self, timeout):
        return _Expect(self)

    def close(self):
        self.closed = True


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
        self.memos = parse_memo_grid(self.grid, agent_code=DEFAULT_AGENT_CODE)
        self.pdfs = {memo.document_id: pdf_bytes(memo.policy_number.encode()) for memo in self.memos}
        self.browser = ScriptedBrowser(self.grid, self.pdfs)
        self.portal = FaoCommunicationsMemoPortal(self.browser, self.ledger)
        self.api = SimpleNamespace(writes=0)
        self.worker = ProgressiveRetrieval(self.api, self.archive)

    def test_pull_writes_named_pdfs_once_and_skips_them_on_replay(self):
        items = self.worker.pull_fao_communications(self.portal, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(tuple(item.filename for item in items), EXPECTED_NAMES)
        self.assertEqual(self.browser.loads, 1)
        self.assertEqual(self.browser.shots, 1)
        self.assertEqual(len(self.browser.captures), 4)
        day = self.output / "2026-09-25"
        shot = day / "fao-communications-memo-2026-09-25.png"
        self.assertEqual(shot.read_bytes(), LIST_PNG)
        self.assertEqual(shot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.portal.verification["by_date"], [{
            "processed_date": "2026-09-25", "memo_rows": 4, "pdfs": 4,
        }])
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        readme = (day / "README.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["carrier"], "progressive")
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["memo_rows"], 4)
        self.assertEqual(len(manifest["memos"]), 4)
        self.assertEqual(manifest["drive"]["status"], "not_run")
        self.assertEqual(manifest["drive"]["parent_id"], DRIVE_QA_PARENT_ID)
        self.assertEqual(manifest["drive"]["folder_id"], DRIVE_PROGRESSIVE_FOLDER_ID)
        self.assertIn("860521214", readme)
        self.assertIn("Status: PULLED", readme)
        self.assertIn("## Pulled this run", readme)
        self.assertEqual((day / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((day / "README.md").stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.output / "fao-memo-ledger.json").is_file())
        self.assertFalse((day / "fao-memo-ledger.json").exists())
        self.assertEqual(self.api.writes, 0)
        for item in items:
            named = day / item.filename
            self.assertEqual(named.read_bytes(), item.content)
            self.assertEqual(named.stat().st_mode & 0o777, 0o600)
            self.assertTrue((self.output / "sources").is_dir())
        replay_browser = ScriptedBrowser(self.grid, self.pdfs)
        replay = FaoCommunicationsMemoPortal(replay_browser, self.ledger)
        second = ProgressiveRetrieval(self.api, self.archive).pull_fao_communications(
            replay, start=PROVE_DAY, end=PROVE_DAY,
        )
        self.assertEqual(second, ())
        self.assertEqual(replay_browser.captures, [])
        self.assertEqual(replay.verification["by_date"][0]["pdfs"], 4)
        self.assertEqual(list(day.glob("fao-communications-memo-*.png")), [shot])
        replay_readme = (day / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Already present", replay_readme)
        self.assertIn("860521214 Progressive Memo General.pdf", replay_readme)
        self.assertEqual(replay.skipped_document_ids, tuple(memo.document_id for memo in self.memos))

    def test_other_scopes_and_bad_windows_do_not_download(self):
        for scope in ("policies_need_service", "bop_pending_cancel_nonpayment", "guessed"):
            with self.assertRaises(IntakeHold):
                self.portal.list_documents(scope=scope, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(self.browser.loads, 0)
        with self.assertRaises(IntakeHold):
            self.worker.pull_fao_communications(
                self.portal, start=date(2026, 1, 1), end=date(2026, 9, 1),
            )
        self.assertEqual(self.browser.captures, [])

    def test_out_of_window_row_holds_before_download(self):
        rows = [tuple(row) for row in self.grid.rows]
        rows[0] = ("860521214", "3JR Contracting LLC", "General", "Memo", "09/24/2026", "Memo")
        browser = ScriptedBrowser(MemoGrid(LIST_URL, HEADERS, tuple(rows), (1, 1, 1, 1), False), self.pdfs)
        portal = FaoCommunicationsMemoPortal(browser, self.ledger)
        with self.assertRaisesRegex(IntakeHold, "outside"):
            self.worker.pull_fao_communications(portal, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(browser.captures, [])

    def test_enabled_next_page_holds_without_a_partial_download(self):
        browser = ScriptedBrowser(prove_grid(more_pages=True), self.pdfs)
        portal = FaoCommunicationsMemoPortal(browser, self.ledger)
        with self.assertRaisesRegex(IntakeHold, "incomplete or ambiguous"):
            self.worker.pull_fao_communications(portal, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(browser.captures, [])

    def test_conflicting_local_file_is_kept_and_not_replaced(self):
        filename = EXPECTED_NAMES[0]
        path = self.ledger.date_dir(PROVE_DAY) / filename
        path.write_bytes(pdf_bytes(b"different"))
        with self.assertRaisesRegex(IntakeHold, "conflicts"):
            self.worker.pull_fao_communications(self.portal, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(path.read_bytes(), pdf_bytes(b"different"))
        self.assertEqual(self.browser.captures, [])

    def test_production_refuses_before_the_portal_is_asked(self):
        calls = []
        portal = SimpleNamespace(
            list_documents=lambda **kwargs: calls.append(kwargs),
            download_document=lambda document_id: calls.append(document_id),
        )
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}):
            with self.assertRaisesRegex(IntakeHold, "TEST"):
                self.worker.pull_fao_communications(portal, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(calls, [])

    def test_cli_prints_a_pull_receipt_and_refuses_production(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--pull-only", "--as-of", "2026-09-26", "--start", "2026-09-25", "--end", "2026-09-25", "--output", str(self.output)],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "PULLED")
        self.assertEqual(payload["ezlynx"], "not_run")
        self.assertEqual(payload["count"], 4)
        self.assertEqual(payload["process"], "progressive")
        self.assertEqual(payload["downloaded"][1]["filename"], EXPECTED_NAMES[1])
        self.assertEqual(payload["verification"]["gate"], "memo_rows_equal_pdfs")
        self.assertEqual(payload["verification"]["by_date"][0]["memo_rows"], 4)
        self.assertTrue(payload["verification"]["screenshot"].endswith("fao-communications-memo-2026-09-25.png"))
        self.assertTrue(payload["downloaded"][0]["path"].endswith("2026-09-25/" + EXPECTED_NAMES[0]))
        self.assertIn("2026-09-25", payload["verification"]["packs"])

        called = []
        stdout.seek(0)
        stdout.truncate()
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}), patch("sys.stdout", stdout):
            code = main(
                ["--start", "2026-09-25", "--end", "2026-09-25", "--output", str(self.output)],
                browser_factory=lambda args: called.append(args),
            )
        self.assertEqual(code, 2)
        self.assertEqual(called, [])
        self.assertEqual(json.loads(stdout.getvalue())["status"], "HELD")

    def test_default_cli_asks_the_filing_stage_and_pull_only_does_not(self):
        calls = []

        def fake_file(items, **kwargs):
            calls.append({"items": items, "sheet_day": kwargs.get("sheet_day")})
            return {
                "status": "disabled",
                "reason": "switch off",
                "attempted_writes": False,
                "results": [],
            }

        stdout = io.StringIO()
        with patch("robie_job_engine.document_retrieval_filing.file_progressive_memos", fake_file), patch("sys.stdout", stdout):
            code = main(
                ["--as-of", "2026-09-26", "--start", "2026-09-25", "--end", "2026-09-25", "--output", str(self.output)],
                browser_factory=lambda args: self.browser,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "PULLED")
        self.assertEqual(payload["ezlynx"], "disabled")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["sheet_day"], date(2026, 9, 26))
        self.assertEqual(len(calls[0]["items"]), 4)
        manifest = json.loads((self.output / "2026-09-25" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["ezlynx"], "disabled")
        self.assertFalse(manifest["filing"]["attempted_writes"])

        calls.clear()
        other = Path(self.tmp.name) / "pull-only"
        stdout.seek(0)
        stdout.truncate()
        with patch("robie_job_engine.document_retrieval_filing.file_progressive_memos", fake_file), patch("sys.stdout", stdout):
            code = main(
                ["--pull-only", "--as-of", "2026-09-26", "--start", "2026-09-25", "--end", "2026-09-25", "--output", str(other)],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(stdout.getvalue())["ezlynx"], "not_run")

    def test_bad_screenshot_holds_before_any_memo_download(self):
        self.browser.screenshot_bytes = b"GIF89a-not-a-png"
        with self.assertRaisesRegex(IntakeHold, "not a PNG"):
            self.worker.pull_fao_communications(self.portal, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(self.browser.captures, [])
        self.assertEqual(list(self.output.glob("*.png")), [])

    def test_count_mismatch_writes_a_held_pack_and_does_not_claim_success(self):
        class ShortLedger(LocalDeliveryLedger):
            def pdf_ids_for_date(self, day):
                found = super().pdf_ids_for_date(day)
                return set(list(found)[:-1]) if found else found

        ledger = ShortLedger(self.output)
        portal = FaoCommunicationsMemoPortal(self.browser, ledger)
        with self.assertRaisesRegex(IntakeHold, "4 memo rows and 3 PDFs"):
            self.worker.pull_fao_communications(portal, start=PROVE_DAY, end=PROVE_DAY)
        day = self.output / "2026-09-25"
        self.assertEqual((day / "fao-communications-memo-2026-09-25.png").read_bytes(), LIST_PNG)
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "HELD")
        self.assertEqual(manifest["memo_rows"], 4)
        self.assertEqual(manifest["pdfs"], 3)
        self.assertIn("4 memo rows and 3 PDFs", manifest["held"])
        self.assertIn("Status: HELD", (day / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(len(self.browser.captures), 4)
        self.assertTrue((day / EXPECTED_NAMES[0]).is_file())
        self.assertIsNotNone(portal.verification)

    def test_empty_communications_list_still_saves_a_zero_count_screenshot(self):
        browser = ScriptedBrowser(MemoGrid(LIST_URL, HEADERS, (), (), False), {})
        portal = FaoCommunicationsMemoPortal(browser, self.ledger)
        items = self.worker.pull_fao_communications(portal, start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(items, ())
        self.assertEqual(portal.verification["by_date"], [{
            "processed_date": "2026-09-25", "memo_rows": 0, "pdfs": 0,
        }])
        day = self.output / "2026-09-25"
        self.assertEqual((day / "fao-communications-memo-2026-09-25.png").read_bytes(), LIST_PNG)
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["memos"], [])

    def test_parity_rejects_a_short_or_extra_pdf_count(self):
        rows = [
            {"document_id": "a", "processed_or_effective_date": "2026-09-25"},
            {"document_id": "b", "processed_or_effective_date": "2026-09-25"},
        ]

        class Ids:
            def __init__(self, ids):
                self._ids = set(ids)

            def pdf_ids_for_date(self, day):
                return set(self._ids)

        with self.assertRaisesRegex(IntakeHold, "2 memo rows and 1 PDFs"):
            require_memo_pdf_parity(rows=rows, ledger=Ids({"a"}), start=PROVE_DAY, end=PROVE_DAY)
        with self.assertRaisesRegex(IntakeHold, "2 memo rows and 3 PDFs"):
            require_memo_pdf_parity(rows=rows, ledger=Ids({"a", "b", "c"}), start=PROVE_DAY, end=PROVE_DAY)

        class ByDay:
            def pdf_ids_for_date(self, day):
                return {"a", "b"} if day == PROVE_DAY else set()

        evidence = require_memo_pdf_parity(
            rows=rows, ledger=ByDay(), start=PROVE_DAY, end=date(2026, 9, 26),
        )
        self.assertEqual(
            [(row["processed_date"], row["memo_rows"], row["pdfs"]) for row in evidence],
            [("2026-09-25", 2, 2), ("2026-09-26", 0, 0)],
        )

    def test_cli_holds_a_wide_window_before_attaching(self):
        called = []
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--as-of", "2026-09-26", "--start", "2026-01-01", "--end", "2026-09-01", "--output", str(self.output)],
                browser_factory=lambda args: called.append("attached"),
            )
        self.assertEqual(code, 2)
        self.assertEqual(called, [])
        self.assertIn("standing retrieval window", json.loads(stdout.getvalue())["reason"])
        self.assertFalse(self.output.exists())

    def test_omitted_dates_follow_the_monday_standing_window(self):
        start, end, today = resolve_processed_window(None, None, "2026-09-28")
        self.assertEqual(today, date(2026, 9, 28))
        self.assertEqual((start, end), (date(2026, 9, 25), date(2026, 9, 28)))
        with self.assertRaisesRegex(IntakeHold, "standing retrieval window"):
            resolve_processed_window("2026-09-25", "2026-09-25", "2026-09-29")

    def test_default_output_is_the_hermes_qa_root_and_does_not_create_it(self):
        existed = DEFAULT_QA_ROOT.exists()
        args = build_parser().parse_args(["--start", "2026-09-25", "--end", "2026-09-25"])
        self.assertEqual(Path(args.output), DEFAULT_QA_ROOT)
        self.assertFalse(args.upload_drive)
        self.assertFalse(args.pull_only)
        self.assertFalse(args.file_ezlynx)
        self.assertEqual(DEFAULT_QA_ROOT.exists(), existed)

    def test_each_processed_date_gets_its_own_qa_pack(self):
        rows = (
            ("860521214", "3JR Contracting LLC", "General", "Memo", "09/25/2026", "Memo"),
            ("879512352", "ALTI TRANSPORT LLC", "Policy Verific.", "Memo", "09/26/2026", "Memo"),
        )
        grid = MemoGrid(LIST_URL, HEADERS, rows, (1, 1), False)
        memos = parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)
        pdfs = {memo.document_id: pdf_bytes(memo.policy_number.encode()) for memo in memos}
        browser = ScriptedBrowser(grid, pdfs)
        portal = FaoCommunicationsMemoPortal(browser, self.ledger)
        items = self.worker.pull_fao_communications(portal, start=PROVE_DAY, end=date(2026, 9, 26))
        self.assertEqual(len(items), 2)
        for day, policy in (("2026-09-25", "860521214"), ("2026-09-26", "879512352")):
            folder = self.output / day
            self.assertEqual((folder / f"fao-communications-memo-{day}.png").read_bytes(), LIST_PNG)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "PULLED")
            self.assertTrue(manifest["screenshot_covers_window"])
            self.assertEqual(manifest["window"], {"end": "2026-09-26", "start": "2026-09-25"})
            self.assertEqual([memo["policy_number"] for memo in manifest["memos"]], [policy])
            self.assertIn("whole window", (folder / "README.md").read_text(encoding="utf-8"))
            self.assertEqual(manifest["drive"]["path"], f"Robie Carrier Pull QA (Nicole)/Progressive/{day}/")
        self.assertTrue((self.output / "fao-memo-ledger.json").is_file())

    def test_upload_drive_writes_the_local_pack_then_fails_closed(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                [
                    "--pull-only",
                    "--as-of",
                    "2026-09-26",
                    "--start",
                    "2026-09-25",
                    "--end",
                    "2026-09-25",
                    "--output",
                    str(self.output),
                    "--upload-drive",
                ],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "HELD")
        self.assertIn("not available", payload["reason"])
        self.assertEqual(payload["verification"]["drive_upload"], "HELD")
        self.assertEqual(payload["verification"]["by_date"][0]["pdfs"], 4)
        day = self.output / "2026-09-25"
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["drive"]["status"], "HELD")
        self.assertEqual(manifest["drive"]["folder_id"], DRIVE_PROGRESSIVE_FOLDER_ID)
        self.assertIn("refusing to report the pack as uploaded", (day / "README.md").read_text(encoding="utf-8"))
        self.assertTrue((day / EXPECTED_NAMES[0]).is_file())


class LocatorContractTests(unittest.TestCase):
    def test_registered_names_are_exact_and_not_positional(self):
        page = LocatorRegistry().get_page("progressive_fao", "communications_memo")
        self.assertIsNotNone(page)
        selectors = []
        for field in page.fields.values():
            field.validate()
            selectors.append(field.primary_selector)
            if field.fallback_selector:
                selectors.append(field.fallback_selector)
        manage = page.get_field("manage_policies")
        activity = page.get_field("policy_activity")
        self.assertEqual(manage.primary_strategy, "css")
        self.assertEqual(manage.primary_selector, MANAGE_POLICIES_CSS)
        self.assertEqual(manage.fallback_selector, "link:Manage Policies Home")
        self.assertEqual(manage.name_pattern, MANAGE_POLICIES_NAME.pattern)
        self.assertTrue(manage.exact)
        drawer = page.get_field("main_nav_drawer")
        self.assertEqual(drawer.primary_strategy, "css")
        self.assertEqual(drawer.primary_selector, HEADER_DRAWER_OPEN_CSS)
        self.assertIn("header-drawer__content--show", drawer.primary_selector)
        self.assertIn("Policy Activity", drawer.description)
        self.assertIn("Agency Admin", drawer.description)
        self.assertEqual(page.version, "1.8")
        view = page.get_field("view_activity_by")
        self.assertIn("#606", view.description)
        self.assertIn("does not ask Gemini", view.description)
        self.assertIn("Zero matches", view.description)
        self.assertIn("PROCESSEDDATE", view.description)
        self.assertIn("not click targets", view.description)
        self.assertIn("/managepolicies/policyactivity", view.description)
        self.assertEqual(view.primary_selector, VIEW_ACTIVITY_BY_CSS)
        self.assertEqual(activity.primary_selector, "link:" + POLICY_ACTIVITY_NAMES[0])
        self.assertEqual(activity.fallback_selector, "link:" + POLICY_ACTIVITY_NAMES[1])
        self.assertTrue(activity.exact)
        self.assertNotIn("tab:Communications", selectors)
        self.assertNotIn("Processed date from", selectors)
        self.assertNotIn("Processed date to", selectors)
        expected = {
            "view_activity_by": (VIEW_ACTIVITY_BY_CSS, VIEW_ACTIVITY_BY_LABEL),
            "processed_date_option": (PROCESSED_DATE_OPTION_CSS, PROCESSED_DATE_OPTION_LABEL),
            "processed_date_range": (PROCESSED_DATE_RANGE_CSS, ""),
            "processed_date_from": (START_DATE_CSS, START_DATE_LABEL),
            "processed_date_to": (END_DATE_CSS, END_DATE_LABEL),
            "get_policy_activity": (GET_POLICY_ACTIVITY_CSS, GET_POLICY_ACTIVITY_LABEL),
        }
        for name, (selector, accessible) in expected.items():
            field = page.get_field(name)
            self.assertIsNotNone(field, name)
            self.assertEqual(field.primary_strategy, "css")
            self.assertEqual(field.primary_selector, selector)
            self.assertEqual(field.accessible_name, accessible)
            self.assertFalse(field.fallback_selector)
        date_range = page.get_field("processed_date_range")
        self.assertEqual(CUSTOM_DATE_RANGE_LABEL, "Select Date Range")
        self.assertIn(CUSTOM_DATE_RANGE_LABEL, date_range.description)
        self.assertIn("select#PDDateRange", date_range.primary_selector)
        self.assertIn(PROCESSED_DATE_OPTION_VALUE, page.get_field("processed_date_option").primary_selector)
        self.assertIsNone(page.get_field("search"))
        self.assertNotIn("button:Search", selectors)
        results = page.get_field("processed_date_results")
        self.assertEqual(results.primary_selector, "Policy Activity Processed Date Results")
        self.assertIn("processeddateresults/cancels/", results.description)
        self.assertIn("does not click Search", results.description)
        self.assertIsNone(page.get_field("communications_tab"))
        communications = page.get_field("communications_link")
        self.assertEqual(communications.primary_strategy, "css")
        self.assertEqual(communications.primary_selector, COMMUNICATIONS_LINK_CSS)
        self.assertEqual(communications.fallback_selector, "link:" + COMMUNICATIONS_LINK_LABEL)
        self.assertEqual(communications.accessible_name, COMMUNICATIONS_LINK_LABEL)
        self.assertIn("does not read aria-selected", communications.description)
        self.assertIn("policy-activity-tab-communications", communications.description)
        self.assertIn("underwriting", communications.description)
        self.assertIn("does not require role=tab", communications.description)
        self.assertIn("link:" + COMMUNICATIONS_LINK_LABEL, selectors)
        self.assertIsNotNone(MANAGE_POLICIES_NAME.search("Manage Policies"))
        self.assertIsNotNone(MANAGE_POLICIES_NAME.search("Manage Policies Home"))
        self.assertIsNone(MANAGE_POLICIES_NAME.search("Menu Manage Policies"))
        self.assertIsNone(MANAGE_POLICIES_NAME.search("manage policies home"))
        for selector in selectors:
            self.assertFalse(locator_is_positional_guess(selector))

    def test_one_fao_tab_is_required(self):
        app = SimpleNamespace(url=LIST_URL)
        login = SimpleNamespace(url="https://foragentsonlylogin.progressive.com/login")
        other = SimpleNamespace(url="https://ezlynx.com/web/")
        self.assertIs(select_fao_page([login, other, app]), app)
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_fao_page([app, SimpleNamespace(url=LIST_URL + "/other")])
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_fao_page([login])

    def test_remote_cdp_is_refused(self):
        with self.assertRaises(IntakeHold):
            require_loopback_cdp("http://10.0.0.5:9222")
        self.assertEqual(require_loopback_cdp("http://127.0.0.1:9222"), "http://127.0.0.1:9222")

    def test_module_has_no_ezlynx_write_api(self):
        text = Path("robie_job_engine/progressive_fao_memo.py").read_text(encoding="utf-8")
        for banned in (
            "upload_applicant_document",
            "add_note_to_discussion",
            "create_task_once",
            "DocumentApi",
            "DiscussionApi",
            "googleapiclient",
            "GoogleDriveUploader",
        ):
            self.assertNotIn(banned, text)
        self.assertIn("success path does not call Gemini", text)
        self.assertIn("View Activity By does not ask Gemini", text)
        self.assertIn("does not call Jev", text)
        self.assertIn("gemini-api-key", text)
        self.assertIn("TypeSafe System One", text)
        self.assertNotIn("JEV_", text)
        self.assertNotIn("typesafe", text.casefold().replace("typesafe system one", ""))


_CSS_ATTR = re.compile(r'\[([A-Za-z_][\w-]*)="([^"]*)"\]')
_CSS_ID = re.compile(r"#([A-Za-z_][\w-]*)")
_CSS_TAG = re.compile(r"[A-Za-z][\w-]*")
_CSS_CLASS = re.compile(r"\.([A-Za-z_][\w-]*)")


def _css_match(node, selector):
    body = selector
    tag = None
    if body[:1].isalpha():
        tag_match = _CSS_TAG.match(body)
        if tag_match is None:
            raise KeyError(selector)
        tag = tag_match.group(0)
        body = body[tag_match.end():]
    element_id = None
    attrs = {}
    classes = []
    while body:
        if body.startswith("#") and element_id is None:
            id_match = _CSS_ID.match(body)
            if id_match is None:
                raise KeyError(selector)
            element_id = id_match.group(1)
            body = body[id_match.end():]
            continue
        if body.startswith("."):
            class_match = _CSS_CLASS.match(body)
            if class_match is None:
                raise KeyError(selector)
            classes.append(class_match.group(1))
            body = body[class_match.end():]
            continue
        if body.startswith("["):
            attr_match = _CSS_ATTR.match(body)
            if attr_match is None:
                raise KeyError(selector)
            key = attr_match.group(1)
            if key in attrs:
                raise KeyError(selector)
            attrs[key] = attr_match.group(2)
            body = body[attr_match.end():]
            continue
        raise KeyError(selector)
    if tag is None and element_id is None and not attrs and not classes:
        raise KeyError(selector)
    if tag is not None and node.role != tag and not (tag == "a" and node.role == "link"):
        return False
    if element_id is not None and node.attrs.get("id") != element_id:
        return False
    if classes:
        node_classes = set(str(node.attrs.get("class") or "").split())
        if any(token not in node_classes for token in classes):
            return False
    return all(node.attrs.get(key) == value for key, value in attrs.items())


class FakeNode:
    def __init__(self, role, name="", text=None, children=None, attrs=None, disabled=False, visible=True, label=""):
        self.role = role
        self.name = name
        self.text = name if text is None else text
        self.children = children or []
        self.attrs = attrs or {}
        self.disabled = disabled
        self.visible = visible
        self.label = label
        self.value = ""
        self.input_override = None

    def find(self, selector):
        found = [self] if self.matches(selector) else []
        for child in self.children:
            found.extend(child.find(selector))
        return found

    def find_role(self, role, name, exact):
        found = []
        if self.role == role and self._name_matches(name, exact):
            found.append(self)
        for child in self.children:
            found.extend(child.find_role(role, name, exact))
        return found

    def find_label(self, label, exact):
        own = self.attrs.get("aria-label") or self.label
        found = []
        if own and ((own == label) if exact else label in own):
            found.append(self)
        for child in self.children:
            found.extend(child.find_label(label, exact))
        return found

    def _name_matches(self, name, exact):
        if name is None:
            return True
        if hasattr(name, "search"):
            return name.search(self.name or "") is not None
        if exact:
            return self.name == name
        return str(name) in (self.name or "")

    def matches(self, selector):
        if selector == MANAGE_POLICIES_CSS:
            return self.role == "link" and self.attrs.get("data-at") == "header-nav__parent-link--manage-policies"
        known = {
            "table": self.role == "table",
            "thead": self.role == "thead",
            "tbody": self.role == "tbody",
            "th": self.role == "th",
            "td": self.role == "td",
            "tr": self.role == "tr",
            "body": self.role == "body",
            "iframe": self.role == "iframe",
            "a": self.role == "link",
            "embed[type='application/pdf']": self.role == "embed" and self.attrs.get("type") == "application/pdf",
            "input[type='password']": self.attrs.get("type") == "password",
        }
        if selector in known:
            return known[selector]
        return _css_match(self, selector)


class NodeLocator:
    def __init__(self, nodes, page):
        self.nodes = list(nodes)
        self.page = page

    def count(self):
        return len(self.nodes)

    def click(self):
        node = self.nodes[0]
        self.page.clicks.append(node.name)
        self.page.on_click(node)

    def fill(self, value):
        node = self.nodes[0]
        if not node.visible:
            self.page.filled_while_hidden.append(node)
        node.value = value

    def input_value(self):
        node = self.nodes[0]
        return node.value if node.input_override is None else node.input_override

    def inner_text(self, timeout=None):
        return self.nodes[0].text

    def get_attribute(self, name):
        return self.nodes[0].attrs.get(name)

    def is_disabled(self):
        return self.nodes[0].disabled

    def is_visible(self, timeout=None):
        return len(self.nodes) == 1 and bool(self.nodes[0].visible)

    def wait_for(self, state="visible", timeout=None):
        if (
            state == "visible"
            and len(self.nodes) == 1
            and not self.nodes[0].visible
            and getattr(self.nodes[0], "reveal_on_wait", False)
        ):
            self.nodes[0].visible = True
            return None
        if state != "visible" or len(self.nodes) != 1 or not self.nodes[0].visible:
            raise TimeoutError("visible control did not appear")

    def nth(self, index):
        return NodeLocator([self.nodes[index]], self.page)

    def or_(self, other):
        merged = []
        for node in (*self.nodes, *other.nodes):
            if node not in merged:
                merged.append(node)
        return NodeLocator(merged, self.page if self.page is not None else other.page)

    def and_(self, other):
        other_nodes = list(other.nodes)
        merged = [node for node in self.nodes if node in other_nodes]
        return NodeLocator(merged, self.page if self.page is not None else other.page)

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

    def evaluate(self, expression):
        if "aria-label" not in expression or "labels" not in expression:
            raise AssertionError(expression)
        node = self.nodes[0]
        aria = node.attrs.get("aria-label")
        if aria:
            return aria
        return node.label or ""

    def select_option(self, value=None, label=None):
        if len(self.nodes) != 1:
            raise TimeoutError("select_option expected one element")
        node = self.nodes[0]
        chosen = []
        for child in node.children:
            if child.role != "option":
                continue
            if value is not None and child.attrs.get("value") == value:
                chosen.append(child)
            elif label is not None and child.text == label:
                chosen.append(child)
        if len(chosen) != 1:
            raise TimeoutError("select_option missed")
        node.value = chosen[0].attrs.get("value", "")
        self.page.on_select(node, chosen[0])
        return [node.value]


class NavPage:
    def __init__(self, *, body="StreetSmart Risk Mgr CA33617", url=LIST_URL, password=False,
                 duplicate_manage=False, next_mode="disabled", open_mode="download",
                 date_override=None, go_back_restores=True, manage_name="Manage Policies",
                 manage_role="link", manage_visible=True, manage_attrs=None,
                 extra_manage=None, policy_name="Policy Activity", policy_role="link",
                 policy_visible=True, policy_items=None, include_main_nav=True,
                 duplicate_main_nav=False, main_nav_visible=True,
                 drawer_mode=None, drawer_open=False, drawer_on_main_nav=False,
                 view_mode="ok", option_mode="ok", range_mode="reveal",
                 form_url_on_wait=False,
                 start_mode="ok", end_mode="ok", button_mode="ok",
                 legacy_dates=False, results_section="cancels", results_url=None,
                 results_mode="navigate", comms_mode="ok", search_on_results=False,
                 communications_section="underwriting"):
        self.url = url
        self.list_url = url
        self.body_text = body
        self.password = password
        self.duplicate_manage = duplicate_manage
        self.next_mode = next_mode
        self.open_mode = open_mode
        self.date_override = date_override
        self.go_back_restores = go_back_restores
        self.manage_name = manage_name
        self.manage_role = manage_role
        self.manage_visible = manage_visible
        self.manage_attrs = manage_attrs or {}
        self.extra_manage = extra_manage
        self.policy_name = policy_name
        self.policy_role = policy_role
        self.policy_visible = policy_visible
        self.policy_items = policy_items
        self.include_main_nav = include_main_nav
        self.duplicate_main_nav = duplicate_main_nav
        self.main_nav_visible = main_nav_visible
        self.drawer_mode = drawer_mode
        self.drawer_open = drawer_open
        self.drawer_on_main_nav = drawer_on_main_nav
        self.view_mode = view_mode
        self.form_url_on_wait = form_url_on_wait
        self.option_mode = option_mode
        self.range_mode = range_mode
        self.start_mode = start_mode
        self.end_mode = end_mode
        self.button_mode = button_mode
        self.legacy_dates = legacy_dates
        self.results_section = results_section
        self.results_mode = results_mode
        self.comms_mode = comms_mode
        self.search_on_results = search_on_results
        self.communications_section = communications_section
        self.results_url = results_url or (
            "https://www.foragentsonly.com/managepolicies/policyactivity/"
            f"processeddateresults/{results_section}/"
        )
        self.clicks = []
        self.escape_presses = []
        self.keyboard = _NavKeyboard(self)
        self.filled_while_hidden = []
        self.memo_opens = []
        self.screenshot_calls = 0
        self.closed = False
        self.pending_download = None
        self.state = "home"
        self.table_visible = False
        self.context = NavContext(self)
        self._build()

    def _build(self):
        self.body = FakeNode("body", text=self.body_text)
        self.password_node = FakeNode("input", attrs={"type": "password"})
        manage = [FakeNode(
            self.manage_role,
            self.manage_name,
            attrs=dict(self.manage_attrs),
            visible=self.manage_visible,
        )]
        if self.duplicate_manage:
            manage.append(FakeNode(
                self.manage_role,
                self.manage_name,
                attrs=dict(self.manage_attrs),
                visible=self.manage_visible,
            ))
        if self.extra_manage is not None:
            role, name, visible = self.extra_manage
            manage.append(FakeNode(role, name, visible=visible))
        self.home = manage
        if self.policy_items is None:
            self.policies = [FakeNode(self.policy_role, self.policy_name, visible=self.policy_visible)]
        else:
            self.policies = [
                FakeNode(self.policy_role, name, visible=visible)
                for name, visible in self.policy_items
            ]
        self.main_nav_nodes = []
        if self.include_main_nav:
            nav_attrs = {"aria-expanded": "true"} if self.drawer_mode == "toggle" else {}
            self.main_nav_nodes.append(
                FakeNode(
                    "button",
                    MAIN_NAVIGATION_NAME,
                    visible=self.main_nav_visible,
                    attrs=nav_attrs,
                )
            )
            if self.duplicate_main_nav:
                self.main_nav_nodes.append(
                    FakeNode("button", MAIN_NAVIGATION_NAME, visible=self.main_nav_visible)
                )
        self.drawer = None
        self.drawer_close = None
        self.drawer_overlay = None
        self.agency_admin = None
        if self.drawer_mode:
            close_attrs = {"aria-label": "Close"}
            if self.drawer_mode != "panel-close":
                close_attrs = {
                    "class": "header-drawer__close",
                    "aria-label": "Close",
                    "data-at": "header-drawer-close",
                }
            self.drawer_close = FakeNode("button", "Close", attrs=close_attrs)
            self.drawer_overlay = FakeNode(
                "div",
                "Drawer overlay",
                attrs={
                    "class": "header-drawer__overlay",
                    "data-at": "header-drawer-overlay",
                },
            )
            self.agency_admin = FakeNode("link", AGENCY_ADMIN_NAME)
            shown = " header-drawer__content--show" if self.drawer_open else ""
            self.drawer = FakeNode(
                "div",
                AGENCY_ADMIN_NAME,
                text=AGENCY_ADMIN_NAME,
                attrs={"class": "header-drawer__content" + shown},
                children=[self.agency_admin, self.drawer_close, self.drawer_overlay],
            )
        self.view_nodes = self._view_nodes()
        start_label = {"unlabeled": "", "mistitled": "From Date"}.get(self.start_mode, START_DATE_LABEL)
        start_id = (
            "js-datepicker__date-start-alt"
            if self.start_mode == "alt"
            else "js-datepicker__date-start"
        )
        self.start_date = self._date_input(
            start_label,
            start_id,
            "datatable-daterangepicker-startdate",
        )
        if self.date_override is not None:
            self.start_date.input_override = self.date_override
        self.end_date = self._date_input(
            END_DATE_LABEL if self.end_mode != "unlabeled" else "",
            "js-datepicker__date-end",
            "datatable-daterangepicker-enddate",
        )
        self.get_activity = self._activity_button("Get Policy Activity" if self.button_mode != "misnamed" else "Apply")
        self.date_range = self._preset_select()
        self.preset_selects = []
        if self.range_mode != "missing":
            self.preset_selects.append(self.date_range)
            if self.range_mode == "duplicate":
                self.preset_selects.append(self._preset_select())
        self.page_dates = []
        if self.start_mode != "missing":
            self.page_dates.append(self.start_date)
            if self.start_mode == "duplicate":
                self.page_dates.append(self._date_input(
                    START_DATE_LABEL, "js-datepicker__date-start", "datatable-daterangepicker-startdate",
                ))
        if self.start_mode == "outside":
            self.page_dates.append(self._date_input(
                START_DATE_LABEL, "js-datepicker__date-start", "datatable-daterangepicker-startdate",
            ))
        if self.end_mode != "missing":
            self.page_dates.append(self.end_date)
            if self.end_mode == "duplicate":
                self.page_dates.append(self._date_input(
                    END_DATE_LABEL, "js-datepicker__date-end", "datatable-daterangepicker-enddate",
                ))
        self.page_buttons = []
        if self.button_mode != "missing":
            self.page_buttons.append(self.get_activity)
            if self.button_mode == "duplicate":
                self.page_buttons.append(self._activity_button("Get Policy Activity"))
        self.search_button = FakeNode("button", "Search")
        self.activity_extras = []
        if self.legacy_dates:
            self.legacy_from = FakeNode("textbox", "Processed date from", label="Processed date from")
            self.legacy_to = FakeNode("textbox", "Processed date to", label="Processed date to")
            self.activity_extras.extend([self.legacy_from, self.legacy_to])
        if self.view_mode == "split-label":
            self.activity_extras.append(FakeNode("div", VIEW_ACTIVITY_BY_LABEL, label=VIEW_ACTIVITY_BY_LABEL))
        self.communications_nodes = self._communications_nodes()
        self.rows = []
        body_rows = []
        for policy, insured, reason, memo_type, processed in PROVE_ROWS[:2]:
            link = FakeNode("link", "Memo", attrs={"policy": policy})
            cells = [
                FakeNode("td", text=policy),
                FakeNode("td", text=insured),
                FakeNode("td", text=reason),
                FakeNode("td", text=memo_type),
                FakeNode("td", text=processed),
                FakeNode("td", text="Memo", children=[link]),
            ]
            row = FakeNode("tr", children=cells)
            self.rows.append(row)
            body_rows.append(row)
        headers = [FakeNode("th", text=header) for header in HEADERS]
        self.table = FakeNode("table", children=[
            FakeNode("thead", children=[FakeNode("tr", children=headers)]),
            FakeNode("tbody", children=body_rows),
        ])
        self.next_nodes = []
        if self.next_mode == "disabled":
            self.next_nodes.append(FakeNode("button", "Next", attrs={"aria-disabled": "true"}, disabled=True))
        elif self.next_mode == "enabled":
            self.next_nodes.append(FakeNode("button", "Next", attrs={"aria-disabled": "false"}, disabled=False))
        elif self.next_mode == "ambiguous":
            self.next_nodes.extend([
                FakeNode("button", "Next", disabled=False),
                FakeNode("link", "Next", disabled=False),
            ])

    def drawer_is_shown(self):
        if self.drawer is None:
            return False
        return "header-drawer__content--show" in str(self.drawer.attrs.get("class") or "").split()

    def close_drawer(self):
        if self.drawer is None or self.drawer_mode == "stuck":
            return
        classes = [
            token for token in str(self.drawer.attrs.get("class") or "").split()
            if token != "header-drawer__content--show"
        ]
        self.drawer.attrs["class"] = " ".join(classes)
        self.drawer.visible = False

    def open_drawer(self):
        if self.drawer is None:
            return
        classes = set(str(self.drawer.attrs.get("class") or "").split())
        classes.add("header-drawer__content")
        classes.add("header-drawer__content--show")
        self.drawer.attrs["class"] = " ".join(sorted(classes))
        self.drawer.visible = True

    def roots(self):
        nodes = [self.body, *self.main_nav_nodes]
        if self.drawer_is_shown():
            nodes.append(self.drawer)
        if self.password:
            nodes.append(self.password_node)
        nodes.extend({
            "home": self.home,
            "policies": self.policies,
            "activity": self._activity_roots(),
            "results": self._results_roots(),
            "comms": self._results_roots(),
        }[self.state])
        if self.state == "comms" and self.table_visible:
            nodes.append(self.table)
            nodes.extend(self.next_nodes)
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

    def get_by_label(self, label, exact=True):
        found = []
        for node in self.roots():
            found.extend(node.find_label(label, exact))
        return NodeLocator(found, self)

    def _activity_roots(self):
        return [
            *self.view_nodes,
            *self.activity_extras,
            *self.preset_selects,
            *self.page_dates,
            *self.page_buttons,
        ]

    def _communications_attrs(self):
        return {"data-at": "policy-activity-tab-communications"}

    def _communications_nodes(self):
        attrs = self._communications_attrs()
        mode = self.comms_mode
        if mode == "missing":
            return []
        if mode == "tab":
            return [FakeNode("tab", "Communications", attrs={"aria-selected": "false"})]
        if mode == "named":
            return [FakeNode("link", "Communications")]
        if mode == "unlabeled":
            return [FakeNode("link", "", text="", attrs=dict(attrs))]
        if mode == "mistitled":
            return [FakeNode("link", "Alerts", text="Alerts", attrs=dict(attrs))]
        if mode == "mislabeled":
            return [FakeNode("link", "Communications", attrs={**attrs, "aria-label": "Notices"})]
        if mode == "hidden":
            return [FakeNode("link", "Communications", attrs=dict(attrs), visible=False)]
        if mode == "duplicate":
            return [
                FakeNode("link", "Communications", attrs=dict(attrs)),
                FakeNode("link", "Communications", attrs=dict(attrs)),
            ]
        if mode == "split":
            return [
                FakeNode("link", "Communications", attrs=dict(attrs)),
                FakeNode("link", "Communications"),
            ]
        if mode == "foreign-label":
            return [
                FakeNode("link", "Communications", attrs=dict(attrs)),
                FakeNode("span", "Elsewhere", label=COMMUNICATIONS_LINK_LABEL),
            ]
        if mode == "tab-and-link":
            return [
                FakeNode("tab", "Communications", attrs={"aria-selected": "false"}),
                FakeNode("link", "Communications", attrs=dict(attrs)),
            ]
        return [FakeNode("link", "Communications", attrs=dict(attrs))]

    @property
    def communications_url(self):
        return (
            "https://www.foragentsonly.com/managepolicies/policyactivity/"
            f"processeddateresults/{self.communications_section}/"
        )

    def _results_roots(self):
        nodes = list(self.communications_nodes)
        if self.search_on_results:
            nodes.append(self.search_button)
        return nodes

    def wait_for_url(self, url, timeout=None):
        pattern = getattr(url, "pattern", "")
        if self.form_url_on_wait and pattern.endswith("policyactivity/?$"):
            self.url = LIST_URL
            self.form_url_on_wait = False
        if hasattr(url, "search"):
            if url.search(self.url):
                return None
        elif str(url) == self.url:
            return None
        raise TimeoutError("processed-date results page did not appear")

    def _view_nodes(self):
        if self.view_mode == "missing":
            return []
        label = "" if self.view_mode in {"unlabeled", "split-label"} else VIEW_ACTIVITY_BY_LABEL
        if self.view_mode == "mistitled":
            label = "Activity By"
        attrs = {"id": "PDDateType", "name": "DateKind" if self.view_mode == "renamed" else "DateType"}
        nodes = [self._view_select(label, attrs)]
        if self.view_mode == "hidden":
            nodes[0].visible = False
        if self.view_mode == "late":
            nodes[0].visible = False
            nodes[0].reveal_on_wait = True
        if self.view_mode == "duplicate":
            nodes.append(self._view_select(label, dict(attrs)))
        if self.view_mode == "extra-name":
            nodes.append(self._view_select("", {"id": "OtherDateType", "name": "DateType"}))
        return nodes

    def _view_select(self, label, attrs):
        return FakeNode(
            "select",
            VIEW_ACTIVITY_BY_LABEL,
            label=label,
            attrs=attrs,
            children=self._processed_options(),
        )

    def _processed_options(self):
        options = [
            FakeNode("option", "Select", text="Select", attrs={"value": "SELECT"}),
            FakeNode("option", "Effective Date", text="Effective Date", attrs={"value": "EFFECTIVEDATE"}),
        ]
        if self.option_mode != "missing":
            text = "Posted Date" if self.option_mode == "mistyped" else PROCESSED_DATE_OPTION_LABEL
            options.append(FakeNode("option", text, text=text, attrs={"value": PROCESSED_DATE_OPTION_VALUE}))
        if self.option_mode == "duplicate":
            options.append(FakeNode(
                "option", PROCESSED_DATE_OPTION_LABEL, text=PROCESSED_DATE_OPTION_LABEL,
                attrs={"value": PROCESSED_DATE_OPTION_VALUE},
            ))
        if self.option_mode == "duplicate-text":
            options.append(FakeNode(
                "option", PROCESSED_DATE_OPTION_LABEL, text=PROCESSED_DATE_OPTION_LABEL,
                attrs={"value": "OTHERDATE"},
            ))
        return options

    def _date_input(self, label, element_id, data_at):
        attrs = {"type": "date", "data-at": data_at}
        if element_id:
            attrs["id"] = element_id
        return FakeNode("input", label or "date", label=label, attrs=attrs, visible=False)

    def _preset_options(self):
        options = [
            FakeNode("option", "Yesterday", text="Yesterday", attrs={"value": FIXTURE_PRESET_YESTERDAY}),
            FakeNode("option", "Last 30 Days", text="Last 30 Days", attrs={"value": FIXTURE_PRESET_LAST_30}),
        ]
        if self.range_mode == "missing-custom":
            return options
        value = "" if self.range_mode == "blank-custom" else FIXTURE_CUSTOM_RANGE_VALUE
        options.append(FakeNode(
            "option", CUSTOM_DATE_RANGE_LABEL, text=CUSTOM_DATE_RANGE_LABEL, attrs={"value": value},
        ))
        if self.range_mode == "duplicate-custom":
            options.append(FakeNode(
                "option", CUSTOM_DATE_RANGE_LABEL, text=CUSTOM_DATE_RANGE_LABEL, attrs={"value": "5"},
            ))
        if self.range_mode == "duplicate-value":
            options.append(FakeNode("option", "Custom", text="Custom", attrs={"value": value}))
        return options

    def _preset_select(self):
        return FakeNode("select", "PDDateRange", attrs={"id": "PDDateRange"}, children=self._preset_options())

    def _activity_button(self, value):
        return FakeNode(
            "input",
            value,
            text=value,
            attrs={"type": "submit", "data-at": "ProcessedDateButton", "value": value},
        )

    def screenshot(self, full_page=True, type="png"):
        self.screenshot_calls += 1
        if full_page and type == "png" and self.table_visible and self.state == "comms":
            return LIST_PNG
        return b""

    def expect_download(self, timeout):
        return _NavExpect(self)

    def go_back(self):
        if self.go_back_restores:
            self.url = self.list_url

    def on_click(self, node):
        if (
            self.drawer is not None
            and node is self.drawer_close
            and self.drawer_mode in {"close", "panel-close"}
        ):
            self.close_drawer()
            return
        if self.drawer is not None and node is self.drawer_overlay and self.drawer_mode == "overlay":
            self.close_drawer()
            return
        if self.drawer is not None and node in (self.drawer_close, self.drawer_overlay, self.agency_admin):
            return
        if node in self.main_nav_nodes:
            for item in (*self.home, *self.policies):
                item.visible = True
            if self.drawer_on_main_nav:
                self.open_drawer()
            if self.drawer_mode == "toggle":
                self.close_drawer()
            return
        if node in self.home and self.state == "home":
            self.state = "policies"
        elif node in self.policies and self.state == "policies":
            self.state = "activity"
        elif node is self.get_activity and self.state == "activity" and self.results_mode == "navigate":
            self.state = "results"
            self.url = self.results_url
            self.list_url = self.results_url
        elif (
            node.role == "link"
            and node in self.communications_nodes
            and self.state == "results"
            and self.comms_mode != "stay"
        ):
            self.state = "comms"
            self.table_visible = True
            self.url = self.communications_url
            self.list_url = self.communications_url
        elif node.name == "Memo":
            policy = node.attrs["policy"]
            self.memo_opens.append(policy)
            token = pdf_bytes(policy.encode())
            if self.open_mode == "download":
                self.pending_download = SimpleNamespace(save_as=lambda path, token=token: Path(path).write_bytes(token))
            elif self.open_mode == "tab":
                self.context.add_page(_pdf_tab(token))
            elif self.open_mode == "same":
                self.url = "https://www.foragentsonly.com/memos/" + policy + ".pdf"
                self.pending_download = None
                self.context.request_body = token
            elif self.open_mode == "same-stuck":
                self.url = "https://www.foragentsonly.com/memos/" + policy + ".pdf"
                self.pending_download = None
                self.context.request_body = token
                self.go_back_restores = False

    def on_select(self, node, option):
        self.clicks.append(option.text)
        if (
            node is self.date_range
            and option.text == CUSTOM_DATE_RANGE_LABEL
            and self.range_mode != "stuck"
        ):
            for item in self.page_dates:
                item.visible = True


class NavContext:
    def __init__(self, page):
        self.page = page
        self.listeners = []
        self.request_body = b""
        self.request = SimpleNamespace(get=self._get)

    def on(self, event, fn):
        if event == "page":
            self.listeners.append(fn)

    def remove_listener(self, event, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    def add_page(self, page):
        for fn in list(self.listeners):
            fn(page)

    def _get(self, url, timeout):
        return SimpleNamespace(ok=True, body=lambda: self.request_body)


class _NavKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.escape_presses.append(key)
        if key == "Escape" and self.page.drawer_mode == "escape":
            self.page.close_drawer()


class _NavExpect:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        if self.page.pending_download is None:
            raise TimeoutError("Timeout exceeded while waiting for download")
        return False

    @property
    def value(self):
        return self.page.pending_download


def _pdf_tab(token: bytes):
    return SimpleNamespace(
        url="https://www.foragentsonly.com/memos/opened.pdf",
        context=SimpleNamespace(request=SimpleNamespace(get=lambda url, timeout: SimpleNamespace(ok=True, body=lambda: token))),
        close=lambda: None,
        wait_for_load_state=lambda *args, **kwargs: None,
        locator=lambda selector: NodeLocator([], None),
        evaluate=lambda script, url: None,
    )


class _BodyPage:
    def __init__(self, body: str):
        self._body = body

    def locator(self, selector):
        if selector != "body":
            raise AssertionError(selector)
        return SimpleNamespace(inner_text=lambda: self._body)


class AgentContextTests(unittest.TestCase):
    def test_ca33617_alone_is_accepted(self):
        assert_agent_context(_BodyPage("StreetSmart Risk Mgr CA33617"), DEFAULT_AGENT_CODE)

    def test_parenthesized_agency_and_login_id_are_the_same_agent(self):
        bodies = (
            "Streetsmart Risk Mgr (33617)",
            "Welcome, Carlo Ferrara\n33617c",
            "Streetsmart Risk Mgr (33617)\nWelcome, Carlo Ferrara\n33617c",
            "33617",
            "CA33617 (33617) 33617c",
        )
        for body in bodies:
            with self.subTest(body=body):
                assert_agent_context(_BodyPage(body), DEFAULT_AGENT_CODE)

    def test_empty_or_unrecognized_body_holds(self):
        for body in ("", "Welcome, Carlo Ferrara", "   "):
            with self.subTest(body=body):
                with self.assertRaisesRegex(IntakeHold, "agent context is missing or ambiguous"):
                    assert_agent_context(_BodyPage(body), DEFAULT_AGENT_CODE)

    def test_multiple_distinct_agencies_hold(self):
        bodies = (
            "CA33617 CA11111",
            "Streetsmart Risk Mgr (33617) CA11111",
            "(33617) (11111)",
            "33617c 99999c",
            "CA33617 (11111)",
        )
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaisesRegex(IntakeHold, "agent context is missing or ambiguous"):
                    assert_agent_context(_BodyPage(body), DEFAULT_AGENT_CODE)

    def test_street_smart_display_does_not_satisfy_a_different_agent(self):
        with self.assertRaisesRegex(IntakeHold, "agent context is missing or ambiguous"):
            assert_agent_context(_BodyPage("Streetsmart Risk Mgr (33617) 33617c"), "CA11111")

    def test_unrelated_bare_five_digit_token_is_not_a_second_agency(self):
        assert_agent_context(
            _BodyPage("Streetsmart Risk Mgr (33617)\nTampa FL 90210"),
            DEFAULT_AGENT_CODE,
        )


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_communications_list_screenshot_is_a_full_page_png(self):
        page = NavPage(next_mode="none")
        browser = PlaywrightFaoMemoBrowser(page)
        browser.load_communications(start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE)
        self.assertEqual(browser.screenshot_communications(), LIST_PNG)
        self.assertEqual(page.screenshot_calls, 1)

    def test_navigation_order_date_readback_and_selected_memo(self):
        page = NavPage()
        browser = PlaywrightFaoMemoBrowser(page)
        grid = browser.load_communications(start=PROVE_DAY, end=date(2026, 9, 26), agent_code=DEFAULT_AGENT_CODE)
        memos = parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)
        self.assertEqual([memo.policy_number for memo in memos], ["860521214", "879512352"])
        self.assertEqual(page.start_date.value, "2026-09-25")
        self.assertEqual(page.end_date.value, "2026-09-26")
        observation = browser.capture_memo(memos[1].document_id)
        self.assertEqual(pdf_bytes_from_observation(observation), pdf_bytes(b"879512352"))
        self.assertEqual(page.memo_opens, ["879512352"])
        self.assertEqual(page.date_range.role, "select")
        self.assertEqual(page.date_range.value, FIXTURE_CUSTOM_RANGE_VALUE)
        self.assertNotEqual(page.date_range.value, FIXTURE_PRESET_YESTERDAY)
        self.assertNotIn(page.start_date, page.date_range.children)
        self.assertNotIn(page.end_date, page.date_range.children)
        self.assertNotIn(page.get_activity, page.date_range.children)
        self.assertTrue(page.start_date.visible)
        self.assertTrue(page.end_date.visible)
        self.assertEqual(page.filled_while_hidden, [])
        self.assertEqual(page.clicks, [
            "Manage Policies",
            "Policy Activity",
            "Processed Date",
            CUSTOM_DATE_RANGE_LABEL,
            "Get Policy Activity",
            "Communications",
            "Memo",
        ])
        self.assertNotIn("Search", page.clicks)
        self.assertEqual(page.url, RESULTS_UNDERWRITING_URL)
        self.assertNotIn(MAIN_NAVIGATION_NAME, page.clicks)
        self.assertEqual(page.escape_presses, [])
        self.assertFalse(page.closed)

    def test_live_home_agency_display_without_ca_prefix_reaches_communications(self):
        page = NavPage(body="Streetsmart Risk Mgr (33617)\nWelcome, Carlo Ferrara\n33617c")
        browser = PlaywrightFaoMemoBrowser(page)
        grid = browser.load_communications(start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE)
        self.assertEqual(len(grid.rows), 2)
        self.assertNotIn("Search", page.clicks)
        self.assertIn("Communications", page.clicks)
        self.assertEqual(page.url, RESULTS_UNDERWRITING_URL)

    def _load(self, page):
        return PlaywrightFaoMemoBrowser(page).load_communications(
            start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE,
        )

    def test_manage_policies_home_hidden_until_main_navigation(self):
        attrs = {"data-at": "header-nav__parent-link--manage-policies"}
        hidden = NavPage(manage_name="Manage Policies Home", manage_attrs=attrs, manage_visible=False)
        self._load(hidden)
        self.assertEqual(
            hidden.clicks[:3],
            [MAIN_NAVIGATION_NAME, "Manage Policies Home", "Policy Activity"],
        )
        self.assertEqual(hidden.clicks.count(MAIN_NAVIGATION_NAME), 1)

        visible = NavPage(manage_name="Manage Policies Home", manage_attrs=attrs, manage_visible=True)
        self._load(visible)
        self.assertEqual(visible.clicks[0], "Manage Policies Home")
        self.assertNotIn(MAIN_NAVIGATION_NAME, visible.clicks)

    def test_manage_policies_data_at_matches_without_the_accessible_name(self):
        page = NavPage(
            manage_name="Menu Manage Policies",
            manage_attrs={"data-at": "header-nav__parent-link--manage-policies"},
        )
        self._load(page)
        self.assertEqual(page.clicks[0], "Menu Manage Policies")
        self.assertNotIn(MAIN_NAVIGATION_NAME, page.clicks)

        button = NavPage(manage_role="button", manage_name="Manage Policies Home")
        self._load(button)
        self.assertEqual(button.clicks[0], "Manage Policies Home")

    def test_manage_policies_ambiguity_stays_closed(self):
        cases = (
            ("two visible", NavPage(duplicate_manage=True), "Manage Policies", []),
            (
                "css and name are different elements",
                NavPage(
                    manage_name="Menu Manage Policies",
                    manage_attrs={"data-at": "header-nav__parent-link--manage-policies"},
                    extra_manage=("button", "Manage Policies Home", True),
                ),
                "Manage Policies",
                [],
            ),
            (
                "visible plus hidden",
                NavPage(manage_name="Manage Policies Home", extra_manage=("link", "Manage Policies", False)),
                "Manage Policies",
                [],
            ),
            (
                "two hidden",
                NavPage(duplicate_manage=True, manage_visible=False),
                "Manage Policies",
                [],
            ),
            (
                "missing expands once",
                NavPage(manage_name="Open Manage Policies"),
                "Manage Policies",
                [MAIN_NAVIGATION_NAME],
            ),
            (
                "hidden without main navigation",
                NavPage(manage_name="Manage Policies Home", manage_visible=False, include_main_nav=False),
                "Main Navigation",
                [],
            ),
            (
                "ambiguous main navigation",
                NavPage(manage_visible=False, duplicate_main_nav=True),
                "Main Navigation",
                [],
            ),
            (
                "hidden main navigation",
                NavPage(manage_visible=False, main_nav_visible=False),
                "Main Navigation",
                [],
            ),
        )
        for label, page, hold, clicks in cases:
            with self.subTest(label):
                with self.assertRaisesRegex(IntakeHold, hold):
                    self._load(page)
                self.assertEqual(page.clicks, clicks)
                self.assertNotIn("Search", page.clicks)

    def test_policy_activity_landing_label_and_collapsed_header(self):
        landing = NavPage(policy_name="View policy activity reports")
        self._load(landing)
        self.assertEqual(landing.clicks[1], "View policy activity reports")
        self.assertNotIn(MAIN_NAVIGATION_NAME, landing.clicks)

        collapsed = NavPage(policy_visible=False)
        self._load(collapsed)
        self.assertEqual(
            collapsed.clicks[:3],
            ["Manage Policies", MAIN_NAVIGATION_NAME, "Policy Activity"],
        )
        self.assertEqual(collapsed.clicks.count(MAIN_NAVIGATION_NAME), 1)

        both_hidden = NavPage(manage_visible=False, policy_visible=False)
        self._load(both_hidden)
        self.assertEqual(both_hidden.clicks.count(MAIN_NAVIGATION_NAME), 1)
        self.assertEqual(
            both_hidden.clicks[:3],
            [MAIN_NAVIGATION_NAME, "Manage Policies", "Policy Activity"],
        )

        sibling = NavPage(policy_items=(
            ("View policy activity reports", True),
            ("Policy Activity", False),
        ))
        self._load(sibling)
        self.assertEqual(sibling.clicks[1], "View policy activity reports")
        self.assertNotIn(MAIN_NAVIGATION_NAME, sibling.clicks)

        ambiguous = NavPage(policy_items=(
            ("View policy activity reports", True),
            ("Policy Activity", True),
        ))
        with self.assertRaisesRegex(IntakeHold, "Policy Activity"):
            self._load(ambiguous)
        self.assertEqual(ambiguous.clicks, ["Manage Policies"])
        self.assertNotIn("Search", ambiguous.clicks)

    def test_open_main_nav_drawer_is_cleared_before_policy_activity(self):
        cases = (
            ("escape", "Escape"),
            ("close", "Close"),
            ("panel-close", "Close"),
            ("overlay", "Drawer overlay"),
            ("toggle", MAIN_NAVIGATION_NAME),
        )
        for mode, marker in cases:
            with self.subTest(mode):
                page = NavPage(drawer_mode=mode, drawer_open=True)
                self._load(page)
                self.assertEqual(page.escape_presses, ["Escape"])
                self.assertNotIn(AGENCY_ADMIN_NAME, page.clicks)
                self.assertIn("Policy Activity", page.clicks)
                self.assertIn("Communications", page.clicks)
                self.assertFalse(page.drawer_is_shown())
                if mode == "escape":
                    self.assertNotIn("Close", page.clicks)
                    self.assertNotIn("Drawer overlay", page.clicks)
                else:
                    self.assertLess(page.clicks.index(marker), page.clicks.index("Policy Activity"))

    def test_stuck_main_nav_drawer_does_not_click_policy_activity(self):
        page = NavPage(drawer_mode="stuck", drawer_open=True)
        self.assertFalse(is_rescuable_control_failure(IntakeHold(MAIN_NAV_DRAWER_HOLD)))
        with patch("robie_job_engine.gemini_ui_rescue.rescued_locator") as rescued:
            with self.assertRaisesRegex(IntakeHold, "Policy Activity was not clicked"):
                self._load(page)
            rescued.assert_not_called()
        self.assertIn("Manage Policies", page.clicks)
        self.assertNotIn("Policy Activity", page.clicks)
        self.assertNotIn("Communications", page.clicks)
        self.assertNotIn("Memo", page.clicks)
        self.assertNotIn("Get Policy Activity", page.clicks)
        self.assertNotIn(AGENCY_ADMIN_NAME, page.clicks)
        self.assertTrue(page.drawer_is_shown())
        self.assertIn("header-drawer__content--show", str(page.drawer.attrs.get("class")))

    def test_drawer_opened_with_main_navigation_closes_before_policy_activity(self):
        page = NavPage(
            policy_visible=False,
            drawer_mode="escape",
            drawer_on_main_nav=True,
        )
        self._load(page)
        self.assertEqual(
            page.clicks[:3],
            ["Manage Policies", MAIN_NAVIGATION_NAME, "Policy Activity"],
        )
        self.assertEqual(page.escape_presses, ["Escape"])
        self.assertNotIn(AGENCY_ADMIN_NAME, page.clicks)
        self.assertFalse(page.drawer_is_shown())
        self.assertIn("Communications", page.clicks)

    def test_drawer_left_open_by_main_navigation_blocks_policy_activity(self):
        page = NavPage(
            policy_visible=False,
            drawer_mode="stuck",
            drawer_on_main_nav=True,
        )
        with patch("robie_job_engine.gemini_ui_rescue.rescued_locator") as rescued:
            with self.assertRaisesRegex(IntakeHold, "header-drawer__content--show"):
                self._load(page)
            rescued.assert_not_called()
        self.assertIn(MAIN_NAVIGATION_NAME, page.clicks)
        self.assertNotIn("Policy Activity", page.clicks)
        self.assertNotIn("Communications", page.clicks)
        self.assertTrue(page.drawer_is_shown())

    def test_login_password_wrong_agent_and_ambiguous_controls_do_not_search(self):
        cases = (
            NavPage(url="https://foragentsonlylogin.progressive.com/"),
            NavPage(password=True),
            NavPage(body="CA99999"),
            NavPage(body="CA33617 CA11111"),
            NavPage(duplicate_manage=True),
        )
        for page in cases:
            with self.subTest(url=page.url, body=page.body_text):
                with self.assertRaises(IntakeHold):
                    PlaywrightFaoMemoBrowser(page).load_communications(
                        start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE,
                    )
                self.assertNotIn("Search", page.clicks)

    def test_date_that_does_not_stick_holds_before_search(self):
        page = NavPage(date_override="yesterday")
        with self.assertRaisesRegex(IntakeHold, "did not stick"):
            PlaywrightFaoMemoBrowser(page).load_communications(
                start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE,
            )
        self.assertEqual(page.clicks, [
            "Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL,
        ])
        self.assertEqual(page.end_date.value, "")
        self.assertNotIn("Get Policy Activity", page.clicks)
        self.assertNotIn("Search", page.clicks)

    def test_policy_activity_uses_live_date_controls_not_old_labels(self):
        page = NavPage()
        page.state = "activity"
        self.assertEqual(page.get_by_label("Processed date from", exact=True).count(), 0)
        self.assertEqual(page.get_by_label("Processed date to", exact=True).count(), 0)
        self.assertEqual(page.locator(VIEW_ACTIVITY_BY_CSS).count(), 1)
        self.assertEqual(page.get_by_label(VIEW_ACTIVITY_BY_LABEL, exact=True).count(), 1)
        preset = page.locator(PROCESSED_DATE_RANGE_CSS)
        self.assertEqual(preset.count(), 1)
        self.assertTrue(preset.is_visible())
        option_text = [item.inner_text() for item in preset.locator("option").all()]
        self.assertEqual(option_text, ["Yesterday", "Last 30 Days", CUSTOM_DATE_RANGE_LABEL])
        self.assertEqual(
            preset.locator("option").nth(2).get_attribute("value"),
            FIXTURE_CUSTOM_RANGE_VALUE,
        )
        self.assertEqual(preset.locator(START_DATE_CSS).count(), 0)
        self.assertEqual(preset.locator(END_DATE_CSS).count(), 0)
        self.assertEqual(preset.locator(GET_POLICY_ACTIVITY_CSS).count(), 0)
        self.assertEqual(page.locator(START_DATE_CSS).count(), 1)
        self.assertFalse(page.locator(START_DATE_CSS).is_visible())
        self.assertEqual(page.locator(END_DATE_CSS).count(), 1)
        self.assertFalse(page.locator(END_DATE_CSS).is_visible())
        self.assertEqual(
            page.get_by_label(START_DATE_LABEL, exact=True).and_(page.locator(START_DATE_CSS)).count(),
            1,
        )
        self.assertEqual(
            page.get_by_label(END_DATE_LABEL, exact=True).and_(page.locator(END_DATE_CSS)).count(),
            1,
        )
        self.assertTrue(page.locator(GET_POLICY_ACTIVITY_CSS).is_visible())
        self.assertEqual(page.locator(GET_POLICY_ACTIVITY_CSS).get_attribute("value"), GET_POLICY_ACTIVITY_LABEL)
        self.assertEqual(page.locator(PROCESSED_DATE_OPTION_CSS).count(), 1)

    def test_legacy_processed_date_labels_do_not_satisfy_the_filter(self):
        page = NavPage(legacy_dates=True)
        self._load(page)
        self.assertEqual(page.start_date.value, PROVE_DAY.isoformat())
        self.assertEqual(page.end_date.value, PROVE_DAY.isoformat())
        self.assertEqual(page.legacy_from.value, "")
        self.assertEqual(page.legacy_to.value, "")
        self.assertIn("Get Policy Activity", page.clicks)

        missing = NavPage(view_mode="missing", legacy_dates=True)
        with self.assertRaisesRegex(IntakeHold, "View Activity By") as caught:
            self._load(missing)
        self.assertNotIn("Processed date from", str(caught.exception))
        self.assertNotIn("Processed date to", str(caught.exception))
        self.assertEqual(missing.legacy_from.value, "")
        self.assertEqual(missing.legacy_to.value, "")
        self.assertNotIn("Search", missing.clicks)
        self.assertNotIn("Get Policy Activity", missing.clicks)

    def test_unique_selector_without_an_accessible_name_still_filters(self):
        for kwargs in ({"view_mode": "unlabeled"}, {"start_mode": "unlabeled"}):
            with self.subTest(kwargs):
                page = NavPage(**kwargs)
                self._load(page)
                self.assertEqual(page.start_date.value, PROVE_DAY.isoformat())
                self.assertEqual(page.end_date.value, PROVE_DAY.isoformat())
                self.assertIn("Get Policy Activity", page.clicks)
                self.assertIn("Communications", page.clicks)
                self.assertNotIn("Search", page.clicks)

    def test_view_activity_by_unique_match_continues_without_gemini(self):
        for mode in ("ok", "unlabeled", "late", "extra-name"):
            with self.subTest(mode=mode):
                page = NavPage(view_mode=mode)
                with patch("robie_job_engine.gemini_ui_rescue.rescued_locator") as rescued:
                    grid = self._load(page)
                rescued.assert_not_called()
                self.assertEqual(len(grid.rows), 2)
                self.assertIn("Processed Date", page.clicks)
                self.assertIn(CUSTOM_DATE_RANGE_LABEL, page.clicks)
                self.assertIn("Get Policy Activity", page.clicks)
                self.assertIn("Communications", page.clicks)
                self.assertNotIn("Search", page.clicks)
                self.assertEqual(page.view_nodes[0].value, PROCESSED_DATE_OPTION_VALUE)
                if mode == "extra-name":
                    self.assertEqual(page.view_nodes[1].value, "")

    def test_view_activity_by_form_url_with_query_continues(self):
        page = NavPage(
            url="https://user:secretpass@www.foragentsonly.com/managepolicies/policyactivity?token=sekret",
        )
        with patch("robie_job_engine.gemini_ui_rescue.rescued_locator") as rescued:
            self._load(page)
        rescued.assert_not_called()
        self.assertIn("Processed Date", page.clicks)
        self.assertEqual(page.view_nodes[0].value, PROCESSED_DATE_OPTION_VALUE)

    def test_view_activity_by_waits_for_policy_activity_form_then_continues(self):
        page = NavPage(url="https://www.foragentsonly.com/", form_url_on_wait=True)
        with patch("robie_job_engine.gemini_ui_rescue.rescued_locator") as rescued:
            self._load(page)
        rescued.assert_not_called()
        self.assertEqual(page.view_nodes[0].value, PROCESSED_DATE_OPTION_VALUE)
        self.assertIn("Processed Date", page.clicks)

    def test_view_activity_by_zero_or_multiple_holds_without_gemini(self):
        form = "page https://www.foragentsonly.com/managepolicies/policyactivity"
        cases = (
            ("missing", "missing", "matched 0 elements", (
                "select#PDDateType matched 0",
                'select[name="DateType"] matched 0',
            )),
            ("renamed id or name", "renamed", "matched 0 elements", (
                "select#PDDateType matched 1",
                'select[name="DateType"] matched 0',
            )),
            ("duplicate", "duplicate", "matched 2 elements", (
                "select#PDDateType matched 2",
                'select[name="DateType"] matched 2',
            )),
            ("hidden", "hidden", "was not visible", ()),
            ("mistitled", "mistitled", "is not the registered select", ()),
            ("split label", "split-label", "is not the registered select", ()),
        )
        for label, mode, reason, diagnostics in cases:
            with self.subTest(label):
                page = NavPage(view_mode=mode)
                client = _RescueClient(json.dumps({
                    "decision": "unique",
                    "locator": 'select#PDDateType[name="DateType"] >> nth=0',
                }))
                with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
                    with patch(
                        "robie_job_engine.gemini_ui_rescue.build_default_client",
                        return_value=client,
                    ):
                        with patch("robie_job_engine.gemini_ui_rescue.rescued_locator") as rescued:
                            with self.assertRaises(IntakeHold) as caught:
                                self._load(page)
                            rescued.assert_not_called()
                message = str(caught.exception)
                self.assertFalse(is_rescuable_control_failure(caught.exception))
                self.assertIn("View Activity By", message)
                self.assertIn(reason, message)
                self.assertIn(VIEW_ACTIVITY_BY_CSS, message)
                self.assertIn(form, message)
                for phrase in diagnostics:
                    self.assertIn(phrase, message)
                self.assertIn("date filter was not changed", message)
                self.assertNotIn("gemini:", message)
                self.assertNotIn("nth=", message)
                self.assertEqual(client.calls, 0)
                self.assertEqual(page.clicks, ["Manage Policies", "Policy Activity"])
                self.assertNotIn("Processed Date", page.clicks)
                self.assertNotIn("Get Policy Activity", page.clicks)
                self.assertNotIn("Communications", page.clicks)
                self.assertNotIn("Search", page.clicks)

    def test_view_activity_by_wrong_page_holds_without_gemini(self):
        cases = (
            ("home", "https://www.foragentsonly.com/", "page https://www.foragentsonly.com/;"),
            (
                "results",
                "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/cancels/",
                "page https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/cancels/;",
            ),
            (
                "secret query",
                "https://user:secretpass@www.foragentsonly.com/?token=sekret",
                "page https://www.foragentsonly.com/;",
            ),
        )
        for label, url, safe in cases:
            with self.subTest(label):
                page = NavPage(url=url)
                client = _RescueClient(json.dumps({
                    "decision": "unique",
                    "locator": 'select#PDDateType[name="DateType"] >> nth=0',
                }))
                with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
                    with patch(
                        "robie_job_engine.gemini_ui_rescue.build_default_client",
                        return_value=client,
                    ):
                        with patch("robie_job_engine.gemini_ui_rescue.rescued_locator") as rescued:
                            with self.assertRaises(IntakeHold) as caught:
                                self._load(page)
                            rescued.assert_not_called()
                message = str(caught.exception)
                self.assertFalse(is_rescuable_control_failure(caught.exception))
                self.assertIn("page is not the Policy Activity form", message)
                self.assertIn("matched 1 element", message)
                self.assertIn(safe, message)
                self.assertNotIn("secretpass", message)
                self.assertNotIn("sekret", message)
                self.assertNotIn("gemini:", message)
                self.assertEqual(client.calls, 0)
                self.assertEqual(page.clicks, ["Manage Policies", "Policy Activity"])
                self.assertNotIn("Processed Date", page.clicks)
                self.assertEqual(page.view_nodes[0].value, "")

    def test_date_filter_fails_closed_when_controls_are_missing_or_ambiguous(self):
        cases = (
            ("missing view", dict(view_mode="missing"), "View Activity By", ["Manage Policies", "Policy Activity"]),
            ("duplicate view", dict(view_mode="duplicate"), "View Activity By", ["Manage Policies", "Policy Activity"]),
            ("mistitled view", dict(view_mode="mistitled"), "View Activity By", ["Manage Policies", "Policy Activity"]),
            ("split label", dict(view_mode="split-label"), "View Activity By", ["Manage Policies", "Policy Activity"]),
            ("renamed select", dict(view_mode="renamed"), "View Activity By", ["Manage Policies", "Policy Activity"]),
            ("hidden view", dict(view_mode="hidden"), "View Activity By", ["Manage Policies", "Policy Activity"]),
            ("missing option", dict(option_mode="missing"), "Processed Date", ["Manage Policies", "Policy Activity"]),
            ("duplicate option", dict(option_mode="duplicate"), "Processed Date", ["Manage Policies", "Policy Activity"]),
            ("mistyped option", dict(option_mode="mistyped"), "Processed Date", ["Manage Policies", "Policy Activity"]),
            (
                "duplicate option text",
                dict(option_mode="duplicate-text"),
                "Processed Date",
                ["Manage Policies", "Policy Activity"],
            ),
            (
                "range select missing",
                dict(range_mode="missing"),
                "Processed date range",
                ["Manage Policies", "Policy Activity", "Processed Date"],
            ),
            (
                "duplicate range select",
                dict(range_mode="duplicate"),
                "Processed date range",
                ["Manage Policies", "Policy Activity", "Processed Date"],
            ),
            (
                "custom range option missing",
                dict(range_mode="missing-custom"),
                CUSTOM_DATE_RANGE_LABEL,
                ["Manage Policies", "Policy Activity", "Processed Date"],
            ),
            (
                "duplicate custom range option",
                dict(range_mode="duplicate-custom"),
                CUSTOM_DATE_RANGE_LABEL,
                ["Manage Policies", "Policy Activity", "Processed Date"],
            ),
            (
                "custom range option has no value",
                dict(range_mode="blank-custom"),
                CUSTOM_DATE_RANGE_LABEL,
                ["Manage Policies", "Policy Activity", "Processed Date"],
            ),
            (
                "custom range value is shared",
                dict(range_mode="duplicate-value"),
                CUSTOM_DATE_RANGE_LABEL,
                ["Manage Policies", "Policy Activity", "Processed Date"],
            ),
            (
                "custom range does not reveal dates",
                dict(range_mode="stuck"),
                "Start Date",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "missing start",
                dict(start_mode="missing"),
                "Start Date",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "duplicate start",
                dict(start_mode="duplicate"),
                "Start Date",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "mistitled start",
                dict(start_mode="mistitled"),
                "Start Date",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "second page-level start",
                dict(start_mode="outside"),
                "Start Date",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "missing end",
                dict(end_mode="missing"),
                "End Date",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "duplicate end",
                dict(end_mode="duplicate"),
                "End Date",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "missing submit",
                dict(button_mode="missing"),
                "Get Policy Activity",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "duplicate submit",
                dict(button_mode="duplicate"),
                "Get Policy Activity",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
            (
                "misnamed submit",
                dict(button_mode="misnamed"),
                "Get Policy Activity",
                ["Manage Policies", "Policy Activity", "Processed Date", CUSTOM_DATE_RANGE_LABEL],
            ),
        )
        for label, kwargs, hold, clicks in cases:
            with self.subTest(label):
                page = NavPage(**kwargs)
                with self.assertRaisesRegex(IntakeHold, hold):
                    self._load(page)
                self.assertEqual(page.clicks, clicks)
                self.assertNotIn("Get Policy Activity", page.clicks)
                self.assertNotIn("Search", page.clicks)
                self.assertEqual(page.filled_while_hidden, [])
                if kwargs.get("range_mode") == "stuck":
                    self.assertEqual(page.date_range.value, FIXTURE_CUSTOM_RANGE_VALUE)
                    self.assertFalse(page.start_date.visible)
                    self.assertEqual(page.start_date.value, "")
                if kwargs.get("range_mode") in {
                    "missing-custom", "duplicate-custom", "blank-custom", "duplicate-value", "duplicate",
                }:
                    self.assertEqual(page.date_range.value, "")
                    self.assertFalse(page.start_date.visible)
                if kwargs.get("start_mode") in {"duplicate", "missing", "mistitled", "outside"}:
                    self.assertEqual(page.start_date.value, "")
                if kwargs.get("end_mode") == "duplicate":
                    self.assertEqual(page.start_date.value, PROVE_DAY.isoformat())
                    self.assertEqual(page.end_date.value, "")
                if kwargs.get("button_mode") in {"missing", "duplicate", "misnamed"}:
                    self.assertEqual(page.start_date.value, PROVE_DAY.isoformat())
                    self.assertEqual(page.end_date.value, PROVE_DAY.isoformat())

    def test_new_tab_pdf_is_captured_and_enabled_next_holds(self):
        page = NavPage(open_mode="tab", next_mode="none")
        browser = PlaywrightFaoMemoBrowser(page)
        grid = browser.load_communications(start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE)
        memo = parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)[0]
        self.assertTrue(pdf_bytes_from_observation(browser.capture_memo(memo.document_id)).startswith(b"%PDF"))

        enabled = NavPage(next_mode="enabled")
        with self.assertRaisesRegex(IntakeHold, "incomplete or ambiguous"):
            FaoCommunicationsMemoPortal(
                PlaywrightFaoMemoBrowser(enabled),
                LocalDeliveryLedger(Path(tempfile.mkdtemp()) / "out"),
            ).list_documents(scope="fao_communications", start=PROVE_DAY, end=PROVE_DAY)

    def test_same_tab_pdf_restores_the_list_or_holds(self):
        page = NavPage(open_mode="same", next_mode="none")
        browser = PlaywrightFaoMemoBrowser(page)
        grid = browser.load_communications(start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE)
        memo = parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)[0]
        blob = pdf_bytes_from_observation(browser.capture_memo(memo.document_id))
        self.assertEqual(blob, pdf_bytes(b"860521214"))
        self.assertEqual(page.url, RESULTS_UNDERWRITING_URL)

        stuck = NavPage(open_mode="same-stuck", next_mode="none")
        browser = PlaywrightFaoMemoBrowser(stuck)
        grid = browser.load_communications(start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE)
        memo = parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)[0]
        with self.assertRaisesRegex(IntakeHold, "left the Communications list"):
            browser.capture_memo(memo.document_id)

    def test_cancels_results_open_communications_without_search(self):
        page = NavPage(search_on_results=True)
        grid = self._load(page)
        self.assertEqual(page.url, RESULTS_UNDERWRITING_URL)
        self.assertEqual(len(grid.rows), 2)
        self.assertNotIn("Search", page.clicks)
        self.assertEqual(page.clicks.count("Communications"), 1)
        self.assertEqual(page.get_by_role("button", name="Search", exact=True).count(), 1)
        self.assertEqual(page.get_by_role("tab", name="Communications", exact=True).count(), 0)
        self.assertEqual(page.locator(COMMUNICATIONS_LINK_CSS).count(), 1)

    def test_sibling_processed_date_results_also_open_communications(self):
        page = NavPage(results_section="renewals")
        grid = self._load(page)
        self.assertEqual(page.url, RESULTS_UNDERWRITING_URL)
        self.assertEqual(len(grid.rows), 2)
        self.assertNotIn("Search", page.clicks)
        self.assertEqual(page.clicks[-1], "Communications")

    def test_communications_section_link_opens_without_a_tab(self):
        for mode in ("named", "unlabeled", "tab-and-link"):
            with self.subTest(mode=mode):
                page = NavPage(comms_mode=mode, search_on_results=True)
                grid = self._load(page)
                self.assertEqual(len(grid.rows), 2)
                self.assertEqual(page.url, RESULTS_UNDERWRITING_URL)
                self.assertNotIn("Search", page.clicks)
                self.assertEqual(page.memo_opens, [])
        both = NavPage(comms_mode="tab-and-link")
        self._load(both)
        tab = next(node for node in both.communications_nodes if node.role == "tab")
        self.assertEqual(tab.attrs["aria-selected"], "false")
        self.assertEqual(both.clicks.count("Communications"), 1)

    def test_communications_section_family_is_accepted(self):
        for section in (
            "underwriting",
            "underwriting-legacy",
            "underwriting_legacy",
            "underwritinglegacy",
            "communications",
            "Underwriting",
        ):
            with self.subTest(section=section):
                page = NavPage(communications_section=section)
                self._load(page)
                self.assertEqual(page.url, page.communications_url)
                self.assertNotIn("Search", page.clicks)
                self.assertIn("Communications", page.clicks)

    def test_results_page_holds_closed_without_clicking_search(self):
        cases = (
            ("stays on the form", dict(results_mode="stay"), PROCESSED_DATE_RESULTS_HOLD, "Get Policy Activity"),
            (
                "communications link missing",
                dict(comms_mode="missing"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "communications link duplicated",
                dict(comms_mode="duplicate"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "data-at and a second named link",
                dict(comms_mode="split"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "communications is only a tab",
                dict(comms_mode="tab"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "section link text is not Communications",
                dict(comms_mode="mistitled"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "section link accessible name differs",
                dict(comms_mode="mislabeled"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "Communications label is on another element",
                dict(comms_mode="foreign-label"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "communications link is hidden",
                dict(comms_mode="hidden"),
                "Communications",
                "Get Policy Activity",
            ),
            (
                "click stays on cancels",
                dict(comms_mode="stay"),
                COMMUNICATIONS_SECTION_HOLD,
                "Communications",
            ),
            (
                "click lands outside the communications family",
                dict(communications_section="renewals"),
                COMMUNICATIONS_SECTION_HOLD,
                "Communications",
            ),
        )
        for label, kwargs, hold, last_click in cases:
            with self.subTest(label):
                page = NavPage(search_on_results=True, **kwargs)
                with self.assertRaisesRegex(IntakeHold, hold):
                    self._load(page)
                self.assertNotIn("Search", page.clicks)
                self.assertEqual(page.clicks[-1], last_click)
                self.assertEqual(page.memo_opens, [])

    def test_processed_date_results_url_fails_closed(self):
        class UrlPage:
            def __init__(self, url):
                self.url = url

            def wait_for_url(self, pattern, timeout=None):
                if not pattern.search(self.url):
                    raise TimeoutError("processed-date results page did not appear")

        accepted = (
            RESULTS_CANCELS_URL,
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/Cancels",
            "https://foragentsonly.com/managepolicies/policyactivity/processeddateresults/endorsements/",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(require_processed_date_results(UrlPage(url)))
        rejected = (
            LIST_URL,
            RESULTS_CANCELS_URL + "?day=2026-09-25",
            RESULTS_CANCELS_URL + "#memo",
            "http://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/cancels/",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/cancels/extra/",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/",
            "https://foragentsonlylogin.progressive.com/managepolicies/policyactivity/processeddateresults/cancels/",
            "https://evil.example/managepolicies/policyactivity/processeddateresults/cancels/",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaisesRegex(IntakeHold, PROCESSED_DATE_RESULTS_HOLD):
                    require_processed_date_results(UrlPage(url))

    def test_communications_section_url_fails_closed(self):
        class UrlPage:
            def __init__(self, url):
                self.url = url

            def wait_for_url(self, pattern, timeout=None):
                if not pattern.search(self.url):
                    raise TimeoutError("communications section did not appear")

        accepted = (
            RESULTS_UNDERWRITING_URL,
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/underwriting-legacy/",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/underwriting_legacy",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/underwritinglegacy/",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/communications/",
            "https://foragentsonly.com/managepolicies/policyactivity/processeddateresults/Underwriting/",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(require_communications_section(UrlPage(url)))
        rejected = (
            RESULTS_CANCELS_URL,
            RESULTS_UNDERWRITING_URL + "?day=2026-09-25",
            RESULTS_UNDERWRITING_URL + "#memo",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/renewals/",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/underwriting/extra/",
            "https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/communications-legacy/",
            "http://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/underwriting/",
            "https://evil.example/managepolicies/policyactivity/processeddateresults/underwriting/",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaisesRegex(IntakeHold, COMMUNICATIONS_SECTION_HOLD):
                    require_communications_section(UrlPage(url))

    def test_ambiguous_next_holds(self):
        page = NavPage(next_mode="ambiguous")
        with self.assertRaisesRegex(IntakeHold, "incomplete or ambiguous"):
            FaoCommunicationsMemoPortal(
                PlaywrightFaoMemoBrowser(page),
                LocalDeliveryLedger(Path(tempfile.mkdtemp()) / "out"),
            ).list_documents(scope="fao_communications", start=PROVE_DAY, end=PROVE_DAY)


class WorkerRowTests(unittest.TestCase):
    def test_duplicate_identity_holds_before_download(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            downloads = []
            row = {
                "document_id": "one",
                "source_account": "CA33617",
                "requires_action": True,
                "already_delivered": False,
                "processed_or_effective_date": "2026-09-25",
                "policy_number": "860521214",
                "reason": "General",
            }
            other = dict(row, document_id="two")
            portal = SimpleNamespace(
                list_documents=lambda **kwargs: ReadResult((row, other), True, True),
                download_document=lambda document_id: downloads.append(document_id),
            )
            with tempfile.TemporaryDirectory() as tmp:
                worker = ProgressiveRetrieval(None, SourceArchive(Path(tmp) / "sources"))
                with self.assertRaisesRegex(IntakeHold, "ambiguous"):
                    worker.pull_fao_communications(portal, start=PROVE_DAY, end=PROVE_DAY)
            self.assertEqual(downloads, [])


class GeminiUiRescueWiringTests(unittest.TestCase):
    """Progressive FAO pull uses the Test-only rescue. No live Gemini call."""

    def _load(self, page):
        return PlaywrightFaoMemoBrowser(page).load_communications(
            start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE,
        )

    def test_unique_control_does_not_call_gemini(self):
        page = NavPage()

        def forbid_client():
            raise AssertionError("Gemini was called for a unique control")

        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                forbid_client,
            ):
                grid = self._load(page)
        self.assertEqual(len(grid.rows), 2)
        self.assertIn("Communications", page.clicks)

    def test_production_skips_rescue_even_if_the_test_flag_is_set(self):
        page = NavPage(start_mode="missing")

        def forbid_client():
            raise AssertionError("Production called Gemini")

        with patch.dict(
            os.environ,
            {"ROBIE_ENV": "PRODUCTION", "ROBIE_FAO_GEMINI_UI_RESCUE": "1"},
            clear=False,
        ):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                forbid_client,
            ):
                with self.assertRaises(IntakeHold) as caught:
                    self._load(page)
        self.assertIn("Start Date", str(caught.exception))
        self.assertNotIn("gemini:", str(caught.exception))
        self.assertIn("Processed Date", page.clicks)
        self.assertNotIn("Get Policy Activity", page.clicks)

    def test_missing_key_holds_not_configured(self):
        page = NavPage(start_mode="missing")
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.load_gemini_api_key",
                return_value="",
            ):
                with self.assertRaisesRegex(IntakeHold, "gemini: not_configured") as caught:
                    self._load(page)
        self.assertIn("Start Date", str(caught.exception))
        self.assertIn("Processed Date", page.clicks)
        self.assertNotIn("Get Policy Activity", page.clicks)
        self.assertNotIn("Search", page.clicks)

    def test_unique_locator_retries_a_later_control_once(self):
        page = NavPage(start_mode="alt")
        page.url = "https://user:secretpass@www.foragentsonly.com/managepolicies/policyactivity?token=sekret"
        client = _RescueClient(json.dumps({
            "decision": "unique",
            "locator": (
                'input[type="date"]#js-datepicker__date-start-alt'
                '[data-at="datatable-daterangepicker-startdate"]'
            ),
        }))
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                return_value=client,
            ):
                grid = self._load(page)
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(grid.rows), 2)
        self.assertIn("Communications", page.clicks)
        self.assertNotIn("token=", client.prompts[0])
        self.assertNotIn("sekret", client.prompts[0])
        self.assertNotIn("secretpass", client.prompts[0])
        self.assertNotIn("860521214", client.prompts[0])
        self.assertIn("www.foragentsonly.com", client.prompts[0])
        self.assertIn("Start Date", client.prompts[0])
        self.assertNotIn("View Activity By", client.prompts[0])

    def test_unsure_and_ambiguous_locators_hold(self):
        unsure = NavPage(start_mode="missing")
        unsure_client = _RescueClient(json.dumps({
            "decision": "unsure",
            "reason": "two controls",
        }))
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                return_value=unsure_client,
            ):
                with self.assertRaisesRegex(IntakeHold, "gemini: unsure"):
                    self._load(unsure)
        self.assertEqual(unsure_client.calls, 1)
        self.assertIn("Processed Date", unsure.clicks)
        self.assertNotIn("Get Policy Activity", unsure.clicks)

        ambiguous = NavPage(start_mode="duplicate")
        ambiguous_client = _RescueClient(json.dumps({
            "decision": "unique",
            "locator": START_DATE_CSS,
        }))
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                return_value=ambiguous_client,
            ):
                with self.assertRaisesRegex(IntakeHold, "gemini: ambiguous"):
                    self._load(ambiguous)
        self.assertEqual(ambiguous_client.calls, 1)
        self.assertNotIn("Get Policy Activity", ambiguous.clicks)

        positional = NavPage(start_mode="duplicate")
        positional_client = _RescueClient(json.dumps({
            "decision": "unique",
            "locator": START_DATE_CSS + " >> nth=0",
        }))
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                return_value=positional_client,
            ):
                with self.assertRaisesRegex(IntakeHold, "gemini: ambiguous"):
                    self._load(positional)
        self.assertNotIn("Get Policy Activity", positional.clicks)
        self.assertIn("nth=", positional_client.prompts[0])

    def test_second_failure_does_not_call_gemini_again(self):
        page = NavPage(start_mode="alt", end_mode="missing")
        client = _RescueClient(json.dumps({
            "decision": "unique",
            "locator": (
                'input[type="date"]#js-datepicker__date-start-alt'
                '[data-at="datatable-daterangepicker-startdate"]'
            ),
        }))
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                return_value=client,
            ):
                with self.assertRaises(IntakeHold) as caught:
                    self._load(page)
        self.assertEqual(client.calls, 1)
        self.assertIn("End Date", str(caught.exception))
        self.assertNotIn("gemini:", str(caught.exception))
        self.assertNotIn("Get Policy Activity", page.clicks)


class _RescueClient:
    def __init__(self, raw: str):
        self.raw = raw
        self.calls = 0
        self.prompts: list[str] = []

    def generate_unique_locator(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.raw


if __name__ == "__main__":
    unittest.main()
