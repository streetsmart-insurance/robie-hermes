"""Regressions for wrong-policy Playwright writes and false COMPLETE.

No live EZLynx. No real customer, policy, coverage, or payment data.
Production release stays FAIL. live_test_complete stays false.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.complete_guard import (
    destination_identity_missing,
    expected_postcondition_missing,
    postcondition_mismatch,
    require_complete_postcondition,
)
from robie_job_engine.engine import JobEngine
from robie_job_engine.models import VERIFIER_AUTHORITY, JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from robie_job_engine.playwright_write_guard import (
    install_playwright_write_guards,
    locator_is_positional_guess,
    require_unique_write_target,
    unique_write_block_reason,
)
from robie_job_engine.release_gate import production_release_decision
from robie_job_engine.store import JobStore


ROOT = Path(__file__).resolve().parents[1]
ENGINE_GUARD = ROOT / "robie_job_engine" / "playwright_write_guard.py"
OVERLAY_GUARD = ROOT / "deploy" / "hermes" / "tools" / "playwright_write_guard.py"


class FakeLocator:
    def __init__(self, matches: int, selector: str):
        self.matches = matches
        self.selector = selector
        self.fills: list[str] = []

    def count(self):
        return self.matches

    @property
    def first(self):
        return FakeLocator(1, f"{self.selector} >> nth=0")

    def nth(self, index: int):
        return FakeLocator(1, f"{self.selector} >> nth={index}")

    def fill(self, value: str):
        require_unique_write_target(self)
        self.fills.append(value)


class FakePage:
    def __init__(self, fields: dict[str, FakeLocator]):
        self.fields = fields

    def locator(self, selector: str):
        return self.fields.get(selector, FakeLocator(0, selector))

    def fill(self, selector: str, value: str):
        require_unique_write_target(self, selector=selector)
        self.locator(selector).fill(value)


class FakePlaywrightLocator:
    fill = FakeLocator.fill


class PlaywrightPolicyFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_overlay_write_guard_matches_job_engine_source(self):
        self.assertEqual(ENGINE_GUARD.read_text(), OVERLAY_GUARD.read_text())

    def test_positional_first_fill_fails_closed_and_does_not_write(self):
        effective_dates = FakeLocator(2, "textbox:effective-date")
        self.assertTrue(locator_is_positional_guess(effective_dates.first))
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
            effective_dates.first.fill("2026-09-01")
        self.assertEqual(effective_dates.fills, [])
        self.assertEqual(effective_dates.first.fills, [])

    def test_ambiguous_label_fill_fails_closed_instead_of_guessing(self):
        name_fields = FakeLocator(2, "textbox:name")
        reason = unique_write_block_reason(name_fields)
        self.assertIn("matched 2 fields", reason)
        with self.assertRaisesRegex(RuntimeError, "refuse to guess"):
            name_fields.fill("WRONG-INSURED")
        self.assertEqual(name_fields.fills, [])

    def test_missing_field_fill_fails_closed(self):
        missing = FakeLocator(0, "textbox:policy-number")
        with self.assertRaisesRegex(RuntimeError, "matched 0 fields"):
            missing.fill("TEST-POLICY-A")
        self.assertEqual(missing.fills, [])

    def test_unique_labeled_field_may_write(self):
        policy_number = FakeLocator(1, "textbox:policy-number")
        policy_number.fill("TEST-POLICY-A")
        self.assertEqual(policy_number.fills, ["TEST-POLICY-A"])

    def test_page_fill_uses_the_same_unique_identity_rule(self):
        page = FakePage(
            {
                "input.name": FakeLocator(2, "input.name"),
                "input.policy-number": FakeLocator(1, "input.policy-number"),
            }
        )
        with self.assertRaisesRegex(RuntimeError, "matched 2 fields"):
            page.fill("input.name", "WRONG-INSURED")
        page.fill("input.policy-number", "TEST-POLICY-A")
        self.assertEqual(page.locator("input.policy-number").fills, ["TEST-POLICY-A"])

    def test_installed_guard_patches_fill_before_user_code_can_guess(self):
        scope = {"Locator": FakePlaywrightLocator}
        install_playwright_write_guards(scope)
        self.assertTrue(scope["_robie_unique_write_guard"])
        target = FakeLocator(2, "input.fein")
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
            FakePlaywrightLocator.fill(target, "12-3456789")
        self.assertEqual(target.fills, [])

    def test_ok_only_postcondition_cannot_complete(self):
        self.assertIn("success flag", expected_postcondition_missing({"ok": True}))
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "text": "set up the policy"},
            idempotency_key="ok-only",
        )
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.checkpoint(
            job["id"],
            "action",
            {"action": "hermes.google_chat_task", "destination": {"record_id": "acct-test"}},
        )
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {"ok": True},
                {"ok": True},
                True,
                datetime.now(timezone.utc).isoformat(),
                "acct-test",
            ),
        )
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.assertNotEqual(self.store.get_job(job["id"])["status"], JobStatus.COMPLETE)

    def test_complete_requires_intended_record_identity(self):
        self.assertIn(
            "action target identity is missing",
            destination_identity_missing(
                locator="page",
                expected={"status": "done"},
                observed={"status": "done"},
                intended=None,
            ),
        )
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua"},
            idempotency_key="no-intended",
        )
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {"status": "done"},
                {"status": "done"},
                True,
                datetime.now(timezone.utc).isoformat(),
                "page",
            ),
        )
        with self.assertRaisesRegex(PermissionError, "action target identity is missing"):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )

    def test_wrong_policy_record_cannot_complete(self):
        expected = {
            "record_id": "acct-test",
            "policy_number": "TEST-POLICY-A",
            "status": "Quoted",
        }
        observed = {
            "record_id": "acct-test",
            "policy_number": "TEST-POLICY-B",
            "status": "Quoted",
        }
        self.assertIn("policy_number", postcondition_mismatch(expected, observed) or "")
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "record_id": "acct-test"},
            idempotency_key="wrong-policy",
        )

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "hermes.google_chat_task", {"record_id": "acct-test"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "FRESH_PAGE_READBACK",
                        "destination",
                        expected,
                        observed,
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        "acct-test",
                    ),
                )

        final = JobEngine(
            self.store,
            {"hermes-cua": Worker()},
            {"hermes.google_chat_task": Verifier()},
        ).run(job["id"])
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)
        self.assertIn("policy_number", str(final.get("last_error") or ""))

    def test_nothing_happened_cannot_complete_from_worker_receipt(self):
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "record_id": "acct-test"},
            idempotency_key="nothing-happened",
        )

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "hermes.google_chat_task", {"record_id": "acct-test"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "FRESH_PAGE_READBACK",
                        "destination",
                        {"record_id": "acct-test", "status": "Quoted"},
                        {"record_id": "acct-test"},
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        "acct-test",
                    ),
                )

        final = JobEngine(
            self.store,
            {"hermes-cua": Worker()},
            {"hermes.google_chat_task": Verifier()},
        ).run(job["id"])
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)

    def test_chat_policy_setup_claim_without_evidence_is_not_success(self):
        job_id = open_chat_job(
            str(self.store.path),
            "spaces/s/messages/policy-setup",
            "Please set up the insurance policy in EZLynx",
        )
        self.assertIsNotNone(job_id)
        response = guard_chat_response(
            str(self.store.path),
            job_id,
            "I set up the policy. The work is done.",
        )
        self.assertIn("UNVERIFIED", response)
        self.assertNotIn("— COMPLETE", response)
        self.assertNotIn("I set up the policy", response)
        self.assertIn("suppressed", response)
        self.assertEqual(self.store.get_job(job_id)["status"], JobStatus.UNVERIFIED)

    def test_unbound_policy_success_claim_is_not_reported(self):
        response = guard_chat_response(
            str(self.store.path),
            None,
            "I set up the policy in EZLynx.",
        )
        self.assertIn("not treating this as successful", response)
        self.assertNotIn("I set up the policy", response)

    def test_matching_intended_record_still_completes(self):
        expected = {
            "record_id": "acct-test",
            "policy_number": "TEST-POLICY-A",
            "status": "Quoted",
        }
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "record_id": "acct-test"},
            idempotency_key="right-policy",
        )

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "hermes.google_chat_task", {"record_id": "acct-test"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "FRESH_PAGE_READBACK",
                        "destination",
                        expected,
                        dict(expected),
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        "acct-test",
                    ),
                )

        final = JobEngine(
            self.store,
            {"hermes-cua": Worker()},
            {"hermes.google_chat_task": Verifier()},
        ).run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)

    def test_require_complete_postcondition_rejects_ok_only_even_with_authority(self):
        with self.assertRaises(PermissionError):
            require_complete_postcondition(
                current=JobStatus.VERIFYING,
                authority=VERIFIER_AUTHORITY,
                verified=True,
                authoritative=True,
                expected={"ok": True},
                observed={"ok": True},
                captured_at=datetime.now(timezone.utc).isoformat(),
                evidence_ref="sha",
                locator="acct-test",
                job_id="job-1",
                verifier_authority=VERIFIER_AUTHORITY,
                intended="acct-test",
            )

    def test_production_release_stays_fail(self):
        self.assertEqual(production_release_decision(), "FAIL")
        self.assertEqual(
            production_release_decision(independent_reviewer_pass=True), "FAIL"
        )


if __name__ == "__main__":
    unittest.main()
