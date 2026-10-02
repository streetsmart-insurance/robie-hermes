"""Document retrieval filing: date window, dedupe, and fail-closed writes."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from robie_job_engine.document_retrieval_filing import (
    GEICO_NOC_RULE,
    KILL_SWITCH_ENV,
    NATGEN_NOC_RULE,
    NO_WORKFLOW_SHEET_COMMENT,
    PROGRESSIVE_BOP_RULE,
    TRAVELERS_ACTIVITY_RULE,
    PROGRESSIVE_MEMO_RULE,
    FilingDeps,
    FilingUnavailable,
    document_is_duplicate,
    eastern_today,
    file_carrier_batch,
    file_progressive_memos,
    filing_note,
    live_filing_decision,
    nicole_status_comment,
    plan_status_sheet_edit,
    require_retrieval_window,
    retrieval_date_window,
    review_task_payload,
)
from robie_job_engine import zapier_tasks
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
        self.tasks = []

    def policy_search(self, policy_number):
        return policy_payload(*self.applicants, policy=policy_number)

    def documents_search(self, applicant_id):
        return self.documents

    def list_discussions(self, applicant_id):
        return self.discussions

    def upload(self, applicant_id, document_name, file_bytes, filename=None):
        self.uploads.append({"applicant_id": applicant_id, "filename": filename, "bytes": file_bytes})
        return {"document_id": "501", "read_back": True}

    def add_note(self, applicant_id, note_text, discussion_title=None, document_id=None):
        self.notes.append({
            "applicant_id": applicant_id,
            "text": note_text,
            "title": discussion_title,
            "document_id": document_id,
        })
        return {
            "status": "filed",
            "note_id": "77",
            "discussion_id": "disc-1",
            "read_back": True,
            "discussion_title": discussion_title,
        }

    def activities(self, applicant_id):
        return list(self.activity_rows or [])

    def fire_task(self, payload, *, dry_run=False):
        zapier_tasks.validate_task_payload(payload)
        self.tasks.append({"payload": dict(payload), "dry_run": dry_run})
        return {"ok": True, "dry_run": dry_run}

    def as_deps(self):
        return FilingDeps(
            policy_search=self.policy_search,
            documents_search=self.documents_search,
            list_discussions=self.list_discussions,
            upload=self.upload,
            add_note=self.add_note,
            sheets=self.sheets,
            activities=self.activities if self.activity_rows is not None else None,
            fire_task=self.fire_task,
        )


def file_with(deps, items, environ=None, *, rule=None, zapier_dry_run=True):
    factory = deps.as_deps if isinstance(deps, FakeDeps) else deps
    kwargs = dict(
        items=items,
        environ=ENABLED if environ is None else environ,
        hostname=HOST,
        client_factory=factory,
        sheet_day=SHEET_DAY,
        zapier_dry_run=zapier_dry_run,
    )
    if rule is None:
        return file_progressive_memos(**kwargs)
    return file_carrier_batch(rule=rule, **kwargs)


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

    def test_natgen_and_geico_pdf_names_match_on_policy_and_document_type(self):
        # Live EZLynx docs 824464961 and 824464983 keep the .pdf extension.
        self.assertTrue(document_is_duplicate(
            policy_number="2035471506 00",
            filename="2035471506 00 NatGen NOC non-payment.pdf",
            existing_name="2035471506 00 NatGen NOC non-payment.pdf",
        ))
        self.assertTrue(document_is_duplicate(
            policy_number="2031936859 00",
            filename="2031936859 00 NatGen NOC nsf.pdf",
            existing_name="2031936859 00 NatGen NOC nsf.pdf",
        ))
        self.assertTrue(document_is_duplicate(
            policy_number="2035471506 00",
            filename="2035471506 00 NatGen NOC non-payment.pdf",
            existing_name="2035471506-00 NatGen NOC non-payment.PDF",
        ))
        self.assertFalse(document_is_duplicate(
            policy_number="2035471506 00",
            filename="2035471506 00 NatGen NOC non-payment.pdf",
            existing_name="2035471506 00 NatGen NOC nsf.pdf",
        ))
        self.assertTrue(document_is_duplicate(
            policy_number="6123456789",
            filename="6123456789 NOC Geico.pdf",
            existing_name="6123456789 NOC Geico.pdf",
        ))
        self.assertFalse(document_is_duplicate(
            policy_number="6123456789",
            filename="6123456789 NOC Geico.pdf",
            existing_name="6123456789 Declarations Geico.pdf",
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
        self.assertEqual(deps.tasks, [])
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

    def test_missing_workflow_uploads_skips_the_note_and_dry_runs_the_task(self):
        deps = FakeDeps(discussions=[])
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "filed_no_workflow")
        self.assertEqual(len(deps.uploads), 1)
        self.assertEqual(deps.notes, [])
        self.assertEqual(len(deps.tasks), 1)
        self.assertTrue(deps.tasks[0]["dry_run"])
        payload = deps.tasks[0]["payload"]
        self.assertEqual(payload["applicant_id"], "220250093")
        self.assertEqual(payload["assignee"], "SSNicole")
        self.assertEqual(payload["source"], "document-retrieval")
        self.assertEqual(payload["due_date"], "2026-09-26")
        self.assertEqual(
            payload["task_title"],
            "Document Retrieval review — Progressive Memo — Yolanda Concepcion — 993334183",
        )
        row = result["results"][0]
        self.assertEqual(row["document_id"], "501")
        self.assertEqual(row["comment"], NO_WORKFLOW_SHEET_COMMENT)
        self.assertNotIn("note_id", row)
        self.assertEqual(deps.sheets.writes[-1][1][6], NO_WORKFLOW_SHEET_COMMENT)
        self.assertTrue(result["attempted_writes"])

    def test_document_upload_failure_does_not_fire_a_task(self):
        deps = FakeDeps(discussions=[])

        def upload(*args, **kwargs):
            raise RuntimeError("document api down")

        deps.upload = upload
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "held")
        self.assertEqual(deps.tasks, [])
        self.assertEqual(deps.notes, [])
        self.assertNotIn("document_id", result["results"][0])

    def test_zapier_dry_run_reaches_fire_task_without_a_live_hook(self):
        import json
        import os
        import tempfile

        script = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        script.write(
            "import json, sys\n"
            "if '--dry-run' not in sys.argv:\n"
            "    print(json.dumps({'ok': False, 'fired': True}))\n"
            "    raise SystemExit(2)\n"
            "print(json.dumps({'ok': True, 'dry_run': True}))\n"
        )
        script.close()
        deps = FakeDeps(discussions=[])

        def fire(payload, *, dry_run=False):
            deps.tasks.append({"payload": dict(payload), "dry_run": dry_run})
            return zapier_tasks.fire_task(payload, dry_run=dry_run)

        deps.fire_task = fire
        original = os.environ.get("ROBIE_ZAP_TRIGGER")
        os.environ["ROBIE_ZAP_TRIGGER"] = script.name
        try:
            result = file_with(deps, [memo_item()], zapier_dry_run=True)
        finally:
            if original is None:
                os.environ.pop("ROBIE_ZAP_TRIGGER", None)
            else:
                os.environ["ROBIE_ZAP_TRIGGER"] = original
            os.unlink(script.name)
        self.assertEqual(result["status"], "filed_no_workflow")
        self.assertEqual(deps.notes, [])
        self.assertTrue(deps.tasks[0]["dry_run"])
        self.assertTrue(result["results"][0]["zapier_dry_run"])
        expected = review_task_payload(
            PROGRESSIVE_MEMO_RULE,
            applicant_id="220250093",
            insured_name="Yolanda Concepcion",
            policy_number="993334183",
            due_on=SHEET_DAY,
        )
        self.assertEqual(deps.tasks[0]["payload"]["task_title"], expected["task_title"])

    def test_geico_sketch_uses_the_same_task_fallback(self):
        deps = FakeDeps(discussions=[])
        item = memo_item(
            policy_number="6253395526",
            insured_name="Charlemagne Guevara",
            filename="6253395526 NOC Geico.pdf",
        )
        result = file_with(deps, [item], rule=GEICO_NOC_RULE)
        self.assertEqual(result["status"], "filed_no_workflow")
        self.assertEqual(deps.notes, [])
        self.assertEqual(len(deps.uploads), 1)
        self.assertEqual(
            deps.tasks[0]["payload"]["task_title"],
            "Document Retrieval review — Geico Cancellation — Charlemagne Guevara — 6253395526",
        )
        self.assertEqual(PROGRESSIVE_BOP_RULE.carrier_section, "Progressive BOP/CGL")
        self.assertEqual(PROGRESSIVE_BOP_RULE.workflow_title, "")
        self.assertEqual(NATGEN_NOC_RULE.document_type, "NOC")
        self.assertEqual(NATGEN_NOC_RULE.workflow_title, "")
        self.assertEqual(TRAVELERS_ACTIVITY_RULE.document_type, "Policy Activity")
        self.assertEqual(TRAVELERS_ACTIVITY_RULE.workflow_title, "")

    def test_rejected_review_task_does_not_write_the_no_workflow_comment(self):
        deps = FakeDeps(discussions=[])

        def fire(payload, *, dry_run=False):
            deps.tasks.append({"payload": dict(payload), "dry_run": dry_run})
            return {"ok": False}

        deps.fire_task = fire
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["results"][0]["status"], "document_filed_task_held")
        self.assertEqual(result["status"], "held")
        self.assertEqual(len(deps.uploads), 1)
        self.assertEqual(deps.notes, [])
        self.assertEqual(deps.sheets.writes, [])
        self.assertNotEqual(result["results"][0].get("comment"), NO_WORKFLOW_SHEET_COMMENT)

    def test_two_applicants_hold_without_a_write(self):
        deps = FakeDeps(applicants=("220250093", "220250094"))
        result = file_with(deps, [memo_item()])
        self.assertEqual(result["status"], "held")
        self.assertEqual(deps.uploads, [])
        self.assertEqual(deps.tasks, [])
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
        self.assertEqual(deps.tasks, [])
        self.assertTrue(deps.notes[0]["text"].endswith("ROBIE was here"))
        self.assertEqual(deps.notes[0]["title"], PROGRESSIVE_MEMO_RULE.workflow_title)
        comment = nicole_status_comment(PROGRESSIVE_MEMO_RULE)
        self.assertEqual(comment, "Added to the Additional Information folder and the Additional Information - Progressive Memo workflow.")
        written = [values for _row, values in deps.sheets.writes]
        self.assertTrue(any(row[6] == comment and row[2] == "993334183" for row in written))
        self.assertTrue(any(row[0] == "Progressive BOP/CGL" for row in deps.sheets.rows))
        self.assertEqual(result["results"][0]["document_id"], "501")
        self.assertEqual(result["results"][0]["note_id"], "77")
        self.assertEqual(deps.notes[0]["document_id"], "501")
        self.assertEqual(result["results"][0]["folder_field"], "not_in_proven_document_upload")

    def test_unconfirmed_note_stays_held_in_plain_english(self):
        deps = FakeDeps()

        def add_note(applicant_id, note_text, discussion_title=None, document_id=None):
            deps.notes.append(
                {
                    "applicant_id": applicant_id,
                    "text": note_text,
                    "title": discussion_title,
                    "document_id": document_id,
                }
            )
            return {
                "status": "held",
                "note_id": None,
                "read_back": False,
                "reason": (
                    "The note was sent, but the discussion did not show exactly one new note. "
                    "It was not sent again."
                ),
            }

        deps.add_note = add_note
        result = file_with(deps, [memo_item()])
        reason = result["results"][0]["reason"]
        self.assertEqual(result["results"][0]["status"], "document_filed_note_held")
        self.assertIn("not sent again", reason)
        self.assertNotIn("note_id", reason)
        self.assertNotIn("DiscussionApi", reason)
        self.assertEqual(deps.notes[0]["document_id"], "501")
        self.assertEqual(deps.sheets.writes, [])

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
        self.assertIn("fire_task", text)
        self.assertIn("custom.zapier-webhook", text)
        self.assertNotIn("playwright", text.casefold())
        self.assertNotIn("discussions/v1/notes", text)
        self.assertIn("hermes-poc-01", text)
        self.assertNotIn("systemd", text.casefold())


TEST_OVERRIDE = {**ENABLED, "ROBIE_DOCUMENT_RETRIEVAL_TEST_APPLICANT_ID": "26356199"}
BB_DISCUSSIONS = [
    {"discussionId": "819225260", "title": "Additional Information - CHANGE ME"},
    {"discussionId": "819225210", "title": "additional information"},
    {"discussionId": "900", "title": ""},
]


class TestApplicantOverrideTests(unittest.TestCase):
    def test_override_is_refused_outside_test(self):
        from robie_job_engine.document_retrieval_filing import FilingHeld, test_applicant_override

        with self.assertRaises(FilingHeld):
            test_applicant_override({**TEST_OVERRIDE, "ROBIE_ENV": "PRODUCTION"}, HOST)
        with self.assertRaises(FilingHeld):
            test_applicant_override(TEST_OVERRIDE, "hermes-poc-01")
        with self.assertRaises(FilingHeld):
            test_applicant_override({**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_APPLICANT_ID": "bb"}, HOST)
        self.assertIsNone(test_applicant_override(ENABLED, HOST))
        self.assertEqual(test_applicant_override(TEST_OVERRIDE, HOST), "26356199")

    def test_override_on_production_host_writes_nothing(self):
        deps = FakeDeps()
        result = file_carrier_batch(
            [memo_item()], rule=PROGRESSIVE_MEMO_RULE, environ=TEST_OVERRIDE,
            hostname="hermes-poc-01", client_factory=deps.as_deps,
        )
        self.assertNotEqual(result["status"], "filed")
        self.assertEqual(deps.uploads, [])

    def test_files_to_test_account_with_plain_note_and_no_sheet_or_task(self):
        deps = FakeDeps(discussions=list(BB_DISCUSSIONS), applicants=("999",))
        env = {**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE": "Additional Information - CHANGE ME"}
        item = memo_item(
            policy_number="876263535", insured_name="Groesbeck, Zachary",
            filename="876263535 Progressive Memo Discount Memo.pdf", processed_on="2026-09-29",
        )
        result = file_with(deps, [item], env)
        self.assertEqual(result["status"], "filed", result)
        self.assertEqual(deps.uploads[0]["applicant_id"], "26356199")
        self.assertEqual(deps.uploads[0]["filename"], "876263535 Progressive Memo Discount Memo.pdf")
        self.assertEqual(deps.notes[0]["title"], "Additional Information - CHANGE ME")
        self.assertEqual(
            deps.notes[0]["text"],
            filing_note(
                PROGRESSIVE_MEMO_RULE, date(2026, 9, 29),
                policy_number="876263535",
                filename="876263535 Progressive Memo Discount Memo.pdf",
            ),
        )
        self.assertIn("file 876263535 Progressive Memo Discount Memo", deps.notes[0]["text"])
        self.assertNotIn("Groesbeck", deps.notes[0]["text"])
        self.assertEqual(deps.sheets.writes, [])
        self.assertEqual(deps.tasks, [])
        row = result["results"][0]
        self.assertEqual((row["real_client"], row["real_policy"], row["document_id"], row["note_id"]),
                         ("Groesbeck, Zachary", "876263535", "501", "77"))
        self.assertEqual(result["test_account_discussion"]["id"], "819225260")

    def test_phone_like_policy_is_shortened_in_the_note(self):
        deps = FakeDeps(discussions=list(BB_DISCUSSIONS))
        env = {**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE": "Additional Information - CHANGE ME"}
        item = memo_item(
            policy_number="2035471506 00", insured_name="A&E CONTRACTOR LLC",
            filename="2035471506 00 NatGen NOC non-payment.pdf", processed_on="2026-09-30",
        )
        result = file_with(deps, [item], env, rule=NATGEN_NOC_RULE)
        self.assertEqual(result["status"], "filed", result)
        self.assertEqual(
            deps.notes[0]["text"],
            filing_note(
                NATGEN_NOC_RULE, date(2026, 9, 30),
                policy_number="2035471506 00",
                filename="2035471506 00 NatGen NOC non-payment.pdf",
            ),
        )
        self.assertIn("non-payment", deps.notes[0]["text"])
        self.assertNotIn("A&E", deps.notes[0]["text"])
        reject_phone_numbers(deps.notes[0]["text"])

    def test_dirty_fao_client_name_stays_out_of_the_note(self):
        deps = FakeDeps(discussions=list(BB_DISCUSSIONS))
        env = {**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE": "Additional Information - CHANGE ME"}
        dirty = "Groesbeck, Zachary 2 Round Hill Rd Jackson, Nj 08527 H:(732) 995-2407 Email"
        filename = "876263535 Progressive Memo Discount Memo.pdf"
        item = memo_item(
            policy_number="876263535",
            insured_name=dirty,
            filename=filename,
            processed_on="2026-09-29",
        )
        result = file_with(deps, [item], env)
        self.assertEqual(result["status"], "filed", result)
        note = deps.notes[0]["text"]
        self.assertEqual(
            note,
            filing_note(
                PROGRESSIVE_MEMO_RULE, date(2026, 9, 29),
                policy_number="876263535", filename=filename,
            ),
        )
        self.assertNotIn("995-2407", note)
        self.assertNotIn("Round Hill", note)
        self.assertEqual(result["results"][0]["real_client"], "Groesbeck, Zachary")
        reject_phone_numbers(note)

    def test_existing_natgen_and_geico_pdfs_are_skipped(self):
        env = {**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE": "Additional Information - CHANGE ME"}
        natgen = FakeDeps(
            discussions=list(BB_DISCUSSIONS),
            documents={
                "results": [
                    {"id": "824464961", "documentName": "2035471506 00 NatGen NOC non-payment.pdf"},
                    {"id": "824464983", "documentName": "2031936859 00 NatGen NOC nsf.pdf"},
                ],
            },
        )
        natgen_items = [
            memo_item(
                policy_number="2035471506 00",
                insured_name="A&E CONTRACTOR LLC",
                filename="2035471506 00 NatGen NOC non-payment.pdf",
                processed_on="2026-09-30",
            ),
            memo_item(
                policy_number="2031936859 00",
                insured_name="Sample Client",
                filename="2031936859 00 NatGen NOC nsf.pdf",
                processed_on="2026-09-30",
            ),
        ]
        result = file_with(natgen, natgen_items, env, rule=NATGEN_NOC_RULE)
        self.assertEqual(
            [row["status"] for row in result["results"]],
            ["skipped_duplicate", "skipped_duplicate"],
            result,
        )
        self.assertEqual(natgen.uploads, [])
        geico = FakeDeps(
            discussions=list(BB_DISCUSSIONS),
            documents={"results": [{"id": "900", "documentName": "6123456789 NOC Geico.pdf"}]},
        )
        geico_item = memo_item(
            policy_number="6123456789",
            insured_name="Sample Client",
            filename="6123456789 NOC Geico.pdf",
            processed_on="2026-09-30",
        )
        geico_result = file_with(geico, [geico_item], env, rule=GEICO_NOC_RULE)
        self.assertEqual(geico_result["results"][0]["status"], "skipped_duplicate", geico_result)
        self.assertEqual(geico.uploads, [])

    def test_duplicate_on_test_account_skips_upload(self):
        deps = FakeDeps(
            discussions=list(BB_DISCUSSIONS),
            documents={"results": [{"name": "876263535 Progressive Memo Discount Memo.pdf", "id": 5}]},
        )
        env = {**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE": "Additional Information - CHANGE ME"}
        item = memo_item(policy_number="876263535", filename="876263535 Progressive Memo Discount Memo.pdf")
        result = file_with(deps, [item], env)
        self.assertEqual(result["results"][0]["status"], "skipped_duplicate", result)
        self.assertEqual(deps.uploads, [])

    def test_missing_discussion_holds_and_lists_titles_without_creating(self):
        deps = FakeDeps(discussions=list(BB_DISCUSSIONS))
        result = file_with(deps, [memo_item()], TEST_OVERRIDE)
        self.assertEqual(result["status"], "held")
        self.assertIn("Additional Information - CHANGE ME", result["test_account_discussions"])
        self.assertEqual(deps.uploads, [])
        self.assertEqual(deps.notes, [])

    def test_ambiguous_substring_title_is_not_used(self):
        deps = FakeDeps(discussions=list(BB_DISCUSSIONS))
        env = {**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE": "additional information"}
        result = file_with(deps, [memo_item()], env)
        self.assertEqual(result["status"], "held")
        self.assertEqual(deps.uploads, [])

    def test_filename_without_policy_is_held(self):
        deps = FakeDeps(discussions=list(BB_DISCUSSIONS))
        env = {**TEST_OVERRIDE, "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE": "Additional Information - CHANGE ME"}
        result = file_with(deps, [memo_item(filename="memo.pdf")], env)
        self.assertEqual(result["results"][0]["status"], "held")
        self.assertEqual(deps.uploads, [])


class PackItemTests(unittest.TestCase):
    def test_pack_items_read_pulled_pdfs_from_manifests(self):
        import json
        import tempfile

        from robie_job_engine.document_retrieval_filing import pack_filing_items

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-09-30"
            day.mkdir()
            (day / "2035471506 00 NatGen NOC non-payment.pdf").write_bytes(PDF)
            (day / "manifest.json").write_text(json.dumps({
                "processed_date": "2026-09-30",
                "nocs": [
                    {"filename": "2035471506 00 NatGen NOC non-payment.pdf", "policy_number": "2035471506 00",
                     "insured_name": "A&E CONTRACTOR LLC", "disposition": "pulled"},
                    {"filename": "missing.pdf", "policy_number": "1", "insured_name": "X", "disposition": "pulled"},
                    {"filename": "held.pdf", "policy_number": "2", "insured_name": "Y", "disposition": "held"},
                ],
            }))
            items = pack_filing_items(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["policy_number"], "2035471506 00")
        self.assertEqual(items[0]["processed_on"], "2026-09-30")

    def test_fao_pack_client_name_drops_the_street_and_phone(self):
        import json
        import tempfile

        from robie_job_engine.document_retrieval_filing import pack_filing_items

        dirty = "Groesbeck, Zachary 2 Round Hill Rd Jackson, Nj 08527 H:(732) 995-2407 Email"
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-09-29"
            day.mkdir()
            (day / "876263535 Progressive Memo Discount Memo.pdf").write_bytes(PDF)
            (day / "manifest.json").write_text(json.dumps({
                "processed_date": "2026-09-29",
                "memos": [{
                    "filename": "876263535 Progressive Memo Discount Memo.pdf",
                    "policy_number": "876263535",
                    "insured_name": dirty,
                    "disposition": "pulled",
                }],
            }))
            items = pack_filing_items(tmp)
        self.assertEqual(items[0]["insured_name"], "Groesbeck, Zachary")
        self.assertNotIn("995-2407", items[0]["insured_name"])


class OutputPrivacyTests(unittest.TestCase):
    def test_pull_output_directories_are_created_at_0700(self):
        import os
        import tempfile

        from robie_job_engine.natgen_pending_cancellation import LocalDeliveryLedger as NatGenLedger
        from robie_job_engine.progressive_fao_memo import LocalDeliveryLedger as FaoLedger

        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for ledger in (FaoLedger(root / "fao"), NatGenLedger(root / "natgen")):
                    ledger.ensure_private()
                    self.assertEqual(ledger.root.stat().st_mode & 0o777, 0o700)
                    day = ledger.date_dir(date(2026, 9, 29))
                    self.assertEqual(day.stat().st_mode & 0o777, 0o700)
        finally:
            os.umask(previous)


class PlainSheetTextTests(unittest.TestCase):
    def test_sheet_texts_have_no_codes(self):
        for text in (NO_WORKFLOW_SHEET_COMMENT, nicole_status_comment(PROGRESSIVE_MEMO_RULE)):
            self.assertNotIn("WF", text)
            self.assertNotIn(";", text)


if __name__ == "__main__":
    unittest.main()
