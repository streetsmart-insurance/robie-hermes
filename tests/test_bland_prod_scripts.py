"""Splice scripts, marketing opt-in, and once-per-day pickup dedupe.

Bland, RingCentral, and EZLynx are fakes. No live dial.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from robie_job_engine import robie_call_handler as rch
from robie_job_engine.call_opt_in import CallOptInStore
from robie_job_engine.call_pickup import CallDedupeStore, classify_call_request
from robie_job_engine.ezlynx_driver_gate import DriverDecision
from robie_job_engine.ezlynx_task_jobs import job_payload_for_task
from robie_job_engine.ezlynx_task_report import AssignedTask, parse_task_report
from robie_job_engine.robie_call_handler import (
    RobieCallConfig,
    RobieCallPorts,
    handle_robie_call_task,
)
from robie_job_engine.splice_scripts import WORKFLOWS, render_text
from robie_job_engine.task_assignment_worker import TaskAssignmentWorker

TEST_APPLICANT = "220250093"
IN_WINDOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))
AUDIT_BODY = WORKFLOWS["audit_not_complete"].body
_LEASE_PATCH = None


def setUpModule():
    global _LEASE_PATCH
    _LEASE_PATCH = patch(
        "robie_job_engine.ezlynx_driver_gate.require_driver_in",
        return_value=DriverDecision(True, "TEST", "unit test; lease not read"),
    )
    _LEASE_PATCH.start()


def tearDownModule():
    if _LEASE_PATCH is not None:
        _LEASE_PATCH.stop()


class _Phone:
    def __init__(self, mobile=False):
        self.mobile = mobile

    def get_phone(self, applicant_id):
        return "732-555-0142" if applicant_id == TEST_APPLICANT else None

    def is_mobile(self, applicant_id):
        return self.mobile and applicant_id == TEST_APPLICANT


class _Bland:
    def __init__(self):
        self.calls = []

    def place_call_with_double_dial(self, phone, task_text, first_sentence, voicemail, metadata=None):
        self.calls.append({
            "phone": phone,
            "task_text": task_text,
            "first_sentence": first_sentence,
            "voicemail_message": voicemail,
            "metadata": metadata,
        })
        return {
            "success": True,
            "call_ids": ["call-1"],
            "attempts": [{"attempt": 1, "success": True,
                          "final_status": {"status": "completed",
                                           "answered_by": "human",
                                           "duration": 20}}],
            "error": None,
        }

    def recent_calls(self, phone, since_seconds=1800):
        return {"ok": True, "calls": []}

    def get_call_status(self, call_id):
        return {"ok": True, "status": "completed", "answered_by": "human", "duration_s": 20}


class _Discussions:
    def __init__(self, discussions):
        self.discussions = discussions
        self.appended = []
        self._notes = {}

    def get_discussions(self, applicant_id):
        return list(self.discussions)

    def append_note(self, discussion_id, body, note_type="Note"):
        self.appended.append({"discussion_id": discussion_id, "body": body})
        note_id = f"N-{len(self.appended)}"
        self._notes.setdefault(discussion_id, []).append(note_id)
        return {"noteId": note_id, "discussionId": discussion_id}

    def get_discussion(self, discussion_id):
        return {
            "discussionId": discussion_id,
            "title": "Renewal chat",
            "notes": [{"noteId": nid} for nid in self._notes.get(discussion_id, [])],
        }


def _task(**overrides):
    task = {
        "Task ID": "TASK-SCRIPT",
        "Task Subject": "Task Note",
        "Task Description": "Robie audit",
        "Applicant ID": TEST_APPLICANT,
        "Account Name": "Mary Smith",
        "Assigned Producer": "Jane Producer",
        "Activity Labels": "Robie audit",
        "Discussion ID": "D-100",
    }
    task.update(overrides)
    return task


def _ports(**overrides):
    discussions = overrides.pop("discussions", None)
    if discussions is None:
        discussions = [{"discussionId": "D-100", "title": "Renewal chat"}]
    kw = {
        "phone_lookup": _Phone(),
        "bland": _Bland(),
        "discussion_client": _Discussions(discussions),
        "chat_alert": lambda _text: True,
    }
    kw.update(overrides)
    return RobieCallPorts(**kw)


def _handle(task, ports, **config):
    rch._reset_module_state_for_tests()
    return handle_robie_call_task(
        task, RobieCallConfig(dry_run=False, now=IN_WINDOW, **config), ports,
    )


def test_live_frame_drops_the_tollfree_sentence_and_keeps_press_six():
    source = open("robie_job_engine/splice_scripts.py", encoding="utf-8").read()
    assert "Tollfree" not in source
    assert "17324622360" not in source
    ports = _ports()
    result = _handle(_task(), ports)
    assert result["ok"] is True
    spoken = ports.bland.calls[0]["task_text"]
    voicemail = ports.bland.calls[0]["voicemail_message"]
    assert AUDIT_BODY in spoken
    assert AUDIT_BODY in voicemail
    assert "please press 6" in spoken
    assert "Press 1:" not in spoken
    assert "press 2" not in spoken.lower()
    assert "732-462-8343" in spoken
    assert "Jane Producer" in spoken
    assert "Mary" in spoken
    assert "Tollfree" not in spoken
    assert "press 6" not in voicemail.lower()
    assert "Tollfree" not in voicemail
    assert "17324622360" not in spoken


def test_press_one_uses_the_lookup_and_press_two_needs_mobile_sms_and_a_text_body():
    class Lookup:
        def get_transfer_number(self, name):
            return "+15559876543" if name == "Jane Producer" else None

    ports = _ports(phone_lookup=_Phone(mobile=True), transfer_lookup=Lookup())
    _handle(_task(), ports, sms_configured=True)
    spoken = ports.bland.calls[0]["task_text"]
    assert "Press 1:" in spoken
    assert "+15559876543" in spoken
    assert "press 2" in spoken.lower()
    assert "17324622360" not in spoken
    assert ports.bland.calls[0]["metadata"]["transfer_phone_number"] == "+15559876543"

    rch._reset_module_state_for_tests()
    renewal = _ports(phone_lookup=_Phone(mobile=True))
    _handle(_task(**{
        "Task ID": "TASK-RENEW",
        "Task Description": "Robie renewal reach-out",
        "Activity Labels": "Robie renewal reach-out",
    }), renewal, sms_configured=True)
    assert "press 2" not in renewal.bland.calls[0]["task_text"].lower()
    assert render_text(WORKFLOWS["renewal_reach_out"]) is None
    assert render_text(WORKFLOWS["unresponsive"]) is None
    winback_text = render_text(WORKFLOWS["winback_campaign"])
    assert "appreciate your past business" not in winback_text
    assert "win you back with a new offer" in winback_text


def test_sales_center_and_winback_skip_without_a_recorded_opt_in(tmp_path):
    store = CallOptInStore(tmp_path / "optin.sqlite")
    sales = _task(**{
        "Task ID": "TASK-SALES",
        "Task Description": "Sales Center Reviewed Status",
        "Activity Labels": "Robie sales center",
    })
    ports = _ports(opt_in_store=store)
    skipped = _handle(sales, ports)
    assert skipped["skipped_opt_in"] is True
    assert skipped["ok"] is False
    assert ports.bland.calls == []
    assert "opt-in" in skipped["error"]

    store.record_opt_in(TEST_APPLICANT, source="applicant-created")
    winback = _ports(opt_in_store=store)
    placed = _handle(_task(**{
        "Task ID": "TASK-WIN",
        "Task Description": "Winback Campaign",
        "Activity Labels": "Robie winback",
    }), winback)
    assert placed["ok"] is True
    assert WORKFLOWS["winback_campaign"].body in winback.bland.calls[0]["task_text"]


def test_robie_call_without_a_workflow_is_not_dialed():
    ports = _ports()
    result = _handle(_task(**{
        "Task Description": "Robie Call",
        "Activity Labels": "Robie Call",
    }), ports)
    assert result["skipped_unscripted"] is True
    assert ports.bland.calls == []
    assert "no script was used" in ports.discussion_client.appended[0]["body"]
    assert classify_call_request("Robie Call", "Please call John about his renewal.") .action == "skip_unscripted"
    assert classify_call_request("", "Please call John about his renewal.").action == "not_labeled"


def test_each_note_is_called_at_most_once_per_day(tmp_path):
    dedupe = CallDedupeStore(tmp_path / "dedupe.sqlite")
    first = _ports(call_dedupe=dedupe)
    placed = _handle(_task(), first)
    assert placed["ok"] is True
    assert len(first.bland.calls) == 1
    second = _ports(call_dedupe=dedupe)
    again = _handle(_task(**{"Task ID": "TASK-AGAIN"}), second)
    assert again["duplicate_suppressed"] is True
    assert second.bland.calls == []
    assert "already called today" in second.discussion_client.appended[0]["body"]


def test_outcome_note_lands_on_the_titled_discussion_only():
    ports = _ports(discussions=[
        {"discussionId": "D-UNTITLED", "title": "Untitled"},
        {"discussionId": "D-100", "title": "Renewal chat"},
    ])
    result = _handle(_task(), ports)
    assert result["ok"] is True
    assert len(ports.discussion_client.appended) == 1
    note = ports.discussion_client.appended[0]
    assert note["discussion_id"] == "D-100"
    assert "Audit Not Complete" in note["body"]
    assert "Jane Producer" in note["body"]
    assert "732" not in note["body"]
    assert "lost track" not in note["body"]

    untitled = _ports(discussions=[{"discussionId": "D-BARE", "title": "Untitled"}])
    _handle(_task(**{"Task ID": "TASK-BARE"}), untitled)
    assert untitled.discussion_client.appended == []


def test_check_in_labels_become_a_workflow_and_a_bare_label_does_not():
    csv = """Task ID,Applicant ID,Account Name,Task Assigned To,Task Status,Task Due Date,Task Priority,Task Created Date,Task Last Modified Date,Note,Activity Type,Discussion ID,Activity Labels,Assigned Producer
T-1,220250093,Mary Smith,Robie AI,Open,2026-10-07,Normal,2026-10-07,2026-10-07,Robie audit,Task Note,D-100,Robie audit,Jane Producer
T-2,220250093,Mary Smith,Robie AI,Open,2026-10-07,Normal,2026-10-07,2026-10-07,Robie Call,Task Note,D-200,Robie Call,Jane Producer
"""
    tasks = parse_task_report(csv)
    assert tasks[0].activity_labels == "Robie audit"
    payload = job_payload_for_task(tasks[0])
    assert payload["workflow"] == "audit_not_complete"
    assert job_payload_for_task(tasks[1])["workflow"] == ""
    worker = TaskAssignmentWorker(discussion_client=object())
    assert worker._categorize_task(tasks[0]) == "callback"
    plain = AssignedTask(
        task_id="9", title="Task Note", description="File the dec page",
        applicant_id=TEST_APPLICANT, applicant_name="Mary Smith",
        assigned_to="Robie AI", due_date="", priority="", created_date="",
        status="Open", discussion_id="D-1", last_modified="",
    )
    assert worker._categorize_task(plain) != "callback"
