"""Progressive BOP pending-cancel pull. No live FAO session."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from robie_job_engine.intake_core import IntakeHold
from robie_job_engine.progressive_bop import (
    BOP_SCOPE,
    DEFAULT_OUTPUT_ROOT,
    DRIVE_QA_PARENT_ID,
    DRIVE_BOP_CHILD_NAME,
    PendingCancelPolicy,
    PolicyDocument,
    ReportCapture,
    RowHold,
    assess_bop_gate,
    build_parser,
    main,
    module_import_names,
    navigate_to_pending_cancel,
    noc_filename,
    parse_excel_report,
    parse_pdf_report,
    parse_report_rows,
    policies_from_report_text,
    read_report_from_page,
    report_from_extracted,
    require_loopback_cdp,
    select_fao_page,
    select_notice,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
DAY = date(2026, 9, 26)
PDF_A = b"%PDF-1.4\nnotice-a\n%%EOF\n"
PDF_B = b"%PDF-1.4\nnotice-b\n%%EOF\n"


def _excel(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _text_pdf(text: str) -> bytes:
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 700 Td ({safe}) Tj ET".encode("latin-1")
    chunks = [b"%PDF-1.4\n"]

    def obj(number: int, content: bytes) -> None:
        chunks.append(f"{number} 0 obj\n".encode() + content + b"\nendobj\n")

    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    obj(4, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    obj(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    xref_pos = sum(len(chunk) for chunk in chunks)
    cursor = len(chunks[0])
    offsets = []
    for chunk in chunks[1:]:
        offsets.append(cursor)
        cursor += len(chunk)
    xref = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    xref.extend(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    trailer = f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return b"".join(chunks) + b"".join(xref) + trailer


class FakeLocator:
    def __init__(self, page, selector="", role=None, name=None):
        self.page = page
        self.selector = selector
        self.role = role
        self.name = name

    def count(self):
        if self.selector == "input[type='password']":
            return self.page.password_count
        if self.selector == "table":
            return 0
        if self.selector.startswith("embed"):
            return 0
        if self.role is not None:
            return 1 if (self.role, self.name) in self.page.roles else 0
        return 0

    def click(self):
        self.page.clicked.append((self.role, self.name))

    def inner_text(self):
        if self.selector == "body":
            return self.page.body
        return ""

    def nth(self, index):
        return self

    def all(self):
        return []

    def locator(self, selector):
        return FakeLocator(self.page, selector)

    def get_attribute(self, name):
        return None


class FakePage:
    def __init__(self, roles, *, body="CA33617", url="https://www.foragentsonly.com/home", popup=None):
        self.roles = set(roles)
        self.body = body
        self.url = url
        self.password_count = 0
        self.clicked = []
        self.popup = popup

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_role(self, role, name=None, exact=True):
        return FakeLocator(self, role=role, name=name)

    def expect_popup(self, timeout=None):
        page = self

        class _Popup:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.value = page.popup
                return False

        return _Popup()


class FakeBrowser:
    def __init__(self, capture, notices=None, root: Path | None = None):
        self.capture = capture
        self.notices = dict(notices or {})
        self.root = root
        self.downloaded: list[str] = []
        self.shell_blocked = None

    def load_report(self, report_date):
        if isinstance(self.capture, Exception):
            raise self.capture
        if self.root is not None:
            folder = self.root / report_date.isoformat()
            folder.mkdir(mode=0o700, exist_ok=True)
            os.chmod(folder, 0o700)
            (folder / noc_filename("860521214")).write_bytes(b"%PDF-1.4\nconflict\n")
        return self.capture

    def download_notice(self, policy):
        self.downloaded.append(policy.policy_number)
        item = self.notices[policy.policy_number]
        if isinstance(item, Exception):
            raise item
        return item


def _policy(number: str, insured: str = "ACME LLC") -> PendingCancelPolicy:
    return PendingCancelPolicy(number, insured, DAY)


def _report(policies, *, source="table", blank=False, observed=None):
    from robie_job_engine.progressive_bop import PendingCancelReport
    return PendingCancelReport(DAY, observed, tuple(policies), source, blank)


class ProgressiveBopTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        self._env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "qa"
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_scope_and_filename(self):
        self.assertEqual(BOP_SCOPE, "bop_pending_cancel_nonpayment")
        self.assertEqual(noc_filename("860521214"), "860521214 - NOC - Non Payment.pdf")
        self.assertEqual(DRIVE_QA_PARENT_ID, "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2")
        self.assertEqual(DRIVE_BOP_CHILD_NAME, "Progressive BOP")
        with self.assertRaises(IntakeHold):
            noc_filename("12")
        with self.assertRaises(IntakeHold):
            noc_filename("../860521214")

    def test_output_root_default_is_the_hermes_test_pack(self):
        args = build_parser().parse_args(["--report-date", "2026-09-26"])
        self.assertEqual(
            Path(args.output_root),
            DEFAULT_OUTPUT_ROOT,
        )
        self.assertTrue(str(DEFAULT_OUTPUT_ROOT).endswith(
            "carrier-pull-qa/progressive-bop"
        ))

    def test_module_does_not_import_ezlynx_or_drive_client(self):
        names = module_import_names()
        self.assertFalse(any("ezlynx" in name for name in names))
        self.assertFalse(any("googleapiclient" in name or name.startswith("google") for name in names))

    def test_table_policies_and_blank_header(self):
        report = parse_report_rows(
            [
                ("Report Date", "09/26/2026"),
                ("Policy Number", "Named Insured"),
                ("860521214", "3JR Contracting LLC"),
                (860521215, "ALTI TRANSPORT LLC"),
            ],
            report_date=DAY,
            source="table",
        )
        self.assertEqual([row.policy_number for row in report.policies], ["860521214", "860521215"])
        self.assertFalse(report.blank)
        blank = parse_report_rows(
            [("Policy Number", "Named Insured")],
            report_date=DAY,
            source="table",
        )
        self.assertTrue(blank.blank)
        self.assertEqual(blank.policies, ())

    def test_duplicate_policy_wrong_date_and_unparsed_text_hold(self):
        with self.assertRaises(IntakeHold):
            parse_report_rows(
                [("Policy Number",), ("860521214",), ("860521214",)],
                report_date=DAY,
                source="table",
            )
        with self.assertRaises(IntakeHold):
            parse_report_rows(
                [("Policy Number", "Cancel Date"), ("860521214", "09/25/2026")],
                report_date=DAY,
                source="table",
            )
        with self.assertRaises(IntakeHold):
            policies_from_report_text("Pending Cancel for Nonpayment\nPage 1", report_date=DAY)
        blank = policies_from_report_text("No records found", report_date=DAY)
        self.assertTrue(blank.blank)

    def test_excel_and_pdf_reports(self):
        excel = parse_excel_report(
            _excel([
                ["Policy Number", "Named Insured", "Cancel Date"],
                [860521214, "3JR Contracting LLC", date(2026, 9, 26)],
            ]),
            report_date=DAY,
        )
        self.assertEqual(excel.policies[0].filename, "860521214 - NOC - Non Payment.pdf")
        self.assertEqual(excel.source, "excel")
        blank = parse_excel_report(
            _excel([["Policy Number", "Named Insured"]]),
            report_date=DAY,
        )
        self.assertTrue(blank.blank)
        with self.assertRaises(IntakeHold):
            parse_excel_report(_excel([]), report_date=DAY)
        pdf = parse_pdf_report(
            _text_pdf("Policy Number 879512352 ALTI TRANSPORT LLC"),
            report_date=DAY,
        )
        self.assertEqual(pdf.policies[0].policy_number, "879512352")
        self.assertIn("ALTI", pdf.policies[0].insured_name)

    def test_two_policy_tables_hold_and_blank_page_text_is_empty(self):
        with self.assertRaises(IntakeHold):
            report_from_extracted(
                tables=[
                    (("Policy Number",), (("111111111",),)),
                    (("Policy Number",), (("222222222",),)),
                ],
                page_text="",
                excel_bytes=None,
                pdf_bytes=None,
                report_date=DAY,
            )
        report = report_from_extracted(
            tables=[],
            page_text="Pending Cancel for Nonpayment. No policies found.",
            excel_bytes=None,
            pdf_bytes=None,
            report_date=DAY,
        )
        self.assertTrue(report.blank)
        self.assertEqual(report.source, "page")

    def test_notice_selection_skips_other_docs_and_holds_missing(self):
        chosen = select_notice(
            [
                PolicyDocument("Declarations Page", DAY, 0),
                PolicyDocument("Notice of Non Payment", DAY, 1),
                PolicyDocument("Notice of Non-Payment", date(2026, 9, 25), 2),
            ],
            DAY,
        )
        self.assertEqual(chosen.row_index, 1)
        named = select_notice(
            [PolicyDocument("Notice of Non Payment 09/26/2026", None, 0)],
            DAY,
        )
        self.assertEqual(named.row_index, 0)
        with self.assertRaises(RowHold) as missing:
            select_notice([PolicyDocument("Invoice", DAY, 0)], DAY)
        self.assertIn("missing", str(missing.exception))
        with self.assertRaises(RowHold):
            select_notice(
                [
                    PolicyDocument("Notice of Non Payment", DAY, 0),
                    PolicyDocument("Notice of Nonpayment", DAY, 1),
                ],
                DAY,
            )
        with self.assertRaises(RowHold) as undated:
            select_notice([PolicyDocument("Notice of Non Payment", None, 0)], DAY)
        self.assertIn("date is missing", str(undated.exception))

    def test_gate_empty_pulled_and_held(self):
        from robie_job_engine.progressive_bop import PolicyOutcome
        self.assertEqual(assess_bop_gate(policy_count=0, outcomes=[], pdf_count=0, screenshot_ok=True), ("EMPTY", None))
        pulled = PolicyOutcome("860521214", "A", "id", "860521214 - NOC - Non Payment.pdf", "pulled")
        self.assertEqual(
            assess_bop_gate(policy_count=1, outcomes=[pulled], pdf_count=1, screenshot_ok=True)[0],
            "PULLED",
        )
        held = PolicyOutcome("860521214", "A", "id", "f.pdf", "held", reason="missing")
        status, reason = assess_bop_gate(policy_count=1, outcomes=[held], pdf_count=0, screenshot_ok=True)
        self.assertEqual(status, "HELD")
        self.assertIn("860521214", reason)
        mismatch = assess_bop_gate(policy_count=1, outcomes=[pulled], pdf_count=2, screenshot_ok=True)
        self.assertEqual(mismatch[0], "HELD")
        self.assertIn("does not match", mismatch[1])
        self.assertEqual(
            assess_bop_gate(policy_count=0, outcomes=[], pdf_count=0, screenshot_ok=False)[0],
            "HELD",
        )

    def test_loopback_and_single_fao_tab(self):
        self.assertEqual(require_loopback_cdp("http://127.0.0.1:9222"), "http://127.0.0.1:9222")
        with self.assertRaises(IntakeHold):
            require_loopback_cdp("http://10.0.0.8:9222")
        page = FakePage(set(), url="https://www.foragentsonly.com/home")
        self.assertIs(select_fao_page([page, FakePage(set(), url="https://example.test")]), page)
        with self.assertRaises(IntakeHold):
            select_fao_page([page, FakePage(set(), url="https://portal.foragentsonly.com/other")])
        with self.assertRaises(IntakeHold):
            select_fao_page([])

    def test_navigation_order_and_auth_hold(self):
        report = FakePage(
            {("link", "View Reports"), ("button", "Pending Cancel for Nonpayment")},
            url="https://www.foragentsonly.com/bop",
        )
        shell = FakePage(
            {("link", "Manage Policies"), ("link", "Businessowner/Contractor GL")},
            popup=report,
        )
        opened = navigate_to_pending_cancel(shell, "ca33617")
        self.assertIs(opened, report)
        self.assertEqual(
            shell.clicked,
            [("link", "Manage Policies"), ("link", "Businessowner/Contractor GL")],
        )
        self.assertEqual(
            report.clicked,
            [("link", "View Reports"), ("button", "Pending Cancel for Nonpayment")],
        )
        locked = FakePage({("link", "Manage Policies")}, body="CA33617", url="https://foragentsonlylogin.progressive.com/login")
        with self.assertRaises(IntakeHold):
            navigate_to_pending_cancel(locked, "CA33617")
        self.assertEqual(locked.clicked, [])
        ambiguous = FakePage(
            {("link", "Manage Policies")},
            body="CA33617 CA11111",
        )
        with self.assertRaises(IntakeHold):
            navigate_to_pending_cancel(ambiguous, "CA33617")
        self.assertEqual(ambiguous.clicked, [])

    def test_blank_page_text_does_not_require_an_export(self):
        page = FakePage(set(), body="Pending Cancel for Nonpayment. No records.")
        report = read_report_from_page(page, DAY)
        self.assertTrue(report.blank)
        self.assertEqual(report.policies, ())

    def _run(self, browser, *extra):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(
                ["--report-date", DAY.isoformat(), "--output-root", str(self.root), *extra],
                browser_factory=lambda args: browser,
            )
        payload = json.loads(stdout.getvalue() or "{}")
        return code, payload

    def test_blank_report_writes_empty_pack(self):
        browser = FakeBrowser(ReportCapture(PNG, _report((), blank=True, source="page"), None))
        code, payload = self._run(browser)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "EMPTY")
        self.assertEqual(payload["pdfs"], 0)
        self.assertEqual(payload["ezlynx"], "not_run")
        folder = self.root / DAY.isoformat()
        self.assertTrue((folder / "pending-cancel-nonpayment-2026-09-26.png").read_bytes().startswith(b"\x89PNG"))
        self.assertFalse(list(folder.glob("*.pdf")))
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "EMPTY")
        self.assertEqual(manifest["policies_on_report"], 0)
        self.assertEqual(manifest["drive"]["parent_id"], DRIVE_QA_PARENT_ID)
        self.assertEqual(manifest["drive"]["child_name"], "Progressive BOP")
        self.assertEqual(manifest["drive"]["status"], "not_run")
        self.assertIn("No policies", (folder / "README.md").read_text())
        self.assertEqual(browser.downloaded, [])

    def test_pulls_matching_notices_and_holds_the_missing_row(self):
        policies = (_policy("860521214", "3JR Contracting LLC"), _policy("879512352", "ALTI TRANSPORT LLC"))
        browser = FakeBrowser(
            ReportCapture(PNG, _report(policies), None),
            {
                "860521214": PDF_A,
                "879512352": RowHold("Notice of Non Payment matching 2026-09-26 is missing"),
            },
        )
        code, payload = self._run(browser)
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "HELD")
        self.assertEqual(browser.downloaded, ["860521214", "879512352"])
        folder = self.root / DAY.isoformat()
        saved = list(folder.glob("*.pdf"))
        self.assertEqual([path.name for path in saved], ["860521214 - NOC - Non Payment.pdf"])
        self.assertEqual(saved[0].read_bytes(), PDF_A)
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "HELD")
        self.assertEqual(manifest["successful_noc"], 1)
        self.assertEqual(manifest["pdfs"], 1)
        self.assertEqual(manifest["policies_on_report"], 2)
        held = [row for row in manifest["policies"] if row["disposition"] == "held"]
        self.assertEqual(held[0]["policy_number"], "879512352")
        self.assertIn("missing", held[0]["reason"])
        readme = (folder / "README.md").read_text()
        self.assertIn("860521214 - NOC - Non Payment.pdf", readme)
        self.assertIn("879512352", readme)

    def test_replay_skips_download_when_the_ledger_matches(self):
        policy = _policy("860521214")
        browser = FakeBrowser(
            ReportCapture(PNG, _report((policy,)), None),
            {"860521214": PDF_A},
        )
        first, payload = self._run(browser)
        self.assertEqual(first, 0)
        self.assertEqual(payload["status"], "PULLED")
        second, again = self._run(browser)
        self.assertEqual(second, 0)
        self.assertEqual(again["status"], "PULLED")
        self.assertEqual(browser.downloaded, ["860521214"])
        manifest = json.loads((self.root / DAY.isoformat() / "manifest.json").read_text())
        self.assertEqual(manifest["policies"][0]["disposition"], "already_present")

    def test_conflicting_pdf_holds_without_a_second_download(self):
        policy = _policy("860521214")
        browser = FakeBrowser(
            ReportCapture(PNG, _report((policy, _policy("879512352"))), None),
            {"860521214": PDF_A, "879512352": PDF_B},
            root=self.root,
        )
        code, payload = self._run(browser)
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "HELD")
        self.assertEqual(browser.downloaded, [])
        self.assertIn("conflicts", payload["reason"])

    def test_screenshot_failure_and_bad_png_do_not_claim_empty(self):
        failed = FakeBrowser(IntakeHold("Businessowner/Contractor GL did not open a single new window"))
        code, payload = self._run(failed)
        self.assertEqual(code, 2)
        self.assertNotIn("output", payload)
        self.assertFalse((self.root / DAY.isoformat() / "manifest.json").exists())
        bad = FakeBrowser(ReportCapture(b"not-a-png", None, None))
        code, payload = self._run(bad)
        self.assertEqual(code, 2)
        self.assertIn("screenshot", payload["reason"])
        self.assertFalse((self.root / DAY.isoformat() / "manifest.json").exists())

    def test_unparsed_report_still_saves_the_screenshot_as_held(self):
        browser = FakeBrowser(ReportCapture(PNG, None, "Pending Cancel report is missing or ambiguous"))
        code, payload = self._run(browser)
        self.assertEqual(code, 2)
        folder = self.root / DAY.isoformat()
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "HELD")
        self.assertEqual(manifest["pdfs"], 0)
        self.assertTrue((folder / "pending-cancel-nonpayment-2026-09-26.png").exists())
        self.assertIn("ambiguous", payload["reason"])

    def test_upload_drive_fails_closed_without_calling_google(self):
        browser = FakeBrowser(ReportCapture(PNG, _report((), blank=True, source="page"), None))
        code, payload = self._run(browser, "--upload-drive")
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "HELD")
        self.assertIn("not available", payload["reason"])
        self.assertIn(DRIVE_QA_PARENT_ID, payload["reason"])
        manifest = json.loads((self.root / DAY.isoformat() / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "EMPTY")
        self.assertEqual(manifest["drive"]["status"], "HELD")
        self.assertIn("Progressive BOP", manifest["drive"]["reason"])
        readme = (self.root / DAY.isoformat() / "README.md").read_text()
        self.assertIn("Drive: HELD", readme)
        self.assertNotIn("googleapiclient", module_import_names())

    def test_production_and_unset_env_refuse_before_output(self):
        browser = FakeBrowser(ReportCapture(PNG, _report((), blank=True), None))
        for env in ("PRODUCTION", ""):
            target = Path(self.tmp.name) / f"env-{env or 'unset'}"
            stdout = io.StringIO()
            with patch.dict(os.environ, {"ROBIE_ENV": env}), patch("sys.stdout", stdout):
                code = main(
                    ["--report-date", DAY.isoformat(), "--output-root", str(target)],
                    browser_factory=lambda args: browser,
                )
            self.assertEqual(code, 2, env)
            self.assertFalse(target.exists(), env)
            self.assertIn("HELD", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
