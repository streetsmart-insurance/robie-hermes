"""Document retrieval filing: date window, dedupe, and fail-closed writes."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from robie_job_engine.document_retrieval_filing import (
    KILL_SWITCH_ENV,
    PROGRESSIVE_MEMO_RULE,
    FilingDeps,
    FilingUnavailable,
    document_is_duplicate,
    eastern_today,
    file_progressive_memos,
    filing_note,
    live_filing_decision,
    nicole_status_comment,
    plan_status_sheet_edit,
    require_retrieval_window,
    retrieval_date_window,
)
from robie_job_engine.ezlynx_discussions import reject_phone_numbers


PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
HOST = "hermes-test-01"
ENABLED = {"ROBIE_ENV": "TEST", KILL_SWITCH_ENV: "1"}
SHEET_DAY = date(2026, 9, 26)
WORKFLOW = {
    "discussionId": "disc-1",
    "title": "Additional Information - Progressive Memo",
}


def memo_item(**overrides):
    item = {
        "policy_number": "993334183",
        "insured_name": "Yolanda Concepcion",
        "filename": "993334183 Progressive Memo Signature.pdf",
        "processed_on": "2026-09-25",
        "department": "Personal",
        "content": PDF,
    }
    item.update(overrides)
    return item


def policy_payload(*applicants, policy="993334183"):
    return {
        "data": {
            "Policies": [
                {"PolicyNumber": policy, "ApplicantId": applicant, "InsuredName": "Yolanda Concepcion"}
                for applicant in applicants
            ]
        }
    }


SHEET_ROWS = [
    ["", "Insured Name", "Policy Number", "Department", "Document Type", "Memo Date", "Comment"],
    ["", "", "", "", "", "", ""],
    ["GEICO", "Charlemagne Guevara", "6253395526", "Personal", "Cancellation", "6/24/2026", "pending"],
    ["Progressive", "3JR Contracting LLC", "860521214", "Commercial", "Memo", "9/25/2026", "pending"],
    ["", "ALTI TRANSPORT LLC", "879512352", "Commercial", "Memo", "9/25/2026", "pending"],
    ["Progressive BOP/CGL", "Matthew Brennan Plumbing Inc.", "PGR973343766", "Commercial", "Cancellation", "9/22/2026", "pending"],
]


class FakeSheet:
    def __init__(self, rows=None, tabs=None):
        self.rows = [list(row) for row in (rows if rows is not None else SHEET_ROWS)]
        self.tabs = {"9/26.": 4} if tabs is None else dict(tabs)
        self.writes = []
        self.inserts = []

    def list_tabs(self):
        return dict(self.tabs)

    def read(self, tab):
        if tab not in self.tabs:
            raise RuntimeError("missing tab")
        return self.rows

    def insert_row(self, sheet_id, before_index):
        self.inserts.append((sheet_id, before_index))
        self.rows.insert(before_index, ["", "", "", "", "", "", ""])

    def write_row(self, tab, row_number, values):
        self.rows[row_number - 1] = list(values)
        self.writes.append((row_number, list(values)))


class FakeDeps:
    def __init__(self, *, documents=None, discussions=None, applicants=("220250093",), activities=None, sheet=None):
        self.uploads = []
        self.notes = []
        self.documents = documents if documents is not None else {"results": []}
        self.discussions = discussions if discussions is not None else [dict(WORKFLOW)]
        self.applicants = applicants
        self.activity_rows = activities
        self.sheets = sheet if sheet is not None else FakeSheet()

    def policy_search(self, policy_number):
        return policy_payload(*self.applicants, policy=policy_number)

    def documents_search(self, applicant_id):
        return self.documents

    def list_discussions(self, applicant_id):
        return self.discussions

    def upload(self, applicant_id, document_name, file_bytes, filename=None):
        self.uploads.append({"applicant_id": applicant_id, "filename": filename, "bytes": file_bytes})
        return {"document_id": "501", "read_back": True}

    def add_note(self, applicant_id, note_text, discussion_title=None):
        self.notes.append({"applicant_id": applicant_id, "text": note_text, "title": discussion_title})
        return {
            "status": "filed",
            "note_id": "77",
            "discussion_id": "disc-1",
            "read_back": True,
            "discussion_title": discussion_title,
        }

    def activities(self, applicant_id):
        return list(self.activity_rows or [])

    def as_deps(self):
        return FilingDeps(
            policy_search=self.policy_search,
            documents_search=self.documents_search,
            list_discussions=self.list_discussions,
            upload=self.upload,
            add_note=self.add_note,
            sheets=self.sheets,
            activities=self.activities if self.activity_rows is not None else None,
        )


def file_with(deps, items, environ=None):
    return file_progressive_memos(
        items,
        environ=ENABLED if environ is None else environ,
        hostname=HOST,
        client_factory=deps.as_deps if isinstance(deps, FakeDeps) else deps,
        sheet_day=SHEET_DAY,
    )


class DateWindowTests(unittest.TestCase):
    def test_saturday_is_yesterday_and_today_only(self):
        start, end = retrieval_date_window(date(2026, 9, 26))
        self.assertEqual((start, end), (date(2026, 9, 25), date(2026, 9, 26)))

    def test_monday_includes_friday_through_monday(self):
        start, end = retrieval_date_window(date(2026, 9, 28))
        self.assertEqual(date(2026, 9, 28).weekday(), 0)
        self.assertEqual((start, end), (date(2026, 9, 25), date(2026, 9, 28)))

    def test_sunday_does_not_include_friday(self):
        start, end = retrieval_date_window(date(2026, 9, 27))
        self.assertEqual((start, end), (date(2026, 9, 26), date(2026, 9, 27)))

    def test_tuesday_is_yesterday_and_today(self):
        start, end = retrieval_date_window(date(2026, 9, 29))
        self.assertEqual((start, end), (date(2026, 9, 28), date(2026, 9, 29)))

    def test_explicit_dates_outside_the_window_hold(self):
        with self.assertRaisesRegex(Exception, "standing retrieval window"):
            require_retrieval_window(date(2026, 9, 25), date(2026, 9, 25), as_of=date(2026, 9, 29))

    def test_eastern_today_is_not_utc(self):
        moment = datetime(2026, 9, 27, 2, 30, tzinfo=timezone.utc)
        self.assertEqual(eastern_today(moment), date(2026, 9, 26))


class DedupeTests(unittest.TestCase):
    def test_same_memo_name_is_a_duplicate(self):
        self.assertTrue(document_is_duplicate(
            policy_number="993334183",
            filename="993334183 Progressive Memo Signature.pdf",
            existing_name="993334183 Progressive Memo Signature.pdf",
        ))

    def test_a_suffixed_copy_is_a_duplicate_and_a_different_reason_is_not(self):
        self.assertTrue(document_is_duplicate(
            policy_number="993334183",
            filename="993334183 Progressive Memo Signature.pdf",
            existing_name="993334183 Progressive Memo Signature (1).pdf",
        ))
        self.assertFalse(document_is_duplicate(
            policy_number="860521214",
            filename="860521214 Progressive Memo General.pdf",
            existing_name="860521214 Progressive Memo Signature.pdf",
        ))

    def test_another_document_type_on_the_same_policy_is_not_a_duplicate(self):
        self.assertFalse(document_is_duplicate(
            policy_number="860521214",
            filename="860521214 Progressive Memo General.pdf",
            existing_name="860521214 Declarations.pdf",
            existing_policy="860521214",
        ))

    def test_a_different_policy_does_not_match(self):
        self.assertFalse(document_is_duplicate(
            policy_number="993334183",
            filename="993334183 Progressive Memo Signature.pdf",
            existing_name="860521214 Progressive Memo Signature.pdf",
            existing_policy="860521214",
        ))


class FilingGateTests(unittest.TestCase):
    def test_kill_switch_defaults_off_and_does_not_build_a_client(self):
        def boom():
            raise AssertionError("client factory must not run")

        result = file_progressive_memos(
            [memo_item()],
            environ={"ROBIE_ENV": "TEST"},
            hostname=HOST,
            client_factory=boom,
            sheet_day=SHEET_DAY,
        )
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["attempted_writes"])
        self.assertIn(KILL_SWITCH_ENV, result["reason"])
        self.assertEqual(result["results"], [])

    def test_production_and_the_production_host_stay_disabled(self):
        self.assertFalse(live_filing_decision({"ROBIE_ENV": "PRODUCTION", KILL_SWITCH_ENV: "1"}, HOST).allowed)
        self.assertFalse(live_filing_decision({"ROBIE_ENV": "TEST", KILL_SWITCH_ENV: "1"}, "hermes-poc-01").allowed)
        self.assertFalse(live_filing_decision({"ROBIE_ENV": "TEST", KILL_SWITCH_ENV: "1"}, "laptop").allowed)
        self.assertTrue(live_filing_decision(ENABLED, HOST).allowed)

    def test_missing_api_client_holds_without_a_document_id(self):
        def factory():
            raise FilingUnavailable("EZLynx API is not configured")

        result = file_progressive_memos(
            [memo_item()],
            environ=ENABLED,
            hostname=HOST,
            client_factory=factory,
            sheet_day=SHEET_DAY,
        )
        self.assertEqual(result["status"], "held")
        self.assertFalse(result["attempted_writes"])
        self.assertNotIn("document_id", result)
        self.assertEqual(result["results"], [])
        self.assertIn("unavailable", result["reason"])

    def test_duplicate_document_skips_upload_and_note(self):
        deps = FakeDeps(documents={
            "results": [{"id": "9", "documentName": "993334183 Progressive Memo Signature.pdf"}],
        })
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "skipped_duplicate")
        self.assertEqual(deps.uploads, [])
        self.assertEqual(deps.notes, [])
        self.assertFalse(result["attempted_writes"])
        self.assertEqual(result["results"][0]["status"], "skipped_duplicate")
        self.assertNotIn("document_id", result["results"][0])

    def test_activity_match_skips_when_a_caller_supplies_activities(self):
        deps = FakeDeps(activities=[{
            "policy_number": "993334183",
            "title": "993334183 Progressive Memo Signature",
        }])
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["activities_check"], "checked")
        self.assertEqual(result["results"][0]["status"], "skipped_duplicate")
        self.assertEqual(deps.uploads, [])

    def test_activities_are_not_used_when_no_client_is_wired(self):
        deps = FakeDeps()
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["activities_check"], "not_used")
        self.assertEqual(len(deps.uploads), 1)

    def test_missing_workflow_holds_before_upload(self):
        deps = FakeDeps(discussions=[])
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "held")
        self.assertEqual(deps.uploads, [])
        self.assertIn("Refusing to invent", result["results"][0]["reason"])
        self.assertNotIn("document_id", result["results"][0])

    def test_two_applicants_hold_without_a_write(self):
        deps = FakeDeps(applicants=("220250093", "220250094"))
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "held")
        self.assertEqual(deps.uploads, [])
        self.assertIn("one applicant", result["results"][0]["reason"])

    def test_successful_file_is_one_applicant_then_the_next(self):
        deps = FakeDeps()
        second = memo_item(
            policy_number="983754955",
            insured_name="MHS LLC",
            filename="983754955 Progressive Memo General.pdf",
            department="Commercial",
        )
        result = file_with(deps, [memo_item(), second])
        self.assertEqual(result["status"], "filed")
        self.assertEqual([row["filename"] for row in deps.uploads], [
            "993334183 Progressive Memo Signature.pdf",
            "983754955 Progressive Memo General.pdf",
        ])
        self.assertEqual(len(deps.notes), 2)
        self.assertTrue(deps.notes[0]["text"].endswith("ROBIE was here"))
        self.assertEqual(deps.notes[0]["title"], PROGRESSIVE_MEMO_RULE.workflow_title)
        comment = nicole_status_comment(PROGRESSIVE_MEMO_RULE)
        self.assertEqual(comment, "Added to the Additional Information folder and WF: Additional Information - Progressive Memo")
        written = [values for _row, values in deps.sheets.writes]
        self.assertTrue(any(row[6] == comment and row[2] == "993334183" for row in written))
        self.assertTrue(any(row[0] == "Progressive BOP/CGL" for row in deps.sheets.rows))
        self.assertEqual(result["results"][0]["document_id"], "501")
        self.assertEqual(result["results"][0]["note_id"], "77")
        self.assertEqual(result["results"][0]["folder_field"], "not_in_proven_document_upload")

    def test_upload_without_read_back_does_not_claim_success(self):
        deps = FakeDeps()

        def upload(*args, **kwargs):
            deps.uploads.append(args)
            return {"document_id": "501", "read_back": False}

        deps.upload = upload
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "held")
        self.assertEqual(deps.notes, [])
        self.assertNotEqual(result["results"][0].get("status"), "filed")

    def test_missing_daily_tab_holds_before_upload(self):
        deps = FakeDeps(sheet=FakeSheet(tabs={"9/25.": 1}))
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "held")
        self.assertEqual(deps.uploads, [])
        self.assertIn("does not exist", result["reason"])

    def test_note_is_short_plain_english(self):
        text = filing_note(PROGRESSIVE_MEMO_RULE, date(2026, 9, 25))
        reject_phone_numbers(text)
        self.assertTrue(text.endswith("ROBIE was here"))
        self.assertNotIn("993334183", text)
        self.assertIn("Additional Information", text)


class StatusSheetPlanTests(unittest.TestCase):
    def test_new_progressive_memo_inserts_before_the_next_carrier(self):
        edit = plan_status_sheet_edit(
            SHEET_ROWS,
            carrier="Progressive",
            insured_name="Yolanda Concepcion",
            policy_number="993334183",
            department="Personal",
            document_type="Memo",
            memo_date="9/25/2026",
            comment="Added to the Additional Information folder and WF: Additional Information - Progressive Memo",
        )
        self.assertEqual(edit.kind, "insert")
        self.assertEqual(edit.row_index, 5)
        self.assertEqual(edit.values[0], "")
        self.assertEqual(edit.values[2], "993334183")
        self.assertNotEqual(SHEET_ROWS[5][0], "Yolanda Concepcion")

    def test_same_policy_and_memo_date_updates_the_comment(self):
        edit = plan_status_sheet_edit(
            SHEET_ROWS,
            carrier="Progressive",
            insured_name="3JR Contracting LLC",
            policy_number="860521214",
            department="Commercial",
            document_type="Memo",
            memo_date="2026-09-25",
            comment="Added to the Additional Information folder and WF: Additional Information - Progressive Memo",
        )
        self.assertEqual(edit.kind, "update")
        self.assertEqual(edit.values[0], "Progressive")
        self.assertEqual(edit.values[6].startswith("Added to the Additional Information folder"), True)

    def test_missing_carrier_section_holds(self):
        with self.assertRaisesRegex(Exception, "no Farmers"):
            plan_status_sheet_edit(
                SHEET_ROWS,
                carrier="Farmers",
                insured_name="Ada",
                policy_number="1",
                department="",
                document_type="Memo",
                memo_date="9/26/2026",
                comment="Added to the folder and WF: example",
            )


class SourceContractTests(unittest.TestCase):
    def test_filing_reuses_the_api_writers_and_does_not_import_playwright(self):
        text = Path("robie_job_engine/document_retrieval_filing.py").read_text(encoding="utf-8")
        self.assertIn("upload_document_via_api", text)
        self.assertIn("add_note_to_discussion", text)
        self.assertNotIn("playwright", text.casefold())
        self.assertNotIn("discussions/v1/notes", text)
        self.assertIn("hermes-poc-01", text)
        self.assertNotIn("systemd", text.casefold())


if __name__ == "__main__":
    unittest.main()
