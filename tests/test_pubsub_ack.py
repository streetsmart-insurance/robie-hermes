import asyncio
import unittest
from concurrent.futures import Future
from pathlib import Path

from tests.durable_temp import durable_temporary_directory
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.pubsub_ack import PubSubAckCoordinator


class FakeDeduplicator:
    def __init__(self):
        self.completed: set[str] = set()

    def contains(self, message_id: str) -> bool:
        return message_id in self.completed

    def is_duplicate(self, message_id: str) -> bool:
        duplicate = message_id in self.completed
        self.completed.add(message_id)
        return duplicate

    def discard(self, message_id: str) -> None:
        self.completed.discard(message_id)


class FakeMessage:
    def __init__(self):
        self.acks = 0
        self.nacks = 0

    def ack(self):
        self.acks += 1

    def nack(self):
        self.nacks += 1


async def work():
    return None


class PubSubAckTests(unittest.TestCase):
    def setUp(self):
        self.dedup = FakeDeduplicator()
        self.coordinator = PubSubAckCoordinator(self.dedup)

    def test_ack_waits_for_successful_handoff(self):
        future = Future()
        message = FakeMessage()
        coro = work()
        self.coordinator.schedule(
            coro=coro,
            message=message,
            message_id="m-success",
            submit=lambda submitted: (submitted.close(), future)[1],
        )
        self.assertEqual((message.acks, message.nacks), (0, 0))
        future.set_result(None)
        self.assertEqual((message.acks, message.nacks), (1, 0))
        self.assertTrue(self.dedup.contains("m-success"))

    def test_failure_nacks_and_releases_message_for_retry(self):
        future = Future()
        message = FakeMessage()
        errors = []
        coro = work()
        self.coordinator.schedule(
            coro=coro,
            message=message,
            message_id="m-retry",
            submit=lambda submitted: (submitted.close(), future)[1],
            on_error=errors.append,
        )
        future.set_exception(RuntimeError("handoff failed"))
        self.assertEqual((message.acks, message.nacks), (0, 1))
        self.assertFalse(self.dedup.contains("m-retry"))
        self.assertEqual(len(errors), 1)

    def test_inflight_duplicate_shares_original_outcome(self):
        future = Future()
        first = FakeMessage()
        duplicate = FakeMessage()
        submissions = []

        def submit(coro):
            coro.close()
            submissions.append(True)
            return future

        self.coordinator.schedule(
            coro=work(), message=first, message_id="m-dupe", submit=submit
        )
        self.coordinator.schedule(
            coro=work(), message=duplicate, message_id="m-dupe", submit=submit
        )
        self.assertEqual(len(submissions), 1)
        self.assertEqual((first.acks, duplicate.acks), (0, 0))
        future.set_result(None)
        self.assertEqual((first.acks, duplicate.acks), (1, 1))
        self.assertEqual((first.nacks, duplicate.nacks), (0, 0))

    def test_schedule_failure_is_not_false_success(self):
        message = FakeMessage()
        coro = work()
        self.coordinator.schedule(
            coro=coro,
            message=message,
            message_id="m-schedule-failure",
            submit=lambda submitted: None,
        )
        self.assertEqual((message.acks, message.nacks), (0, 1))
        self.assertFalse(self.dedup.contains("m-schedule-failure"))
        coro.close()

    def test_resume_exception_nacks_before_on_error_and_keeps_pulling(self):
        """A HITL RuntimeError must nack first so max_messages=1 cannot stall.

        Production hermes-poc-01 2026-08-27: resume_human_input raised
        inside the done callback, journalctl went silent after 21:30:09 UTC,
        and a later RETRY created no jobs.db row. Settlement before logging
        keeps the streaming-pull callback able to take the next delivery.
        """
        future = Future()
        message = FakeMessage()
        order: list[str] = []

        def on_error(exc: BaseException) -> None:
            order.append(f"error:{exc}")
            raise RuntimeError("logger/journald failed")

        original_nack = message.nack

        def nack_and_record() -> None:
            order.append("nack")
            original_nack()

        message.nack = nack_and_record  # type: ignore[method-assign]
        self.coordinator.schedule(
            coro=work(),
            message=message,
            message_id="spaces/AAQAZbLJO78/messages/retry",
            submit=lambda submitted: (submitted.close(), future)[1],
            on_error=on_error,
        )
        future.set_exception(RuntimeError("bound Job is not awaiting human input"))
        self.assertEqual(order[0], "nack")
        self.assertEqual(order[1], "error:bound Job is not awaiting human input")
        self.assertEqual((message.acks, message.nacks), (0, 1))
        self.assertFalse(self.dedup.contains("spaces/AAQAZbLJO78/messages/retry"))

        nxt = Future()
        follow = FakeMessage()
        self.coordinator.schedule(
            coro=work(),
            message=follow,
            message_id="spaces/AAQAZbLJO78/messages/later-retry",
            submit=lambda submitted: (submitted.close(), nxt)[1],
        )
        nxt.set_result(None)
        self.assertEqual((follow.acks, follow.nacks), (1, 0))

    def test_missing_correlation_exception_nacks_when_on_error_raises(self):
        future = Future()
        message = FakeMessage()

        def on_error(exc: BaseException) -> None:
            raise RuntimeError(f"on_error exploded: {exc}")

        self.coordinator.schedule(
            coro=work(),
            message=message,
            message_id="m-missing-correlation",
            submit=lambda submitted: (submitted.close(), future)[1],
            on_error=on_error,
        )
        future.set_exception(
            RuntimeError("active human-input correlation is missing")
        )
        self.assertEqual((message.acks, message.nacks), (0, 1))
        self.assertNotIn("m-missing-correlation", self.coordinator._inflight)

    def test_done_callback_does_not_raise_out_of_set_exception(self):
        future = Future()
        message = FakeMessage()
        self.coordinator.schedule(
            coro=work(),
            message=message,
            message_id="m-done-isolated",
            submit=lambda submitted: (submitted.close(), future)[1],
            on_error=lambda exc: (_ for _ in ()).throw(exc),
        )
        future.set_exception(RuntimeError("bound Job is not awaiting human input"))
        self.assertEqual(message.nacks, 1)

    def test_nack_failure_last_ditch_does_not_escape_or_wedge(self):
        future = Future()

        class PoisonNack:
            def __init__(self) -> None:
                self.acks = 0
                self.nacks = 0
                self.attempts = 0

            def ack(self) -> None:
                self.acks += 1

            def nack(self) -> None:
                self.attempts += 1
                self.nacks += 1
                if self.attempts == 1:
                    raise RuntimeError("subscriber nack failed")

        message = PoisonNack()
        self.coordinator.schedule(
            coro=work(),
            message=message,
            message_id="m-poison-nack",
            submit=lambda submitted: (submitted.close(), future)[1],
        )
        future.set_exception(RuntimeError("bound Job is not awaiting human input"))
        self.assertGreaterEqual(message.nacks, 1)
        self.assertNotIn("m-poison-nack", self.coordinator._inflight)

        nxt = Future()
        follow = FakeMessage()
        self.coordinator.schedule(
            coro=work(),
            message=follow,
            message_id="m-after-poison",
            submit=lambda submitted: (submitted.close(), nxt)[1],
        )
        nxt.set_result(None)
        self.assertEqual((follow.acks, follow.nacks), (1, 0))

    def test_ack_follows_durable_enqueue_not_worker_completion(self):
        """The handoff future settles while executable work is still queued."""
        with durable_temporary_directory() as tmp:
            queue = DurableChatEventQueue(Path(tmp) / "durable" / "jobs.db")
            future = Future()
            message = FakeMessage()
            submitted = []

            async def durable_handoff():
                queue.enqueue(
                    event_id="m-durable",
                    conversation_id="spaces/dm",
                    message_id="m-durable",
                    payload={"job_id": "job-1", "action_type": "browser.read"},
                    job_id="job-1",
                )

            self.coordinator.schedule(
                coro=durable_handoff(),
                message=message,
                message_id="m-durable",
                submit=lambda coro: (submitted.append(coro), future)[1],
            )
            self.assertEqual((message.acks, message.nacks), (0, 0))
            asyncio.run(submitted.pop())
            future.set_result(None)
            self.assertEqual((message.acks, message.nacks), (1, 0))
            self.assertEqual(queue.get("m-durable")["state"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
