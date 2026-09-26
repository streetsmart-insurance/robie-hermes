"""Tests that every untrusted spreadsheet write uses valueInputOption="RAW".

USER_ENTERED parses cell values as if typed by a human, so any value
beginning with =, +, - or @ is interpreted as a formula (classic formula
injection). None of the Control Center / Confirmations writes are intended
formulas, so every write path must use RAW.

The mode assertions below FAIL on the pre-fix tree (USER_ENTERED) and PASS
after the fix. The end-to-end test additionally simulates real Sheets
write semantics in the fake: under USER_ENTERED, formula-looking strings
are transformed on write; under RAW they land literally.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from robie_job_engine import confirmation_board, confirmations, sheets_sync
from robie_job_engine.store import JobStore


SHEET_ID = "test-sheet"


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


_FORMULA_LEADERS = ("=", "+", "-", "@")


def _sheets_semantics(value, value_input_option):
    """Approximate how Google Sheets stores a written cell value.

    RAW: stored exactly as sent. USER_ENTERED: parsed as if typed by a
    user, so a leading =, +, - or @ marks the value as a formula and the
    stored cell is no longer the literal text.
    """
    text = value if isinstance(value, str) else str(value)
    if value_input_option == "RAW":
        return text
    if text.startswith(_FORMULA_LEADERS):
        return "<EVALUATED-FORMULA>"
    return text


class RecordingValuesApi:
    """Fake Sheets values API that captures valueInputOption per write call
    and stores cells with simulated Sheets write semantics."""

    def __init__(self):
        self.cells = {}
        self.titles = set()
        self.update_modes = []      # valueInputOption of each values.update call
        self.batch_modes = []       # valueInputOption of each values.batchUpdate call

    def _store(self, spreadsheet_id, tab, rows, mode):
        self.cells[(spreadsheet_id, tab)] = [
            [_sheets_semantics(cell, mode) for cell in row] for row in rows
        ]

    # -- values API --
    def get(self, spreadsheetId, range):  # noqa: N803
        tab = range.split("!", 1)[0]
        return _FakeExecutable(
            {"values": [list(r) for r in self.cells.get((spreadsheetId, tab), [])]}
        )

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        tab = range.split("!", 1)[0]
        self.titles.add(tab)
        self.update_modes.append(valueInputOption)
        self._store(spreadsheetId, tab, body["values"], valueInputOption)
        return _FakeExecutable({"updatedRange": range})

    def batchUpdate(self, spreadsheetId, body):  # noqa: N803
        mode = (body or {}).get("valueInputOption")
        self.batch_modes.append(mode)
        for write in (body or {}).get("data", []):
            tab = write["range"].split("!", 1)[0]
            self.titles.add(tab)
            self._store(spreadsheetId, tab, write["values"], mode)
        return _FakeExecutable({})

    def clear(self, spreadsheetId, range):  # noqa: N803
        tab = range.split("!", 1)[0]
        self.cells[(spreadsheetId, tab)] = []
        return _FakeExecutable({})


class FakeSheetsApi:
    def __init__(self, values):
        self.values = values

    def get(self, spreadsheetId, fields):  # noqa: N803
        return _FakeExecutable(
            {"sheets": [{"properties": {"title": t}} for t in sorted(self.values.titles)]}
        )

    def batchUpdate(self, spreadsheetId, body):  # noqa: N803
        for req in body.get("requests", []):
            title = req.get("addSheet", {}).get("properties", {}).get("title")
            if title:
                self.values.titles.add(title)
        return _FakeExecutable({})


def _job_dashboard(job):
    return {"jobs": [job]}


class RawInputModeSheetsSyncTests(unittest.TestCase):
    def test_upsert_job_rows_writes_raw(self):
        values = MagicMock()
        values.get.return_value.execute.return_value = {
            "values": [["old"] * 17 + ["target-job"]]
        }
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value = values
        dashboard = _job_dashboard({
            "id": "target-job",
            "action_type": "deployment.smoke",
            "payload": {"task": "Live Test acceptance"},
            "status": "COMPLETE",
            "created_at": "2026-08-24T12:00:00+00:00",
            "updated_at": "2026-08-24T12:01:00+00:00",
            "completed_at": "2026-08-24T12:01:00+00:00",
        })
        operations = MagicMock()
        operations.dashboard_rows.return_value = dashboard
        with patch.object(sheets_sync, "_service", return_value=service), \
             patch.object(sheets_sync, "OperationsStore", return_value=operations):
            sheets_sync.upsert_job_rows("jobs.db", "sheet", ["target-job"])

        request = values.batchUpdate.call_args.kwargs
        self.assertEqual("RAW", request["body"]["valueInputOption"])

    def test_publish_job_to_control_center_writes_raw(self):
        job = {
            "id": "verified-job",
            "action_type": "ezlynx.submission_audit",
            "payload": {"task": "Read-only Submission Center audit"},
            "status": "COMPLETE",
            "authoritative_evidence_count": 1,
            "recording_links": [{
                "segment": 1,
                "url": "https://drive.google.com/file/d/recording/view",
            }],
        }
        evidence = {
            "job_id": "verified-job",
            "verified": 1,
            "method": "EZLYNX_PLAYWRIGHT_FRESH_READBACK",
            "source": "EZLynx Submission Center",
            "authoritative": 1,
            "expected_json": "{}",
            "observed_json": "{}",
            "locator": "https://app.ezlynx.com/web/",
            "evidence_sha256": "evidence-digest",
            "captured_at": "2026-08-25T12:00:00+00:00",
            "created_at": "2026-08-25T12:00:00+00:00",
        }
        expected_job_row = sheets_sync._friendly_jobs([job], limit=1)[0]
        evidence_row = [evidence[key] for key in (
            "job_id", "verified", "method", "source", "authoritative",
            "expected_json", "observed_json", "locator", "evidence_sha256",
            "captured_at", "created_at",
        )]
        values = MagicMock()
        responses = (
            {"values": []},
            {"values": [expected_job_row]},
            {"values": [evidence_row]},
        )
        values.get.side_effect = [
            MagicMock(execute=MagicMock(return_value=response)) for response in responses
        ]
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value = values
        operations = MagicMock()
        operations.dashboard_rows.return_value = {"jobs": [job], "evidence": [evidence]}
        with patch.object(sheets_sync, "_service", return_value=service), \
             patch.object(sheets_sync, "OperationsStore", return_value=operations), \
             patch.object(sheets_sync, "upsert_job_rows",
                          return_value={"jobs": 1, "updated": 0, "appended": 1}):
            sheets_sync.publish_job_to_control_center(
                "jobs.db", "sheet", "verified-job"
            )

        request = values.batchUpdate.call_args.kwargs
        self.assertEqual("RAW", request["body"]["valueInputOption"])

    def test_sync_writes_raw(self):
        values = MagicMock()
        values.get.return_value.execute.return_value = {"values": []}
        spreadsheets = MagicMock()
        spreadsheets.values.return_value = values
        # Note: no "Confirmations" tab, so the board sync is not invoked here.
        spreadsheets.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"title": title}}
                for title in (
                    "Dashboard", "Assignments", "Jobs", "Evidence",
                    "Schedules", "Artifacts",
                )
            ]
        }
        service = MagicMock()
        service.spreadsheets.return_value = spreadsheets
        operations = MagicMock()
        operations.claim_due_schedules.return_value = []
        operations.dashboard_rows.return_value = {
            "jobs": [{
                "id": "verified-job",
                "action_type": "deployment.smoke",
                "payload": {"task": "Production smoke"},
                "status": "COMPLETE",
            }],
            "evidence": [],
            "schedules": [],
            "artifacts": [],
            "releases": [],
            "reports": [],
            "recordings": [],
        }
        with patch.object(sheets_sync, "_service", return_value=service), \
             patch.object(sheets_sync, "OperationsStore", return_value=operations), \
             patch.object(sheets_sync, "JobStore", return_value=MagicMock()):
            sheets_sync.sync("jobs.db", "sheet")

        request = values.batchUpdate.call_args.kwargs
        self.assertEqual("RAW", request["body"]["valueInputOption"])


class RawInputModeConfirmationBoardTests(unittest.TestCase):
    def _sync_once(self, db_path, values):
        sheets = FakeSheetsApi(values)
        return confirmation_board.sync_confirmations(
            db_path, SHEET_ID, values_api=values, sheets_api=sheets
        )

    def test_sync_confirmations_writes_raw(self):
        import tempfile, os
        db_path = os.path.join(tempfile.mkdtemp(), "jobs.db")
        store = JobStore(db_path)
        confirmations.request_confirmation(
            store=store,
            loop_job_id="loop-1",
            job_type="policy_change",
            draft_summary="Raise written premium",
            changes_json={"policy_number": "HO-1",
                          "changes": {"writtenPremium": {"old": "2000", "new": "2450"}}},
            requested_by="robie",
        )
        values = RecordingValuesApi()
        self._sync_once(db_path, values)

        self.assertTrue(values.update_modes, "expected at least one values.update call")
        for mode in values.update_modes:
            self.assertEqual("RAW", mode)

    def test_formula_looking_decision_text_round_trips_literally(self):
        """End-to-end: human-typed '=1+1' / '+cmd|...' on the Confirmations
        tab must read back as the literal strings, not evaluated formulas."""
        import tempfile, os
        db_path = os.path.join(tempfile.mkdtemp(), "jobs.db")
        store = JobStore(db_path)
        cid = confirmations.request_confirmation(
            store=store,
            loop_job_id="loop-1",
            job_type="policy_change",
            draft_summary="Raise written premium",
            changes_json={"policy_number": "HO-1",
                          "changes": {"writtenPremium": {"old": "2000", "new": "2450"}}},
            requested_by="robie",
        )
        values = RecordingValuesApi()
        sheets = FakeSheetsApi(values)
        # Human types formula-looking strings on the tab before the sync.
        seed_row = list(confirmation_board.HEADERS)
        seed_row[confirmation_board.ID] = cid
        seed_row[confirmation_board.DECISION] = "=1+1"
        seed_row[confirmation_board.REASON] = "+cmd|'/c calc'!A0"
        seed_row[confirmation_board.STATUS] = "PENDING"
        values.cells[(SHEET_ID, "Confirmations")] = [
            list(confirmation_board.HEADERS), seed_row,
        ]
        values.titles.add("Confirmations")

        confirmation_board.sync_confirmations(
            db_path, SHEET_ID, values_api=values, sheets_api=sheets
        )

        rows = confirmation_board.read_tab_rows(values, SHEET_ID)
        data = [row for row in rows[1:] if row[confirmation_board.ID] == cid]
        self.assertEqual(1, len(data))
        self.assertEqual("=1+1", data[0][confirmation_board.DECISION])
        self.assertEqual("+cmd|'/c calc'!A0", data[0][confirmation_board.REASON])


if __name__ == "__main__":
    unittest.main()
