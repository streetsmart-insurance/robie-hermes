"""Ascend locator + artifact audit: CI assertions only. No live Ascend/EZLynx."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.ascend_locator_audit import (
    CONCAT_SCENARIO_ID,
    FORBIDDEN_ACCOUNTS,
    JOB_TYPE,
    MINIMAL_PDF,
    PRODUCTION_EB96_ARTIFACT_ID,
    PRODUCTION_EB96_JOB_ID,
    PRODUCTION_EB96_WRONG_FOLDER,
    REPORT_KIND,
    SCENARIO_ID,
    STOP_BEFORE,
    ArtifactPathError,
    AscendLocatorAuditVerifier,
    AscendLocatorAuditWorker,
    FinanceCompleteError,
    ForbiddenAccountError,
    MissingQuotePdfError,
    PunchList,
    UniqueLocatorError,
    assert_artifact_path_matches_job_id,
    assert_quote_pdf_openable,
    classify_locator_failure,
    default_audit_payload,
    is_sliced_job_id_plus_artifact_id,
    looks_concatenated_job_id,
    refuse_complete_without_destination_evidence,
    refuse_finance_agreement_complete,
    refuse_forbidden_account,
    require_unique_locator,
    run_ci_assertion_battery,
    run_concat_job_id_eb96f620_scenario,
    save_and_lookup_quote_pdf,
    site_workflow_hold_reason,
    sliced_concat_job_folder,
    worker_lookup_artifact_dir,
    live_operator_idempotency_key,
)
from robie_job_engine.job_type_gate import is_job_type_production_ready
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import classify_request
from robie_job_engine.runtime_env import ProductionGuardError
from robie_job_engine.store import JobStore


class _CountTarget:
    def __init__(self, count: int, selector: str = "get_by_label('Insured')") -> None:
        self._count = count
        self._selector = selector

    def count(self) -> int:
        return self._count


class UniqueLocatorTests(unittest.TestCase):
    def test_strict_mode_violation_is_fail(self):
        step = classify_locator_failure(
            RuntimeError(
                "strict mode violation: get_by_role('button') resolved to 2 elements"
            ),
            locator='get_by_role("button", name="New program", exact=True)',
            step_id="new_program",
        )
        self.assertEqual(step.status, "FAIL")
        self.assertIn("strict mode violation", step.error or "")
        self.assertIn("New program", step.locator)

    def test_too_soon_zero_element_strict_mode_is_fail(self):
        step = classify_locator_failure(
            RuntimeError(
                'strict mode violation: locator resolved to 0 elements: '
                'get_by_role("button", name="Import document", exact=True)'
            ),
            locator='get_by_role("button", name="Import document", exact=True)',
            step_id="import_document",
        )
        self.assertEqual(step.status, "FAIL")
        self.assertIn("strict mode violation", step.error or "")
        self.assertIn("resolved to 0 elements", step.error or "")

    def test_positional_first_nth_last_is_fail(self):
        for locator in (
            'page.locator("button").first',
            'page.locator("input").nth(0)',
            'page.locator("div").last',
        ):
            step = classify_locator_failure(
                RuntimeError("matched"),
                locator=locator,
                step_id="positional",
            )
            self.assertEqual(step.status, "FAIL", locator)
            self.assertIn("positional", step.error or "")

    def test_timeout_error_is_fail(self):
        step = classify_locator_failure(
            TimeoutError("Timeout 30000ms exceeded"),
            locator='get_by_label("Agency Fee")',
            step_id="agency_fee",
        )
        self.assertEqual(step.status, "FAIL")
        self.assertIn("TimeoutError", step.error or "")

    def test_require_unique_locator_rejects_count_not_one(self):
        with self.assertRaises(UniqueLocatorError) as raised:
            require_unique_locator(_CountTarget(2))
        self.assertIn("strict mode violation", str(raised.exception))
        require_unique_locator(_CountTarget(1))

    def test_too_soon_zero_element_lookup_is_fail(self):
        with self.assertRaises(UniqueLocatorError) as raised:
            require_unique_locator(
                _CountTarget(0),
                locator='get_by_role("button", name="Import document", exact=True)',
            )
        text = str(raised.exception)
        self.assertIn("strict mode violation", text)
        self.assertIn("resolved to 0 elements", text)

    def test_no_gemini_and_no_positional_in_audit_source(self):
        source = Path("robie_job_engine/ascend_locator_audit.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("gemini_field_helper", source)
        self.assertNotIn("consult_gemini", source)
        self.assertIn("No Gemini", source)


class ArtifactPathTests(unittest.TestCase):
    def test_path_must_equal_job_id_not_concatenated(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp) / "artifacts"
            job_id = "11111111-2222-3333-4444-555555555555"
            good = root / job_id / "quote.pdf"
            good.parent.mkdir(parents=True)
            good.write_bytes(MINIMAL_PDF)
            assert_artifact_path_matches_job_id(
                good, job_id=job_id, artifact_root=root
            )
            concat = root / f"{job_id}{job_id}" / "quote.pdf"
            concat.parent.mkdir(parents=True)
            concat.write_bytes(MINIMAL_PDF)
            with self.assertRaises(ArtifactPathError) as raised:
                assert_artifact_path_matches_job_id(
                    concat, job_id=job_id, artifact_root=root
                )
            self.assertIn("concatenat", str(raised.exception).casefold())
            self.assertTrue(looks_concatenated_job_id(f"{job_id}{job_id}"))
            self.assertFalse(looks_concatenated_job_id(job_id))

    def test_eb96f620_sliced_job_id_plus_artifact_id_is_fail(self):
        job_id = PRODUCTION_EB96_JOB_ID
        artifact_id = PRODUCTION_EB96_ARTIFACT_ID
        wrong = sliced_concat_job_folder(job_id, artifact_id)
        self.assertEqual(wrong, PRODUCTION_EB96_WRONG_FOLDER)
        self.assertEqual(wrong, f"{job_id[:-11]}{artifact_id}")
        self.assertTrue(is_sliced_job_id_plus_artifact_id(wrong, job_id, artifact_id))
        self.assertTrue(looks_concatenated_job_id(wrong))
        self.assertFalse(is_sliced_job_id_plus_artifact_id(job_id, job_id, artifact_id))
        with durable_temporary_directory() as tmp:
            root = Path(tmp) / "artifacts"
            legal = worker_lookup_artifact_dir(root, job_id, artifact_id=artifact_id)
            self.assertEqual(legal, root / job_id)
            self.assertEqual(legal.name, job_id)
            wrong_pdf = root / wrong / f"{artifact_id}-quote.pdf"
            wrong_pdf.parent.mkdir(parents=True)
            wrong_pdf.write_bytes(MINIMAL_PDF)
            with self.assertRaises(ArtifactPathError) as raised:
                assert_artifact_path_matches_job_id(
                    wrong_pdf,
                    job_id=job_id,
                    artifact_root=root,
                    artifact_id=artifact_id,
                )
            message = str(raised.exception).casefold()
            self.assertTrue(
                "concatenat" in message or "job_id[:n]" in message or "full_job_id" in message,
                message,
            )

    def test_missing_pdf_after_save_is_fail(self):
        with durable_temporary_directory() as tmp:
            missing = Path(tmp) / "no-such.pdf"
            with self.assertRaises(MissingQuotePdfError) as raised:
                assert_quote_pdf_openable(missing)
            self.assertIn("missing PDF after save", str(raised.exception))
            with self.assertRaises(MissingQuotePdfError):
                assert_quote_pdf_openable(None)

    def test_chat_style_save_lookup_uses_job_id_folder(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            artifacts = str(Path(tmp) / "artifacts")
            store = JobStore(db)
            job = store.create_job(JOB_TYPE, default_audit_payload())
            record = save_and_lookup_quote_pdf(
                db_path=db,
                job_id=job["id"],
                artifact_root=artifacts,
            )
            stored = Path(record["stored_path"])
            self.assertEqual(stored.parent.name, job["id"])
            self.assertTrue(stored.read_bytes().startswith(b"%PDF"))
            assert_quote_pdf_openable(stored)


class CompleteAndReportTests(unittest.TestCase):
    def test_complete_never_allowed_without_destination_evidence(self):
        with self.assertRaises(PermissionError) as raised:
            refuse_complete_without_destination_evidence(
                expected={"ok": True},
                observed={"ok": True},
                locator=None,
                job_id="job-1",
            )
        self.assertIn("COMPLETE prohibited", str(raised.exception))

    def test_audit_job_is_a_report_not_finance_complete(self):
        with self.assertRaises(FinanceCompleteError):
            refuse_finance_agreement_complete(
                {"finance_agreement": True, "program_id": "prog"}
            )
        refuse_finance_agreement_complete(
            {"report_id": "rep-1", "kind": REPORT_KIND, "finance_agreement": False}
        )

    def test_verifier_rejects_finance_agreement_destination(self):
        verifier = AscendLocatorAuditVerifier()
        result = verifier.verify(
            {"payload": {}},
            {
                "destination": {
                    "report_id": "rep-1",
                    "kind": "finance_agreement",
                    "finance_agreement": True,
                    "program_id": "prog",
                    "stored_path": "/no/such/report.json",
                }
            },
        )
        self.assertFalse(result.verified)
        self.assertNotEqual(result.evidence.expected.get("kind"), "finance_agreement")


class WorkerAndGateTests(unittest.TestCase):
    def test_explicit_operator_run_id_creates_stable_distinct_key(self):
        first = live_operator_idempotency_key("20260828T1708Z-0a25586")
        self.assertEqual(first, live_operator_idempotency_key("20260828T1708Z-0a25586"))
        self.assertNotEqual(first, live_operator_idempotency_key("20260828T1709Z-0a25586"))
        self.assertIsNone(live_operator_idempotency_key(""))
        with self.assertRaises(ValueError):
            live_operator_idempotency_key("bad run/id")

    def test_job_type_is_not_production_ready(self):
        self.assertFalse(is_job_type_production_ready(JOB_TYPE))
        self.assertTrue(is_job_type_production_ready("ezlynx.commercial_auto"))
        reason = site_workflow_hold_reason("ascend", env="PRODUCTION", passing_audit_count=0)
        self.assertIsNotNone(reason)
        self.assertIn("3", reason or "")
        self.assertIn("not a free pass", (reason or "").casefold())
        self.assertIsNone(site_workflow_hold_reason("ascend", env="TEST"))

    def test_skill_is_not_production_ready_and_not_loom_finance(self):
        text = Path("skills/ascend-locator-artifact-audit/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("production_ready: false", text)
        self.assertIn(JOB_TYPE, text)
        self.assertIn("not", text.casefold())
        self.assertIn("ascend-finance", text)
        self.assertIn("Do not overwrite", text)
        self.assertNotIn("production_ready: true", text)
        self.assertIn("PR 35 battery", text)
        self.assertIn("automatic simulator", text)
        self.assertIn("do not call 36 the simulator", text)
        self.assertIn("Dusty walks the live site himself", text)
        self.assertIn("just trying it", text)
        self.assertIn("N=1", text)
        self.assertIn("follow-tab", text.casefold())
        self.assertIn("500", text)

    def test_classify_request_and_stop_before(self):
        self.assertEqual(
            classify_request("Run the Ascend locator-and-artifact-audit").action_type,
            JOB_TYPE,
        )
        self.assertNotEqual(
            classify_request("finish the Ascend finance agreement and bind").action_type,
            JOB_TYPE,
        )
        self.assertIn("Save program", STOP_BEFORE)
        self.assertIn("bind", STOP_BEFORE)
        self.assertIn("PAWIVA", FORBIDDEN_ACCOUNTS)

    def test_refuses_real_client_and_production_live(self):
        with self.assertRaises(ForbiddenAccountError):
            refuse_forbidden_account("PAWIVA commercial auto")
        with self.assertRaises(ForbiddenAccountError):
            refuse_forbidden_account("account 221398001")
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            worker = AscendLocatorAuditWorker()
            result = worker.perform(
                {
                    "id": "job-live",
                    "payload": {"live": True, "db_path": "x", "artifact_root": "y"},
                },
                idempotency_key="k",
            )
            self.assertFalse(result.succeeded)
            self.assertIn("Production", result.error or "")

    def test_worker_persists_report_not_finance_complete(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            artifacts = str(Path(tmp) / "artifacts")
            store = JobStore(db)
            job = store.create_job(
                JOB_TYPE,
                {
                    **default_audit_payload(live=False),
                    "worker": "ascend-locator-audit",
                    "db_path": db,
                    "artifact_root": artifacts,
                },
            )
            job["db_path"] = db
            result = AscendLocatorAuditWorker(artifact_root=artifacts).perform(
                job, idempotency_key=job["idempotency_key"]
            )
            self.assertTrue(result.succeeded)
            self.assertEqual(result.destination.get("kind"), REPORT_KIND)
            self.assertFalse(result.destination.get("finance_agreement"))
            self.assertNotEqual(store.get_job(job["id"])["status"], JobStatus.COMPLETE)
            stored = Path(result.destination["stored_path"])
            self.assertEqual(stored.parent.name, job["id"])
            punch = PunchList(
                job_id=job["id"],
                steps=[],
            )
            self.assertEqual(punch.overall, "FAIL")

    def test_named_eb96f620_scenario_fails_sliced_concat_and_passes_full_id(self):
        with durable_temporary_directory() as tmp:
            report = run_concat_job_id_eb96f620_scenario(work_dir=Path(tmp) / "eb96")
        self.assertEqual(report["id"], CONCAT_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))
        self.assertEqual(report["outcome"], "PASS")
        self.assertIn(PRODUCTION_EB96_JOB_ID, report["evidence"])
        self.assertIn(PRODUCTION_EB96_WRONG_FOLDER, report["evidence"])

    def test_ci_battery_scenario_passes_without_live_ascend(self):
        source = Path("robie_job_engine/ascend_locator_audit.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sync_playwright", source)
        runner = Path("robie_job_engine/ascend_locator_audit_runner.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from playwright.sync_api import sync_playwright", runner)
        battery = Path("robie_job_engine/regression_battery.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("dashboard.useascend.com", battery)
        with durable_temporary_directory() as tmp:
            report = run_ci_assertion_battery(work_dir=Path(tmp) / "ascend")
        self.assertEqual(report["id"], SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))
        self.assertEqual(report["outcome"], "PASS")
        self.assertFalse(report["finance_agreement"])

    def test_ci_refuses_live_hermes_path(self):
        with self.assertRaises(ProductionGuardError):
            run_ci_assertion_battery(
                work_dir=Path("/opt/streetsmart-hermes/robie-job-engine/data")
            )


if __name__ == "__main__":
    unittest.main()
