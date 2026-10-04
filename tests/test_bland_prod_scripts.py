"""Call labels, the lead follow-up script, and once-per-day dedupe.

Bland, RingCentral, and EZLynx are fakes. No live dial. Short names such
as "Robie audit" are not the nine official labels and do not select a
script. Official labels are covered in test_splice_workflow_labels.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from robie_job_engine import robie_call_handler as rch
from robie_job_engine.call_opt_in import CallOptInStore
from robie_job_engine.call_opt_out import CallOptOutStore
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
SALES_BODY = WORKFLOWS["sales_center_reviewed"].body
LEAD_BODY = WORKFLOWS["lead_follow_up"].body
CALL_INSTRUCTION = "Please call Mary about the renewal documents."
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
        "Task Description": "Robie Lead Follow Up",
        "Applicant ID": TEST_APPLICANT,
        "Account Name": "Mary Smith",
        "Assigned Producer": "Jane Producer",
        "Activity Labels": "Robie Lead Follow Up",
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


def test_robie_call_label_dials_the_description_verbatim():
    ports = _ports()
    typed = CALL_INSTRUCTION + " Call at 732-555-0142."
    result = _handle(_task(**{
        "Task ID": "TASK-FREE",
        "Task Description": typed,
        "Activity Labels": "Robie Call",
    }), ports)
    assert result["ok"] is True
    assert result.get("skipped_unscripted") is not True
    spoken = ports.bland.calls[0]["task_text"]
    assert CALL_INSTRUCTION in spoken
    assert ports.bland.calls[0]["phone"].endswith("5550142")
    assert "on behalf of Jane Producer" in spoken
    assert "17324622360" not in spoken
    assert AUDIT_BODY not in spoken
    assert SALES_BODY not in spoken
    assert classify_call_request(
        "Robie Call", "Please call John about his renewal.",
    ).action == "freeform"
    assert classify_call_request("", "Please call John about his renewal.").action == "not_labeled"


def test_lead_follow_up_uses_the_sales_center_frame_without_opt_in(tmp_path):
    source = open("robie_job_engine/splice_scripts.py", encoding="utf-8").read()
    assert "Tollfree" not in source
    assert "17324622360" not in source
    assert WORKFLOWS["lead_follow_up"].marketing is False
    assert WORKFLOWS["sales_center_reviewed"].marketing is True

    class Lookup:
        def get_transfer_number(self, name):
            return "+15559876543" if name == "Jane Producer" else None

    ports = _ports(phone_lookup=_Phone(mobile=True), transfer_lookup=Lookup())
    result = _handle(_task(), ports, sms_configured=True)
    assert result["ok"] is True
    spoken = ports.bland.calls[0]["task_text"]
    voicemail = ports.bland.calls[0]["voicemail_message"]
    assert LEAD_BODY in spoken
    assert LEAD_BODY in voicemail
    assert "on behalf of your agent, Jane Producer" in spoken
    assert "insurance inquiry or quote" in spoken
    assert "please press 6" in spoken
    assert "Press 1:" in spoken
    assert "+15559876543" in spoken
    assert "press 2" in spoken.lower()
    assert "732-462-8343" in spoken
    assert "17324622360" not in spoken
    assert SALES_BODY not in spoken
    assert ports.bland.calls[0]["metadata"]["transfer_phone_number"] == "+15559876543"
    assert "press 6" not in voicemail.lower()

    quiet = _ports(phone_lookup=_Phone(mobile=True))
    _handle(_task(**{"Task ID": "TASK-NO-SMS"}), quiet, sms_configured=False)
    assert "press 2" not in quiet.bland.calls[0]["task_text"].lower()
    assert "Press 1:" not in quiet.bland.calls[0]["task_text"]
    assert "Do not transfer" in quiet.bland.calls[0]["task_text"]
    assert "take a message" in quiet.bland.calls[0]["task_text"]

    alias = _ports()
    placed = _handle(_task(**{
        "Task ID": "TASK-ALIAS",
        "Task Description": "The lead asked about a homeowners quote.",
        "Activity Labels": "Robie Call Follow Up",
        "Account Name": "Alex Lead",
    }), alias)
    assert placed["ok"] is True
    assert LEAD_BODY in alias.bland.calls[0]["task_text"]
    assert classify_call_request("Robie lead follow-up", "").workflow_id == "lead_follow_up"

    opted = CallOptOutStore(tmp_path / "optout.sqlite")
    opted.record_opt_out(TEST_APPLICANT, source="press-6")
    blocked = _ports(opt_out_store=opted)
    skipped = _handle(_task(**{"Task ID": "TASK-OUT"}), blocked)
    assert skipped["ok"] is False
    assert blocked.bland.calls == []
    assert "opted out" in blocked.discussion_client.appended[0]["body"].lower()

    no_opt_in = CallOptInStore(tmp_path / "optin.sqlite")
    still = _ports(opt_in_store=no_opt_in)
    dialed = _handle(_task(**{
        "Task ID": "TASK-IN",
        "Account Name": "Casey Inquiry",
    }), still)
    assert dialed.get("skipped_opt_in") is not True
    assert dialed["ok"] is True
    assert len(still.bland.calls) == 1


def test_splice_labels_do_not_trigger_a_call(tmp_path, monkeypatch):
    # Official labels stay off unless Test or the Production flag is on.
    # "Robie returned mail", "Robie renewal reach-out", and "Robie
    # unresponsive" are the same names as three official labels once case
    # and hyphens are ignored, so this test pins Production-off.
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    monkeypatch.delenv("ROBIE_SPLICE_WORKFLOWS_LIVE", raising=False)
    monkeypatch.setattr(
        "robie_job_engine.call_pickup.socket.gethostname",
        lambda: "hermes-poc-01",
    )
    store = CallOptInStore(tmp_path / "optin.sqlite")
    store.record_opt_in(TEST_APPLICANT, source="applicant-created")
    for label, note in (
        ("Robie audit", "Robie audit"),
        ("Robie sales center", "Sales Center Reviewed Status"),
        ("Robie winback", "Winback Campaign"),
        ("Robie renewal reach-out", "Robie renewal reach-out"),
        ("Robie recommendations", "Recommendations Follow-Up"),
        ("Robie returned mail", "Returned Mail"),
        ("Robie e-sign", "E-signature Follow-Up"),
        ("Robie additional info", "Additional Information Follow-Up"),
        ("Robie unresponsive", "Unresponsive"),
    ):
        ports = _ports(opt_in_store=store)
        result = _handle(_task(**{
            "Task ID": f"TASK-{label}",
            "Task Description": note,
            "Activity Labels": label,
            "Workflow": "audit_not_complete",
        }), ports)
        assert ports.bland.calls == [], label
        assert result["ok"] is False, label
        assert AUDIT_BODY not in str(result)
        assert classify_call_request(label, note).action == "not_labeled"
    assert SALES_BODY.startswith("We are following up on the quote")
    assert render_text(WORKFLOWS["renewal_reach_out"]) is None
    assert render_text(WORKFLOWS["unresponsive"]) is None
    assert "win you back with a new offer" in render_text(WORKFLOWS["winback_campaign"])


def test_each_note_is_called_at_most_once_per_day(tmp_path):
    dedupe = CallDedupeStore(tmp_path / "dedupe.sqlite")
    task = _task(**{
        "Task Description": CALL_INSTRUCTION + " Call at 732-555-0142.",
        "Activity Labels": "Robie Call",
    })
    first = _ports(call_dedupe=dedupe)
    placed = _handle(task, first)
    assert placed["ok"] is True
    assert len(first.bland.calls) == 1
    second = _ports(call_dedupe=dedupe)
    again = _handle(task | {"Task ID": "TASK-AGAIN"}, second)
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
    assert "Lead Follow Up" in note["body"]
    assert "Jane Producer" in note["body"]
    assert "732" not in note["body"]
    assert "lost track" not in note["body"]

    untitled = _ports(discussions=[{"discussionId": "D-BARE", "title": "Untitled"}])
    _handle(_task(**{"Task ID": "TASK-BARE"}), untitled)
    assert untitled.discussion_client.appended == []


def test_check_in_labels_map_only_call_and_lead_follow_up():
    csv = """Task ID,Applicant ID,Account Name,Task Assigned To,Task Status,Task Due Date,Task Priority,Task Created Date,Task Last Modified Date,Note,Activity Type,Discussion ID,Activity Labels,Assigned Producer
T-1,220250093,Mary Smith,Robie AI,Open,2026-10-07,Normal,2026-10-07,2026-10-07,Robie audit,Task Note,D-100,Robie audit,Jane Producer
T-2,220250093,Mary Smith,Robie AI,Open,2026-10-07,Normal,2026-10-07,2026-10-07,Please call Mary about the renewal documents.,Task Note,D-200,Robie Call,Jane Producer
T-3,220250093,Mary Smith,Robie AI,Open,2026-10-07,Normal,2026-10-07,2026-10-07,Robie Lead Follow Up,Task Note,D-300,Robie Lead Follow Up,Jane Producer
T-4,220250093,Mary Smith,Robie AI,Open,2026-10-07,Normal,2026-10-07,2026-10-07,Robie Call Follow Up,Task Note,D-400,Robie Call Follow Up,Jane Producer
"""
    tasks = parse_task_report(csv)
    assert job_payload_for_task(tasks[0])["workflow"] == ""
    assert job_payload_for_task(tasks[1])["workflow"] == ""
    assert job_payload_for_task(tasks[2])["workflow"] == "lead_follow_up"
    assert job_payload_for_task(tasks[3])["workflow"] == "lead_follow_up"
    worker = TaskAssignmentWorker(discussion_client=object())
    assert worker._categorize_task(tasks[0]) != "callback"
    assert worker._categorize_task(tasks[1]) == "callback"
    assert worker._categorize_task(tasks[2]) == "callback"
    plain = AssignedTask(
        task_id="9", title="Task Note", description="File the dec page",
        applicant_id=TEST_APPLICANT, applicant_name="Mary Smith",
        assigned_to="Robie AI", due_date="", priority="", created_date="",
        status="Open", discussion_id="D-1", last_modified="",
    )
    assert worker._categorize_task(plain) != "callback"
