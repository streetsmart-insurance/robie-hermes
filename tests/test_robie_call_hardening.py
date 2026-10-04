"""Hardening tests for the Robie Call handler: crash recovery, ambiguous
phones, outcome verification, and reassignment read-back.

All external I/O is faked. No real Bland calls, no real EZLynx writes.
Covers Carlo's focus areas: no duplicate calls after retries/restarts,
recovery after interruptions, missing/ambiguous phones, verified call
outcomes, and reassignment read-back.
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
    handle_robie_call_task,
)


TEST_APPLICANT = "220250093"
IN_WINDOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))
_LEASE_PATCH = None
TERMINAL_STATUS = {
    "ok": True, "status": "completed", "answered_by": "human",
    "duration_s": 120, "ended_at": "2026-10-03T12:00:00Z",
}


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
        "Activity Labels": "Robie Call",
    }
    task.update(overrides)
    return task


class FakeCheckpointPort:
    """Durable checkpoint stand-in (survives _reset_module_state_for_tests,
    like a real Job Engine job row would survive a process restart)."""

    def __init__(self, initial: Optional[Dict[str, Dict[str, Any]]] = None):
        self.store: Dict[str, Dict[str, Any]] = dict(initial or {})
        self.sets: List[tuple] = []

    def get_checkpoint(self, key: str) -> Dict[str, Any]:
        return dict(self.store.get(key, {}))

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        self.sets.append((key, dict(value)))
        self.store[key] = dict(value)


class FakePhonePort:
    def __init__(self, answer: Any = "+17326688161"):
        self.answer = answer
        self.calls: List[str] = []

    def get_phone(self, applicant_id: str) -> Any:
        self.calls.append(applicant_id)
        return self.answer


class FakeBlandPort:
    def __init__(self, result: Optional[Dict[str, Any]] = None,
                 statuses: Optional[Dict[str, Dict[str, Any]]] = None):
        self.result = result or {
            "success": True, "call_ids": ["call-1"],
            "attempts": [], "voicemail_hit": False, "redialed": False,
            "recording_url": None, "error": None,
        }
        self.statuses = statuses or {}
        self.calls: List[Dict[str, Any]] = []
        self.status_checks: List[str] = []

    def place_call_with_double_dial(self, phone, task_text, first_sentence,
                                    voicemail_message, metadata=None):
        self.calls.append({"phone": phone, "metadata": metadata})
        return dict(self.result)

    def recent_calls(self, phone, since_seconds=1800):
        return {"ok": True, "calls": []}

    def get_call_status(self, call_id: str) -> Dict[str, Any]:
        self.status_checks.append(call_id)
        return dict(self.statuses.get(call_id, TERMINAL_STATUS))


class BlandPortNoStatus(FakeBlandPort):
    """Port without a status API: outcome can never be verified."""
    get_call_status = None  # type: ignore[assignment]


class FakeDiscussionClient:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.appended: List[Dict[str, Any]] = []

    def get_discussions(self, applicant_id: str):
        return [{"discussionId": "D-100", "title": "t"}]

    def append_note(self, discussion_id: str, body: str, note_type: str = "Note"):
        if self.fail:
            raise RuntimeError("DiscussionApi 500")
        self.appended.append({"discussion_id": discussion_id, "body": body})
        nid = f"N-{len(self.appended)}"
        return {"noteId": nid, "discussionId": discussion_id}

    def get_discussion(self, discussion_id: str):
        return {"discussionId": discussion_id,
                "notes": [{"noteId": f"N-{i+1}"}
                          for i in range(len(self.appended))]}


class FakeReassignPort:
    def __init__(self, ok: bool = True, read_back: Any = "__last_to_user__"):
        self.ok = ok
        self.read_back = read_back
        self.calls: List[Dict[str, Any]] = []
        self.read_calls: List[str] = []

    def reassign_task(self, task_id: str, to_user: str, note: str):
        self.calls.append({"task_id": task_id, "to_user": to_user})
        return {"ok": self.ok}

    def read_task_assignee(self, task_id: str) -> Optional[str]:
        self.read_calls.append(task_id)
        if self.read_back == "__last_to_user__":
            return self.calls[-1]["to_user"] if self.calls else None
        return self.read_back


class FakeReassignPortNoReadback(FakeReassignPort):
    """Port without the read-back method: getattr(..., None) -> None."""
    read_task_assignee = None  # type: ignore[assignment]


def make_ports(**overrides: Any) -> RobieCallPorts:
    kw: Dict[str, Any] = {
        "phone_lookup": FakePhonePort(),
        "bland": FakeBlandPort(),
        "discussion_client": FakeDiscussionClient(),
        "task_reassign": FakeReassignPort(),
        "chat_alert": MagicMock(return_value=True),
        "job_checkpoint": FakeCheckpointPort(),
    }
    kw.update(overrides)
    return RobieCallPorts(**kw)


def live_config(**overrides: Any) -> RobieCallConfig:
    kw: Dict[str, Any] = {
        "dry_run": False,
        "now": IN_WINDOW,
        "outcome_poll_tries": 1,
        "outcome_poll_interval_s": 0,
    }
    kw.update(overrides)
    return RobieCallConfig(**kw)


class TestCrashRecovery(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_checkpoint_saved_immediately_after_dial(self):
        """Even when the writeback fails, the call_ids are checkpointed —
        a crash between dial and note can never redial."""
        ports = make_ports(discussion_client=FakeDiscussionClient(fail=True))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])  # writeback failed
        stored = ports.job_checkpoint.store.get("robie-call:TASK-1", {})
        self.assertEqual(stored.get("bland_call_ids"), ["call-1"])
        self.assertIsNone(stored.get("completed_at"))

    def test_restart_reconciles_instead_of_redialing(self):
        """Simulated restart: in-memory state wiped, checkpoint survives.
        The handler must NOT dial again; it reconciles the known call."""
        checkpoint = FakeCheckpointPort({
            "robie-call:TASK-1": {
                "bland_call_ids": ["call-9"],
                "phone": "+17326688161",
                "completed_at": None,
            }
        })
        ports = make_ports(job_checkpoint=checkpoint)
        rch._reset_module_state_for_tests()  # simulate the restart
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertEqual(ports.bland.calls, [])  # never dialed
        self.assertTrue(result.get("recovered_from_checkpoint"))
        self.assertTrue(result["outcome_verified"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.discussion_client.appended), 1)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("recovered", body.lower())
        self.assertIn("did NOT call again", body)

    def test_recovery_with_unverifiable_outcome_fails_closed(self):
        checkpoint = FakeCheckpointPort({
            "robie-call:TASK-1": {
                "bland_call_ids": ["call-9"],
                "phone": "+17326688161",
                "completed_at": None,
            }
        })
        bland = FakeBlandPort(statuses={
            "call-9": {"ok": True, "status": "in-progress",
                       "answered_by": None, "duration_s": 0},
        })
        ports = make_ports(job_checkpoint=checkpoint, bland=bland)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertEqual(ports.bland.calls, [])
        self.assertFalse(result["ok"])
        self.assertIn("unverifiable", result["error"])
        self.assertEqual(ports.task_reassign.calls, [])  # no reassign
        self.assertTrue(result["chat_alerted"])

    def test_completed_checkpoint_suppresses_rerun(self):
        checkpoint = FakeCheckpointPort({
            "robie-call:TASK-1": {
                "bland_call_ids": ["call-1"],
                "phone": "+17326688161",
                "completed_at": "2026-10-03T12:00:00Z",
            }
        })
        ports = make_ports(job_checkpoint=checkpoint)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result.get("duplicate_suppressed"))
        self.assertEqual(ports.bland.calls, [])
        self.assertEqual(ports.discussion_client.appended, [])

    def test_inflight_claim_suppresses_concurrent_duplicate(self):
        ports = make_ports()
        self.assertTrue(rch._claim_inflight("TASK-1"))
        try:
            result = handle_robie_call_task(make_task(), live_config(), ports)
        finally:
            rch._release_inflight("TASK-1")
        self.assertTrue(result.get("duplicate_suppressed"))
        self.assertEqual(ports.bland.calls, [])


class TestAmbiguousPhone(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_ambiguous_phone_fails_closed_no_dial(self):
        phone = FakePhonePort(answer={
            "phone": None, "ambiguous": True,
            "candidates": [{"label": "Cell"}, {"label": "Business"}],
        })
        ports = make_ports(phone_lookup=phone)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("phone_ambiguous"))
        self.assertEqual(ports.bland.calls, [])  # never dialed
        self.assertTrue(result.get("clarification_note_filed"))
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("Cell", body)
        # Notes must never contain digits.
        import re
        self.assertIsNone(re.search(r"\d{7,}", body))

    def test_dict_phone_clear_result_dials(self):
        phone = FakePhonePort(answer={"phone": "(732) 668-8161", "ambiguous": False})
        ports = make_ports(phone_lookup=phone)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(ports.bland.calls[0]["phone"], "+17326688161")


class TestOutcomeVerification(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_verified_terminal_status_marks_ok(self):
        ports = make_ports()
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["outcome_verified"])
        self.assertTrue(result["outcome_verification_available"])
        self.assertTrue(result["ok"])
        self.assertEqual(ports.bland.status_checks, ["call-1"])

    def test_unverifiable_outcome_fails_closed(self):
        bland = FakeBlandPort(statuses={
            "call-1": {"ok": True, "status": "in-progress",
                       "answered_by": None, "duration_s": 5},
        })
        ports = make_ports(bland=bland)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["outcome_verified"])
        self.assertFalse(result["ok"])
        self.assertIn("could not be verified", result["error"])
        # Note filed honestly, but no reassignment and a chat alert.
        self.assertEqual(len(ports.discussion_client.appended), 1)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("couldn't confirm", body)
        self.assertEqual(ports.task_reassign.calls, [])
        self.assertTrue(result["chat_alerted"])

    def test_no_verification_opt_out_exists(self):
        # The waiver is gone: there is no flag that lets a placement ack
        # alone mark the task ok. Constructing with the old flag fails
        # loudly instead of silently ignoring it.
        with self.assertRaises(TypeError):
            live_config(require_outcome_verification=False)  # type: ignore[call-arg]

    def test_placement_ack_alone_never_marks_ok(self):
        # Bland accepted the dial (success=True) but has no status API, so
        # the outcome cannot be confirmed. The task must NOT be ok — an
        # HTTP 200 is not proof the call happened.
        ports = make_ports(bland=BlandPortNoStatus())
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertFalse(result["outcome_verified"])
        self.assertFalse(result["outcome_verification_available"])
        self.assertIn("could not be verified", result["error"])

    def test_no_status_api_fails_closed_by_default(self):
        ports = make_ports(bland=BlandPortNoStatus())
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertIn("could not be verified", result["error"])


class TestReassignmentReadback(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_readback_match_marks_reassigned(self):
        ports = make_ports()
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["reassigned"])
        self.assertIsNone(result["reassign_error"])
        # Two reads: ownership pre-check before reassigning + read-back after.
        self.assertEqual(ports.task_reassign.read_calls, ["TASK-1", "TASK-1"])

    def test_readback_mismatch_alerts_not_reassigned(self):
        # Owned by someone else: the ownership pre-check refuses BEFORE any
        # reassign attempt (stronger than the old read-back-mismatch path).
        ports = make_ports(task_reassign=FakeReassignPort(read_back="someone_else"))
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["reassigned"])
        self.assertIn("refusing to reassign", result["reassign_error"])
        self.assertEqual(ports.task_reassign.calls, [])
        self.assertTrue(result["chat_alerted"])
        # Call + note still ok; only the routing is flagged.
        self.assertTrue(result["ok"])

    def test_readback_mismatch_after_reassign_alerts(self):
        # Owned by Robie at pre-check, but the post-reassign read-back shows
        # a different assignee: mismatch alert, not reassigned.
        port = FakeReassignPort(read_back="__last_to_user__")
        orig_read = port.read_task_assignee
        calls = {"n": 0}
        def flaky_read(task_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return "Robie AI"  # pre-check passes
            return "someone_else"  # post-reassign read-back mismatches
        port.read_task_assignee = flaky_read
        ports = make_ports(task_reassign=port)
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["reassigned"])
        self.assertIn("reads back", result["reassign_error"])
        self.assertTrue(result["chat_alerted"])
        self.assertTrue(result["ok"])

    def test_no_readback_never_claims_reassigned(self):
        ports = make_ports(task_reassign=FakeReassignPortNoReadback())
        result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertFalse(result["reassigned"])
        self.assertIn("NOT verified", result["reassign_error"])
        self.assertTrue(result["ok"])  # call + note succeeded


if __name__ == "__main__":
    unittest.main()
