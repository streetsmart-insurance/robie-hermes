"""Did a Robie Call reach a live person? (call_contact, pure)

Reference case: Bland call d25f46d6 (task 63558413, Oct 8 2026) was
"completed", answered_by unknown, 40 s, but only a hold message and a
recorded life-insurance pitch answered. It must not count as successful.
"""
from __future__ import annotations

import json

import pytest

from robie_job_engine import call_contact as cc
from robie_job_engine.bland_double_dial import classify_outcome, redial_eligible

D25F46D6_GET = {
    # Shape of GET /v1/calls/{id} for d25f46d6 (transcripts list of dicts).
    "call_id": "d25f46d6-1e3d-46a4-8a94-ba5bb4aa9008",
    "completed": True,
    "queue_status": "complete",
    "status": "completed",
    "answered_by": "unknown",
    "call_length": 0.666666666666667,
    "call_ended_by": "ASSISTANT",
    "error_message": None,
    "analysis": None,
    "summary": (
        "The call was a brief, automated introduction to a life insurance "
        "service, where the user was asked to hold and then greeted by a "
        "representative named Jimmy. ... the call ended abruptly without "
        "further discussion or interaction."
    ),
    "transcripts": [
        {"user": "assistant", "text": "Hi, this is an AI assistant calling on behalf -"},
        {"user": "user", "text": "Please hold while I try to connect you."},
        {"user": "assistant", "text": "Thank you."},
        {"user": "user", "text": (
            "Hi. Jimmy here. Life insurance is one of the most cost effective "
            "ways to protect the people who depend on you financially... "
            "Please continue to stay on the line. We will be with you shortly.")},
        {"user": "agent-action", "text": "Ended call"},
        {"user": "assistant", "text": "Goodbye."},
    ],
}


def _st(**kw):
    base = {"status": "completed", "answered_by": "unknown", "duration_s": 1.0}
    base.update(kw)
    return base


def test_d25f46d6_get_shape_is_recording():
    contact, reason = cc.classify_contact(
        {**D25F46D6_GET, "duration_s": D25F46D6_GET["call_length"]})
    assert contact == cc.RECORDING
    assert "hold message" in reason
    assert contact not in cc.SUCCESSFUL_CONTACTS


def test_d25f46d6_concatenated_shape_is_recording():
    text = "\n".join(f"{t['user']}: {t['text']}" for t in D25F46D6_GET["transcripts"])
    assert cc.classify_contact(_st(concatenated_transcript=text))[0] == cc.RECORDING


def test_d25f46d6_is_not_redialed_by_the_double_dial_policy():
    """No new auto-retry: the existing double-dial policy does not redial
    this call (inconclusive, not a voicemail)."""
    verdict = classify_outcome(D25F46D6_GET)
    assert redial_eligible(verdict) is False


@pytest.mark.parametrize("turns,expected", [
    # Real conversation, answered_by unknown (common in Bland data).
    (["assistant: Hi, this is an AI assistant.", "user: Hello?",
      "user: Yes, it's still 12 Main Street."], cc.PERSON),
    # Phone menu, then a person.
    (["user: Thank you for calling. Press 1 for billing.",
      "user: Hi, this is Sarah, how can I help you?"], cc.PERSON),
    # Phone menu only.
    (["user: Thank you for calling. Para español, oprima dos. Press 1 for billing."],
     cc.RECORDING),
    # Robocall only.
    (["user: This is an automated message about your car's extended warranty."],
     cc.RECORDING),
    # Number out of service.
    (["user: The number you have dialed is not in service."], cc.RECORDING),
    # Call screener with no pickup.
    (["user: Hi, the person you're calling is using a screening service. "
      "Please state your name and the reason for your call."], cc.RECORDING),
    # Voicemail greeting, no message left (first dial).
    (["user: You've reached Jake. Please leave a message after the tone."],
     cc.VOICEMAIL_NO_MESSAGE),
])
def test_transcript_decides(turns, expected):
    assert cc.classify_contact(_st(transcript=turns))[0] == expected


def test_voicemail_greeting_on_second_dial_is_message_left():
    turns = ["user: Please leave a message after the tone."]
    assert cc.classify_contact(_st(transcript=turns), message_left=True)[0] == \
        cc.VOICEMAIL_MESSAGE_LEFT


def test_answered_by_voicemail_without_message_is_not_successful():
    assert cc.classify_contact(_st(answered_by="voicemail"))[0] == cc.VOICEMAIL_NO_MESSAGE


def test_answered_by_human_without_transcript_trusts_bland():
    assert cc.classify_contact(_st(answered_by="human", duration_s=2))[0] == cc.PERSON


def test_answered_by_human_but_only_a_recording_spoke():
    turns = ["user: Please hold while I try to connect you.",
             "user: Your call is important to us. Please stay on the line."]
    assert cc.classify_contact(_st(answered_by="human", transcript=turns))[0] == cc.RECORDING


def test_no_transcript_no_human_is_unconfirmed_not_successful():
    contact, _ = cc.classify_contact(_st(duration_s=30))
    assert contact == cc.UNCONFIRMED
    assert contact not in cc.SUCCESSFUL_CONTACTS


def test_summary_only_automated_is_recording():
    st = _st(summary="An automated system answered and played a recorded message.")
    assert cc.classify_contact(st)[0] == cc.RECORDING


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "canceled"])
def test_misses(status):
    assert cc.classify_contact(_st(status=status, duration_s=0))[0] == cc.MISS


def test_zero_length_completed_is_a_miss():
    assert cc.classify_contact(_st(duration_s=0))[0] == cc.MISS


def test_best_contact_across_two_dials():
    assert cc.best_contact([cc.VOICEMAIL_NO_MESSAGE, cc.VOICEMAIL_MESSAGE_LEFT]) == \
        cc.VOICEMAIL_MESSAGE_LEFT
    assert cc.best_contact([cc.RECORDING, cc.PERSON]) == cc.PERSON


def test_prod_port_passes_transcript_and_summary_through():
    """The Prod Bland port returns the transcript, so the handler can tell a
    recording from a person (before this, only status/answered_by/length)."""
    from robie_job_engine.bland_transport import KILL_SWITCH_SECRET
    from test_bland_prod_double_dial import _port

    def reader(name):
        assert name == KILL_SWITCH_SECRET
        return "0"

    port, _ = _port(D25F46D6_GET, reader=reader, sleeper=lambda s: None)
    status = port.get_call_status("d25f46d6-1e3d-46a4-8a94-ba5bb4aa9008")
    assert status["ok"] is True
    assert status["status"] == "completed"
    assert status["transcripts"] == D25F46D6_GET["transcripts"]
    assert "automated" in status["summary"]
    assert cc.classify_contact(status)[0] == cc.RECORDING
    json.dumps(status)  # stays JSON-safe for the checkpoint/log
