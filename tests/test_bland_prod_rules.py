"""Prod call rules: holidays, test-cell dialing, opt-out, and kill switch.

Bland, Secret Manager, and EZLynx are fakes. No number is hardcoded as
Jake's cell, and logs must not contain a full phone number.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from robie_job_engine import robie_call_handler as rch
from robie_job_engine.bland_call_port import (
    JAKE_CELL_SECRET,
    BlandTransportCallPort,
    select_dial_target,
)
from robie_job_engine.bland_transport import (
    BLAND_KEY_SECRET_PROD,
    BLAND_KEY_SECRET_TEST,
    KILL_SWITCH_SECRET,
    bland_api_key_secret,
)
from robie_job_engine.call_opt_out import CallOptOutStore, pressed_opt_out
from robie_job_engine.ezlynx_driver_gate import DriverDecision
from robie_job_engine.robie_call_handler import (
    RobieCallConfig,
    RobieCallPorts,
    handle_robie_call_task,
)

TEST_APPLICANT = "220250093"
IN_WINDOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))
COLUMBUS_DAY = datetime(2026, 10, 12, 10, 0, tzinfo=ZoneInfo("America/New_York"))
TEST_CELL = "+17325550199"
CLIENT = "+17325550142"
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
    def get_phone(self, applicant_id):
        return "732-555-0142" if applicant_id == TEST_APPLICANT else None


class _Bland:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "success": True,
            "call_ids": ["call-1"],
            "attempts": [{"attempt": 1, "success": True,
                          "final_status": {"status": "completed",
                                           "answered_by": "human",
                                           "duration": 30}}],
            "voicemail_hit": False,
            "redialed": False,
            "error": None,
        }

    def place_call_with_double_dial(self, phone, task_text, first_sentence, voicemail, metadata=None):
        self.calls.append({"phone": phone, "task_text": task_text})
        return dict(self.result)

    def recent_calls(self, phone, since_seconds=1800):
        return {"ok": True, "calls": []}

    def get_call_status(self, call_id):
        return {"ok": True, "status": "completed", "answered_by": "human", "duration_s": 30}


class _Discussions:
    def __init__(self):
        self.appended = []
        self._notes = {}
        self.discussions = [{"discussionId": "D-100", "title": "Renewal chat"}]

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
            "notes": [{"noteId": nid} for nid in self._notes.get(discussion_id, [])],
        }


def _task(**overrides):
    task = {
        "Task ID": "TASK-RULES",
        "Task Subject": "Please call about renewal",
        "Task Description": "Please call John about his renewal.",
        "Applicant ID": TEST_APPLICANT,
        "Account Name": "John Test",
        "Assigned Producer": "Jane Producer",
        "Activity Labels": "Robie Call",
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


def _capture(logger_name):
    logs = []

    class _Handler(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    handler = _Handler()
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logs, logger, handler


def test_federal_holiday_queues_and_does_not_dial():
    rch._reset_module_state_for_tests()
    ports = _ports()
    result = handle_robie_call_task(
        _task(), RobieCallConfig(dry_run=False, now=COLUMBUS_DAY), ports,
    )
    assert result["ok"] is False
    assert result["queued_for_calling_window"] is True
    assert ports.bland.calls == []
    assert "holiday" in result["error"]


def test_unlabeled_do_not_call_and_splice_place_zero_dials():
    rch._reset_module_state_for_tests()
    cases = [
        {"Activity Labels": "", "Task Description": "Please call John about his renewal."},
        {"Activity Labels": "", "Task Description": "Do not call or contact anyone."},
        {"Activity Labels": "", "Task Subject": "[CALLBACK REQUIRED]"},
        {"Activity Labels": "Robie audit", "Task Description": "Splice audit follow-up."},
    ]
    for overrides in cases:
        ports = _ports()
        result = handle_robie_call_task(
            _task(**overrides), RobieCallConfig(dry_run=False, now=IN_WINDOW), ports,
        )
        assert result["ok"] is False
        assert ports.bland.calls == []


def test_dry_run_writes_nothing_and_names_the_producer():
    rch._reset_module_state_for_tests()
    ports = _ports()
    result = handle_robie_call_task(_task(), RobieCallConfig(now=IN_WINDOW), ports)
    assert result["writeback"]["reason"] == "dry run: nothing written"
    assert ports.discussion_client.appended == []
    assert ports.bland.calls == []
    labeled = rch._format_outcome_note(
        "John Test", "Please call John about his renewal.",
        {"success": True, "dry_run": True, "call_ids": [], "attempts": []},
        "skipped", producer_name="Jane Producer",
    )
    assert "DRY RUN" in labeled
    assert "Jane Producer" in labeled
    assert "lost track" not in labeled
    assert "Jake" not in labeled


def test_test_mode_dials_only_the_secret_cell_and_logs_the_last_four():
    source = Path("robie_job_engine/bland_call_port.py").read_text(encoding="utf-8")
    assert TEST_CELL not in source
    assert "7326688161" not in source
    logs, logger, handler = _capture("robie_job_engine.bland_call_port")
    try:
        missing, error = select_dial_target(CLIENT, env={}, secret_reader=None)
        assert missing is None
        assert "not dialing" in error

        def reader(name):
            assert name == JAKE_CELL_SECRET
            return TEST_CELL

        dial, error = select_dial_target(CLIENT, env={}, secret_reader=reader)
        assert error is None
        assert dial == TEST_CELL
        text = "\n".join(logs)
        assert TEST_CELL not in text
        assert CLIENT not in text
        assert TEST_CELL[-4:] in text
    finally:
        logger.removeHandler(handler)


def test_real_client_flag_posts_the_client_number_and_a_policy_number_is_refused():
    refused, error = select_dial_target(
        "7685786571", env={"ROBIE_PHONE_REAL_CLIENTS": "1"}, secret_reader=None,
    )
    assert refused is None
    assert "not E.164" in error
    dial, error = select_dial_target(
        CLIENT, env={"ROBIE_PHONE_REAL_CLIENTS": "1"}, secret_reader=None,
    )
    assert error is None
    assert dial == CLIENT


def test_kill_switch_secret_halts_before_the_socket_and_before_the_test_cell():
    seen = {"names": [], "sockets": 0}

    def reader(name):
        seen["names"].append(name)
        if name == KILL_SWITCH_SECRET:
            return "halt"
        raise AssertionError(name)

    def boom(*_args, **_kwargs):
        seen["sockets"] += 1
        raise AssertionError("urlopen must not be called")

    port = BlandTransportCallPort(
        env={"ROBIE_ENV": "TEST", "ROBIE_PHONE_LIVE_CALLS": "1"},
        hostname="hermes-test-01",
        api_key="SYN-KEY",
        secret_reader=reader,
        urlopen=boom,
        execute=True,
    )
    result = port.place_call_with_double_dial(CLIENT, "task", "hi", "vm")
    assert result["success"] is False
    assert seen["sockets"] == 0
    assert seen["names"] == [KILL_SWITCH_SECRET]
    assert bland_api_key_secret({"ROBIE_ENV": "PRODUCTION"}) == BLAND_KEY_SECRET_PROD
    assert bland_api_key_secret({"ROBIE_ENV": "TEST"}) == BLAND_KEY_SECRET_TEST


def test_opt_out_skips_the_dial_and_press_six_records_the_next_skip(tmp_path: Path):
    store = CallOptOutStore(tmp_path / "optouts.sqlite")
    rch._reset_module_state_for_tests()
    store.record_opt_out(TEST_APPLICANT, source="press-6")
    ports = _ports(opt_out_store=store)
    skipped = handle_robie_call_task(
        _task(), RobieCallConfig(dry_run=False, now=IN_WINDOW), ports,
    )
    assert skipped["ok"] is False
    assert ports.bland.calls == []
    assert "opted out" in ports.discussion_client.appended[0]["body"].lower()

    fresh = CallOptOutStore(tmp_path / "press.sqlite")
    bland = _Bland(result={
        "success": True,
        "call_ids": ["call-6"],
        "digits": "6",
        "attempts": [{"attempt": 1, "success": True,
                      "final_status": {"status": "completed",
                                       "answered_by": "human", "duration": 12}}],
        "error": None,
    })
    rch._reset_module_state_for_tests()
    first = _ports(bland=bland, opt_out_store=fresh)
    placed = handle_robie_call_task(
        _task(**{"Task ID": "TASK-PRESS"}),
        RobieCallConfig(dry_run=False, now=IN_WINDOW),
        first,
    )
    assert placed["ok"] is True
    assert len(first.bland.calls) == 1
    assert fresh.is_opted_out(TEST_APPLICANT) is True
    assert pressed_opt_out({"digits": "16"}) is False
    assert pressed_opt_out({"digits": ["1", "4"]}) is False

    rch._reset_module_state_for_tests()
    second = _ports(opt_out_store=fresh)
    again = handle_robie_call_task(
        _task(**{
            "Task ID": "TASK-NEXT",
            "Task Description": "Please call John about the audit documents.",
        }),
        RobieCallConfig(dry_run=False, now=IN_WINDOW),
        second,
    )
    assert again["ok"] is False
    assert second.bland.calls == []
