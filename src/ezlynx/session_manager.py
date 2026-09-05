"""Autonomous EZLynx Session & Login Manager via Playwright.

Handles:
- Headless authentication with persistent storage state (data/ezlynx_storage_state.json).
- Detection and autonomous resolution of email 2FA verification codes via robie@streetsmart.insurance.
- Session validation and auto-refresh without kicking other users.
- Fast execution of internal EZLynx API calls within the browser context.
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from src.config import settings, BASE_DIR
from src.security.secrets_manager import secrets_mgr
from src.security.email_2fa_handler import email_2fa_resolver

logger = logging.getLogger("ezlynx_session")

class EZLynxSessionManager:
    """Manages authenticated browser sessions and cookies for EZLynx."""

    LOGIN_URL = "https://app.ezlynx.com/auth/account/login"
    DASHBOARD_URL = "https://app.ezlynx.com/web/dashboard"

    def __init__(
        self,
        storage_state_path: Optional[Path] = None,
        user_data_dir: Optional[Path] = None,
        cdp_url: Optional[str] = None
    ):
        self.storage_state_path = storage_state_path or (BASE_DIR / "data" / "ezlynx_storage_state.json")
        self.user_data_dir = user_data_dir or (BASE_DIR / "data" / "ezlynx_chrome_profile")
        self.cdp_url = cdp_url or settings.ezlynx_cdp_endpoint

    def get_credentials(self) -> Dict[str, str]:
        """Retrieves EZLynx credentials from SecretsManager (GCP/Keychain), settings, or environment."""
        username = secrets_mgr.get_credential("ezlynx", "username") or settings.ezlynx_username or "SSRobie"
        password = secrets_mgr.get_credential("ezlynx", "password") or settings.ezlynx_password or ""
        return {"username": username, "password": password}

    async def get_authenticated_context(
        self,
        playwright_instance,
        headless: bool = True
    ) -> tuple[Browser, BrowserContext]:
        """
        Returns an authenticated Playwright Browser and BrowserContext.
        Uses existing storage state if valid, or logs in headlessly if expired.
        """
        # Option 1: Connect to existing Chrome over CDP if explicitly running
        if self.cdp_url:
            try:
                logger.info(f"Connecting to existing Chrome instance over CDP at {self.cdp_url}...")
                browser = await playwright_instance.chromium.connect_over_cdp(self.cdp_url)
                ctx = browser.contexts[0]
                if await self._is_context_authenticated(ctx):
                    logger.info("CDP Chrome session is authenticated with EZLynx.")
                    return browser, ctx
            except Exception as e:
                logger.debug(f"CDP connection to {self.cdp_url} unavailable ({e}), falling back to standalone...")

        # Option 2: Launch Chromium with persistent storage state
        browser = await playwright_instance.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )

        ctx_kwargs = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        }

        if self.storage_state_path.exists():
            try:
                ctx_kwargs["storage_state"] = str(self.storage_state_path)
                logger.info(f"Loaded existing EZLynx storage state from {self.storage_state_path.name}")
            except Exception as e:
                logger.warning(f"Failed to load storage state: {e}")

        context = await browser.new_context(**ctx_kwargs)

        # Validate whether session is alive
        if await self._is_context_authenticated(context):
            logger.info("Existing EZLynx session is active and valid.")
            return browser, context

        # Otherwise perform fresh login
        logger.info("Session expired or missing. Performing automated login for Robie...")
        success = await self._perform_login(context)
        if not success:
            logger.error("Failed to authenticate into EZLynx.")
            raise RuntimeError("EZLynx authentication failed.")

        # Save new storage state
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(self.storage_state_path))
        logger.info(f"✅ Saved updated EZLynx session state to {self.storage_state_path.name}")

        return browser, context

    async def _is_context_authenticated(self, context: BrowserContext) -> bool:
        """Verifies if the current context has an active EZLynx session."""
        page = await context.new_page()
        try:
            # Check internal policy API using a known dummy or test request
            logger.debug("Testing EZLynx session health via internal endpoint...")
            resp = await page.goto("https://app.ezlynx.com/web/dashboard", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            current_url = page.url.lower()
            if "auth/account/login" in current_url:
                logger.debug("Session check redirected to login page.")
                return False

            if "ezlynx.com/web/" in current_url:
                return True

            return False
        except Exception as e:
            logger.debug(f"Session validation check exception: {e}")
            return False
        finally:
            await page.close()

    async def _perform_login(self, context: BrowserContext) -> bool:
        """Executes the login form entry, 2FA detection, and OTP submission."""
        creds = self.get_credentials()
        username = creds.get("username")
        password = creds.get("password")

        if not password:
            logger.error("No EZLynx password configured in environment or Secret Manager.")
            return False

        page = await context.new_page()
        try:
            logger.info(f"Navigating to {self.LOGIN_URL}...")
            await page.goto(self.LOGIN_URL, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)

            # Check if login form is present
            user_input = page.locator("#txtUserName, input[name='Username']").first
            pwd_input = page.locator("#txtPassword, input[name='Password']").first
            login_btn = page.locator("#btnLogin, button:has-text('Log in')").first

            if not await user_input.is_visible(timeout=5000):
                logger.error("EZLynx login inputs not found on page.")
                return False

            logger.info(f"Submitting credentials for user: {username}...")
            await user_input.fill(username)
            await asyncio.sleep(0.3)
            await pwd_input.fill(password)
            await asyncio.sleep(0.3)
            await login_btn.click()

            # Wait for response / 2FA screen
            await asyncio.sleep(3)

            # Check for 2FA challenge
            otp_selectors = [
                "input[placeholder*='code' i]", "input[placeholder*='passcode' i]",
                "input#code", "input#otp", "input[name*='code' i]",
                "input[name*='passcode' i]", "input[name*='otp' i]",
                "input[name*='verification' i]"
            ]

            for sel in otp_selectors:
                otp_elem = page.locator(sel).first
                if await otp_elem.is_visible(timeout=2000):
                    logger.info("🔐 EZLynx 2FA verification challenge detected!")
                    logger.info("Listening on robie@streetsmart.insurance for incoming 2FA email...")
                    code = await email_2fa_resolver.wait_for_code("EZLynx", timeout_sec=90)
                    if not code:
                        logger.error("Failed to retrieve 2FA code from email.")
                        return False

                    logger.info(f"Injecting 2FA OTP code: {code}...")
                    await otp_elem.fill(code)
                    await asyncio.sleep(0.5)

                    submit_2fa = page.locator("button[type='submit'], button:has-text('Verify'), button:has-text('Submit'), button:has-text('Continue')").first
                    await submit_2fa.click()
                    await asyncio.sleep(4)
                    break

            # Confirm navigation to main dashboard
            await page.wait_for_url("**/web/**", timeout=25000)
            logger.info(f"✅ Successfully authenticated into EZLynx as {username}!")
            return True

        except Exception as e:
            logger.error(f"Error during EZLynx login automation: {e}")
            return False
        finally:
            await page.close()

    async def fetch_applicant_policies(self, applicant_id: str) -> List[Dict[str, Any]]:
        """
        Executes real-time policy query within the authenticated browser context:
        GET /PolicyAPI/v1/PolicyCard/GetPolicies?applicantId={applicantId}
        """
        async with async_playwright() as p:
            browser, context = await self.get_authenticated_context(p, headless=settings.playwright_headless)
            page = await context.new_page()
            try:
                # Ensure we are on an ezlynx domain to have cookie scope
                await page.goto(f"https://app.ezlynx.com/web/account/{applicant_id}/activity", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(1.5)

                result = await page.evaluate(f'''async () => {{
                    try {{
                        const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId={applicant_id}');
                        if (!r.ok) return [];
                        const j = await r.json();
                        return (j.policyCards || []).map(p => ({{
                            policyNumber: p.policyNumber,
                            carrierName: p.carrierName,
                            lob: p.lob,
                            statusId: p.policyStatusViewModelID,
                            cancellationDate: p.cancellationDate,
                            expirationDate: p.expirationDate,
                            premium: p.premium
                        }}));
                    }} catch (e) {{
                        return [];
                    }}
                }}''')
                return result
            finally:
                await page.close()
                await browser.close()
