import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright
from src.security.secrets_manager import secrets_mgr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coterie_crawler")

async def run(code=None):
    creds = secrets_mgr.get_login_pair("Coterie")
    username = creds.get("username", "carlo@streetsmart.insurance")
    password = creds.get("password", "dezrax-myqwy9-wYrrom")
    profile_dir = Path.home() / ".coterie_chrome_profile"
    downloads_dir = Path("data/downloads")
    downloads_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"]
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        logger.info("Navigating to Coterie dashboard...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Check if login is required
        if "login" in page.url or "authorize" in page.url:
            logger.info("Login required. Entering username...")
            user_input = page.locator("input[name='identifier'], input[name='username']")
            if await user_input.count() > 0:
                await user_input.first.fill(username)
                await page.locator("input.button-primary[type='submit'], button[type='submit']").first.click()
                await page.wait_for_timeout(3000)

            # Check if Send me an email is present
            send_email_btn = page.locator("input.button-primary[value='Send me an email']")
            if await send_email_btn.count() > 0:
                logger.info("Clicking 'Send me an email'...")
                await send_email_btn.first.click()
                await page.wait_for_timeout(3000)

            # Check for OTP code input
            passcode_input = page.locator("input[name='credentials.passcode']")
            if await passcode_input.count() > 0:
                if code:
                    logger.info(f"Submitting 2FA code: {code}...")
                    await passcode_input.first.fill(code)
                    submit_btn = page.locator("button[type='submit']:has-text('Enter a verification code instead'), input[type='submit'], button[type='submit']")
                    if await submit_btn.count() > 0:
                        await submit_btn.first.click()
                    else:
                        await passcode_input.first.press("Enter")
                    await page.wait_for_timeout(6000)
                else:
                    logger.info("Awaiting 2FA code from user...")
                    await page.screenshot(path="data/screenshots/coterie_ready_for_code.png")
                    await browser.close()
                    return

            # Check if password is requested next
            pwd_input = page.locator("input[type='password'], input[name='credentials.passcode']")
            if await pwd_input.count() > 0:
                logger.info("Submitting password...")
                await pwd_input.first.fill(password)
                verify_btn = page.locator("input[type='submit'], button[type='submit']")
                if await verify_btn.count() > 0:
                    await verify_btn.first.click()
                    await page.wait_for_timeout(8000)

        logger.info(f"Authenticated Landing URL: {page.url}")
        await page.screenshot(path="data/screenshots/coterie_logged_in.png")

        # Navigate to policies page or search
        logger.info("Navigating to Policies tab / searching...")
        policy_num = "CBB-00113127-02"
        
        # Try searching directly
        search_box = page.locator("input[placeholder*='Search' i], input[type='search']")
        if await search_box.count() > 0:
            logger.info(f"Searching for policy {policy_num}...")
            await search_box.first.fill(policy_num)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)
            await page.screenshot(path="data/screenshots/coterie_policy_found.png")

        await page.context.storage_state(path="data/coterie_auth_state.json")
        logger.info("Session state saved successfully.")
        await browser.close()

if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(c))
