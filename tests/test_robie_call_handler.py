"""Unit tests for the freeform Robie Call task handler.

All external I/O is faked. No real Bland calls, no real EZLynx writes.
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
    bland_payload_spec,
    handle_robie_call_task,
    is_call_task,
)


TEST_APPLICANT = "220250093"  # repo write-allowlist test applicant
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
        "Task Description": "Please call John about his renewal. Be friendly. Call at 732-668-8161.",
        "Applicant ID": TEST_APPLICANT,
        "Account Name": "John Test",
        "Task Created By": "carlo1",
        "Assigned Producer": "Jane Producer",
        "Activity Labels": "Robie Call",
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
    kw: Dict[str, Any] = {"dry_run": False, "now": IN_WINDOW}
    kw.update(overrides)
    return RobieCallConfig(**kw)


class TestIsCallTask(unittest.TestCase):
    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)

    def test_detects_call_keyword(self):
        self.assertTrue(is_call_task(make_task()))
        self.assertFalse(is_call_task(make_task(**{"Activity Labels": ""})))

    def test_detects_phone_keyword(self):
        self.assertFalse(is_call_task(make_task(**{
            "Activity Labels": "",
            "Task Subject": "Follow up", "Task Description": "Phone the client today",
        })))

    def test_detects_dial_keyword(self):
        self.assertFalse(is_call_task(make_task(**{
            "Activity Labels": "",
            "Task Subject": "Dial them", "Task Description": "",
        })))

    def test_non_call_task(self):
        self.assertFalse(is_call_task(make_task(**{
            "Activity Labels": "",
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
        self.assertFalse(is_call_task(task))
        task["Activity Labels"] = "Robie Call"
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
            "Activity Labels": "",
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
        task = make_task(**{
            "Task Description": "Please call John about his renewal. Be friendly.",
        })
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertIn("phone", result["error"])
        self.assertEqual(ports.phone_lookup.calls, [])
        self.assertEqual(ports.bland.calls, [])
        self.assertIn(
            "Make a new Robie Call task with the number to call.",
            ports.discussion_client.appended[0]["body"],
        )

    def test_garbage_phone_fails_closed(self):
        ports = make_ports(phone_lookup=FakePhonePort({TEST_APPLICANT: "N/A"}))
        task = make_task(**{
            "Activity Labels": "Robie Lead Follow Up",
            "Task Description": "The lead asked about a homeowners quote.",
        })
        result = handle_robie_call_task(task, live_config(), ports)
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
            result = handle_robie_call_task(make_task(**{
                "Activity Labels": "Robie Lead Follow Up",
                "Task Description": "The lead asked about a homeowners quote.",
            }), live_config(), ports)
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
        self.assertIn("on behalf of Jane Producer", task_text)
        self.assertNotIn("Jake", task_text)
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
        result = handle_robie_call_task(
            make_task(), RobieCallConfig(now=IN_WINDOW), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(ports.bland.calls, [])
        self.assertTrue(result["call"]["dry_run"])
        self.assertEqual(ports.discussion_client.appended, [])
        self.assertNotIn("lost track", str(result))


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

    def test_real_client_outcome_note_uses_the_all_clients_scope(self):
        applicant = "80026158"
        for key in ("ROBIE_EZLYNX_WRITE_SCOPE", "ROBIE_PLAYGROUND"):
            os.environ.pop(key, None)
        ports = make_ports(phone_lookup=FakePhonePort({applicant: "732-668-8161"}))
        refused = handle_robie_call_task(
            make_task(**{"Applicant ID": applicant, "Account Name": "Avery Sample"}),
            live_config(),
            ports,
        )
        self.assertFalse(refused["ok"])
        self.assertEqual(ports.discussion_client.appended, [])
        self.assertIn(
            "EZLYNX_WRITE_SCOPE_REFUSED",
            f"{refused.get('error')} {refused.get('writeback')}",
        )

        os.environ["ROBIE_EZLYNX_WRITE_SCOPE"] = "all"
        os.environ["ROBIE_PLAYGROUND"] = "1"
        try:
            from robie_job_engine.playground_guardrails import classify_playground_request

            blocked = classify_playground_request("delete the policy")
            self.assertTrue(blocked.blocked)
            self.assertEqual(blocked.code, "delete_or_cancel")
            rch._reset_module_state_for_tests()
            ports = make_ports(phone_lookup=FakePhonePort({applicant: "732-668-8161"}))
            filed = handle_robie_call_task(
                make_task(**{
                    "Applicant ID": applicant,
                    "Task ID": "TASK-REAL",
                    "Account Name": "Avery Sample",
                    "Task Description": (
                        "Please call about the renewal. Be friendly. "
                        "Call at 732-668-8161."
                    ),
                }),
                live_config(),
                ports,
            )
            self.assertTrue(filed["ok"], filed.get("error"))
            self.assertEqual(len(ports.discussion_client.appended), 1)
            body = ports.discussion_client.appended[0]["body"]
            self.assertIn("Avery Sample", body)
            self.assertIn("They answered and I talked to them.", body)
            self.assertNotIn("The call was successful.", body)
            self.assertNotIn("..", body)
            self.assertNotEqual(applicant, "220250093")
        finally:
            os.environ.pop("ROBIE_EZLYNX_WRITE_SCOPE", None)
            os.environ.pop("ROBIE_PLAYGROUND", None)

    def test_outcome_note_names_applicant_plain_english(self):
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("John Test", body)
        self.assertIn("They answered and I talked to them.", body)
        self.assertNotIn("The call was successful.", body)
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
                         "Task Description": "Call about the renewal. Call at 732-668-8161."}),
            live_config(), ports)
        handle_robie_call_task(
            make_task(**{"Task ID": "TASK-2",
                         "Task Description": "Call about the audit. Call at 732-668-8161."}),
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
        long_desc = "Please call at 732-668-8161. " + ("x" * 5000)
        handle_robie_call_task(make_task(**{"Task Description": long_desc}), live_config(), ports)
        task_text = ports.bland.calls[0]["task_text"]
        self.assertLessEqual(len(task_text), 2000 + 600)  # instruction cap + wrapper

    def test_subject_used_when_no_description(self):
        ports = make_ports()
        handle_robie_call_task(
            make_task(**{
                "Task Description": "",
                "Task Subject": "Call about renewal. Call at 732-668-8161.",
            }),
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
        self.assertNotIn("transfer_phone_number", p)
        self.assertFalse(p["record"])

    def test_attempt2_leaves_message(self):
        p = bland_payload_spec("+17326688161", "task", "first", "slow message", 2)
        self.assertEqual(p["voicemail_action"], "leave_message")
        self.assertEqual(p["voicemail_message"], "slow message")


class _MemCheckpoint:
    def __init__(self):
        self.store: Dict[str, Dict[str, Any]] = {}

    def get_checkpoint(self, key: str) -> Dict[str, Any]:
        return dict(self.store.get(key, {}))

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        self.store[key] = dict(value)


class TestCallRequestRound(unittest.TestCase):
    """Regressions from the Test round of main 80a36b0 (PR #758)."""

    def setUp(self):
        rch._reset_module_state_for_tests()
        os.environ.pop(rch.KILL_SWITCH_ENV, None)
        os.environ.pop(rch.CALL_WINDOW_START_ENV, None)
        os.environ.pop(rch.CALL_WINDOW_END_ENV, None)
        os.environ.pop(rch.CALL_WINDOW_TZ_ENV, None)

    def test_policy_number_is_not_dialed(self):
        task = make_task(**{
            "Account Name": "John Smith",
            "Task Description": "Call John Smith about policy 7685786571",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])
        self.assertEqual(ports.phone_lookup.calls, [])
        self.assertIn(
            "Make a new Robie Call task with the number to call.",
            ports.discussion_client.appended[0]["body"],
        )

    def test_policy_claim_quote_and_pol_context_never_dial(self):
        samples = (
            "Call John Smith about policy number 7685786571",
            "Call John Smith about pol 7685786571",
            "Call John Smith about claim 7685786571",
            "Call John Smith about quote 7685786571",
            "Call John Smith about policy 768-578-6571",
        )
        for index, description in enumerate(samples):
            rch._reset_module_state_for_tests()
            task = make_task(**{
                "Task ID": f"TASK-POL-{index}",
                "Account Name": "John Smith",
                "Task Description": description,
            })
            ports = make_ports()
            result = handle_robie_call_task(task, live_config(), ports)
            self.assertFalse(result["ok"], description)
            self.assertEqual(ports.bland.calls, [], description)
            self.assertEqual(ports.phone_lookup.calls, [], description)
            if index == 0:
                self.assertIn(
                    "Make a new Robie Call task with the number to call.",
                    ports.discussion_client.appended[0]["body"],
                )

    def test_clear_phone_wording_or_format_is_dialed(self):
        cases = (
            ("Call John Smith at phone 7325550142", "+17325550142"),
            ("Call the cell 7325550142 about the renewal", "+17325550142"),
            ("Call John Smith at 7325550142", "+17325550142"),
            ("Call Progressive at 1-800-776-4737 about the surcharge", "+18007764737"),
            ("Call John at 555-999-8888 about renewal", "+15559998888"),
        )
        for index, (description, expected) in enumerate(cases):
            rch._reset_module_state_for_tests()
            task = make_task(**{
                "Task ID": f"TASK-PH-{index}",
                "Account Name": "John Smith",
                "Task Description": description,
            })
            ports = make_ports()
            result = handle_robie_call_task(task, live_config(), ports)
            self.assertTrue(result["ok"], description)
            self.assertEqual(ports.bland.calls[0]["phone"], expected, description)

    def test_known_policy_shape_is_ignored(self):
        task = make_task(**{
            "Account Name": "John Smith",
            "Task Description": "Call John Smith about 123456789 and policy WC5-33S-B276B9-026",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])
        self.assertEqual(ports.phone_lookup.calls, [])
        self.assertIn(
            "Make a new Robie Call task with the number to call.",
            ports.discussion_client.appended[0]["body"],
        )

    def test_ambiguous_number_asks_instead_of_dialing(self):
        task = make_task(**{
            "Account Name": "John Smith",
            "Task Description": "Call John Smith about 7685786571",
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("phone", body.lower())
        self.assertNotIn("7685786571", body)
        self.assertNotIn("lost track", body)
        import re
        self.assertIsNone(re.search(r"\d{6,}", body))

    def test_no_transfer_lookup_takes_a_message(self):
        import inspect
        self.assertNotIn("17324622360", inspect.getsource(rch))
        self.assertFalse(hasattr(rch, "TRANSFER_NUMBER"))
        ports = make_ports()
        handle_robie_call_task(make_task(), live_config(), ports)
        text = ports.bland.calls[0]["task_text"]
        self.assertIn("do not transfer", text)
        self.assertIn("Take a message", text)
        self.assertIn("732-462-8343", text)
        self.assertNotIn("17324622360", text)
        self.assertIsNone(ports.bland.calls[0]["metadata"]["transfer_to"])

    def test_resolved_transfer_number_is_offered(self):
        class Lookup:
            def get_transfer_number(self, name):
                return "+15559876543" if name == "Jane Producer" else None

        ports = make_ports(transfer_lookup=Lookup())
        handle_robie_call_task(make_task(), live_config(), ports)
        text = ports.bland.calls[0]["task_text"]
        self.assertIn("transfer the call", text)
        self.assertIn("Jane Producer", text)
        self.assertIn("+15559876543", text)
        self.assertEqual(ports.bland.calls[0]["metadata"]["transfer_to"], "Jane Producer")
        self.assertNotIn("17324622360", text)

    def test_dry_run_writes_nothing_and_not_a_lost_outcome(self):
        ports = make_ports()
        result = handle_robie_call_task(
            make_task(), RobieCallConfig(now=IN_WINDOW), ports)
        self.assertTrue(RobieCallConfig().dry_run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["writeback"]["status"], "dry_run")
        self.assertEqual(result["writeback"]["reason"], "dry run: nothing written")
        self.assertIsNone(result["writeback"]["note_id"])
        self.assertEqual(ports.discussion_client.appended, [])
        self.assertEqual(ports.bland.calls, [])
        self.assertNotIn("lost track", str(result))
        labeled = rch._format_outcome_note(
            "John Test", "Please call John about his renewal.",
            {"success": True, "dry_run": True, "call_ids": [], "attempts": []},
            "skipped", producer_name="Jane Producer",
        )
        self.assertIn("DRY RUN", labeled)
        self.assertNotIn("lost track", labeled)

    def test_note_names_who_was_called_and_the_assigned_producer(self):
        task = make_task(**{
            "Account Name": "Mary Smith",
            "Assigned Producer": "Jane Producer",
            "Task Created By": "Jake",
            "Task Description": (
                "Call Progressive at 1-800-776-4737 about Mary Smith's "
                "policy surcharge."
            ),
        })
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(ports.bland.calls[0]["phone"], "+18007764737")
        text = ports.bland.calls[0]["task_text"]
        self.assertIn("on behalf of Jane Producer", text)
        self.assertIn("calling Progressive", text)
        self.assertNotIn("on behalf of Jake", text)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("Called Progressive for Jane Producer", body)
        self.assertIn("about Mary Smith's policy surcharge.", body)
        self.assertIn("Eva identified herself as an AI assistant", body)
        self.assertIn("They answered and I talked to them.", body)
        self.assertNotIn("Please call", body)
        self.assertNotIn("..", body)
        self.assertNotIn("Called Mary Smith", body)
        self.assertNotIn("Jake", body)
        self.assertNotIn("800", body)

    def test_missing_assigned_producer_asks_instead_of_dialing(self):
        task = make_task(**{"Assigned Producer": ""})
        ports = make_ports()
        result = handle_robie_call_task(task, live_config(), ports)
        self.assertFalse(result["ok"])
        self.assertEqual(ports.bland.calls, [])
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("assigned producer", body.lower())
        self.assertNotIn("Jake", body)

    def test_outside_calling_window_never_dials(self):
        blocked = (
            datetime(2026, 10, 7, 8, 30, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 7, 18, 0, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 4, 11, 0, tzinfo=ZoneInfo("America/New_York")),
        )
        for moment in blocked:
            rch._reset_module_state_for_tests()
            ports = make_ports()
            result = handle_robie_call_task(
                make_task(**{"Task ID": "TASK-H"}),
                live_config(now=moment), ports)
            self.assertFalse(result["ok"], moment.isoformat())
            self.assertTrue(result["queued_for_calling_window"], moment.isoformat())
            self.assertEqual(ports.bland.calls, [], moment.isoformat())

        rch._reset_module_state_for_tests()
        opening = datetime(2026, 10, 7, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        ports = make_ports()
        result = handle_robie_call_task(make_task(), live_config(now=opening), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.bland.calls), 1)
        self.assertNotIn("queued_for_calling_window", result)

    def test_live_outside_window_queues_one_note(self):
        evening = datetime(2026, 10, 7, 20, 0, tzinfo=ZoneInfo("America/New_York"))
        ports = make_ports(job_checkpoint=_MemCheckpoint())
        first = handle_robie_call_task(make_task(), live_config(now=evening), ports)
        second = handle_robie_call_task(make_task(), live_config(now=evening), ports)
        self.assertTrue(first["queued_for_calling_window"])
        self.assertTrue(second["queued_for_calling_window"])
        self.assertEqual(ports.bland.calls, [])
        self.assertEqual(len(ports.discussion_client.appended), 1)
        body = ports.discussion_client.appended[0]["body"]
        self.assertIn("queued", body.lower())
        self.assertIn("did not dial", body.lower())
        self.assertNotIn("lost track", body)
        self.assertFalse(first.get("duplicate_suppressed"))
        self.assertFalse(second.get("duplicate_suppressed"))

    def test_dry_run_outside_window_writes_nothing(self):
        evening = datetime(2026, 10, 7, 20, 0, tzinfo=ZoneInfo("America/New_York"))
        ports = make_ports()
        result = handle_robie_call_task(
            make_task(), RobieCallConfig(dry_run=True, now=evening), ports)
        self.assertTrue(result["queued_for_calling_window"])
        self.assertEqual(ports.bland.calls, [])
        self.assertEqual(ports.discussion_client.appended, [])
        self.assertEqual(result["writeback"]["reason"],
                         "outside calling window; nothing written")
        self.assertNotIn("lost track", str(result))

    def test_calling_window_is_configurable(self):
        with patch.dict(os.environ, {
            rch.CALL_WINDOW_START_ENV: "10",
            rch.CALL_WINDOW_END_ENV: "11",
            rch.CALL_WINDOW_TZ_ENV: "America/New_York",
        }):
            inside = datetime(2026, 10, 7, 10, 30, tzinfo=ZoneInfo("America/New_York"))
            ports = make_ports()
            result = handle_robie_call_task(
                make_task(), RobieCallConfig(dry_run=False, now=inside), ports)
            self.assertTrue(result["ok"])
            self.assertEqual(len(ports.bland.calls), 1)

            rch._reset_module_state_for_tests()
            closed = datetime(2026, 10, 7, 11, 0, tzinfo=ZoneInfo("America/New_York"))
            ports = make_ports()
            result = handle_robie_call_task(
                make_task(**{"Task ID": "TASK-W"}),
                RobieCallConfig(dry_run=False, now=closed), ports)
            self.assertTrue(result["queued_for_calling_window"])
            self.assertEqual(ports.bland.calls, [])

            # An explicit config window wins over the env override.
            rch._reset_module_state_for_tests()
            early = datetime(2026, 10, 7, 9, 30, tzinfo=ZoneInfo("America/New_York"))
            ports = make_ports()
            result = handle_robie_call_task(
                make_task(**{
                    "Task ID": "TASK-C",
                    "Task Description": "Call about the audit documents. Call at 732-668-8161.",
                }),
                RobieCallConfig(
                    dry_run=False, now=early,
                    calling_window_start_hour=9, calling_window_end_hour=18,
                ), ports)
            self.assertTrue(result["ok"])
            self.assertEqual(len(ports.bland.calls), 1)

    def test_unusable_calling_window_does_not_dial(self):
        ports = make_ports()
        result = handle_robie_call_task(
            make_task(),
            live_config(calling_window_tz="Not/AZone"),
            ports)
        self.assertTrue(result["queued_for_calling_window"])
        self.assertEqual(ports.bland.calls, [])
        rch._reset_module_state_for_tests()
        ports = make_ports()
        result = handle_robie_call_task(
            make_task(**{"Task ID": "TASK-BAD"}),
            live_config(calling_window_start_hour=18, calling_window_end_hour=9),
            ports)
        self.assertTrue(result["queued_for_calling_window"])
        self.assertEqual(ports.bland.calls, [])

    def test_note_write_does_not_read_the_driver_lease(self):
        with patch.dict(os.environ, {
            "ROBIE_EZLYNX_DRIVER_GATE_REQUIRED": "1",
            "ROBIE_EZLYNX_DRIVER_HOLDER": "TEST",
        }):
            with patch(
                "robie_job_engine.ezlynx_driver_gate.read_metadata",
                side_effect=AssertionError("driver lease must not be read"),
            ):
                ports = make_ports()
                result = handle_robie_call_task(make_task(), live_config(), ports)
        self.assertTrue(result["ok"])
        self.assertEqual(len(ports.discussion_client.appended), 1)


if __name__ == "__main__":
    unittest.main()
