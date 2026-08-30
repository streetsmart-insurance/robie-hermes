from __future__ import annotations

import hashlib
import os
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.chat_verifiers import (
    EzlynxSubmissionAuditVerifier,
    FilesystemSkillUpdateVerifier,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import classify_request
from robie_job_engine.store import JobStore


class _SubmissionReadback:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def fresh_authenticated_structured_read(self, scope):
        self.calls += 1
        return dict(self.result)


class ChatVerifierRoutingTests(unittest.TestCase):
    def test_skill_file_update_completes_only_after_exact_fresh_reread(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            skill = root / "skills" / "policy" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            expected_content = "---\nname: policy\n---\nUse the exact destination.\n"
            digest = hashlib.sha256(expected_content.encode()).hexdigest()
            db = str(root / "jobs.db")
            job_id = open_chat_job(
                db,
                "skill-update-1",
                f"Update skill file {skill}",
                action_payload={
                    "target_path": str(skill),
                    "expected_content": expected_content,
                    "expected_sha256": digest,
                },
            )
            store = JobStore(db)
            written_at = datetime.now(timezone.utc).isoformat()
            skill.write_text(expected_content, encoding="utf-8")
            store.checkpoint(job_id, "action", {
                "action": "filesystem.skill_update",
                "destination": {
                    "target_path": str(skill),
                    "expected_content": expected_content,
                    "expected_sha256": digest,
                },
                "detail": {"written_at": written_at},
            })
            response = guard_chat_response(
                db,
                job_id,
                "The skill was updated.",
                verifiers={
                    "filesystem.skill_update": FilesystemSkillUpdateVerifier([root / "skills"])
                },
            )
            self.assertNotIn("UNVERIFIED", response)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE)
            skill.write_text("tampered\n", encoding="utf-8")
            second = open_chat_job(
                db,
                "skill-update-2",
                f"Update skill file {skill}",
                action_payload={
                    "target_path": str(skill),
                    "expected_content": expected_content,
                    "expected_sha256": digest,
                },
            )
            store.checkpoint(second, "action", {
                "action": "filesystem.skill_update",
                "destination": {
                    "target_path": str(skill),
                    "expected_content": expected_content,
                    "expected_sha256": digest,
                },
                "detail": {"written_at": written_at},
            })
            guard_chat_response(
                db,
                second,
                "The skill was updated.",
                verifiers={
                    "filesystem.skill_update": FilesystemSkillUpdateVerifier([root / "skills"])
                },
            )
            self.assertEqual(store.get_job(second)["status"], JobStatus.UNVERIFIED)

    def test_submission_audit_completes_from_fresh_authenticated_playwright_evidence(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            scope = {"producer": "Jake", "age_days": 30}
            postcondition = {"overdue_count": 4}
            job_id = open_chat_job(
                db,
                "submission-audit-1",
                "Audit the EZLynx Submission Center overdue items",
                action_payload={
                    "resource_id": "submission-center:jake",
                    "scope": scope,
                    "expected_postcondition": postcondition,
                },
            )
            store = JobStore(db)
            store.checkpoint(job_id, "action", {
                "action": "ezlynx.submission_audit",
                "destination": {
                    "resource_id": "submission-center:jake",
                    "scope": scope,
                    "expected_postcondition": postcondition,
                },
                "detail": {},
            })
            readback = _SubmissionReadback({
                "resource_id": "submission-center:jake",
                "authenticated": True,
                "scope": scope,
                "postcondition": postcondition,
                "engine": "playwright",
                "fresh_navigation": True,
            })
            response = guard_chat_response(
                db,
                job_id,
                "Four overdue items were found.",
                verifiers={
                    "ezlynx.submission_audit": EzlynxSubmissionAuditVerifier(readback)
                },
            )
            self.assertNotIn("UNVERIFIED", response)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE)
            self.assertEqual(readback.calls, 1)

    def test_submission_audit_chat_reports_the_verified_page_state(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            scope = {
                "time_frame": "All Submissions",
                "assigned_producer": "Streetsmart Insurance",
                "my_submissions": False,
            }
            postcondition = {
                "pager_total": 4751,
                "mat_row_count": 100,
                "status_aria_sort": "ascending",
                "first_row_status": "In Progress",
                "first_closed_row_index": 56,
                "rows_inspected_through_boundary": 57,
                "distinct_non_closed_statuses": ["In Progress", "Submitted"],
            }
            job_id = open_chat_job(
                db,
                "submission-audit-summary",
                "Run a read-only Submission Center audit. Do not modify records or send emails.",
                action_payload={
                    "resource_id": "ezlynx:submission-center",
                    "scope": scope,
                    "expected_postcondition": postcondition,
                },
            )
            store = JobStore(db)
            store.checkpoint(job_id, "action", {
                "action": "ezlynx.submission_audit",
                "destination": {
                    "resource_id": "ezlynx:submission-center",
                    "scope": scope,
                    "expected_postcondition": postcondition,
                },
                "detail": {},
            })
            readback = _SubmissionReadback({
                "resource_id": "ezlynx:submission-center",
                "authenticated": True,
                "scope": scope,
                "postcondition": postcondition,
                "engine": "playwright",
                "fresh_navigation": True,
            })
            response = guard_chat_response(
                db,
                job_id,
                "generic worker prose",
                verifiers={
                    "ezlynx.submission_audit": EzlynxSubmissionAuditVerifier(readback)
                },
            )
            self.assertIn("Live pager total: 4751", response)
            self.assertIn("First closed row: row 57", response)
            self.assertIn("Record changes: none", response)
            self.assertIn("Emails sent: none", response)
            self.assertNotIn("generic worker prose", response)

    def test_complete_is_failed_when_control_center_publication_fails(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            scope = {"producer": "Jake"}
            postcondition = {"overdue_count": 4}
            job_id = open_chat_job(
                db,
                "submission-audit-publication-failure",
                "Audit the EZLynx Submission Center overdue items",
                action_payload={
                    "resource_id": "submission-center:jake",
                    "scope": scope,
                    "expected_postcondition": postcondition,
                },
            )
            store = JobStore(db)
            store.checkpoint(job_id, "action", {
                "action": "ezlynx.submission_audit",
                "destination": {
                    "resource_id": "submission-center:jake",
                    "scope": scope,
                    "expected_postcondition": postcondition,
                },
                "detail": {},
            })
            readback = _SubmissionReadback({
                "resource_id": "submission-center:jake",
                "authenticated": True,
                "scope": scope,
                "postcondition": postcondition,
                "engine": "playwright",
                "fresh_navigation": True,
            })
            with patch.dict(os.environ, {"ROBIE_DASHBOARD_SHEET_ID": "sheet"}), patch(
                "robie_job_engine.chat_guard.publish_job_to_control_center",
                side_effect=RuntimeError("sheet write rejected"),
            ):
                response = guard_chat_response(
                    db,
                    job_id,
                    "Four overdue items were found.",
                    verifiers={
                        "ezlynx.submission_audit": EzlynxSubmissionAuditVerifier(readback)
                    },
                )
            self.assertIn("FAILED", response)
            self.assertIn("Control Center publication failed", response)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.FAILED)

    def test_complete_mentions_control_center_only_after_exact_publication(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            expected_content = "verified content\n"
            target = Path(tmp) / "skills" / "policy" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text(expected_content, encoding="utf-8")
            digest = hashlib.sha256(expected_content.encode()).hexdigest()
            job_id = open_chat_job(
                db,
                "published-skill-update",
                f"Update skill file {target}",
                action_payload={
                    "target_path": str(target),
                    "expected_content": expected_content,
                    "expected_sha256": digest,
                },
            )
            store = JobStore(db)
            store.checkpoint(job_id, "action", {
                "action": "filesystem.skill_update",
                "destination": {
                    "target_path": str(target),
                    "expected_content": expected_content,
                    "expected_sha256": digest,
                },
                "detail": {"written_at": datetime.now(timezone.utc).isoformat()},
            })
            publication = {
                "job_id": job_id,
                "sheet_row": 7,
                "evidence_rows": 1,
                "recording": "Segment 1: https://drive.google.com/file/d/test/view",
                "status": "Complete — independently verified",
            }
            with patch.dict(os.environ, {"ROBIE_DASHBOARD_SHEET_ID": "sheet"}), patch(
                "robie_job_engine.chat_guard.publish_job_to_control_center",
                return_value=publication,
            ):
                response = guard_chat_response(
                    db,
                    job_id,
                    "Updated.",
                    verifiers={
                        "filesystem.skill_update": FilesystemSkillUpdateVerifier([Path(tmp) / "skills"])
                    },
                )
            self.assertIn("Control Center row was reread successfully", response)
            self.assertEqual(
                store.get_checkpoint(job_id, "control_center_publication"), publication
            )

    def test_unknown_action_without_structured_verifier_remains_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "unknown-1", "Perform a custom external action")
            response = guard_chat_response(db, job_id, "Done")
            self.assertIn("UNVERIFIED", response)
            self.assertIn("structured destination", JobStore(db).get_job(job_id)["last_error"])

    def test_progress_wrapper_does_not_force_premature_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "audit-progress-1",
                "Audit the EZLynx Submission Center overdue list",
            )
            response = guard_chat_response(
                db,
                job_id,
                "Working — 9 min — playwright_exec",
            )
            self.assertIn("RUNNING", response)
            self.assertEqual(JobStore(db).get_job(job_id)["status"], JobStatus.PENDING)
            self.assertIsNone(JobStore(db).get_checkpoint(job_id, "action"))

    def test_submission_audit_intake_has_server_owned_read_only_scope(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "audit-scope-1",
                "Audit the EZLynx Submission Center overdue list",
            )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["action_type"], "ezlynx.submission_audit")
            self.assertEqual(job["payload"]["scope"]["page_size"], 100)
            self.assertFalse(job["payload"]["scope"]["my_submissions"])
            self.assertTrue(job["payload"]["read_only"])

    def test_ordinary_questions_and_followups_create_no_executable_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            for index, message in enumerate((
                "What is a submission?",
                "How does this work?",
                "Is this ready?",
                "yes",
                "sure",
            )):
                self.assertIsNone(open_chat_job(db, f"ordinary-{index}", message))
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_destination_specific_classification(self):
        self.assertEqual(
            classify_request("Update the policy SKILL.md file").action_type,
            "filesystem.skill_update",
        )
        self.assertEqual(
            classify_request("Audit the EZLynx Submission Center overdue list").action_type,
            "ezlynx.submission_audit",
        )
        self.assertEqual(
            classify_request("Create a program in Ascend").action_type,
            "ascend.create_program",
        )
        self.assertEqual(
            classify_request(
                "Run a read-only Submission Center audit. "
                "Do not modify records or send emails."
            ).action_type,
            "ezlynx.submission_audit",
        )
        self.assertNotEqual(
            classify_request(
                "Audit the Submission Center, but send the producer an email."
            ).action_type,
            "ezlynx.submission_audit",
        )

    def test_ascend_create_program_chat_is_api_only(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "ascend-api-routing",
                "Create a program in Ascend",
                action_payload={
                    "program": {
                        "insured_id": "00000000-0000-0000-0000-000000000001",
                        "producer_id": "00000000-0000-0000-0000-000000000002",
                        "account_manager_id": "00000000-0000-0000-0000-000000000003",
                    },
                    "billables": [
                        {
                            "billable_identifier": "TEST-API-1",
                            "carrier_identifier": "progressive",
                            "coverage_identifier": "commercial_auto",
                            "effective_date": "2026-09-01",
                            "expiration_date": "2027-09-01",
                            "premium_cents": 100000,
                        }
                    ],
                },
            )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["action_type"], "ascend.create_program")
            self.assertEqual(job["payload"]["worker"], "ascend-api")
            self.assertTrue(job["payload"]["execute"])
            self.assertTrue(job["payload"]["api_only"])
            self.assertNotIn("programs_url", job["payload"])


if __name__ == "__main__":
    unittest.main()
