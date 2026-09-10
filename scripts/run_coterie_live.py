import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright

from src.security.secrets_manager import secrets_mgr
from src.security.email_2fa_handler import email_2fa_resolver
from src.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coterie_live")

async def run():
    creds = secrets_mgr.get_login_pair("Coterie")
    username, password = creds.get("username"), creds.get("password")
    policy_number = "CBB-00113127-02"
    insured_name = "Green Lion Lawn Care LLC DBA Lawn Buddies"

    logger.info(f"Starting Coterie Okta live crawl for user: {username}")
    screenshots_dir = Path("data/screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = Path("data/downloads")
    downloads_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # Step 1: Navigate to Coterie login
        logger.info("Navigating to Coterie dashboard/login page...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        await page.screenshot(path="data/screenshots/coterie_step1_login.png")

        # Step 2: Fill Username / Identifier (Okta Widget)
        logger.info("Looking for username input...")
        user_sel = "input[name='identifier'], input#okta-signin-username, input[name='username'], input[type='text'], input[type='email']"
        await page.wait_for_selector(user_sel, timeout=15000)
        await page.fill(user_sel, username)
        await page.screenshot(path="data/screenshots/coterie_step2_username_entered.png")

        # Step 3: Click Next
        next_btn = await page.query_selector("input[type='submit'], button[type='submit'], input.button-primary, button:has-text('Next')")
        if next_btn:
            logger.info("Clicking Next button...")
            await next_btn.click()
            await page.wait_for_timeout(4000)

        await page.screenshot(path="data/screenshots/coterie_step3_password_or_auth.png")
        logger.info(f"Current URL: {page.url}")

        # Step 4: Fill Password if prompted
        pwd_sel = "input[name='credentials.passcode'], input[name='password'], input#okta-signin-password, input[type='password']"
        pwd_elem = await page.query_selector(pwd_sel)
        if pwd_elem:
            logger.info("Password input found. Entering password...")
            await pwd_elem.fill(password)
            await page.screenshot(path="data/screenshots/coterie_step4_pwd_filled.png")
            verify_btn = await page.query_selector("input[type='submit'], button[type='submit'], button:has-text('Verify'), button:has-text('Sign In'), button:has-text('Next')")
            if verify_btn:
                await verify_btn.click()
                await page.wait_for_timeout(5000)
            await page.screenshot(path="data/screenshots/coterie_step5_after_password.png")

        # Step 5: Check for 2FA / Factor Selection / Email Code
        # Look for factor buttons (e.g. "Get a code emailed to...")
        email_factor_btn = await page.query_selector("a:has-text('Email'), button:has-text('Email'), a:has-text('Send me an email'), div.authenticator-button:has-text('Email')")
        if email_factor_btn:
            logger.info("Email 2FA factor button detected. Clicking to send code...")
            await email_factor_btn.click()
            await page.wait_for_timeout(3000)
            await page.screenshot(path="data/screenshots/coterie_step6_email_factor_clicked.png")

        # Check for OTP input
        otp_sel = "input[name='credentials.passcode'], input[placeholder*='code' i], input[placeholder*='passcode' i], input#code, input#otp, input[name*='passcode' i]"
        otp_elem = await page.query_selector(otp_sel)
        if otp_elem and await otp_elem.is_visible():
            logger.info("🔐 2FA OTP prompt active! Polling email for code...")
            code = await email_2fa_resolver.wait_for_code("Coterie", timeout_sec=90)
            if code:
                logger.info(f"Submitting 2FA code: {code}")
                await otp_elem.fill(code)
                submit_otp = await page.query_selector("input[type='submit'], button[type='submit'], button:has-text('Verify'), button:has-text('Submit')")
                if submit_otp:
                    await submit_otp.click()
                    await page.wait_for_timeout(6000)
                await page.screenshot(path="data/screenshots/coterie_step7_after_2fa.png")
            else:
                logger.warning("No 2FA code found in email.")

        # Step 6: Logged in Dashboard
        logger.info(f"Dashboard Landing URL: {page.url}")
        await page.wait_for_timeout(5000)
        await page.screenshot(path="data/screenshots/coterie_step8_dashboard.png")

        # Search for policy
        search_input = await page.query_selector("input[placeholder*='Search' i], input[type='search'], input[name*='search' i]")
        if search_input:
            logger.info(f"Searching for policy #{policy_number}...")
            await search_input.fill(policy_number)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)
            await page.screenshot(path="data/screenshots/coterie_step9_search_results.png")

        await browser.close()
        logger.info("Coterie live crawl script finished.")

if __name__ == "__main__":
    asyncio.run(run())
