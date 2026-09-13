"""HITL Gmail replies resume the waiting job and apply Coverage A–F.

Live miss: Carlo replied on [ROBIE HITL] Job 28bff7c8 with A–D and F
(no E). Inbox created hermes.email_task a8068d3d instead of resuming
28bff7c8. Coverages never applied. Mocks only. No live EZLynx.
No Production. Do not invent Coverage E.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.email_guard import run_guarded_email_task
from robie_job_engine.email_hitl import (
    apply_hitl_coverage_fill,
    find_parked_email_hitl_job,
    ingest_email_hitl_reply,
    missing_coverage_letters,
    parse_hitl_job_token,
    policy_setup_args_from_hitl_payload,
)
from robie_job_engine.hitl_copy import missing_coverage_letters_human_text
from robie_job_engine.models import JobStatus
from robie_job_engine.policy_setup_dispatch import parse_coverage_amounts_from_reply
from robie_job_engine.store import JobStore

CARLO_REPLY = (
    "Coverage A $1,200,000; B $120,000; C $500,000; D $500,000; F $10,000"
)


def _intake_count(store: JobStore) -> int:
    with store.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM job_intake").fetchone()[0])


def _job_count(store: JobStore) -> int:
    with store.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


def _park_coverage_hitl(store: JobStore) -> dict:
    job = store.create_job(
        "hermes.email_task",
        {
            "worker": "hermes-cua",
            "gmail_message_id": "orig-28bff7c8",
            "prompt": "create homeowners TEST-HO-20260911-E01 on 220250093",
            "request_text": "create homeowners TEST-HO-20260911-E01",
            "policy_number": "TEST-HO-20260911-E01",
            "policy_id": "83669533",
            "applicant_id": "220250093",
            "effective_date": "10/02/2026",
            "expiration_date": "10/02/2027",
        },
        idempotency_key="gmail:orig-28bff7c8",
    )
    store.checkpoint(
        job["id"],
        "action",
        {
            "action": "ezlynx_policy_setup",
            "destination": {
                "policy_number": "TEST-HO-20260911-E01",
                "policy_id": "83669533",
                "applicant_id": "220250093",
            },
        },
    )
    store.transition(
        job["id"],
        JobStatus.AWAITING_HUMAN_INPUT,
        expected={JobStatus.PENDING},
        error="coverage amounts not on the job; will not invent them",
        resume_status=JobStatus.PENDING,
        release_lease=True,
    )
    return store.get_job(job["id"])


class ParseCoverageReplyTests(unittest.TestCase):
    def test_carlo_reply_parses_a_d_and_f_not_e(self) -> None:
        amounts = parse_coverage_amounts_from_reply(CARLO_REPLY)
        self.assertEqual(amounts["dwelling"], "1200000")
        self.assertEqual(amounts["other_structures"], "120000")
        self.assertEqual(amounts["personal_property"], "500000")
        self.assertEqual(amounts["loss_of_use"], "500000")
        self.assertEqual(amounts["medical_payments"], "10000")
        self.assertNotIn("personal_liability", amounts)
        self.assertEqual(missing_coverage_letters(amounts), ["E"])

    def test_letter_only_amounts_without_coverage_word(self) -> None:
        amounts = parse_coverage_amounts_from_reply(
            "A $1,200,000 B $120,000 C $500,000 D $500,000 F $10,000"
        )
        self.assertEqual(amounts["dwelling"], "1200000")
        self.assertEqual(missing_coverage_letters(amounts), ["E"])

    def test_omitted_letter_is_not_invented(self) -> None:
        amounts = parse_coverage_amounts_from_reply("Coverage A $250000")
        self.assertEqual(amounts, {"dwelling": "250000"})
        self.assertEqual(
            missing_coverage_letters(amounts),
            ["B", "C", "D", "E", "F"],
        )


class FindParkedHitlJobTests(unittest.TestCase):
    def test_subject_prefix_resolves_parked_email_job(self) -> None:
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            parked = _park_coverage_hitl(store)
            token = parse_hitl_job_token(
                f"Re: [ROBIE HITL] Job {parked['id'][:8]} stuck at coverage_fill"
            )
            self.assertEqual(token, parked["id"][:8])
            found = find_parked_email_hitl_job(
                store,
                subject=f"Re: [ROBIE HITL] Job {parked['id'][:8]} stuck at coverage_fill",
                body=CARLO_REPLY,
            )
            self.assertIsNotNone(found)
            self.assertEqual(found["id"], parked["id"])

    def test_unrelated_email_does_not_match(self) -> None:
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            _park_coverage_hitl(store)
            self.assertIsNone(
                find_parked_email_hitl_job(
                    store,
                    subject="Quote for a new homeowners policy",
                    body="Please create TEST-HO-20260911-E01",
                )
            )


class ResumeSameJobTests(unittest.TestCase):
    def test_reply_resumes_same_job_and_does_not_create_intake(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            parked = _park_coverage_hitl(store)
            before_jobs = _job_count(store)
            before_intake = _intake_count(store)
            prompt = (
                f"Subject: Re: [ROBIE HITL] Job {parked['id'][:8]} "
                f"stuck at coverage_fill\n\n{CARLO_REPLY}"
            )
            seen: list[str] = []

            def worker(prompt_text, job_id, db_path):
                seen.append(job_id)
                payload = JobStore(db_path).get_job(job_id)["payload"]
                self.assertTrue(payload.get("hitl_resume"))
                coverage = payload["human_input_values"]["coverage"]
                self.assertEqual(coverage["dwelling"], "1200000")
                self.assertNotIn("personal_liability", coverage)
                return missing_coverage_letters_human_text(
                    channel="email", missing=["E"], have=["A", "B", "C", "D", "F"]
                )

            response = run_guarded_email_task(
                db_path=db,
                gmail_message_id="reply-a8068d3d",
                prompt=prompt,
                run_agent=lambda _p: self.fail("must use context callback"),
                run_agent_with_context=worker,
                verifiers={},
            )
            self.assertEqual(seen, [parked["id"]])
            self.assertIn(parked["id"], response)
            self.assertEqual(_job_count(store), before_jobs)
            self.assertEqual(_intake_count(store), before_intake)
            after = store.get_job(parked["id"])
            self.assertNotEqual(after["status"], "PENDING")
            self.assertEqual(
                after["payload"]["human_input_values"]["coverage"]["medical_payments"],
                "10000",
            )

    def test_new_email_still_creates_a_job(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            _park_coverage_hitl(store)
            before = _job_count(store)
            run_guarded_email_task(
                db_path=db,
                gmail_message_id="unrelated-new",
                prompt="Please file this endorsement",
                run_agent=lambda _p: "filed",
                verifiers={},
            )
            self.assertEqual(_job_count(store), before + 1)


class ApplyHitlAmountsTests(unittest.TestCase):
    def test_resume_invokes_setup_with_reply_amounts_not_playwright(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            parked = _park_coverage_hitl(store)
            ingest_email_hitl_reply(
                store,
                job_id=parked["id"],
                gmail_message_id="reply-a8068d3d",
                subject=f"Re: [ROBIE HITL] Job {parked['id'][:8]} stuck at coverage_fill",
                body=CARLO_REPLY,
            )
            captured: list[dict] = []

            def fake_invoke(args):
                captured.append(dict(args))
                return {
                    "success": True,
                    "policy_id": "83669533",
                    "phase_reached": "coverage_fill",
                    "formentry_found": True,
                }

            with patch(
                "robie_job_engine.policy_setup_dispatch.invoke_policy_setup_tool",
                side_effect=fake_invoke,
            ):
                text = apply_hitl_coverage_fill(store, parked["id"])
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["policy_number"], "TEST-HO-20260911-E01")
            self.assertEqual(captured[0]["dwelling"], "1200000")
            self.assertEqual(captured[0]["medical_payments"], "10000")
            self.assertNotIn("personal_liability", captured[0])
            folded = text.casefold()
            self.assertIn("i still need the coverage e", folded)
            self.assertIn("will not invent it", folded)
            self.assertIn("reply to this email", folded)
            self.assertNotIn("playwright_blocked", folded)
            self.assertNotIn("chat thread", folded)
            self.assertNotIn("policy_setup_order", folded)
            args = policy_setup_args_from_hitl_payload(store.get_job(parked["id"])["payload"])
            self.assertEqual(args["dwelling"], "1200000")
            self.assertNotIn("personal_liability", args)


class MissingLetterCopyTests(unittest.TestCase):
    def test_email_asks_only_for_e(self) -> None:
        text = missing_coverage_letters_human_text(
            channel="email", missing=["E"], have=["A", "B", "C", "D", "F"]
        )
        self.assertIn("I have Coverage A, B, C, D, and F.", text)
        self.assertIn("I still need the Coverage E dollar amount.", text)
        self.assertIn("I will not invent it.", text)
        self.assertIn("Reply to this email with that number.", text)
        self.assertNotIn("Chat thread", text)


if __name__ == "__main__":
    unittest.main()
