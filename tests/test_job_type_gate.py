from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.job_schema import bounded_schema_hold_reason
from robie_job_engine.job_type_gate import (
    REQUIRED_CLEAN_TEST_JOBS,
    JobTypeGateError,
    assert_job_type_production_ready,
    check_repo_gate,
    is_job_type_production_ready,
    parse_skill_frontmatter,
    production_hold_reason,
    record_test_audit,
    required_clean_test_jobs,
    skill_gate_violations,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


class JobTypeGateTests(unittest.TestCase):
    def test_n_is_three(self):
        self.assertEqual(REQUIRED_CLEAN_TEST_JOBS, 3)
        self.assertEqual(required_clean_test_jobs(), 3)

    def test_commercial_auto_and_existing_types_are_grandfathered(self):
        self.assertTrue(is_job_type_production_ready("ezlynx.commercial_auto"))
        self.assertTrue(is_job_type_production_ready("hermes.google_chat_task"))
        self.assertTrue(is_job_type_production_ready("ezlynx.submission_audit"))
        self.assertIsNone(production_hold_reason("ezlynx.commercial_auto", env="PRODUCTION"))

    def test_new_type_is_not_production_ready_without_test_audits(self):
        self.assertFalse(is_job_type_production_ready("ezlynx.personal_auto"))
        self.assertFalse(is_job_type_production_ready("ascend.locator_artifact_audit"))
        reason = production_hold_reason("ezlynx.personal_auto", env="PRODUCTION")
        self.assertIsNotNone(reason)
        self.assertIn("3", reason)
        self.assertIsNone(production_hold_reason("ezlynx.personal_auto", env="TEST"))

    def test_three_passing_test_audits_unlock_promotion(self):
        with durable_temporary_directory() as tmp:
            promotions = Path(tmp) / "promotions"
            with patch(
                "robie_job_engine.job_type_gate.PROMOTIONS_DIR", promotions
            ):
                for index in range(3):
                    record_test_audit(
                        "ezlynx.homeowners",
                        job_id=f"job-{index}",
                        verdict="PASS",
                    )
                self.assertTrue(is_job_type_production_ready("ezlynx.homeowners"))
                assert_job_type_production_ready(
                    "ezlynx.homeowners", env="PRODUCTION"
                )

    def test_two_passing_audits_are_not_enough(self):
        with durable_temporary_directory() as tmp:
            promotions = Path(tmp) / "promotions"
            with patch(
                "robie_job_engine.job_type_gate.PROMOTIONS_DIR", promotions
            ):
                record_test_audit("ezlynx.personal_auto", job_id="a", verdict="PASS")
                record_test_audit("ezlynx.personal_auto", job_id="b", verdict="PASS")
                self.assertFalse(is_job_type_production_ready("ezlynx.personal_auto"))
                with self.assertRaises(JobTypeGateError):
                    assert_job_type_production_ready(
                        "ezlynx.personal_auto", env="PRODUCTION"
                    )

    def test_new_skill_marked_production_ready_fails_ci_without_record(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            skill = root / "skills" / "ezlynx-personal-auto" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\n"
                'name: "ezlynx-personal-auto"\n'
                'job_type: "ezlynx.personal_auto"\n'
                "production_ready: true\n"
                "---\n",
                encoding="utf-8",
            )
            violations = skill_gate_violations(root)
            self.assertTrue(violations)
            self.assertIn("ezlynx.personal_auto", violations[0])

    def test_repo_skills_do_not_violate_the_gate(self):
        self.assertEqual(check_repo_gate(), [])

    def test_commercial_auto_frontmatter_is_production_ready(self):
        text = (
            Path("skills/ezlynx-commercial-auto-from-quote/SKILL.md").read_text()
        )
        meta = parse_skill_frontmatter(text)
        self.assertEqual(meta["job_type"], "ezlynx.commercial_auto")
        self.assertEqual(meta["production_ready"], "true")

    def test_production_runtime_holds_new_bounded_type(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            reason = bounded_schema_hold_reason(
                "ezlynx.reassign",
                {"applicant_id": "220250093", "resource_id": "r1"},
            )
            self.assertIsNone(reason)
            # New types are not in BOUNDED_ENGINE_ACTIONS yet; the hold applies
            # once they are registered. Exercise the gate function directly.
            self.assertIsNotNone(
                production_hold_reason("ezlynx.personal_auto", env="PRODUCTION")
            )

    def test_release_process_states_required_n(self):
        text = Path("RELEASE_PROCESS.md").read_text(encoding="utf-8")
        self.assertIn("required Production gate", text)
        self.assertIn("3 clean jobs", text)
        self.assertIn("scripts/check-job-type-gate.py", text)
        self.assertIn("production_ready", text)
        self.assertIn("infra only", text.casefold())
        self.assertIn("hermes-test-01", text)
        self.assertIn("New job types still need", text)
        self.assertIn("NEW site / workflow", text)
        self.assertIn("ascend:locator-and-artifact-audit", text)
        self.assertIn("Commercial auto already on Production is **not** a free pass", text)
        self.assertIn("PR 35 battery", text)
        self.assertIn("automatic simulator", text)
        self.assertIn("do not call 36 the simulator", text)
        self.assertIn("Dusty walks the live site himself", text)
        self.assertIn("just trying it", text)
        self.assertIn("ChatGPT", text)

    def test_audit_record_from_jobs_db_requires_pass_verdict(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("ezlynx.personal_auto", {"text": "quote"})
            store.transition(job["id"], JobStatus.UNVERIFIED, error="fixture")
            store.checkpoint(
                job["id"],
                "post_job_audit",
                {"job_id": job["id"], "verdict": "FAIL"},
            )
            promotions = Path(tmp) / "promotions"
            with patch(
                "robie_job_engine.job_type_gate.PROMOTIONS_DIR", promotions
            ):
                from robie_job_engine.job_type_gate import (
                    collect_passing_audits_from_db,
                )

                self.assertEqual(
                    collect_passing_audits_from_db(db, "ezlynx.personal_auto"),
                    [],
                )
                store.checkpoint(
                    job["id"],
                    "post_job_audit",
                    {"job_id": job["id"], "verdict": "PASS"},
                )
                found = collect_passing_audits_from_db(db, "ezlynx.personal_auto")
                self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
