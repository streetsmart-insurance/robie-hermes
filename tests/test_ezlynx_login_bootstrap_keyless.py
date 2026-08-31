import pytest

import ezlynx_login_bootstrap as bootstrap


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Users:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def getProfile(self, **kwargs):
        return _Execute({"emailAddress": self.mailbox})


class _Gmail:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def users(self):
        return _Users(self.mailbox)


def test_gmail_service_uses_keyless_delegation_when_configured(monkeypatch):
    calls = []
    service = _Gmail(bootstrap.EXPECTED_MAILBOX)
    monkeypatch.setenv(
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT",
        "robie-test@example.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        bootstrap,
        "build_keyless_mailbox_service",
        lambda account: calls.append(account) or service,
    )
    monkeypatch.setattr(
        bootstrap.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: pytest.fail("legacy OAuth token must not be read"),
    )

    assert bootstrap.gmail_service() is service
    assert calls == ["robie-test@example.iam.gserviceaccount.com"]


def test_gmail_service_rejects_wrong_delegated_mailbox(monkeypatch):
    monkeypatch.setenv(
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT",
        "robie-test@example.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        bootstrap,
        "build_keyless_mailbox_service",
        lambda _account: _Gmail("someone-else@streetsmart.insurance"),
    )

    with pytest.raises(bootstrap.MailboxIdentityError):
        bootstrap.gmail_service()
