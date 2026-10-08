"""Prod Bland transport, applicant phone lookup, and transfer directory.

No sockets. Bland, RingCentral, and EZLynx are fakes or local files.
"""
from __future__ import annotations

import json
import logging

from robie_job_engine.bland_call_port import BlandTransportCallPort
from robie_job_engine.bland_prod_wiring import build_call_dependencies, live_calls_enabled
from robie_job_engine.bland_transport import (
    PROD_HOST,
    BlandTransportRefused,
    get_call,
    post_call,
)
from robie_job_engine.ezlynx_applicant_phone import (
    EzlynxApplicantPhoneLookup,
    extract_dialable_phone,
)
from robie_job_engine.ringcentral_transfer_lookup import (
    RingCentralTransferLookup,
    load_staff_directory,
    run_live_check,
)

SECRET = "SYN-KEY-DO-NOT-LEAK"
PROD_ENV = {
    "ROBIE_ENV": "PRODUCTION",
    "ROBIE_PHONE_LIVE_CALLS": "1",
    "ROBIE_BLAND_ALLOWED_HOSTS": "hermes-poc-01,hermes-test-01",
    "ROBIE_BLAND_ALLOWED_ENVS": "PRODUCTION,TEST",
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


def _urlopen_factory(seen):
    def urlopen(request, timeout=0):
        seen.append(json.loads(request.data.decode("utf-8")))
        return _Response(b'{"call_id":"SYN-CALL"}')
    return urlopen


def test_policy_number_field_is_refused_and_not_logged():
    record = {
        "PolicyNumber": "7685786571",
        "ClaimNumber": "4445556666",
        "QuoteNumber": "1112223333",
        "CellPhone": "",
    }
    logs = []

    class _Handler(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    handler = _Handler()
    logging.getLogger("robie_job_engine.ezlynx_applicant_phone").addHandler(handler)
    try:
        assert extract_dialable_phone(record) is None
    finally:
        logging.getLogger("robie_job_engine.ezlynx_applicant_phone").removeHandler(handler)
    text = "\n".join(logs)
    assert "PolicyNumber" in text
    assert "7685786571" not in text
    assert "4445556666" not in text


def test_phone_field_e164_is_used_and_policy_field_is_not():
    phone = extract_dialable_phone({
        "PolicyNumber": "7685786571",
        "CellPhone": "(732) 555-0142",
        "BusinessPhone": "not-a-phone",
    })
    assert phone == "+17325550142"
    lookup = EzlynxApplicantPhoneLookup(lambda applicant_id: {
        "PolicyNumber": "7685786571",
    } if applicant_id == "app-1" else {"HomePhone": "732-555-0199"})
    assert lookup.get_phone("app-1") is None
    assert lookup.get_phone("app-2") == "+17325550199"


def test_staff_directory_has_no_fallback_number():
    directory = load_staff_directory()
    assert len(directory) == 32
    lookup = RingCentralTransferLookup(directory)
    assert lookup.get_transfer_number("Nobody Here") is None
    assert lookup.get_transfer_number("") is None
    matched = lookup.get_transfer_number("ashley huntley")
    assert matched == directory["ashley huntley"]["did"]
    assert matched != directory["carlo ferrara"]["did"]
    assert "17324622360" not in open(
        "robie_job_engine/ringcentral_transfer_lookup.py", encoding="utf-8"
    ).read()


def test_live_ringcentral_check_is_off_by_default():
    seen = {"secrets": 0, "fetches": 0}

    def reader(_name):
        seen["secrets"] += 1
        return "unused"

    def fetch():
        seen["fetches"] += 1
        return []

    assert run_live_check(load_staff_directory(), secret_reader=reader, fetch_records=fetch) == []
    assert seen == {"secrets": 0, "fetches": 0}


def test_default_transport_still_refuses_prod_host_and_long_calls():
    env = {"ROBIE_ENV": "TEST", "ROBIE_PHONE_LIVE_CALLS": "1"}
    body = {"phone_number": "+15555550123", "voice": "SYN-VOICE", "max_duration": 12}
    try:
        post_call(body, api_key=SECRET, execute=True, env=env, hostname=PROD_HOST, urlopen=lambda *_a, **_k: None)
        raised = False
    except BlandTransportRefused as exc:
        raised = True
        assert "hermes-test-01" in str(exc)
    assert raised
    try:
        post_call(
            body, api_key=SECRET, execute=True, env=env,
            hostname="hermes-test-01", urlopen=lambda *_a, **_k: None,
        )
        raised = False
    except BlandTransportRefused as exc:
        raised = True
        assert "max_duration" in str(exc)
    assert raised


def test_configured_prod_transport_allows_poc_host_and_twelve_minutes():
    seen = []
    posted = post_call(
        {"phone_number": "+15555550123", "voice": "SYN-VOICE", "max_duration": 12},
        api_key=SECRET,
        execute=True,
        env=PROD_ENV,
        hostname=PROD_HOST,
        urlopen=_urlopen_factory(seen),
    )
    assert posted["call_id"] == "SYN-CALL"
    assert seen[0]["max_duration"] == 12


def test_kill_switch_refuses_before_the_socket():
    env = dict(PROD_ENV)
    env["ROBIE_BLAND_KILL_SWITCH"] = "1"

    def boom(*_args, **_kwargs):
        raise AssertionError("urlopen must not be called")

    try:
        post_call(
            {"phone_number": "+15555550123", "voice": "SYN-VOICE", "max_duration": 1},
            api_key=SECRET, execute=True, env=env, hostname=PROD_HOST, urlopen=boom,
        )
        raised = False
    except BlandTransportRefused as exc:
        raised = True
        assert "kill-switch" in str(exc)
        assert SECRET not in str(exc)
    assert raised


def test_call_port_refuses_a_policy_number_and_posts_an_e164_number():
    seen = []
    env = dict(PROD_ENV)
    env["ROBIE_PHONE_REAL_CLIENTS"] = "1"

    def kill_switch_off(name: str) -> str:
        assert name == "bland-dispatcher-kill-switch"
        return "0"

    port = BlandTransportCallPort(
        env=env, hostname=PROD_HOST, api_key=SECRET,
        secret_reader=kill_switch_off,
        urlopen=_urlopen_factory(seen), execute=True,
    )
    refused = port.place_call_with_double_dial("7685786571", "task", "hi", "vm")
    assert refused["success"] is False
    assert seen == []
    placed = port.place_call_with_double_dial("+17325550142", "task", "hi", "vm")
    assert placed["call_ids"] == ["SYN-CALL"]
    assert seen[0]["phone_number"] == "+17325550142"
    assert seen[0]["from"] == "+17322986745"


def test_intake_defaults_to_dry_run_and_wires_the_bland_client():
    assert live_calls_enabled({}) is False
    phone, bland, transfer, dry_run = build_call_dependencies(env={})
    assert dry_run is True
    assert bland.execute is False
    assert phone.get_phone("app-1") is None
    assert transfer.get_transfer_number("Nobody") is None
    _phone, live_bland, _transfer, live_dry = build_call_dependencies(
        env={"ROBIE_PHONE_LIVE_CALLS": "1"}
    )
    assert live_dry is False
    assert live_bland.execute is True


def test_requests_carry_a_browser_user_agent_for_cloudflare():
    """Cloudflare in front of api.bland.ai answers the stock Python-urllib
    User-Agent with HTTP 403 (error code 1010). Every request must carry a
    browser User-Agent. Proven 2026-10-07 from hermes-test-01: stock UA 403,
    browser UA 200 on a read-only GET."""
    headers = []

    def urlopen(request, timeout=0):
        headers.append({k.lower(): v for k, v in request.header_items()})
        return _Response(b'{"call_id":"SYN-CALL"}')

    post_call(
        {"phone_number": "+15555550123", "voice": "SYN-VOICE", "max_duration": 1},
        api_key=SECRET, execute=True, env=PROD_ENV, hostname=PROD_HOST, urlopen=urlopen,
    )
    get_call(
        "SYN-CALL", api_key=SECRET, execute=True, env=PROD_ENV, hostname=PROD_HOST,
        urlopen=urlopen,
    )
    assert len(headers) == 2
    for sent in headers:
        agent = sent.get("user-agent", "")
        assert agent.startswith("Mozilla/5.0"), agent
        assert "python" not in agent.casefold()
        assert sent.get("authorization") == SECRET
