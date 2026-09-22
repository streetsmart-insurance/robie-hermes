"""Regression tests for Chat APP credential selection (PR #531 SA-key support).

The Chat API client must accept the Chat app's own service-account key via
ROBIE_CHAT_SA_KEY_FILE, because the VM's default service account does not
carry the chat.bot scope (live proof: 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT).
"""
from __future__ import annotations

from unittest import mock

import pytest

googleapiclient = pytest.importorskip("googleapiclient")
pytest.importorskip("google.auth")

from robie_job_engine import chat_app_post


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ROBIE_CHAT_SA_KEY_FILE", "ROBIE_GOOGLE_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)


def _patch_build(monkeypatch):
    builds = []
    fake_client = object()
    monkeypatch.setattr(
        "googleapiclient.discovery.build",
        lambda *args, **kwargs: builds.append((args, kwargs)) or fake_client,
    )
    return builds, fake_client


def test_sa_key_file_takes_precedence(monkeypatch, tmp_path):
    key = tmp_path / "chat-sa.json"
    key.write_text("{}")
    monkeypatch.setenv("ROBIE_CHAT_SA_KEY_FILE", str(key))
    # A token file must NOT win when the SA key is configured.
    monkeypatch.setenv("ROBIE_GOOGLE_TOKEN_FILE", "/tmp/should-not-be-used.json")
    builds, fake_client = _patch_build(monkeypatch)
    with (
        mock.patch(
            "google.oauth2.service_account.Credentials.from_service_account_file"
        ) as from_sa,
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


def test_token_file_used_when_no_sa_key(monkeypatch):
    monkeypatch.setenv("ROBIE_GOOGLE_TOKEN_FILE", "/tmp/user-token.json")
    builds, fake_client = _patch_build(monkeypatch)
    with (
        mock.patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file"
        ) as from_user,
        mock.patch("google.auth.default") as default,
    ):
        client = chat_app_post._chat_app_client()
    assert client is fake_client
    from_user.assert_called_once_with("/tmp/user-token.json")
    default.assert_not_called()
    assert builds[0][1]["credentials"] is from_user.return_value


def test_adc_fallback_when_no_files_configured(monkeypatch):
    builds, fake_client = _patch_build(monkeypatch)
    fake_creds = object()
    with mock.patch(
        "google.auth.default", return_value=(fake_creds, None)
    ) as default:
        client = chat_app_post._chat_app_client()
    assert client is fake_client
    default.assert_called_once_with(scopes=[chat_app_post.CHAT_BOT_SCOPE])
    assert builds[0][1]["credentials"] is fake_creds
