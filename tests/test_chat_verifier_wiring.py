"""Chat verifier wiring: browser.read jobs must verify, not auto-UNVERIFY.

Regression test for the Chat path historically registering no verifiers, so
every browser job fell to UNVERIFIED with "no independent verifier
registered" even when the work succeeded. ``_default_chat_verifiers()`` must
include the read-only browser verifier, and a Chat-driven browser.read job
must be able to reach COMPLETE through ``guard_chat_response`` without the
caller passing explicit verifiers.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine import chat_guard
from robie_job_engine.browser_read import BrowserReadVerifier
from robie_job_engine.message_verification import MessageOutcomeVerifier
from robie_job_engine.chat_ezlynx_destination_verifier import HermesChatEzlynxDestinationVerifier
from robie_job_engine.chat_guard import _default_chat_verifiers, guard_chat_response, open_chat_job
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


class _FakeReadPort:
    """Stand-in for the CDP read port; returns a canned fresh snapshot."""

    def __init__(self, snapshot):
        self.snapshot = dict(snapshot)
        self.calls = 0

    def read_fresh(self, locator):
        self.calls += 1
        return dict(self.snapshot)


class ChatVerifierWiringTests(unittest.TestCase):
    def test_default_chat_verifiers_include_browser_read(self):
        verifiers = _default_chat_verifiers()
        self.assertIn("browser.read", verifiers)
        self.assertIsInstance(verifiers["browser.read"], BrowserReadVerifier)

    def test_default_chat_verifiers_include_google_chat_task(self):
        verifiers = _default_chat_verifiers()
        self.assertIn("hermes.google_chat_task", verifiers)
        self.assertIsInstance(verifiers["hermes.google_chat_task"], MessageOutcomeVerifier)
        self.assertIsInstance(verifiers["hermes.google_chat_task"].reader, HermesChatEzlynxDestinationVerifier)

    def test_browser_read_chat_job_verifies_without_explicit_verifiers(self):
        url = "https://example.com/policy/1"
        snapshot = {"url": url, "title": "Policy 1", "engine": "playwright"}
        fake = BrowserReadVerifier(_FakeReadPort(snapshot))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "m-read-1",
                f"read only verify the mortgagee at {url}",
                action_payload={"expected": {"url": url, "title": "Policy 1"}},
            )
            store = JobStore(db)
            job = store.get_job(job_id)
            self.assertEqual(job["action_type"], "browser.read")
            store.checkpoint(
                job_id,
                "action",
                {
                    "action": "browser.read",
                    "destination": {"url": url, "title": "Policy 1"},
                },
            )
            with patch.object(
                chat_guard, "_default_chat_verifiers", return_value={"browser.read": fake}
            ):
                guard_chat_response(
                    db, job_id, "The mortgagee clause was verified on screen."
                )
            final = store.get_job(job_id)
            self.assertEqual(
                fake_verifier_calls(fake),
                1,
                "the registered verifier must actually run",
            )
            self.assertNotIn(
                "no independent verifier registered",
                str(final.get("last_error") or ""),
            )
            self.assertEqual(final["status"], JobStatus.COMPLETE)

    def test_browser_read_chat_job_fails_closed_on_mismatch(self):
        url = "https://example.com/policy/1"
        # Destination drifted: title no longer matches expected.
        snapshot = {"url": url, "title": "Policy 999", "engine": "playwright"}
        fake = BrowserReadVerifier(_FakeReadPort(snapshot))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "m-read-2",
                f"read only verify the mortgagee at {url}",
                action_payload={"expected": {"url": url, "title": "Policy 1"}},
            )
            store = JobStore(db)
            store.checkpoint(
                job_id,
                "action",
                {
                    "action": "browser.read",
                    "destination": {"url": url, "title": "Policy 1"},
                },
            )
            with patch.object(
                chat_guard, "_default_chat_verifiers", return_value={"browser.read": fake}
            ):
                guard_chat_response(
                    db, job_id, "The mortgagee clause was verified on screen."
                )
            final = store.get_job(job_id)
            self.assertNotEqual(final["status"], JobStatus.COMPLETE)


class ChatEzlynxDestinationWiringTests(unittest.TestCase):
    def test_bind_runs_before_checkpoint_read_and_can_complete(self):
        applicant = "194066748"
        policy = "73834086"
        url = f"https://app.uatezlynx.com/web/account/{applicant}/overview"

        class Port:
            def policy_by_number(self, policy_number):
                return {
                    "status": "success",
                    "data": {
                        "Policies": [
                            {"PolicyNumber": policy_number, "ApplicantId": applicant}
                        ]
                    },
                }

            def documents_for_applicant(self, applicant_id, policy_id=0):
                return []

            def download_document(self, document_id):
                return b"%PDF-1.4 test"

            def discussions_for_applicant(self, applicant_id):
                return []

        fake = HermesChatEzlynxDestinationVerifier(Port())
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "set up the Gold Eagle Bond"},
            )
            job_id = job["id"]
            store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.add_playwright_exec(
                job_id,
                "playwright_exec",
                "ok",
                code_preview=f'page.goto("{url}")',
                result={"url": url},
            )
            self.assertIsNone(store.get_checkpoint(job_id, "action"))
            with patch.object(
                chat_guard,
                "_default_chat_verifiers",
                return_value={"hermes.google_chat_task": fake},
            ):
                guard_chat_response(
                    db,
                    job_id,
                    f"Created Western Surety policy {policy} and posted a Bond note.",
                )
            action = store.get_checkpoint(job_id, "action")
            self.assertIsNotNone(action)
            self.assertTrue(action["detail"]["this_is_a_claim_not_evidence"])
            self.assertEqual(action["destination"]["applicant_id"], applicant)
            self.assertEqual(action["destination"]["policy_number"], policy)
            self.assertEqual(
                action["detail"]["provenance"]["applicant_id"],
                "derived_from_playwright_exec",
            )
            self.assertEqual(
                action["detail"]["provenance"]["policy_number"],
                "claimed_by_worker",
            )
            final = store.get_job(job_id)
            self.assertEqual(final["status"], JobStatus.COMPLETE)
            self.assertEqual(final["payload"]["applicant_id"], applicant)


def fake_verifier_calls(verifier):
    return verifier.port.calls


if __name__ == "__main__":
    unittest.main()
