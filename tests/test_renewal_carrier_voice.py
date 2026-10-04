"""Carrier voice config. No live Bland call and no secret."""
from __future__ import annotations

import json

import pytest

from robie_job_engine.manual_renewal_worker import dispatch_carrier_voice_call


def _call(**env):
    return dispatch_carrier_voice_call(
        policy_number="SYN-POL",
        carrier_name="Synthetic Carrier",
        insured_name="Synthetic Insured",
        phone_e164="+15555550100",
        expiration=None,
    )


def test_missing_voice_id_refuses_without_a_default_or_transport(monkeypatch):
    monkeypatch.setenv("VOICE_AI_API_KEY", "synthetic-key")
    monkeypatch.delenv("ROBIE_BLAND_VOICE_ID", raising=False)
    monkeypatch.setenv("ROBIE_BLAND_MAX_DURATION_MINUTES", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("transport called")

    monkeypatch.setattr("robie_job_engine.bland_transport.post_call", boom)
    result = _call()
    assert result["call_placed"] is False
    assert result["status"] == "VOICE_CONFIG_REFUSED"
    assert "nat" not in json.dumps(result)


def test_missing_max_duration_refuses_before_transport(monkeypatch):
    monkeypatch.setenv("VOICE_AI_API_KEY", "synthetic-key")
    monkeypatch.setenv("ROBIE_BLAND_VOICE_ID", "SYN-VOICE-ID")
    monkeypatch.delenv("ROBIE_BLAND_MAX_DURATION_MINUTES", raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("transport called")

    monkeypatch.setattr("robie_job_engine.bland_transport.post_call", boom)
    result = _call()
    assert result["status"] == "VOICE_CONFIG_REFUSED"
    assert result["call_placed"] is False


def test_live_gates_off_refuse_before_urlopen(monkeypatch):
    monkeypatch.setenv("VOICE_AI_API_KEY", "synthetic-key")
    monkeypatch.setenv("ROBIE_BLAND_VOICE_ID", "SYN-VOICE-ID")
    monkeypatch.setenv("ROBIE_BLAND_MAX_DURATION_MINUTES", "1")
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "0")

    def boom(*_args, **_kwargs):
        raise AssertionError("urlopen called")

    monkeypatch.setattr("robie_job_engine.bland_transport.urllib.request.urlopen", boom)
    result = _call()
    assert result["call_placed"] is False
    assert result["status"] == "LIVE_GATES_REFUSED"


def test_configured_voice_and_duration_are_what_the_transport_sends(monkeypatch):
    monkeypatch.setenv("VOICE_AI_API_KEY", "synthetic-key")
    monkeypatch.setenv("ROBIE_BLAND_VOICE_ID", "SYN-VOICE-ID")
    monkeypatch.setenv("ROBIE_BLAND_MAX_DURATION_MINUTES", "1")
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    monkeypatch.setattr(
        "robie_job_engine.bland_transport.socket.gethostname",
        lambda: "hermes-test-01",
    )
    seen = []

    class _Response:
        def __init__(self, payload: bytes):
            self._payload = payload
        def read(self):
            return self._payload
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout=0):
        seen.append(request)
        if request.get_method() == "POST":
            body = json.loads(request.data.decode())
            assert body["voice"] == "SYN-VOICE-ID"
            assert body["max_duration"] == 1
            assert body["voice"] != "nat"
            return _Response(b'{"call_id":"SYN-CALL"}')
        return _Response(b'{"queue_status":"queued","status":"queued"}')

    monkeypatch.setattr(
        "robie_job_engine.bland_transport.urllib.request.urlopen",
        fake_urlopen,
    )
    result = _call()
    assert result["call_placed"] is True
    assert result["call_id"] == "SYN-CALL"
    assert seen[0].get_method() == "POST"
    assert "synthetic-key" == seen[0].get_header("Authorization")
