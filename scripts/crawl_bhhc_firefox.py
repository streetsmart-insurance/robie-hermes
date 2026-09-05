import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_crawler")

TARGET_POLICY = "02TRM066190-01"
TARGET_INSURED = "ABC TRANSPIRATION"

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds.get("username") or "carlo@streetsmart.insurance"
    password = creds.get("password") or "Policy!2026Shield"
    
    logger.info(f"Using credentials for {username}...")
    
    os.makedirs("data/screenshots/bhhc", exist_ok=True)
    os.makedirs("data/downloads/carrier_renewals", exist_ok=True)

    async with async_playwright() as p:
        logger.info("Launching standalone Firefox in headless mode...")
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )
        page = await context.new_page()

        logger.info("Navigating to https://portal.bhhomestate.com/...")
        await page.goto("https://portal.bhhomestate.com/", wait_until="networkidle", timeout=45000)
        logger.info(f"Current page URL: {page.url}")
        await page.screenshot(path="data/screenshots/bhhc/1_initial.png")

        # Okta login
        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            logger.info("Entering username & password on Okta login...")
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.screenshot(path="data/screenshots/bhhc/2_creds_filled.png")
            
            logger.info("Submitting login form...")
            await page.click("#okta-signin-submit")
            await asyncio.sleep(4)
            logger.info(f"After submit URL: {page.url}")
            await page.screenshot(path="data/screenshots/bhhc/3_after_submit.png")

        # Check if email OTP verification required
        send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
        click_time = time.time()
        if await send_code_btn.count() > 0:
            logger.info("Clicking 'Send me the code' button...")
            click_time = time.time()
            await send_code_btn.first.click()
            await asyncio.sleep(3)
            await page.screenshot(path="data/screenshots/bhhc/4_code_sent.png")

        # Intercept OTP code from carlo@streetsmart.insurance
        otp_field = page.locator("input[name='credentials.passcode'], input[type='tel'], input[name='answer'], input[name='code'], input.okta-form-input-field")
        if await otp_field.count() > 0:
            logger.info("OTP field detected. Polling Gmail for verification code from noreply@bhhomestate.com...")
            interceptor = MultiInboxOTPInterceptor()
            service = interceptor.get_service("carlo@streetsmart.insurance")
            
            code = None
            for attempt in range(25):
                await asyncio.sleep(3)
                try:
                    res = service.users().messages().list(
                        userId="me",
                        q="from:noreply@bhhomestate.com newer_than:2m",
                        maxResults=2
                    ).execute()
                    messages = res.get("messages", [])
                    if messages:
                        latest_id = messages[0]["id"]
                        msg = service.users().messages().get(userId="me", id=latest_id, format="full").execute()
                        internal_date = int(msg.get("internalDate", 0)) / 1000.0
                        if internal_date >= click_time - 15:
                            body = interceptor.extract_body(msg.get("payload", {}))
                            m = re.search(r"\b(\d{6})\b", body)
                            if m:
                                code = m.group(1)
                                logger.info(f"Successfully intercepted OTP: {code}")
                                break
                except Exception as e:
                    logger.warning(f"Error querying Gmail: {e}")
                logger.info(f"Attempt {attempt+1}/25: Waiting for OTP...")

            if not code:
                logger.error("Timed out waiting for OTP code from Gmail.")
                await browser.close()
                return

            logger.info(f"Entering OTP code {code} into verification input...")
            await otp_field.first.fill(code)
            await page.screenshot(path="data/screenshots/bhhc/5_otp_filled.png")
            
            verify_btn = page.locator("input[value='Verify'], button:has-text('Verify'), input[type='submit']")
            await verify_btn.first.click()
            logger.info("Clicked Verify. Waiting for portal dashboard...")
            await asyncio.sleep(8)

        logger.info(f"Landed on: {page.url} (Title: {await page.title()})")
        await page.screenshot(path="data/screenshots/bhhc/6_dashboard.png")

        # Check Auto menu
        auto_link = page.locator("text='Auto'")
        if await auto_link.count() > 0:
            logger.info("Navigating to Auto section...")
            await auto_link.first.click()
            await asyncio.sleep(3)
            await page.screenshot(path="data/screenshots/bhhc/7_auto_menu.png")

        # Check 'Manage Your Policy' or Policy Search
        manage_policy = page.locator("text='Manage Your Policy'")
        target_page = page
        if await manage_policy.count() > 0:
            logger.info("Clicking 'Manage Your Policy'...")
            try:
                async with context.expect_page(timeout=10) as new_page_info:
                    await manage_policy.first.click()
                policy_page = await new_page_info.value
                logger.info(f"Manage policy opened in new tab: {policy_page.url}")
                await policy_page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(4)
                target_page = policy_page
            except Exception as e:
                logger.info(f"Did not open new tab ({e}), checking current tab...")
                await asyncio.sleep(4)
                target_page = page

        logger.info(f"Active policy page URL: {target_page.url} (Title: {await target_page.title()})")
        await target_page.screenshot(path="data/screenshots/bhhc/8_policy_search_page.png")

        # Dump text and links on the target page
        links = await target_page.locator("a").all_inner_texts()
        logger.info(f"Page links: {[l.strip() for l in links if l.strip()]}")
        body_text = await target_page.inner_text("body")
        logger.info(f"Page text excerpt:\n{body_text[:2000]}")

        await browser.close()
        logger.info("Firefox run complete.")

if __name__ == "__main__":
    asyncio.run(run())
