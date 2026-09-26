"""Fixture tests for Progressive FAO Communications memo pull. No live login."""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    PlaywrightFaoMemoBrowser,
    build_parser,
    classify_memo_row,
    collect_memo_observation,
    main,
    memo_document_id,
    memo_filename,
    parse_memo_grid,
    pdf_bytes_from_observation,
    read_playwright_pdf_view,
    require_loopback_cdp,
    require_memo_pdf_parity,
    resolve_processed_window,
    select_fao_page,
)
from robie_job_engine.progressive_retrieval import ProgressiveRetrieval


LIST_URL = "https://www.foragentsonly.com/managepolicies/policyactivity"
LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
PROVE_DAY = date(2026, 9, 25)
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
        self.assertIn("link:Manage Policies", selectors)
        self.assertIn("tab:Communications", selectors)
        self.assertIn("Processed date from", selectors)
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


class FakeNode:
    def __init__(self, role, name="", text=None, children=None, attrs=None, disabled=False):
        self.role = role
        self.name = name
        self.text = name if text is None else text
        self.children = children or []
        self.attrs = attrs or {}
        self.disabled = disabled
        self.value = ""
        self.input_override = None

    def find(self, selector):
        found = [self] if self.matches(selector) else []
        for child in self.children:
            found.extend(child.find(selector))
        return found

    def find_role(self, role, name, exact):
        found = []
        if self.role == role and (name is None or ((self.name == name) if exact else name in self.name)):
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
            "body": self.role == "body",
            "iframe": self.role == "iframe",
            "a": self.role == "link",
            "embed[type='application/pdf']": self.role == "embed" and self.attrs.get("type") == "application/pdf",
            "input[type='password']": self.attrs.get("type") == "password",
        }[selector]


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
        self.nodes[0].value = value

    def input_value(self):
        node = self.nodes[0]
        return node.value if node.input_override is None else node.input_override

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
    def __init__(self, *, body="StreetSmart Risk Mgr CA33617", url=LIST_URL, password=False,
                 duplicate_manage=False, next_mode="disabled", open_mode="download",
                 date_override=None, go_back_restores=True):
        self.url = url
        self.list_url = url
        self.body_text = body
        self.password = password
        self.duplicate_manage = duplicate_manage
        self.next_mode = next_mode
        self.open_mode = open_mode
        self.date_override = date_override
        self.go_back_restores = go_back_restores
        self.clicks = []
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
        manage = [FakeNode("link", "Manage Policies")]
        if self.duplicate_manage:
            manage.append(FakeNode("link", "Manage Policies"))
        self.home = manage
        self.policies = [FakeNode("link", "Policy Activity")]
        self.from_box = FakeNode("textbox", "Processed date from")
        self.to_box = FakeNode("textbox", "Processed date to")
        if self.date_override is not None:
            self.from_box.input_override = self.date_override
        self.activity = [self.from_box, self.to_box, FakeNode("button", "Search")]
        self.tab = FakeNode("tab", "Communications", attrs={"aria-selected": "false"})
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

    def roots(self):
        nodes = [self.body]
        if self.password:
            nodes.append(self.password_node)
        nodes.extend({"home": self.home, "policies": self.policies, "activity": self.activity, "comms": [self.tab]}[self.state])
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
            found.extend(node.find_role("textbox", label, exact))
        return NodeLocator(found, self)

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
        if node.name == "Manage Policies" and self.state == "home":
            self.state = "policies"
        elif node.name == "Policy Activity" and self.state == "policies":
            self.state = "activity"
        elif node.name == "Search" and self.state == "activity":
            self.state = "comms"
        elif node.name == "Communications" and self.state == "comms":
            node.attrs["aria-selected"] = "true"
            self.table_visible = True
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
        grid = browser.load_communications(start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE)
        memos = parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)
        self.assertEqual([memo.policy_number for memo in memos], ["860521214", "879512352"])
        self.assertEqual(page.from_box.value, "09/25/2026")
        self.assertEqual(page.to_box.value, "09/25/2026")
        observation = browser.capture_memo(memos[1].document_id)
        self.assertEqual(pdf_bytes_from_observation(observation), pdf_bytes(b"879512352"))
        self.assertEqual(page.memo_opens, ["879512352"])
        self.assertEqual(page.clicks, ["Manage Policies", "Policy Activity", "Search", "Communications", "Memo"])
        self.assertFalse(page.closed)

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
        self.assertNotIn("Search", page.clicks)

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
        self.assertEqual(page.url, LIST_URL)

        stuck = NavPage(open_mode="same-stuck", next_mode="none")
        browser = PlaywrightFaoMemoBrowser(stuck)
        grid = browser.load_communications(start=PROVE_DAY, end=PROVE_DAY, agent_code=DEFAULT_AGENT_CODE)
        memo = parse_memo_grid(grid, agent_code=DEFAULT_AGENT_CODE)[0]
        with self.assertRaisesRegex(IntakeHold, "left the Communications list"):
            browser.capture_memo(memo.document_id)

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


if __name__ == "__main__":
    unittest.main()
