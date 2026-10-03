"""Review-blocker regression tests for the Robie Call handler.

Covers the exact failure modes the review reproduced against PRs #745/#746:
  1. false-success: a Bland terminal status of failed/busy/no-answer/canceled
     must NOT count as task success ("call ended" != "task succeeded").
  2. swallowed-checkpoint-error: a checkpoint save failure must surface loudly
     (and never let the handler proceed as if the dial were recorded).
  3. dial-intent recovery: a timeout during the Bland POST (intent saved, no
     call_ids) must reconcile via Bland recent-calls, never auto-redial.
  4. reassign ownership: a task no longer assigned to Robie must NOT be
     reassigned (refuse + chat alert).
  5. write-once notes: a restart after a filed note must not post it again.
  6. authorization: a task assigned to someone other than Robie must not dial.
  7. identity gate: instruction naming a different person than the applicant
     must fail closed before dialing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine import robie_call_handler as rch


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCheckpoint:
    def __init__(self, fail_on_save: bool = False):
        self.data: Dict[str, Dict[str, Any]] = {}
        self.fail_on_save = fail_on_save
        self.saves = 0

    def get_checkpoint(self, key: str) -> Dict[str, Any]:
        return dict(self.data.get(key, {}))

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        self.saves += 1
        if self.fail_on_save:
            raise RuntimeError("disk full (simulated)")
        self.data[key] = dict(value)


class FakeBland:
    def __init__(
        self,
        call_ids: Optional[List[str]] = None,
        statuses: Optional[Dict[str, Dict[str, Any]]] = None,
        recent: Optional[List[Dict[str, Any]]] = None,
        raise_on_dial: bool = False,
    ):
        self._call_ids = call_ids or ["call-1"]
        self._statuses = statuses or {}
        self._recent = recent
        self._raise_on_dial = raise_on_dial
        self.dials = 0

    def place_call_with_double_dial(
        self, phone: str, eva_task: str, first_sentence: str,
        voicemail_message: str, metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.dials += 1
        if self._raise_on_dial:
            raise TimeoutError("POST timed out after Bland accepted (simulated)")
        return {"success": True, "call_ids": list(self._call_ids),
                "attempts": [], "error": None}

    def get_call_status(self, call_id: str) -> Dict[str, Any]:
        st = self._statuses.get(call_id, {"status": "completed",
                                          "answered_by": "human",
                                          "duration_s": 42})
        return {"ok": True, **st}

    def recent_calls(self, phone: str, since_seconds: int = 0) -> Dict[str, Any]:
        if self._recent is None:
            return {"ok": False}
        return {"ok": True, "calls": list(self._recent)}


class FakePhone:
    def __init__(self, phone: str = "+15551234567"):
        self._phone = phone

    def get_phone(self, applicant_id: str) -> Optional[str]:
        return self._phone


class FakeReassign:
    """TaskReassignmentPort fake with controllable assignee reads."""

    def __init__(self, current_assignee: str = "Robie AI"):
        self.current_assignee = current_assignee
        self.reassigns: List[tuple] = []

    def reassign_task(self, task_id: str, new_assignee: str,
                      note: str = "") -> Dict[str, Any]:
        self.reassigns.append((task_id, new_assignee))
        self.current_assignee = new_assignee
        return {"ok": True}

    def read_task_assignee(self, task_id: str) -> Optional[str]:
        return self.current_assignee


class WritebackSpy:
    """Monkeypatched _writeback_outcome_note: counts posts, returns filed."""

    def __init__(self):
        self.calls = 0

    def __call__(self, discussion_client: Any, applicant_id: str,
                 body: str, title_hint: Any = None) -> Dict[str, Any]:
        self.calls += 1
        return {"status": "filed", "note_id": f"NOTE-{self.calls}",
                "discussion_id": "D1", "applicant_id": applicant_id}


def make_ports(**over) -> Any:
    checkpoint = over.get("checkpoint", FakeCheckpoint())
    bland = over.get("bland", FakeBland())
    reassign = over.get("reassign", FakeReassign())
    return rch.RobieCallPorts(
        phone_lookup=over.get("phone", FakePhone()),
        bland=bland,
        discussion_client=object(),
        task_reassign=reassign,
        recording_upload=None,
        chat_alert=lambda text: True,
        task_status=None,
        job_checkpoint=checkpoint,
    )


def make_config(**over) -> Any:
    kw = dict(dry_run=False, require_outcome_verification=True,
              outcome_poll_tries=1, outcome_poll_interval_s=0)
    kw.update(over)
    return rch.RobieCallConfig(**kw)


def make_task(**over) -> Dict[str, Any]:
    task = {
        "task_id": "T-100",
        "Task Subject": "Please call about renewal",
        "Task Description": "Call John Smith about his renewal documents",
        "Applicant ID": "A-100",
        "Applicant Name": "John Smith",
        "Task Created By": "Jane Producer",
        "Assigned To": "Robie AI",
        "Task Due Date": "2026-10-10",
    }
    task.update(over)
    return task


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    rch._reset_module_state_for_tests()
    spy = WritebackSpy()
    monkeypatch.setattr(rch, "_writeback_outcome_note", spy)
    return spy


# ---------------------------------------------------------------------------
# 1. false-success: terminal but unsuccessful calls must not be ok
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["failed", "busy", "no-answer", "canceled"])
def test_terminal_unsuccessful_call_is_not_ok(clean_state, status):
    bland = FakeBland(statuses={"call-1": {"status": status,
                                           "answered_by": "",
                                           "duration_s": 0}})
    ports = make_ports(bland=bland)
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert result["ok"] is False
    assert result["outcome_verified"] is True
    assert result["outcome_successful"] is False
    assert result["reassigned"] is False
    assert result["chat_alerted"] is True


def test_completed_human_call_is_ok(clean_state):
    ports = make_ports()  # default: completed/human/42s
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert result["ok"] is True
    assert result["outcome_verified"] is True
    assert result["outcome_successful"] is True
    assert result["reassigned"] is True


def test_completed_zero_duration_is_not_successful(clean_state):
    bland = FakeBland(statuses={"call-1": {"status": "completed",
                                           "answered_by": "unknown",
                                           "duration_s": 0}})
    ports = make_ports(bland=bland)
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert result["ok"] is False
    assert result["outcome_successful"] is False


# ---------------------------------------------------------------------------
# 2. swallowed-checkpoint-error: save failure must raise loudly
# ---------------------------------------------------------------------------

def test_checkpoint_save_failure_raises(clean_state):
    """The Job Engine adapter must NOT swallow a checkpoint save failure.

    A failed checkpoint write means a restart could redial — the error must
    propagate so the handler reconciles instead of proceeding blindly.
    """
    from robie_job_engine.robie_call_job_engine_adapters import (
        JobEngineCheckpointAdapter,
    )

    class ExplodingStore:
        def connect(self):
            raise AssertionError("should not be called")

    class FakeCursor:
        def fetchone(self):
            class Row:
                def __getitem__(self, k):
                    return "job-1"
            return Row()

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return FakeCursor()

    class StoreWithFailingCheckpoint:
        def connect(self):
            return FakeConn()

        def get_checkpoint(self, job_id, kind):
            return {}

        def checkpoint(self, job_id, kind, data):
            raise RuntimeError("disk full (simulated)")

    adapter = JobEngineCheckpointAdapter(StoreWithFailingCheckpoint())
    with pytest.raises(RuntimeError, match="disk full"):
        adapter.set_checkpoint("robie-call:T-100", {"x": 1})


def test_dial_intent_checkpoint_saved_before_dial(clean_state):
    checkpoint = FakeCheckpoint()
    ports = make_ports(checkpoint=checkpoint)
    rch.handle_robie_call_task(make_task(), make_config(), ports)
    # After a full successful run the intent is cleared and call_ids recorded.
    saved = checkpoint.data.get("robie-call:T-100", {})
    assert saved.get("bland_call_ids") == ["call-1"]
    assert not (saved.get("dial_intent") or {}).get("status") == "dial_attempted"


# ---------------------------------------------------------------------------
# 3. dial-intent recovery: timeout during POST reconciles, never redials
# ---------------------------------------------------------------------------

def test_timeout_during_dial_reconciles_instead_of_redial(clean_state):
    # First run: no recent call (guard passes), dial intent saved, then the
    # POST raises (timeout after Bland accepted).
    checkpoint = FakeCheckpoint()
    bland = FakeBland(raise_on_dial=True, recent=None)
    ports = make_ports(checkpoint=checkpoint, bland=bland)
    first = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert first["ok"] is False
    assert bland.dials == 1
    intent = checkpoint.data.get("robie-call:T-100", {}).get("dial_intent") or {}
    assert intent.get("status") == "dial_attempted"

    # Simulate a restart: clear in-memory idempotency, keep the checkpoint.
    rch._reset_module_state_for_tests()
    # Second run: must NOT dial again; reconciles via recent_calls.
    bland2 = FakeBland(call_ids=["call-99"],
                       statuses={"call-99": {"status": "completed",
                                             "answered_by": "human",
                                             "duration_s": 30}},
                       recent=[{"call_id": "call-99", "id": "call-99"}])
    ports2 = make_ports(checkpoint=checkpoint, bland=bland2)
    second = rch.handle_robie_call_task(make_task(), make_config(), ports2)
    assert bland2.dials == 0, "must never redial after a dial intent"
    assert second["ok"] is True
    assert second["call"]["call_ids"] == ["call-99"]


def test_dial_intent_with_no_recent_call_fails_closed(clean_state):
    checkpoint = FakeCheckpoint()
    bland = FakeBland(raise_on_dial=True, recent=[])
    ports = make_ports(checkpoint=checkpoint, bland=bland)
    rch.handle_robie_call_task(make_task(), make_config(), ports)

    rch._reset_module_state_for_tests()
    bland2 = FakeBland(recent=[])
    ports2 = make_ports(checkpoint=checkpoint, bland=bland2)
    second = rch.handle_robie_call_task(make_task(), make_config(), ports2)
    assert bland2.dials == 0
    assert second["ok"] is False
    assert "NOT redialing" in (second.get("error") or "")


# ---------------------------------------------------------------------------
# 4. reassign ownership: not owned by Robie -> refuse
# ---------------------------------------------------------------------------

def test_reassign_refused_when_task_not_owned_by_robie(clean_state):
    reassign = FakeReassign(current_assignee="Jane Producer")
    ports = make_ports(reassign=reassign)
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert result["ok"] is True  # the call itself succeeded
    assert result["reassigned"] is False
    assert reassign.reassigns == [], "must not touch a task owned by someone else"
    assert "refusing to reassign" in (result.get("reassign_error") or "")
    assert result["chat_alerted"] is True


def test_reassign_verified_by_exact_task_id(clean_state):
    reassign = FakeReassign(current_assignee="Robie AI")
    ports = make_ports(reassign=reassign)
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert result["reassigned"] is True
    assert reassign.reassigns == [("T-100", "Jane Producer")]


# ---------------------------------------------------------------------------
# 5. write-once: restart after a filed note does not re-post
# ---------------------------------------------------------------------------

def test_note_not_reposted_after_restart(clean_state):
    checkpoint = FakeCheckpoint()
    ports = make_ports(checkpoint=checkpoint)
    first = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert first["ok"] is True
    assert clean_state.calls == 1

    # Restart: clear in-memory state, keep the durable checkpoint.
    rch._reset_module_state_for_tests()
    ports2 = make_ports(checkpoint=checkpoint)
    second = rch.handle_robie_call_task(make_task(), make_config(), ports2)
    assert clean_state.calls == 1, "note must not be posted twice"
    assert second.get("duplicate_suppressed") is True


# ---------------------------------------------------------------------------
# 6. authorization: task not assigned to Robie must not dial
# ---------------------------------------------------------------------------

def test_task_assigned_to_other_person_does_not_dial(clean_state):
    bland = FakeBland()
    ports = make_ports(bland=bland)
    task = make_task(**{"Assigned To": "Jane Producer"})
    result = rch.handle_robie_call_task(task, make_config(), ports)
    assert result["ok"] is False
    assert bland.dials == 0
    assert "not Robie" in (result.get("error") or "")


# ---------------------------------------------------------------------------
# 7. identity gate: instruction naming someone else fails closed
# ---------------------------------------------------------------------------

def test_instruction_naming_different_person_fails_closed(clean_state):
    bland = FakeBland()
    ports = make_ports(bland=bland)
    task = make_task(**{
        "Task Description": "Call Mary Johnson about her renewal documents",
    })
    result = rch.handle_robie_call_task(task, make_config(), ports)
    assert result["ok"] is False
    assert bland.dials == 0
    assert "Mary Johnson" in (result.get("error") or "")
