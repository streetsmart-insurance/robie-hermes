"""Daily status-sheet tab, one row per PDF, EZLynx policy match (2026-10-10)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from robie_job_engine import carrier_daily_report as report
from robie_job_engine import carrier_daily_run as daily
from robie_job_engine import carrier_qa_drive as qa

TEMPLATE_ROWS = [
    ["", "Insured Name", "Policy Number", "Department", "Document Type", "Memo Date", "Comment"],
    ["", "Insured Name", "Policy Number", "Department", "Document Type", "Memo Date", "Comment"],
    ["GEICO"], ["ASI"], ["Progressive"], ["Progressive BOP/CGL"], ["Farmers of Salem - (FOS Portal)"],
    ["Guard"], ["Kingstone"], ["Travelers"], ["Utica First  \n(UFIRST Now)"], ["NatGen"], ["NBIC"],
    ["Safeco"], ["Universal Property"],
]


class FakeSheets:
    """Just enough of the Sheets v4 client: tabs, duplicate, insert, values."""

    def __init__(self):
        self.tabs = {"TEMPLATE": {"sheetId": 0, "index": 0, "rows": [list(r) for r in TEMPLATE_ROWS]}}
        self.calls = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def _x(self, value):
        return SimpleNamespace(execute=lambda: value)

    def get(self, spreadsheetId, range=None):
        if range is None:
            return self._x({"sheets": [{"properties": {"title": t, "sheetId": v["sheetId"], "index": v["index"]}}
                                       for t, v in self.tabs.items()]})
        tab = range.split("'")[1]
        return self._x({"values": [list(r) for r in self.tabs[tab]["rows"]]})

    def _by_id(self, sheet_id):
        return next(v for v in self.tabs.values() if v["sheetId"] == sheet_id)

    def batchUpdate(self, spreadsheetId, body):
        req = body["requests"][0]
        self.calls.append(next(iter(req)))
        if "duplicateSheet" in req:
            d = req["duplicateSheet"]
            new_id = 100 + len(self.tabs)
            self.tabs[d["newSheetName"]] = {"sheetId": new_id, "index": d["insertSheetIndex"],
                                            "rows": [list(r) for r in self._by_id(d["sourceSheetId"])["rows"]]}
            return self._x({"replies": [{"duplicateSheet": {"properties": {"sheetId": new_id}}}]})
        r = req["insertDimension"]["range"]
        self._by_id(r["sheetId"])["rows"].insert(r["startIndex"], [])
        return self._x({})

    def update(self, spreadsheetId, range, valueInputOption, body):
        tab, cells = range.split("'")[1], range.split("!")[1]
        self.calls.append((valueInputOption, cells))
        col, row = cells.split(":")[0][0], int("".join(c for c in cells.split(":")[0] if c.isdigit()))
        rows = self.tabs[tab]["rows"]
        while len(rows) < row:
            rows.append([])
        cur = rows[row - 1] + [""] * (7 - len(rows[row - 1]))
        start = "ABCDEFG".index(col)
        for i, v in enumerate(body["values"][0]):
            cur[start + i] = v
        rows[row - 1] = cur
        return self._x({})


def write_pack(root: Path, carrier: str, day: str, entries: list[dict], *, sub: str = "2026-10-06") -> Path:
    pack = root / "packs" / carrier / day
    folder = pack / sub
    folder.mkdir(parents=True, exist_ok=True)
    for e in entries:
        (folder / e["filename"]).write_bytes(b"%PDF-1.4 " + e["filename"].encode())
    (folder / "manifest.json").write_text(json.dumps({"processed_date": sub, "notices": entries}))
    return pack


def write_ledger(root: Path, carrier: str, keys: dict[str, str]) -> None:
    items = {k: {"drive_file_id": v} for k, v in keys.items()}
    (root / "packs" / carrier / qa.DRIVE_LEDGER_NAME).write_text(json.dumps({"items": items}))


def search_for(table: dict[str, list[str]]):
    def search(policy):
        if policy == "BOOM":
            raise RuntimeError("api down")
        return {"data": [{"PolicyNumber": policy, "ApplicantId": a} for a in table.get(policy, [])]}
    return search


class PackItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_reads_manifest_and_links_drive(self):
        pack = write_pack(self.root, "geico", "2026-10-12", [
            {"filename": "a.pdf", "policy_number": "P1", "insured_name": "Ann Lee", "line": "personal",
             "notice_type": "Notice of Cancellation", "issued_date": "10/06/2026"},
            {"filename": "b.pdf", "policy_number": "P2", "insured": "Bo Diaz", "disposition": "held"},
            {"filename": "c.pdf", "policy_number": ""},
        ])
        items = report.pack_items("geico", pack, {"items": {"2026-10-06/a.pdf": {"drive_file_id": "F1"}}})
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["insured_name"], "Ann Lee")
        self.assertEqual(item["department"], "Personal Lines")
        self.assertEqual(item["document_type"], "Notice of Cancellation")
        self.assertEqual(item["memo_date"], "10/06/2026")
        self.assertEqual(item["drive_link"], "https://drive.google.com/file/d/F1/view")

    def test_missing_fields_stay_blank(self):
        pack = write_pack(self.root, "natgen", "2026-10-12", [{"filename": "a.pdf", "policy_number": "P1"}])
        item = report.pack_items("natgen", pack, {})[0]
        self.assertEqual((item["insured_name"], item["department"], item["document_type"], item["drive_link"]),
                         ("", "", "", ""))
        self.assertEqual(item["memo_date"], "2026-10-06")


class MatchTests(unittest.TestCase):
    def test_one_none_many_and_error(self):
        search = search_for({"P1": ["111"], "P2": ["111", "222"]})
        self.assertEqual(report.match_policy(search, "P1"), {"status": "MATCHED", "applicants": ["111"]})
        self.assertEqual(report.match_policy(search, "P0")["status"], "NONE")
        self.assertEqual(report.match_policy(search, "P2")["status"], "MULTIPLE")
        self.assertEqual(report.match_policy(search, "BOOM")["status"], "ERROR")
        self.assertIn("needs a person", report.match_text({"status": "MULTIPLE", "applicants": ["1", "2"]}))
        self.assertIn("not filed yet", report.match_text({"status": "MATCHED", "applicants": ["111"]}))

    def test_comment_is_a_quoted_hyperlink(self):
        cell = report.comment_cell("https://drive.google.com/file/d/F/view", 'say "hi"')
        self.assertEqual(cell, '=HYPERLINK("https://drive.google.com/file/d/F/view","PDF in Drive. say ""hi""")')
        self.assertEqual(report.comment_cell("", "x"), "Not in Drive. x")


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sheets = FakeSheets()
        env = mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, **kw):
        return report.run_report(day=date(2026, 10, 12), root=self.root, carriers=["geico", "uticafirst"],
                                 sheet_factory=lambda: self.sheets, **kw)

    def test_copies_template_once_and_writes_rows_under_each_carrier(self):
        write_pack(self.root, "geico", "2026-10-12", [
            {"filename": "a.pdf", "policy_number": "P1", "insured_name": "Ann Lee"},
            {"filename": "b.pdf", "policy_number": "P2", "insured_name": "Bo Diaz"},
        ])
        write_pack(self.root, "uticafirst", "2026-10-12", [{"filename": "u.pdf", "policy_number": "U9"}])
        write_ledger(self.root, "geico", {"2026-10-06/a.pdf": "FA", "2026-10-06/b.pdf": "FB"})
        out = self._run(search_factory=lambda: search_for({"P1": ["111"]}))
        self.assertEqual(out["status"], "OK")
        self.assertEqual((out["tab"], out["tab_created"], out["written"]), ("10/12.", True, 3))
        self.assertEqual(out["matches"], {"MATCHED": 1, "NONE": 2})
        rows = self.sheets.tabs["10/12."]["rows"]
        self.assertEqual(rows[2][:3], ["GEICO", "Ann Lee", "P1"])
        self.assertEqual(rows[3][:3], ["", "Bo Diaz", "P2"])
        self.assertTrue(rows[2][6].startswith('=HYPERLINK("https://drive.google.com/file/d/FA/view"'))
        self.assertIn("EZLynx client 111", rows[2][6])
        utica = next(r for r in rows if r and r[0].startswith("Utica First"))
        self.assertEqual(utica[2], "U9")
        self.assertEqual(self.sheets.tabs["TEMPLATE"]["rows"], TEMPLATE_ROWS)
        self.assertEqual(self.sheets.tabs["10/12."]["index"], 1)
        # Only the Comment cell is written as a formula.
        self.assertTrue(all(cells.startswith("G") for opt, cells in
                            [c for c in self.sheets.calls if isinstance(c, tuple)] if opt == "USER_ENTERED"))

    def test_rerun_updates_rows_and_reuses_the_tab(self):
        write_pack(self.root, "geico", "2026-10-12", [{"filename": "a.pdf", "policy_number": "P1"}])
        self._run(match_clients=False)
        before = len(self.sheets.tabs["10/12."]["rows"])
        out = self._run(match_clients=False)
        self.assertFalse(out["tab_created"])
        self.assertEqual(len(self.sheets.tabs["10/12."]["rows"]), before)
        self.assertEqual(self.sheets.calls.count("duplicateSheet"), 1)

    def test_insured_name_that_looks_like_a_formula_is_written_raw(self):
        write_pack(self.root, "geico", "2026-10-12", [{"filename": "a.pdf", "policy_number": "P1",
                                                       "insured_name": "=IMPORTXML(1)"}])
        self._run(match_clients=False)
        raw_ranges = [cells for opt, cells in [c for c in self.sheets.calls if isinstance(c, tuple)] if opt == "RAW"]
        self.assertTrue(raw_ranges and all(r.startswith("A") for r in raw_ranges))

    def test_missing_template_or_sheet_holds_without_raising(self):
        del self.sheets.tabs["TEMPLATE"]
        out = self._run(match_clients=False)
        self.assertEqual(out["status"], "HELD")
        self.assertIn("TEMPLATE", out["reason"])
        out = report.run_report(day=date(2026, 10, 12), root=self.root, carriers=["geico"],
                                sheet_factory=lambda: (_ for _ in ()).throw(RuntimeError("no token")))
        self.assertEqual(out["status"], "HELD")

    def test_lookup_unavailable_marks_rows_for_a_person(self):
        write_pack(self.root, "geico", "2026-10-12", [{"filename": "a.pdf", "policy_number": "P1"}])
        out = self._run(search_factory=lambda: (_ for _ in ()).throw(RuntimeError("no api")))
        self.assertEqual(out["matches"], {"ERROR": 1})
        self.assertIn("lookup failed", self.sheets.tabs["10/12."]["rows"][2][6])

    def test_daily_run_puts_the_tab_link_in_the_summary(self):
        write_pack(self.root, "geico", "2026-10-12", [{"filename": "a.pdf", "policy_number": "P1"}])
        summary = daily.run_daily(
            day=date(2026, 10, 12), root=self.root, carriers=("geico",),
            run_one=lambda name, **kw: {"display": "GEICO", "status": "OK", "downloaded": 1, "held": []},
            close_tabs=lambda url: [], open_tab=lambda name, url: None,
            status_sheet=True, match_clients=True, sheet_factory=lambda: self.sheets,
            search_factory=lambda: search_for({"P1": ["111"]}),
        )
        self.assertIn("Status sheet tab 10/12.: 1 row(s) written, 1 matched to an EZLynx client, 0 need a person.",
                      summary["text"])
        self.assertIn("edit#gid=", summary["text"])


if __name__ == "__main__":
    unittest.main()
