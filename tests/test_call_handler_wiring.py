"""Wiring tests: the #745 intake worker routes callback tasks to the
#746 Robie Call handler inside the same durable job, using the real
Job Engine checkpoint/status adapters.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine.ezlynx_task_report import AssignedTask
from robie_job_engine.task_assignment_worker import TaskAssignmentWorker

rch = pytest.importorskip(
    "robie_job_engine.robie_call_handler",
    reason="PR #746 not merged yet; call-handler wiring tests need it",
)


class FakeStore:
    """Minimal JobStore stand-in for the checkpoint adapter."""

    def __init__(self):
        self.checkpoints: Dict[str, Dict[str, Any]] = {}

    def connect(self):
        store = self

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a, **k):
                class Cursor:
                    def fetchone(self):
                        class Row:
                            def __getitem__(self, key):
                                return "job-1"
                        return Row()
                return Cursor()

        return Conn()

    def get_checkpoint(self, job_id: str, kind: str) -> Dict[str, Any]:
        return dict(self.checkpoints.get(kind, {}))

    def checkpoint(self, job_id: str, kind: str, data: Dict[str, Any]) -> None:
        self.checkpoints[kind] = dict(data)


class FakeBland:
    def __init__(self):
        self.dials = 0

    def place_call_with_double_dial(self, phone, eva_task, first_sentence,
                                    voicemail_message, metadata):
        self.dials += 1
        return {"success": True, "call_ids": ["call-1"],
                "attempts": [], "error": None}

    def get_call_status(self, call_id):
        return {"ok": True, "status": "completed",
                "answered_by": "human", "duration_s": 42}

    def recent_calls(self, phone, since_seconds=0):
        return {"ok": False}


class FakePhone:
    def get_phone(self, applicant_id):
        return "+15551234567"


class FakeReassigner:
    """Worker-protocol reassigner."""

    def __init__(self):
        self.calls: List[tuple] = []
        self.current = "Robie AI"

    def reassign(self, task_id, applicant_id, new_assignee, description="",
                 expected_assignee="Robie AI"):
        self.calls.append((task_id, new_assignee))
        self.current = new_assignee
        return new_assignee

    def read_assignee(self, task_id, applicant_id, description=""):
        return self.current


def make_task(**over) -> AssignedTask:
    kw = dict(
        task_id="63429200",
        title="Callback request",
        description="Call John Smith about his renewal documents",
        applicant_id="25486200",
        applicant_name="John Smith",
        assigned_to="Robie AI",
        due_date="2026-10-10",
        priority="Normal",
        created_date="2026-10-03",
        status="Open",
        discussion_id="D-200",
        last_modified="2026-10-03T10:00:00Z",
        created_by="Jane Producer",
        assigned_producer="",
        csr="",
    )
    kw.update(over)
    return AssignedTask(**kw)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    rch._reset_module_state_for_tests()
    calls = {"n": 0}

    def fake_writeback(discussion_client, applicant_id, body, title_hint=None):
        calls["n"] += 1
        return {"status": "filed", "note_id": f"NOTE-{calls['n']}",
                "discussion_id": "D-200", "applicant_id": applicant_id}

    monkeypatch.setattr(rch, "_writeback_outcome_note", fake_writeback)
    return calls


def test_callback_routes_to_call_handler(clean_state):
    store = FakeStore()
    bland = FakeBland()
    reassigner = FakeReassigner()
    worker = TaskAssignmentWorker(
        discussion_client=object(),
        task_reassigner=reassigner,
        reassign_enabled=True,
        phone_lookup=FakePhone(),
        bland_client=bland,
        call_dry_run=False,
    )
    assert worker._call_handler_available() is True
    job = {"id": "job-1", "payload": {
        "task_id": "63429200", "applicant_id": "25486200",
        "account_name": "John Smith", "assigned_to": "Robie AI",
        "title": "Callback request",
        "description": "Call John Smith about his renewal documents",
        "due_date": "2026-10-10", "priority": "Normal",
        "created_date": "2026-10-03", "task_status": "Open",
        "discussion_id": "D-200", "last_modified": "2026-10-03T10:00:00Z",
        "task_created_by": "Jane Producer",
        "assigned_producer": "", "csr": "",
    }}
    action = worker._do_work(store, job)
    assert action["call_task"] is True
    assert action["ok"] is True
    assert action["outcome_successful"] is True
    assert bland.dials == 1
    # The handler checkpointed inside the Job Engine job (kind robie-call).
    assert "robie-call" in store.checkpoints
    # Reassignment went through the worker-protocol adapter.
    assert reassigner.calls == [("63429200", "Jane Producer")]


def test_callback_without_ports_takes_generic_path():
    worker = TaskAssignmentWorker(discussion_client=object())
    assert worker._call_handler_available() is False


def test_non_callback_task_not_routed_to_handler(clean_state):
    store = FakeStore()
    bland = FakeBland()
    worker = TaskAssignmentWorker(
        discussion_client=object(),
        phone_lookup=FakePhone(),
        bland_client=bland,
        call_dry_run=False,
    )
    task = make_task(title="Document request",
                     description="Upload the renewal documents to EZLynx")
    # _do_work would continue down the generic path; here we only assert
    # the router does not claim it.
    assert worker._categorize_task(task) == "document"
    assert bland.dials == 0
