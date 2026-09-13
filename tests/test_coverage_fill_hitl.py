"""Empty FormEntry coverage fill must be honest HITL, not UNVERIFIED.

Job 2b30d293: date fix minted FormEntry, then "no coverage labels were filled".
The email job closed UNVERIFIED ("no policy number") instead of
AWAITING_HUMAN_INPUT. The last email had Written Premium 1.0 and no A-F
amounts — those must not be guessed. Mocks only. No live EZLynx.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from robie_job_engine.email_guard import HermesEmailWorker
from robie_job_engine.ezlynx_policy_setup import (
    EzlynxPolicySetupPage,
    HomeownersCoverageItem,
    PolicyShellInput,
    homeowners_coverage_amounts_missing,
)
from robie_job_engine.hitl import coverage_fill_miss_hitl_text
from robie_job_engine.hitl_escalation import HitlRequest, HitlResponse, build_hitl_notice
from robie_job_engine.models import JobStatus
from robie_job_engine.policy_setup_dispatch import (
    is_coverage_fill_miss,
    is_policy_setup_honest_hitl,
)
from robie_job_engine.store import JobStore
from robie_job_engine.worker_contract import classify_chat_close_without_checkpoint
from durable_temp import durable_temporary_directory
from tests.test_home_formentry_fill import REAL_FORMENTRY_URL
from tests.test_ezlynx_field_widgets import FakeGemini


NO_AMOUNTS = "coverage amounts not on the job; will not guess coverage amounts"
NO_LABELS = "no coverage labels were filled"


class CoverageFillDetectTests(unittest.TestCase):
    def test_empty_job_amounts_are_a_miss_not_a_guess(self) -> None:
        self.assertTrue(homeowners_coverage_amounts_missing(None))
        self.assertTrue(homeowners_coverage_amounts_missing(HomeownersCoverageItem()))
        self.assertFalse(
            homeowners_coverage_amounts_missing(
                HomeownersCoverageItem(dwelling_a="250000")
            )
        )
        self.assertFalse(HomeownersCoverageItem().liability_e)
        self.assertFalse(HomeownersCoverageItem().med_pay_f)

    def test_honest_hitl_includes_coverage_miss(self) -> None:
        self.assertTrue(is_coverage_fill_miss(NO_LABELS))
        self.assertTrue(is_coverage_fill_miss(NO_AMOUNTS))
        self.assertTrue(is_policy_setup_honest_hitl(NO_LABELS))
        text = coverage_fill_miss_hitl_text(detail=NO_LABELS)
        self.assertIn("ROBIE HITL: STOP AND ASK", text)
        self.assertIn("not guessed", text.casefold())
        self.assertNotIn("Gemini already handled", text)
        self.assertNotIn("job is still working", text.casefold())
        self.assertIn("not still working", text.casefold())

    def test_worker_contract_parks_awaiting_not_unverified(self) -> None:
        decision = classify_chat_close_without_checkpoint(
            content=NO_LABELS,
            last_error="no policy number on the Job or the action checkpoint",
        )
        self.assertEqual(decision.status, "AWAITING_HUMAN_INPUT")
        self.assertIn("coverage", decision.reason)

    def test_notice_does_not_claim_gemini_or_missing_formentry(self) -> None:
        notice = build_hitl_notice(
            HitlRequest(
                job_id="2b30d293",
                phase="coverage_fill",
                error=NO_LABELS,
                page_state={"url": REAL_FORMENTRY_URL},
                attempted=["coverage_fill"],
                applicant_id="220250093",
                policy_id="83669533",
                formentry_exists=True,
            )
        )
        blob = notice["subject"] + notice["body"] + notice["chat"]
        self.assertIn("not guessed", blob.casefold())
        self.assertIn("gemini did not handle this", blob.casefold())
        self.assertNotIn("FormEntry does not exist", blob)
        self.assertNotIn("job is continuing", blob.casefold())


class _MintedPage:
    url = REAL_FORMENTRY_URL

    def __init__(self) -> None:
        self.goto = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.screenshot = AsyncMock()
        self.evaluate = AsyncMock(return_value=[])
        self.context = MagicMock()
        self.context.pages = [self]
        self.frames = []

    def locator(self, *_a, **_k):
        loc = MagicMock()
        loc.count = AsyncMock(return_value=0)
        loc.first = loc
        loc.click = AsyncMock()
        return loc

    def get_by_role(self, *_a, **_k):
        return self.locator()


class CoverageFillSetupTests(unittest.TestCase):
    def test_no_job_amounts_is_hitl_and_persists_policy_id(self) -> None:
        async def _run() -> None:
            page = _MintedPage()
            setup = EzlynxPolicySetupPage(
                page,
                job_id="2b30d293-test",
                hitl_deps={
                    "gemini_client": FakeGemini('{"decision":"unsure"}'),
                    "email_sender": lambda **_k: None,
                    "chat_sender": lambda _m: True,
                },
            )

            async def _already_minted(policy_id, applicant_id="", **_k):
                return {
                    "formentry_found": True,
                    "formentry_url": REAL_FORMENTRY_URL,
                    "policy_id": policy_id,
                }

            setup._mint_formentry = _already_minted  # type: ignore[method-assign]
            with patch(
                "robie_job_engine.hitl_escalation.escalate",
                return_value=HitlResponse(
                    source="system",
                    suggestion="STOP AND ASK",
                    actionable=False,
                    hitl_posted=True,
                ),
            ) as mock_escalate, patch(
                "robie_job_engine.ezlynx_api.EzlynxApiClient"
            ), patch(
                "robie_job_engine.ezlynx_api.load_ezlynx_api_config"
            ), patch(
                "robie_job_engine.policy_setup_proof.search_first_create",
                return_value={
                    "policy_id": "83669533",
                    "verdict": "ALREADY_EXISTS",
                    "read_back": {"policyId": "83669533"},
                },
            ), patch(
                "robie_job_engine.formentry_coverages.afill_coverages_by_label",
                side_effect=AssertionError("must not guess coverage amounts"),
            ):
                result = await setup.setup_policy_by_lob(
                    PolicyShellInput(
                        applicant_id="220250093",
                        lob="HOME",
                        policy_number="TEST-HO-20260911-E01",
                        effective_date="10/02/2026",
                        expiration_date="10/02/2027",
                    )
                )

            self.assertFalse(result.success)
            self.assertEqual(result.phase_reached, "coverage_fill")
            self.assertEqual(result.policy_id, "83669533")
            self.assertIn("will not guess", result.error or "")
            self.assertTrue(result.hitl_posted)
            mock_escalate.assert_called()
            req = mock_escalate.call_args[0][0]
            self.assertEqual(req.phase, "coverage_fill")
            self.assertEqual(req.policy_id, "83669533")
            self.assertTrue(req.formentry_exists)
            outer = result.to_dict()
            self.assertEqual(outer["policy_id"], "83669533")
            self.assertEqual(outer["policyId"], "83669533")

        asyncio.run(_run())

    def test_empty_live_label_fill_is_hitl(self) -> None:
        async def _run() -> None:
            page = _MintedPage()
            setup = EzlynxPolicySetupPage(
                page,
                job_id="2b30d293-labels",
                hitl_deps={
                    "email_sender": lambda **_k: None,
                    "chat_sender": lambda _m: True,
                },
            )

            async def _already_minted(policy_id, applicant_id="", **_k):
                return {
                    "formentry_found": True,
                    "formentry_url": REAL_FORMENTRY_URL,
                    "policy_id": policy_id,
                }

            setup._mint_formentry = _already_minted  # type: ignore[method-assign]
            with patch(
                "robie_job_engine.hitl_escalation.escalate",
                return_value=HitlResponse(
                    source="system",
                    suggestion="STOP AND ASK",
                    actionable=False,
                    hitl_posted=True,
                ),
            ) as mock_escalate, patch(
                "robie_job_engine.ezlynx_api.EzlynxApiClient"
            ), patch(
                "robie_job_engine.ezlynx_api.load_ezlynx_api_config"
            ), patch(
                "robie_job_engine.policy_setup_proof.search_first_create",
                return_value={
                    "policy_id": "83669533",
                    "verdict": "ALREADY_EXISTS",
                    "read_back": {"policyId": "83669533"},
                },
            ), patch(
                "robie_job_engine.formentry_coverages.afill_coverages_by_label",
                return_value={
                    "filled_count": 0,
                    "not_found": ["Dwelling"],
                    "labels": {"Dwelling": {"found": False, "reason": "label text not found"}},
                },
            ):
                result = await setup.setup_policy_by_lob(
                    PolicyShellInput(
                        applicant_id="220250093",
                        lob="HOME",
                        policy_number="TEST-HO-20260911-E01",
                        effective_date="10/02/2026",
                        expiration_date="10/02/2027",
                        homeowners_coverage=HomeownersCoverageItem(dwelling_a="250000"),
                    )
                )

            self.assertFalse(result.success)
            self.assertIn(NO_LABELS, result.error or "")
            self.assertTrue(result.hitl_posted)
            self.assertEqual(result.policy_id, "83669533")
            mock_escalate.assert_called()
            self.assertEqual(mock_escalate.call_args[0][0].phase, "coverage_fill")

        asyncio.run(_run())


class EmailWorkerCoverageHitlTests(unittest.TestCase):
    def test_email_parks_coverage_miss_and_keeps_policy_number(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.email_task",
                {
                    "prompt": "create homeowners TEST-HO-20260911-E01",
                    "gmail_message_id": "2b30d293",
                    "request_text": "Written Premium 1.0",
                },
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
            worker = HermesEmailWorker(
                lambda _prompt: coverage_fill_miss_hitl_text(detail=NO_LABELS),
                store,
            )
            result = worker.perform(job, idempotency_key=job["idempotency_key"])
            self.assertEqual(result.hold_status, JobStatus.AWAITING_HUMAN_INPUT)
            self.assertFalse(result.succeeded)
            self.assertIn("ROBIE HITL", result.error or "")
            self.assertEqual(
                result.destination.get("policy_number"), "TEST-HO-20260911-E01"
            )
            self.assertEqual(result.destination.get("policy_id"), "83669533")
            payload = store.get_job(job["id"])["payload"]
            self.assertEqual(payload.get("policy_number"), "TEST-HO-20260911-E01")
            self.assertEqual(payload.get("policy_id"), "83669533")


if __name__ == "__main__":
    unittest.main()
