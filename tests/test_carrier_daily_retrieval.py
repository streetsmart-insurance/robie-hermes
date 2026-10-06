"""Fixture tests for the daily carrier retrieval workflow.

No live portals, no real Drive/Sheets: the dry-run runner, Drive client,
and Sheets client are all injected fakes.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine import carrier_daily_retrieval as cdr
from robie_job_engine.carrier_daily_retrieval import (
    QA_ROOT_FOLDER_ID,
    SHEET_COLUMNS,
    STATUS_SPREADSHEET_ID,
    build_parser,
    build_sheet_values,
    collect_documents,
    run_daily_retrieval,
    upload_carrier_pack,
)
from robie_job_engine.carrier_dry_run import CARRIER_ORDER, SPECS
from robie_job_engine.intake_core import IntakeHold

AS_OF = date(2026, 10, 5)
TEST_ENV = {"ROBIE_ENV": "TEST"}

PDF_BYTES = (
    b"%PDF-1.7\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF"
)


class FakeDrive:
    def __init__(self):
        self.folders: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str, str]] = []
        self._folder_ids: dict[tuple[str, str], str] = {}

    def ensure_folder(self, parent_id, name):
        self.folders.append((parent_id, name))
        key = (parent_id, name)
        if key not in self._folder_ids:
            self._folder_ids[key] = f"folder-{len(self._folder_ids)}"
        return self._folder_ids[key]

    def upload_pdf(self, local_path, folder_id, name):
        assert Path(local_path).is_file()
        fid = f"file-{len(self.uploads)}"
        link = f"https://drive.google.com/file/d/{fid}/view"
        self.uploads.append((str(local_path), folder_id, name))
        return {"id": fid, "webViewLink": link}


class FakeSheets:
    def __init__(self):
        self.tabs: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, list]] = []

    def create_tab(self, spreadsheet_id, title):
        self.tabs.append((spreadsheet_id, title))

    def write_values(self, spreadsheet_id, tab_title, values):
        self.writes.append((spreadsheet_id, tab_title, values))


def _write_pack(tmp_path: Path, name: str, files: dict[str, str | None]) -> str:
    """Create a dated pack folder with PDFs and an optional ledger JSON.

    files: filename -> issued_date (or None for no ledger entry).
    """
    pack = tmp_path / name / AS_OF.isoformat()
    pack.mkdir(parents=True)
    items = {}
    for i, (fname, issued) in enumerate(files.items()):
        (pack / fname).write_bytes(PDF_BYTES)
        if issued is not None:
            items[f"doc-{i}"] = {"filename": fname, "sha256": "abc", "bytes": 10, "issued_date": issued}
    (pack / f"{name}-ledger.json").write_text(json.dumps({"items": items}))
    return str(pack)


def _carrier_result(name: str, pack: str, status="OK", downloaded=0, held=None, error=None):
    return {
        "display": SPECS[name].display,
        "status": status,
        "downloaded": downloaded,
        "skipped": 0,
        "held": held or [],
        "error": error,
        "pack": pack,
    }


def _make_runner(results: dict[str, dict]):
    def runner(*, as_of, output_root=None):
        return {
            "as_of": as_of.isoformat(),
            "mode": "dry-run",
            "carriers": results,
            "totals": {"ok": 0, "held": 0, "failed": 0},
        }

    return runner


class TabNamingTests(unittest.TestCase):
    def test_tab_title_is_iso_date(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            drive, sheets = FakeDrive(), FakeSheets()
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                result = run_daily_retrieval(
                    AS_OF,
                    dry_run_runner=_make_runner({}),
                    drive=drive,
                    sheets=sheets,
                    output_root=tmp,
                )
        self.assertEqual(result["sheet_tab"], "2026-10-05")
        self.assertEqual(sheets.tabs, [(STATUS_SPREADSHEET_ID, "2026-10-05")])
        # values written to the same tab
        self.assertEqual(sheets.writes[0][0], STATUS_SPREADSHEET_ID)
        self.assertEqual(sheets.writes[0][1], "2026-10-05")


class DriveFolderStructureTests(unittest.TestCase):
    def test_folder_hierarchy_and_uploads(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = _write_pack(Path(tmp), "progressive", {"970498127 Cancel_Notice Progressive.pdf": "2026-09-28"})
            results = {"progressive": _carrier_result("progressive", pack, downloaded=1)}
            drive, sheets = FakeDrive(), FakeSheets()
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                result = run_daily_retrieval(
                    AS_OF, dry_run_runner=_make_runner(results), drive=drive, sheets=sheets
                )
        # carrier folder under the QA root, then the dated folder under it
        carrier_folder = drive._folder_ids[(QA_ROOT_FOLDER_ID, "Progressive (FAO)")]
        date_folder = drive._folder_ids[(carrier_folder, "2026-10-05")]
        self.assertEqual(len(drive.uploads), 1)
        local, folder_id, name = drive.uploads[0]
        self.assertEqual(folder_id, date_folder)
        self.assertEqual(name, "970498127 Cancel_Notice Progressive.pdf")
        self.assertEqual(result["uploaded"]["progressive"][0]["drive_link"],
                         "https://drive.google.com/file/d/file-0/view")


class DocumentCollectionTests(unittest.TestCase):
    def test_policy_and_type_parsed_from_filename(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(_write_pack(Path(tmp), "guard", {"PRAU716089 Cancellation_-_09-23-2026 Guard.pdf": "2026-09-23"}))
            docs = collect_documents(pack, "Guard", as_of=AS_OF)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].policy_number, "PRAU716089")
        self.assertIn("Cancellation", docs[0].document_type)
        self.assertEqual(docs[0].document_date, "2026-09-23")
        # insured name is not invented
        self.assertEqual(docs[0].insured_name, "")

    def test_missing_pack_is_empty_not_error(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            docs = collect_documents(Path(tmp) / "nope", "Guard", as_of=AS_OF)
        self.assertEqual(docs, [])


class SheetContentTests(unittest.TestCase):
    def test_header_and_hyperlink_rows(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = _write_pack(Path(tmp), "progressive", {"970498127 Cancel_Notice Progressive.pdf": "2026-09-28"})
            results = {"progressive": _carrier_result("progressive", pack, downloaded=1)}
            drive, sheets = FakeDrive(), FakeSheets()
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                run_daily_retrieval(
                    AS_OF, dry_run_runner=_make_runner(results), drive=drive, sheets=sheets
                )
        values = sheets.writes[0][2]
        self.assertIn(SHEET_COLUMNS, values)
        header_idx = values.index(SHEET_COLUMNS)
        row = values[header_idx + 1]
        self.assertEqual(row[0], "Progressive (FAO)")
        self.assertEqual(row[1], "970498127")
        self.assertEqual(row[4], "2026-09-28")
        self.assertTrue(row[5].startswith('=HYPERLINK("https://drive.google.com/file/d/file-0/view"'))
        self.assertEqual(row[6], "Downloaded")
        # summary row mentions the document count
        self.assertIn("1 documents", values[0][0])


class FailureIsolationTests(unittest.TestCase):
    def test_failed_carrier_does_not_stop_others(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = _write_pack(Path(tmp), "guard", {"PRAU716089 Cancel Guard.pdf": None})
            results = {
                "progressive": _carrier_result(
                    "progressive", "/nonexistent", status="FAILED", error="RuntimeError: boom"
                ),
                "guard": _carrier_result("guard", pack, downloaded=1),
                "geico": _carrier_result(
                    "geico", "/nonexistent", status="HELD",
                    held=[{"reason": "portal layout changed"}],
                ),
            }
            drive, sheets = FakeDrive(), FakeSheets()
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                result = run_daily_retrieval(
                    AS_OF, dry_run_runner=_make_runner(results), drive=drive, sheets=sheets
                )
        # guard still uploaded
        self.assertEqual(len(drive.uploads), 1)
        # sheet still written with held/failed rows
        values = sheets.writes[0][2]
        flat = " ".join(cell for row in values for cell in row)
        self.assertIn("HELD: portal layout changed", flat)
        self.assertIn("FAILED: RuntimeError: boom", flat)
        # failure recorded in result errors
        self.assertTrue(any("Progressive (FAO)" in e for e in result["errors"]))

    def test_drive_failure_does_not_stop_sheet(self):
        import tempfile

        class BadDrive(FakeDrive):
            def upload_pdf(self, local_path, folder_id, name):
                raise RuntimeError("quota exceeded")

        with tempfile.TemporaryDirectory() as tmp:
            pack = _write_pack(Path(tmp), "guard", {"PRAU716089 Cancel Guard.pdf": None})
            results = {"guard": _carrier_result("guard", pack, downloaded=1)}
            drive, sheets = BadDrive(), FakeSheets()
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                result = run_daily_retrieval(
                    AS_OF, dry_run_runner=_make_runner(results), drive=drive, sheets=sheets
                )
        self.assertTrue(any("Drive upload failed" in e for e in result["errors"]))
        # sheet still written (documents listed without links)
        self.assertEqual(len(sheets.writes), 1)
        values = sheets.writes[0][2]
        header_idx = values.index(SHEET_COLUMNS)
        self.assertEqual(values[header_idx + 1][5], "")


class SkipFlagTests(unittest.TestCase):
    def test_skip_upload(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = _write_pack(Path(tmp), "guard", {"PRAU716089 Cancel Guard.pdf": None})
            results = {"guard": _carrier_result("guard", pack, downloaded=1)}
            drive, sheets = FakeDrive(), FakeSheets()
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                result = run_daily_retrieval(
                    AS_OF, skip_upload=True, dry_run_runner=_make_runner(results),
                    drive=drive, sheets=sheets,
                )
        self.assertEqual(drive.uploads, [])
        self.assertEqual(len(sheets.writes), 1)
        self.assertEqual(result["documents"], 1)

    def test_skip_sheet(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = _write_pack(Path(tmp), "guard", {"PRAU716089 Cancel Guard.pdf": None})
            results = {"guard": _carrier_result("guard", pack, downloaded=1)}
            drive, sheets = FakeDrive(), FakeSheets()
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                result = run_daily_retrieval(
                    AS_OF, skip_sheet=True, dry_run_runner=_make_runner(results),
                    drive=drive, sheets=sheets,
                )
        self.assertEqual(len(drive.uploads), 1)
        self.assertEqual(sheets.writes, [])
        self.assertIsNone(result["sheet_tab"])


class GateTests(unittest.TestCase):
    def test_non_test_env_refused(self):
        with unittest.mock.patch.dict(os.environ, {"ROBIE_ENV": "PROD"}, clear=False):
            with self.assertRaises(IntakeHold):
                run_daily_retrieval(AS_OF, dry_run_runner=_make_runner({}))

    def test_default_clients_fail_closed_without_google(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pack = _write_pack(Path(tmp), "guard", {"PRAU716089 Cancel Guard.pdf": None})
            results = {"guard": _carrier_result("guard", pack, downloaded=1)}
            with unittest.mock.patch.dict(os.environ, TEST_ENV):
                with unittest.mock.patch(
                    "robie_job_engine.carrier_daily_retrieval.GwsDriveClient",
                    side_effect=IntakeHold("Google Drive is unavailable"),
                ), unittest.mock.patch(
                    "robie_job_engine.carrier_daily_retrieval.GwsSheetsClient",
                    side_effect=IntakeHold("Google Sheets is unavailable"),
                ):
                    result = run_daily_retrieval(AS_OF, dry_run_runner=_make_runner(results))
        self.assertTrue(any("Drive upload failed" in e for e in result["errors"]))
        self.assertTrue(any("Sheet update failed" in e for e in result["errors"]))


class CliTests(unittest.TestCase):
    def test_bad_as_of_exits_2(self):
        self.assertEqual(cdr.main(["--as-of", "not-a-date"]), 2)

    def test_parser_flags(self):
        args = build_parser().parse_args(["--skip-upload", "--skip-sheet", "--as-of", "2026-10-05"])
        self.assertTrue(args.skip_upload)
        self.assertTrue(args.skip_sheet)
        self.assertEqual(args.as_of, "2026-10-05")


if __name__ == "__main__":
    unittest.main()
