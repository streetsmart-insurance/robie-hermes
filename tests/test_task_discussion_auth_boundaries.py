"""Authentication boundaries of the task flow's Discussion API client, and the separate lookup script.

Exact approved HTTPS auth/API pairs per route (no substring checks), no redirects, no browser cookies.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine import task_field_inspector as insp
from robie_job_engine import task_discussion_route as route_mod

PROD_REF = "projects/p/secrets/ezlynx-api-prod/versions/latest"
UAT_REF = "projects/p/secrets/ezlynx-api-uat/versions/latest"
LIVE_TOKEN = "https://app.ezlynx.com/auth/connect/token"
LIVE_DOC = "https://app.ezlynx.com/DocumentApi/"
UAT_TOKEN = "https://app.uatezlynx.com/auth/connect/token"
UAT_DOC = "https://app.uatezlynx.com/DocumentApi/"


def _payload(token, doc):
    return json.dumps({"client_id": "id", "client_secret": "S3CRET", "username": "vendor", "integration_group_id": "g",
                       "scope": "s", "token_endpoint": token, "document_base_url": doc})


class Accessor:
    def __init__(self, token, doc):
        self.refs, self.token, self.doc = [], token, doc

    def access(self, ref):
        self.refs.append(ref)
        return _payload(self.token, self.doc)


@pytest.fixture
def env(monkeypatch):
    for name in ("ROBIE_ENV", "ROBIE_TASK_DISCUSSION_ROUTE", "ROBIE_EZLYNX_DISCUSSION_API"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBIE_EZLYNX_API_PROD_SECRET", PROD_REF)
    monkeypatch.setenv("ROBIE_EZLYNX_API_UAT_SECRET", UAT_REF)
    return monkeypatch


def _live(env):
    env.setenv("ROBIE_ENV", "TEST"); env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "live"); env.setenv("ROBIE_EZLYNX_DISCUSSION_API", "live")


def _uat(env):
    env.setenv("ROBIE_ENV", "TEST"); env.setenv("ROBIE_TASK_DISCUSSION_ROUTE", "uat"); env.delenv("ROBIE_EZLYNX_DISCUSSION_API", raising=False)


def _build(accessor, **kw):
    return route_mod.build_task_discussion_client(accessor=accessor, **kw)


# ---- exact approved HTTPS pairs -------------------------------------------------------------

def test_the_exact_approved_pairs_are_accepted(env):
    _live(env)
    assert _build(Accessor(LIVE_TOKEN, LIVE_DOC)).route_record["host"] == "app.ezlynx.com"
    _uat(env)
    assert _build(Accessor(UAT_TOKEN, UAT_DOC)).route_record["host"] == "app.uatezlynx.com"


@pytest.mark.parametrize("token,doc", [
    ("http://app.ezlynx.com/auth/connect/token", LIVE_DOC),                        # not https
    (LIVE_TOKEN, "http://app.ezlynx.com/DocumentApi/"),
    ("https://app.ezlynx.com.evil.invalid/auth/connect/token", LIVE_DOC),          # lookalike suffix
    ("https://evilapp.ezlynx.com/auth/connect/token", LIVE_DOC),
    ("https://app.ezlynx.com@evil.invalid/auth/connect/token", LIVE_DOC),          # userinfo trick
    ("https://app.ezlynx.com:8443/auth/connect/token", LIVE_DOC),                  # port
    ("https://app.ezlynx.com/auth/connect/token?x=1", LIVE_DOC),                   # query
    ("https://app.ezlynx.com/auth/connect/token#f", LIVE_DOC),
    ("https://app.ezlynx.com/other/connect/token", LIVE_DOC),                      # path
    (LIVE_TOKEN, "https://app.ezlynx.com.evil.invalid/DocumentApi/"),
    (LIVE_TOKEN, "https://app.uatezlynx.com/DocumentApi/"),                        # mixed live auth + UAT api
    (UAT_TOKEN, LIVE_DOC),                                                         # mixed UAT auth + live api
    ("https://app.uatezlynx.com/auth/connect/token", "https://evil.invalid/DocumentApi/"),
    ("https://identity.example.com/connect/token", LIVE_DOC),                      # unrelated host
    ("", LIVE_DOC),
])
def test_anything_but_the_exact_pair_is_refused_before_any_request(env, token, doc):
    _live(env)
    requests = []
    with pytest.raises(route_mod.DiscussionRouteRefused):
        _build(Accessor(token, doc), urlopen=lambda *a, **k: requests.append(a))
    assert requests == []


def test_a_uat_route_refuses_the_live_pair_and_vice_versa(env):
    _uat(env)
    with pytest.raises(route_mod.DiscussionRouteRefused):
        _build(Accessor(LIVE_TOKEN, LIVE_DOC))
    _live(env)
    with pytest.raises(route_mod.DiscussionRouteRefused):
        _build(Accessor(UAT_TOKEN, UAT_DOC))


# ---- the client only ever talks to its approved HTTPS origin, and never follows a redirect -------

class _Resp:
    def __init__(self, body): self._b = body
    def read(self): return json.dumps(self._b).encode()


def _client(env, calls):
    _live(env)
    def opener(url, *, data, headers, timeout):
        calls.append((url, data, dict(headers)))
        return _Resp({"access_token": "T", "expires_in": 3600} if url.endswith("/connect/token") else ["1"])
    return _build(Accessor(LIVE_TOKEN, LIVE_DOC), urlopen=opener)


def test_requests_go_only_to_the_approved_origin(env):
    calls = []
    client = _client(env, calls)
    assert client.get_discussion_ids("220250093") == ["1"]
    assert [c[0].split("?")[0] for c in calls] == [LIVE_TOKEN, "https://app.ezlynx.com/DiscussionApi/v8/discussions/ids-by-applicant"]


@pytest.mark.parametrize("url", ["http://app.ezlynx.com/DiscussionApi/x", "https://evil.invalid/DiscussionApi/x",
                                 "https://app.uatezlynx.com/DiscussionApi/x", "https://app.ezlynx.com.evil.invalid/x",
                                 "https://app.ezlynx.com@evil.invalid/x"])
def test_the_guard_refuses_any_other_url_before_sending(env, url):
    calls = []
    client = _client(env, calls)
    with pytest.raises(Exception):
        client._request_json("GET", url, data=None, headers={}, authenticated=False)
    assert calls == []


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = []
    def do_GET(self):
        type(self).hits.append((self.path, self.headers.get("Authorization"), self.headers.get("Cookie")))
        if self.path == "/start":
            self.send_response(302); self.send_header("Location", "/landed"); self.end_headers()
        else:
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass


def test_the_default_opener_never_follows_a_redirect():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _Handler.hits.clear()
        from urllib import error
        with pytest.raises(error.HTTPError) as caught:
            route_mod.no_redirect_urlopen(f"http://127.0.0.1:{server.server_port}/start", data=None,
                                          headers={"Authorization": "Bearer X"}, timeout=5, _allow_insecure_test_origin=True)
        assert caught.value.code == 302
        assert [h[0] for h in _Handler.hits] == ["/start"], "the redirect target was requested"
    finally:
        server.shutdown()


# ---- browser cookies are never read or attached ------------------------------------------

@pytest.fixture
def cookie_traps(monkeypatch):
    hits = []
    def trap(name):
        def inner(*a, **k):
            hits.append(name); raise AssertionError(f"browser session access: {name}")
        return inner
    monkeypatch.setattr(disc, "_discussion_cookies", trap("_discussion_cookies"))
    monkeypatch.setattr(disc, "_cdp_port_open", trap("_cdp_port_open"))
    monkeypatch.setattr(disc, "discussion_request_headers", trap("discussion_request_headers"))
    import robie_job_engine.ezlynx_portal_session as portal
    monkeypatch.setattr(portal, "load_cdp_session_cookies", trap("load_cdp_session_cookies"), raising=False)
    monkeypatch.setattr(portal, "portal_session_headers", trap("portal_session_headers"), raising=False)
    real = socket.create_connection
    def guarded(address, *a, **k):
        if address[0] in ("127.0.0.1", "localhost", "::1") and address[1] == 9222:
            hits.append("cdp socket"); raise AssertionError("connected to the browser debug port")
        return real(address, *a, **k)
    monkeypatch.setattr(socket, "create_connection", guarded)
    return hits


def test_the_default_client_never_touches_browser_cookies(env, cookie_traps):
    calls = []
    client = _client(env, calls)
    client.get_discussion_ids("220250093")
    client.get_discussions("220250093") if False else None
    assert cookie_traps == []
    for _, _, headers in calls:
        assert not ({"Cookie", "Origin", "Referer"} & set(headers)), headers
    assert client.route_record["browser_cookies"] is False and client.route_record["password_grant"] is False


def test_the_token_request_carries_no_password_and_no_cookie(env, cookie_traps):
    calls = []
    client = _client(env, calls)
    client.get_discussion_ids("220250093")
    token_call = calls[0]
    body = token_call[1].decode()
    assert "grant_type=vendor_data_access" in body and "password=" not in body
    assert "Cookie" not in token_call[2]


def test_the_explicit_browser_session_option_keeps_the_old_helper_and_is_recorded(env):
    _live(env)
    client = _build(Accessor(LIVE_TOKEN, LIVE_DOC), browser_session=True, urlopen=lambda *a, **k: None)
    assert client._session_headers is disc.discussion_request_headers
    assert client.route_record["browser_cookies"] is True


def test_the_intake_builder_preserves_its_existing_session_behaviour(env):
    from robie_job_engine import ezlynx_task_intake as intake
    env.setenv("ROBIE_ENV", "PRODUCTION")
    env.setattr(route_mod, "load_ezlynx_api_config", lambda **k: __import__("robie_job_engine.ezlynx_api", fromlist=["x"]).EzlynxApiConfig(
        token_endpoint=LIVE_TOKEN, document_base_url=LIVE_DOC, client_id="i", client_secret="s", username="u",
        integration_group_id="g", scope="s"))
    client = intake._build_discussion_client()
    assert client._session_headers is disc.discussion_request_headers and client.route_record["browser_cookies"] is True


def test_the_inspector_refuses_a_client_that_can_use_browser_cookies(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "63429523")
    monkeypatch.delenv("EZLYNX_TASK_REASSIGN_ENABLED", raising=False)
    monkeypatch.delenv("ROBIE_PHONE_LIVE_CALLS", raising=False)
    monkeypatch.setattr(insp, "_hostname", lambda: "hermes-test-01")

    class Api:
        route_record = {"route": "live", "host": "h", "secret_ref": "r", "password_grant": False, "browser_cookies": True}
        calls = []
        def get_discussion_ids(self, a): self.calls.append("ids"); return ["1"]
        def get_discussion(self, d): self.calls.append("get"); return {}
    api = Api()
    with pytest.raises(insp.InspectionRefused, match="cookie"):
        insp.run_api_inspection(client=api, task_id="63429523", applicant_id="220250093", discussion_id="1",
                                output_path=tmp_path / "o.json", operator="x", expected_route="live")
    assert api.calls == []


# ---- the SEPARATE lookup script (its own review; the inspector's review does not cover it) -----

def _lookup():
    import importlib.util, pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "lookup_applicant_discussions.py"
    spec = importlib.util.spec_from_file_location("lookup_applicant_discussions", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class FakeApi:
    route_record = {"route": "live", "host": "app.ezlynx.com", "secret_ref": "r", "password_grant": False, "browser_cookies": False}
    def __init__(self):
        self.calls = []
    def get_discussion_ids(self, applicant): self.calls.append(("ids", applicant)); return ["849945654", "7"]
    def get_discussions(self, applicant):
        self.calls.append(("by-applicant", applicant))
        return [{"id": 849945654, "title": "SECRET TITLE Jane Doe", "notes": [
                    {"id": "n1", "body": "call 973-555-0100 about policy ABC123", "task": {"id": 63429523, "assignedUserId": 5},
                     "createdDate": "2026-10-06T09:00:00Z", "apiToken": "tok"}]},
                {"id": 7, "title": "Other", "notes": []}]


def test_the_lookup_makes_exactly_the_two_reads_and_nothing_else():
    api = FakeApi()
    module = _lookup()
    module.lookup(api, applicant_id="220250093")
    assert api.calls == [("ids", "220250093"), ("by-applicant", "220250093")]


def test_the_lookup_output_is_identifiers_and_field_metadata_only():
    out = json.dumps(_lookup().lookup(FakeApi(), applicant_id="220250093"), sort_keys=True)
    for leaked in ("SECRET TITLE", "Jane Doe", "973-555-0100", "ABC123", "tok", "2026-10-06", "Other"):
        assert leaked not in out, leaked
    data = json.loads(out)
    assert data["discussions"][0]["discussion_id"] == "849945654" and data["discussions"][0]["note_count"] == 1
    assert data["discussions"][0]["note_key_types"]["task.assignedUserId"] == "int"
    assert data["route"]["browser_cookies"] is False


def test_the_lookup_can_flag_a_candidate_task_without_printing_any_value():
    out = _lookup().lookup(FakeApi(), applicant_id="220250093", task_id="63429523")
    assert [d["contains_task_id"] for d in out["discussions"]] == [True, False]
    assert "63429523" not in json.dumps({k: v for k, v in out.items() if k != "task_id_checked"})


def test_the_lookup_refuses_other_applicants_other_routes_and_cookie_capable_clients():
    module = _lookup()
    with pytest.raises(SystemExit):
        module.lookup(FakeApi(), applicant_id="221398001")
    for bad in ({"route": "uat"}, {"password_grant": True}, {"browser_cookies": True}):
        api = FakeApi(); api.route_record = {**FakeApi.route_record, **bad}
        with pytest.raises(SystemExit):
            module.lookup(api, applicant_id="220250093")
        assert api.calls == []


def test_the_lookup_refuses_any_host_but_the_test_vm(monkeypatch):
    module = _lookup()
    monkeypatch.setattr(module.socket, "gethostname", lambda: "hermes-poc-01")
    assert module.main(["--applicant-id", "220250093"], client=FakeApi()) == 2


def test_the_lookup_source_has_no_write_call_no_cookie_no_credential_loader_and_no_file_io():
    import pathlib
    source = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "lookup_applicant_discussions.py").read_text().lower()
    source = source.replace("browser_cookies", "").replace("password_grant", "").replace("no password", "")  # flag names / prose
    for forbidden in ("append_note", "_post", "create_task", "reassign", "cookie", "load_discussion_api_config",
                      "password", "open(", "write(", "urlopen", "import requests", "http.client", "subprocess"):
        assert forbidden not in source, forbidden
