"""Unit tests for EZLynxSessionManager."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

from src.ezlynx.session_manager import EZLynxSessionManager


def test_session_manager_credentials_resolution():
    with patch("src.ezlynx.session_manager.secrets_mgr.get_credential") as mock_cred:
        def cred_side_effect(service, key):
            if key == "username":
                return "SSRobie"
            if key == "password":
                return "SecretPassword123"
            return None
        mock_cred.side_effect = cred_side_effect

        mgr = EZLynxSessionManager()
        creds = mgr.get_credentials()
        assert creds["username"] == "SSRobie"
        assert creds["password"] == "SecretPassword123"


@pytest.mark.asyncio
async def test_session_manager_is_context_authenticated_true():
    mgr = EZLynxSessionManager()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.close = AsyncMock()
    mock_page.url = "https://app.ezlynx.com/web/dashboard"
    mock_context.new_page = AsyncMock(return_value=mock_page)

    is_auth = await mgr._is_context_authenticated(mock_context)
    assert is_auth is True
    mock_page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_manager_is_context_authenticated_false_on_login_redirect():
    mgr = EZLynxSessionManager()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.close = AsyncMock()
    mock_page.url = "https://app.ezlynx.com/auth/account/login"
    mock_context.new_page = AsyncMock(return_value=mock_page)

    is_auth = await mgr._is_context_authenticated(mock_context)
    assert is_auth is False
    mock_page.close.assert_awaited_once()
