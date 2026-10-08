"""Tests for Jake's double-dial voicemail policy.

Policy: attempt 1 -> voicemail_action=hangup (silent hangup on voicemail).
If voicemail was hit: wait 10s, attempt 2 -> voicemail_action=leave_message
with the full SLOW message (AI disclosure + callback number digit by digit).

These tests never touch the network: dry_run builds the exact payloads,
and _attempt_payload is the single source of truth for both paths.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from bland_client import BlandClient, digit_by_digit
from prompts import voicemail_message


def test_digit_by_digit():
    assert digit_by_digit("732-462-8343") == "7 3 2 4 6 2 8 3 4 3"
    assert digit_by_digit("+17324628343") == "1 7 3 2 4 6 2 8 3 4 3"


def test_voicemail_message_has_disclosure_and_callback():
    msg = voicemail_message("I'm following up on your renewal.")
    lowered = msg.lower()
    assert "eva" in lowered
    assert "a.i." in lowered or "ai" in lowered
    assert "jake" in lowered
    assert "streetsmart insurance" in lowered
    assert "7 3 2 4 6 2 8 3 4 3" in msg  # digit-by-digit callback number
    assert "732-462-8343" not in msg  # never the dashed form in TTS


def test_attempt1_payload_is_silent_hangup():
    client = BlandClient(api_key="test")
    p = client._attempt_payload(
        "5551234567", "task", "Hi, this is Eva...", "voicemail msg", 1, None)
    assert p["voicemail_action"] == "hangup"
    assert "voicemail_message" not in p  # silent: no message on attempt 1
    assert p["voice"] == Config.VOICE_ID
    assert p["from"] == Config.FROM_NUMBER
    assert p["phone_number"] == "+15551234567"


def test_attempt2_payload_leaves_full_message():
    client = BlandClient(api_key="test")
    p = client._attempt_payload(
        "5551234567", "task", "Hi, this is Eva...", "SLOW MESSAGE", 2, None)
    assert p["voicemail_action"] == "leave_message"
    assert p["voicemail_message"] == "SLOW MESSAGE"


def test_dry_run_builds_both_attempts_without_dialing():
    client = BlandClient(api_key="test")
    out = client.call_with_double_dial(
        "5551234567", "task", "first", "vm", dry_run=True)
    assert out["mode"] == "DRY_RUN"
    assert len(out["attempts"]) == 2
    assert out["attempts"][0]["payload"]["voicemail_action"] == "hangup"
    assert out["attempts"][1]["payload"]["voicemail_action"] == "leave_message"
    assert out["attempts"][0]["mode"] == "DRY_RUN"


def test_redial_delay_matches_jakes_policy():
    assert Config.REDIAL_DELAY_SECONDS == 10


def test_phone_normalization():
    assert BlandClient._normalize_phone("(555) 123-4567") == "+15551234567"
    assert BlandClient._normalize_phone("15551234567") == "+15551234567"
    assert BlandClient._normalize_phone("+15551234567") == "+15551234567"
