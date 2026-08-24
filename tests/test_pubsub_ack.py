import asyncio
import unittest
from concurrent.futures import Future

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


if __name__ == "__main__":
    unittest.main()
