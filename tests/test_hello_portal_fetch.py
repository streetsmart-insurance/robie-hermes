"""Tests for the hello intake portal login fetch (hello_portal_fetch.py)
and its wiring into hello_match.retrieve_link_docs.

NO real credentials and NO real network in these tests: the secret
accessor and the browser driver are always fakes. One hard rule is
asserted throughout: secret VALUES never appear in outputs, evidence,
reasons, or exceptions.
"""

import pytest

from robie_job_engine.hello_portal_fetch import (
    HANDLERS,
    PortalConfig,
    _PORTALS,
    fetch_portal_document,
    make_portal_fetch,
    portal_for_url,
    register_handler,
    register_portal,
)

DOMAIN = "agents.testcarrier.example"
USERNAME_REF = ("projects/streetsmart-hermes-poc/secrets/"
                "testcarrier-username/versions/latest")
PASSWORD_REF = ("projects/streetsmart-hermes-poc/secrets/"
                "testcarrier-password/versions/latest")
LOGIN_URL = "https://agents.testcarrier.example/login"


class FakeSecrets:
    def __init__(self, username="boxuser", password="s3cr3t-hunter2",
                 fail=False):
        self.username = username
        self.password = password
        self.fail = fail
        self.calls = []

    def access(self, resource_name):
        self.calls.append(resource_name)
        if self.fail:
            raise RuntimeError("permission denied")
        if "username" in resource_name:
            return self.username
        return self.password


class FakeBrowser:
    def __init__(self, data=b"doc-bytes", fail=False):
        self.data = data
        self.fail = fail
        self.calls = []

    def __call__(self, portal, username, password, target_url, timeout_secs):
        self.calls.append({"portal": portal, "username": username,
                           "password": password, "target_url": target_url,
                           "timeout_secs": timeout_secs})
        if self.fail:
            # A hostile error message carrying the password — the module
            # must NOT propagate it.
            raise RuntimeError(f"login failed for password {password}")
        return self.data


@pytest.fixture()
def portal():
    before = dict(_PORTALS)
    before_handlers = dict(HANDLERS)
    register_portal(PortalConfig(
        domain=DOMAIN, username_secret=USERNAME_REF,
        password_secret=PASSWORD_REF, login_url=LOGIN_URL))
    yield _PORTALS[DOMAIN]
    _PORTALS.clear()
    _PORTALS.update(before)
    HANDLERS.clear()
    HANDLERS.update(before_handlers)


# ---------------------------------------------------------------------------
# Registry: validation and domain resolution
# ---------------------------------------------------------------------------

def test_register_rejects_bad_secret_ref():
    with pytest.raises(ValueError):
        register_portal(PortalConfig(
            domain="x.example", username_secret="just-a-name",
            password_secret=PASSWORD_REF, login_url=LOGIN_URL))


def test_register_rejects_non_https_login():
    with pytest.raises(ValueError):
        register_portal(PortalConfig(
            domain="x.example", username_secret=USERNAME_REF,
            password_secret=PASSWORD_REF,
            login_url="http://x.example/login"))


def test_register_rejects_empty_domain():
    with pytest.raises(ValueError):
        register_portal(PortalConfig(
            domain=" ", username_secret=USERNAME_REF,
            password_secret=PASSWORD_REF, login_url=LOGIN_URL))


def test_portal_for_url_exact_and_subdomain(portal):
    assert portal_for_url("https://agents.testcarrier.example/doc/1") is portal
    assert portal_for_url(
        "https://docs.agents.testcarrier.example/doc/1") is portal


def test_portal_for_url_longest_match_wins(portal):
    register_portal(PortalConfig(
        domain="testcarrier.example", username_secret=USERNAME_REF,
        password_secret=PASSWORD_REF,
        login_url="https://testcarrier.example/login"))
    assert (portal_for_url("https://agents.testcarrier.example/d").domain
            == DOMAIN)


def test_portal_for_url_unknown_and_garbage():
    assert portal_for_url("https://unknown-carrier.example/doc") is None
    assert portal_for_url("not a url") is None
    assert portal_for_url("") is None


def test_config_repr_carries_no_values(portal):
    assert "s3cr3t" not in repr(portal)
    assert "hunter2" not in repr(portal)
    # Only names are recorded — the refs themselves are fine to show.
    assert "testcarrier-username" in repr(portal)


# ---------------------------------------------------------------------------
# fetch_portal_document: happy path and every fallback
# ---------------------------------------------------------------------------

def test_fetch_success_wires_secrets_to_browser(portal):
    secrets = FakeSecrets()
    browser = FakeBrowser(data=b"%PDF-1.4 fake")
    data, reason = fetch_portal_document(
        "https://agents.testcarrier.example/doc/9",
        secret_accessor=secrets, browser_fetch=browser)
    assert data == b"%PDF-1.4 fake"
    assert reason == ""
    assert secrets.calls == [USERNAME_REF, PASSWORD_REF]
    assert len(browser.calls) == 1
    call = browser.calls[0]
    assert call["username"] == "boxuser"
    assert call["password"] == "s3cr3t-hunter2"
    assert call["portal"].domain == DOMAIN
    assert call["target_url"] == "https://agents.testcarrier.example/doc/9"


def test_fetch_unknown_portal_defers_without_touching_secrets_or_browser():
    secrets = FakeSecrets()
    browser = FakeBrowser()
    data, reason = fetch_portal_document(
        "https://nope.example/doc", secret_accessor=secrets,
        browser_fetch=browser)
    assert data is None
    assert reason == "unknown_portal"
    assert secrets.calls == []
    assert browser.calls == []


def test_fetch_secret_failure_defers(portal):
    data, reason = fetch_portal_document(
        "https://agents.testcarrier.example/doc",
        secret_accessor=FakeSecrets(fail=True),
        browser_fetch=FakeBrowser())
    assert data is None
    assert reason == "secret_unavailable"


def test_fetch_empty_secret_value_defers(portal):
    data, reason = fetch_portal_document(
        "https://agents.testcarrier.example/doc",
        secret_accessor=FakeSecrets(username="", password=""),
        browser_fetch=FakeBrowser())
    assert data is None
    assert reason == "secret_unavailable"


def test_fetch_login_failure_defers_and_scrubs_secrets(portal):
    data, reason = fetch_portal_document(
        "https://agents.testcarrier.example/doc",
        secret_accessor=FakeSecrets(),
        browser_fetch=FakeBrowser(fail=True))
    assert data is None
    assert reason == "login_failed"
    assert "s3cr3t-hunter2" not in reason
    assert "boxuser" not in reason


def test_fetch_makes_exactly_one_attempt(portal):
    browser = FakeBrowser(fail=True)
    fetch_portal_document(
        "https://agents.testcarrier.example/doc",
        secret_accessor=FakeSecrets(), browser_fetch=browser)
    assert len(browser.calls) == 1  # no retry loop


def test_fetch_empty_document_defers(portal):
    data, reason = fetch_portal_document(
        "https://agents.testcarrier.example/doc",
        secret_accessor=FakeSecrets(), browser_fetch=FakeBrowser(data=b""))
    assert data is None
    assert reason == "empty_document"


def test_fetch_oversized_document_defers(portal):
    big = b"x" * (26 * 1024 * 1024)
    data, reason = fetch_portal_document(
        "https://agents.testcarrier.example/doc",
        secret_accessor=FakeSecrets(), browser_fetch=FakeBrowser(data=big))
    assert data is None
    assert reason == "document_too_large"


def test_fetch_unknown_handler_defers(portal):
    register_portal(PortalConfig(
        domain="weird.testcarrier.example", username_secret=USERNAME_REF,
        password_secret=PASSWORD_REF, login_url=LOGIN_URL,
        handler="no_such_handler"))
    data, reason = fetch_portal_document(
        "https://weird.testcarrier.example/doc",
        secret_accessor=FakeSecrets(), browser_fetch=FakeBrowser())
    assert data is None
    assert reason == "unknown_handler"


def test_make_portal_fetch_builds_callable(portal):
    fetch = make_portal_fetch(secret_accessor=FakeSecrets(),
                              browser_fetch=FakeBrowser(data=b"abc"))
    data, reason = fetch("https://agents.testcarrier.example/d")
    assert data == b"abc"
    assert reason == ""


def test_custom_handler_registry_dispatch(portal):
    seen = {}

    def custom(page, p, username, password, target_url, timeout_ms):
        seen["handler"] = p.handler
        return b"custom-bytes"

    register_handler("custom_one", custom)
    register_portal(PortalConfig(
        domain="custom.testcarrier.example", username_secret=USERNAME_REF,
        password_secret=PASSWORD_REF, login_url=LOGIN_URL,
        handler="custom_one"))
    # Dispatch through the registry the way _playwright_login_fetch does.
    handler = HANDLERS["custom_one"]
    assert handler(None, _PORTALS["custom.testcarrier.example"], "u", "p",
                   "https://custom.testcarrier.example/d", 10000) == b"custom-bytes"
    assert seen["handler"] == "custom_one"


# ---------------------------------------------------------------------------
# hello_match wiring: retrieve_link_docs attempts portal links
# ---------------------------------------------------------------------------

def _portal_test_setup():
    from robie_job_engine import hello_match
    before = dict(_PORTALS)
    register_portal(PortalConfig(
        domain=DOMAIN, username_secret=USERNAME_REF,
        password_secret=PASSWORD_REF, login_url=LOGIN_URL))
    return hello_match, before


def _portal_test_teardown(before):
    _PORTALS.clear()
    _PORTALS.update(before)


def test_retrieve_link_docs_fetches_portal_html_link():
    hello_match, before = _portal_test_setup()
    try:
        def fake_url_fetch(url):
            return b"<html>carrier login</html>", "text/html"

        def portal_fetch(url):
            return b"Named Insured: Portal Client Inc", ""

        docs, portal_links = hello_match.retrieve_link_docs(
            "see https://agents.testcarrier.example/docs/42 for the dec",
            url_fetch=fake_url_fetch, portal_fetch=portal_fetch)
        assert portal_links == []
        assert len(docs) == 1
        assert docs[0]["via_portal"] is True
        assert "Portal Client Inc" in (docs[0]["text"] or "")
    finally:
        _portal_test_teardown(before)


def test_retrieve_link_docs_portal_failure_falls_back_to_deferral():
    hello_match, before = _portal_test_setup()
    try:
        def fake_url_fetch(url):
            err = Exception("forbidden")
            err.code = 403
            raise err

        def portal_fetch(url):
            return None, "login_failed"

        docs, portal_links = hello_match.retrieve_link_docs(
            "doc at https://agents.testcarrier.example/docs/42",
            url_fetch=fake_url_fetch, portal_fetch=portal_fetch)
        assert portal_links == ["https://agents.testcarrier.example/docs/42"]
        assert any("deferred to human" in (d.get("note") or "")
                   for d in docs)
    finally:
        _portal_test_teardown(before)


def test_retrieve_link_docs_unknown_portal_still_defers():
    hello_match, before = _portal_test_setup()
    try:
        def fake_url_fetch(url):
            err = Exception("forbidden")
            err.code = 403
            raise err

        def portal_fetch(url):
            return None, "unknown_portal"

        docs, portal_links = hello_match.retrieve_link_docs(
            "doc at https://mystery-carrier.example/docs/7",
            url_fetch=fake_url_fetch, portal_fetch=portal_fetch)
        assert portal_links == ["https://mystery-carrier.example/docs/7"]
    finally:
        _portal_test_teardown(before)


def test_retrieve_link_docs_no_portal_fetch_keeps_old_deferral():
    hello_match, before = _portal_test_setup()
    try:
        def fake_url_fetch(url):
            return b"<html>login</html>", "text/html"

        docs, portal_links = hello_match.retrieve_link_docs(
            "see https://agents.testcarrier.example/docs/42",
            url_fetch=fake_url_fetch)
        assert portal_links == ["https://agents.testcarrier.example/docs/42"]
        assert not any(d.get("via_portal") for d in docs)
    finally:
        _portal_test_teardown(before)


def test_match_result_carries_portal_fetched_evidence():
    hello_match, before = _portal_test_setup()
    try:
        from robie_job_engine.hello_match import (
            handle_match_result, match_hello)
        result = match_hello(
            identity={"sender_email": "x@example.com",
                      "company_name": None, "policy_numbers": []},
            roster=[], portal_links_fetched=[
                "https://agents.testcarrier.example/docs/42"])
        out = handle_match_result(
            result, sender_email="x@example.com", subject="t",
            queue_path="/tmp/q.jsonl")
        assert out["action"] == "queued"
        assert "portal_link_fetched" in out["entry"]["strategies_tried"]
        assert any("portal_login_fetch" in e
                   for e in out["entry"]["evidence"])
    finally:
        _portal_test_teardown(before)


def test_no_secret_values_anywhere_in_wiring_outputs():
    hello_match, before = _portal_test_setup()
    try:
        from robie_job_engine.hello_match import retrieve_link_docs

        def fake_url_fetch(url):
            err = Exception("denied")
            err.code = 403
            raise err

        def portal_fetch(url):
            return None, "login_failed"

        docs, portal_links = retrieve_link_docs(
            "doc https://agents.testcarrier.example/docs/1",
            url_fetch=fake_url_fetch, portal_fetch=portal_fetch)
        blob = repr((docs, portal_links))
        assert "s3cr3t-hunter2" not in blob
        assert "boxuser" not in blob
    finally:
        _portal_test_teardown(before)
