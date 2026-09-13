"""HITL retry loop: Gemini first, then Carlo (30 min), then kill.

Honesty contract for the new loop (Carlo's 2026-09-13 directive):
- A retry counts as recovered ONLY when the job's own retry reports success.
  Gemini saying "fixed" without a successful retry is not recovery.
- Carlo is looped in only after Gemini's attempts are exhausted, with honest
  wording: what failed, what Gemini tried. Never "resolved", never
  "continuing" unless the job actually recovered.
- 30 minutes of silence (or an explicit KILL) kills the job: fail closed.

Mocks only. No Chrome. No live EZLynx.
"""

from __future__ import annotations

import asyncio
import unittest

from robie_job_engine.hitl_loop import (
    HitlLoopConfig,
    RetryOutcome,
    build_loop_carlo_notice,
    build_loop_kill_notice,
    hitl_retry_loop,
)


def _tiny_config(**overrides) -> HitlLoopConfig:
    cfg = dict(carlo_timeout_seconds=3, poll_interval_seconds=1)
    cfg.update(overrides)
    return HitlLoopConfig(**cfg)


def _run(coro):
    return asyncio.run(coro)


class LoopRecoveryTests(unittest.TestCase):
    def test_gemini_fix_plus_successful_retry_recovers_without_carlo(self) -> None:
        chats: list[str] = []
        consults = []

        async def consult(evidence):
            consults.append(evidence)
            return {"action": "use_exact_label", "reason": "map to visible label"}

        async def retry(fix):
            self.assertEqual(fix["action"], "use_exact_label")
            return RetryOutcome(success=True, detail="filled 2 labels", evidence={"filled": 2})

        result = _run(
            hitl_retry_loop(
                job_id="job-loop-1",
                phase="coverage_fill",
                first_error="0 labels matched",
                first_evidence={"wanted": ["Dwelling"]},
                consult_gemini=consult,
                retry_with_fix=retry,
                deps={"chat_sender": lambda m: chats.append(m) or True},
                config=_tiny_config(),
            )
        )
        self.assertTrue(result["recovered"])
        self.assertFalse(result["killed"])
        self.assertFalse(result["carlo_looped_in"])
        self.assertEqual(chats, [])
        self.assertEqual(result["gemini_attempts"], 1)

    def test_gemini_words_without_successful_retry_are_not_recovery(self) -> None:
        """Gemini claims a fix twice; the job's own retry fails twice.

        The loop must NOT report recovered, and must loop Carlo in with
        honest wording instead of inventing success.
        """
        chats: list[str] = []
        retries = []

        async def consult(evidence):
            return {"action": "try_again", "reason": "Gemini is confident this works"}

        async def retry(fix):
            retries.append(fix)
            return RetryOutcome(success=False, detail="still 0 labels matched")

        result = _run(
            hitl_retry_loop(
                job_id="job-loop-2",
                phase="coverage_fill",
                first_error="0 labels matched",
                first_evidence={},
                consult_gemini=consult,
                retry_with_fix=retry,
                deps={"chat_sender": lambda m: chats.append(m) or True},
                config=_tiny_config(),
            )
        )
        self.assertFalse(result["recovered"])
        self.assertEqual(result["gemini_attempts"], 2)
        self.assertEqual(len(retries), 2)
        self.assertTrue(result["carlo_looped_in"])
        self.assertTrue(chats)
        blob = chats[0].casefold()
        self.assertNotIn("resolved", blob)
        self.assertNotIn("continuing", blob)
        self.assertIn("gemini tried and failed", blob)

    def test_two_gemini_attempts_then_carlo(self) -> None:
        consults = []

        async def consult(evidence):
            consults.append(1)
            return {"action": "x", "reason": "y"}

        async def retry(fix):
            return RetryOutcome(success=False, detail="nope")

        result = _run(
            hitl_retry_loop(
                job_id="job-loop-3",
                phase="coverage_fill",
                first_error="boom",
                first_evidence={},
                consult_gemini=consult,
                retry_with_fix=retry,
                deps={"chat_sender": lambda m: True},
                config=_tiny_config(),
            )
        )
        self.assertEqual(len(consults), 2)
        self.assertTrue(result["carlo_looped_in"])


class LoopKillTests(unittest.TestCase):
    def test_silence_kills_job_with_final_summary(self) -> None:
        chats: list[str] = []

        async def consult(evidence):
            return {"action": "x", "reason": "y"}

        async def retry(fix):
            return RetryOutcome(success=False, detail="still broken")

        result = _run(
            hitl_retry_loop(
                job_id="job-loop-4",
                phase="coverage_fill",
                first_error="0 labels matched",
                first_evidence={},
                consult_gemini=consult,
                retry_with_fix=retry,
                deps={
                    "chat_sender": lambda m: chats.append(m) or True,
                    # no chat_reply_checker wired: nobody answers
                },
                config=_tiny_config(),
            )
        )
        self.assertFalse(result["recovered"])
        self.assertTrue(result["killed"])
        self.assertFalse(result["carlo_replied"])
        self.assertIn("did not respond", result["final_error"])
        self.assertIn("killed", result["final_error"])
        # Carlo got the honest ping first, then the kill notice.
        self.assertEqual(len(chats), 2)
        self.assertIn("KILLED", chats[1])

    def test_carlo_says_kill_stops_immediately(self) -> None:
        async def consult(evidence):
            return {"action": "x", "reason": "y"}

        async def retry(fix):
            raise AssertionError("no retry after KILL")

        def chat_reply_checker(job_id):
            self.assertEqual(job_id, "job-loop-5")
            return {"body": "KILL"}

        result = _run(
            hitl_retry_loop(
                job_id="job-loop-5",
                phase="coverage_fill",
                first_error="0 labels matched",
                first_evidence={},
                consult_gemini=consult,
                retry_with_fix=retry,
                deps={
                    "chat_sender": lambda m: True,
                    "chat_reply_checker": chat_reply_checker,
                },
                config=_tiny_config(),
            )
        )
        self.assertTrue(result["killed"])
        self.assertTrue(result["carlo_replied"])
        self.assertFalse(result["recovered"])
        self.assertIn("KILL", result["final_error"])

    def test_carlo_guidance_gets_exactly_one_final_retry(self) -> None:
        calls = []

        async def consult(evidence):
            return {"action": "x", "reason": "y"}

        async def retry(fix):
            calls.append(fix)
            if fix.get("action") == "carlo_guidance":
                return RetryOutcome(success=True, detail="Carlo's value worked")
            return RetryOutcome(success=False, detail="gemini fix failed")

        result = _run(
            hitl_retry_loop(
                job_id="job-loop-6",
                phase="coverage_fill",
                first_error="0 labels matched",
                first_evidence={},
                consult_gemini=consult,
                retry_with_fix=retry,
                deps={
                    "chat_sender": lambda m: True,
                    "chat_reply_checker": lambda job_id: {"body": "use the exact visible label"},
                },
                config=_tiny_config(),
            )
        )
        self.assertTrue(result["recovered"])
        self.assertFalse(result["killed"])
        self.assertTrue(result["carlo_replied"])
        carlo_calls = [c for c in calls if c.get("action") == "carlo_guidance"]
        self.assertEqual(len(carlo_calls), 1)


class LoopNoticeHonestyTests(unittest.TestCase):
    def test_carlo_notice_never_claims_resolved_or_continuing(self) -> None:
        notice = build_loop_carlo_notice(
            job_id="job-loop-7",
            phase="coverage_fill",
            first_error="0 labels matched",
            attempted=["gemini consult x2", "retry x2"],
            gemini_log=["attempt 1: applied fix, still failing"],
            timeout_seconds=1800,
        )
        blob = (notice["subject"] + notice["body"] + notice["chat"]).casefold()
        self.assertNotIn("resolved", blob)
        self.assertNotIn("continuing", blob)
        self.assertIn("gemini could not get it unstuck", notice["body"].casefold())
        self.assertIn("30 minutes", notice["body"])
        self.assertIn("killed automatically", notice["body"])

    def test_kill_notice_names_job_phase_and_last_error(self) -> None:
        notice = build_loop_kill_notice(
            job_id="job-loop-8", phase="coverage_fill", last_error="0 labels matched"
        )
        self.assertIn("job-loop-8", notice)
        self.assertIn("coverage_fill", notice)
        self.assertIn("0 labels matched", notice)
        self.assertIn("KILLED", notice)


if __name__ == "__main__":
    unittest.main()
