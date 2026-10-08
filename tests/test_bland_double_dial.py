"""Double-dial policy and worker tests.

Synthetic fixtures ONLY: invented numbers, transcripts, and call IDs. No
real phone numbers, recordings, transcripts, logs, credentials, or client
data may ever appear here (repo may be public; owner ruling pending).
"""
from __future__ import annotations

import contextvars
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from robie_job_engine.bland_double_dial import (
    CallOutcome,
    DoubleDialConfig,
    TargetRefused,
    classify_outcome,
    decide_redial,
    utc_from_epoch,
    validate_target,
)
from robie_job_engine.double_dial_worker import DoubleDialWorker

# Invented, reserved-for-fiction range numbers (555 prefix). Never real.
TARGET = "+15550100042"
CLIENT = "+15550100077"
CALLER = "+17322986745"  # pinned caller ID already in worker_adapters.py

BASE = 1_800_000_000.0


class FakeClock:
    def __init__(self, t=BASE):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakePort:
    """Scripted dispatcher + call-detail reads. No network."""

    def __init__(self):
        self.dispatched = []  # (target, caller_id, metadata) per dispatch
        self.details = {}     # call_id -> list of detail dicts to serve
        self._next = 0

    def queue_details(self, call_id, *details):
        self.details[call_id] = list(details)

    def dispatch_call(self, *, target, task, caller_id, metadata):
        self._next += 1
        call_id = f"SYN-CALL-{self._next:03d}"
        self.dispatched.append({"target": target, "caller_id": caller_id,
                                "metadata": dict(metadata), "call_id": call_id})
        return {"call_id": call_id, "status": "success"}

    def get_call_detail(self, call_id):
        seq = self.details.get(call_id) or [{}]
        detail = seq.pop(0) if len(seq) > 1 else seq[0]
        return detail


_STORE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "doubledial_store", default=None
)


@pytest.fixture(autouse=True)
def durable_dir():
    """Private 0700 directory outside /tmp. Idempotency rejects /tmp stores."""
    path = Path(tempfile.mkdtemp(prefix="doubledial-test-", dir=str(Path.home())))
    os.chmod(path, 0o700)
    token = _STORE.set(path)
    try:
        yield path
    finally:
        _STORE.reset(token)
        shutil.rmtree(path, ignore_errors=True)


def _private_db(name="dd.db"):
    root = _STORE.get()
    if root is None:
        raise RuntimeError("durable_dir fixture is required")
    slot = Path(tempfile.mkdtemp(prefix="worker-", dir=str(root)))
    os.chmod(slot, 0o700)
    return os.path.join(slot, name)


def make_worker(port, clock, **config_kwargs):
    config_kwargs.setdefault("poll_intervals_seconds", (0, 0, 0, 0))
    config = DoubleDialConfig(**config_kwargs)
    return DoubleDialWorker(db_path=_private_db(), port=port, config=config, clock=clock)


def voicemail_detail(call_id):
    return {"call_id": call_id, "completed": True, "queue_status": "complete",
            "status": "completed", "answered_by": "voicemail",
            "call_length": 0.4, "price": 0.02,
            "concatenated_transcript": "AI: ... VM: please leave a message after the tone"}


def human_detail(call_id):
    return {"call_id": call_id, "completed": True, "queue_status": "complete",
            "status": "completed", "answered_by": "human",
            "call_length": 1.2, "price": 0.05,
            "concatenated_transcript": "AI: Hi, Robie from the agency. Human: speaking. AI: ..."}


# --- fixture class 1: mailbox --------------------------------------------

def test_mailbox_triggers_exactly_one_redial_same_caller_within_window():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="synthetic task",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, voicemail_detail(rec.call_id))
    result = worker.poll_until_conclusive(rec.call_id)
    assert result.outcome is CallOutcome.VOICEMAIL_NO_MESSAGE
    clock.advance(30)
    redial = worker.maybe_redial(target=TARGET, task="synthetic task")
    assert redial.attempt_seq == 2 and redial.call_id
    assert [d["caller_id"] for d in port.dispatched] == [CALLER, CALLER]
    assert port.dispatched[1]["metadata"]["redial_of"] == rec.call_id
    # A second redial attempt is refused by the INSERT-once guard.
    again = worker.maybe_redial(target=TARGET, task="synthetic task")
    assert again.call_id and again.attempt_seq == 2
    assert len(port.dispatched) == 2  # no third call


def test_missed_window_records_missed_window_and_never_dials():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, voicemail_detail(rec.call_id))
    worker.poll_until_conclusive(rec.call_id)
    clock.advance(181)  # past the 180s window from FIRST dispatch
    redial = worker.maybe_redial(target=TARGET, task="t")
    assert "window" in redial.reason and len(port.dispatched) == 1
    row = worker.attempts_for(TARGET)[0]
    assert row["outcome"] == CallOutcome.MISSED_WINDOW.value


# --- fixture class 2: screener-then-human ---------------------------------

def test_screener_then_human_is_not_voicemail_and_no_redial():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    detail = {"call_id": rec.call_id, "completed": True,
              "queue_status": "complete", "status": "completed",
              "answered_by": "voicemail", "call_length": 1.1, "price": 0.04,
              "concatenated_transcript":
                  "Screener: the person you are calling is using a screening "
                  "service. May I ask who's calling? AI: Robie from the agency "
                  "about a policy audit. Screener: connecting you. Human: hello? "
                  "this is Sam speaking. AI: ..."}
    port.queue_details(rec.call_id, detail)
    result = worker.poll_until_conclusive(rec.call_id)
    assert result.outcome is CallOutcome.HUMAN_REACHED
    clock.advance(10)
    redial = worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1  # human reached - never redial


def test_screener_declined_is_not_misread_as_voicemail():
    detail = {"call_id": "SYN-X", "completed": True, "queue_status": "complete",
              "status": "completed", "answered_by": "voicemail",
              "call_length": 0.6, "price": 0.02,
              "concatenated_transcript":
                  "Screener: may I ask who's calling and the reason for your "
                  "call? AI: Robie from the agency. Screener: they are not "
                  "available. Goodbye."}
    result = classify_outcome(detail)
    assert result.outcome is CallOutcome.SCREENER_DECLINED
    assert result.conclusive
    # Jake's position: a screen with no pickup earns the automatic callback
    # within 10 seconds - redial-eligible by default, flag flips it.
    port2, clock2 = FakePort(), FakeClock()
    worker2 = make_worker(port2, clock2)
    rec2 = worker2.start_campaign(target=TARGET, task="t",
                                  number_source="carrier_directory")
    detail["call_id"] = rec2.call_id
    port2.queue_details(rec2.call_id, detail)
    worker2.poll_until_conclusive(rec2.call_id)
    clock2.advance(10)  # 10s redial delay honored
    redial2 = worker2.maybe_redial(target=TARGET, task="t")
    assert len(port2.dispatched) == 2 and redial2.attempt_seq == 2
    # ...and the owner's ruling can flip the default without rework
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock, redial_on_screener_declined=False)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    detail["call_id"] = rec.call_id
    port.queue_details(rec.call_id, detail)
    worker.poll_until_conclusive(rec.call_id)
    clock.advance(10)
    worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1


# --- fixture class 3: human-first -----------------------------------------

def test_human_first_means_no_redial():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, human_detail(rec.call_id))
    result = worker.poll_until_conclusive(rec.call_id)
    assert result.outcome is CallOutcome.HUMAN_REACHED
    worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1


def test_answered_by_human_without_corroboration_is_not_conclusive():
    result = classify_outcome({"call_id": "SYN-H", "completed": True,
                               "queue_status": "complete", "status": "completed",
                               "answered_by": "human"})
    assert result.outcome is CallOutcome.UNKNOWN and not result.conclusive


# --- fixture class 4: no-answer -------------------------------------------

def test_no_answer_is_terminal_by_default_no_redial():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, {"call_id": rec.call_id, "completed": True,
                                     "queue_status": "complete",
                                     "status": "no-answer", "answered_by": "no_answer"})
    result = worker.poll_until_conclusive(rec.call_id)
    assert result.outcome is CallOutcome.NO_ANSWER
    worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1


# --- fixture class 5: failed dispatch --------------------------------------

def test_failed_dispatch_is_retryable_and_does_not_consume_redial():
    result = classify_outcome({"call_id": "SYN-Q", "queue_status": "queue_error",
                               "status": "error", "completed": False,
                               "error_message": "carrier congestion"})
    assert result.outcome is CallOutcome.FAILED_DISPATCH
    assert result.conclusive and result.retryable_dispatch
    # A failed attempt-1 dispatch leaves no redial decision to make; a later
    # re-dispatch is a fresh attempt 1, still guarded by the idempotency key.
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, {"call_id": rec.call_id,
                                     "queue_status": "queue_error",
                                     "status": "error", "completed": False})
    worker.poll_until_conclusive(rec.call_id)
    worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1  # failed dispatch never consumed a redial


# --- fixture class 6: duplicate events -------------------------------------

def test_duplicate_call_detail_reads_write_one_event_and_one_redial():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    detail = voicemail_detail(rec.call_id)
    port.queue_details(rec.call_id, detail)  # served on the pre-dispatch re-read
    worker.record_outcome(rec.call_id, detail)
    worker.record_outcome(rec.call_id, dict(detail))   # duplicate delivery
    worker.record_outcome(rec.call_id, dict(detail))   # duplicate delivery
    assert worker.event_count(rec.call_id) == 1
    clock.advance(20)
    worker.maybe_redial(target=TARGET, task="t")
    worker.maybe_redial(target=TARGET, task="t")       # duplicate trigger
    assert len(port.dispatched) == 2
    assert len(worker.attempts_for(TARGET)) == 2


# --- fixture class 7: timeout ----------------------------------------------

def test_poll_timeout_is_unknown_and_never_dials():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock, max_poll_seconds=1)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id,
                       {"call_id": rec.call_id, "completed": False,
                        "queue_status": "started", "status": "in_progress"})
    result = worker.poll_until_conclusive(rec.call_id)
    assert result.outcome is CallOutcome.UNKNOWN and result.conclusive
    clock.advance(10)
    worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1


# --- fixture class 8: second-call failure ----------------------------------

def test_second_call_failure_is_terminal_no_third_call():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, voicemail_detail(rec.call_id))
    worker.poll_until_conclusive(rec.call_id)
    clock.advance(15)
    redial = worker.maybe_redial(target=TARGET, task="t")
    port.queue_details(redial.call_id, {"call_id": redial.call_id,
                                        "queue_status": "queue_error",
                                        "status": "error", "completed": False,
                                        "error_message": "synthetic failure"})
    result = worker.poll_until_conclusive(redial.call_id)
    assert result.outcome is CallOutcome.FAILED_DISPATCH
    worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 2  # never a third call


# --- persistence -------------------------------------------------------------

def test_attempt_records_persist_id_target_caller_policy_time_cost():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, voicemail_detail(rec.call_id))
    worker.poll_until_conclusive(rec.call_id)
    row = worker.attempts_for(TARGET)[0]
    assert row["call_id"] == rec.call_id
    assert row["target"] == TARGET and row["caller_id"] == CALLER
    assert row["policy_version"] == "double-dial-v1"
    assert row["dispatch_started_at"] and row["ended_at"]
    assert row["duration_minutes"] == 0.4 and row["price"] == 0.02
    assert row["outcome"] == CallOutcome.VOICEMAIL_NO_MESSAGE.value
    assert row["transcript"]


# --- carriers-only boundary ---------------------------------------------------

def test_client_and_row_numbers_are_refused():
    with pytest.raises(TargetRefused):
        validate_target(target_number=CLIENT, number_source="client")
    with pytest.raises(TargetRefused):
        validate_target(target_number=CLIENT, number_source="row")
    with pytest.raises(TargetRefused):
        validate_target(target_number=CLIENT, number_source="prospect")
    with pytest.raises(TargetRefused):
        validate_target(target_number=CLIENT, number_source="")
    with pytest.raises(TargetRefused):
        validate_target(target_number=TARGET, number_source="carrier_directory",
                        client_phones=[TARGET])  # directory hit that is a client
    with pytest.raises(TargetRefused):
        validate_target(target_number="5550100042", number_source="carrier_directory")
    assert validate_target(target_number=TARGET,
                           number_source="carrier_directory") == TARGET


def test_worker_refuses_client_campaign_without_dispatch():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    with pytest.raises(TargetRefused):
        worker.start_campaign(target=CLIENT, task="t", number_source="client")
    assert port.dispatched == []


def test_decide_redial_rejects_wrong_caller_id():
    decision = decide_redial(
        attempt_outcomes=[CallOutcome.VOICEMAIL_NO_MESSAGE],
        classification=classify_outcome(voicemail_detail("SYN-C")),
        first_dispatch_at=utc_from_epoch(BASE),
        now=utc_from_epoch(BASE + 60),
        caller_id="+15550199999",
    )
    assert not decision.redial and "pinned" in decision.reason


# --- Jake's 2026-09-28 production config ---------------------------------

def test_screener_engaged_mid_call_is_live_wait_not_terminal():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id,
                       {"call_id": rec.call_id, "completed": False,
                        "queue_status": "started", "status": "in_progress",
                        "concatenated_transcript":
                            "Screener: may I ask who's calling and the reason "
                            "for your call? AI: This is Eva, an AI assistant "
                            "calling on behalf of Jake from StreetSmart "
                            "Insurance."})
    result = worker.poll_until_conclusive(rec.call_id)
    # poll exhausts with the call still live-waiting -> UNKNOWN, never SCREENER_DECLINED
    assert result.outcome is CallOutcome.UNKNOWN
    mid = classify_outcome(port.details[rec.call_id][0])
    assert mid.outcome is CallOutcome.SCREENER_WAITING and not mid.conclusive
    worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1  # live-wait never triggers a redial


def test_attempt_payloads_voicemail_behavior_and_script_content():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="about your policy audit",
                                number_source="carrier_directory")
    assert port.dispatched[0]["metadata"]["voicemail_action"] == "no_message"
    assert "voicemail_script" not in port.dispatched[0]["metadata"]
    port.queue_details(rec.call_id, voicemail_detail(rec.call_id))
    worker.poll_until_conclusive(rec.call_id)
    clock.advance(10)
    worker.maybe_redial(target=TARGET, task="about your policy audit",
                        call_reason="about your policy audit")
    meta = port.dispatched[1]["metadata"]
    assert meta["voicemail_action"] == "leave_message"
    script = meta["voicemail_script"]
    assert "Eva" in script and "AI assistant" in script
    assert "Jake" in script and "StreetSmart Insurance" in script
    assert "732-462-8343" in script
    assert meta["agent_identity"].startswith("Eva, an AI assistant")


def test_ten_second_redial_delay_honored_within_180s_window():
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    rec = worker.start_campaign(target=TARGET, task="t",
                                number_source="carrier_directory")
    port.queue_details(rec.call_id, voicemail_detail(rec.call_id))
    worker.poll_until_conclusive(rec.call_id)
    clock.advance(5)  # inside the 10s delay
    early = worker.maybe_redial(target=TARGET, task="t")
    assert len(port.dispatched) == 1 and "wait until" in early.reason
    clock.advance(5)  # delay now honored, still inside 180s window
    redial = worker.maybe_redial(target=TARGET, task="t")
    assert redial.attempt_seq == 2 and len(port.dispatched) == 2


# --- attempt-1 concurrency (racing workers) --------------------------------

def test_concurrent_attempt_1_dispatch_places_exactly_one_call(durable_dir):
    """Two racing workers must not both place the first call."""
    import threading
    db = os.path.join(durable_dir, "dd.db")
    port_a, port_b = FakePort(), FakePort()
    clock = FakeClock()
    worker_a = DoubleDialWorker(db_path=db, port=port_a, clock=clock)
    worker_b = DoubleDialWorker(db_path=db, port=port_b, clock=clock)
    barrier = threading.Barrier(2)
    results = []

    def race(worker):
        barrier.wait()
        results.append(worker.start_campaign(
            target=TARGET, task="t", number_source="carrier_directory"))

    t1 = threading.Thread(target=race, args=(worker_a,))
    t2 = threading.Thread(target=race, args=(worker_b,))
    t1.start(); t2.start(); t1.join(); t2.join()
    total_dispatches = len(port_a.dispatched) + len(port_b.dispatched)
    assert total_dispatches == 1  # exactly one call placed, whoever won
    # Both workers converge on the single attempt record; the loser never
    # dispatched.
    call_ids = {r.call_id for r in results if r and r.call_id}
    assert len(call_ids) == 1
    winner = port_a if port_a.dispatched else port_b
    assert winner.dispatched[0]["call_id"] in call_ids
    assert len(worker_a.attempts_for(TARGET)) == 1


def test_sequential_duplicate_trigger_never_redispatches():
    """A repeated trigger after the first returns the existing attempt."""
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)
    first = worker.start_campaign(target=TARGET, task="t",
                                  number_source="carrier_directory")
    second = worker.start_campaign(target=TARGET, task="t",
                                   number_source="carrier_directory")
    assert second.call_id == first.call_id
    assert len(port.dispatched) == 1


def test_claim_without_call_id_is_never_blindly_redispatched():
    """Owner crashed between claim and dispatch: surface, never redispatch."""
    port, clock = FakePort(), FakeClock()
    worker = make_worker(port, clock)

    class CrashingPort(FakePort):
        def dispatch_call(self, **kwargs):
            raise RuntimeError("synthetic crash before receipt")

    crashed = DoubleDialWorker(db_path=worker.db_path, port=CrashingPort(),
                               clock=clock)
    with pytest.raises(RuntimeError):
        crashed.start_campaign(target=TARGET, task="t",
                               number_source="carrier_directory")
    # The claim row is committed with call_id NULL; a later trigger must not
    # place the call again.
    retry = worker.start_campaign(target=TARGET, task="t",
                                  number_source="carrier_directory")
    assert retry.call_id is None and "investigate" in retry.reason
    assert len(port.dispatched) == 0
