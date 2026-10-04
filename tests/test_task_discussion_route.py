"""The task flow's Discussion API route is EXPLICIT and fails closed (never falls back).

Routes: "uat" (ROBIE_ENV=TEST, UAT secret) and "live" (PRODUCTION secret, vendor grant, NO password).
The shared SSRobie username/password route (the note tool's live path) is never used by the task flow.
"""
from __future__ import annotations

import json

import pytest

from robie_job_engine import ezlynx_task_intake as intake
from robie_job_engine import task_field_inspector as insp

UAT_REF = "projects/p/secrets/ezlynx-api-uat/versions/latest"
PROD_REF = "projects/p/secrets/ezlynx-api-prod/versions/latest"
USER_REF = "projects/p/secrets/ezlynx-username/versions/latest"
PASS_REF = "projects/p/secrets/ezlynx-password/versions/latest"


def _payload(host):
    return json.dumps({"client_id": "id", "client_secret": "S3CRET-VALUE", "username": "vendor",
                       "integration_group_id": "g", "scope": "s",
                       "token_endpoint": f"https://{host}/auth/connect/token",
                       "document_base_url": f"https://{host}/DocumentApi"})


class Accessor:
    def __init__(self, uat_host="app.uatezlynx.com", prod_host="app.ezlynx.com"):
        self.refs = []
        self.hosts = {UAT_REF: uat_host, PROD_REF: prod_host}

    def access(self, ref):
        self.refs.append(ref)
        if ref not in self.hosts:
            raise AssertionError(f"unexpected secret read: {ref}")
        return _payload(self.hosts[ref])


@pytest.fixture
def env(monkeypatch):
    for name in ("ROBIE_ENV", "ROBIE_TASK_DISCUSSION_ROUTE", "ROBIE_EZLYNX_DISCUSSION_API",
                 "ROBIE_EZLYNX_API_UAT_SECRET", "ROBIE_EZLYNX_API_PROD_SECRET",
                 "ROBIE_EZLYNX_USERNAME_SECRET", "ROBIE_EZLYNX_PASSWORD_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBIE_EZLYNX_API_UAT_SECRET", UAT_REF)
    monkeypatch.setenv("ROBIE_EZLYNX_API_PROD_SECRET", PROD_REF)
    monkeypatch.setenv("ROBIE_EZLYNX_USERNAME_SECRET", USER_REF)
    monkeypatch.setenv("ROBIE_EZLYNX_PASSWORD_SECRET", PASS_REF)
    return monkeypatch


def _build(accessor):
    from robie_job_engine.task_discussion_route import build_task_discussion_client
    return build_task_discussion_client(accessor=accessor)


def _refused():
    from robie_job_engine.task_discussion_route import DiscussionRouteRefused
    return pytest.raises(DiscussionRouteRefused)


def test_test_requires_an_explicit_route_and_reads_no_secret_without_one(env):
    env.setenv("ROBIE_ENV", "TEST")
    acc = Accessor()
    with _refused():
        _build(acc)
    assert acc.refs == []


@pytest.mark.parametrize("value", ["", "both", "prod", "UAT2", "auto", "fallback"])
def test_an_unknown_route_value_is_refused(env, value):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", value)
    acc = Accessor()
    with _refused():
        _build(acc)
    assert acc.refs == []


def test_test_uat_route_reads_only_the_uat_secret(env):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "uat")
    acc = Accessor()
    client = _build(acc)
    assert acc.refs == [UAT_REF]
    record = client.route_record
    assert record["route"] == "uat" and record["secret_ref"] == UAT_REF and record["host"] == "app.uatezlynx.com"
    assert record["password_grant"] is False and client._config.password == ""


def test_test_live_route_needs_the_live_discussion_switch_and_reads_only_the_prod_secret(env):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "live")
    acc = Accessor()
    with _refused():          # route says live but the environment's own switch does not
        _build(acc)
    assert acc.refs == []
    env.setenv("ROBIE_EZLYNX_DISCUSSION_API", "live")
    client = _build(acc)
    assert acc.refs == [PROD_REF]
    assert client.route_record["route"] == "live" and client.route_record["host"] == "app.ezlynx.com"


def test_a_declared_uat_route_conflicting_with_the_live_switch_is_refused(env):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "uat")
    env.setenv("ROBIE_EZLYNX_DISCUSSION_API", "live")
    acc = Accessor()
    with _refused():
        _build(acc)
    assert acc.refs == []


def test_the_shared_ssrobie_username_and_password_are_never_read_or_sent(env):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "live")
    env.setenv("ROBIE_EZLYNX_DISCUSSION_API", "live")
    acc = Accessor()
    client = _build(acc)
    assert USER_REF not in acc.refs and PASS_REF not in acc.refs
    assert client._config.password == "" and client.route_record["password_grant"] is False


@pytest.mark.parametrize("route,host", [("live", "app.uatezlynx.com"), ("uat", "app.ezlynx.com")])
def test_a_secret_pointing_at_the_other_environment_is_refused(env, route, host):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", route)
    env.setenv("ROBIE_EZLYNX_DISCUSSION_API", "live" if route == "live" else "uat")
    acc = Accessor(uat_host=host, prod_host=host)
    with _refused():
        _build(acc)
    assert len(acc.refs) == 1, "no second secret may be tried as a fallback"


def test_a_missing_uat_secret_never_falls_back_to_the_prod_secret(env):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "uat")
    env.delenv("ROBIE_EZLYNX_API_UAT_SECRET")
    acc = Accessor()
    with _refused():
        _build(acc)
    assert PROD_REF not in acc.refs


def test_a_failing_prod_secret_never_falls_back_to_uat(env):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "live")
    env.setenv("ROBIE_EZLYNX_DISCUSSION_API", "live")

    class Broken(Accessor):
        def access(self, ref):
            self.refs.append(ref)
            raise RuntimeError("permission denied")

    acc = Broken()
    with _refused():
        _build(acc)
    assert acc.refs == [PROD_REF]


def test_production_keeps_its_existing_live_route_without_any_new_setting(env):
    env.setenv("ROBIE_ENV", "PRODUCTION")
    acc = Accessor()
    client = _build(acc)
    assert acc.refs == [PROD_REF]
    assert client.route_record["route"] == "live" and client.route_record["password_grant"] is False


@pytest.mark.parametrize("name", ["PRODUCTION", "PROD", "LIVE"])
def test_production_cannot_be_pointed_at_uat(env, name):
    env.setenv("ROBIE_ENV", name)
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "uat")
    acc = Accessor()
    with _refused():
        _build(acc)
    assert acc.refs == []


def test_an_unset_robie_env_is_refused(env):
    acc = Accessor()
    with _refused():
        _build(acc)
    assert acc.refs == []


def test_the_intake_builder_uses_the_explicit_route(env):
    env.setenv("ROBIE_ENV", "TEST")
    with _refused():
        intake._build_discussion_client()


def test_the_route_record_never_contains_a_secret_value(env):
    env.setenv("ROBIE_ENV", "TEST")
    env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "uat")
    client = _build(Accessor())
    assert "S3CRET-VALUE" not in json.dumps(client.route_record)


# ---- the inspector must be told the route it expects, and refuse any other ----

class _Api:
    def __init__(self, route):
        self.route_record = {"route": route, "host": "h", "secret_ref": "r", "password_grant": False, "browser_cookies": False}
        self.calls = []

    def get_discussion_ids(self, applicant):
        self.calls.append("ids"); return ["849945654"]

    def get_discussion(self, discussion_id):
        self.calls.append("discussion"); return {"id": discussion_id, "notes": []}


def _api_args(tmp_path, client, **over):
    base = dict(client=client, task_id="63429523", applicant_id="220250093", discussion_id="849945654",
                output_path=tmp_path / "o.json", operator="Claude for Carlo Ferrara")
    base.update(over)
    return base


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "63429523")
    monkeypatch.delenv("EZLYNX_TASK_REASSIGN_ENABLED", raising=False)
    monkeypatch.delenv("ROBIE_PHONE_LIVE_CALLS", raising=False)
    monkeypatch.setattr(insp, "_hostname", lambda: "hermes-test-01")


def test_the_inspector_requires_an_expected_route(api_env, tmp_path):
    with pytest.raises(TypeError):
        insp.run_api_inspection(**_api_args(tmp_path, _Api("live")))


def test_the_inspector_refuses_a_client_on_a_different_route_before_any_read(api_env, tmp_path):
    client = _Api("uat")
    with pytest.raises(insp.InspectionRefused, match="route"):
        insp.run_api_inspection(**_api_args(tmp_path, client, expected_route="live"))
    assert client.calls == [] and not (tmp_path / "o.json").exists()


def test_the_inspector_refuses_a_client_that_cannot_state_its_route(api_env, tmp_path):
    client = _Api("live"); del client.route_record
    with pytest.raises(insp.InspectionRefused, match="route"):
        insp.run_api_inspection(**_api_args(tmp_path, client, expected_route="live"))
    assert client.calls == []


def test_the_inspector_records_the_route_it_used(api_env, tmp_path):
    record = insp.run_api_inspection(**_api_args(tmp_path, _Api("live"), expected_route="live"))
    assert record["discussion_route"] == {"route": "live", "host": "h", "secret_ref": "r", "password_grant": False, "browser_cookies": False}


def test_the_cli_requires_an_explicit_route_and_has_no_default():
    import subprocess, sys
    proc = subprocess.run([sys.executable, "scripts/inspect_task_fields.py", "api", "--task-id", "1", "--applicant-id", "220250093",
                           "--discussion-id", "2", "--operator", "x", "--output", "/nonexistent/o.json"],
                          capture_output=True, text=True, env={"PYTHONPATH": "."}, timeout=60)
    assert proc.returncode == 2 and "--api-route" in proc.stderr
