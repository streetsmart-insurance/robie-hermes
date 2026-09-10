"""Live CDP preflight: Login wall fails closed; cookies/Classic API cannot pass."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ezlynx.cdp_session_preflight import (
    HITL_RELOGIN_MESSAGE,
    MAX_PREFLIGHT_CHECKS,
    CdpSessionBlocked,
    assert_live_cdp_authed,
    choose_preflight_result,
    classify_live_page,
    preflight_live_cdp_session,
)
from src.ezlynx.policy_renewer import (
    DEFAULT_PRODUCER_CSR,
    ManualPolicyRenewer,
    RenewalJobSpec,
)
from src.ezlynx.session_manager import EZLynxSessionManager


LOGIN_URL = "https://app.ezlynx.com/auth/account/login"
FORCED_OFF_URL = "https://app.ezlynx.com/auth/account/forcedOff"
DASHBOARD_URL = "https://app.ezlynx.com/web/dashboard"
ACCOUNT_URL = "https://app.ezlynx.com/web/account/196126698/activity"


def test_login_url_blocked_immediately():
    result = classify_live_page(
        LOGIN_URL,
        title="EZLynx Login",
        body="Username Password Log in Forgot your password",
    )
    assert result.ok is False
    assert result.status == "blocked"
    assert result.reason == "login_url"
    assert "login_or_forcedoff_url" in result.signals
    with pytest.raises(CdpSessionBlocked, match="HITL/BLOCKED"):
        assert_live_cdp_authed(result)
    assert "re-login SSRobie" in HITL_RELOGIN_MESSAGE
    assert "password-reset" in HITL_RELOGIN_MESSAGE
    assert result.checks_run == 1
    assert result.checks_run <= MAX_PREFLIGHT_CHECKS


def test_forced_off_url_blocked():
    result = classify_live_page(FORCED_OFF_URL, title="Forced Off", body="You have been forced off")
    assert result.ok is False
    assert result.status == "blocked"
    assert result.reason == "forcedoff_url"


def test_dashboard_url_passes():
    result = classify_live_page(
        DASHBOARD_URL,
        title="EZLynx Dashboard",
        body="My Queue Applicants",
    )
    assert result.ok is True
    assert result.status == "authed"
    assert result.reason == "live_dashboard"
    assert assert_live_cdp_authed(result) is result


def test_storage_state_active_plus_live_login_still_blocked():
    result = classify_live_page(
        LOGIN_URL,
        title="Login",
        body="Please log in",
        storage_state_active=True,
        classic_api_active=True,
    )
    assert result.ok is False
    assert result.status == "blocked"
    assert result.storage_state_active is True
    assert result.classic_api_active is True
    payload = result.to_dict()
    assert payload["trusted_storage_state"] is False
    assert payload["trusted_classic_api"] is False
    assert "storage_state_active_ignored" in result.signals
    assert "classic_api_active_ignored" in result.signals
    with pytest.raises(CdpSessionBlocked):
        assert_live_cdp_authed(result)


def test_login_form_without_authed_url_blocked():
    result = classify_live_page(
        "https://app.ezlynx.com/",
        title="EZLynx",
        body="",
        has_login_form=True,
        storage_state_active=True,
    )
    assert result.ok is False
    assert result.reason == "login_page_signals"


def test_authed_account_tab_preferred_over_login_tab():
    login = classify_live_page(LOGIN_URL, title="Login", storage_state_active=True)
    dash = classify_live_page(ACCOUNT_URL, title="Activity")
    chosen = choose_preflight_result([login, dash], storage_state_active=True)
    assert chosen.ok is True
    assert chosen.url == ACCOUNT_URL


def test_no_pages_blocked():
    chosen = choose_preflight_result([])
    assert chosen.ok is False
    assert chosen.reason == "no_live_ezlynx_page"


@pytest.mark.asyncio
async def test_preflight_inspects_live_page_once_no_navigation():
    page = MagicMock()
    page.url = LOGIN_URL
    page.title = AsyncMock(return_value="EZLynx Login")
    page.evaluate = AsyncMock(
        return_value={"body": "Forgot your password", "hasLoginForm": True}
    )
    page.goto = AsyncMock()
    ctx = MagicMock()
    ctx.pages = [page]

    result = await preflight_live_cdp_session(
        ctx, storage_state_active=True, classic_api_active=True
    )
    assert result.ok is False
    assert result.reason == "login_url"
    assert result.checks_run == 1
    page.goto.assert_not_called()
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_connected_job_login_url_blocks_before_applicant_navigation(tmp_path: Path):
    proof = tmp_path / "fagone.proof.json"
    preflight = classify_live_page(
        LOGIN_URL,
        title="EZLynx Login",
        body="Username Password Log in",
        storage_state_active=True,
        classic_api_active=True,
    )

    class BoomSession:
        async def __aenter__(self):
            raise CdpSessionBlocked(preflight)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    spec = RenewalJobSpec(
        applicant_id="196126698",
        policy_id="99",
        policy_number="HO-TEST",
        line_of_business="Homeowners",
        dry_run=False,
        env="test",
        cdp_url="http://localhost:9222",
        proof_json=proof,
        writing_company="Carrier",
        premium=Decimal("1"),
        effective_date="2026-10-01",
        expiration_date="2027-10-01",
        producer=DEFAULT_PRODUCER_CSR,
    )
    renewer = ManualPolicyRenewer(api_client=MagicMock())
    renewer.verify_pending_shells_via_ui = AsyncMock()
    renewer._upload_if_allowed = AsyncMock()
    renewer._key_renewal_shell = AsyncMock()

    with patch("src.ezlynx.policy_renewer.ConnectedCdpSession", return_value=BoomSession()):
        result = await renewer.run_connected_job(spec)

    assert result.status == "blocked"
    assert result.proof_source == "live_cdp_preflight"
    assert result.preflight is not None
    assert result.preflight["reason"] == "login_url"
    assert result.preflight["trusted_storage_state"] is False
    assert "re-login SSRobie" in (result.error or "")
    renewer.verify_pending_shells_via_ui.assert_not_awaited()
    renewer._upload_if_allowed.assert_not_awaited()
    renewer._key_renewal_shell.assert_not_awaited()
    payload = json.loads(proof.read_text())
    assert payload["status"] == "blocked"
    assert payload["preflight"]["url"] == LOGIN_URL
    assert payload["preflight"]["checks_run"] == 1


@pytest.mark.asyncio
async def test_connected_job_dashboard_preflight_proceeds(tmp_path: Path):
    page = MagicMock()
    page.url = DASHBOARD_URL
    page.goto = AsyncMock()
    session = MagicMock()
    session.page = page
    session.preflight = classify_live_page(DASHBOARD_URL, title="Dashboard")

    class OkSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    spec = RenewalJobSpec(
        applicant_id="196126698",
        policy_id="99",
        policy_number="HO-TEST",
        line_of_business="Homeowners",
        dry_run=False,
        verify_only=True,
        env="test",
        cdp_url="http://localhost:9222",
        proof_json=tmp_path / "dash.proof.json",
        writing_company="Carrier",
        premium=Decimal("1"),
        effective_date="2026-10-01",
        expiration_date="2027-10-01",
        producer=DEFAULT_PRODUCER_CSR,
    )
    renewer = ManualPolicyRenewer(api_client=MagicMock())
    renewer.verify_pending_shells_via_ui = AsyncMock(return_value=([], ["policy_summary_history"]))

    with patch("src.ezlynx.policy_renewer.ConnectedCdpSession", return_value=OkSession()):
        result = await renewer.run_connected_job(spec)

    assert result.status == "verified_clear"
    assert result.preflight is not None
    assert result.preflight["ok"] is True
    renewer.verify_pending_shells_via_ui.assert_awaited_once()
    page.goto.assert_not_called()


@pytest.mark.asyncio
async def test_session_manager_cdp_login_does_not_password_login():
    mgr = EZLynxSessionManager(cdp_url="http://localhost:9222")
    login_page = MagicMock()
    login_page.url = LOGIN_URL
    login_page.title = AsyncMock(return_value="EZLynx Login")
    login_page.evaluate = AsyncMock(
        return_value={"body": "Forgot your password", "hasLoginForm": True}
    )

    ctx = MagicMock()
    ctx.pages = [login_page]
    ctx.new_page = AsyncMock()
    browser = MagicMock()
    browser.contexts = [ctx]
    pw = MagicMock()
    pw.chromium.connect_over_cdp = AsyncMock(return_value=browser)
    pw.chromium.launch = AsyncMock()

    with patch.object(mgr, "_perform_login", new_callable=AsyncMock) as perform_login:
        with pytest.raises(CdpSessionBlocked, match="HITL/BLOCKED"):
            await mgr.get_authenticated_context(pw)
        perform_login.assert_not_awaited()
    pw.chromium.launch.assert_not_called()
    ctx.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_manager_cdp_dashboard_returns_context():
    mgr = EZLynxSessionManager(cdp_url="http://localhost:9222")
    dash = MagicMock()
    dash.url = DASHBOARD_URL
    dash.title = AsyncMock(return_value="Dashboard")
    dash.evaluate = AsyncMock(return_value={"body": "My Queue", "hasLoginForm": False})
    ctx = MagicMock()
    ctx.pages = [dash]
    browser = MagicMock()
    browser.contexts = [ctx]
    pw = MagicMock()
    pw.chromium.connect_over_cdp = AsyncMock(return_value=browser)

    with patch.object(mgr, "_perform_login", new_callable=AsyncMock) as perform_login:
        got_browser, got_ctx = await mgr.get_authenticated_context(pw)
        perform_login.assert_not_awaited()
    assert got_browser is browser
    assert got_ctx is ctx
