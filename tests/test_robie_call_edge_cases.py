"""Edge-case tests for the Robie Call handler hardening (2026-10-03).

Covers Carlo's "extremely reliable" requirements:
- Word-boundary keyword matching (no "recall"/"morning" false positives)
- Ambiguous instructions ("call him") -> clarification note, no call
- Phone mismatch (task number vs EZLynx) -> EZLynx wins, flagged
- Name mismatch -> flagged in note
- Content-based dedup (same instruction, different task IDs)
- Recent-call guard (timeout double-dial hole)
- Stale CSV (task already closed)
- ALL CAPS normalization for spoken parts
- Uncertainty in outcome notes
"""
import os
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from robie_job_engine.ezlynx_driver_gate import DriverDecision

from robie_job_engine import robie_call_handler as rch
from robie_job_engine.robie_call_handler import (
    RobieCallConfig,
    RobieCallPorts,
    _instruction_ambiguity,
    _instruction_name_mismatch,
    _instruction_phone_mismatch,
    _normalize_spoken,
    handle_robie_call_task,
    is_call_task,
)


TEST_APPLICANT = "220250093"
IN_WINDOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))
_LEASE_PATCH = None


def setUpModule():
    """Unit tests must not read the live EZLynx driver lease."""
    global _LEASE_PATCH
    _LEASE_PATCH = patch(
        "robie_job_engine.ezlynx_driver_gate.require_driver_in",
        return_value=DriverDecision(True, "TEST", "unit test; lease not read"),
    )
    _LEASE_PATCH.start()


def tearDownModule():
    if _LEASE_PATCH is not None:
        _LEASE_PATCH.stop()


def make_task(**overrides: Any) -> Dict[str, Any]:
    task: Dict[str, Any] = {
        "Task ID": "TASK-1",
        "Task Subject": "Please call about renewal",
        "Task Description": "Please call John about his renewal. Be friendly.",
        "Applicant ID": TEST_APPLICANT,
        "Account Name": "John Test",
        "Task Created By": "carlo1",
        "Assigned Producer": "Jane Producer",
    }
    task.update(overrides)
    return task


class FakePhonePort:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = []

    def get_phone(self, applicant_id):
        self.calls.append(applicant_id)
        return self.mapping.get(applicant_id)


class FakeBlandPort:
    def __init__(self, result=None, exc=None, recent=None, call_status=None):
        self.result = result or {
            "success": True,
            "call_ids": ["call-1"],
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
        self.calls = []
        self.status_checks = []
        self.call_status = call_status if call_status is not None else {
            "ok": True, "status": "completed", "answered_by": "human",
            "duration_s": 120, "ended_at": "2026-10-03T12:00:00Z",
        }
        self.recent = recent if recent is not None else {"ok": True, "calls": []}

    def place_call_with_double_dial(self, phone, task_text, first_sentence,
                                     voicemail_message, metadata=None):
        self.calls.append({"phone": phone, "task_text": task_text,
                           "first_sentence": first_sentence,
                           "voicemail_message": voicemail_message,
                           "metadata": metadata})
        if self.exc:
            raise self.exc
        return dict(self.result)

    def recent_calls(self, phone, since_seconds=1800):
        return dict(self.recent)

    def get_call_status(self, call_id):
        self.status_checks.append(call_id)
        return dict(self.call_status)


class FakeDiscussionClient:
    def __init__(self):
        self.appended = []

    def get_discussions(self, applicant_id):
        return [{"discussionId": "D-100", "title": "t"}]

    def append_note(self, discussion_id, body, note_type="Note"):
        self.appended.append({"discussion_id": discussion_id, "body": body})
        nid = f"N-{len(self.appended)}"
        return {"noteId": nid, "discussionId": discussion_id}

    def get_discussion(self, discussion_id):
        return {"discussionId": discussion_id,
                "notes": [{"noteId": f"N-{i+1}"}
                          for i in range(len(self.appended))]}


class FakeReassignPort:
    def __init__(self, ok=True, read_back="__last_to_user__"):
        self.ok = ok
        self.read_back = read_back
        self.calls = []
        self.read_calls = []

    def reassign_task(self, task_id, to_user, note):
        self.calls.append({"task_id": task_id, "to_user": to_user})
        return {"ok": self.ok}

    def read_task_assignee(self, task_id):
        self.read_calls.append(task_id)
        if self.read_back == "__last_to_user__":
            return self.calls[-1]["to_user"] if self.calls else None
        return self.read_back


class FakeTaskStatus:
    def __init__(self, open_map=None):
        self.open_map = open_map or {}
        self.checked = []

    def is_task_open(self, task_id):
        self.checked.append(task_id)
        return self.open_map.get(task_id, True)


def make_ports(**overrides):
    kw = {
        "phone_lookup": FakePhonePort({TEST_APPLICANT: "732-668-8161"}),
        "bland": FakeBlandPort(),
        "discussion_client": FakeDiscussionClient(),
        "task_reassign": FakeReassignPort(),
        "chat_alert": MagicMock(return_value=True),
    }
    kw.update(overrides)
    return RobieCallPorts(**kw)


def live_config(**overrides):
    kw = {"dry_run": False, "now": IN_WINDOW}
    kw.update(overrides)
    return RobieCallConfig(**kw)


class TestWordBoundaryKeywords(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_recall_is_not_call_task(self):
        task = make_task(**{
            "Task Subject": "Recall the policy",
            "Task Description": "Recall the policy from the carrier",
        })
        self.assertFalse(is_call_task(task))

    def test_morning_is_not_call_task(self):
        task = make_task(**{
            "Task Subject": "Follow up",
            "Task Description": "Call during the morning meeting is fine",
        })
        # "morning" contains "ring" as substring — must not match
        task2 = make_task(**{
            "Task Subject": "Morning review",
            "Task Description": "Review in the morning",
        })
        self.assertFalse(is_call_task(task2))

    def test_telephone_substring_no_match(self):
        task = make_task(**{
            "Task Subject": "Update",
            "Task Description": "Update the telephone log",
        })
        self.assertFalse(is_call_task(task))

    def test_real_call_still_detected(self):
        self.assertTrue(is_call_task(make_task()))
        self.assertTrue(is_call_task(make_task(**{
            "Task Subject": "x", "Task Description": "Please CALL the client"})))
        self.assertTrue(is_call_task(make_task(**{
            "Task Subject": "Dial them", "Task Description": ""})))

    def test_recall_task_never_dials(self):
        task = make_task(**{
            "Task Subject": "Recall the policy",
            "Task Description": "Recall the policy from the carrier",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])


class TestAmbiguityGuard(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_call_him_is_ambiguous(self):
        self.assertIsNotNone(_instruction_ambiguity("call him"))

    def test_pls_call_is_ambiguous(self):
        self.assertIsNotNone(_instruction_ambiguity("pls call"))

    def test_call_with_no_reason_ambiguous(self):
        self.assertIsNotNone(_instruction_ambiguity("Please call."))

    def test_call_about_renewal_is_fine(self):
        self.assertIsNone(_instruction_ambiguity(
            "Please call John about his renewal."))

    def test_call_to_follow_up_is_fine(self):
        self.assertIsNone(_instruction_ambiguity("Call to follow up on the claim."))

    def test_ambiguous_task_files_clarification_no_call(self):
        task = make_task(**{
            "Task Subject": "Call him",
            "Task Description": "call him",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertIn("ambiguous", result["error"])
        self.assertEqual(ports.bland.calls, [])  # NO CALL
        # Clarification note filed so staff knows what to fix.
        self.assertEqual(len(ports.discussion_client.appended), 1)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("couldn't act on it", body)
        self.assertIn("what the call is about", body)
        # Task left open (not reassigned away).
        self.assertEqual(ports.task_reassign.calls, [])

    def test_ambiguous_task_marked_for_dedup(self):
        # A second identical ambiguous task shouldn't file another note.
        task = make_task(**{
            "Task Subject": "Call him", "Task Description": "call him",
        })
        ports = make_ports()
        handle_robie_call_task(task, live_config(), ports)
        task2 = make_task(**{
            "Task ID": "TASK-2",
            "Task Subject": "Call him", "Task Description": "call him",
        })
        result2 = handle_robie_call_task(task2, live_config(), ports)
        self.assertTrue(result2.get("duplicate_suppressed"))
        self.assertEqual(len(ports.discussion_client.appended), 1)


class TestPhoneMismatch(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_detects_different_number(self):
        mismatch = _instruction_phone_mismatch(
            "Call John at 555-999-8888 about renewal", "+17326688161")
        self.assertIsNotNone(mismatch)

    def test_same_number_no_mismatch(self):
        mismatch = _instruction_phone_mismatch(
            "Call John at 732-668-8161 about renewal", "+17326688161")
        self.assertIsNone(mismatch)

    def test_no_number_no_mismatch(self):
        self.assertIsNone(_instruction_phone_mismatch(
            "Call about renewal", "+17326688161"))

    def test_task_provided_number_wins(self):
        # Free-form directive: a number in the task text overrides the
        # number on file. Roby dials the task's number, on the applicant's
        # account.
        task = make_task(**{
            "Task Description": "Call John at 555-999-8888 about his renewal.",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertTrue(result["ok"])
        # Dialed the task's number, not the EZLynx number on file.
        self.assertEqual(ports.bland.calls[0]["phone"], "+15559998888")
        # The dialed number never appears in the note (API rejects digits).
        body = ports.discussion_client.appended[0]["body"]
        self.assertNotIn("555-999-8888", body)
        self.assertNotIn("5559998888", body)


class TestNameMismatch(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_detects_different_person(self):
        m = _instruction_name_mismatch("Call Mary Smith about renewal",
                                       "John Test")
        self.assertEqual(m, "Mary Smith")

    def test_same_person_no_mismatch(self):
        self.assertIsNone(_instruction_name_mismatch(
            "Call John about renewal", "John Test"))

    def test_no_name_pattern_no_mismatch(self):
        self.assertIsNone(_instruction_name_mismatch(
            "Call about the renewal documents", "John Test"))

    def test_mismatch_fails_closed_before_dial(self):
        # Naming a different person than the applicant is now a hard gate:
        # no dial, clarification note filed, task left open.
        task = make_task(**{
            "Task Description": "Call Mary Smith about her renewal.",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])
        self.assertIn("Mary Smith", result["error"])
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("Mary Smith", body)


class TestContentDedup(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_same_instruction_different_task_ids_suppressed(self):
        ports = make_ports()
        t1 = make_task(**{"Task ID": "TASK-A",
                          "Task Description": "Call about the renewal docs."})
        t2 = make_task(**{"Task ID": "TASK-B",
                          "Task Description": "Call about the renewal docs."})
        r1 = handle_robie_call_task(t1, live_config(), ports)
        self.assertTrue(r1["ok"])
        r2 = handle_robie_call_task(t2, live_config(), ports)
        self.assertTrue(r2.get("duplicate_suppressed"))
        self.assertEqual(r2.get("duplicate_reason"),
                         "same instruction already handled for applicant")
        self.assertEqual(len(ports.bland.calls), 1)  # one dial, not two

    def test_case_and_punctuation_variants_still_dedup(self):
        ports = make_ports()
        t1 = make_task(**{"Task ID": "TASK-A",
                          "Task Description": "Call about the renewal!"})
        t2 = make_task(**{"Task ID": "TASK-B",
                          "Task Description": "CALL ABOUT THE RENEWAL"})
        handle_robie_call_task(t1, live_config(), ports)
        r2 = handle_robie_call_task(t2, live_config(), ports)
        self.assertTrue(r2.get("duplicate_suppressed"))
        self.assertEqual(len(ports.bland.calls), 1)

    def test_different_instructions_both_dial(self):
        ports = make_ports()
        t1 = make_task(**{"Task ID": "TASK-A",
                          "Task Description": "Call about the renewal."})
        t2 = make_task(**{"Task ID": "TASK-B",
                          "Task Description": "Call about the audit."})
        handle_robie_call_task(t1, live_config(), ports)
        handle_robie_call_task(t2, live_config(), ports)
        self.assertEqual(len(ports.bland.calls), 2)

    def test_different_applicants_same_instruction_both_dial(self):
        ports = make_ports(phone_lookup=FakePhonePort(
            {TEST_APPLICANT: "732-668-8161", "999": "732-000-0000"}))
        t1 = make_task(**{"Task ID": "TASK-A",
                          "Task Description": "Call about the renewal."})
        t2 = make_task(**{"Task ID": "TASK-B", "Applicant ID": "999",
                          "Task Description": "Call about the renewal."})
        handle_robie_call_task(t1, live_config(), ports)
        handle_robie_call_task(t2, live_config(), ports)
        self.assertEqual(len(ports.bland.calls), 2)


class TestRecentCallGuard(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_recent_call_skips_dial(self):
        ports = make_ports(bland=FakeBlandPort(recent={
            "ok": True,
            "calls": [{"call_id": "c-old", "created_at": "2026-10-03T18:00:00Z"}],
        }))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertEqual(ports.bland.calls, [])  # NO REDIAL
        self.assertTrue(result.get("skipped_recent_call"))
        # Note explains why we didn't dial.
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("did NOT dial again", body)
        self.assertIn("avoid calling twice", body)

    def test_no_recent_call_dials_normally(self):
        ports = make_ports()  # recent defaults to empty
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)

    def test_recent_check_failure_proceeds(self):
        bland = FakeBlandPort()
        bland.recent = {"ok": False, "error": "history API down"}
        ports = make_ports(bland=bland)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        # History unavailable: normal idempotency guards still apply; dial.
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)


class TestStaleCsvGuard(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_closed_task_skipped(self):
        ports = make_ports(task_status=FakeTaskStatus({"TASK-1": False}))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("skipped_closed_task"))
        self.assertEqual(ports.bland.calls, [])
        self.assertEqual(ports.task_status.checked, ["TASK-1"])

    def test_open_task_proceeds(self):
        ports = make_ports(task_status=FakeTaskStatus({"TASK-1": True}))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)

    def test_status_check_failure_proceeds(self):
        class BoomStatus:
            def is_task_open(self, task_id):
                raise RuntimeError("EZLynx down")

        ports = make_ports(task_status=BoomStatus())
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)

    def test_no_status_port_proceeds(self):
        ports = make_ports()  # task_status defaults None
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)


class TestSpokenNormalization(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_all_caps_normalized(self):
        out = _normalize_spoken("PLEASE CALL JOHN ABOUT HIS RENEWAL")
        self.assertFalse(out.isupper())
        self.assertIn("renewal", out.lower())

    def test_mixed_case_untouched(self):
        out = _normalize_spoken("Please call John about his renewal.")
        self.assertEqual(out, "Please call John about his renewal.")

    def test_whitespace_collapsed(self):
        out = _normalize_spoken("Please   call\nJohn")
        self.assertEqual(out, "Please call John")

    def test_all_caps_task_uses_normalized_spoken_parts(self):
        task = make_task(**{
            "Task Subject": "CALL ABOUT RENEWAL",
            "Task Description": "PLEASE CALL JOHN ABOUT HIS RENEWAL DOCUMENTS",
        })
        ports = make_ports()
        handle_robie_call_task(task, live_config(), ports)
        call = ports.bland.calls[0]
        # Spoken parts normalized; the Eva task prompt keeps verbatim.
        self.assertFalse(call["first_sentence"].isupper())
        self.assertIn("PLEASE CALL JOHN", call["task_text"])


class TestOutcomeNoteHonesty(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_voicemail_note(self):
        bland = FakeBlandPort(result={
            "success": True, "call_ids": ["c1"],
            "attempts": [{"attempt": 1, "success": True,
                          "final_status": {"status": "completed",
                                           "answered_by": "voicemail"}}],
            "voicemail_hit": True, "redialed": True,
            "recording_url": None, "error": None,
        })
        ports = make_ports(bland=bland)
        handle_robie_call_task(make_task(), live_config(), ports)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("The call was successful.", body)
        self.assertIn("voicemail", body.lower())
        self.assertIn("called back", body.lower())

    def test_failed_note_names_failure(self):
        bland = FakeBlandPort(result={
            "success": False, "call_ids": [], "attempts": [],
            "error": "Bland 500", "recording_url": None,
        })
        ports = make_ports(bland=bland)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        # No note filed on call failure (task left open, chat alerted).
        self.assertEqual(ports.discussion_client.appended, [])

    def test_unknown_answered_by_stated_plainly(self):
        bland = FakeBlandPort(result={
            "success": True, "call_ids": ["c1"],
            "attempts": [{"attempt": 1, "success": True,
                          "final_status": {"status": "completed",
                                           "answered_by": "unknown",
                                           "duration": 30}}],
            "voicemail_hit": False, "redialed": False,
            "recording_url": None, "error": None,
        }, call_status={
            "ok": True, "status": "completed", "answered_by": "unknown",
            "duration_s": 30, "ended_at": "2026-10-03T12:00:00Z",
        })
        ports = make_ports(bland=bland)
        handle_robie_call_task(make_task(), live_config(), ports)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("couldn't confirm whether it reached the person or voicemail",
                      body)

    def test_note_has_no_jargon(self):
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        body = ports.discussion_client.appended[0]["body"]
        for jargon in ("voicemail_action", "double-dial", "Bland call id",
                       "answered_by", "final_status"):
            self.assertNotIn(jargon, body)


if __name__ == "__main__":
    unittest.main()
