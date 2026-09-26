"""Fixture tests for the Travelers Policy Activity pull. No live login."""
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

from robie_job_engine.intake_core import IntakeHold, SourceArchive
from robie_job_engine.locator_registry import LocatorRegistry
from robie_job_engine.playwright_write_guard import locator_is_positional_guess
from robie_job_engine.travelers_retrieval import (
    DRIVE_QA_PARENT_ID,
    ActivityGrid,
    LocalDeliveryLedger,
    PagePdfView,
    PdfOpenObservation,
    PlaywrightTravelersActivityBrowser,
    TravelersActivityPortal,
    TravelersRetrieval,
    activity_filename,
    build_parser,
    classify_activity_row,
    date_pack_status,
    main,
    monday_weekend_window,
    parse_activity_grid,
    pdf_bytes_from_observation,
    qa_root,
    read_playwright_pdf_view,
    require_loopback_cdp,
    resolve_processed_window,
    select_travelers_page,
)


LIST_URL = "https://www.travelers.com/foragents/policy-activity"
LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
PROVE_DAY = date(2026, 9, 25)
MONDAY = date(2026, 9, 28)
SATURDAY = date(2026, 9, 26)
SUNDAY = date(2026, 9, 27)
TUESDAY = date(2026, 9, 29)
HEADERS = ("Policy Number", "Document Title", "Process Date", "Type")
CL_POLICY = "UB-8370B557-25"
CL_NAME = "UB-8370B557-25 Premium Adjustment Notice Travelers.pdf"


def pdf_bytes(mark: bytes) -> bytes:
    return b"%PDF-1.4\n" + mark + b"\n%%EOF\n"


def activity_grid(rows, insured, agent, *, more_pages=False, headers=HEADERS, url=LIST_URL):
    return ActivityGrid(url, headers, tuple(rows), tuple(insured), tuple(agent), more_pages)


class ScriptedBrowser:
    def __init__(self, grid: ActivityGrid, pdfs: dict[str, bytes]):
        self.grid = grid
        self.pdfs = pdfs
        self.loads = 0
        self.shots = 0
        self.screenshot_bytes = LIST_PNG
        self.captures: list[str] = []

    def load_policy_activity(self, *, lob, start, end, as_of, include_weekends):
        self.loads += 1
        self.window = (lob, start, end, as_of, include_weekends)
        return self.grid

    def screenshot_policy_activity(self):
        self.shots += 1
        return self.screenshot_bytes

    def capture_pdf(self, document_id):
        self.captures.append(document_id)
        return PdfOpenObservation(downloads=(self.pdfs[document_id],), pages=())


class ExplodingApi:
    def __getattr__(self, name):
        raise AssertionError(name)


class ClassifyTests(unittest.TestCase):
    def test_commercial_name_prefers_the_insured_copy(self):
        self.assertEqual(
            activity_filename("ub-8370b557-25", "Premium Adjustment Notice."),
            CL_NAME,
        )
        self.assertEqual(
            classify_activity_row("cl", "Premium Adjustment Notice", "Policy", 1, 1),
            "download_insured",
        )
        self.assertEqual(classify_activity_row("cl", "NOC", None, 0, 1), "download_agent")
        self.assertEqual(classify_activity_row("cl", "Proposal-New", None, 0, 0), "skip")
        self.assertEqual(classify_activity_row("cl", "Proposal-New", None, 1, 0), "download_insured")

    def test_personal_dashboard_alert_without_a_pdf_is_a_note(self):
        self.assertEqual(classify_activity_row("pl", "Renewal", "Policy", 1, 0), "download_insured")
        self.assertEqual(classify_activity_row("pl", "Renewal", "Policy", 0, 1), "ambiguous")
        self.assertEqual(classify_activity_row("pl", "Renewal", "Policy", 0, 0), "ambiguous")
        self.assertEqual(
            classify_activity_row("pl", "New Business Action Items", "Alert", 0, 0),
            "held_no_pdf",
        )
        self.assertEqual(
            classify_activity_row("pl", "Renewal", "Dashboard Alert", 0, 0),
            "held_no_pdf",
        )
        self.assertEqual(
            classify_activity_row("pl", "Dashboard Alert", None, 1, 0),
            "download_insured",
        )

    def test_filename_rejects_path_pieces_and_short_policy_numbers(self):
        with self.assertRaises(IntakeHold):
            activity_filename(CL_POLICY, "../Notice")
        with self.assertRaises(IntakeHold):
            activity_filename("ABC", "Renewal")

    def test_non_action_without_a_pdf_is_skipped_and_unknown_titles_hold(self):
        rows = (
            ("1234567890", "Proposal-New", "09/25/2026", "Policy"),
            (CL_POLICY, "Premium Adjustment Notice", "09/25/2026", "Policy"),
        )
        parsed = parse_activity_grid(activity_grid(rows, (0, 1), (0, 1), ), lob="cl")
        self.assertEqual(tuple(item.filename for item in parsed.downloads), (CL_NAME,))
        self.assertEqual(parsed.downloads[0].copy, "insured")
        self.assertEqual(parsed.skipped[0].document_title, "Proposal-New")
        self.assertEqual(parsed.held_notes, ())
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            parse_activity_grid(
                activity_grid((("1234567890", "Billing Letter", "09/25/2026", "Policy"),), (0,), (0,)),
                lob="pl",
            )

    def test_duplicate_policy_title_and_date_holds(self):
        row = (CL_POLICY, "Audit", "2026-09-25", "Policy")
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            parse_activity_grid(activity_grid((row, row), (1, 1), (0, 0)), lob="cl")


class WindowTests(unittest.TestCase):
    def test_monday_flag_is_saturday_through_sunday_and_other_days_refuse_it(self):
        self.assertEqual(monday_weekend_window(MONDAY), (SATURDAY, SUNDAY))
        self.assertEqual(
            resolve_processed_window(as_of=MONDAY, include_weekends=True, start=None, end=None),
            (SATURDAY, SUNDAY),
        )
        self.assertEqual(
            resolve_processed_window(as_of=TUESDAY, include_weekends=False, start=None, end=None),
            (MONDAY, MONDAY),
        )
        with self.assertRaisesRegex(IntakeHold, "include-weekends"):
            resolve_processed_window(as_of=MONDAY, include_weekends=False, start=None, end=None)
        with self.assertRaisesRegex(IntakeHold, "only when the run date is a Monday"):
            resolve_processed_window(as_of=TUESDAY, include_weekends=True, start=None, end=None)
        with self.assertRaisesRegex(IntakeHold, "Saturday through Sunday"):
            resolve_processed_window(
                as_of=MONDAY, include_weekends=True, start=SUNDAY, end=SUNDAY,
            )

    def test_explicit_historical_window_does_not_require_the_monday_flag(self):
        window = resolve_processed_window(
            as_of=MONDAY, include_weekends=False, start=PROVE_DAY, end=PROVE_DAY,
        )
        self.assertEqual(window, (PROVE_DAY, PROVE_DAY))


class PdfTests(unittest.TestCase):
    def test_disagreeing_or_non_pdf_bytes_hold_and_html_is_not_printed(self):
        blob = pdf_bytes(b"same")
        chosen = pdf_bytes_from_observation(PdfOpenObservation(
            downloads=(blob,),
            pages=(PagePdfView("https://www.travelers.com/notice.pdf", (blob,)),),
        ))
        self.assertEqual(chosen, blob)
        with self.assertRaisesRegex(IntakeHold, "not a PDF"):
            pdf_bytes_from_observation(PdfOpenObservation(
                downloads=(b"<!DOCTYPE html>",),
                pages=(PagePdfView("blob:notice", (blob,)),),
            ))
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            pdf_bytes_from_observation(PdfOpenObservation(downloads=(), pages=()))

    def test_remote_pdf_fetch_stays_on_travelers(self):
        token = pdf_bytes(b"portal")
        requests = []

        class Response:
            ok = True

            def body(self):
                return token

        class Request:
            def get(self, url, timeout):
                requests.append(url)
                return Response()

        class Locator:
            def count(self):
                return 0

        class Page:
            def __init__(self, url):
                self.url = url
                self.context = SimpleNamespace(request=Request())

            def locator(self, selector):
                return Locator()

        view = read_playwright_pdf_view(Page("https://www.travelers.com/docs/notice.pdf"))
        self.assertEqual(view.pdfs, (token,))
        self.assertEqual(requests, ["https://www.travelers.com/docs/notice.pdf"])
        foreign = read_playwright_pdf_view(Page("https://cdn.example/notice.pdf"))
        self.assertEqual(foreign.pdfs, ())
        self.assertEqual(requests, ["https://www.travelers.com/docs/notice.pdf"])

        encoded = base64.b64encode(token).decode("ascii")

        class BlobPage(Page):
            def evaluate(self, script, url):
                return encoded

        blob_view = read_playwright_pdf_view(BlobPage("blob:travelers-notice"))
        self.assertEqual(blob_view.pdfs, (token,))


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
        self.rows = (
            (CL_POLICY, "Premium Adjustment Notice", "09/25/2026", "Policy"),
            ("1234567890", "Proposal-New", "09/25/2026", "Policy"),
        )
        self.grid = activity_grid(self.rows, (1, 0), (1, 0))
        self.parsed = parse_activity_grid(self.grid, lob="cl")
        self.pdfs = {
            item.document_id: pdf_bytes(item.policy_number.encode()) for item in self.parsed.downloads
        }
        self.browser = ScriptedBrowser(self.grid, self.pdfs)
        self.portal = TravelersActivityPortal(
            self.browser, self.ledger, lob="cl", as_of=TUESDAY, include_weekends=False,
        )
        self.worker = TravelersRetrieval(ExplodingApi(), self.archive)

    def test_pull_writes_the_insured_pdf_screenshot_and_count_gate(self):
        items = self.worker.pull_policy_activity(self.portal, lob="cl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(tuple(item.filename for item in items), (CL_NAME,))
        self.assertEqual(self.browser.loads, 1)
        self.assertEqual(self.browser.shots, 1)
        self.assertEqual(len(self.browser.captures), 1)
        day = self.output / "2026-09-25"
        shot = day / "policy-activity-report-2026-09-25.png"
        self.assertEqual(shot.read_bytes(), LIST_PNG)
        self.assertEqual(shot.stat().st_mode & 0o777, 0o600)
        named = day / CL_NAME
        self.assertEqual(named.read_bytes(), items[0].content)
        self.assertEqual(named.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(day.glob("*.pdf")), [named])
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        readme = (day / "README.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["carrier"], "travelers")
        self.assertEqual(manifest["lob"], "cl")
        self.assertEqual(manifest["status"], "PULLED")
        self.assertTrue(manifest["gate_passed"])
        self.assertEqual(manifest["activity_rows"], 1)
        self.assertEqual(manifest["pdfs"], 1)
        self.assertEqual(manifest["drive"]["status"], "not_run")
        self.assertEqual(manifest["drive"]["parent_id"], DRIVE_QA_PARENT_ID)
        self.assertIsNone(manifest["drive"]["folder_id"])
        self.assertEqual(
            manifest["drive"]["path"],
            "Robie Carrier Pull QA (Nicole)/Travelers CL/2026-09-25/",
        )
        self.assertEqual(manifest["skipped"][0]["document_title"], "Proposal-New")
        self.assertIn("Status: PULLED", readme)
        self.assertIn(CL_NAME, readme)
        self.assertIn("EZLynx: not_run", readme)
        self.assertTrue((self.output / "travelers-activity-ledger.json").is_file())
        self.assertFalse((day / "travelers-activity-ledger.json").exists())
        self.assertTrue((self.output / "sources").is_dir())

        replay_browser = ScriptedBrowser(self.grid, self.pdfs)
        replay = TravelersActivityPortal(
            replay_browser, self.ledger, lob="cl", as_of=TUESDAY, include_weekends=False,
        )
        second = TravelersRetrieval(ExplodingApi(), self.archive).pull_policy_activity(
            replay, lob="cl", start=PROVE_DAY, end=PROVE_DAY,
        )
        self.assertEqual(second, ())
        self.assertEqual(replay_browser.captures, [])
        self.assertIn("## Already present", (day / "README.md").read_text(encoding="utf-8"))

    def test_dashboard_alert_without_a_pdf_is_held_and_not_written(self):
        rows = (
            ("1234567890", "Renewal", "09/25/2026", "Policy"),
            ("1234567890", "Dashboard Alert", "09/25/2026", "Dashboard Alert"),
        )
        grid = activity_grid(rows, (1, 0), (0, 0))
        parsed = parse_activity_grid(grid, lob="pl")
        self.assertEqual(len(parsed.held_notes), 1)
        self.assertEqual(parsed.held_notes[0].filename, "")
        pdfs = {item.document_id: pdf_bytes(b"renewal") for item in parsed.downloads}
        browser = ScriptedBrowser(grid, pdfs)
        portal = TravelersActivityPortal(
            browser, self.ledger, lob="pl", as_of=TUESDAY, include_weekends=False,
        )
        with self.assertRaisesRegex(IntakeHold, "not saved as PDFs"):
            self.worker.pull_policy_activity(portal, lob="pl", start=PROVE_DAY, end=PROVE_DAY)
        day = self.output / "2026-09-25"
        alert = day / activity_filename("1234567890", "Dashboard Alert")
        self.assertFalse(alert.exists())
        self.assertEqual(len(list(day.glob("*.pdf"))), 1)
        self.assertEqual(len(browser.captures), 1)
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "HELD")
        self.assertTrue(manifest["gate_passed"])
        self.assertEqual(manifest["activity_rows"], 1)
        self.assertEqual(manifest["pdfs"], 1)
        self.assertEqual(manifest["held_notes"], 1)
        self.assertIn("held_no_pdf", {item["disposition"] for item in manifest["documents"]})
        self.assertIn("note, no PDF", (day / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(manifest["drive"]["path"], "Robie Carrier Pull QA (Nicole)/Travelers PL/2026-09-25/")

    def test_clean_day_stays_pulled_when_another_day_has_only_a_note(self):
        rows = (
            ("1234567890", "Renewal", "09/26/2026", "Policy"),
            ("0987654321", "New Business Action Items", "09/27/2026", "Alert"),
        )
        grid = activity_grid(rows, (1, 0), (0, 0))
        parsed = parse_activity_grid(grid, lob="pl")
        pdfs = {item.document_id: pdf_bytes(b"sat") for item in parsed.downloads}
        browser = ScriptedBrowser(grid, pdfs)
        portal = TravelersActivityPortal(
            browser, self.ledger, lob="pl", as_of=MONDAY, include_weekends=True,
        )
        with self.assertRaisesRegex(IntakeHold, "2026-09-27"):
            self.worker.pull_policy_activity(portal, lob="pl", start=SATURDAY, end=SUNDAY)
        saturday = json.loads((self.output / "2026-09-26" / "manifest.json").read_text(encoding="utf-8"))
        sunday = json.loads((self.output / "2026-09-27" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saturday["status"], "PULLED")
        self.assertIsNone(saturday["held"])
        self.assertEqual(sunday["status"], "HELD")
        self.assertTrue(sunday["gate_passed"])
        self.assertEqual(sunday["activity_rows"], 0)
        self.assertEqual(sunday["pdfs"], 0)
        self.assertIn("not saved as PDFs", sunday["held"])
        self.assertEqual(list((self.output / "2026-09-27").glob("*.pdf")), [])
        self.assertTrue(saturday["screenshot_covers_window"])

    def test_count_mismatch_writes_a_held_pack_and_does_not_claim_success(self):
        class ShortLedger(LocalDeliveryLedger):
            def pdf_ids_for_date(self, day):
                found = super().pdf_ids_for_date(day)
                return set(list(found)[:-1]) if found else found

        ledger = ShortLedger(self.output)
        portal = TravelersActivityPortal(
            self.browser, ledger, lob="cl", as_of=TUESDAY, include_weekends=False,
        )
        with self.assertRaisesRegex(IntakeHold, "1 activity rows and 0 PDFs"):
            self.worker.pull_policy_activity(portal, lob="cl", start=PROVE_DAY, end=PROVE_DAY)
        day = self.output / "2026-09-25"
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "HELD")
        self.assertFalse(manifest["gate_passed"])
        self.assertEqual(manifest["activity_rows"], 1)
        self.assertEqual(manifest["pdfs"], 0)
        self.assertIn("1 activity rows and 0 PDFs", manifest["held"])
        self.assertTrue((day / "policy-activity-report-2026-09-25.png").is_file())
        self.assertTrue((day / CL_NAME).is_file())

    def test_empty_report_still_saves_a_zero_count_screenshot(self):
        browser = ScriptedBrowser(activity_grid((), (), ()), {})
        portal = TravelersActivityPortal(
            browser, self.ledger, lob="pl", as_of=TUESDAY, include_weekends=False,
        )
        items = self.worker.pull_policy_activity(portal, lob="pl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(items, ())
        manifest = json.loads((self.output / "2026-09-25" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["documents"], [])
        self.assertEqual(manifest["activity_rows"], 0)
        self.assertEqual(manifest["pdfs"], 0)

    def test_bad_screenshot_personal_agent_copy_and_next_page_do_not_save_a_pdf(self):
        self.browser.screenshot_bytes = b"GIF89a-not-a-png"
        with self.assertRaisesRegex(IntakeHold, "not a PNG"):
            self.worker.pull_policy_activity(self.portal, lob="cl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(self.browser.captures, [])
        self.assertEqual(list(self.output.glob("**/*.png")), [])

        agent_only = activity_grid((("1234567890", "Renewal", "09/25/2026", "Policy"),), (0,), (1,))
        browser = ScriptedBrowser(agent_only, {})
        portal = TravelersActivityPortal(
            browser, self.ledger, lob="pl", as_of=TUESDAY, include_weekends=False,
        )
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            self.worker.pull_policy_activity(portal, lob="pl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(browser.captures, [])
        self.assertEqual(list(self.output.glob("**/*.pdf")), [])

        paged = ScriptedBrowser(activity_grid(self.rows, (1, 0), (1, 0), more_pages=True), self.pdfs)
        portal = TravelersActivityPortal(
            paged, self.ledger, lob="cl", as_of=TUESDAY, include_weekends=False,
        )
        with self.assertRaisesRegex(IntakeHold, "incomplete or ambiguous"):
            self.worker.pull_policy_activity(portal, lob="cl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(paged.captures, [])

    def test_conflicting_local_file_is_kept(self):
        path = self.ledger.date_dir(PROVE_DAY) / CL_NAME
        path.write_bytes(pdf_bytes(b"different"))
        with self.assertRaisesRegex(IntakeHold, "conflicts"):
            self.worker.pull_policy_activity(self.portal, lob="cl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(path.read_bytes(), pdf_bytes(b"different"))
        self.assertEqual(self.browser.captures, [])

    def test_other_line_wide_window_and_production_do_not_download(self):
        with self.assertRaisesRegex(IntakeHold, "one Travelers line"):
            self.worker.pull_policy_activity(self.portal, lob="pl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(self.browser.loads, 0)
        with self.assertRaisesRegex(IntakeHold, "32 inclusive"):
            self.worker.pull_policy_activity(
                self.portal, lob="cl", start=date(2026, 1, 1), end=date(2026, 9, 1),
            )
        self.assertEqual(self.browser.captures, [])
        calls = []
        portal = SimpleNamespace(
            lob="cl",
            list_documents=lambda **kwargs: calls.append(kwargs),
            download_document=lambda document_id: calls.append(document_id),
        )
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}):
            with self.assertRaisesRegex(IntakeHold, "TEST"):
                self.worker.pull_policy_activity(portal, lob="cl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(calls, [])

    def test_out_of_window_row_holds_before_download(self):
        rows = ((CL_POLICY, "Premium Adjustment Notice", "09/24/2026", "Policy"),)
        browser = ScriptedBrowser(activity_grid(rows, (1,), (0,)), self.pdfs)
        portal = TravelersActivityPortal(
            browser, self.ledger, lob="cl", as_of=TUESDAY, include_weekends=False,
        )
        with self.assertRaisesRegex(IntakeHold, "outside"):
            self.worker.pull_policy_activity(portal, lob="cl", start=PROVE_DAY, end=PROVE_DAY)
        self.assertEqual(browser.captures, [])

    def test_cli_receipt_monday_flag_drive_refusal_and_default_roots(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--lob", "cl", "--as-of", "2026-09-29", "--start", "2026-09-25", "--end", "2026-09-25",
                 "--output", str(self.output)],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "PULLED")
        self.assertEqual(payload["ezlynx"], "not_run")
        self.assertEqual(payload["lob"], "cl")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["verification"]["gate"], "activity_rows_equal_pdfs")
        self.assertTrue(payload["verification"]["screenshot"].endswith("policy-activity-report-2026-09-25.png"))

        stdout = io.StringIO()
        called = []
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}), patch("sys.stdout", stdout):
            code = main(
                ["--lob", "cl", "--start", "2026-09-25", "--end", "2026-09-25", "--output", str(self.output)],
                browser_factory=lambda args: called.append(args),
            )
        self.assertEqual(code, 2)
        self.assertEqual(called, [])
        self.assertEqual(json.loads(stdout.getvalue())["status"], "HELD")

        missing = Path(self.tmp.name) / "missing"
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--lob", "pl", "--as-of", "2026-09-28", "--output", str(missing)],
                browser_factory=lambda args: called.append("attached"),
            )
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())
        self.assertIn("include-weekends", json.loads(stdout.getvalue())["reason"])

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--lob", "cl", "--as-of", "2026-09-26", "--start", "2026-01-01", "--end", "2026-09-01",
                 "--output", str(missing)],
                browser_factory=lambda args: called.append("attached"),
            )
        self.assertEqual(code, 2)
        self.assertIn("32 inclusive", json.loads(stdout.getvalue())["reason"])
        self.assertFalse(missing.exists())

        fresh = Path(self.tmp.name) / "drive"
        drive_browser = ScriptedBrowser(self.grid, self.pdfs)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--lob", "cl", "--as-of", "2026-09-29", "--start", "2026-09-25", "--end", "2026-09-25",
                 "--output", str(fresh), "--upload-drive"],
                browser_factory=lambda args: drive_browser,
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "HELD")
        self.assertIn("not available", payload["reason"])
        self.assertEqual(payload["verification"]["drive_upload"], "HELD")
        manifest = json.loads((fresh / "2026-09-25" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["drive"]["status"], "HELD")
        self.assertEqual(manifest["drive"]["parent_id"], DRIVE_QA_PARENT_ID)
        self.assertIn("refusing to report the pack as uploaded", (fresh / "2026-09-25" / "README.md").read_text(encoding="utf-8"))

        self.assertEqual(qa_root("pl").name, "travelers-pl")
        self.assertEqual(qa_root("cl").name, "travelers-cl")
        self.assertFalse(qa_root("pl").exists())
        self.assertFalse(qa_root("cl").exists())
        args = build_parser().parse_args(["--lob", "pl", "--as-of", "2026-09-28", "--include-weekends"])
        self.assertIsNone(args.output)
        self.assertFalse(args.upload_drive)
        self.assertFalse(qa_root("pl").exists())

    def test_parity_helper_rejects_a_short_count_and_accepts_a_note_separately(self):
        status, reason = date_pack_status(
            {"processed_date": "2026-09-25", "activity_rows": 2, "pdfs": 1}, (),
        )
        self.assertEqual(status, "HELD")
        self.assertIn("2 activity rows and 1 PDFs", reason)
        note = parse_activity_grid(
            activity_grid((("1234567890", "Dashboard Alert", "09/25/2026", "Alert"),), (0,), (0,)),
            lob="pl",
        ).held_notes
        status, reason = date_pack_status(
            {"processed_date": "2026-09-25", "activity_rows": 0, "pdfs": 0}, note,
        )
        self.assertEqual(status, "HELD")
        self.assertIn("not saved as PDFs", reason)


class LocatorAndBrowserTests(unittest.TestCase):
    def test_registered_names_are_exact_and_not_positional(self):
        page = LocatorRegistry().get_page("travelers", "policy_activity")
        self.assertIsNotNone(page)
        selectors = []
        for field in page.fields.values():
            field.validate()
            selectors.append(field.primary_selector)
            if field.fallback_selector:
                selectors.append(field.fallback_selector)
        self.assertIn("tab:Personal Insurance", selectors)
        self.assertIn("tab:Business Insurance", selectors)
        self.assertIn("link:Policy Activity Report", selectors)
        self.assertIn("link:Insured PDF", selectors)
        self.assertIn("Processed Within", selectors)
        for selector in selectors:
            self.assertFalse(locator_is_positional_guess(selector))

    def test_one_travelers_tab_and_loopback_cdp_are_required(self):
        app = SimpleNamespace(url=LIST_URL)
        login = SimpleNamespace(url="https://www.travelers.com/login")
        other = SimpleNamespace(url="https://ezlynx.com/web/")
        self.assertIs(select_travelers_page([login, other, app]), app)
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_travelers_page([app, SimpleNamespace(url=LIST_URL + "/other")])
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_travelers_page([login])
        with self.assertRaises(IntakeHold):
            require_loopback_cdp("http://10.0.0.5:9222")
        self.assertEqual(require_loopback_cdp("http://127.0.0.1:9222"), "http://127.0.0.1:9222")

    def test_module_does_not_call_ezlynx_drive_or_print_html_to_pdf(self):
        text = Path("robie_job_engine/travelers_retrieval.py").read_text(encoding="utf-8")
        for banned in (
            "upload_applicant_document",
            "add_note_to_discussion",
            "create_task_once",
            "DocumentApi",
            "DiscussionApi",
            "googleapiclient",
            "GoogleDriveUploader",
            "page.pdf",
            "systemd",
            "hermes-poc-01",
        ):
            self.assertNotIn(banned, text)

    def test_navigation_order_and_monday_custom_range(self):
        pl = _recording_page(
            "https://www.travelers.com/foragents/home",
            {
                ("role:tab", "Personal Insurance"),
                ("role:link", "Featured Reports"),
                ("role:link", "Policy Activity Report"),
                ("label", "Processed Within"),
                ("role:button", "Search"),
            },
        )
        browser = PlaywrightTravelersActivityBrowser(pl)
        browser.open_policy_activity(
            lob="pl", start=PROVE_DAY, end=PROVE_DAY, as_of=SATURDAY, include_weekends=False,
        )
        self.assertEqual(
            browser.trail,
            ["Personal Insurance", "Featured Reports", "Policy Activity Report", "last_day", "Search"],
        )
        self.assertEqual(pl.filled["Processed Within"], "Last Day")

        cl = _recording_page(
            "https://agent.travelers.com/home",
            {
                ("role:tab", "Business Insurance"),
                ("role:link", "Agency Reports"),
                ("role:link", "Policy Activity Report"),
                ("label", "Processed date from"),
                ("label", "Processed date to"),
                ("role:button", "Search"),
            },
        )
        browser = PlaywrightTravelersActivityBrowser(cl)
        browser.open_policy_activity(
            lob="cl", start=SATURDAY, end=SUNDAY, as_of=MONDAY, include_weekends=True,
        )
        self.assertEqual(
            browser.trail,
            ["Business Insurance", "Agency Reports", "Policy Activity Report", "custom_range", "Search"],
        )
        self.assertEqual(cl.filled["Processed date from"], "09/26/2026")
        self.assertEqual(cl.filled["Processed date to"], "09/27/2026")

        missing = _recording_page(
            "https://www.travelers.com/foragents/home",
            {("role:tab", "Personal Insurance"), ("role:button", "Search")},
        )
        with self.assertRaisesRegex(IntakeHold, "Featured Reports"):
            PlaywrightTravelersActivityBrowser(missing).open_policy_activity(
                lob="pl", start=PROVE_DAY, end=PROVE_DAY, as_of=SATURDAY, include_weekends=False,
            )
        self.assertNotIn(("role:button", "Search"), missing.clicked)

        locked = _recording_page("https://www.travelers.com/login", set())
        with self.assertRaisesRegex(IntakeHold, "not authenticated"):
            PlaywrightTravelersActivityBrowser(locked).open_policy_activity(
                lob="pl", start=PROVE_DAY, end=PROVE_DAY, as_of=SATURDAY, include_weekends=False,
            )
        self.assertEqual(locked.clicked, [])

    def test_screenshot_is_a_full_page_png_and_the_grid_counts_pdf_controls(self):
        page = _recording_page(LIST_URL, set())
        page.table_count = 1
        browser = PlaywrightTravelersActivityBrowser(page)
        browser._grid = activity_grid((), (), ())
        browser._list_url = LIST_URL
        self.assertEqual(browser.screenshot_policy_activity(), LIST_PNG)
        self.assertEqual(page.shot_kwargs, {"full_page": True, "type": "png"})

        headers, rows, insured, agent, _locators = _extract_sample()
        self.assertEqual(headers[0], "Policy Number")
        self.assertEqual(rows[0][1], "Renewal")
        self.assertEqual(insured, (1,))
        self.assertEqual(agent, (0,))


class CountOnly:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class AllNodes:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    def all(self):
        return self.nodes

    def count(self):
        return len(self.nodes)


class Hit:
    def __init__(self, page, kind, name):
        self.page = page
        self.kind = kind
        self.name = name

    def count(self):
        return 1 if (self.kind, self.name) in self.page.available else 0

    def click(self):
        if self.count() != 1:
            raise AssertionError(self.name)
        self.page.clicked.append((self.kind, self.name))

    def fill(self, value):
        self.page.filled[self.name] = value

    def input_value(self):
        return self.page.filled.get(self.name, "")

    def select_option(self, label):
        self.page.filled[self.name] = label

    def get_attribute(self, name):
        return None

    def is_disabled(self):
        return True


class RecordingPage:
    def __init__(self, url, available):
        self.url = url
        self.available = available
        self.clicked = []
        self.filled = {}
        self.password_count = 0
        self.table_count = 0
        self.shot_kwargs = None

    def locator(self, selector):
        if selector == "input[type='password']":
            return CountOnly(self.password_count)
        if selector == "table":
            return CountOnly(self.table_count)
        return CountOnly(0)

    def get_by_role(self, role, name, exact=True):
        return Hit(self, f"role:{role}", name)

    def get_by_label(self, label, exact=True):
        return Hit(self, "label", label)

    def screenshot(self, **kwargs):
        self.shot_kwargs = kwargs
        return LIST_PNG


def _recording_page(url, available):
    return RecordingPage(url, available)


class _Text:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _Row:
    def __init__(self, cells, insured, agent):
        self.cells = [_Text(cell) for cell in cells]
        self.insured = insured
        self.agent = agent

    def locator(self, selector):
        if selector == "td":
            return AllNodes(self.cells)
        raise AssertionError(selector)

    def get_by_role(self, role, name, exact=True):
        if role != "link":
            return CountOnly(0)
        if name == "Insured PDF":
            return CountOnly(self.insured)
        if name == "Agent PDF":
            return CountOnly(self.agent)
        return CountOnly(0)


class _Section:
    def __init__(self, nodes):
        self.nodes = nodes

    def count(self):
        return 1

    def locator(self, selector):
        if selector in {"th", "tr"}:
            return AllNodes(self.nodes)
        raise AssertionError(selector)


class _Table:
    def __init__(self, headers, rows):
        self.headers = headers
        self.rows = rows

    def count(self):
        return 1

    def locator(self, selector):
        if selector == "thead":
            return _Section([_Text(header) for header in self.headers])
        if selector == "tbody":
            return _Section(self.rows)
        raise AssertionError(selector)


class _TablePage:
    def __init__(self, table):
        self.table = table

    def locator(self, selector):
        if selector == "table":
            return self.table
        raise AssertionError(selector)


def _extract_sample():
    from robie_job_engine.travelers_retrieval import extract_activity_grid

    table = _Table(
        HEADERS,
        [_Row(("1234567890", "Renewal", "09/25/2026", "Policy"), 1, 0)],
    )
    return extract_activity_grid(_TablePage(table))


if __name__ == "__main__":
    unittest.main()
