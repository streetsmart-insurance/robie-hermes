"""Unit tests for EZLynxSessionManager."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

from src.ezlynx.session_manager import (
    EMAIL_2FA_RADIO_SELECTOR,
    OTP_CODE_FALLBACK_SELECTORS,
    OTP_CODE_SELECTOR,
    TWO_FACTOR_NEXT_SELECTOR,
    EZLynxSessionManager,
)


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


def test_otp_selectors_never_target_verification_radios():
    """After password submit, fill #verification-code — never radios named verification*."""
    assert EMAIL_2FA_RADIO_SELECTOR == "#VerificationType_EMAIL-input"
    assert TWO_FACTOR_NEXT_SELECTOR == "#two-factor-next"
    assert OTP_CODE_SELECTOR == "#verification-code"
    joined = " ".join(OTP_CODE_FALLBACK_SELECTORS)
    assert "verification" not in joined.lower()
    assert "name*='verification'" not in joined
    assert OTP_CODE_SELECTOR not in OTP_CODE_FALLBACK_SELECTORS


@pytest.mark.asyncio
async def test_complete_email_otp_selects_email_then_fills_code():
    mgr = EZLynxSessionManager()
    mock_page = MagicMock()

    email_radio = MagicMock()
    email_radio.count = AsyncMock(return_value=1)
    email_radio.is_visible = AsyncMock(return_value=True)
    email_radio.click = AsyncMock()

    next_btn = MagicMock()
    next_btn.count = AsyncMock(return_value=1)
    next_btn.is_visible = AsyncMock(return_value=True)
    next_btn.click = AsyncMock()

    code_input = MagicMock()
    code_input.count = AsyncMock(return_value=1)
    code_input.is_visible = AsyncMock(return_value=True)
    code_input.fill = AsyncMock()

    submit_btn = MagicMock()
    submit_btn.click = AsyncMock()

    def locator_side_effect(selector):
        loc = MagicMock()
        if selector == EMAIL_2FA_RADIO_SELECTOR:
            loc.first = email_radio
        elif selector == TWO_FACTOR_NEXT_SELECTOR:
            loc.first = next_btn
        elif selector == OTP_CODE_SELECTOR:
            loc.first = code_input
        elif TWO_FACTOR_NEXT_SELECTOR in selector:
            loc.first = submit_btn
        else:
            missing = MagicMock()
            missing.count = AsyncMock(return_value=0)
            missing.is_visible = AsyncMock(return_value=False)
            loc.first = missing
        return loc

    mock_page.locator.side_effect = locator_side_effect

    with patch("src.ezlynx.session_manager.email_2fa_resolver.wait_for_code", new_callable=AsyncMock) as wait_code, \
            patch("src.ezlynx.session_manager.asyncio.sleep", new_callable=AsyncMock):
        wait_code.return_value = "482910"
        ok = await mgr._complete_email_otp(mock_page)

    assert ok is True
    email_radio.click.assert_awaited_once()
    next_btn.click.assert_awaited()
    code_input.fill.assert_awaited_once_with("482910")
    submit_btn.click.assert_awaited_once()
