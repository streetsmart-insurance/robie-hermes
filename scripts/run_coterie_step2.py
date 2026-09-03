import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright

from src.security.secrets_manager import secrets_mgr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coterie_step2")

async def run():
    creds = secrets_mgr.get_login_pair("Coterie")
    username = creds.get("username", "carlo@streetsmart.insurance")

    profile_dir = Path.home() / ".coterie_chrome_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"]
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        logger.info("Navigating to Coterie login...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        # Enter username
        user_sel = "input[name='identifier'], input#okta-signin-username, input[name='username'], input[type='text']"
        if await page.query_selector(user_sel):
            await page.fill(user_sel, username)
            next_btn = await page.query_selector("input[type='submit'], button[type='submit'], button:has-text('Next')")
            if next_btn:
                await next_btn.click()
                await page.wait_for_timeout(4000)

        # Click 'Send me an email'
        send_email_btn = await page.query_selector("button:has-text('Send me an email'), a:has-text('Send me an email')")
        if send_email_btn:
            logger.info("Clicking 'Send me an email' button...")
            await send_email_btn.click()
            await page.wait_for_timeout(3000)

        await page.screenshot(path="data/screenshots/coterie_code_sent.png")
        logger.info(f"Page URL after sending code: {page.url}")

        # Keep browser open for a minute or save state
        await browser.close()
        logger.info("Coterie verification email triggered.")

if __name__ == "__main__":
    asyncio.run(run())
