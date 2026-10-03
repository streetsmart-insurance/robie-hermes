"""Unit tests for the freeform Robie Call task handler.

All external I/O is faked. No real Bland calls, no real EZLynx writes.
"""

import os
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from robie_job_engine import robie_call_handler as rch
from robie_job_engine.robie_call_handler import (
    RobieCallConfig,
    RobieCallPorts,
    bland_payload_spec,
    handle_robie_call_task,
    is_call_task,
)


TEST_APPLICANT = "220250093"  # repo write-allowlist test applicant


def make_task(**overrides: Any) -> Dict[str, Any]:
    task: Dict[str, Any] = {
        "Task ID": "TASK-1",
        "Task Subject": "Please call about renewal",
        "Task Description": "Please call John about his renewal. Be friendly.",
        "Applicant ID": TEST_APPLICANT,
        "Account Name": "John Test",
        "Task Created By": "carlo1",
    }
    task.update(overrides)
    return task


class FakePhonePort:
    def __init__(self, mapping: Optional[Dict[str, Optional[str]]] = None):
        self.mapping = mapping or {}
        self.calls: List[str] = []

    def get_phone(self, applicant_id: str) -> Optional[str]:
        self.calls.append(applicant_id)
        return self.mapping.get(applicant_id)


class FakeBlandPort:
    def __init__(self, result: Optional[Dict[str, Any]] = None, exc: Optional[Exception] = None,
                 call_status: Optional[Dict[str, Any]] = None):
        self.result = result or {
            "success": True,
            "call_ids": ["call-1", "call-2"],
            "attempts": [{"attempt": 1, "success": True,
                          "final_status": {"status": "completed",
                                           "answered_by": "human",
                                           "duration": 120}}],
            "voicemail_hit": False,
            "redialed": False,
            "recording_url": None,
            "error": None,
        }
        self.exc = exc
        # Verified terminal status for outcome verification (Bland's own word).
        self.call_status = call_status if call_status is not None else {
            "ok": True, "status": "completed", "answered_by": "human",
            "duration_s": 120, "ended_at": "2026-10-03T12:00:00Z",
        }
        self.calls: List[Dict[str, Any]] = []
        self.status_checks: List[str] = []
        self.recent: Dict[str, Any] = {"ok": True, "calls": []}

    def place_call_with_double_dial(self, phone, task_text, first_sentence, voicemail_message, metadata=None):
        self.calls.append({
            "phone": phone, "task_text": task_text, "first_sentence": first_sentence,
            "voicemail_message": voicemail_message, "metadata": metadata,
        })
        if self.exc:
            raise self.exc
        return dict(self.result)

    def recent_calls(self, phone, since_seconds=1800):
        return dict(self.recent)

    def get_call_status(self, call_id: str) -> Dict[str, Any]:
        self.status_checks.append(call_id)
        return dict(self.call_status)


class FakeDiscussionClient:
    """Satisfies file_note_to_existing_discussion's client contract,
    including the read-back verification."""

    def __init__(self, discussions: Optional[List[Dict[str, Any]]] = None):
        self.discussions = discussions if discussions is not None else [
            {"discussionId": "D-100", "title": "Renewal chat"}
        ]
        self.appended: List[Dict[str, Any]] = []
        self._notes: Dict[str, List[str]] = {}

    def get_discussions(self, applicant_id: str):
        return list(self.discussions)

    def append_note(self, discussion_id: str, body: str, note_type: str = "Note"):
        self.appended.append({"discussion_id": discussion_id, "body": body})
        note_id = f"N-{len(self.appended)}"
        self._notes.setdefault(discussion_id, []).append(note_id)
        return {"noteId": note_id, "discussionId": discussion_id}

    def get_discussion(self, discussion_id: str):
        return {
            "discussionId": discussion_id,
            "notes": [{"noteId": nid} for nid in self._notes.get(discussion_id, [])],
        }


class FakeReassignPort:
    def __init__(self, ok: bool = True, read_back: Optional[str] = "__last_to_user__"):
        self.ok = ok
        # What read_task_assignee returns. "__last_to_user__" echoes the user
        # from the most recent reassign_task call (the honest fake); pass a
        # name to simulate mismatch, or None to simulate an unreadable task.
        self.read_back = read_back
        self.calls: List[Dict[str, Any]] = []
        self.read_calls: List[str] = []

    def reassign_task(self, task_id: str, to_user: str, note: str):
        self.calls.append({"task_id": task_id, "to_user": to_user, "note": note})
        return {"ok": self.ok, "error": None if self.ok else "boom"}

    def read_task_assignee(self, task_id: str) -> Optional[str]:
        self.read_calls.append(task_id)
        if self.read_back == "__last_to_user__":
            return self.calls[-1]["to_user"] if self.calls else None
        return self.read_back


def make_ports(**overrides: Any) -> RobieCallPorts:
    kw: Dict[str, Any] = {
        "phone_lookup": FakePhonePort({TEST_APPLICANT: "732-668-8161"}),
        "bland": FakeBlandPort(),
        "discussion_client": FakeDiscussionClient(),
        "task_reassign": FakeReassignPort(),
        "chat_alert": MagicMock(return_value=True),
    }
    kw.update(overrides)
    return RobieCallPorts(**kw)


def live_config(**overrides: Any) -> RobieCallConfig:
    kw: Dict[str, Any] = {"dry_run": False}
    kw.update(overrides)
    return RobieCallConfig(**kw)


class TestIsCallTask(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_detects_call_keyword(self):
        self.assertTrue(is_call_task(make_task()))

    def test_detects_phone_keyword(self):
        self.assertTrue(is_call_task(make_task(**{
            "Task Subject": "Follow up", "Task Description": "Phone the client today",
        })))

    def test_detects_dial_keyword(self):
        self.assertTrue(is_call_task(make_task(**{
            "Task Subject": "Dial them", "Task Description": "",
        })))

    def test_non_call_task(self):
        self.assertFalse(is_call_task(make_task(**{
            "Task Subject": "File paperwork", "Task Description": "Scan the dec page",
        })))

    def test_empty_task(self):
        self.assertFalse(is_call_task({}))

    def test_alternate_header_names(self):
        task = {
            "ID": "T-9", "Title": "Ring client",
            "Notes": "Give them a ring about billing",
            "ApplicantID": TEST_APPLICANT,
        }
        self.assertTrue(is_call_task(task))


class TestValidation(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_missing_task_id_fails_closed(self):
        task = make_task(**{"Task ID": ""})
        result = handle_robie_call_task(task, live_config(), make_ports())
        self.assertFalse(result["ok"])
        self.assertIn("task_id", result["error"])

    def test_missing_applicant_id_fails_closed(self):
        task = make_task(**{"Applicant ID": ""})
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertIn("applicant", result["error"])
        self.assertEqual(ports.bland.calls, [])  # no call placed

    def test_non_call_task_skipped(self):
        task = make_task(**{
            "Task Subject": "File paperwork", "Task Description": "Scan the dec page",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])

    def test_never_raises_on_garbage(self):
        result = handle_robie_call_task({}, live_config(), make_ports())
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["error"])


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()

    def tearDown(self):
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_kill_switch_fails_closed(self):
        os.environ[rch.KILL_SWITCH_ENV] = "1"
        ports = make_ports()
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertIn("kill switch", result["error"])
        self.assertEqual(ports.bland.calls, [])

    def test_kill_switch_off_allows(self):
        os.environ[rch.KILL_SWITCH_ENV] = "0"
        ports = make_ports()
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)


class TestPhoneLookup(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_no_phone_fails_closed(self):
        ports = make_ports(phone_lookup=FakePhonePort({}))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertIn("phone", result["error"])
        self.assertEqual(ports.bland.calls, [])

    def test_garbage_phone_fails_closed(self):
        ports = make_ports(phone_lookup=FakePhonePort({TEST_APPLICANT: "N/A"}))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])

    def test_phone_normalized_to_e164(self):
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        self.assertEqual(ports.bland.calls[0]["phone"], "+17326688161")

    def test_phone_lookup_exception_fails_closed(self):
        class BoomPhone:
            def get_phone(self, applicant_id):
                raise ConnectionError("down")

        ports = make_ports(phone_lookup=BoomPhone())
        # retries x3 then fail — patch sleep to keep the test fast
        orig_sleep = rch.time.sleep
        rch.time.sleep = lambda s: None
        try:
            result = handle_robie_call_task(make_task(), live_config(), ports)
        finally:
            rch.time.sleep = orig_sleep
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])

class TestBlandCall(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_success_end_to_end(self):
        ports = make_ports()
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)
        # writeback went through the repo-standard path
        self.assertEqual(len(ports.discussion_client.appended), 1)
        self.assertEqual(ports.discussion_client.appended[0]["discussion_id"], "D-100")
        # reassigned to the original assigner
        self.assertTrue(result["reassigned"])
        self.assertEqual(ports.task_reassign.calls[0]["to_user"], "carlo1")
        self.assertEqual(ports.task_reassign.calls[0]["task_id"], "TASK-1")

    def test_eva_task_has_ai_disclosure(self):
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        task_text = ports.bland.calls[0]["task_text"]
        self.assertIn("AI assistant", task_text)
        self.assertIn("Jake", task_text)
        self.assertIn("StreetSmart Insurance", task_text)
        self.assertIn("Please call John about his renewal", task_text)

    def test_first_sentence_and_voicemail(self):
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        call = ports.bland.calls[0]
        self.assertIn("AI assistant", call["first_sentence"])
        self.assertIn("7 3 2", call["voicemail_message"])  # digit-by-digit callback

    def test_bland_failure_leaves_task_open_no_reassign(self):
        ports = make_ports(bland=FakeBlandPort(
            result={"success": False, "error": "Bland 500", "call_ids": []}
        ))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertIn("Bland", result["error"])
        # NOT reassigned — a human must see it
        self.assertFalse(result["reassigned"])
        self.assertEqual(ports.task_reassign.calls, [])
        # chat alerted
        self.assertTrue(result["chat_alerted"])
        ports.chat_alert.assert_called()
        alert_text = ports.chat_alert.call_args[0][0]
        self.assertIn("FAILED", alert_text)
        self.assertIn("OPEN", alert_text)

    def test_bland_exception_single_attempt_no_retry_spam(self):
        ports = make_ports(bland=FakeBlandPort(exc=TimeoutError("hung")))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        # exactly one attempt — no POST retry (Bland has no idempotency key)
        self.assertEqual(len(ports.bland.calls), 1)

    def test_dry_run_default_places_no_call(self):
        ports = make_ports()
        result = handle_robie_call_task(make_task(), RobieCallConfig(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(ports.bland.calls, [])
        self.assertTrue(result["call"]["dry_run"])


class TestWriteback(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_writeback_failure_alerts_immediately(self):
        class FailingClient(FakeDiscussionClient):
            def append_note(self, discussion_id, body, note_type="Note"):
                raise RuntimeError("DiscussionApi 500")

        ports = make_ports(discussion_client=FailingClient())
        result = handle_robie_call_task(make_task(), live_config(), ports)
        # call succeeded, note failed -> ok False + immediate chat alert
        self.assertFalse(result["ok"])
        self.assertTrue(result["chat_alerted"])
        alert_text = ports.chat_alert.call_args[0][0]
        self.assertIn("writeback FAILED", alert_text)
        self.assertIn(TEST_APPLICANT, alert_text)
        # task NOT reassigned on writeback failure (record is incomplete)
        self.assertFalse(result["reassigned"])

    def test_outcome_note_has_no_phone_number(self):
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        body = ports.discussion_client.appended[0]["body"]
        # repo rule: notes must not contain dialable numbers
        import re
        self.assertIsNone(re.search(r"\d{3}[-.\s]?\d{3}[-.\s]?\d{4}", body))

    def test_outcome_note_names_applicant_plain_english(self):
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("John Test", body)
        self.assertIn("The call was successful.", body)
        # No call IDs, no jargon in staff-facing notes.
        self.assertNotIn("call-1", body)
        self.assertNotIn("Bland call id", body)

    def test_recording_failure_noted_not_silent(self):
        class FailUpload:
            def upload_recording(self, applicant_id, name, url):
                return {"ok": False, "error": "DocumentApi 500"}

        ports = make_ports(recording_upload=FailUpload())
        ports.bland.result["recording_url"] = "https://example.com/rec.mp3"
        handle_robie_call_task(make_task(), live_config(), ports)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("not available", body)


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_duplicate_task_suppressed(self):
        ports = make_ports()
        first = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(first["ok"])
        self.assertEqual(len(ports.bland.calls), 1)
        second = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(second["ok"])
        self.assertTrue(second.get("duplicate_suppressed"))
        # still exactly one call — no double dial to the client
        self.assertEqual(len(ports.bland.calls), 1)

    def test_different_task_ids_both_processed(self):
        ports = make_ports()
        handle_robie_call_task(
            make_task(**{"Task ID": "TASK-1",
                         "Task Description": "Call about the renewal"}),
            live_config(), ports)
        handle_robie_call_task(
            make_task(**{"Task ID": "TASK-2",
                         "Task Description": "Call about the audit"}),
            live_config(), ports)
        self.assertEqual(len(ports.bland.calls), 2)


class TestReassignment(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_no_reassign_port_degrades_gracefully(self):
        ports = make_ports(task_reassign=None)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        # writeback ok -> overall ok, but flagged for manual reassignment
        self.assertTrue(result["ok"])
        self.assertFalse(result["reassigned"])
        self.assertIn("manual reassignment", result["reassign_error"])

    def test_reassign_failure_does_not_flip_ok(self):
        ports = make_ports(task_reassign=FakeReassignPort(ok=False))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])  # call + note succeeded
        self.assertFalse(result["reassigned"])
        self.assertIsNotNone(result["reassign_error"])

    def test_no_assigned_by_skips_reassign(self):
        ports = make_ports()
        result = handle_robie_call_task(
            make_task(**{"Task Created By": ""}), live_config(), ports
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["reassigned"])
        self.assertEqual(ports.task_reassign.calls, [])


class TestInstructionHandling(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_instruction_capped(self):
        ports = make_ports()
        long_desc = "Please call. " + ("x" * 5000)
        handle_robie_call_task(make_task(**{"Task Description": long_desc}), live_config(), ports)
        task_text = ports.bland.calls[0]["task_text"]
        self.assertLessEqual(len(task_text), 2000 + 600)  # instruction cap + wrapper

    def test_subject_used_when_no_description(self):
        ports = make_ports()
        handle_robie_call_task(
            make_task(**{"Task Description": "", "Task Subject": "Call about renewal"}),
            live_config(), ports,
        )
        self.assertEqual(len(ports.bland.calls), 1)

    def test_empty_instruction_fails_closed(self):
        ports = make_ports()
        result = handle_robie_call_task(
            make_task(**{"Task Description": "", "Task Subject": "Paperwork"}),
            live_config(), ports,
        )
        # not a call task at all -> skipped before instruction check
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])


class TestBlandPayloadSpec(unittest.TestCase):
    def test_attempt1_silent_hangup(self):
        p = bland_payload_spec("+17326688161", "task", "first", "vm", 1)
        self.assertEqual(p["voicemail_action"], "hangup")
        self.assertNotIn("voicemail_message", p)
        self.assertEqual(p["voice"], "29158307-9893-4149-8a75-bc9ce313d64e")
        self.assertEqual(p["from"], "+17322986745")
        self.assertEqual(p["transfer_phone_number"], "+17324622360")
        self.assertFalse(p["record"])

    def test_attempt2_leaves_message(self):
        p = bland_payload_spec("+17326688161", "task", "first", "slow message", 2)
        self.assertEqual(p["voicemail_action"], "leave_message")
        self.assertEqual(p["voicemail_message"], "slow message")


if __name__ == "__main__":
    unittest.main()
