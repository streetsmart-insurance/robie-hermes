from __future__ import annotations

import hashlib
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

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

    def test_unknown_action_without_structured_verifier_remains_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "unknown-1", "Perform a custom external action")
            response = guard_chat_response(db, job_id, "Done")
            self.assertIn("UNVERIFIED", response)
            self.assertIn("structured destination", JobStore(db).get_job(job_id)["last_error"])

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


if __name__ == "__main__":
    unittest.main()
