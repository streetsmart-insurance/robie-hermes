import sys
import types
import unittest
from datetime import datetime, timezone

import pytest

from robie_job_engine.gmail_accountability import (
    GMAIL_METADATA_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GmailAccountabilityError,
    approved_mailboxes_from_role_registry,
    build_keyless_delegated_service,
    build_notice_gmail_service,
    collect_agency_summary,
    mailbox_allowlist,
    notice_gmail_scopes,
    summarize_mailbox_threads,
)


AS_OF = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)


def _thread(thread_id: str, sender: str, when_ms: int):
    return {
        "id": thread_id,
        "messages": [{
            "internalDate": str(when_ms),
            "payload": {"headers": [{"name": "From", "value": sender}]},
        }],
    }


def _millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def test_metadata_summary_classifies_reply_owner_without_bodies_or_subjects():
    summary = summarize_mailbox_threads(
        "jackie@streetsmart.insurance",
        [
            _thread("customer-last", "Client <client@example.com>", _millis(datetime(2026, 8, 28, 17, tzinfo=timezone.utc))),
            _thread("employee-last", "Jackie <jackie@streetsmart.insurance>", _millis(datetime(2026, 8, 30, 12, tzinfo=timezone.utc))),
        ],
        as_of=AS_OF,
    )
    assert summary["awaiting_employee"] == 1
    assert summary["awaiting_customer"] == 1
    assert summary["stalled_threads"] == 1
    assert "subject" not in str(summary).lower()
    assert "body" not in str(summary).lower()


def test_mailbox_allowlist_is_explicit_normalized_and_deduplicated():
    assert mailbox_allowlist(" Jackie@StreetSmart.Insurance, jazmin@streetsmart.insurance, jackie@streetsmart.insurance ") == (
        "jackie@streetsmart.insurance",
        "jazmin@streetsmart.insurance",
    )


def test_approved_mailboxes_are_derived_from_complete_active_role_registry():
    assert approved_mailboxes_from_role_registry({
        "source_status": "available",
        "employees": {
            "Jazmin Molina": {"email": "Jazmin@StreetSmart.Insurance"},
            "Karla Brown": {"email": "karla@streetsmart.insurance"},
        },
    }) == (
        "jazmin@streetsmart.insurance",
        "karla@streetsmart.insurance",
    )


def test_approved_mailboxes_fail_closed_when_roster_email_is_missing():
    with pytest.raises(GmailAccountabilityError, match="missing work email"):
        approved_mailboxes_from_role_registry({
            "source_status": "available",
            "employees": {"Jazmin Molina": {"email": ""}},
        })


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Threads:
    def list(self, **_kwargs):
        return _Request({"threads": []})


class _Users:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def getProfile(self, **_kwargs):
        return _Request({"emailAddress": self.mailbox})

    def threads(self):
        return _Threads()


class _Service:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def users(self):
        return _Users(self.mailbox)


def test_agency_summary_verifies_every_roster_mailbox_with_metadata_only():
    summary = collect_agency_summary(
        environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@test.invalid"},
        approved_users=["karla@streetsmart.insurance", "jazmin@streetsmart.insurance"],
        service_factory=lambda _service_account, mailbox: _Service(mailbox),
        as_of=AS_OF,
    )
    assert summary["source_status"] == "available"
    assert summary["mailboxes_verified"] == 2
    assert summary["allowlist_source"] == "approved_active_employee_roster"
    assert summary["scope"].endswith("gmail.metadata")
    assert summary["body_access"] is False


def test_agency_summary_fails_closed_on_delegated_mailbox_mismatch():
    with pytest.raises(GmailAccountabilityError, match="verification mismatch"):
        collect_agency_summary(
            environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@test.invalid"},
            approved_users=["karla@streetsmart.insurance"],
            service_factory=lambda *_args: _Service("someone-else@streetsmart.insurance"),
            as_of=AS_OF,
        )


def test_notice_gmail_scopes_are_readonly_unless_modify_requested():
    assert notice_gmail_scopes() == (GMAIL_READONLY_SCOPE,)
    assert notice_gmail_scopes(modify=False) == (GMAIL_READONLY_SCOPE,)
    assert notice_gmail_scopes(modify=True) == (GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE)
    assert GMAIL_METADATA_SCOPE not in notice_gmail_scopes()
    assert GMAIL_METADATA_SCOPE not in notice_gmail_scopes(modify=True)


def _install_fake_google_modules(monkeypatch, captured):
    class FakeSigner:
        def __init__(self, *_args, **_kwargs):
            pass

    class FakeRequest:
        def __init__(self, *_args, **_kwargs):
            pass

    class FakeCredentials:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def fake_default(**_kwargs):
        return (object(), "project")

    def fake_build(api, version, *, credentials, cache_discovery):
        captured["build"] = {
            "api": api,
            "version": version,
            "credentials": credentials,
            "cache_discovery": cache_discovery,
        }
        return {"service": True}

    google = types.ModuleType("google")
    google_auth = types.ModuleType("google.auth")
    google_auth.default = fake_default
    iam = types.ModuleType("google.auth.iam")
    iam.Signer = FakeSigner
    transport = types.ModuleType("google.auth.transport")
    requests = types.ModuleType("google.auth.transport.requests")
    requests.Request = FakeRequest
    oauth2 = types.ModuleType("google.oauth2")
    service_account = types.ModuleType("google.oauth2.service_account")
    service_account.Credentials = FakeCredentials
    gapiclient = types.ModuleType("googleapiclient")
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = fake_build

    google.auth = google_auth
    google.oauth2 = oauth2
    google_auth.iam = iam
    google_auth.transport = transport
    transport.requests = requests
    oauth2.service_account = service_account
    gapiclient.discovery = discovery
    for name in (
        "google",
        "google.auth",
        "google.auth.iam",
        "google.auth.transport",
        "google.auth.transport.requests",
        "google.oauth2",
        "google.oauth2.service_account",
        "googleapiclient",
        "googleapiclient.discovery",
    ):
        module = {
            "google": google,
            "google.auth": google_auth,
            "google.auth.iam": iam,
            "google.auth.transport": transport,
            "google.auth.transport.requests": requests,
            "google.oauth2": oauth2,
            "google.oauth2.service_account": service_account,
            "googleapiclient": gapiclient,
            "googleapiclient.discovery": discovery,
        }[name]
        if "." not in name or name.count(".") == 1:
            module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)


def test_build_keyless_delegated_service_defaults_to_metadata(monkeypatch):
    captured = {}
    _install_fake_google_modules(monkeypatch, captured)
    service = build_keyless_delegated_service(
        "hermes-poc@example.test", "jackie@streetsmart.insurance"
    )
    assert service == {"service": True}
    assert captured["scopes"] == [GMAIL_METADATA_SCOPE]
    assert captured["subject"] == "jackie@streetsmart.insurance"
    assert captured["service_account_email"] == "hermes-poc@example.test"


def test_build_keyless_delegated_service_honors_explicit_scopes(monkeypatch):
    captured = {}
    _install_fake_google_modules(monkeypatch, captured)
    build_keyless_delegated_service(
        "hermes-poc@example.test",
        "hello@streetsmart.insurance",
        scopes=(GMAIL_READONLY_SCOPE,),
    )
    assert captured["scopes"] == [GMAIL_READONLY_SCOPE]
    assert captured["subject"] == "hello@streetsmart.insurance"


def test_build_notice_gmail_service_requests_readonly_by_default(monkeypatch):
    captured = {}
    _install_fake_google_modules(monkeypatch, captured)
    build_notice_gmail_service("hermes-poc@example.test", "hello@streetsmart.insurance")
    assert captured["scopes"] == [GMAIL_READONLY_SCOPE]
    assert GMAIL_METADATA_SCOPE not in captured["scopes"]


def test_build_notice_gmail_service_requests_modify_when_enabled(monkeypatch):
    captured = {}
    _install_fake_google_modules(monkeypatch, captured)
    build_notice_gmail_service(
        "hermes-poc@example.test",
        "hello@streetsmart.insurance",
        modify=True,
    )
    assert captured["scopes"] == [GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE]
    assert GMAIL_METADATA_SCOPE not in captured["scopes"]


class TestDelegatedGmailScopeSelection(unittest.TestCase):
    """Unittest discover collects this; pytest-style helpers above stay too."""

    def test_notice_scopes_never_include_metadata(self):
        self.assertEqual(notice_gmail_scopes(), (GMAIL_READONLY_SCOPE,))
        self.assertEqual(
            notice_gmail_scopes(modify=True),
            (GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE),
        )
        self.assertNotIn(GMAIL_METADATA_SCOPE, notice_gmail_scopes(modify=True))

    def test_accountability_summary_stays_metadata_only(self):
        summary = collect_agency_summary(
            environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@test.invalid"},
            approved_users=["karla@streetsmart.insurance"],
            service_factory=lambda _service_account, mailbox: _Service(mailbox),
            as_of=AS_OF,
        )
        self.assertTrue(summary["scope"].endswith("gmail.metadata"))
        self.assertFalse(summary["body_access"])
