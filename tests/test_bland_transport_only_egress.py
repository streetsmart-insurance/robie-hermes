"""Bland egress lives in one module, and every gate fails before the network."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from robie_job_engine.bland_transport import (
    BlandTransportRefused,
    get_call,
    post_call,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "SYN-KEY-DO-NOT-LEAK"
ALLOWED = {
    "ROBIE_ENV": "TEST",
    "ROBIE_PHONE_LIVE_CALLS": "1",
}
BODY = {"phone_number": "+15555550123", "voice": "SYN-VOICE", "max_duration": 1}


def _boom(*_args, **_kwargs):
    raise AssertionError("urlopen must not be called")


def _post(**overrides):
    args = dict(
        api_key=SECRET,
        execute=True,
        env=dict(ALLOWED),
        hostname="hermes-test-01",
        urlopen=_boom,
    )
    args.update(overrides)
    return post_call(dict(BODY), **args)


def _get(**overrides):
    args = dict(
        api_key=SECRET,
        execute=True,
        env=dict(ALLOWED),
        hostname="hermes-test-01.example.internal",
        urlopen=_boom,
    )
    args.update(overrides)
    return get_call("SYN-CALL", **args)


def test_tracked_python_reaches_bland_only_through_transport():
    listed = subprocess.check_output(
        ["git", "ls-files", "robie_job_engine", "scripts"],
        cwd=ROOT,
        text=True,
    )
    hits = []
    for rel in listed.splitlines():
        if not rel.endswith(".py"):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8")
        if "api.bland.ai" in text:
            hits.append(rel)
    assert hits == ["robie_job_engine/bland_transport.py"]


@pytest.mark.parametrize(
    "override",
    [
        {"env": {}},
        {"env": {"ROBIE_ENV": "PRODUCTION", "ROBIE_PHONE_LIVE_CALLS": "1"}},
        {"env": {"ROBIE_ENV": "test", "ROBIE_PHONE_LIVE_CALLS": "1"}},
    ],
)
def test_post_refuses_wrong_env(override):
    with pytest.raises(BlandTransportRefused, match="ROBIE_ENV"):
        _post(**override)


@pytest.mark.parametrize(
    "override",
    [
        {"env": {"ROBIE_ENV": "TEST"}},
        {"env": {"ROBIE_ENV": "TEST", "ROBIE_PHONE_LIVE_CALLS": "0"}},
        {"env": {"ROBIE_ENV": "TEST", "ROBIE_PHONE_LIVE_CALLS": "true"}},
        {"env": {"ROBIE_ENV": "TEST", "ROBIE_PHONE_LIVE_CALLS": ""}},
    ],
)
def test_post_refuses_live_call_flag(override):
    with pytest.raises(BlandTransportRefused, match="ROBIE_PHONE_LIVE_CALLS"):
        _post(**override)


@pytest.mark.parametrize("hostname", ["hermes-poc-01", "localhost", "hermes-test-02", ""])
def test_post_refuses_wrong_host(hostname):
    with pytest.raises(BlandTransportRefused, match="hermes-test-01"):
        _post(hostname=hostname)


def test_post_refuses_when_execute_is_not_true():
    with pytest.raises(BlandTransportRefused, match="execute"):
        _post(execute=False)


@pytest.mark.parametrize("body", [
    {"phone_number": "+15555550123", "max_duration": 1},
    {"phone_number": "+15555550123", "voice": "", "max_duration": 1},
    {"phone_number": "+15555550123", "voice": "   ", "max_duration": 1},
])
def test_post_refuses_missing_voice_without_a_default(body):
    with pytest.raises(BlandTransportRefused, match="voice") as caught:
        post_call(
            body,
            api_key=SECRET,
            execute=True,
            env=dict(ALLOWED),
            hostname="hermes-test-01",
            urlopen=_boom,
        )
    assert SECRET not in str(caught.value)
    assert "voice" in body or body.get("voice", "") == "" or not str(body.get("voice", "")).strip()


@pytest.mark.parametrize("duration", [None, 2, 30, 0, -1, "1", True])
def test_post_refuses_max_duration_above_one_minute(duration):
    body = {"phone_number": "+15555550123", "voice": "SYN-VOICE"}
    if duration is not None:
        body["max_duration"] = duration
    with pytest.raises(BlandTransportRefused, match="max_duration"):
        post_call(
            body,
            api_key=SECRET,
            execute=True,
            env=dict(ALLOWED),
            hostname="hermes-test-01",
            urlopen=_boom,
        )


def test_get_refuses_env_host_flag_and_execute():
    with pytest.raises(BlandTransportRefused, match="ROBIE_ENV"):
        _get(env={})
    with pytest.raises(BlandTransportRefused, match="hermes-test-01"):
        _get(hostname="hermes-poc-01")
    with pytest.raises(BlandTransportRefused, match="ROBIE_PHONE_LIVE_CALLS"):
        _get(env={"ROBIE_ENV": "TEST", "ROBIE_PHONE_LIVE_CALLS": "0"})
    with pytest.raises(BlandTransportRefused, match="execute"):
        _get(execute=False)


def test_allowed_post_and_get_open_one_request_and_do_not_retry():
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

    def fake_urlopen(request, timeout=0):
        seen.append((request.get_method(), request.full_url, timeout, request.get_header("Authorization")))
        if len(seen) > 1:
            raise AssertionError("transport retried")
        method = request.get_method()
        payload = b'{"call_id":"SYN-CALL"}' if method == "POST" else b'{"status":"queued"}'
        return _Response(payload)

    posted = post_call(
        dict(BODY),
        api_key=SECRET,
        execute=True,
        env=dict(ALLOWED),
        hostname="hermes-test-01",
        urlopen=fake_urlopen,
    )
    assert posted == {"call_id": "SYN-CALL"}
    assert seen == [("POST", "https://api.bland.ai/v1/calls", 30, SECRET)]
    seen.clear()
    detail = get_call(
        "SYN-CALL",
        api_key=SECRET,
        execute=True,
        env=dict(ALLOWED),
        hostname="hermes-test-01",
        urlopen=fake_urlopen,
    )
    assert detail == {"status": "queued"}
    assert seen == [("GET", "https://api.bland.ai/v1/calls/SYN-CALL", 30, SECRET)]


def test_refusal_text_does_not_contain_the_api_key():
    with pytest.raises(BlandTransportRefused) as caught:
        _post(execute=False)
    assert SECRET not in str(caught.value)
