"""Offline transport failures: no live Chat, browser or EZLynx calls."""
import asyncio
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from durable_temp import durable_temporary_directory
from robie_job_engine.chat_reply_outbox import ChatReplyOutbox, LEASE_SECONDS, MAX_ATTEMPTS
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from test_round10_reply_lifecycle import SPACE, THREAD, OTHER, _adapter_module, _chat


class ApiError(Exception):
    def __init__(self, status):
        self.resp = SimpleNamespace(status=status)
        super().__init__(f"synthetic HTTP {status}")


class Messages:
    """Model server-side idempotency, not just attempted create calls."""
    def __init__(self):
        self.calls = []
        self.accepted = {}
        self.failures = []
        self.accept_then_timeout = False
        self.collide = False
        self.on_accept = lambda: None

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))

        def execute(http=None):
            if self.failures:
                raise ApiError(self.failures.pop(0))
            key = kwargs["parent"] + "/messages/" + kwargs["messageId"]
            if key in self.accepted and self.collide:
                raise ApiError(409)
            if key not in self.accepted:
                self.accepted[key] = dict(copy.deepcopy(kwargs["body"]), name=key)
            self.on_accept()
            if self.accept_then_timeout:
                self.accept_then_timeout = False
                raise TimeoutError("accepted but response timed out")
            return self.accepted[key]
        return SimpleNamespace(execute=execute)

    def get(self, name):
        return SimpleNamespace(execute=lambda http=None: self.accepted[name])


class ReplyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.job = self.store.create_job("hermes.google_chat_task", {"text": "synthetic lookup", "conversation_id": SPACE})["id"]
        self.store.transition(self.job, JobStatus.RUNNING, expected={JobStatus.PENDING})
        bind_job_chat_thread(self.store, self.job, THREAD)
        self.adapter = _adapter_module()
        self.clock = 1000000.0
        self.enterContext(patch.object(self.adapter, "ROBIE_JOB_DB", self.db))
        self.enterContext(patch("robie_job_engine.chat_reply_outbox.time", SimpleNamespace(time=lambda: self.clock)))
        self.enterContext(patch.dict("os.environ", {"ROBIE_HEALTH_CHAT_SPACE": ""}))
        self.messages = Messages()
        self.outbox = ChatReplyOutbox(self.db)

    def chat(self):
        chat = _chat(self.db)
        chat._chat_api._spaces._messages = self.messages
        chat._chat_api.messages = self.messages
        chat._set_fatal_error = lambda **kw: None
        return chat

    def run_async(self, coro):
        async def no_sleep(_seconds):
            pass
        with patch.object(self.adapter.asyncio, "sleep", no_sleep):
            return asyncio.run(coro)

    def send(self, chat, text="The requested result.", **metadata):
        return self.run_async(chat.send(SPACE, text, metadata={"robie_job_id": self.job, "robie_delivery_kind": "notice", **metadata}))

    def rows(self):
        with self.store.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM chat_reply_outbox ORDER BY rowid")]
        return [self.outbox.get(i) for i in ids]

    def failed(self):
        self.messages.failures = [429] * 3
        self.assertFalse(self.send(self.chat()).success)
        self.assertEqual(len(self.messages.accepted), 0)
        return self.rows()[0]

    def test_exhausted_429_restart_recovers_without_inbound_or_action(self):
        row = self.failed()
        self.clock += 31
        recovered = self.chat()
        recovered._active_chat_job[SPACE] = "unrelated-job"
        recovered._last_inbound_thread[SPACE] = OTHER
        with patch.object(recovered, "_finish_sent_reply", side_effect=AssertionError("no business finalization")), patch.object(
            self.adapter, "guard_chat_response", side_effect=AssertionError("no EZLynx re-read")
        ):
            self.run_async(recovered._recover_pending_replies())
        self.assertEqual(len(self.messages.accepted), 1)
        delivered = next(iter(self.messages.accepted.values()))
        self.assertEqual(delivered["thread"], {"name": THREAD})
        self.assertEqual(self.messages.calls[-1]["messageReplyOption"], "REPLY_MESSAGE_OR_FAIL")
        self.assertEqual(self.outbox.get(row["id"])["state"], "delivered")
        self.assertTrue(self.store.get_checkpoint(self.job, "chat_delivery")["posted"])
        self.assertTrue(self.store.get_checkpoint(self.job, "chat_delivery_failed")["posted"])
        self.assertEqual(self.store.get_job(self.job)["status"], "RUNNING")

    def test_repeated_recovery_and_repeated_send_do_not_duplicate(self):
        self.assertTrue(self.send(self.chat()).success)
        before = len(self.messages.calls)
        self.run_async(self.chat()._recover_pending_replies())
        self.assertTrue(self.send(self.chat()).success)
        self.assertEqual(len(self.messages.calls), before)
        self.assertEqual(len(self.messages.accepted), 1)

    def test_acceptance_followed_by_timeout_reuses_server_identity(self):
        self.messages.accept_then_timeout = True
        self.assertTrue(self.send(self.chat()).success)
        self.assertEqual(len(self.messages.accepted), 1)
        self.assertEqual(len(self.messages.calls), 2)
        self.assertEqual(self.messages.calls[0], self.messages.calls[1])

    def test_crash_after_acceptance_before_receipt_recovers_same_message(self):
        with patch.object(ChatReplyOutbox, "delivered_chunk", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                self.send(self.chat())
        self.assertEqual(len(self.messages.accepted), 1)
        self.assertEqual(self.rows()[0]["state"], "sending")
        self.clock += LEASE_SECONDS + 1
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(len(self.messages.accepted), 1)
        self.assertEqual(self.rows()[0]["state"], "delivered")

    def test_conflict_requires_matching_message_readback(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                text = f"Result variant {mismatch}"
                with patch.object(ChatReplyOutbox, "delivered_chunk", side_effect=asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        self.send(self.chat(), text)
                row = self.rows()[-1]
                if mismatch:
                    key = f"{SPACE}/messages/client-{self.outbox.request_id(row, 0)}"
                    self.messages.accepted[key]["text"] = "Different reply"
                self.messages.collide = True
                self.clock += LEASE_SECONDS + 1
                self.run_async(self.chat()._recover_pending_replies())
                self.assertEqual(self.outbox.get(row["id"])["state"], "failed" if mismatch else "delivered")

    def test_partial_multichunk_reply_resumes_only_unsent_chunks(self):
        chat = self.chat()
        chat._chunk_text = lambda _: ["first", "second", "third"]
        def interrupt():
            if len(self.messages.accepted) == 1:
                self.messages.failures = [429] * 3
        self.messages.on_accept = interrupt
        self.assertFalse(self.send(chat).success)
        row = self.rows()[0]
        self.assertEqual(row["next_chunk"], 1)
        self.messages.on_accept = lambda: None
        self.clock += 31
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual([v["text"] for v in self.messages.accepted.values()], ["first", "second", "third"])
        self.assertEqual(self.outbox.get(row["id"])["state"], "delivered")

    def test_explicit_stop_suppresses_old_reply_but_allows_stop_notice(self):
        self.failed()
        self.store.transition(self.job, JobStatus.CANCELLED, expected={JobStatus.RUNNING})
        self.clock += 31
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.rows()[0]["state"], "cancelled")
        self.assertFalse(self.messages.accepted)
        self.assertTrue(self.send(self.chat(), "Stopped. That job is cancelled.", robie_stop_notice=True, robie_delivery_kind="stop").success)

    def test_user_answer_supersedes_old_question(self):
        self.failed()
        self.store.checkpoint(self.job, "clarification_reply", {"text": "the second one"})
        self.clock += 31
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.rows()[0]["state"], "cancelled")
        self.assertFalse(self.messages.accepted)

    def test_parked_question_recovers_but_terminal_question_is_suppressed(self):
        self.store.transition(self.job, JobStatus.NEEDS_CLARIFICATION, expected={JobStatus.RUNNING})
        self.messages.failures = [429] * 3
        self.assertFalse(self.send(self.chat(), "Which client should I use?").success)
        self.clock += 31
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.rows()[0]["state"], "delivered")
        self.assertEqual(self.store.get_job(self.job)["status"], "NEEDS_CLARIFICATION")
        self.messages.failures = [429] * 3
        self.assertFalse(self.send(self.chat(), "Which discussion should I use?").success)
        self.store.transition(self.job, JobStatus.FAILED, expected={JobStatus.NEEDS_CLARIFICATION})
        self.clock += 31
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.rows()[-1]["state"], "cancelled")
        self.assertEqual(len(self.messages.accepted), 1)

    def test_permanent_error_is_held_and_transient_error_recovers(self):
        for status in (403, 404, 503):
            with self.subTest(status=status):
                self.messages.failures = [status] * (3 if status == 503 else 1)
                if status == 503:
                    with self.assertRaises(ApiError):
                        self.send(self.chat(), f"Result {status}")
                else:
                    # Adapter's real HttpError class is lazy-loaded; use the
                    # transport method directly to avoid depending on that import.
                    rid = self.outbox.prepare(self.job, SPACE, "notice", [{"text": f"Result {status}", "thread": {"name": THREAD}}])
                    with self.assertRaises(ApiError):
                        self.run_async(self.chat()._deliver_durable_reply(rid))
                row = self.rows()[-1]
                self.assertEqual(row["state"], "pending" if status == 503 else "failed")
                self.clock += 31
                self.run_async(self.chat()._recover_pending_replies())
                self.assertEqual(self.outbox.get(row["id"])["state"], "delivered" if status == 503 else "failed")

    def test_live_lease_excludes_another_drainer(self):
        row = self.failed()
        self.clock += 31
        self.assertIsNotNone(self.outbox.claim(row["id"]))
        self.assertIsNone(ChatReplyOutbox(self.db).claim(row["id"]))
        calls = len(self.messages.calls)
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(len(self.messages.calls), calls)

    def test_retry_budget_and_expiry_are_durable(self):
        row = self.failed()
        for _ in range(MAX_ATTEMPTS - 1):
            self.clock += 301
            self.messages.failures = [429] * 3
            self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.outbox.get(row["id"])["state"], "failed")
        self.assertEqual(self.outbox.get(row["id"])["attempts"], MAX_ATTEMPTS)
        self.messages.failures = []
        self.clock += 301
        self.run_async(self.chat()._recover_pending_replies())
        self.assertFalse(self.messages.accepted)
        other = self.outbox.prepare(self.job, SPACE, "notice", [{"text": "Expired result", "thread": {"name": THREAD}}])
        self.clock += 7201
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.outbox.get(other)["state"], "failed")

    def test_outbox_write_failure_prevents_first_http_call(self):
        with patch.object(ChatReplyOutbox, "prepare", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.send(self.chat())
        self.assertFalse(self.messages.calls)

    def test_receipt_disk_failure_leaves_recoverable_lease(self):
        with patch.object(ChatReplyOutbox, "delivered_chunk", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.send(self.chat())
        self.assertEqual(self.rows()[0]["state"], "sending")
        self.clock += LEASE_SECONDS + 1
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.rows()[0]["state"], "delivered")
        self.assertEqual(len(self.messages.accepted), 1)

    def test_reply_cannot_be_retargeted_to_another_space(self):
        with self.assertRaises(ValueError):
            self.outbox.prepare(self.job, "spaces/OTHER", "notice", [{"text": "result", "thread": {"name": THREAD}}])
        self.assertFalse(self.messages.calls)

    def test_background_drainer_runs_without_a_user_send(self):
        self.failed()
        self.clock += 31
        async def exercise():
            chat = self.chat()
            chat._shutting_down = False
            chat._ensure_reply_recovery()
            task = chat._reply_recovery_task
            chat._ensure_reply_recovery()
            self.assertIs(chat._reply_recovery_task, task)
            try:
                for _ in range(100):
                    if self.rows()[0]["state"] == "delivered":
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(self.rows()[0]["state"], "delivered")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        asyncio.run(exercise())

    def test_legacy_failure_without_frozen_envelope_is_not_guessed(self):
        self.store.checkpoint(self.job, "chat_delivery_failed", {"text": "old reply", "status": 429})
        self.run_async(self.chat()._recover_pending_replies())
        self.assertFalse(self.messages.calls)

    def test_recovery_replays_formatted_wire_text_without_internal_codes(self):
        self.messages.failures = [429] * 3
        self.assertFalse(self.send(self.chat(), "ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: client name").success)
        body = self.rows()[0]["bodies"][0]
        self.assertNotIn("ROBIE_BLOCKED", body["text"])
        self.assertNotIn("MISSING_REQUIRED_FIELD", body["text"])
        self.clock += 31
        self.run_async(self.chat()._recover_pending_replies())
        self.assertEqual(self.messages.calls[-1]["body"], body)

    def test_disconnect_cancels_background_drainer(self):
        async def exercise():
            chat = self.chat()
            chat._shutting_down = False
            chat._chat_queue_drain_task = chat._supervisor_task = None
            chat._streaming_pull_future = chat._subscriber = None
            chat._mark_disconnected = lambda: None
            chat._ensure_reply_recovery()
            task = chat._reply_recovery_task
            await chat.disconnect()
            self.assertTrue(task.cancelled())
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
