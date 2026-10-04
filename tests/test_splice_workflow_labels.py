"""Nine Splice labels: mapping, Test on, Production off, baseline, test cell.

Bland, Secret Manager, and EZLynx are fakes. No live dial. Jake's cell is
never hardcoded; the test double returns a synthetic E.164 value. The
proof applicant is a synthetic id, not Buster Brown and not ROBIE Test LLC.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from robie_job_engine import robie_call_handler as rch
from robie_job_engine.bland_call_port import (
    JAKE_CELL_SECRET,
    BlandTransportCallPort,
    select_dial_target,
)
from robie_job_engine.call_opt_in import CallOptInStore
from robie_job_engine.call_pickup import (
    SPLICE_LABELS,
    SPLICE_PROD_FLAG,
    SPLICE_TEST_APPLICANT_ENV,
    CallDedupeStore,
    classify_call_request,
    splice_test_account_reason,
    splice_workflows_enabled,
)
from robie_job_engine.ezlynx_seen_tasks import SeenTaskStore
from robie_job_engine.ezlynx_task_inbox import IngestedReport
from robie_job_engine.ezlynx_task_intake import run_intake
from robie_job_engine.ezlynx_task_jobs import job_payload_for_task
from robie_job_engine.ezlynx_task_report import AssignedTask
from robie_job_engine.robie_call_handler import (
    RobieCallConfig,
    RobieCallPorts,
    handle_robie_call_task,
)
from robie_job_engine.splice_scripts import WORKFLOWS
from robie_job_engine.ezlynx_driver_gate import DriverDecision
from robie_job_engine.store import JobStore

_LEASE_PATCH = None


def setup_module():
    global _LEASE_PATCH
    _LEASE_PATCH = patch(
        "robie_job_engine.ezlynx_driver_gate.require_driver_in",
        return_value=DriverDecision(True, "TEST", "unit test; lease not read"),
    )
    _LEASE_PATCH.start()


def teardown_module():
    if _LEASE_PATCH is not None:
        _LEASE_PATCH.stop()


JAKE_ACCOUNT = "910000111"
BUSTER_BROWN = "26356199"
ROBIE_TEST_LLC = "220250093"
CLIENT_PHONE = "+17325550142"
TEST_CELL = "+17325550199"
IN_WINDOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))
PROD_HOST = "hermes-poc-01"
TEST_HOST = "hermes-test-01"
T1 = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)


def _allow_notes(monkeypatch):
    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    monkeypatch.setenv("ROBIE_PLAYGROUND", "1")


def _prod_env(monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    monkeypatch.delenv(SPLICE_PROD_FLAG, raising=False)
    _allow_notes(monkeypatch)
    monkeypatch.setattr(
        "robie_job_engine.call_pickup.socket.gethostname", lambda: PROD_HOST,
    )


def _test_env(monkeypatch, applicant=JAKE_ACCOUNT):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    if applicant:
        monkeypatch.setenv(SPLICE_TEST_APPLICANT_ENV, applicant)
    else:
        monkeypatch.delenv(SPLICE_TEST_APPLICANT_ENV, raising=False)
    _allow_notes(monkeypatch)
    monkeypatch.setattr(
        "robie_job_engine.call_pickup.socket.gethostname", lambda: TEST_HOST,
    )


class _Phone:
    def get_phone(self, applicant_id):
        return "732-555-0142" if applicant_id == JAKE_ACCOUNT else None

    def is_mobile(self, applicant_id):
        del applicant_id
        return False


class _Bland:
    def __init__(self):
        self.calls = []

    def place_call_with_double_dial(self, phone, task_text, first_sentence, voicemail, metadata=None):
        self.calls.append({
            "phone": phone,
            "task_text": task_text,
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
        del phone, since_seconds
        return {"ok": True, "calls": []}

    def get_call_status(self, call_id):
        del call_id
        return {"ok": True, "status": "completed", "answered_by": "human", "duration_s": 20}


class _Discussions:
    def __init__(self):
        self.appended = []
        self._notes = {}
        self.discussions = [{"discussionId": "D-100", "title": "Renewal chat"}]

    def get_discussions(self, applicant_id):
        del applicant_id
        return list(self.discussions)

    def append_note(self, discussion_id, body, note_type="Note"):
        del note_type
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
        "Task ID": "910001",
        "Task Subject": "Task Note",
        "Task Description": "Workflow proof for Jake Ferrara.",
        "Applicant ID": JAKE_ACCOUNT,
        "Account Name": "Jake Ferrara",
        "Assigned Producer": "Jane Producer",
        "Assigned To": "Robie AI",
        "Activity Labels": "Robie Audit Not Complete",
        "Discussion ID": "D-100",
        "Created Date": "2026-10-07T09:00:00",
    }
    task.update(overrides)
    return task


def _ports(**overrides):
    kw = {
        "phone_lookup": _Phone(),
        "bland": _Bland(),
        "discussion_client": _Discussions(),
        "chat_alert": lambda _text: True,
    }
    kw.update(overrides)
    return RobieCallPorts(**kw)


def _handle(task, ports, **config):
    rch._reset_module_state_for_tests()
    return handle_robie_call_task(
        task, RobieCallConfig(dry_run=False, now=IN_WINDOW, **config), ports,
    )


def test_nine_labels_map_case_insensitively_only_when_enabled(monkeypatch):
    assert len(SPLICE_LABELS) == 9
    assert {workflow_id for _label, workflow_id in SPLICE_LABELS} == set(WORKFLOWS) - {"lead_follow_up"}
    _prod_env(monkeypatch)
    assert splice_workflows_enabled(hostname=PROD_HOST) is False
    for label, workflow_id in SPLICE_LABELS:
        folded = label.casefold().replace(" ", "-").replace("_", " ")
        assert classify_call_request(label, "", hostname=PROD_HOST).action == "not_labeled"
        assert classify_call_request(folded, "Please call the client.", hostname=PROD_HOST).action == "not_labeled"
        assert job_payload_for_task(_assigned(label))["workflow"] == ""
        _ = workflow_id

    _test_env(monkeypatch)
    assert splice_workflows_enabled(hostname=TEST_HOST) is True
    for label, workflow_id in SPLICE_LABELS:
        pickup = classify_call_request(label, "Do not call", hostname=TEST_HOST)
        assert pickup.action == "workflow", label
        assert pickup.workflow_id == workflow_id
        shouted = label.upper().replace(" ", "_").replace("-", " ")
        again = classify_call_request(shouted, "", hostname=TEST_HOST)
        assert again.workflow_id == workflow_id
        assert job_payload_for_task(_assigned(label))["workflow"] == workflow_id

    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    monkeypatch.setenv(SPLICE_PROD_FLAG, "1")
    assert splice_workflows_enabled(hostname=PROD_HOST) is True
    assert classify_call_request(
        "Robie Winback Campaign", "", hostname=PROD_HOST,
    ).workflow_id == "winback_campaign"
    monkeypatch.setenv(SPLICE_PROD_FLAG, "0")
    assert splice_workflows_enabled(hostname=PROD_HOST) is False


def test_short_names_and_the_two_live_labels_do_not_change(monkeypatch):
    _test_env(monkeypatch)
    monkeypatch.setenv(SPLICE_PROD_FLAG, "1")
    for label in (
        "Robie audit",
        "Robie sales center",
        "Robie winback",
        "Robie recommendations",
        "Robie e-sign",
        "Robie additional info",
        "Robie Caller",
        "Robie Call extra",
    ):
        assert classify_call_request(label, "Please call the client.", hostname=TEST_HOST).action == "not_labeled"
    # These fold to the official labels. Hyphens and case are not a different name.
    assert classify_call_request(
        "Robie returned mail", "", hostname=TEST_HOST,
    ).workflow_id == "returned_mail"
    assert classify_call_request(
        "Robie renewal reach-out", "", hostname=TEST_HOST,
    ).workflow_id == "renewal_reach_out"
    assert classify_call_request(
        "Robie unresponsive", "", hostname=TEST_HOST,
    ).workflow_id == "unresponsive"
    assert classify_call_request("Robie Call", "", hostname=TEST_HOST).action == "freeform"
    assert classify_call_request("Robie Call", "", hostname=PROD_HOST).action == "freeform"
    lead = classify_call_request("Robie lead follow-up", "", hostname=PROD_HOST)
    assert lead.action == "workflow" and lead.workflow_id == "lead_follow_up"
    alias = classify_call_request("robie-call-follow-up", "", hostname=TEST_HOST)
    assert alias.workflow_id == "lead_follow_up"
    both = classify_call_request("Robie Call, Robie Audit Not Complete", "", hostname=TEST_HOST)
    assert both.action == "freeform"


def _assigned(label: str) -> AssignedTask:
    return AssignedTask(
        task_id="910001",
        title="Task Note",
        description="Workflow proof.",
        applicant_id=JAKE_ACCOUNT,
        applicant_name="Jake Ferrara",
        assigned_to="Robie AI",
        due_date="2026-10-07",
        priority="Normal",
        created_date="2026-10-07",
        status="Open",
        discussion_id="D-100",
        last_modified="2026-10-07T09:00:00",
        assigned_producer="Jane Producer",
        activity_labels=label,
        created_at="2026-10-07T09:00:00",
        created_at_et="2026-10-07T10:00:00-04:00",
    )


def test_enabled_labels_speak_the_script_for_the_producer_not_jake(monkeypatch, tmp_path):
    _test_env(monkeypatch)
    opted = CallOptInStore(tmp_path / "optin.sqlite")
    opted.record_opt_in(JAKE_ACCOUNT, source="applicant-created")
    for index, (label, workflow_id) in enumerate(SPLICE_LABELS, start=1):
        ports = _ports(opt_in_store=opted)
        result = _handle(_task(**{
            "Task ID": f"91000{index}",
            "Activity Labels": label,
            "Task Description": "The workers comp payroll audit needs a look.",
        }), ports)
        assert result["ok"] is True, label
        spoken = ports.bland.calls[0]["task_text"]
        assert WORKFLOWS[workflow_id].body in spoken
        assert "on behalf of your agent, Jane Producer" in spoken
        assert "on behalf of Jake" not in spoken
        assert "payroll" not in spoken
        assert ports.bland.calls[0]["metadata"]["on_behalf_of_producer"] == "Jane Producer"
        assert ports.bland.calls[0]["metadata"]["workflow"] == workflow_id

    quiet = _ports(opt_in_store=CallOptInStore(tmp_path / "empty.sqlite"))
    skipped = _handle(_task(**{
        "Task ID": "910099",
        "Activity Labels": "Robie Winback Campaign",
    }), quiet)
    assert skipped["ok"] is False
    assert quiet.bland.calls == []
    assert skipped.get("skipped_opt_in") is True


def test_production_default_dials_nothing_and_the_flag_does(monkeypatch):
    _prod_env(monkeypatch)
    ports = _ports()
    blocked = _handle(_task(), ports)
    assert blocked["ok"] is False
    assert ports.bland.calls == []
    assert "call task" in blocked["error"]

    monkeypatch.setenv(SPLICE_PROD_FLAG, "1")
    ports = _ports()
    placed = _handle(_task(**{"Task ID": "910088"}), ports)
    assert placed["ok"] is True
    assert WORKFLOWS["audit_not_complete"].body in ports.bland.calls[0]["task_text"]
    assert "Jane Producer" in ports.bland.calls[0]["task_text"]

    live = _ports()
    call = _handle(_task(**{
        "Task ID": "910077",
        "Activity Labels": "Robie Call",
        "Task Description": "Please call Jake about the renewal documents.",
    }), live)
    assert call["ok"] is True
    assert "Please call Jake about the renewal documents." in live.bland.calls[0]["task_text"]
    assert WORKFLOWS["audit_not_complete"].body not in live.bland.calls[0]["task_text"]
    lead_ports = _ports()
    lead = _handle(_task(**{
        "Task ID": "910066",
        "Activity Labels": "Robie Lead Follow Up",
        "Task Description": "Robie Lead Follow Up",
    }), lead_ports)
    assert lead["ok"] is True
    assert WORKFLOWS["lead_follow_up"].body in lead_ports.bland.calls[0]["task_text"]


def test_test_account_must_be_jakes_and_buster_brown_is_refused(monkeypatch):
    _test_env(monkeypatch, applicant="")
    monkeypatch.delenv(SPLICE_TEST_APPLICANT_ENV, raising=False)
    ports = _ports()
    missing = _handle(_task(), ports)
    assert missing["ok"] is False
    assert ports.bland.calls == []
    assert missing.get("skipped_test_account") is True
    assert missing.get("chat_alerted") is True
    assert "Jake Ferrara" in ports.discussion_client.appended[0]["body"]
    assert "Buster Brown" in ports.discussion_client.appended[0]["body"]

    for applicant, name in ((BUSTER_BROWN, "Buster Brown"), (ROBIE_TEST_LLC, "ROBIE Test LLC")):
        monkeypatch.setenv(SPLICE_TEST_APPLICANT_ENV, applicant)
        assert name in splice_test_account_reason(applicant, hostname=TEST_HOST)
        refused = _ports()
        result = _handle(_task(**{
            "Task ID": f"task-{applicant}",
            "Applicant ID": applicant,
        }), refused)
        assert result["ok"] is False
        assert refused.bland.calls == []

    monkeypatch.setenv(SPLICE_TEST_APPLICANT_ENV, JAKE_ACCOUNT)
    other = _ports()
    wrong = _handle(_task(**{
        "Task ID": "910055",
        "Applicant ID": "910000222",
    }), other)
    assert wrong["ok"] is False
    assert other.bland.calls == []
    assert "Jake Ferrara" in other.discussion_client.appended[0]["body"]

    _prod_env(monkeypatch)
    monkeypatch.setenv(SPLICE_PROD_FLAG, "1")
    assert splice_test_account_reason(BUSTER_BROWN, hostname=PROD_HOST) is None


def test_missing_producer_asks_and_kill_switch_and_dedupe_still_hold(monkeypatch, tmp_path):
    _test_env(monkeypatch)
    ports = _ports()
    asked = _handle(_task(**{"Task ID": "910044", "Assigned Producer": ""}), ports)
    assert asked["ok"] is False
    assert ports.bland.calls == []
    assert "assigned producer" in ports.discussion_client.appended[0]["body"].lower()

    monkeypatch.setenv("ROBIE_CALL_HALT", "1")
    halted = _ports()
    stopped = _handle(_task(**{"Task ID": "910033"}), halted)
    assert stopped["ok"] is False
    assert halted.bland.calls == []
    assert "kill switch" in stopped["error"]
    monkeypatch.delenv("ROBIE_CALL_HALT", raising=False)

    dedupe = CallDedupeStore(tmp_path / "dedupe.sqlite")
    first = _ports(call_dedupe=dedupe)
    placed = _handle(_task(**{"Task ID": "910022"}), first)
    assert placed["ok"] is True
    assert len(first.bland.calls) == 1
    second = _ports(call_dedupe=dedupe)
    again = _handle(_task(**{"Task ID": "910021"}), second)
    assert again.get("duplicate_suppressed") is True
    assert second.bland.calls == []


def test_test_dial_cannot_fall_through_to_a_client_number():
    def reader(name):
        assert name == JAKE_CELL_SECRET
        return TEST_CELL

    dial, error = select_dial_target(
        CLIENT_PHONE,
        env={"ROBIE_ENV": "TEST", "ROBIE_PHONE_REAL_CLIENTS": "1"},
        secret_reader=reader,
        hostname=TEST_HOST,
    )
    assert error is None
    assert dial == TEST_CELL
    assert dial != CLIENT_PHONE

    missing, error = select_dial_target(
        CLIENT_PHONE,
        env={"ROBIE_ENV": "TEST", "ROBIE_PHONE_REAL_CLIENTS": "1"},
        secret_reader=None,
        hostname=PROD_HOST,
    )
    assert missing is None
    assert "not dialing" in error

    forced, error = select_dial_target(
        CLIENT_PHONE,
        env={"ROBIE_ENV": "PRODUCTION", "ROBIE_PHONE_REAL_CLIENTS": "1"},
        secret_reader=reader,
        hostname=TEST_HOST,
    )
    assert forced == TEST_CELL
    assert forced != CLIENT_PHONE

    client, error = select_dial_target(
        CLIENT_PHONE,
        env={"ROBIE_ENV": "PRODUCTION", "ROBIE_PHONE_REAL_CLIENTS": "1"},
        secret_reader=None,
        hostname=PROD_HOST,
    )
    assert error is None
    assert client == CLIENT_PHONE

    seen = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout=0):
        del timeout
        data = getattr(request, "data", None)
        if data:
            body = json.loads(data.decode("utf-8"))
            seen.append(body)
            assert body["phone_number"] == TEST_CELL
            assert CLIENT_PHONE not in json.dumps(body)
        return _Response(
            b'{"status":"completed","call_id":"SYN-CALL","answered_by":"human","call_length":20}'
        )

    secrets = {"names": []}

    def secret_reader(name):
        secrets["names"].append(name)
        if name == "bland-dispatcher-kill-switch":
            return "0"
        if name == JAKE_CELL_SECRET:
            return TEST_CELL
        raise AssertionError(name)

    port = BlandTransportCallPort(
        env={
            "ROBIE_ENV": "TEST",
            "ROBIE_PHONE_LIVE_CALLS": "1",
            "ROBIE_PHONE_REAL_CLIENTS": "1",
            "ROBIE_BLAND_ALLOWED_ENVS": "TEST",
            "ROBIE_BLAND_ALLOWED_HOSTS": TEST_HOST,
            "ROBIE_BLAND_MAX_DURATION_MINUTES": "12",
        },
        hostname=TEST_HOST,
        api_key="SYN-KEY",
        secret_reader=secret_reader,
        urlopen=urlopen,
        execute=True,
        sleeper=lambda _seconds: None,
    )
    placed = port.place_call_with_double_dial(
        CLIENT_PHONE, "task", "Hi,", "voicemail",
    )
    assert placed["call_ids"] == ["SYN-CALL"]
    assert seen
    assert all(body["phone_number"] == TEST_CELL for body in seen)
    assert CLIENT_PHONE not in json.dumps(seen)

    def boom(*_args, **_kwargs):
        raise AssertionError("urlopen must not be called")

    blind = BlandTransportCallPort(
        env={
            "ROBIE_ENV": "TEST",
            "ROBIE_PHONE_REAL_CLIENTS": "1",
            "ROBIE_BLAND_ALLOWED_ENVS": "TEST",
            "ROBIE_BLAND_ALLOWED_HOSTS": TEST_HOST,
        },
        hostname=TEST_HOST,
        api_key="SYN-KEY",
        secret_reader=lambda name: "0" if name == "bland-dispatcher-kill-switch" else "",
        urlopen=boom,
        execute=True,
    )
    refused = blind.place_call_with_double_dial(CLIENT_PHONE, "task", "Hi,", "voicemail")
    assert refused["success"] is False
    assert refused["call_ids"] == []


class _IntakeNotes:
    def __init__(self):
        self.notes = []

    def get_discussion_ids(self, applicant_id):
        del applicant_id
        return ["D-100"]

    def append_note(self, discussion_id, body, note_type="Note", applicant_id=None):
        del note_type, applicant_id
        self.notes.append((discussion_id, body))
        return {"noteId": f"N-{len(self.notes)}"}


class _IntakeBland:
    def __init__(self):
        self.dials = 0

    def place_call_with_double_dial(self, *args, **kwargs):
        del args, kwargs
        self.dials += 1
        return {"success": False, "error": "test bland", "call_ids": []}

    def recent_calls(self, phone, since_seconds=1800):
        del phone, since_seconds
        return {"ok": False, "calls": []}

    def get_call_status(self, call_id):
        del call_id
        return {"ok": False}


def _intake_task(**overrides) -> AssignedTask:
    base = dict(
        task_id="910001",
        title="Task Note",
        description="Please call about the renewal.",
        applicant_id=JAKE_ACCOUNT,
        applicant_name="Jake Ferrara",
        assigned_to="Robie AI",
        due_date="2026-10-06",
        priority="Normal",
        created_date="2026-10-05",
        status="Open",
        discussion_id="D-100",
        last_modified="2026-10-05T09:00:00",
        created_by="Pat Example",
        assigned_producer="Jane Producer",
        activity_labels="Robie Call",
        created_at="2026-10-05T08:00:00",
        created_at_et="2026-10-05T09:00:00-04:00",
    )
    base.update(overrides)
    return AssignedTask(**base)


def _report(*tasks, message_id, digest, received_at):
    newest = ""
    for task in tasks:
        if task.created_at_et and task.created_at_et > newest:
            newest = task.created_at_et
    return IngestedReport(
        message_id=message_id,
        filename="Robie_AI_-_Task_Check-In_synthetic.csv",
        digest=digest,
        received_at=received_at,
        tasks=tuple(tasks),
        row_count=len(tasks),
        newest_created_et=newest,
    )


def _payloads(db):
    with JobStore(str(db)).connect() as conn:
        rows = conn.execute("SELECT payload_json FROM jobs").fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def test_tasks_labeled_before_enablement_never_dial(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["8"], report_digest="seed")
    clock = {"now": T1}
    notes = _IntakeNotes()
    bland = _IntakeBland()
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setenv(SPLICE_TEST_APPLICANT_ENV, JAKE_ACCOUNT)
    monkeypatch.setenv(
        "ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "910001,910002,910003",
    )
    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    monkeypatch.setenv("ROBIE_PLAYGROUND", "1")
    monkeypatch.setattr(
        "robie_job_engine.call_pickup.socket.gethostname", lambda: TEST_HOST,
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._intake_now", lambda: clock["now"],
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._build_discussion_client", lambda: notes,
    )
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, True),
    )
    monkeypatch.setattr(
        "robie_job_engine.report_email_source.build_default_gmail_service",
        lambda: object(),
    )
    old_splice = _intake_task(
        task_id="910002",
        activity_labels="Robie Audit Not Complete",
        description="Audit Not Complete",
        last_modified="2026-10-05T08:30:00",
    )
    live_call = _intake_task(task_id="910001", activity_labels="Robie Call")
    first = _report(
        live_call, old_splice,
        message_id="msg-1", digest="digest-1", received_at="2026-10-05T14:00:00Z",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        lambda _service: first,
    )
    assert run_intake(db_path=str(db), dry_run=False) == 0
    statuses = SeenTaskStore(str(db)).statuses()
    assert statuses["910002"] == "baseline"
    assert statuses["910001"] != "baseline"
    first_payloads = _payloads(db)
    assert [row["task_id"] for row in first_payloads] == ["910001"]
    assert first_payloads[0]["workflow"] == ""
    assert bland.dials == 0

    clock["now"] = T2
    new_splice = _intake_task(
        task_id="910003",
        activity_labels="robie-audit-not-complete",
        description="Audit Not Complete",
        created_at="2026-10-05T10:30:00",
        created_at_et="2026-10-05T11:30:00-04:00",
        last_modified="2026-10-05T10:30:00",
    )
    second = _report(
        live_call, old_splice, new_splice,
        message_id="msg-2", digest="digest-2", received_at="2026-10-05T16:00:00Z",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        lambda _service: second,
    )
    assert run_intake(db_path=str(db), dry_run=False) == 0
    statuses = SeenTaskStore(str(db)).statuses()
    assert statuses["910002"] == "baseline"
    payloads = {row["task_id"]: row for row in _payloads(db)}
    assert "910002" not in payloads
    assert payloads["910003"]["workflow"] == "audit_not_complete"
    assert payloads["910003"]["applicant_id"] == JAKE_ACCOUNT
    assert payloads["910003"]["splice_enabled_at"]
    assert bland.dials == 0


def test_handler_refuses_a_task_created_before_splice_enablement(monkeypatch):
    _test_env(monkeypatch)
    ports = _ports()
    result = _handle(_task(**{
        "Task ID": "910011",
        "Created Date": "2026-10-05T08:00:00",
        "splice_enabled_at": "2026-10-05T14:00:00+00:00",
    }), ports)
    assert result["ok"] is False
    assert ports.bland.calls == []
    assert "before this workflow was enabled" in result["error"]
