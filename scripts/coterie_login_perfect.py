import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright
from src.security.secrets_manager import secrets_mgr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coterie_login")

async def run():
    creds = secrets_mgr.get_login_pair("Coterie")
    username = creds.get("username", "carlo@streetsmart.insurance")
    password = creds.get("password", "dezrax-myqwy9-wYrrom")
    profile_dir = Path.home() / ".coterie_chrome_profile"

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"]
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        logger.info("Step 1: Navigating to Coterie login...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Enter username
        user_input = page.locator("input[name='identifier'], input[name='username']")
        if await user_input.count() > 0:
            logger.info("Entering username...")
            await user_input.first.fill(username)
            await page.locator("input.button-primary[type='submit'], button[type='submit']").first.click()
            await page.wait_for_timeout(3000)

        # Check if "Send me an email" submit button is present
        send_email_btn = page.locator("input.button-primary[value='Send me an email']")
        if await send_email_btn.count() > 0:
            logger.info("Clicking 'Send me an email' button...")
            await send_email_btn.first.click()
            await page.wait_for_timeout(3000)

        # Click "Enter a verification code instead"
        code_link = page.locator("a:has-text('Enter a verification code instead')")
        if await code_link.count() > 0:
            logger.info("Clicking 'Enter a verification code instead' link...")
            await code_link.first.click()
            await page.wait_for_timeout(2000)

        await page.screenshot(path="data/screenshots/coterie_screen_ready_for_code.png")
        logger.info("Screenshot taken: data/screenshots/coterie_screen_ready_for_code.png")
        logger.info(f"Current page URL: {page.url}")

        # Dump visible inputs
        visible_inputs = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('input:not([type="hidden"]), button, a')).map(el => ({
                tag: el.tagName,
                type: el.type || null,
                name: el.name || null,
                placeholder: el.placeholder || null,
                text: el.innerText || el.value || null
            }));
        }''')
        logger.info(f"Visible elements: {visible_inputs}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
