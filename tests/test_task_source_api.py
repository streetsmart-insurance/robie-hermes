"""Task source: the EZLynx API with the emailed report as the fallback.

Offline only. The live listing endpoint is UNVERIFIED (see the module
docstring), so the client is a stub and the HTTP layer is a fake.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from urllib import error

import pytest

from robie_job_engine import ezlynx_task_api as api
from robie_job_engine import ezlynx_task_source as src
from robie_job_engine.ezlynx_seen_tasks import SeenTaskStore
from robie_job_engine.ezlynx_task_intake import run_intake
from robie_job_engine.store import JobStore
from tests.test_task_intake_review_blockers import (
    MONDAY,
    _Bland,
    _Notes,
    _install_fakes,
    _report,
    _task,
)

BUSTER = "26356199"  # Buster Brown, the Test applicant


def _rec(task_id="90026158", **over):
    base = {
        "task_id": task_id,
        "applicant_id": BUSTER,
        "discussion_id": "70026158",
        "assigned_user_id": 438318,
        "status": "Open",
        "text": "Please call about the renewal.",
        "labels": ["Robie Call"],
        "created_at": "2026-10-05T13:30:00Z",  # 9:30 ET
        "last_modified": "2026-10-05T13:30:00Z",
        "applicant_name": "Buster Brown",
        "created_by": "Daniela Aguilar",
    }
    base.update(over)
    return base


class Stub:
    def __init__(self, records=(), complete=True, exc=None):
        self.records, self.complete, self.exc = tuple(records), complete, exc
        self.asked: list[int] = []

    def list_assigned_tasks(self, assignee_id):
        self.asked.append(assignee_id)
        if self.exc:
            raise self.exc
        return src.TaskListing(self.records, self.complete)


# ---------------------------------------------------------------- the switch


def test_the_report_is_the_default_and_only_exact_api_turns_the_api_on():
    assert src.task_source_mode({}) == "report"
    assert src.task_source_mode({"ROBIE_TASK_SOURCE": ""}) == "report"
    assert src.task_source_mode({"ROBIE_TASK_SOURCE": "api2"}) == "report"
    assert src.task_source_mode({"ROBIE_TASK_SOURCE": "off"}) == "report"
    assert src.task_source_mode({"ROBIE_TASK_SOURCE": " API "}) == "api"


def test_assignee_defaults_to_ssrobie_and_must_be_a_number():
    assert src.assignee_id({}) == 438318
    assert src.assignee_id({src.ASSIGNEE_ID_ENV: "123"}) == 123
    with pytest.raises(src.TaskSourceUnavailable):
        src.assignee_id({src.ASSIGNEE_ID_ENV: "robie"})


# ----------------------------------------------------------------- normalizer


def test_a_record_becomes_the_task_the_report_would_have_made():
    task = src.record_to_task(_rec(), 438318)
    assert task.task_id == "90026158" and task.applicant_id == BUSTER
    assert task.discussion_id == "70026158" and task.assigned_to == "Robie AI"
    assert task.activity_labels == "Robie Call" and task.source == "task"
    assert task.created_at_et.startswith("2026-10-05T09:30:00")
    assert task.created_by == "Daniela Aguilar"


def test_a_task_creation_note_shape_is_read_too():
    note = {
        "noteId": 1, "type": "TaskCreationNote", "applicantId": BUSTER,
        "discussionId": 70026158, "body": "Call Buster.", "created": "2026-10-05T13:30:00-04:00",
        "noteLabels": [{"labelName": "Robie Call"}, {"labelName": "VIP"}],
        "task": {"taskId": 777, "assignedUserId": 438318, "dueDate": "2026-10-06T02:00:00Z"},
    }
    task = src.record_to_task(note, 438318)
    assert task.task_id == "777" and task.activity_labels == "Robie Call, VIP"
    assert task.description == "Call Buster." and task.due_date == "2026-10-06T02:00:00Z"


def test_other_assignees_and_closed_tasks_are_skipped_not_errors():
    assert src.record_to_task(_rec(assigned_user_id=99), 438318) is None
    for status in ("Complete", "completed", "Cancelled", "Closed"):
        assert src.record_to_task(_rec(status=status), 438318) is None
    assert src.record_to_task(_rec(status=""), 438318).status == "Open"


@pytest.mark.parametrize("bad", [
    {"task_id": ""}, {"applicant_id": "abc"}, {"discussion_id": ""},
    {"assigned_user_id": ""}, {"created_at": ""}, {"created_at": "not a date"},
    {"created_at": "2026-10-05T09:30:00"},  # no zone: a report would call this Central
])
def test_an_unreadable_row_makes_the_whole_listing_unusable(bad):
    with pytest.raises(src.TaskSourceUnavailable):
        src.record_to_task(_rec(**bad), 438318)


# --------------------------------------------------------------- the report


def test_every_task_is_kept_past_the_old_500_row_cap_and_the_listing_is_a_report():
    records = [_rec(task_id=str(90000000 + i), discussion_id=str(7000000 + i)) for i in range(650)]
    report = src.fetch_api_report(Stub(records), now=MONDAY)
    assert len(report.tasks) == 650 and report.row_count == 650
    assert report.message_id.startswith("api:") and report.filename == "ezlynx-task-api"
    assert report.received_at == str(int(MONDAY.timestamp() * 1000))
    assert report.newest_created_et.startswith("2026-10-05T10:00:00")  # the read time


def test_same_listing_same_id_and_a_change_is_a_new_delivery():
    a = src.fetch_api_report(Stub([_rec()]), now=MONDAY)
    b = src.fetch_api_report(Stub([_rec()]), now=MONDAY)
    c = src.fetch_api_report(Stub([_rec(last_modified="2026-10-05T14:00:00Z")]), now=MONDAY)
    d = src.fetch_api_report(Stub([_rec(labels=[])]), now=MONDAY)
    assert a.message_id == b.message_id
    assert len({a.message_id, c.message_id, d.message_id}) == 3


def test_the_client_is_asked_for_ssrobie_and_only_ssrobie():
    stub = Stub([_rec(), _rec(task_id="2", assigned_user_id=5)])
    report = src.fetch_api_report(stub, now=MONDAY)
    assert stub.asked == [438318]
    assert [t.task_id for t in report.tasks] == ["90026158"]


@pytest.mark.parametrize("stub", [
    Stub([_rec()], complete=False),
    Stub(exc=RuntimeError("boom")),
    Stub(exc=src.TaskSourceUnavailable("no")),
    Stub([_rec(), _rec()]),  # the same task twice
])
def test_a_partial_failed_or_duplicated_listing_is_never_a_report(stub):
    with pytest.raises(src.TaskSourceUnavailable):
        src.fetch_api_report(stub, now=MONDAY)


# ---------------------------------------------------------------- live client


APP = {
    "client_id": "cid-123", "client_secret": "csecret-456", "scope": "DiscussionApi openid",
    "token_endpoint": "https://app.ezlynx.com/auth/connect/token", "integration_group_id": "159",
    "discussion_base": "https://app.ezlynx.com/DiscussionApi",
}


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, json.dumps(body)

    def read(self):
        return self._b.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHttp:
    def __init__(self, *, token_status=200, list_status=200, list_body=None):
        self.calls, self.token_status, self.list_status = [], token_status, list_status
        self.list_body = [_rec()] if list_body is None else list_body

    def __call__(self, req, timeout):
        self.calls.append((req.get_method(), req.full_url, req.data))
        if req.full_url.endswith("/connect/token"):
            if self.token_status != 200:
                raise error.HTTPError(req.full_url, self.token_status, "bad", {}, io.BytesIO(b"{}"))
            return _Resp(200, {"access_token": "tok-abc", "expires_in": 3600})
        if self.list_status != 200:
            raise error.HTTPError(req.full_url, self.list_status, "x", {}, io.BytesIO(b"{}"))
        return _Resp(200, self.list_body)

    def tokens(self):
        return [c for c in self.calls if c[1].endswith("/connect/token")]

    def lists(self):
        return [c for c in self.calls if not c[1].endswith("/connect/token")]


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    api.clear_runtime_caches()
    monkeypatch.setenv(api.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(api.ACT_AS_ENV, "carlo1")
    monkeypatch.delenv(api.DIRECT_TASK_API_ENV, raising=False)
    monkeypatch.setenv(src.LIST_PATH_ENV, "v8/some/verified/path")
    monkeypatch.setattr(api, "load_app_config", lambda accessor=None: (dict(APP), None))
    yield
    api.clear_runtime_caches()


def test_without_a_verified_path_the_live_client_refuses_before_any_http(live_env, monkeypatch):
    monkeypatch.delenv(src.LIST_PATH_ENV)
    http = FakeHttp()
    with pytest.raises(src.TaskSourceUnavailable, match="no verified EZLynx endpoint"):
        src.LiveTaskListClient(urlopen=http).list_assigned_tasks(438318)
    assert http.calls == []


def test_live_client_logs_in_as_carlo1_once_and_reads_with_get_only(live_env):
    http = FakeHttp()
    client = src.LiveTaskListClient(urlopen=http)
    listing = client.list_assigned_tasks(438318)
    client.list_assigned_tasks(438318)
    assert listing.complete and len(listing.records) == 1
    assert len(http.tokens()) == 1  # cached after the first login
    form = http.tokens()[0][2].decode()
    assert "grant_type=vendor_data_access" in form and "username=carlo1" in form
    assert "password" not in form
    assert {c[0] for c in http.lists()} == {"GET"}  # never a write
    assert http.lists()[0][1].endswith("/DiscussionApi/v8/some/verified/path?assignedUserId=438318")


def test_one_failed_login_stops_the_api_source_for_the_hour(live_env):
    http = FakeHttp(token_status=401)
    client = src.LiveTaskListClient(urlopen=http)
    with pytest.raises(src.TaskSourceUnavailable):
        client.list_assigned_tasks(438318)
    with pytest.raises(src.TaskSourceUnavailable, match="circuit_open"):
        client.list_assigned_tasks(438318)
    assert len(http.tokens()) == 1 and http.lists() == []


def test_the_direct_task_api_switch_and_the_act_as_user_are_honored(live_env, monkeypatch):
    http = FakeHttp()
    monkeypatch.setenv(api.DIRECT_TASK_API_ENV, "0")
    with pytest.raises(src.TaskSourceUnavailable, match="switched off"):
        src.LiveTaskListClient(urlopen=http).list_assigned_tasks(438318)
    monkeypatch.delenv(api.DIRECT_TASK_API_ENV)
    monkeypatch.setenv(api.ACT_AS_ENV, "ssr_userPROD")
    with pytest.raises(src.TaskSourceUnavailable, match="act-as"):
        src.LiveTaskListClient(urlopen=http).list_assigned_tasks(438318)
    assert http.calls == []


@pytest.mark.parametrize("body,complete", [
    ([_rec()], True),
    ({"tasks": [_rec()]}, True),
    ({"items": [_rec()], "complete": False}, False),
    ({"notes": [_rec()], "hasMore": True}, False),
    ({"notes": [_rec()], "nextPage": "2"}, False),
])
def test_listing_shapes_and_the_complete_flag(live_env, body, complete):
    listing = src.LiveTaskListClient(urlopen=FakeHttp(list_body=body)).list_assigned_tasks(1)
    assert listing.complete is complete


@pytest.mark.parametrize("kw", [{"list_status": 500}, {"list_status": 404}, {"list_body": {"x": 1}},
                                {"list_body": [1, 2]}, {"list_body": "text"}])
def test_http_errors_and_odd_bodies_mean_use_the_report(live_env, kw):
    with pytest.raises(src.TaskSourceUnavailable):
        src.LiveTaskListClient(urlopen=FakeHttp(**kw)).list_assigned_tasks(1)


# ------------------------------------------------------------- the intake


def _api_intake(monkeypatch, notes, bland, listing_fn):
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setenv("ROBIE_TASK_SOURCE", "api")
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_source.fetch_from_api",
        lambda client=None: listing_fn(),
    )


def _api_report(*records):
    return src.fetch_api_report(Stub(records), now=MONDAY)


def test_buster_brown_task_flows_from_the_api_through_the_unchanged_intake(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")  # not a first run
    notes, bland = _Notes(), _Bland()
    state = {"report": _api_report(_rec())}
    _api_intake(monkeypatch, notes, bland, lambda: state["report"])
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        lambda _s: pytest.fail("the emailed report must not be read when the API answers"),
    )
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    assert run_intake(db_path=str(db)) == 0
    store = JobStore(str(db))
    with store.connect() as conn:
        ids = [str(r[0]) for r in conn.execute("SELECT id FROM jobs")]
        runs = conn.execute("SELECT message_id, status, task_count FROM ezlynx_task_intake_runs").fetchall()
        beats = conn.execute("SELECT status, message_id FROM ezlynx_task_intake_heartbeats").fetchall()
    assert len(ids) == 1
    payload = store.get_job(ids[0])["payload"]
    assert payload["task_id"] == "90026158" and payload["applicant_id"] == BUSTER
    assert runs[0][0].startswith("api:") and runs[0][2] == 1
    assert beats[-1][0] == "ok"
    assert bland.dials == 0  # call_dry_run: simulated, nothing dialed

    # The same listing again is the same delivery: quiet, no second job.
    assert run_intake(db_path=str(db)) == 0
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    # The task changed (same id): a new delivery, still one job for the one task.
    state["report"] = _api_report(_rec(last_modified="2026-10-05T14:00:00Z"))
    assert run_intake(db_path=str(db)) == 0
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert bland.dials == 0


def test_first_run_on_the_api_baselines_and_dials_nothing(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    notes, bland = _Notes(), _Bland()
    _api_intake(monkeypatch, notes, bland, lambda: _api_report(_rec(), _rec(task_id="2", discussion_id="9")))
    assert run_intake(db_path=str(db)) == 0
    assert set(SeenTaskStore(str(db)).statuses().values()) == {"baseline"}
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert bland.dials == 0


def test_an_unlabeled_api_task_is_left_untouched(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes, bland = _Notes(), _Bland()
    _api_intake(monkeypatch, notes, bland, lambda: _api_report(_rec(labels=[])))
    assert run_intake(db_path=str(db)) == 0
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert notes.notes == [] and bland.dials == 0


def test_api_failure_falls_back_to_the_emailed_report(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes, bland = _Notes(), _Bland()
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setenv("ROBIE_TASK_SOURCE", "api")
    # The real fetch_from_api with a refusing live client: no path configured.
    monkeypatch.delenv(src.LIST_PATH_ENV, raising=False)
    report = _report(_task(task_id="90022622", discussion_id="70020002"))
    calls = []
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        lambda _s: calls.append(1) or report,
    )
    assert run_intake(db_path=str(db)) == 0
    assert calls == [1]
    with JobStore(str(db)).connect() as conn:
        rows = conn.execute("SELECT message_id FROM ezlynx_task_intake_runs").fetchall()
    assert [r[0] for r in rows] == ["msg-1"]  # the report's id, not an api: id


def test_default_source_never_touches_the_api(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes, bland = _Notes(), _Bland()
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.delenv("ROBIE_TASK_SOURCE", raising=False)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_source.fetch_from_api",
        lambda client=None: pytest.fail("the API must not be asked unless ROBIE_TASK_SOURCE=api"),
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        lambda _s: _report(_task(task_id="90022622", discussion_id="70020002")),
    )
    assert run_intake(db_path=str(db)) == 0


def test_the_task_restriction_still_applies_to_api_tasks(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes, bland = _Notes(), _Bland()
    _api_intake(monkeypatch, notes, bland, lambda: _api_report(_rec()))
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "11111111")
    assert run_intake(db_path=str(db)) == 0
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_the_source_module_never_writes_to_ezlynx():
    text = open(src.__file__).read()
    for word in ("append_note", "create_task", "POST", "PUT", "DELETE", "upload"):
        assert word not in text.replace("GET", "").replace("never POST", ""), word


# -------------------------------------------------------------------- probe


def _probe_module():
    import importlib.util
    import pathlib

    path = pathlib.Path(src.__file__).resolve().parents[1] / "scripts" / "probe_task_list_endpoint.py"
    spec = importlib.util.spec_from_file_location("probe_task_list_endpoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_reports_key_names_only_and_issues_one_get(live_env):
    probe = _probe_module()
    http = FakeHttp(list_body={"tasks": [_rec(text="SECRET-VALUE")]})
    out = probe.probe("v8/x", 438318, urlopen=http)
    assert out["ok"] and out["list_key"] == "tasks" and out["row_count"] == 1
    assert "task_id" in out["first_row_keys"]
    assert "SECRET-VALUE" not in json.dumps(out)
    assert [c[0] for c in http.lists()] == ["GET"]


def test_probe_stops_on_404_without_guessing_another_path(live_env):
    probe = _probe_module()
    http = FakeHttp(list_status=404)
    out = probe.probe("v8/x", 438318, urlopen=http)
    assert out["ok"] is False and out["status"] == 404
    assert len(http.lists()) == 1
