"""Discovery (the lookup script and the inspector) is genuinely read-only and prints only safe failures.

Uses the REAL client path: build_task_discussion_client -> DiscussionApiClient -> a fake transport.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
from urllib import error

import pytest

from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine import task_discussion_route as route_mod
from robie_job_engine import task_field_inspector as insp
from robie_job_engine.ezlynx_api import EzlynxApiConfig
from robie_job_engine.live_turn_guard import _TURN_JOB
from robie_job_engine.store import JobStore

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "https://app.ezlynx.com/auth/connect/token"
BASE = "https://app.ezlynx.com/DiscussionApi/v8/discussions/"
BODY_MARKER = "SECRET-RESPONSE-BODY-4471"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class Transport:
    """Fake urlopen for the real client. `mode` decides how by-applicant behaves."""
    def __init__(self, mode="ok"):
        self.mode, self.urls = mode, []

    def __call__(self, url, *, data, headers, timeout):
        self.urls.append(url)
        class R:
            def __init__(s, b): s.b = b
            def read(s): return json.dumps(s.b).encode()
        if url.endswith("/connect/token"):
            return R({"access_token": "T", "expires_in": 3600})
        if self.mode == "http403":
            raise error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(BODY_MARKER.encode()))
        if self.mode == "boom":
            raise RuntimeError(BODY_MARKER)
        if self.mode == "urlerror":
            raise error.URLError(BODY_MARKER)
        if "ids-by-applicant" in url:
            return R(["849945654", "7"])
        if "by-applicant" in url:
            return R([{"id": 849945654, "title": "Policy review", "notes": []},
                      {"id": 7, "title": "Billing question", "notes": []}])
        return R({"id": "849945654", "notes": []})


@pytest.fixture
def real(monkeypatch):
    for name in ("ROBIE_JOB_ID", "JOB_ID", "ROBIE_JOB_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "live")
    monkeypatch.setenv("ROBIE_EZLYNX_DISCUSSION_API", "live")
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "63429523")
    monkeypatch.delenv("EZLYNX_TASK_REASSIGN_ENABLED", raising=False)
    monkeypatch.delenv("ROBIE_PHONE_LIVE_CALLS", raising=False)
    monkeypatch.setattr(insp, "_hostname", lambda: "hermes-test-01")
    monkeypatch.setattr(route_mod, "load_ezlynx_api_config", lambda **k: EzlynxApiConfig(
        token_endpoint=TOKEN, document_base_url="https://app.ezlynx.com/DocumentApi/", client_id="i", client_secret="s",
        username="u", integration_group_id="g", scope="s"))
    transport = Transport()
    monkeypatch.setattr(route_mod, "no_redirect_urlopen", transport)
    return transport, monkeypatch


def _set_mode(real, mode):
    real[0].mode = mode


def _lookup_module(monkeypatch):
    module = _script("lookup_applicant_discussions")
    monkeypatch.setattr(module.socket, "gethostname", lambda: "hermes-test-01")
    return module


request_cleanup: list = []


@pytest.fixture(autouse=True)
def _reset_turn_job():
    yield
    while request_cleanup:
        request_cleanup.pop()()


def _job_context(tmp_path, monkeypatch, *, via="env"):
    db = tmp_path / "jobs.db"
    store = JobStore(str(db))
    job = store.create_job("hermes.google_chat_task", {"request_text": "hi"})
    monkeypatch.setenv("ROBIE_JOB_DB", str(db))
    if via == "env":
        monkeypatch.setenv("ROBIE_JOB_ID", job["id"])
    else:
        token = _TURN_JOB.set(job["id"])
        request_cleanup.append(lambda: _TURN_JOB.reset(token))
    return store, job["id"]


def _choices(store, job_id):
    return store.get_checkpoint(job_id, "discussion_choices")


# ---- the side effect is real in the default path, and absent in discovery ------------------------

def test_the_default_get_discussions_does_write_a_choice_checkpoint_in_job_context(real, tmp_path):
    transport, mp = real
    store, job_id = _job_context(tmp_path, mp)
    client = route_mod.build_task_discussion_client()
    client.get_discussions("220250093")
    assert _choices(store, job_id), "control: the default path is expected to remember choices"


def test_get_discussions_can_be_told_not_to_remember_choices(real, tmp_path):
    transport, mp = real
    store, job_id = _job_context(tmp_path, mp)
    client = route_mod.build_task_discussion_client()
    rows = client.get_discussions("220250093", remember_choices=False)
    assert len(rows) == 2 and not _choices(store, job_id)


@pytest.mark.parametrize("via", ["env", "contextvar"])
def test_the_lookup_refuses_inherited_job_context_before_any_request(real, tmp_path, capsys, via):
    transport, mp = real
    store, job_id = _job_context(tmp_path, mp, via=via)
    module = _lookup_module(mp)
    code = module.main(["--applicant-id", "220250093"])
    out = capsys.readouterr()
    assert code == 2 and "job context" in (out.out + out.err).lower()
    assert transport.urls == [] and not _choices(store, job_id)


def test_the_lookup_reads_and_leaves_no_checkpoint_when_there_is_no_job_context(real, tmp_path, capsys):
    transport, mp = real
    db = tmp_path / "jobs.db"; store = JobStore(str(db))
    mp.setenv("ROBIE_JOB_DB", str(db))                  # a db path alone is not a job context
    module = _lookup_module(mp)
    assert module.main(["--applicant-id", "220250093"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["discussion_ids"] == ["849945654", "7"]
    assert [u.split("?")[0].rsplit("/", 1)[-1] for u in transport.urls] == ["token", "ids-by-applicant", "by-applicant"]
    with store.transaction() as conn:
        assert conn.execute("select count(*) from checkpoints").fetchone()[0] == 0


def test_the_inspector_refuses_inherited_job_context_before_reading(real, tmp_path):
    transport, mp = real
    store, job_id = _job_context(tmp_path, mp)
    client = route_mod.build_task_discussion_client()
    with pytest.raises(insp.InspectionRefused, match="job context"):
        insp.run_api_inspection(client=client, task_id="63429523", applicant_id="220250093", discussion_id="849945654",
                                output_path=tmp_path / "o.json", operator="x", expected_route="live")
    assert [u for u in transport.urls if "discussions" in u] == [] and not _choices(store, job_id)


# ---- failures print only a safe category and status --------------------------------------------

@pytest.mark.parametrize("mode,expect", [("http403", "status=403"), ("boom", "unexpected_error"), ("urlerror", "transport")])
def test_the_lookup_prints_only_a_safe_category_on_failure(real, capsys, mode, expect):
    transport, mp = real
    _set_mode(real, mode)
    module = _lookup_module(mp)
    code = module.main(["--applicant-id", "220250093"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert code not in (0, None) and expect in text
    assert BODY_MARKER not in text and "Traceback" not in text and "File \"" not in text


@pytest.mark.parametrize("mode,expect", [("http403", "status=403"), ("boom", "unexpected_error")])
def test_the_inspector_cli_prints_only_a_safe_category_on_failure(real, tmp_path, capsys, mode, expect):
    transport, mp = real
    _set_mode(real, mode)
    cli = _script("inspect_task_fields")
    argv = ["api", "--task-id", "63429523", "--applicant-id", "220250093", "--discussion-id", "849945654",
            "--api-route", "live", "--operator", "Claude", "--output", str(tmp_path / "o.json")]
    code = cli.main(argv)
    out = capsys.readouterr()
    text = out.out + out.err
    assert code not in (0, None) and BODY_MARKER not in text and "Traceback" not in text


def test_an_error_after_ownership_is_verified_is_still_safe(real, tmp_path, capsys):
    transport, mp = real
    class Partial(Transport):
        def __call__(self, url, *, data, headers, timeout):
            if url.split("?")[0].endswith("/849945654"):
                raise error.HTTPError(url, 500, "Server Error", {}, io.BytesIO(BODY_MARKER.encode()))
            return super().__call__(url, data=data, headers=headers, timeout=timeout)
    mp.setattr(route_mod, "no_redirect_urlopen", Partial())
    cli = _script("inspect_task_fields")
    code = cli.main(["api", "--task-id", "63429523", "--applicant-id", "220250093", "--discussion-id", "849945654",
                     "--api-route", "live", "--operator", "Claude", "--output", str(tmp_path / "o.json")])
    out = capsys.readouterr()
    text = out.out + out.err
    assert code not in (0, None) and "status=500" in text
    assert BODY_MARKER not in text and "Traceback" not in text


def test_browser_failures_never_echo_the_exception_text(real, tmp_path, monkeypatch):
    transport, mp = real
    from contextlib import contextmanager
    from robie_job_engine import ezlynx_task_cdp as cdp
    @contextmanager
    def browser(): yield object()
    @contextmanager
    def session(**kw): yield
    mp.setattr(cdp, "_browser_page", browser)
    mp.setattr(cdp, "_goto_activity", lambda p, a: (_ for _ in ()).throw(RuntimeError("SECRET PAGE TEXT 9981")))
    mp.setattr(insp, "_exclusive_session", session)
    mp.setattr(insp, "_job_inventory", lambda db_path=None: [])
    mp.setattr(insp, "_list_browser_tabs", lambda: [])
    mp.setattr(insp, "_driver_gate_status", lambda: {"allowed": True, "holder": "TEST", "reason": "driver is IN"})
    out = tmp_path / "o.json"
    with pytest.raises(insp.InspectionAborted) as caught:
        insp.run_dom_inspection(task_id="63429523", applicant_id="220250093", output_path=out, operator="x",
                                confirm_browser_owner=True, confirm_exclusive=True)
    assert "SECRET PAGE TEXT" not in str(caught.value) and "SECRET PAGE TEXT" not in out.read_text()
