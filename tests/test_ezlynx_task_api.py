"""Unit tests for direct EZLynx Task API via TaskCreationNote."""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from robie_job_engine.ezlynx_task_api import (
    EZLynxTaskAPI,
    TaskCreationRequest,
)


class TestTaskCreationRequest(unittest.TestCase):
    def test_to_task_creation_note(self):
        due = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
        req = TaskCreationRequest(
            applicant_id=26356199,
            assigned_user_id=12345,
            title="Call back Test User",
            description="Test description",
            due_date=due,
            priority="High",
        )
        note = req.to_task_creation_note()
        
        self.assertEqual(note["noteType"], "TaskCreationNote")
        self.assertEqual(note["applicantId"], 26356199)
        self.assertEqual(note["assignedUserId"], 12345)
        self.assertEqual(note["title"], "Call back Test User")
        self.assertEqual(note["description"], "Test description")
        self.assertEqual(note["priority"], "High")
        self.assertIn("2026-10-05", note["dueDate"])

    def test_default_priority(self):
        due = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
        req = TaskCreationRequest(
            applicant_id=1,
            assigned_user_id=2,
            title="Test",
            description="Test",
            due_date=due,
        )
        self.assertEqual(req.priority, "High")


class TestEZLynxTaskAPI(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.api = EZLynxTaskAPI(self.mock_client)

    def test_create_task_new_discussion(self):
        due = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
        req = TaskCreationRequest(
            applicant_id=26356199,
            assigned_user_id=12345,
            title="Test Task",
            description="Test Description",
            due_date=due,
        )
        
        self.mock_client._post.return_value = {"id": "disc123"}
        result = self.api.create_task_new_discussion(req)
        
        # Verify the correct endpoint was called
        self.mock_client._post.assert_called_once()
        call_args = self.mock_client._post.call_args
        self.assertEqual(call_args[0][0], "v8/discussions/with-note")
        
        # Verify payload structure
        payload = call_args[0][1]
        self.assertEqual(payload["applicantId"], 26356199)
        self.assertEqual(payload["note"]["noteType"], "TaskCreationNote")
        self.assertEqual(result, {"id": "disc123"})

    def test_create_task_existing_discussion(self):
        due = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
        req = TaskCreationRequest(
            applicant_id=26356199,
            assigned_user_id=12345,
            title="Test Task",
            description="Test Description",
            due_date=due,
        )
        
        self.mock_client._post.return_value = {"id": "note456"}
        result = self.api.create_task_existing_discussion("disc789", req)
        
        call_args = self.mock_client._post.call_args
        self.assertEqual(call_args[0][0], "v8/discussions/disc789/notes")
        self.assertEqual(result, {"id": "note456"})

    def test_create_callback_task(self):
        call_time = datetime(2026, 10, 2, 14, 30, 0, tzinfo=timezone.utc)
        
        self.mock_client._post.return_value = {"id": "disc999"}
        result = self.api.create_callback_task(
            applicant_id=26356199,
            assigned_user_id=12345,
            caller_name="John Doe",
            caller_phone="555-1234",
            call_time=call_time,
        )
        
        # Verify it created via new discussion
        call_args = self.mock_client._post.call_args
        self.assertEqual(call_args[0][0], "v8/discussions/with-note")
        
        payload = call_args[0][1]
        self.assertEqual(payload["applicantId"], 26356199)
        self.assertIn("John Doe", payload["note"]["title"])
        self.assertIn("555-1234", payload["note"]["description"])


if __name__ == "__main__":
    unittest.main()
