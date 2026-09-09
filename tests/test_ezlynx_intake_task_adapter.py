"""Synthetic tests for the durable EZLynx intake task coordinator."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.ezlynx_intake_task_adapter import (
    DurableEzlynxIntakeTaskAdapter,
)
from robie_job_engine.intake_core import IntakeHold, ReadResult


def read(*rows, authoritative=True, complete=True):
    return ReadResult(tuple(rows), authoritative, complete)


class Reader:
    pass


class Browser:
    def __init__(self):
        self.tasks = {}
        self.creates = 0
        self.raise_after_create = False
        self.raise_before_create = False

    def find_source_tasks(self, key):
        return read(*[row for row in self.tasks.values() if row["source_key"] == key])

    def find_related_work(self, applicant_id, policy_id, source):
        return read()

    def create_task(self, task):
        self.creates += 1
        if self.raise_before_create:
            raise TimeoutError("secret response must not escape")
        task_id = "task-" + str(self.creates)
        self.tasks[task_id] = dict(task, task_id=task_id)
        if self.raise_after_create:
            raise TimeoutError("secret response must not escape")

    def read_task(self, task_id):
        return read(*([self.tasks[task_id]] if task_id in self.tasks else []))


class DurableTaskAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.browser = Browser()
        self.db = Path(self.temp.name) / "durable" / "intake.sqlite3"
        self.adapter = DurableEzlynxIntakeTaskAdapter(Reader(), self.browser, self.db)
        self.task = {
            "source_key": "intake:abc123",
            "manual_upload_required": True,
            "applicant_id": "220250093",
            "assigned_user_id": "test-user",
        }

    def test_successful_create_is_durable_and_replayed_without_second_write(self):
        self.assertEqual(self.adapter.create_task_once(self.task), "task-1")
        restarted = DurableEzlynxIntakeTaskAdapter(Reader(), self.browser, self.db)
        self.assertEqual(restarted.create_task_once(self.task), "task-1")
        self.assertEqual(self.browser.creates, 1)

    def test_timeout_after_create_reconciles_on_restart_without_duplicate(self):
        self.browser.raise_after_create = True
        with self.assertRaisesRegex(IntakeHold, "uncertain"):
            self.adapter.create_task_once(self.task)
        self.browser.raise_after_create = False
        restarted = DurableEzlynxIntakeTaskAdapter(Reader(), self.browser, self.db)
        self.assertEqual(restarted.create_task_once(self.task), "task-1")
        self.assertEqual(self.browser.creates, 1)

    def test_timeout_before_create_never_becomes_permission_to_retry(self):
        self.browser.raise_before_create = True
        with self.assertRaisesRegex(IntakeHold, "uncertain"):
            self.adapter.create_task_once(self.task)
        self.browser.raise_before_create = False
        restarted = DurableEzlynxIntakeTaskAdapter(Reader(), self.browser, self.db)
        with self.assertRaisesRegex(IntakeHold, "reconcile"):
            restarted.create_task_once(self.task)
        self.assertEqual(self.browser.creates, 1)

    def test_conflicting_existing_task_is_held(self):
        self.browser.tasks["task-existing"] = dict(
            self.task, task_id="task-existing", assigned_user_id="other"
        )
        with self.assertRaisesRegex(IntakeHold, "uncertain"):
            self.adapter.create_task_once(self.task)
        self.assertEqual(self.browser.creates, 0)

    def test_incomplete_search_and_production_are_fail_closed(self):
        self.browser.find_source_tasks = lambda key: read(complete=False)
        with self.assertRaises(IntakeHold):
            self.adapter.create_task_once(self.task)
        with patch.dict("os.environ", {"ROBIE_ENV": "PRODUCTION"}):
            with self.assertRaises(IntakeHold):
                self.adapter.create_task_once(self.task)
        self.assertEqual(self.browser.creates, 0)

    def test_manual_upload_flag_and_source_key_are_required(self):
        for task in (
            dict(self.task, manual_upload_required=False),
            dict(self.task, source_key="message-1"),
        ):
            with self.assertRaises(IntakeHold):
                self.adapter.create_task_once(task)
        self.assertEqual(self.browser.creates, 0)


if __name__ == "__main__":
    unittest.main()
