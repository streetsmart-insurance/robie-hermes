"""Global HITL ladder: Gemini first, apply+continue, Carlo, then 30-minute kill.

Mocks only. No live EZLynx. Same rules for email and Chat job types.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.email_guard import HermesEmailWorker
from robie_job_engine.hitl_escalation import HitlRequest, escalate
from robie_job_engine.hitl_ladder import (
    ACTION_AWAIT_HUMAN,
    ACTION_CONTINUE,
    ACTION_KILL,
    HITL_NO_REPLY_ERROR,
    HITL_NO_REPLY_SECONDS,
    HITL_POSTED_AT_KEY,
    HitlLadderState,
    decide_hitl_ladder,
    expire_unanswered_hitl_jobs,
    stamp_hitl_posted_at,
    unanswered_hitl_kill_reason,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


NOW = datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)


def _state(**overrides) -> HitlLadderState:
    payload = dict(
        gemini_asked=False,
        gemini_actionable=False,
        gemini_applied=False,
        applied_retry_attempted=False,
        applied_retry_failed=False,
        unguessable=False,
        carlo_hitl_posted_at=None,
        carlo_replied=False,
        channel="chat",
    )
    payload.update(overrides)
    return HitlLadderState(**payload)


class HitlLadderDecisionTests(unittest.TestCase):
    def test_gemini_applied_then_continue_chat(self) -> None:
        decision = decide_hitl_ladder(
            _state(
                gemini_asked=True,
                gemini_actionable=True,
                gemini_applied=True,
                channel="chat",
            ),
            now=NOW,
        )
        self.assertEqual(decision.action, ACTION_CONTINUE)
        self.assertEqual(decision.status, JobStatus.RUNNING.value)
        self.assertFalse(decision.loop_carlo)
        self.assertTrue(decision.apply_gemini)

    def test_gemini_applied_then_continue_email(self) -> None:
        decision = decide_hitl_ladder(
            _state(
                gemini_asked=True,
                gemini_actionable=True,
                gemini_applied=True,
                channel="email",
            ),
            now=NOW,
        )
        self.assertEqual(decision.action, ACTION_CONTINUE)
        self.assertEqual(decision.channel, "email")
        self.assertEqual(decision.status, JobStatus.RUNNING.value)

    def test_gemini_miss_then_hitl_chat(self) -> None:
        decision = decide_hitl_ladder(
            _state(gemini_asked=True, gemini_actionable=False, channel="chat"),
            now=NOW,
        )
        self.assertEqual(decision.action, ACTION_AWAIT_HUMAN)
        self.assertEqual(decision.status, JobStatus.AWAITING_HUMAN_INPUT.value)
        self.assertTrue(decision.loop_carlo)

    def test_gemini_miss_then_hitl_email(self) -> None:
        decision = decide_hitl_ladder(
            _state(gemini_asked=True, gemini_actionable=False, channel="email"),
            now=NOW,
        )
        self.assertEqual(decision.action, ACTION_AWAIT_HUMAN)
        self.assertEqual(decision.channel, "email")
        self.assertTrue(decision.loop_carlo)

    def test_unguessable_skips_gemini_and_asks_carlo(self) -> None:
        decision = decide_hitl_ladder(_state(unguessable=True), now=NOW)
        self.assertEqual(decision.action, ACTION_AWAIT_HUMAN)
        self.assertIn("must not be guessed", decision.reason)

    def test_thirty_minute_no_reply_kills(self) -> None:
        posted = NOW - timedelta(seconds=HITL_NO_REPLY_SECONDS)
        decision = decide_hitl_ladder(
            _state(carlo_hitl_posted_at=posted, channel="email"),
            now=NOW,
        )
        self.assertEqual(decision.action, ACTION_KILL)
        self.assertEqual(decision.status, JobStatus.FAILED.value)
        self.assertEqual(decision.reason, HITL_NO_REPLY_ERROR)

    def test_twenty_nine_minutes_still_waits(self) -> None:
        posted = NOW - timedelta(seconds=HITL_NO_REPLY_SECONDS - 60)
        decision = decide_hitl_ladder(
            _state(carlo_hitl_posted_at=posted),
            now=NOW,
        )
        self.assertEqual(decision.action, ACTION_AWAIT_HUMAN)
        self.assertEqual(decision.status, JobStatus.AWAITING_HUMAN_INPUT.value)


class HitlLadderExpireTests(unittest.TestCase):
    def _parked(self, store: JobStore, action_type: str, *, posted_at: str) -> dict:
        job = store.create_job(
            action_type,
            {"prompt": "setup homeowners", "stamp": posted_at, "kind": action_type},
            idempotency_key=None,
        )
        store.update_payload(job["id"], stamp_hitl_posted_at(job["payload"], now=posted_at))
        store.transition(
            job["id"],
            JobStatus.RUNNING,
            expected={JobStatus.PENDING},
        )
        store.transition(
            job["id"],
            JobStatus.AWAITING_HUMAN_INPUT,
            expected={JobStatus.RUNNING},
            error="ROBIE HITL: STOP AND ASK",
            release_lease=True,
        )
        return store.get_job(job["id"])

    def test_expire_kills_email_and_chat_after_30_minutes(self) -> None:
        stale = (NOW - timedelta(seconds=HITL_NO_REPLY_SECONDS + 5)).isoformat()
        fresh = NOW.isoformat()
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            email_job = self._parked(store, "hermes.email_task", posted_at=stale)
            chat_job = self._parked(store, "hermes.google_chat_task", posted_at=stale)
            young = self._parked(store, "hermes.email_task", posted_at=fresh)
            killed = expire_unanswered_hitl_jobs(store, now=NOW)
            self.assertIn(email_job["id"], killed)
            self.assertIn(chat_job["id"], killed)
            self.assertNotIn(young["id"], killed)
            self.assertEqual(store.get_job(email_job["id"])["status"], JobStatus.FAILED.value)
            self.assertEqual(store.get_job(chat_job["id"])["status"], JobStatus.FAILED.value)
            self.assertEqual(
                store.get_job(young["id"])["status"],
                JobStatus.AWAITING_HUMAN_INPUT.value,
            )
            self.assertIn("HITL_NO_REPLY", store.get_job(email_job["id"])["last_error"])

    def test_open_chat_job_kills_stale_email_and_chat_hitl(self) -> None:
        """Chat-only days never call JobEngine.run; open_chat_job must sweep."""
        from robie_job_engine.chat_guard import open_chat_job, stop_generic_chat_job_heartbeat

        stale = (
            datetime.now(timezone.utc) - timedelta(seconds=HITL_NO_REPLY_SECONDS + 30)
        ).isoformat()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            email_job = self._parked(store, "hermes.email_task", posted_at=stale)
            chat_job = self._parked(store, "hermes.google_chat_task", posted_at=stale)
            opened = open_chat_job(
                db,
                "message-hitl-sweep",
                "finish the EZLynx commercial auto",
                conversation_id="spaces/hitl-sweep",
            )
            self.assertTrue(opened)
            stop_generic_chat_job_heartbeat(db, opened)
            self.assertEqual(store.get_job(email_job["id"])["status"], JobStatus.FAILED.value)
            self.assertEqual(store.get_job(chat_job["id"])["status"], JobStatus.FAILED.value)
            self.assertIn("HITL_NO_REPLY", store.get_job(email_job["id"])["last_error"])
            self.assertIn("HITL_NO_REPLY", store.get_job(chat_job["id"])["last_error"])

    def test_unanswered_reason_uses_posted_at_not_running(self) -> None:
        job = {
            "status": JobStatus.AWAITING_HUMAN_INPUT.value,
            "updated_at": NOW.isoformat(),
            "payload": {
                HITL_POSTED_AT_KEY: (
                    NOW - timedelta(seconds=HITL_NO_REPLY_SECONDS)
                ).isoformat()
            },
        }
        self.assertEqual(unanswered_hitl_kill_reason(job, now=NOW), HITL_NO_REPLY_ERROR)
        job["status"] = JobStatus.RUNNING.value
        self.assertIsNone(unanswered_hitl_kill_reason(job, now=NOW))


class HitlLadderChannelEscalateTests(unittest.TestCase):
    def test_generic_phase_gemini_apply_continues_email_and_chat(self) -> None:
        """Any job type: Gemini answers → apply and continue, no Carlo HITL."""
        for channel in ("email", "chat"):
            chats: list[str] = []
            emails: list[str] = []
            response = escalate(
                HitlRequest(
                    job_id=f"any-{channel}",
                    phase="unique_write",
                    error="PLAYWRIGHT_BLOCKED: write target matched 3 fields",
                    page_state={"url": "https://app.ezlynx.com"},
                    attempted=["unique_write"],
                    applicant_id="",
                    gemini_asked=False,
                    gemini_applied=False,
                    channel=channel,
                    script_or_job_stopped=False,
                    job_still_running=True,
                ),
                {
                    "gemini_client": type(
                        "C",
                        (),
                        {
                            "generate_content": lambda self, _p: (
                                "FILE: robie_job_engine/playwright_write_guard.py\n"
                                "AFTER: apply the named unique field"
                            )
                        },
                    )(),
                    "chat_sender": lambda m: chats.append(m) or True,
                    "email_sender": lambda **_k: emails.append("sent"),
                },
            )
            self.assertTrue(response.actionable, channel)
            self.assertEqual(response.source, "gemini", channel)
            self.assertFalse(response.hitl_posted, channel)
            self.assertEqual(chats, [])
            self.assertEqual(emails, [])

    def test_generic_phase_gemini_miss_loops_carlo_email_and_chat(self) -> None:
        """Any job type: Gemini miss → Carlo HITL, not fake RUNNING."""
        for channel, email_ok, chat_ok in (
            ("email", True, False),
            ("chat", False, True),
        ):
            chats: list[str] = []
            emails: list[str] = []

            def email_sender(**_k):
                emails.append("sent")
                if not email_ok:
                    raise RuntimeError("signBlob failed")

            response = escalate(
                HitlRequest(
                    job_id=f"miss-{channel}",
                    phase="unique_write",
                    error="PLAYWRIGHT_BLOCKED: write target matched 3 fields",
                    page_state={},
                    attempted=["unique_write", "gemini"],
                    applicant_id="",
                    gemini_asked=False,
                    channel=channel,
                ),
                {
                    "gemini_client": type(
                        "C",
                        (),
                        {"generate_content": lambda self, _p: "UNSURE"},
                    )(),
                    "chat_sender": lambda m: chats.append(m) or chat_ok,
                    "email_sender": email_sender,
                },
            )
            self.assertFalse(response.actionable, channel)
            self.assertTrue(response.hitl_posted, channel)
            self.assertEqual(response.source, "system", channel)

    def test_email_channel_counts_email_send_as_posted(self) -> None:
        sent = []

        def email_sender(*, to, subject, body):
            sent.append(to)
            return None

        response = escalate(
            HitlRequest(
                job_id="email-hitl",
                phase="coverage_fill",
                error="coverage amounts not on the job; will not guess coverage amounts",
                page_state={},
                attempted=["coverage_fill"],
                applicant_id="220250093",
                policy_id="83669533",
                unguessable=True,
                channel="email",
                formentry_exists=True,
            ),
            {"email_sender": email_sender, "chat_sender": lambda _m: False},
        )
        self.assertTrue(response.hitl_posted)
        self.assertFalse(response.actionable)
        self.assertTrue(sent)

    def test_chat_channel_requires_chat_post(self) -> None:
        response = escalate(
            HitlRequest(
                job_id="chat-hitl",
                phase="coverage_fill",
                error="no coverage labels were filled",
                page_state={},
                attempted=["coverage_fill", "gemini_apply_retry"],
                applicant_id="220250093",
                applied_retry_failed=True,
                gemini_asked=True,
                channel="chat",
                formentry_exists=True,
            ),
            {"email_sender": lambda **_k: None, "chat_sender": lambda _m: False},
        )
        self.assertFalse(response.hitl_posted)
        self.assertFalse(response.actionable)


class EmailWorkerAnyHitlTests(unittest.TestCase):
    def test_email_worker_parks_generic_robie_hitl(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.email_task",
                {
                    "prompt": "any stuck job",
                    "gmail_message_id": "hitl-any",
                    "request_text": "help",
                },
            )
            worker = HermesEmailWorker(
                lambda _prompt: "ROBIE HITL: STOP AND ASK. Gemini miss after apply.",
                store,
            )
            result = worker.perform(job, idempotency_key=job["idempotency_key"])
            self.assertEqual(result.hold_status, JobStatus.AWAITING_HUMAN_INPUT)
            payload = store.get_job(job["id"])["payload"]
            self.assertIn(HITL_POSTED_AT_KEY, payload)


if __name__ == "__main__":
    unittest.main()
