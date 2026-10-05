"""Fixture tests for the NatGen Pending Cancellation NOC pull. No live login."""
from __future__ import annotations

import base64
import io
import json
import os
import re
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
    PENDING_LINK_MATCHED_MORE_THAN_ONCE,
    PENDING_LINK_NOT_FOUND,
    PENDING_LINK_NOT_R5,
    PENDING_TABLE_CSS,
    PENDING_TABLE_DID_NOT_APPEAR,
    PENDING_WIDGET_FAILED,
    HistoryEntry,
    LocalDeliveryLedger,
    NatGenPendingCancellationPortal,
    NocGrid,
    NocOpenObservation,
    PagePdfView,
    _PENDING_LINK_NAME,
    build_parser,
    cancel_effective_date_in_pdf,
    choose_most_recent_noc,
    classify_noc_row,
    click_forms_view,
    extract_doc_guid,
    guid_document_id,
    guid_from_observation,
    main,
    noc_filename,
    nonrenewal_document_id,
    open_pending_cancellations,
    parse_noc_grid,
    parse_nonrenewal_grid,
    pdf_bytes_from_observation,
    read_playwright_pdf_view,
    require_doc_guid,
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
        }, url="https://www.natgenagency.com/dashboard", hrefs={
            "Pending Cancellations": "/Reports/AgencyActivityReports.aspx?r=5",
        })
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [
            "Agent Dashboard",
            "Your Notifications",
            "Policy To Dos",
            "Pending Cancellations",
        ])
        self.assertEqual(page.label_calls, [])
        self.assertEqual(page.gotos, [])

        locked = RolePage({}, password_count=1)
        with self.assertRaisesRegex(IntakeHold, "not authenticated"):
            open_pending_cancellations(locked)
        self.assertEqual(locked.clicks, [])

        ambiguous = RolePage({
            ("link", "Agent Dashboard"): 1,
            ("button", "Agent Dashboard"): 1,
        }, url="https://www.natgenagency.com/dashboard")
        with self.assertRaisesRegex(IntakeHold, "ambiguous"):
            open_pending_cancellations(ambiguous)
        self.assertEqual(ambiguous.clicks, [])

    def test_pending_report_url_skips_agent_dashboard(self):
        page = RolePage(
            {("link", "Agent Dashboard"): 1},
            url="https://www.natgenagency.com/reports/agency-activity/pending-cancellations",
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [])

    def test_agency_activity_page_with_rows_skips_agent_dashboard(self):
        page = RolePage(
            {
                ("link", "Agent Dashboard"): 1,
                ("heading", "Pending Cancellations Agency Activity"): 1,
            },
            url="https://www.natgenagency.com/reports/agency-activity",
            table_count=1,
            row_count=2,
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [])

    def test_heading_and_rows_skip_dashboard_when_the_url_is_opaque(self):
        page = RolePage(
            {("heading", "Pending Cancellations"): 1},
            url="https://www.natgenagency.com/portal/default.aspx",
            table_count=1,
            row_count=2,
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [])

    def test_missing_agent_dashboard_still_walks_the_later_steps(self):
        page = RolePage({
            ("link", "Your Notifications"): 1,
            ("link", "Policy To Dos"): 1,
            ("link", "Pending Cancellations"): 1,
        }, url="https://www.natgenagency.com/dashboard", hrefs={
            "Pending Cancellations": "/Reports/AgencyActivityReports.aspx?r=5",
        })
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [
            "Your Notifications",
            "Policy To Dos",
            "Pending Cancellations",
        ])

    def test_agency_activity_reports_r5_skips_nav(self):
        page = RolePage(
            {},
            url="https://natgenagency.com/Reports/AgencyActivityReports.aspx?r=5",
            table_count=1,
            row_count=2,
            body="Pending Cancel for Non Payment",
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [])

    def test_other_agency_activity_report_id_is_not_this_list(self):
        page = RolePage(
            {},
            url="https://natgenagency.com/Reports/AgencyActivityReports.aspx?r=9",
        )
        with self.assertRaises(IntakeHold) as caught:
            open_pending_cancellations(page)
        self.assertEqual(str(caught.exception), PENDING_LINK_NOT_FOUND)
        self.assertEqual(page.clicks, [])

    def test_encoded_pending_url_skips_agent_dashboard(self):
        page = RolePage(
            {("link", "Agent Dashboard"): 1},
            url="https://www.natgenagency.com/reports?name=Pending+Cancellations",
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [])

    def test_report_text_and_rows_skip_dashboard_when_it_is_absent(self):
        page = RolePage(
            {},
            url="https://www.natgenagency.com/portal/default.aspx",
            table_count=1,
            row_count=2,
            body="Pending Cancellations\nAgency Activity\nPolicy 1\nPolicy 2",
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [])

    def test_dashboard_with_a_pending_link_and_rows_still_uses_the_playbook(self):
        page = RolePage({
            ("link", "Agent Dashboard"): 1,
            ("link", "Your Notifications"): 1,
            ("link", "Policy To Dos"): 1,
            ("link", "Pending Cancellations"): 1,
        }, url="https://www.natgenagency.com/dashboard", table_count=1, row_count=2, body="Pending Cancellations", hrefs={
            "Pending Cancellations": "/Reports/AgencyActivityReports.aspx?r=5",
        })
        open_pending_cancellations(page)
        self.assertEqual(page.clicks[0], "Agent Dashboard")
        self.assertIn("Pending Cancellations", page.clicks)

    def test_report_not_found_holds_when_dashboard_is_absent(self):
        page = RolePage({}, url="https://www.natgenagency.com/dashboard")
        with self.assertRaises(IntakeHold) as caught:
            open_pending_cancellations(page)
        self.assertEqual(str(caught.exception), PENDING_LINK_NOT_FOUND)
        self.assertEqual(page.clicks, [])

        forms = RolePage({
            ("link", "Forms View"): 1,
            ("button", "View PDF"): 1,
        })
        with self.assertRaisesRegex(IntakeHold, "Forms View"):
            click_forms_view(forms)


class RolePage:
    def __init__(
        self,
        roles,
        *,
        password_count=0,
        url=LIST_URL,
        table_count=0,
        row_count=0,
        body="",
        hrefs=None,
        on_refresh=None,
        pending_table=True,
        table_after_waits=0,
    ):
        self.roles = roles
        self.password_count = password_count
        self.url = url
        self.table_count = table_count
        self.row_count = row_count
        self.body = body
        self.hrefs = dict(hrefs or {})
        self.on_refresh = on_refresh
        self.pending_table = pending_table
        self.table_after_waits = table_after_waits
        self.clicks = []
        self.label_calls = []
        self.gotos = []
        self.wait_calls = []
        self.pending_clicked = False
        self.waits_after_click = 0
        self._now = 0.0

    def clock(self):
        return self._now

    def wait_for_timeout(self, ms):
        self.wait_calls.append(int(ms))
        self._now += int(ms) / 1000.0
        if self.pending_clicked:
            self.waits_after_click += 1

    def goto(self, url):
        self.gotos.append(url)
        raise AssertionError(f"typed URL navigation is refused: {url}")

    def _matching_names(self, role, name, exact):
        found = []
        for (item_role, item_name), item_count in self.roles.items():
            if item_role != role or not item_count:
                continue
            label = str(item_name)
            if isinstance(name, re.Pattern):
                matched = name.search(label) is not None
            elif exact:
                matched = label == str(name)
            else:
                matched = str(name).casefold() in label.casefold()
            if matched:
                found.extend([label] * int(item_count))
        return found

    def get_by_role(self, role, name=None, exact=True):
        page = self

        class Locator:
            def count(self):
                return len(page._matching_names(role, name, exact))

            def get_attribute(self, attr):
                if attr != "href":
                    return None
                names = page._matching_names(role, name, exact)
                if len(names) != 1:
                    return None
                return page.hrefs.get(names[0])

            def click(self):
                names = page._matching_names(role, name, exact)
                chosen = names[0] if names else (name if isinstance(name, str) else "")
                page.clicks.append(chosen)
                if isinstance(name, re.Pattern):
                    page.pending_clicked = True
                if chosen == "Refresh" and callable(page.on_refresh):
                    page.on_refresh(page)

            def nth(self, index):
                names = page._matching_names(role, name, exact)

                class One:
                    def count(self):
                        return 1 if 0 <= index < len(names) else 0

                    def get_attribute(self, attr):
                        if attr == "href" and 0 <= index < len(names):
                            return page.hrefs.get(names[index])
                        return None

                return One()

        return Locator()

    def get_by_label(self, label, exact=True):
        del exact
        self.label_calls.append(label)
        raise AssertionError("Pending Cancellations list is not day-filtered")

    def locator(self, selector):
        page = self

        def current_count():
            if selector == "input[type='password']":
                return page.password_count
            if selector == "table":
                return page.table_count
            if selector == "tbody tr":
                return page.row_count if page.table_count == 1 else 0
            if selector == "body":
                return 1
            if selector == PENDING_TABLE_CSS:
                if (
                    page.pending_table
                    and page.pending_clicked
                    and page.waits_after_click >= page.table_after_waits
                ):
                    return 1
                return 0
            return 0

        class Locator:
            def count(self):
                return current_count()

            def inner_text(self):
                return page.body if selector == "body" else ""

            def locator(self, child):
                return page.locator(child)

        return Locator()


DASHBOARD_URL = "https://www.natgenagency.com/dashboard"
R5_HREF = "/Reports/AgencyActivityReports.aspx?r=5"


class DashboardPendingLinkTests(unittest.TestCase):
    def _hold(self, page):
        with self.assertRaises(IntakeHold) as caught:
            open_pending_cancellations(page)
        return str(caught.exception)

    def test_counted_names_match_and_exact_string_does_not(self):
        for label in ("3 Pending Cancellations", "0 Pending Cancellations"):
            page = RolePage(
                {("link", label): 1},
                url=DASHBOARD_URL,
                hrefs={label: R5_HREF},
            )
            self.assertEqual(
                page.get_by_role("link", name="Pending Cancellations", exact=True).count(),
                0,
            )
            self.assertEqual(page.get_by_role("link", name=_PENDING_LINK_NAME).count(), 1)
            open_pending_cancellations(page)
            self.assertEqual(page.clicks, [label])
            self.assertEqual(page.gotos, [])

    def test_two_matching_links_hold(self):
        page = RolePage({
            ("link", "3 Pending Cancellations"): 1,
            ("link", "0 Pending Cancellations"): 1,
        }, url=DASHBOARD_URL, hrefs={
            "3 Pending Cancellations": R5_HREF,
            "0 Pending Cancellations": R5_HREF,
        })
        self.assertEqual(self._hold(page), PENDING_LINK_MATCHED_MORE_THAN_ONCE)
        self.assertEqual(page.clicks, [])

    def test_href_other_than_r5_holds(self):
        page = RolePage(
            {("link", "3 Pending Cancellations"): 1},
            url=DASHBOARD_URL,
            hrefs={"3 Pending Cancellations": "/Reports/AgencyActivityReports.aspx?r=9"},
        )
        self.assertEqual(self._hold(page), PENDING_LINK_NOT_R5)
        self.assertEqual(page.clicks, [])
        self.assertEqual(page.gotos, [])

    def test_failed_to_load_text_is_not_the_pending_link(self):
        page = RolePage(
            {("link", "Failed to Load Pending Cancellations"): 1},
            url=DASHBOARD_URL,
            body="Failed to Load Pending Cancellations. - Refresh",
        )
        self.assertEqual(page.get_by_role("link", name=_PENDING_LINK_NAME).count(), 0)
        self.assertEqual(self._hold(page), PENDING_LINK_NOT_FOUND)
        self.assertEqual(page.clicks, [])

    def test_headings_and_plain_elements_still_reach_the_counted_link(self):
        page = RolePage({
            ("heading", "Agent Dashboard"): 1,
            ("heading", "Your Notifications"): 1,
            ("link", "3 Pending Cancellations"): 1,
        }, url=DASHBOARD_URL, body="Policy To Dos", hrefs={
            "3 Pending Cancellations": R5_HREF,
        })
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, ["3 Pending Cancellations"])
        self.assertEqual(page.gotos, [])

    def test_refresh_then_the_counted_link_opens_the_report(self):
        def reveal(page):
            page.roles[("link", "3 Pending Cancellations")] = 1
            page.hrefs["3 Pending Cancellations"] = R5_HREF
            page.body = ""

        page = RolePage(
            {("button", "Refresh"): 1},
            url=DASHBOARD_URL,
            body="Failed to Load Pending Cancellations. - Refresh",
            on_refresh=reveal,
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, ["Refresh", "3 Pending Cancellations"])
        self.assertEqual(page.gotos, [])

    def test_two_failed_refreshes_hold_after_exactly_two_clicks(self):
        page = RolePage(
            {("button", "Refresh"): 1},
            url=DASHBOARD_URL,
            body="Failed to Load Pending Cancellations. - Refresh",
        )
        self.assertEqual(self._hold(page), PENDING_WIDGET_FAILED)
        self.assertEqual(page.clicks, ["Refresh", "Refresh"])

    def test_pending_table_is_required_after_the_click(self):
        missing = RolePage(
            {("link", "3 Pending Cancellations"): 1},
            url=DASHBOARD_URL,
            hrefs={"3 Pending Cancellations": R5_HREF},
            pending_table=False,
        )
        self.assertEqual(self._hold(missing), PENDING_TABLE_DID_NOT_APPEAR)
        self.assertEqual(missing.clicks, ["3 Pending Cancellations"])
        self.assertTrue(missing.wait_calls)

        waited = RolePage(
            {("link", "0 Pending Cancellations"): 1},
            url=DASHBOARD_URL,
            hrefs={"0 Pending Cancellations": R5_HREF},
            table_after_waits=1,
        )
        open_pending_cancellations(waited)
        self.assertEqual(waited.clicks, ["0 Pending Cancellations"])
        self.assertGreaterEqual(waited.waits_after_click, 1)

    def test_r5_shortcut_skips_the_dashboard_link(self):
        page = RolePage(
            {
                ("link", "Agent Dashboard"): 1,
                ("link", "3 Pending Cancellations"): 1,
            },
            url="https://natgenagency.com/Reports/AgencyActivityReports.aspx?r=5",
            hrefs={"3 Pending Cancellations": R5_HREF},
            table_count=1,
            row_count=2,
        )
        open_pending_cancellations(page)
        self.assertEqual(page.clicks, [])
        self.assertEqual(page.wait_calls, [])
        self.assertEqual(page.gotos, [])


def tempfile_dir():
    import tempfile
    return tempfile.TemporaryDirectory()



LIVE_HEADERS = ("POLICY", "NAMED INSURED", "PHONE #", "PRODUCT", "DIV", "REASON", "CANCEL DATE", "AMOUNT DUE", "ADDITIONAL PRODUCTS")


class LiveReportSnapshotTests(unittest.TestCase):
    def live_grid(self):
        row = ("2035471506 00", "A&E CONTRACTOR LLC", "(555) 555-0100", "CA", "1",
               "Pending Cancel for Non Payment", "10/7/2026", "$100.00", "")
        return grid_from_rows([row], headers=LIVE_HEADERS)

    def test_report_without_process_date_is_a_dated_snapshot(self):
        from robie_job_engine.natgen_pending_cancellation import SNAPSHOT_SOURCE

        rows = parse_noc_grid(self.live_grid(), snapshot_on=date(2026, 9, 30))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.policy_number, "2035471506 00")
        self.assertEqual(row.reason, "non-payment")
        self.assertEqual(row.cancel_effective, date(2026, 10, 7))
        self.assertEqual(row.processed_on, date(2026, 9, 30))
        self.assertEqual(row.processed_date_source, SNAPSHOT_SOURCE)
        self.assertEqual(row.document_id, "natgen-noc:2035471506 00:snapshot:non-payment:2026-10-07")

    def test_snapshot_keeps_the_first_seen_date(self):
        rows = parse_noc_grid(
            self.live_grid(), snapshot_on=date(2026, 9, 30), first_seen=lambda _doc: date(2026, 9, 29)
        )
        self.assertEqual(rows[0].processed_on, date(2026, 9, 29))

    def test_live_reason_wording(self):
        from robie_job_engine.natgen_pending_cancellation import normalize_reason

        self.assertEqual(normalize_reason("Pending cancel for NSF"), "nsf")
        self.assertEqual(normalize_reason("Pending Cancel for Non Payment"), "non-payment")

    def test_ledger_first_seen(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger = LocalDeliveryLedger(Path(tmp))
            self.assertIsNone(ledger.first_seen("natgen-noc:x"))


class GuidIdentityTests(unittest.TestCase):
    """The DisplayPDF iid GUID is the durable document identity."""

    GUID_A = "e549962d-9b21-425d-7db0-08df22c41ea7"
    GUID_B = "a1111111-2222-3333-4444-555555555555"
    PDF_URL_A = f"https://natgenagency.com/Policy/DisplayPDF.aspx?iid={GUID_A}"

    def test_extract_guid_from_displaypdf_url(self):
        self.assertEqual(extract_doc_guid(self.PDF_URL_A), self.GUID_A)

    def test_extract_guid_uppercases_to_lower(self):
        upper = self.GUID_A.upper()
        url = f"https://natgenagency.com/Policy/DisplayPDF.aspx?iid={upper}"
        self.assertEqual(extract_doc_guid(url), self.GUID_A)

    def test_extract_guid_rejects_wrong_path(self):
        with self.assertRaises(IntakeHold):
            extract_doc_guid("https://natgenagency.com/Policy/PolicySummary.aspx?iid=" + self.GUID_A)

    def test_extract_guid_rejects_wrong_host(self):
        with self.assertRaises(IntakeHold):
            extract_doc_guid("https://example.com/Policy/DisplayPDF.aspx?iid=" + self.GUID_A)

    def test_extract_guid_rejects_missing_iid(self):
        with self.assertRaises(IntakeHold):
            extract_doc_guid("https://natgenagency.com/Policy/DisplayPDF.aspx")

    def test_extract_guid_rejects_malformed_guid(self):
        with self.assertRaises(IntakeHold):
            extract_doc_guid("https://natgenagency.com/Policy/DisplayPDF.aspx?iid=not-a-guid")

    def test_require_doc_guid_ok(self):
        self.assertEqual(require_doc_guid(self.GUID_A), self.GUID_A)

    def test_require_doc_guid_rejects_blank(self):
        with self.assertRaises(IntakeHold):
            require_doc_guid("")

    def test_guid_document_id_format(self):
        self.assertEqual(
            guid_document_id("2031936859 00", self.GUID_A),
            f"natgen:2031936859 00:{self.GUID_A}",
        )

    def test_same_text_different_guid_is_distinct(self):
        # The reported duplicate-text failure mode: identical display text,
        # different GUIDs must key as distinct ledger documents.
        a = guid_document_id("2031936859 00", self.GUID_A)
        b = guid_document_id("2031936859 00", self.GUID_B)
        self.assertNotEqual(a, b)

    def test_guid_from_observation_single_page(self):
        obs = NocOpenObservation(
            downloads=(),
            pages=(PagePdfView(url=self.PDF_URL_A, pdfs=(b"%PDF-1.4 x",)),),
        )
        self.assertEqual(guid_from_observation(obs), self.GUID_A)

    def test_guid_from_observation_ignores_non_pdf_pages(self):
        obs = NocOpenObservation(
            downloads=(),
            pages=(
                PagePdfView(url="https://natgenagency.com/Policy/PolicySummary.aspx", pdfs=()),
                PagePdfView(url=self.PDF_URL_A, pdfs=(b"%PDF-1.4 x",)),
            ),
        )
        self.assertEqual(guid_from_observation(obs), self.GUID_A)

    def test_guid_from_observation_conflicting_guids_hold(self):
        obs = NocOpenObservation(
            downloads=(),
            pages=(
                PagePdfView(url=self.PDF_URL_A, pdfs=(b"%PDF-1.4 x",)),
                PagePdfView(
                    url=f"https://natgenagency.com/Policy/DisplayPDF.aspx?iid={self.GUID_B}",
                    pdfs=(b"%PDF-1.4 y",),
                ),
            ),
        )
        with self.assertRaises(IntakeHold):
            guid_from_observation(obs)

    def test_guid_from_observation_no_pages_holds(self):
        obs = NocOpenObservation(downloads=(b"%PDF-1.4 x",), pages=())
        with self.assertRaises(IntakeHold):
            guid_from_observation(obs)


class NonRenewalParseTests(unittest.TestCase):
    """Pending Non-Renewal report parsing (same ?r=5 URL, combobox-switched)."""

    HEADERS = (
        "POLICY", "NAMED INSURED", "PHONE", "TYPE", "PRODUCT", "DIV",
        "PROCESSED", "EFFECTIVE", "DESCRIPTION", "PREMIUM", "PRODUCER",
        "ADDITIONAL PRODUCTS",
    )
    ROWS = (
        ("2035017657 00", "Jane Smith", "555-0100", "Non-Renewal", "HO", "1",
         "09/20/2026", "11/01/2026", "Non-renewal notice", "$1,200.00", "Carlo", ""),
        ("2025758382 01", "Bob Jones", "555-0101", "Non-Renewal", "HO", "1",
         "09/21/2026", "11/02/2026", "Non-renewal notice", "$950.00", "Carlo", ""),
    )
    LIST_URL = "https://natgenagency.com/Reports/AgencyActivityReports.aspx?r=5"

    def test_live_columns_parse(self):
        rows = parse_nonrenewal_grid(self.HEADERS, self.ROWS, source_url=self.LIST_URL)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first.policy_number, "2035017657 00")
        self.assertEqual(first.insured_name, "Jane Smith")
        self.assertEqual(first.processed_on, date(2026, 9, 20))
        self.assertEqual(first.effective_on, date(2026, 11, 1))
        self.assertEqual(first.premium, "$1,200.00")
        self.assertEqual(first.row_index, 0)

    def test_document_id_format(self):
        rows = parse_nonrenewal_grid(self.HEADERS, self.ROWS, source_url=self.LIST_URL)
        self.assertTrue(
            rows[0].document_id.startswith("natgen-nonrenewal:2035017657 00:2026-09-20:")
        )
        self.assertNotEqual(rows[0].document_id, rows[1].document_id)

    def test_header_order_not_load_bearing(self):
        shuffled = ("EFFECTIVE", "POLICY", "NAMED INSURED", "PROCESSED",
                    "TYPE", "DESCRIPTION", "PREMIUM")
        rows_in = (("11/01/2026", "2035017657 00", "Jane Smith", "09/20/2026",
                    "Non-Renewal", "Non-renewal notice", "$1,200.00"),)
        rows = parse_nonrenewal_grid(shuffled, rows_in, source_url=self.LIST_URL)
        self.assertEqual(rows[0].insured_name, "Jane Smith")
        self.assertEqual(rows[0].effective_on, date(2026, 11, 1))

    def test_empty_rows_hold(self):
        with self.assertRaises(IntakeHold):
            parse_nonrenewal_grid(self.HEADERS, (), source_url=self.LIST_URL)

    def test_bad_policy_holds(self):
        bad = (("ABC",) + self.ROWS[0][1:],)
        with self.assertRaises(IntakeHold):
            parse_nonrenewal_grid(self.HEADERS, bad, source_url=self.LIST_URL)

    def test_blank_insured_holds(self):
        bad = ((self.ROWS[0][0], "") + self.ROWS[0][2:],)
        with self.assertRaises(IntakeHold):
            parse_nonrenewal_grid(self.HEADERS, bad, source_url=self.LIST_URL)

    def test_nonrenewal_document_id_helper(self):
        self.assertEqual(
            nonrenewal_document_id("2035017657 00", date(2026, 9, 20), date(2026, 11, 1)),
            "natgen-nonrenewal:2035017657 00:2026-09-20:2026-11-01",
        )

    def test_dash_separator_normalizes_to_canonical(self):
        # Live rows read "2035017657 - 00"; the parser normalizes to "2035017657 00".
        rows_in = (("2035017657 - 00",) + self.ROWS[0][1:],)
        rows = parse_nonrenewal_grid(self.HEADERS, rows_in, source_url=self.LIST_URL)
        self.assertEqual(rows[0].policy_number, "2035017657 00")


if __name__ == "__main__":
    unittest.main()
