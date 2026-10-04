"""Prod Bland port places Jake's second attempt after voicemail or a screen."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from robie_job_engine.bland_call_port import DEFAULT_POLL_WAITS, BlandTransportCallPort
from robie_job_engine.bland_transport import KILL_SWITCH_SECRET

SECRET = "SYN-KEY-DO-NOT-LEAK"
PROD_HOST = "hermes-poc-01"
MONDAY = datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo("America/New_York"))
PROD_ENV = {
    "ROBIE_ENV": "PRODUCTION",
    "ROBIE_PHONE_LIVE_CALLS": "1",
    "ROBIE_PHONE_REAL_CLIENTS": "1",
    "ROBIE_BLAND_ALLOWED_HOSTS": "hermes-poc-01",
    "ROBIE_BLAND_ALLOWED_ENVS": "PRODUCTION",
    "ROBIE_BLAND_MAX_DURATION_MINUTES": "12",
}


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _port(detail: dict, *, reader, sleeper):
    posts: list[dict] = []

    def urlopen(request, timeout=0):
        del timeout
        if request.data is None:
            return _Response(json.dumps(detail).encode("utf-8"))
        posts.append(json.loads(request.data.decode("utf-8")))
        return _Response(json.dumps({"call_id": f"CALL-{len(posts)}"}).encode("utf-8"))

    port = BlandTransportCallPort(
        env=dict(PROD_ENV),
        hostname=PROD_HOST,
        api_key=SECRET,
        secret_reader=reader,
        urlopen=urlopen,
        execute=True,
        sleeper=sleeper,
        clock=lambda: MONDAY,
        poll_waits=(0,),
    )
    return port, posts


def test_voicemail_on_attempt_1_leaves_a_message_on_attempt_2():
    slept: list[float] = []

    def reader(name: str) -> str:
        assert name == KILL_SWITCH_SECRET
        return "0"

    port, posts = _port(
        {
            "completed": True,
            "status": "completed",
            "answered_by": "voicemail",
            "concatenated_transcript": "Please leave a message after the tone.",
        },
        reader=reader,
        sleeper=lambda seconds: slept.append(seconds),
    )
    result = port.place_call_with_double_dial("+17325550142", "task", "hi", "please call back")
    assert result["redialed"] is True
    assert result["call_ids"] == ["CALL-1", "CALL-2"]
    assert [body["voicemail_action"] for body in posts] == ["hangup", "leave_message"]
    assert posts[1]["voicemail_message"] == "please call back"
    assert slept == [10]


def test_kill_switch_between_attempts_stops_attempt_2():
    armed = {"value": False}

    def reader(name: str) -> str:
        assert name == KILL_SWITCH_SECRET
        return "1" if armed["value"] else "0"

    def sleeper(seconds: float) -> None:
        assert seconds == 10
        armed["value"] = True

    port, posts = _port(
        {"completed": True, "status": "completed", "answered_by": "voicemail"},
        reader=reader,
        sleeper=sleeper,
    )
    result = port.place_call_with_double_dial("+17325550142", "task", "hi", "vm")
    assert result["call_ids"] == ["CALL-1"]
    assert result["redialed"] is False
    assert result["error"] == "kill switch engaged"
    assert len(posts) == 1
    assert posts[0]["voicemail_action"] == "hangup"


def test_answered_attempt_1_does_not_place_attempt_2():
    def reader(name: str) -> str:
        assert name == KILL_SWITCH_SECRET
        return "0"

    def boom(_seconds: float) -> None:
        raise AssertionError("an answered call must not wait for a redial")

    port, posts = _port(
        {
            "completed": True,
            "status": "completed",
            "answered_by": "human",
            "call_length": 1.2,
            "concatenated_transcript": "Hello, this is Pat speaking. How can I help?",
        },
        reader=reader,
        sleeper=boom,
    )
    result = port.place_call_with_double_dial("+17325550142", "task", "hi", "vm")
    assert result["call_ids"] == ["CALL-1"]
    assert result["redialed"] is False
    assert len(posts) == 1


def test_default_polling_redials_when_voicemail_finishes_at_45s_and_90s():
    assert DEFAULT_POLL_WAITS == (0, 10, 10, 10, 15, 15, 20, 20, 20, 20)
    assert sum(DEFAULT_POLL_WAITS) == 140
    assert sum(DEFAULT_POLL_WAITS) < 180
    for finish_s in (45, 90):
        elapsed = {"seconds": 0.0}
        posts: list[dict] = []

        def sleeper(seconds: float, _elapsed=elapsed) -> None:
            _elapsed["seconds"] += seconds

        def urlopen(request, timeout=0, _elapsed=elapsed, _posts=posts, _finish=finish_s):
            del timeout
            if request.data is None:
                done = _elapsed["seconds"] >= _finish
                if done:
                    payload = {
                        "completed": True,
                        "status": "completed",
                        "answered_by": "voicemail",
                        "concatenated_transcript": "Please leave a message after the tone.",
                    }
                else:
                    payload = {"completed": False, "status": "ringing"}
                return _Response(json.dumps(payload).encode("utf-8"))
            _posts.append(json.loads(request.data.decode("utf-8")))
            return _Response(json.dumps({"call_id": f"CALL-{len(_posts)}"}).encode("utf-8"))

        def reader(name: str) -> str:
            assert name == KILL_SWITCH_SECRET
            return "0"

        port = BlandTransportCallPort(
            env=dict(PROD_ENV),
            hostname=PROD_HOST,
            api_key=SECRET,
            secret_reader=reader,
            urlopen=urlopen,
            execute=True,
            sleeper=sleeper,
            clock=lambda _elapsed=elapsed: MONDAY + timedelta(seconds=_elapsed["seconds"]),
            # The default schedule is the one production uses.
        )
        result = port.place_call_with_double_dial(
            "+17325550142", "task", "hi", "please call back",
        )
        assert port.poll_waits == DEFAULT_POLL_WAITS
        assert result["redialed"] is True, finish_s
        assert result["call_ids"] == ["CALL-1", "CALL-2"]
        assert [body["voicemail_action"] for body in posts] == ["hangup", "leave_message"]
        assert elapsed["seconds"] >= finish_s
