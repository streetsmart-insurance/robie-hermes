"""Fixture tests for the NatGen Pending Cancellation NOC pull. No live login."""
from __future__ import annotations

import base64
import io
import json
import os
import unittest
import zlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.intake_core import IntakeHold, SourceArchive
from robie_job_engine.locator_registry import LocatorRegistry
from robie_job_engine.natgen_pending_cancellation import (
    DEFAULT_QA_ROOT,
    DRIVE_QA_PARENT_ID,
    DRIVE_UPLOAD_UNAVAILABLE,
    HistoryEntry,
    LocalDeliveryLedger,
    NatGenPendingCancellationPortal,
    NocGrid,
    NocOpenObservation,
    PagePdfView,
    build_parser,
    cancel_effective_date_in_pdf,
    choose_most_recent_noc,
    classify_noc_row,
    click_forms_view,
    main,
    noc_filename,
    open_pending_cancellations,
    parse_noc_grid,
    pdf_bytes_from_observation,
    read_playwright_pdf_view,
    require_loopback_cdp,
    require_noc_pdf_parity,
    select_natgen_page,
)
from robie_job_engine.natgen_retrieval import (
    ADDITIONAL_INFO_TODO,
    NatGenRetrieval,
    scrub_window,
)
from robie_job_engine.playwright_write_guard import locator_is_positional_guess


LIST_URL = "https://www.natgenagency.com/agency/pending-cancellations"
LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
FRIDAY = date(2026, 9, 25)
MONDAY = date(2026, 9, 28)
HEADERS = (
    "Policy Number",
    "Insured",
    "Reason",
    "Process Date",
    "Cancel Effective Date",
    "Open",
)


def noc_pdf(cancel_on: date, mark: bytes = b"") -> bytes:
    label = cancel_on.strftime("%m/%d/%Y")
    return b"%PDF-1.4\n" + f"Cancel Effective Date: {label}\n".encode() + mark + b"\n%%EOF\n"


def grid_from_rows(rows, *, controls=None, more_pages=False, headers=HEADERS, url=LIST_URL) -> NocGrid:
    counts = controls if controls is not None else tuple(1 for _ in rows)
    return NocGrid(url, headers, tuple(rows), counts, more_pages)


class ScriptedBrowser:
    def __init__(self, grid: NocGrid, pdfs: dict[str, bytes]):
        self.grid = grid
        self.pdfs = pdfs
        self.loads = 0
        self.shots = 0
        self.screenshot_bytes = LIST_PNG
        self.captures: list[str] = []
        self.events: list[str] = []

    def load_pending_cancellations(self):
        self.loads += 1
        self.events.append("load")
        return self.grid

    def screenshot_pending_cancellations(self):
        self.shots += 1
        self.events.append("shot")
        return self.screenshot_bytes

    def capture_noc(self, document_id):
        self.captures.append(document_id)
        self.events.append("capture")
        return NocOpenObservation(downloads=(self.pdfs[document_id],), pages=())


class ParseTests(unittest.TestCase):
    def test_non_payment_filename_includes_the_reason(self):
        self.assertEqual(noc_filename("NG100001", "Non-Payment"), "NG100001 NatGen NOC non-payment.pdf")
        self.assertEqual(noc_filename("NG100001", "nonpayment"), "NG100001 NatGen NOC non-payment.pdf")
        self.assertEqual(noc_filename("NG100001", "Non Payment."), "NG100001 NatGen NOC non-payment.pdf")
        with self.assertRaises(IntakeHold):
            noc_filename("NG100001", "../non-payment")
        with self.assertRaises(IntakeHold):
            noc_filename("NG", "non-payment")

    def test_monday_scrub_includes_the_prior_weekend_only(self):
        self.assertEqual(MONDAY.weekday(), 0)
        self.assertEqual(scrub_window(MONDAY, MONDAY), (date(2026, 9, 26), MONDAY))
        tuesday = date(2026, 9, 29)
        self.assertEqual(tuesday.weekday(), 1)
        self.assertEqual(scrub_window(tuesday, tuesday), (tuesday, tuesday))
        self.assertEqual(scrub_window(date(2026, 9, 25), MONDAY), (date(2026, 9, 25), MONDAY))
        self.assertEqual(scrub_window(date(2026, 9, 26), MONDAY), (date(2026, 9, 26), MONDAY))

    def test_row_uses_process_date_and_cancel_effective_date(self):
        row = ("NG100001", "Ada Agency LLC", "Non-Payment", "9/25/2026", "10/15/2026", "Open")
        parsed = parse_noc_grid(grid_from_rows((row,)))
        self.assertEqual(parsed[0].filename, "NG100001 NatGen NOC non-payment.pdf")
        self.assertEqual(parsed[0].processed_on, FRIDAY)
        self.assertEqual(parsed[0].cancel_effective, date(2026, 10, 15))
        self.assertEqual(parsed[0].insured_name, "Ada Agency LLC")

    def test_other_notice_without_a_policy_control_is_not_taken(self):
        headers = HEADERS[:5] + ("Type",)
        rows = (
            ("NG100001", "Ada Agency LLC", "Non-Payment", "09/25/2026", "10/15/2026", "Pending Cancellation"),
            ("NG100009", "Other Insured", "Billing", "09/25/2026", "10/15/2026", "Letter"),
        )
        parsed = parse_noc_grid(grid_from_rows(rows, controls=(1, 0), headers=headers))
        self.assertEqual(tuple(row.policy_number for row in parsed), ("NG100001",))

    def test_letter_row_with_a_policy_control_holds_the_list(self):
        headers = HEADERS[:5] + ("Type",)
        rows = (
            ("NG100009", "Other Insured", "Billing", "09/25/2026", "10/15/2026", "Letter"),
        )
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            parse_noc_grid(grid_from_rows(rows, controls=(1,), headers=headers))

    def test_duplicate_policy_reason_date_and_cancel_holds(self):
        row = ("NG100001", "Ada Agency LLC", "Non-Payment", "09/25/2026", "10/15/2026", "Open")
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            parse_noc_grid(grid_from_rows((row, row), controls=(1, 1)))

    def test_ragged_blank_insured_and_bad_policy_hold(self):
        bad = (
            grid_from_rows((("NG100001", "Only two"),)),
            grid_from_rows((("NG100001", "", "Non-Payment", "09/25/2026", "10/15/2026", "Open"),)),
            grid_from_rows((("NG", "Named", "Non-Payment", "09/25/2026", "10/15/2026", "Open"),)),
            grid_from_rows((), url="http://www.natgenagency.com/pending"),
        )
        for item in bad:
            with self.subTest(rows=item.rows):
                with self.assertRaises(IntakeHold):
                    parse_noc_grid(item)

    def test_classify_requires_exactly_one_policy_control(self):
        self.assertEqual(classify_noc_row("Pending Cancellation", 1), "take")
        self.assertEqual(classify_noc_row("NOC", 1), "take")
        self.assertEqual(classify_noc_row("Pending Cancellation", 0), "ambiguous")
        self.assertEqual(classify_noc_row("Letter", 0), "skip")
        self.assertEqual(classify_noc_row(None, 1), "take")
        self.assertEqual(classify_noc_row(None, 0), "ambiguous")

    def test_most_recent_history_noc_wins_and_a_tie_holds(self):
        chosen = choose_most_recent_noc((
            HistoryEntry("Pending Cancellation", date(2026, 9, 1), 1, 0),
            HistoryEntry("NOC", date(2026, 10, 1), 1, 1),
            HistoryEntry("Endorsement", date(2026, 11, 1), 1, 2),
        ))
        self.assertEqual(chosen.row_index, 1)
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            choose_most_recent_noc((
                HistoryEntry("Pending Cancellation", date(2026, 10, 1), 1, 0),
                HistoryEntry("NOC", date(2026, 10, 1), 1, 1),
            ))
        with self.assertRaises(IntakeHold):
            choose_most_recent_noc((HistoryEntry("Endorsement", date(2026, 10, 1), 1, 0),))


class PdfDateTests(unittest.TestCase):
    def test_labeled_cancel_effective_date_is_read(self):
        self.assertEqual(cancel_effective_date_in_pdf(noc_pdf(date(2026, 10, 15))), date(2026, 10, 15))

    def test_flate_stream_date_is_read(self):
        payload = b"Cancel Effective Date: 10/01/2026"
        blob = b"%PDF-1.4\nstream\n" + zlib.compress(payload) + b"\nendstream\n%%EOF\n"
        self.assertEqual(cancel_effective_date_in_pdf(blob), date(2026, 10, 1))

    def test_missing_or_disagreeing_labels_hold(self):
        with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
            cancel_effective_date_in_pdf(b"%PDF-1.4\nno labeled date\n%%EOF\n")
        both = (
            b"%PDF-1.4\nCancel Effective Date: 10/15/2026\n"
            b"NOC Effective Date: 10/16/2026\n%%EOF\n"
        )
        with self.assertRaisesRegex(IntakeHold, "missing or ambiguous"):
            cancel_effective_date_in_pdf(both)

    def test_disagreeing_pdf_bytes_hold(self):
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            pdf_bytes_from_observation(NocOpenObservation(
                downloads=(noc_pdf(date(2026, 10, 15), b"one"),),
                pages=(PagePdfView("blob:noc", (noc_pdf(date(2026, 10, 15), b"two"),)),),
            ))
        with self.assertRaisesRegex(IntakeHold, "not a PDF"):
            pdf_bytes_from_observation(NocOpenObservation(
                downloads=(b"<!DOCTYPE html>",),
                pages=(PagePdfView("blob:noc", (noc_pdf(date(2026, 10, 15)),)),),
            ))

    def test_off_host_pdf_is_not_fetched(self):
        calls = []

        class Response:
            def body(self):
                return noc_pdf(date(2026, 10, 15))

        class Request:
            def get(self, url, timeout):
                calls.append(url)
                return Response()

        class Page:
            def __init__(self, url, embeds):
                self.url = url
                self.context = SimpleNamespace(request=Request())
                self._embeds = embeds

            def locator(self, selector):
                return SimpleNamespace(
                    count=lambda: len(self._embeds.get(selector, [])),
                    all=lambda: self._embeds.get(selector, []),
                )

        page = Page("https://www.natgenagency.com/print", {
            "a": [SimpleNamespace(get_attribute=lambda name: "https://cdn.example/noc.pdf" if name == "href" else None)],
        })
        self.assertEqual(read_playwright_pdf_view(page).pdfs, ())
        self.assertEqual(calls, [])

        encoded = base64.b64encode(noc_pdf(date(2026, 10, 15))).decode("ascii")
        blob = Page("blob:https://www.natgenagency.com/noc", {})
        blob.evaluate = lambda script, url: encoded
        self.assertEqual(read_playwright_pdf_view(blob).pdfs, (noc_pdf(date(2026, 10, 15)),))


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile_dir()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"
        self.ledger = LocalDeliveryLedger(self.output)
        self.archive = SourceArchive(self.output / "sources")
        self.rows = (
            ("NG100001", "Ada Agency LLC", "Non-Payment", "09/25/2026", "10/15/2026", "Open"),
            ("NG100002", "Bram LLC", "Non-Payment", "09/25/2026", "10/20/2026", "Open"),
        )
        self.grid = grid_from_rows(self.rows)
        self.parsed = parse_noc_grid(self.grid)
        self.pdfs = {
            row.document_id: noc_pdf(row.cancel_effective, row.policy_number.encode())
            for row in self.parsed
        }
        self.browser = ScriptedBrowser(self.grid, self.pdfs)
        self.portal = NatGenPendingCancellationPortal(self.browser, self.ledger)
        self.api = SimpleNamespace(writes=0)
        self.worker = NatGenRetrieval(self.api, self.archive)

    def test_pull_writes_named_pdfs_once_and_skips_them_on_replay(self):
        items = self.worker.pull_pending_cancellation_noc(self.portal, start=FRIDAY, end=FRIDAY)
        self.assertEqual(
            tuple(item.filename for item in items),
            ("NG100001 NatGen NOC non-payment.pdf", "NG100002 NatGen NOC non-payment.pdf"),
        )
        self.assertEqual(self.browser.events[0], "load")
        self.assertEqual(self.browser.events[1], "shot")
        self.assertEqual(self.browser.captures, [row.document_id for row in self.parsed])
        self.assertLess(self.browser.events.index("shot"), self.browser.events.index("capture"))
        day = self.output / "2026-09-25"
        shot = day / "pending-cancellations-noc-2026-09-25.png"
        self.assertEqual(shot.read_bytes(), LIST_PNG)
        self.assertEqual(shot.stat().st_mode & 0o777, 0o600)
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        readme = (day / "README.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["carrier"], "natgen")
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["noc_rows"], 2)
        self.assertEqual(manifest["pdfs"], 2)
        self.assertFalse(manifest["list_day_filtered"])
        self.assertEqual(manifest["drive"]["status"], "not_run")
        self.assertEqual(manifest["drive"]["parent_id"], DRIVE_QA_PARENT_ID)
        self.assertIsNone(manifest["drive"]["folder_id"])
        self.assertEqual(manifest["drive"]["path"], "Robie Carrier Pull QA (Nicole)/NatGen/2026-09-25/")
        self.assertIn("NG100001 NatGen NOC non-payment.pdf", readme)
        self.assertIn("Status: PULLED", readme)
        self.assertIn("not a day-filtered view", readme)
        self.assertIn("EZLynx: not_run", readme)
        self.assertEqual(self.api.writes, 0)
        self.assertTrue((self.output / "natgen-noc-ledger.json").is_file())
        self.assertFalse((day / "natgen-noc-ledger.json").exists())
        for item in items:
            named = day / item.filename
            self.assertEqual(named.read_bytes(), item.content)
            self.assertEqual(named.stat().st_mode & 0o777, 0o600)

        replay_browser = ScriptedBrowser(self.grid, self.pdfs)
        replay = NatGenPendingCancellationPortal(replay_browser, self.ledger)
        second = NatGenRetrieval(self.api, self.archive).pull_pending_cancellation_noc(
            replay, start=FRIDAY, end=FRIDAY,
        )
        self.assertEqual(second, ())
        self.assertEqual(replay_browser.captures, [])
        self.assertEqual(replay.verification["by_date"][0]["pdfs"], 2)
        self.assertIn("## Already present", (day / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(list(day.glob("pending-cancellations-noc-*.png")), [shot])

    def test_monday_keeps_weekend_rows_and_scrubs_tuesday(self):
        rows = (
            ("NG100001", "Ada Agency LLC", "Non-Payment", "09/26/2026", "10/01/2026", "Open"),
            ("NG100002", "Bram LLC", "Non-Payment", "09/27/2026", "10/02/2026", "Open"),
            ("NG100003", "Cara LLC", "Non-Payment", "09/28/2026", "10/03/2026", "Open"),
            ("NG100004", "Dora LLC", "Non-Payment", "09/29/2026", "10/04/2026", "Open"),
        )
        grid = grid_from_rows(rows)
        parsed = parse_noc_grid(grid)
        pdfs = {row.document_id: noc_pdf(row.cancel_effective, row.policy_number.encode()) for row in parsed}
        browser = ScriptedBrowser(grid, pdfs)
        portal = NatGenPendingCancellationPortal(browser, self.ledger)
        items = self.worker.pull_pending_cancellation_noc(portal, start=MONDAY, end=MONDAY)
        self.assertEqual(tuple(item.filename[:8] for item in items), ("NG100001", "NG100002", "NG100003"))
        self.assertEqual(browser.loads, 1)
        for day, policy in (
            ("2026-09-26", "NG100001"),
            ("2026-09-27", "NG100002"),
            ("2026-09-28", "NG100003"),
        ):
            folder = self.output / day
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "PULLED")
            self.assertEqual(manifest["requested_window"], {"start": "2026-09-28", "end": "2026-09-28"})
            self.assertEqual(manifest["window"], {"start": "2026-09-26", "end": "2026-09-28"})
            self.assertTrue(manifest["screenshot_covers_window"])
            self.assertEqual([noc["policy_number"] for noc in manifest["nocs"]], [policy])
            self.assertEqual((folder / f"pending-cancellations-noc-{day}.png").read_bytes(), LIST_PNG)
            scrubbed = [row["policy_number"] for row in manifest["scrubbed_out_of_window"]]
            self.assertEqual(scrubbed, ["NG100004"])
            self.assertIn("NG100004", (folder / "README.md").read_text(encoding="utf-8"))
        self.assertFalse((self.output / "2026-09-29").exists())

    def test_tuesday_does_not_pull_the_weekend(self):
        tuesday = date(2026, 9, 29)
        rows = (
            ("NG100001", "Ada Agency LLC", "Non-Payment", "09/26/2026", "10/01/2026", "Open"),
            ("NG100004", "Dora LLC", "Non-Payment", "09/29/2026", "10/04/2026", "Open"),
        )
        grid = grid_from_rows(rows)
        parsed = parse_noc_grid(grid)
        pdfs = {row.document_id: noc_pdf(row.cancel_effective) for row in parsed}
        browser = ScriptedBrowser(grid, pdfs)
        portal = NatGenPendingCancellationPortal(browser, self.ledger)
        items = self.worker.pull_pending_cancellation_noc(portal, start=tuesday, end=tuesday)
        self.assertEqual(tuple(item.filename[:8] for item in items), ("NG100004",))
        self.assertFalse((self.output / "2026-09-26").exists())
        readme = (self.output / "2026-09-29" / "README.md").read_text(encoding="utf-8")
        self.assertIn("NG100001", readme)
        self.assertIn("not day-filtered", readme)

    def test_cancel_effective_mismatch_is_noted_and_not_filed(self):
        wrong = dict(self.pdfs)
        wrong[self.parsed[0].document_id] = noc_pdf(date(2026, 10, 16), b"wrong-doc")
        browser = ScriptedBrowser(self.grid, wrong)
        portal = NatGenPendingCancellationPortal(browser, self.ledger)
        with self.assertRaisesRegex(IntakeHold, "does not match the list for NG100001"):
            self.worker.pull_pending_cancellation_noc(portal, start=FRIDAY, end=FRIDAY)
        day = self.output / "2026-09-25"
        official = day / "NG100001 NatGen NOC non-payment.pdf"
        held = day / "NG100001 NatGen NOC non-payment HELD.pdf"
        self.assertFalse(official.exists())
        self.assertEqual(held.read_bytes(), wrong[self.parsed[0].document_id])
        self.assertTrue((day / "NG100002 NatGen NOC non-payment.pdf").is_file())
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        readme = (day / "README.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["status"], "HELD")
        self.assertEqual(manifest["noc_rows"], 2)
        self.assertEqual(manifest["pdfs"], 1)
        self.assertEqual(portal.verification["gate"], "noc_rows_equal_pdfs_and_cancel_dates_match")
        dispositions = {noc["policy_number"]: noc["disposition"] for noc in manifest["nocs"]}
        self.assertEqual(dispositions["NG100001"], "date_mismatch")
        self.assertEqual(dispositions["NG100002"], "pulled")
        self.assertIn("list 2026-10-15 PDF 2026-10-16", readme)
        self.assertIn("Status: HELD", readme)
        self.assertNotIn('"status": "PULLED"', json.dumps(manifest))
        self.assertEqual(len(list((self.output / "sources").glob("*.source"))), 1)

        fixed = dict(self.pdfs)
        replay_browser = ScriptedBrowser(self.grid, fixed)
        replay = NatGenPendingCancellationPortal(replay_browser, self.ledger)
        items = NatGenRetrieval(self.api, self.archive).pull_pending_cancellation_noc(
            replay, start=FRIDAY, end=FRIDAY,
        )
        self.assertEqual(tuple(item.filename[:8] for item in items), ("NG100001",))
        self.assertEqual(official.read_bytes(), fixed[self.parsed[0].document_id])
        self.assertEqual(held.read_bytes(), wrong[self.parsed[0].document_id])
        self.assertEqual(
            json.loads((day / "manifest.json").read_text(encoding="utf-8"))["status"],
            "PULLED",
        )

    def test_unreadable_pdf_date_holds_without_an_official_file(self):
        bad = dict(self.pdfs)
        bad[self.parsed[0].document_id] = b"%PDF-1.4\nno labeled date\n%%EOF\n"
        browser = ScriptedBrowser(self.grid, bad)
        portal = NatGenPendingCancellationPortal(browser, self.ledger)
        with self.assertRaisesRegex(IntakeHold, "missing or ambiguous for NG100001"):
            self.worker.pull_pending_cancellation_noc(portal, start=FRIDAY, end=FRIDAY)
        day = self.output / "2026-09-25"
        self.assertFalse((day / "NG100001 NatGen NOC non-payment.pdf").exists())
        self.assertFalse((day / "NG100001 NatGen NOC non-payment HELD.pdf").exists())
        readme = (day / "README.md").read_text(encoding="utf-8")
        self.assertIn("missing or ambiguous", readme)
        self.assertIn("Status: HELD", readme)

    def test_additional_info_is_todo_and_does_not_navigate(self):
        with self.assertRaisesRegex(IntakeHold, "TODO"):
            self.worker.pull_policy_todos_additional_info(self.portal, start=FRIDAY, end=FRIDAY)
        with self.assertRaisesRegex(IntakeHold, "TODO"):
            self.portal.list_documents(scope="policy_todos_additional_info", start=FRIDAY, end=FRIDAY)
        self.assertEqual(self.browser.loads, 0)
        self.assertEqual(self.browser.captures, [])
        self.assertIn("Wave A", ADDITIONAL_INFO_TODO)

    def test_other_scopes_and_wide_windows_do_not_download(self):
        with self.assertRaises(IntakeHold):
            self.portal.list_documents(scope="guessed", start=FRIDAY, end=FRIDAY)
        self.assertEqual(self.browser.loads, 0)
        with self.assertRaisesRegex(IntakeHold, "32 inclusive"):
            self.worker.pull_pending_cancellation_noc(
                self.portal, start=date(2026, 1, 1), end=date(2026, 9, 1),
            )
        self.assertEqual(self.browser.captures, [])

    def test_enabled_next_page_holds_without_a_partial_download(self):
        browser = ScriptedBrowser(grid_from_rows(self.rows, more_pages=True), self.pdfs)
        portal = NatGenPendingCancellationPortal(browser, self.ledger)
        with self.assertRaisesRegex(IntakeHold, "incomplete or ambiguous"):
            self.worker.pull_pending_cancellation_noc(portal, start=FRIDAY, end=FRIDAY)
        self.assertEqual(browser.captures, [])
        self.assertEqual(browser.shots, 0)

    def test_conflicting_local_file_is_kept_and_not_replaced(self):
        path = self.ledger.date_dir(FRIDAY) / "NG100001 NatGen NOC non-payment.pdf"
        path.write_bytes(noc_pdf(date(2026, 10, 15), b"different"))
        with self.assertRaisesRegex(IntakeHold, "conflicts"):
            self.worker.pull_pending_cancellation_noc(self.portal, start=FRIDAY, end=FRIDAY)
        self.assertEqual(path.read_bytes(), noc_pdf(date(2026, 10, 15), b"different"))
        self.assertEqual(self.browser.captures, [])

    def test_production_and_unset_env_refuse_before_the_portal_is_asked(self):
        calls = []
        portal = SimpleNamespace(
            list_documents=lambda **kwargs: calls.append(kwargs),
            download_document=lambda document_id: calls.append(document_id),
        )
        for env in ({"ROBIE_ENV": "PRODUCTION"}, {"ROBIE_ENV": ""}):
            calls.clear()
            with patch.dict(os.environ, env):
                with self.assertRaisesRegex(IntakeHold, "TEST"):
                    self.worker.pull_pending_cancellation_noc(portal, start=FRIDAY, end=FRIDAY)
            self.assertEqual(calls, [])

    def test_empty_list_still_saves_a_zero_count_screenshot(self):
        browser = ScriptedBrowser(grid_from_rows(()), {})
        portal = NatGenPendingCancellationPortal(browser, self.ledger)
        items = self.worker.pull_pending_cancellation_noc(portal, start=FRIDAY, end=FRIDAY)
        self.assertEqual(items, ())
        day = self.output / "2026-09-25"
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["noc_rows"], 0)
        self.assertEqual(manifest["pdfs"], 0)
        self.assertEqual((day / "pending-cancellations-noc-2026-09-25.png").read_bytes(), LIST_PNG)

    def test_parity_accepts_zero_and_rejects_a_short_pdf_count(self):
        self.assertEqual(
            require_noc_pdf_parity(rows=(), ledger=self.ledger, start=FRIDAY, end=FRIDAY),
            ({"processed_date": "2026-09-25", "noc_rows": 0, "pdfs": 0},),
        )
        row = {
            "document_id": self.parsed[0].document_id,
            "processed_or_effective_date": "2026-09-25",
        }
        with self.assertRaisesRegex(IntakeHold, "1 NOC rows and 0 PDFs"):
            require_noc_pdf_parity(rows=(row,), ledger=self.ledger, start=FRIDAY, end=FRIDAY)

    def test_bad_screenshot_holds_before_any_download(self):
        self.browser.screenshot_bytes = b"GIF89a"
        with self.assertRaisesRegex(IntakeHold, "not a PNG"):
            self.worker.pull_pending_cancellation_noc(self.portal, start=FRIDAY, end=FRIDAY)
        self.assertEqual(self.browser.captures, [])
        self.assertFalse((self.output / "2026-09-25" / "manifest.json").exists())

    def test_cli_prints_a_pull_receipt_and_refuses_production(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--start", "2026-09-25", "--end", "2026-09-25", "--output", str(self.output)],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "PULLED")
        self.assertEqual(payload["ezlynx"], "not_run")
        self.assertEqual(payload["additional_info"], "TODO")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["process"], "natgen")

        stdout = io.StringIO()
        called = []
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}):
            with patch("sys.stdout", stdout):
                code = main(
                    ["--start", "2026-09-25", "--end", "2026-09-25", "--output", str(self.output)],
                    browser_factory=lambda args: called.append("attached"),
                )
        self.assertEqual(code, 2)
        self.assertEqual(called, [])
        self.assertEqual(json.loads(stdout.getvalue())["status"], "HELD")

    def test_cli_holds_a_wide_window_before_attaching(self):
        called = []
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--start", "2026-01-01", "--end", "2026-09-01", "--output", str(self.output)],
                browser_factory=lambda args: called.append("attached"),
            )
        self.assertEqual(code, 2)
        self.assertEqual(called, [])
        self.assertIn("32 inclusive", json.loads(stdout.getvalue())["reason"])
        self.assertFalse(self.output.exists())

    def test_default_output_is_the_hermes_qa_root_and_does_not_create_it(self):
        existed = DEFAULT_QA_ROOT.exists()
        args = build_parser().parse_args(["--start", "2026-09-28", "--end", "2026-09-28"])
        self.assertEqual(Path(args.output), DEFAULT_QA_ROOT)
        self.assertFalse(args.upload_drive)
        self.assertEqual(DEFAULT_QA_ROOT.exists(), existed)
        self.assertEqual(
            DEFAULT_QA_ROOT,
            Path("/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/natgen"),
        )

    def test_upload_drive_writes_the_local_pack_then_fails_closed(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--start", "2026-09-25", "--end", "2026-09-25", "--output", str(self.output), "--upload-drive"],
                browser_factory=lambda args: self.browser,
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "HELD")
        self.assertIn("not available", payload["reason"])
        self.assertEqual(payload["reason"], DRIVE_UPLOAD_UNAVAILABLE)
        self.assertEqual(payload["verification"]["drive_upload"], "HELD")
        self.assertEqual(payload["ezlynx"], "not_run")
        day = self.output / "2026-09-25"
        manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PULLED")
        self.assertEqual(manifest["drive"]["status"], "HELD")
        self.assertEqual(manifest["drive"]["parent_id"], DRIVE_QA_PARENT_ID)
        self.assertIsNone(manifest["drive"]["folder_id"])
        self.assertIn("refusing to report the pack as uploaded", (day / "README.md").read_text(encoding="utf-8"))
        self.assertTrue((day / "NG100001 NatGen NOC non-payment.pdf").is_file())


class LocatorContractTests(unittest.TestCase):
    def test_registered_names_are_exact_and_not_positional(self):
        page = LocatorRegistry().get_page("natgen", "pending_cancellations")
        self.assertIsNotNone(page)
        selectors = []
        for field in page.fields.values():
            field.validate()
            self.assertTrue(field.exact)
            selectors.append(field.primary_selector)
            if field.fallback_selector:
                selectors.append(field.fallback_selector)
        self.assertIn("link:Agent Dashboard", selectors)
        self.assertIn("link:Your Notifications", selectors)
        self.assertIn("link:Policy To Dos", selectors)
        self.assertIn("link:Pending Cancellations", selectors)
        self.assertIn("link:Policy History", selectors)
        self.assertIn("link:Forms View", selectors)
        for selector in selectors:
            self.assertFalse(locator_is_positional_guess(selector))

    def test_one_natgen_tab_is_required(self):
        app = SimpleNamespace(url=LIST_URL)
        login = SimpleNamespace(url="https://login.natgenagency.com/login")
        other = SimpleNamespace(url="https://ezlynx.com/web/")
        self.assertIs(select_natgen_page([login, other, app]), app)
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_natgen_page([app, SimpleNamespace(url=LIST_URL + "/other")])
        with self.assertRaisesRegex(IntakeHold, "exactly one"):
            select_natgen_page([login])

    def test_remote_cdp_is_refused(self):
        with self.assertRaises(IntakeHold):
            require_loopback_cdp("http://10.0.0.5:9222")
        self.assertEqual(require_loopback_cdp("http://127.0.0.1:9222"), "http://127.0.0.1:9222")

    def test_module_has_no_ezlynx_write_or_timer(self):
        text = Path("robie_job_engine/natgen_pending_cancellation.py").read_text(encoding="utf-8")
        retrieval = Path("robie_job_engine/natgen_retrieval.py").read_text(encoding="utf-8")
        for banned in (
            "upload_applicant_document",
            "add_note_to_discussion",
            "create_task_once",
            "DocumentApi",
            "DiscussionApi",
            "googleapiclient",
            "GoogleDriveUploader",
            "systemd",
            ".timer",
        ):
            self.assertNotIn(banned, text)
            self.assertNotIn(banned, retrieval)

    def test_navigation_does_not_fill_a_date_and_login_holds_first(self):
        page = RolePage({
            ("link", "Agent Dashboard"): 1,
            ("link", "Your Notifications"): 1,
            ("link", "Policy To Dos"): 1,
            ("link", "Pending Cancellations"): 1,
        })
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [
            "Agent Dashboard",
            "Your Notifications",
            "Policy To Dos",
            "Pending Cancellations",
        ])
        self.assertEqual(page.label_calls, [])

        locked = RolePage({}, password_count=1)
        with self.assertRaisesRegex(IntakeHold, "not authenticated"):
            open_pending_cancellations(locked)
        self.assertEqual(locked.clicks, [])

        ambiguous = RolePage({
            ("link", "Agent Dashboard"): 1,
            ("button", "Agent Dashboard"): 1,
        })
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            open_pending_cancellations(ambiguous)

        forms = RolePage({
            ("link", "Forms View"): 1,
            ("button", "View PDF"): 1,
        })
        with self.assertRaisesRegex(IntakeHold, "Forms View"):
            click_forms_view(forms)


class RolePage:
    def __init__(self, roles, *, password_count=0, url=LIST_URL):
        self.roles = roles
        self.password_count = password_count
        self.url = url
        self.clicks = []
        self.label_calls = []

    def get_by_role(self, role, name, exact=True):
        del exact
        count = self.roles.get((role, name), 0)
        page = self

        class Locator:
            def count(self):
                return count

            def click(self):
                page.clicks.append(name)

        return Locator()

    def get_by_label(self, label, exact=True):
        del exact
        self.label_calls.append(label)
        raise AssertionError("Pending Cancellations list is not day-filtered")

    def locator(self, selector):
        count = self.password_count if selector == "input[type='password']" else 0
        return SimpleNamespace(count=lambda: count)


def tempfile_dir():
    import tempfile
    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
