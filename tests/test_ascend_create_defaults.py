"""Create-program defaults and spinner timing. No live Ascend."""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.ascend_create_defaults import (
    AGENCY_FEE_SCENARIO_ID,
    CREATE_PATH,
    CREATE_URL,
    PAWIVA_SPINNER_SECONDS,
    SPINNER_TIMING_SCENARIO_ID,
    TEST_AGENCY_FEE,
    WAIT_FOR_URL,
    agency_fee_default_is_empty_or_zero,
    classify_document_labels,
    create_url_is_new_program,
    log_role_defaults,
    new_program_click_is_primary,
    refuse_unexpected_agency_fee_default,
    refuse_unlogged_agency_fee_default,
    require_create_new_url,
    require_spinner_seconds_logged,
    run_agency_fee_default_scenario,
    run_role_default_log_scenario,
    run_spinner_timing_scenario,
    should_set_test_agency_fee,
    spinner_seconds,
    test_agency_fee_set_value,
)
from robie_job_engine.ascend_locator_audit import (
    JOB_TYPE,
    AscendLocatorAuditWorker,
    default_audit_payload,
    run_ci_assertion_battery,
)
from robie_job_engine.store import JobStore
from robie_job_engine.ascend_sender_roles import (
    CARLO_FERRARA,
    JAKE_FERRARA,
    NEW_PROGRAM_LOCATOR,
    ROBIE_AI,
    ascend_new_program_contract_lines,
)
from robie_job_engine.job_type_gate import is_job_type_production_ready


class SpinnerTimingTests(unittest.TestCase):
    def test_logs_seconds_and_refuses_missing(self):
        self.assertEqual(spinner_seconds(100.0, 112.0), PAWIVA_SPINNER_SECONDS)
        self.assertIsNone(require_spinner_seconds_logged(12.0))
        self.assertIsNotNone(require_spinner_seconds_logged(None))
        self.assertIn("not logged", require_spinner_seconds_logged(None) or "")

    def test_clicks_primary_then_waits_for_create_new(self):
        self.assertTrue(new_program_click_is_primary(NEW_PROGRAM_LOCATOR))
        self.assertFalse(new_program_click_is_primary("split-menu caret"))
        self.assertTrue(create_url_is_new_program(CREATE_URL))
        self.assertTrue(create_url_is_new_program(f"{CREATE_URL}?x=1"))
        self.assertIsNone(require_create_new_url(CREATE_URL))
        self.assertIsNotNone(
            require_create_new_url("https://dashboard.useascend.com/programs")
        )
        self.assertIn(CREATE_PATH, WAIT_FOR_URL)

    def test_named_timing_scenario_passes(self):
        report = run_spinner_timing_scenario()
        self.assertEqual(report["id"], SPINNER_TIMING_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))
        self.assertEqual(report["observed"]["seconds"], 12.0)


class AgencyFeeDefaultTests(unittest.TestCase):
    def test_expects_zero_or_empty_then_sets_500(self):
        for default in ("$0.00", "", "0", "0.00", "$0"):
            with self.subTest(default=default):
                self.assertTrue(agency_fee_default_is_empty_or_zero(default))
                self.assertIsNone(
                    refuse_unexpected_agency_fee_default(default, field_present=True)
                )
        self.assertIsNotNone(
            refuse_unexpected_agency_fee_default("25.00", field_present=True)
        )
        self.assertIsNotNone(
            refuse_unlogged_agency_fee_default(None, field_present=True)
        )
        self.assertEqual(test_agency_fee_set_value(), TEST_AGENCY_FEE)
        self.assertTrue(should_set_test_agency_fee(field_present=True))
        self.assertFalse(should_set_test_agency_fee(field_present=False))

    def test_logs_import_upload_dropzone(self):
        labels = classify_document_labels(
            "Import document", "Upload document", "Drop files here"
        )
        self.assertTrue(labels["import_document"])
        self.assertTrue(labels["upload_document"])
        self.assertTrue(labels["dropzone"])
        self.assertIn("Import document", labels["labels"])
        empty = classify_document_labels("Create a program")
        self.assertTrue(empty["logged"])
        self.assertEqual(empty["labels"], [])

    def test_named_fee_scenario_passes(self):
        report = run_agency_fee_default_scenario()
        self.assertEqual(report["id"], AGENCY_FEE_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))
        self.assertEqual(report["observed"]["set_value"], "500")


class RoleDefaultLogTests(unittest.TestCase):
    def test_logs_prefill_and_fails_if_robie_ai_stays(self):
        for requested, expected in (
            ("Carlo Ferrara", CARLO_FERRARA),
            ("Jake Ferrara", JAKE_FERRARA),
        ):
            with self.subTest(requested=requested):
                logged = log_role_defaults(
                    requested_by=requested,
                    producer=ROBIE_AI,
                    account_manager=ROBIE_AI,
                )
                self.assertTrue(logged["logged"])
                self.assertEqual(logged["producer_default"], ROBIE_AI)
                self.assertIsNotNone(logged["error"])
                overwritten = log_role_defaults(
                    requested_by=requested,
                    producer=expected,
                    account_manager=expected,
                )
                self.assertIsNone(overwritten["error"])
        missing = log_role_defaults(
            requested_by="Carlo Ferrara",
            producer=None,
            account_manager=None,
        )
        self.assertFalse(missing["logged"])
        self.assertIsNotNone(missing["error"])

    def test_named_role_default_scenario_passes(self):
        report = run_role_default_log_scenario()
        self.assertTrue(report["ok"], report.get("evidence"))


class FixtureAndDocsTests(unittest.TestCase):
    def test_ci_fixture_walk_logs_the_three_findings(self):
        with durable_temporary_directory() as tmp:
            report = run_ci_assertion_battery(work_dir=Path(tmp) / "create-defaults")
        self.assertTrue(report["ok"], report.get("evidence"))
        steps = {
            item["id"]: item
            for item in report["punch_list"]["steps"]
        }
        self.assertIn("spinner_timing", steps)
        self.assertEqual(steps["spinner_timing"]["status"], "PASS")
        self.assertIn("agency_fee_default", steps)
        self.assertEqual(steps["agency_fee_default"]["status"], "PASS")
        payload = default_audit_payload(requested_by="Carlo Ferrara")
        self.assertEqual(payload["test_agency_fee"], "500")
        self.assertFalse(is_job_type_production_ready(JOB_TYPE))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            artifacts = str(Path(tmp) / "artifacts")
            store = JobStore(db)
            job = store.create_job(
                JOB_TYPE,
                {
                    **default_audit_payload(live=False, requested_by="Carlo Ferrara"),
                    "db_path": db,
                    "artifact_root": artifacts,
                    "line_of_business": "commercial auto",
                },
            )
            job["db_path"] = db
            result = AscendLocatorAuditWorker(artifact_root=artifacts).perform(
                job, idempotency_key=job["idempotency_key"]
            )
            self.assertTrue(result.succeeded, result.error)
            walked = {
                item["id"]: item
                for item in (result.detail or {}).get("punch_list", {}).get("steps", [])
            }
            self.assertEqual(walked["wait_programs_ready"]["observed"]["seconds"], 12.0)
            self.assertEqual(walked["new_program"]["observed"]["path"], "/create/new")
            self.assertEqual(walked["agency_fee"]["observed"]["default"], "$0.00")
            self.assertEqual(walked["agency_fee"]["observed"]["set_value"], "500")
            self.assertEqual(walked["producer_role"]["observed"]["producer_default"], ROBIE_AI)
            self.assertEqual(walked["producer_role"]["observed"]["set_value"], CARLO_FERRARA)
            self.assertIn("Import document", walked["import_document"]["observed"]["labels"])
            self.assertIn("Save program", walked["stop_before_save"]["observed"]["stop_before"])

    def test_contract_and_docs_name_n1_and_follow_tab(self):
        lines = ascend_new_program_contract_lines(
            "Open Ascend and create a program",
            {"requested_by": "Carlo Ferrara", "action_type": "hermes.google_chat_task"},
        )
        blob = "\n".join(lines)
        self.assertIn("seconds", blob.casefold())
        self.assertIn("/create/new", blob)
        self.assertIn("500", blob)
        self.assertIn("$0.00", blob)
        self.assertIn("Import document", blob)
        self.assertIn("Upload document", blob)
        self.assertIn("dropzone", blob.casefold())
        self.assertIn(CARLO_FERRARA, blob)
        state = Path("CURRENT_STATE.md").read_text(encoding="utf-8")
        release = Path("RELEASE_PROCESS.md").read_text(encoding="utf-8")
        skill = Path("skills/ascend-locator-artifact-audit/SKILL.md").read_text(
            encoding="utf-8"
        )
        for text in (state, release, skill):
            flat = " ".join(text.replace("**", "").split())
            self.assertIn("N=1", flat)
            self.assertIn("follow-tab", flat.casefold())
            self.assertIn("ascend-finance", text.casefold())
            self.assertIn("overwrite", text.casefold())
            self.assertIn("PAWIVA", text)
        self.assertIn("production_ready: false", skill)
        self.assertIn("Do not overwrite", skill)
        self.assertNotIn("production_ready: true", skill)


if __name__ == "__main__":
    unittest.main()
