"""Direct EZLynx Task API: login, stop, cache, ids, and honest results."""

from __future__ import annotations

import io
import json
import os
import stat
from typing import Any
from urllib import error

import pytest

from robie_job_engine import ezlynx_task_api as api
from robie_job_engine import ezlynx_write_scope, zapier_tasks
from robie_job_engine.ezlynx_user_ids import ezlynx_user_id_for

APP = {
    "client_id": "cid-123",
    "client_secret": "csecret-456",
    "scope": "DiscussionApi openid",
    "token_endpoint": "https://app.ezlynx.com/auth/connect/token",
    "integration_group_id": "159",
    "discussion_base": "https://app.ezlynx.com/DiscussionApi",
}
SSROBIE_ID = 438318


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeEZLynx:
    """Routes requests by URL. Records every call."""

    def __init__(self, *, token_status=200, post_status=201, post_body=None,
                 readback_assignee=SSROBIE_ID, discussions=None, post_exc=None,
                 create_status=200, create_lands=True, created_title="Tasks by Robie"):
        self.calls: list[tuple[str, str, Any]] = []
        self.token_status = token_status
        self.post_status = post_status
        self.post_body = {"noteId": 9001} if post_body is None else post_body
        self.readback_assignee = readback_assignee
        self.post_exc = post_exc
        self.create_status = create_status
        self.create_lands = create_lands
        self.created_title = created_title
        self.created_note = None
        self.discussions = discussions if discussions is not None else [
            {"discussionId": 11, "title": "Renewal service"},
            {"discussionId": 22, "title": "Tasks by Robie"},
        ]

    def __call__(self, req, timeout):
        url = req.full_url
        body = req.data.decode() if req.data else None
        self.calls.append((req.get_method(), url, body))
        if url.endswith("/connect/token"):
            if self.token_status != 200:
                raise error.HTTPError(url, self.token_status, "bad", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
            return _Resp(200, {"access_token": "tok-abc", "expires_in": 3600})
        if "/by-applicant" in url:
            return _Resp(200, self.discussions)
        if url.endswith("/v8/discussions/with-note"):
            sent = json.loads(body)
            if self.create_lands:
                self.created_note = dict(sent["note"], noteId=5005,
                                         task=dict(sent["note"]["task"], taskId=888))
                self.discussions = self.discussions + [
                    {"discussionId": 33, "title": self.created_title}]
            if self.create_status >= 400:
                raise error.HTTPError(url, self.create_status, "x", {}, io.BytesIO(b"{}"))
            return _Resp(self.create_status, 33)
        if url.endswith("/v8/discussions/33"):
            return _Resp(200, {"discussionId": 33, "title": self.created_title, "noteCount": 1})
        if url.endswith("/v8/discussions/33/with-notes"):
            return _Resp(200, {"notes": [self.created_note] if self.created_note else []})
        if url.endswith("/notes") and req.get_method() == "POST":
            if self.post_exc is not None:
                raise self.post_exc
            if self.post_status >= 400:
                raise error.HTTPError(url, self.post_status, "x", {}, io.BytesIO(b"{}"))
            return _Resp(self.post_status, self.post_body)
        if url.endswith("/v8/notes/query"):
            return _Resp(200, [{
                "noteId": 9001,
                "type": "TaskCreationNote",
                "task": {"assignedUserId": self.readback_assignee, "taskId": 777,
                         "dueDate": "2026-10-06T02:00:00Z"},
            }])
        raise AssertionError(f"unexpected call {url}")

    def posted_notes(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/notes")]

    def creates(self):
        return [c for c in self.calls if c[1].endswith("/with-note")]

    def token_calls(self):
        return [c for c in self.calls if c[1].endswith("/connect/token")]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    api.clear_runtime_caches()
    monkeypatch.setenv(api.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(api.ACT_AS_ENV, "carlo1")
    monkeypatch.delenv(api.DIRECT_TASK_API_ENV, raising=False)
    monkeypatch.setattr(api, "load_app_config", lambda accessor=None: (dict(APP), None))
    monkeypatch.setattr(
        ezlynx_write_scope, "require_allowed_ezlynx_write_applicant", lambda value: str(value)
    )
    yield
    api.clear_runtime_caches()


def _create(fake, **kw):
    args = dict(applicant_id="26356199", title="Call back", assignee="SSRobie",
                due_date="2026-10-05", urlopen=fake)
    args.update(kw)
    return api.create_task(**args)


def test_user_id_map_is_ported_from_watchdog():
    assert ezlynx_user_id_for("SSRobie") == SSROBIE_ID
    assert ezlynx_user_id_for("ssrobie") == SSROBIE_ID
    assert ezlynx_user_id_for("SCanales") is None  # unconfirmed: Zapier only
    assert ezlynx_user_id_for("") is None


def test_created_only_after_readback_with_requested_assignee():
    fake = FakeEZLynx()
    result = _create(fake)
    assert result["status"] == api.CREATED
    assert result["note_id"] == "9001"
    assert result["task_id"] == "777"
    assert result["discussion_id"] == "22"  # "Tasks by Robie", not the first one
    note = json.loads(fake.posted_notes()[0][2])
    assert note["type"] == "TaskCreationNote"
    assert note["task"]["assignedUserId"] == SSROBIE_ID
    assert note["task"]["due"] == "2026-10-06T02:00:00.000Z"  # 10 PM EDT


def test_token_form_is_vendor_grant_without_password():
    fake = FakeEZLynx()
    _create(fake)
    form = fake.token_calls()[0][2]
    assert "grant_type=vendor_data_access" in form
    assert "username=carlo1" in form
    assert "integration_group_id=159" in form
    assert "password" not in form
    assert "vendor_username" not in form


def test_token_is_cached_in_memory_and_0600_file():
    fake = FakeEZLynx()
    _create(fake)
    api._memory_tokens.clear()  # force the disk read
    _create(fake)
    assert len(fake.token_calls()) == 1
    path = api._state_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_one_failed_login_stops_further_logins():
    fake = FakeEZLynx(token_status=400)
    first = _create(fake)
    second = _create(fake)
    assert first["status"] == "circuit_open"
    assert second["status"] == "circuit_open"
    assert len(fake.token_calls()) == 1
    assert fake.posted_notes() == []
    assert api.zapier_fallback_allowed(first)
    # The stop survives a process restart (disk state).
    api.clear_runtime_caches()
    assert api.breaker_open("carlo1")


def test_login_failure_log_redacts_secrets(caplog):
    fake = FakeEZLynx(token_status=401)
    _create(fake)
    text = caplog.text
    assert "csecret-456" not in text and "cid-123" not in text


def test_vendor_username_refused_before_any_http(monkeypatch):
    monkeypatch.setenv(api.ACT_AS_ENV, "ssr_userPROD")
    fake = FakeEZLynx()
    assert _create(fake)["status"] == "refused_vendor_username"
    assert fake.calls == []


def test_off_switch_and_unset_act_as_make_no_http(monkeypatch):
    fake = FakeEZLynx()
    monkeypatch.setenv(api.DIRECT_TASK_API_ENV, "0")
    assert _create(fake)["status"] == "disabled"
    monkeypatch.delenv(api.DIRECT_TASK_API_ENV)
    monkeypatch.delenv(api.ACT_AS_ENV)
    assert _create(fake)["status"] == "unavailable"
    assert fake.calls == []


def test_unconfirmed_assignee_goes_to_zapier_without_http():
    fake = FakeEZLynx()
    result = _create(fake, assignee="SCanales")
    assert result["status"] == "refused_unresolved_assignee"
    assert api.zapier_fallback_allowed(result)
    assert fake.calls == []


def test_write_allowlist_refusal_makes_no_http(monkeypatch):
    def refuse(value):
        raise ezlynx_write_scope.EzlynxWriteScopeError("not on allowlist")

    monkeypatch.setattr(ezlynx_write_scope, "require_allowed_ezlynx_write_applicant", refuse)
    fake = FakeEZLynx()
    result = _create(fake)
    assert result["status"] == "write_refused"
    assert fake.calls == []


def test_phone_number_in_body_is_refused():
    fake = FakeEZLynx()
    result = _create(fake, description="Call them at 732-555-0100")
    assert result["status"] == "write_refused"
    assert fake.calls == []


ONLY_SERVICE = [{"discussionId": 11, "title": "Renewal service"}]


def test_missing_tasks_by_robie_is_created_with_the_task_and_read_back():
    fake = FakeEZLynx(discussions=list(ONLY_SERVICE))
    result = _create(fake)
    assert result["status"] == api.CREATED
    assert result["discussion_created"] is True
    assert result["discussion_id"] == "33"
    assert result["note_id"] == "5005" and result["task_id"] == "888"
    sent = json.loads(fake.creates()[0][2])
    assert sent["applicantId"] == 26356199
    assert sent["discussion"] == {"title": "Tasks by Robie"}
    assert sent["note"]["type"] == "TaskCreationNote"
    assert sent["note"]["task"]["assignedUserId"] == SSROBIE_ID
    assert fake.posted_notes() == []  # the task is the first note, not a second POST


def test_named_discussion_hint_without_match_never_creates():
    fake = FakeEZLynx(discussions=list(ONLY_SERVICE))
    result = _create(fake, discussion_title="Certificate request - Acme")
    assert result["status"] == "no_discussion"
    assert fake.creates() == [] and fake.posted_notes() == []
    assert api.zapier_fallback_allowed(result)


def test_create_refused_by_allowlist_makes_no_http(monkeypatch):
    def refuse(value):
        raise ezlynx_write_scope.EzlynxWriteScopeError("not on allowlist")

    monkeypatch.setattr(ezlynx_write_scope, "require_allowed_ezlynx_write_applicant", refuse)
    fake = FakeEZLynx(discussions=list(ONLY_SERVICE))
    assert _create(fake)["status"] == "write_refused"
    assert fake.calls == []


def test_create_500_with_nothing_landed_falls_back_to_zapier():
    fake = FakeEZLynx(discussions=list(ONLY_SERVICE), create_status=500, create_lands=False)
    result = _create(fake)
    assert result["status"] == "rejected"
    assert api.zapier_fallback_allowed(result)


def test_create_500_that_actually_landed_is_found_by_relist():
    fake = FakeEZLynx(discussions=list(ONLY_SERVICE), create_status=500, create_lands=True)
    result = _create(fake)
    assert result["status"] == api.CREATED
    assert result["discussion_id"] == "33"


def test_created_discussion_with_wrong_title_is_unverified():
    fake = FakeEZLynx(discussions=list(ONLY_SERVICE), created_title="Untitled")
    result = _create(fake)
    assert result["status"] == api.UNVERIFIED
    assert not api.zapier_fallback_allowed(result)


def test_created_discussion_without_our_task_note_is_unverified(monkeypatch):
    fake = FakeEZLynx(discussions=list(ONLY_SERVICE))
    real = fake.__call__

    def drop_note(req, timeout):
        resp = real(req, timeout)
        if req.full_url.endswith("/with-note"):
            fake.created_note = None
        return resp

    result = _create(drop_note)
    assert result["status"] == api.UNVERIFIED


def test_readback_with_wrong_assignee_is_unverified_not_created():
    fake = FakeEZLynx(readback_assignee=1)
    result = _create(fake)
    assert result["status"] == api.UNVERIFIED
    assert not api.zapier_fallback_allowed(result)


def test_post_timeout_is_unverified_and_blocks_zapier():
    fake = FakeEZLynx(post_exc=TimeoutError())
    result = _create(fake)
    assert result["status"] == api.UNVERIFIED
    assert not api.zapier_fallback_allowed(result)


def test_post_4xx_is_rejected_and_may_fall_back():
    fake = FakeEZLynx(post_status=400)
    result = _create(fake)
    assert result["status"] == "rejected"
    assert api.zapier_fallback_allowed(result)


# --- zapier_tasks.fire_task wiring -------------------------------------------


def _task_payload():
    return {
        "applicant_id": "26356199",
        "task_title": "Ascend cancellation notice - Test LLC",
        "assignee": "SSRobie",
        "source": "inbox-triage",
        "due_date": "2026-10-05",
    }


def test_fire_task_uses_direct_api_first(monkeypatch):
    monkeypatch.setattr(api, "create_task", lambda **kw: {"status": api.CREATED, "note_id": "1"})
    monkeypatch.setattr(zapier_tasks, "_fire_via_zapier", lambda *a, **k: pytest.fail("Zapier fired"))
    result = zapier_tasks.fire_task(_task_payload())
    assert result["ok"] is True and result["method"] == "direct_api"


def test_fire_task_falls_back_to_zapier_when_nothing_written(monkeypatch):
    monkeypatch.setattr(api, "create_task", lambda **kw: {"status": "no_discussion"})
    monkeypatch.setattr(zapier_tasks, "_fire_via_zapier", lambda p, dry_run=False: {"ok": True, "method": "zapier"})
    result = zapier_tasks.fire_task(_task_payload())
    assert result["method"] == "zapier"
    assert result["direct_api"]["status"] == "no_discussion"


def test_fire_task_unverified_is_not_ok_and_does_not_fire_zapier(monkeypatch):
    monkeypatch.setattr(api, "create_task", lambda **kw: {"status": api.UNVERIFIED, "reason": "timeout"})
    monkeypatch.setattr(zapier_tasks, "_fire_via_zapier", lambda *a, **k: pytest.fail("Zapier fired"))
    result = zapier_tasks.fire_task(_task_payload())
    assert result["ok"] is False
    assert "not confirmed" in result["error"]


def test_fire_task_direct_exception_falls_back(monkeypatch):
    def boom(**kw):
        raise RuntimeError("secret manager down")

    monkeypatch.setattr(api, "create_task", boom)
    monkeypatch.setattr(zapier_tasks, "_fire_via_zapier", lambda p, dry_run=False: {"ok": True, "method": "zapier"})
    assert zapier_tasks.fire_task(_task_payload())["method"] == "zapier"
