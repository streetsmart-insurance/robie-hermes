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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine import robie_call_handler as rch

IN_WINDOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))


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
        self.body = body
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
        transfer_lookup=over.get("transfer_lookup"),
    )


def make_config(**over) -> Any:
    # NOTE: there is no verification opt-out. Outcome verification is
    # unconditional: a placement ack alone never marks the task ok.
    kw = dict(dry_run=False, now=IN_WINDOW,
              outcome_poll_tries=1, outcome_poll_interval_s=0)
    kw.update(over)
    return rch.RobieCallConfig(**kw)


def make_task(**over) -> Dict[str, Any]:
    task = {
        "task_id": "T-100",
        "Task Subject": "Please call about renewal",
        "Task Description": "Call John Smith about his renewal documents. Call at 555-123-4567.",
        "Applicant ID": "A-100",
        "Applicant Name": "John Smith",
        "Task Created By": "Jane Producer",
        "Assigned Producer": "Jane Producer",
        "Assigned To": "Robie AI",
        "Task Due Date": "2026-10-10",
        "Activity Labels": "Robie Call",
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
        "Activity Labels": "Robie Lead Follow Up",
        "Task Description": "Call Mary Johnson about her renewal documents",
    })
    result = rch.handle_robie_call_task(task, make_config(), ports)
    assert result["ok"] is False
    assert bland.dials == 0
    assert "Mary Johnson" in (result.get("error") or "")


# ---------------------------------------------------------------------------
# 8. checkpoint failures are real failures (fail or retry), never success
# ---------------------------------------------------------------------------

class FailingReadCheckpoint(FakeCheckpoint):
    def get_checkpoint(self, key: str) -> Dict[str, Any]:
        raise RuntimeError("read failed (simulated)")


class FailNthSaveCheckpoint(FakeCheckpoint):
    """Fails exactly the Nth set_checkpoint call, succeeds the rest."""

    def __init__(self, fail_at: int):
        super().__init__()
        self.fail_at = fail_at

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        self.saves += 1
        if self.saves == self.fail_at:
            raise RuntimeError(f"save #{self.saves} failed (simulated)")
        self.data[key] = dict(value)


class FailFromSaveCheckpoint(FakeCheckpoint):
    """Fails every set_checkpoint call from N onward (persistent outage).

    Use when the save under test is retried: a single-shot failure would
    be masked by _save_checkpoint_retrying, which is the designed
    behavior for transient errors."""

    def __init__(self, fail_from: int):
        super().__init__()
        self.fail_from = fail_from

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        self.saves += 1
        if self.saves >= self.fail_from:
            raise RuntimeError(f"save #{self.saves} failed (simulated outage)")
        self.data[key] = dict(value)


class FlakyCheckpoint(FakeCheckpoint):
    """Fails the first N saves (transient), then succeeds."""

    def __init__(self, fail_first_n: int):
        super().__init__()
        self.fail_first_n = fail_first_n

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        self.saves += 1
        if self.saves <= self.fail_first_n:
            raise RuntimeError(f"transient failure #{self.saves} (simulated)")
        self.data[key] = dict(value)


def test_save_checkpoint_raises_not_swallows():
    """Fix 1 (unit): a failed checkpoint write raises CheckpointError —
    it is never caught and ignored."""
    ports = make_ports(checkpoint=FakeCheckpoint(fail_on_save=True))
    with pytest.raises(rch.CheckpointError):
        rch._save_checkpoint(ports, "T-1", {"a": 1})


def test_checkpoint_write_retried_then_succeeds(monkeypatch):
    """Fix 1 (unit): transient write failures are retried with backoff."""
    monkeypatch.setattr(rch.time, "sleep", lambda s: None)
    ports = make_ports(checkpoint=FlakyCheckpoint(fail_first_n=2))
    rch._merge_checkpoint(ports, "T-1", {"a": 1})  # must not raise
    cp = ports.job_checkpoint
    assert cp.data[rch._checkpoint_key("T-1")] == {"a": 1}
    assert cp.saves == 3  # 2 transient failures + 1 success


def test_checkpoint_write_gives_up_after_retries(monkeypatch):
    """Fix 1 (unit): persistent write failures raise after retries."""
    monkeypatch.setattr(rch.time, "sleep", lambda s: None)
    ports = make_ports(checkpoint=FakeCheckpoint(fail_on_save=True))
    with pytest.raises(rch.CheckpointError):
        rch._merge_checkpoint(ports, "T-1", {"a": 1})


def test_dial_intent_save_failure_means_zero_dials(clean_state):
    """Fix 1: if the pre-dial intent cannot be saved, the handler must NOT
    dial. Zero Bland calls; the task fails open for a later retry."""
    bland = FakeBland()
    ports = make_ports(bland=bland, checkpoint=FakeCheckpoint(fail_on_save=True))
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert result["ok"] is False
    assert bland.dials == 0
    assert result.get("checkpoint_failed") is True
    assert "dial intent" in (result.get("error") or "").lower()


def test_post_dial_checkpoint_failure_fails_loudly(clean_state):
    """Fix 1: the dial went out but the post-dial checkpoint write failed.
    The task must fail (never report success) and alert — the next run
    reconciles via recent-calls instead of redialing."""
    bland = FakeBland()
    # save #1 = dial intent (6a, must succeed or no dial); the post-dial
    # merge (save #2, plus its retries) hits a persistent outage.
    ports = make_ports(bland=bland, checkpoint=FailFromSaveCheckpoint(fail_from=2))
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert bland.dials == 1  # the call did go out
    assert result["ok"] is False
    assert result.get("checkpoint_failed") is True
    assert result.get("chat_alerted") is True
    assert "checkpoint" in (result.get("error") or "").lower()


def test_completion_checkpoint_failure_is_not_ok(clean_state):
    """Fix 1: call placed, note filed, but the completion marker could not
    be saved. The task must NOT report ok=True with a lost checkpoint."""
    bland = FakeBland()
    # saves: 1=intent, 2=post-dial, 3=note marker succeed; the completion
    # merge (save #4 + retries) hits a persistent outage.
    ports = make_ports(bland=bland, checkpoint=FailFromSaveCheckpoint(fail_from=4))
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert bland.dials == 1
    assert result["ok"] is False
    assert result.get("checkpoint_failed") is True


# ---------------------------------------------------------------------------
# 9. duplicate-call protection: retry/restart never dials twice
# ---------------------------------------------------------------------------

def test_checkpoint_read_failure_means_zero_dials(clean_state):
    """Fix 2: an unreadable checkpoint blinds every anti-redial guard
    (completed_at, call_ids, dial_intent). The handler must NOT dial."""
    bland = FakeBland()
    ports = make_ports(bland=bland, checkpoint=FailingReadCheckpoint())
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert result["ok"] is False
    assert bland.dials == 0
    assert "checkpoint unreadable" in (result.get("error") or "").lower()


def test_restart_after_completed_dial_never_redials(clean_state):
    """Fix 2: a process restart after a completed dial reconciles from the
    durable checkpoint — it must not place a second call."""
    bland = FakeBland()
    checkpoint = FakeCheckpoint()
    ports = make_ports(bland=bland, checkpoint=checkpoint)
    r1 = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert r1["ok"] is True
    assert bland.dials == 1

    # Simulate a process restart: wipe in-memory state, keep the durable
    # checkpoint, re-run the same task.
    rch._reset_module_state_for_tests()
    ports2 = make_ports(bland=bland, checkpoint=checkpoint)
    r2 = rch.handle_robie_call_task(make_task(), make_config(), ports2)
    assert bland.dials == 1  # no second call
    assert r2.get("duplicate_suppressed") is True


def test_restart_after_dial_reconciles_outcome(clean_state):
    """Fix 2: a restart after the dial but before completion recovers the
    call_ids from the checkpoint and verifies the outcome — no redial."""
    bland = FakeBland()
    checkpoint = FakeCheckpoint()
    ports = make_ports(bland=bland, checkpoint=checkpoint)
    # Run 1: dial, then simulate a crash by wiping in-memory state BEFORE
    # completion is recorded. We do this by failing the completion save.
    ports_fail = make_ports(
        bland=bland, checkpoint=FailNthSaveCheckpoint(fail_at=99))
    # Instead: manually seed the checkpoint as run 1 would have left it
    # after step 7 (call_ids saved, not completed).
    checkpoint.data[rch._checkpoint_key("T-100")] = {
        "bland_call_ids": ["call-1"],
        "phone": "+15551234567",
        "completed_at": None,
        "dial_intent": None,
    }
    rch._reset_module_state_for_tests()
    ports2 = make_ports(bland=bland, checkpoint=checkpoint)
    r2 = rch.handle_robie_call_task(make_task(), make_config(), ports2)
    assert bland.dials == 0  # recovered, never redialed
    assert r2.get("recovered_from_checkpoint") is True
    assert r2["ok"] is True  # FakeBland reports completed/human


def test_failed_intent_save_then_retry_dials_once(clean_state):
    """Fix 2: run 1 fails to save the intent (no dial); run 2 with a fixed
    store dials exactly once. No double-dial across the retry."""
    bland = FakeBland()
    checkpoint = FakeCheckpoint(fail_on_save=True)
    ports = make_ports(bland=bland, checkpoint=checkpoint)
    r1 = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert r1["ok"] is False
    assert bland.dials == 0

    # Store recovers; retry the same task.
    checkpoint.fail_on_save = False
    rch._reset_module_state_for_tests()
    ports2 = make_ports(bland=bland, checkpoint=checkpoint)
    r2 = rch.handle_robie_call_task(make_task(), make_config(), ports2)
    assert r2["ok"] is True
    assert bland.dials == 1


# ---------------------------------------------------------------------------
# 10. free-form calling: explicit number, on-behalf-of, frustration transfer
# ---------------------------------------------------------------------------

class FakeTransferLookup:
    """TransferLookupPort fake: name -> direct dial number."""

    def __init__(self, directory=None, raise_on_lookup: bool = False):
        self.directory = directory or {}
        self.raise_on_lookup = raise_on_lookup
        self.lookups: List[str] = []

    def get_transfer_number(self, assignee_name: str) -> Optional[str]:
        self.lookups.append(assignee_name)
        if self.raise_on_lookup:
            raise RuntimeError("RingCentral down (simulated)")
        return self.directory.get(assignee_name)


def test_extract_explicit_phone():
    assert rch._extract_explicit_phone(
        "Call Progressive at 1-800-776-4737 about the surcharge") == "+18007764737"
    assert rch._extract_explicit_phone(
        "Call John Smith about his renewal documents") is None
    assert rch._extract_explicit_phone("") is None


def test_explicit_phone_overrides_applicant_lookup(clean_state):
    """Free-form: a number in the task wins over the applicant's number."""
    bland = FakeBland()
    ports = make_ports(bland=bland, phone=FakePhone("+15551234567"))
    task = make_task(**{
        "Task Description": "Call Progressive at 1-800-776-4737 about the policy.",
    })
    result = rch.handle_robie_call_task(task, make_config(), ports)
    assert result["ok"] is True
    assert bland.dials == 1
    # The dialed number is the task's, not the applicant's.
    assert ports.job_checkpoint.data[
        rch._checkpoint_key("T-100")]["phone"] == "+18007764737"


def test_robie_call_without_a_typed_number_asks_and_does_not_use_the_file(clean_state):
    """Robie Call never falls back to the phone on file."""
    bland = FakeBland()
    looked = []

    class CountingPhone(FakePhone):
        def get_phone(self, applicant_id: str) -> Optional[str]:
            looked.append(applicant_id)
            return self._phone

    ports = make_ports(bland=bland, phone=CountingPhone("+15551234567"))
    task = make_task(**{
        "Task Description": "Call John Smith about his renewal documents",
    })
    result = rch.handle_robie_call_task(task, make_config(), ports)
    assert result["ok"] is False
    assert bland.dials == 0
    assert looked == []
    assert result.get("clarification_note_filed") is True
    assert "Type the phone number to call" in clean_state.body


def test_third_party_with_explicit_number_is_on_behalf_of(clean_state):
    """'Call Progressive at 1-800-...' on Mary Smith's account: allowed as
    an on-behalf-of call. Everything logs on the applicant's account."""
    bland = FakeBland()
    ports = make_ports(bland=bland)
    task = make_task(**{
        "Applicant Name": "Mary Smith",
        "Task Description": (
            "Call Progressive at 1-800-776-4737 about Mary Smith's "
            "policy surcharge."),
    })
    result = rch.handle_robie_call_task(task, make_config(), ports)
    assert result["ok"] is True
    assert bland.dials == 1
    # Eva's prompt frames it as on-behalf-of.
    eva_task = rch._build_eva_task(
        task["Task Description"], "Mary Smith", on_behalf_of=True,
        called_party="Progressive", producer_name="Jane Producer",
        transfer_to_name="Jane Producer", transfer_number="+15559876543")
    assert "on behalf of Jane Producer" in eva_task
    assert "calling Progressive" in eva_task
    assert "for Mary Smith" in eva_task
    assert "Jake" not in eva_task


def test_name_mismatch_without_number_asks_for_the_number(clean_state):
    """No typed number: ask for the number. Do not dial the phone on file."""
    bland = FakeBland()
    ports = make_ports(bland=bland)
    task = make_task(**{
        "Task Description": "Call Mary Johnson about her renewal documents",
    })
    result = rch.handle_robie_call_task(task, make_config(), ports)
    assert result["ok"] is False
    assert bland.dials == 0
    assert "typed" in (result.get("error") or "")
    assert "Type the phone number to call" in clean_state.body


def test_transfer_lookup_resolves_assigner_did():
    ports = make_ports(
        transfer_lookup=FakeTransferLookup({"Jane Producer": "+15559876543"}))
    assert rch._resolve_transfer_number(
        ports, "Jane Producer") == "+15559876543"


def test_transfer_lookup_without_a_number_takes_a_message():
    # No port wired, an unknown name, and a failed lookup are all "no target".
    assert rch._resolve_transfer_number(make_ports(), "Jane Producer") is None
    ports = make_ports(transfer_lookup=FakeTransferLookup({}))
    assert rch._resolve_transfer_number(ports, "Nobody Here") is None
    ports = make_ports(transfer_lookup=FakeTransferLookup(raise_on_lookup=True))
    assert rch._resolve_transfer_number(ports, "Jane Producer") is None


def test_eva_prompt_has_frustration_transfer():
    prompt = rch._build_eva_task(
        "Call about the surcharge", "Mary Smith",
        transfer_to_name="Jane Producer", transfer_number="+15559876543")
    assert "frustrated" in prompt
    assert "Jane Producer" in prompt
    assert "+15559876543" in prompt


def test_transferred_call_is_not_ok_but_reassigned(clean_state):
    """Eva transferred a frustrated caller to the assigner: the task is not
    ok (Roby didn't complete it), it goes back to the assigner, and the
    note covers why, the outcome, and the call notes."""
    bland = FakeBland(statuses={
        "call-1": {"status": "completed", "transferred": True,
                   "answered_by": "human", "duration_s": 120,
                   "transcript_summary": "Caller got upset about the wait."},
    })
    ports = make_ports(bland=bland)
    result = rch.handle_robie_call_task(make_task(), make_config(), ports)
    assert bland.dials == 1
    # Not ok: the human took over.
    assert result["ok"] is False
    assert "transferred" in (result.get("error") or "").lower()
    # Reassigned back to the assigner (Jane Producer).
    assert result["reassigned"] is True
    assert ports.task_reassign.reassigns == [("T-100", "Jane Producer")]
    # The note covers why, the outcome, and the call notes.
    assert clean_state.calls == 1  # one structured transfer note filed
    # Content assertions via the formatter directly.
    note = rch._format_transfer_note(
        "John Smith", "Call about renewal",
        {"call_ids": ["call-1"],
         "attempts": [{"summary": "Caller got upset about the wait."}]},
        "Jane Producer")
    assert "transferred this call to Jane Producer" in note
    assert "frustrated" in note
    assert "call IDs call-1" in note
    assert "Caller got upset" in note
    assert "555" not in note  # no digits leak


def test_payload_uses_assigner_transfer_number():
    payload = rch.bland_payload_spec(
        "+18007764737", "task", "hi", "bye", 1,
        transfer_phone_number="+15559876543")
    assert payload["transfer_phone_number"] == "+15559876543"
    fallback = rch.bland_payload_spec("+18007764737", "task", "hi", "bye", 1)
    assert "transfer_phone_number" not in fallback
