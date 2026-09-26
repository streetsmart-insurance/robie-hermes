"""Regression tests for the Chat APP single-identity rule (M4 hardening).

Chat posts go out as the dedicated Chat app's service-account identity and
ONLY as that identity:

- ``ROBIE_CHAT_SA_KEY_FILE`` is the only credential source. The
  ``ROBIE_GOOGLE_TOKEN_FILE`` (user token) and ``google.auth.default`` (ADC)
  fallbacks were removed — fail closed with ``ChatAppIdentityError``.
- The loaded key's ``client_email`` must match ``ROBIE_CHAT_APP_CLIENT_EMAIL``.
- The key file must be a protected secret mount (0600-style perms, owned by
  the service user); inline key JSON via env var is never accepted.
- Identity failures fire the operator alert (email/ops channel, never Chat)
  instead of silently returning False.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

# unittest discover imports this module. pytest.importorskip raises
# pytest.skip at import time, which discover records as an error when
# googleapiclient is not installed. A skip mark leaves the import clean.
try:
    import googleapiclient  # noqa: F401
    import google.auth  # noqa: F401
except ImportError:
    googleapiclient = None

pytestmark = pytest.mark.skipif(
    googleapiclient is None,
    reason="googleapiclient is not installed",
)

from robie_job_engine import chat_app_post


EXPECTED_EMAIL = "robie-chat-app@streetsmart-hermes-poc.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "ROBIE_CHAT_SA_KEY_FILE",
        "ROBIE_GOOGLE_TOKEN_FILE",
        "ROBIE_CHAT_APP_CLIENT_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ROBIE_CHAT_APP_CLIENT_EMAIL", EXPECTED_EMAIL)


@pytest.fixture(autouse=True)
def _clean_alert_sender(monkeypatch):
    # raising=False so this file also fails cleanly (not at fixture setup)
    # on the pre-fix module, which lacks the attribute.
    monkeypatch.setattr(chat_app_post, "_identity_alert_sender", None, raising=False)


def _patch_build(monkeypatch):
    builds = []
    fake_client = object()
    monkeypatch.setattr(
        "googleapiclient.discovery.build",
        lambda *args, **kwargs: builds.append((args, kwargs)) or fake_client,
    )
    return builds, fake_client


def _sa_credentials(email=EXPECTED_EMAIL):
    creds = mock.Mock()
    creds.service_account_email = email
    return creds


def _write_key_file(tmp_path, *, mode=0o600, email=EXPECTED_EMAIL):
    key = tmp_path / "chat-sa.json"
    key.write_text(
        '{"type": "service_account", "client_email": "%s"}' % email
    )
    os.chmod(key, mode)
    return key


def _patch_from_sa(monkeypatch, creds):
    return mock.patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=creds,
    )


def test_sa_key_file_used_with_principal_check(monkeypatch, tmp_path):
    """The SA key is still the honored source — now with identity validation."""
    key = _write_key_file(tmp_path)
    monkeypatch.setenv("ROBIE_CHAT_SA_KEY_FILE", str(key))
    # A token file must NOT win when the SA key is configured (and is ignored).
    monkeypatch.setenv("ROBIE_GOOGLE_TOKEN_FILE", "/tmp/should-not-be-used.json")
    builds, fake_client = _patch_build(monkeypatch)
    with (
        _patch_from_sa(monkeypatch, _sa_credentials()) as from_sa,
        mock.patch("google.auth.default") as default,
        mock.patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file"
        ) as from_user,
    ):
        client = chat_app_post._chat_app_client()
    assert client is fake_client
    from_sa.assert_called_once_with(str(key), scopes=[chat_app_post.CHAT_BOT_SCOPE])
    from_user.assert_not_called()
    default.assert_not_called()
    (args, kwargs), = builds
    assert args[:2] == ("chat", "v1")
    assert kwargs["credentials"] is from_sa.return_value
    assert kwargs["cache_discovery"] is False


def test_token_file_no_longer_honored(monkeypatch):
    """REWRITTEN: the user-token fallback was removed — fail closed.

    Previously this test asserted the token file was USED when no SA key was
    configured. Now a configured user token with no SA key raises instead of
    posting as a human.
    """
    monkeypatch.setenv("ROBIE_GOOGLE_TOKEN_FILE", "/tmp/user-token.json")
    builds, fake_client = _patch_build(monkeypatch)
    with (
        mock.patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file"
        ) as from_user,
        mock.patch("google.auth.default") as default,
    ):
        # NOTE: the exception class is referenced by name so the pre-fix
        # failure mode is a clean "DID NOT RAISE" (the fallback was honored).
        with pytest.raises(Exception) as excinfo:
            chat_app_post._chat_app_client()
    assert type(excinfo.value).__name__ == "ChatAppIdentityError"
    assert "user-token fallback" in str(excinfo.value)
    from_user.assert_not_called()
    default.assert_not_called()
    assert builds == []


def test_no_credential_fallback_when_nothing_configured(monkeypatch):
    """REWRITTEN: no SA key, no token file — fail closed, never ADC.

    Previously this test asserted the ADC fallback. Now _chat_app_client
    raises ChatAppIdentityError and google.auth.default is never consulted.
    """
    builds, fake_client = _patch_build(monkeypatch)
    with mock.patch(
        "google.auth.default", return_value=(object(), None)
    ) as default:
        # NOTE: referenced by name so the pre-fix failure is a clean
        # "DID NOT RAISE" (ADC was consulted).
        with pytest.raises(Exception) as excinfo:
            chat_app_post._chat_app_client()
    assert type(excinfo.value).__name__ == "ChatAppIdentityError"
    assert "ROBIE_CHAT_SA_KEY_FILE" in str(excinfo.value)
    default.assert_not_called()
    assert builds == []


def test_principal_mismatch_refuses_client(monkeypatch, tmp_path):
    """A valid key file for the WRONG service account is refused."""
    key = _write_key_file(
        tmp_path, email="someone-else@evil.iam.gserviceaccount.com"
    )
    monkeypatch.setenv("ROBIE_CHAT_SA_KEY_FILE", str(key))
    _patch_build(monkeypatch)
    with _patch_from_sa(
        monkeypatch, _sa_credentials("someone-else@evil.iam.gserviceaccount.com")
    ):
        with pytest.raises(
            chat_app_post.ChatAppIdentityError, match="principal mismatch"
        ):
            chat_app_post._chat_app_client()


def test_missing_expected_identity_config_refuses(monkeypatch, tmp_path):
    """Without ROBIE_CHAT_APP_CLIENT_EMAIL there is nothing to validate against."""
    key = _write_key_file(tmp_path)
    monkeypatch.setenv("ROBIE_CHAT_SA_KEY_FILE", str(key))
    monkeypatch.delenv("ROBIE_CHAT_APP_CLIENT_EMAIL")
    _patch_build(monkeypatch)
    with _patch_from_sa(monkeypatch, _sa_credentials()):
        with pytest.raises(
            chat_app_post.ChatAppIdentityError, match="ROBIE_CHAT_APP_CLIENT_EMAIL"
        ):
            chat_app_post._chat_app_client()


def test_group_readable_key_file_refused(monkeypatch, tmp_path):
    """A key file readable by group/other is not a protected secret mount."""
    key = _write_key_file(tmp_path, mode=0o640)
    monkeypatch.setenv("ROBIE_CHAT_SA_KEY_FILE", str(key))
    _patch_build(monkeypatch)
    with _patch_from_sa(monkeypatch, _sa_credentials()):
        with pytest.raises(
            chat_app_post.ChatAppIdentityError, match="permission"
        ):
            chat_app_post._chat_app_client()


def test_world_readable_key_file_refused(monkeypatch, tmp_path):
    key = _write_key_file(tmp_path, mode=0o644)
    monkeypatch.setenv("ROBIE_CHAT_SA_KEY_FILE", str(key))
    _patch_build(monkeypatch)
    with _patch_from_sa(monkeypatch, _sa_credentials()):
        with pytest.raises(
            chat_app_post.ChatAppIdentityError, match="permission"
        ):
            chat_app_post._chat_app_client()


def test_missing_key_file_refused(monkeypatch, tmp_path):
    """A configured-but-absent key file fails closed, not via fallback."""
    monkeypatch.setenv("ROBIE_CHAT_SA_KEY_FILE", str(tmp_path / "nope.json"))
    _patch_build(monkeypatch)
    with mock.patch("google.auth.default") as default:
        with pytest.raises(
            chat_app_post.ChatAppIdentityError, match="unreadable"
        ):
            chat_app_post._chat_app_client()
    default.assert_not_called()


def test_inline_key_json_refused(monkeypatch):
    """Inline key JSON via the env var is never accepted."""
    monkeypatch.setenv(
        "ROBIE_CHAT_SA_KEY_FILE", '{"type": "service_account", "client_email": "x"}'
    )
    _patch_build(monkeypatch)
    with pytest.raises(
        chat_app_post.ChatAppIdentityError, match="inline key"
    ):
        chat_app_post._chat_app_client()


def _fake_store(conversation_id="spaces/ABC"):
    store = mock.Mock()
    store.get_job.return_value = {"payload": {"conversation_id": conversation_id}}
    return store


def test_identity_failure_fires_operator_alert(monkeypatch):
    """Identity failure inside post_hitl_to_originating_thread pages the
    operator (email/ops channel) instead of silently returning False."""
    alerts = []
    monkeypatch.setattr(
        chat_app_post,
        "_identity_alert_sender",
        lambda recipients, subject, body: alerts.append(
            (recipients, subject, body)
        ),
    )
    poster = mock.Mock(
        side_effect=chat_app_post.ChatAppIdentityError(
            "ROBIE_CHAT_SA_KEY_FILE is not set"
        )
    )
    ok = chat_app_post.post_hitl_to_originating_thread(
        "hello", job_id="j1", store=_fake_store(), poster=poster
    )
    assert ok is False
    assert len(alerts) == 1
    recipients, subject, body = alerts[0]
    assert recipients == chat_app_post.fail_notify_emails()
    assert "Chat app identity failure" in subject
    assert "ROBIE_CHAT_SA_KEY_FILE" in body
    assert "ROBIE_CHAT_APP_CLIENT_EMAIL" in body


def test_missing_key_post_path_raises_identity_error(monkeypatch, tmp_path):
    """End to end through the default poster: no SA key -> ChatAppIdentityError
    (which the caller above turns into the operator alert)."""
    with mock.patch(
        "google.oauth2.service_account.Credentials.from_service_account_file"
    ):
        with pytest.raises(chat_app_post.ChatAppIdentityError):
            chat_app_post._chat_app_client()


def test_identity_alert_goes_to_email_channel_not_chat(monkeypatch):
    """The alert sender is the injectable email path — nothing posts to Chat."""
    sent = []
    monkeypatch.setattr(
        chat_app_post,
        "_identity_alert_sender",
        lambda recipients, subject, body: sent.append(
            (recipients, subject, body)
        ),
    )
    with mock.patch("googleapiclient.discovery.build") as build:
        result = chat_app_post.alert_chat_app_identity_failure("test reason")
    assert result is True
    build.assert_not_called()
    (recipients, subject, body), = sent
    assert recipients == ["carlo@streetsmart.insurance", "jake@streetsmart.insurance"]
    assert "test reason" in body


def test_other_post_errors_do_not_fire_identity_alert(monkeypatch):
    """Transient post failures stay silent-False; only identity failures page."""
    alerts = []
    monkeypatch.setattr(
        chat_app_post,
        "_identity_alert_sender",
        lambda *a: alerts.append(a),
    )
    poster = mock.Mock(side_effect=RuntimeError("network blew up"))
    ok = chat_app_post.post_hitl_to_originating_thread(
        "hello", job_id="j1", store=_fake_store(), poster=poster
    )
    assert ok is False
    assert alerts == []
