"""Jake's Oct 7 2026 go-live values, checked on the real Bland call port.

Caller ID stays 732-298-6745. Every callback number, voicemails included,
is the agency main line 732-462-8343 (Carlo, Oct 7 2026 10:34 PM). On Test
every dial goes to the Jake cell secret, never the
number passed in. No sockets.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from robie_job_engine.bland_call_port import BlandTransportCallPort
from robie_job_engine.bland_config import (
    CALLBACK_NUMBER,
    CALLBACK_NUMBER_SPOKEN,
    CALLER_ID,
)
from robie_job_engine.robie_call_handler import _build_voicemail_message

SECRET = "SYN-KEY-DO-NOT-LEAK"
TEST_ENV = {
    "ROBIE_ENV": "TEST",
    "ROBIE_PHONE_LIVE_CALLS": "1",
    "ROBIE_BLAND_ALLOWED_HOSTS": "hermes-poc-01,hermes-test-01",
    "ROBIE_BLAND_ALLOWED_ENVS": "PRODUCTION,TEST",
    "ROBIE_BLAND_MAX_DURATION_MINUTES": "12",
}
FAKE_JAKE = "+15555550188"


class _Response:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_values_are_pinned():
    assert CALLER_ID == "+17322986745"
    assert CALLBACK_NUMBER == "732-462-8343"
    assert CALLBACK_NUMBER_SPOKEN == "7 3 2, 4 6 2, 8 3 4 3"


def test_no_other_callback_number_anywhere_in_the_call_code():
    """Only the agency main line is ever given as a callback number."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    retired = ("732-481-2520", "7324812520", "4 8 1, 2 5 2 0")
    for path in (root / "robie_job_engine").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for value in retired:
            assert value not in text, f"{value} in {path.name}"


def test_splice_voicemail_uses_the_agency_main_line():
    from robie_job_engine.splice_scripts import WORKFLOWS, render_voicemail

    workflow = next(iter(WORKFLOWS.values()))
    text = render_voicemail(workflow, first_name="Avery", agent="Pat Example")
    assert "732-462-8343" in text


def test_test_box_posts_jake_cell_caller_id_and_main_line_callback():
    posts: list[dict] = []
    reads = {"n": 0}

    def urlopen(request, timeout=0):
        if request.get_method() == "POST":
            body = json.loads(request.data.decode("utf-8"))
            posts.append(body)
            return _Response({"call_id": f"SYN-CALL-{len(posts)}"})
        reads["n"] += 1
        return _Response({
            "completed": True, "status": "completed", "queue_status": "complete",
            "answered_by": "voicemail", "call_length": 0.3,
        })

    def secrets(name: str) -> str:
        if name == "robie-test-jake-cell":
            return FAKE_JAKE
        if name == "bland-dispatcher-kill-switch":
            return "0"
        raise KeyError(name)

    monday_10_et = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    port = BlandTransportCallPort(
        env=TEST_ENV, hostname="hermes-test-01", api_key=SECRET,
        secret_reader=secrets, urlopen=urlopen, execute=True,
        sleeper=lambda _s: None, clock=lambda: monday_10_et, poll_waits=(0,),
    )
    voicemail = _build_voicemail_message("Following up on your renewal", "Pat Example")
    result = port.place_call_with_double_dial(
        "+17325550142", "task text", "Hi,", voicemail,
    )
    assert result["success"] is True
    assert posts, result
    for body in posts:
        assert body["phone_number"] == FAKE_JAKE  # never the number passed in
        assert body["from"] == "+17322986745"
    assert result["call_ids"] == ["SYN-CALL-1", "SYN-CALL-2"]
    assert len(posts) == 2
    assert posts[0]["voicemail_action"] == "hangup"
    assert "voicemail_message" not in posts[0]
    assert posts[1]["voicemail_action"] == "leave_message"
    assert "7 3 2, 4 6 2, 8 3 4 3" in posts[1]["voicemail_message"]
    assert "7 3 2, 4 6 2, 8 3 4 3" in voicemail


def test_jake_cell_secret_stored_as_ten_digits_is_dialable():
    """Oct 7 2026: on hermes-test-01 the secret holds 10 digits, no +1.
    The old check refused it, so every Test dial failed closed."""
    from robie_job_engine.bland_call_port import select_dial_target

    for stored in ("5555550188", "+15555550188", "15555550188"):
        dial, error = select_dial_target(
            "+17325550142", env=TEST_ENV, hostname="hermes-test-01",
            secret_reader=lambda _name, v=stored: v,
        )
        assert (dial, error) == ("+15555550188", None), stored
    for bad in ("555-555-0188", "5555550188 x12", "55555501", "call me", ""):
        dial, error = select_dial_target(
            "+17325550142", env=TEST_ENV, hostname="hermes-test-01",
            secret_reader=lambda _name, v=bad: v,
        )
        assert dial is None and "not E.164" in error, bad
