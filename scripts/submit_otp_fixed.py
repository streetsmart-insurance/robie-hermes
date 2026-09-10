import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright
from src.security.secrets_manager import secrets_mgr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coterie_otp_submit")

async def run(code="414861"):
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

        logger.info("Opening Coterie login...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # 1. Fill OTP code if we are already on the Enter Code screen
        passcode_input = page.locator("input[name='credentials.passcode']")
        if await passcode_input.count() > 0 and await passcode_input.first.is_visible():
            logger.info(f"Submitting 2FA OTP code: {code}...")
            await passcode_input.first.fill(code)
            await page.wait_for_timeout(500)
            
            # Click Verify
            submit_btn = page.locator("input[type='submit'], button[type='submit']").first
            await submit_btn.click()
            await page.wait_for_timeout(6000)

        await page.screenshot(path="data/screenshots/coterie_step_after_otp.png")
        logger.info(f"Page URL after OTP verify: {page.url}")

        # Check if password prompt appears
        pwd_input = page.locator("input[type='password']")
        if await pwd_input.count() > 0 and await pwd_input.first.is_visible():
            logger.info("Entering password...")
            await pwd_input.first.fill(password)
            submit_pwd = page.locator("input[type='submit'], button[type='submit']").first
            await submit_pwd.click()
            await page.wait_for_timeout(8000)

        await page.screenshot(path="data/screenshots/coterie_step_final.png")
        logger.info(f"Final Landed URL: {page.url}")

        # If on dashboard, search policy CBB-00113127-02
        if "dashboard.coterieinsurance.com" in page.url and "login" not in page.url:
            logger.info("SUCCESSFULLY LOGGED INTO COTERIE DASHBOARD!")
            await page.context.storage_state(path="data/coterie_auth_state.json")
            
            # Policy search
            policy_num = "CBB-00113127-02"
            search_box = page.locator("input[placeholder*='Search' i], input[type='search']")
            if await search_box.count() > 0:
                logger.info(f"Searching for policy {policy_num}...")
                await search_box.first.fill(policy_num)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(5000)
                await page.screenshot(path="data/screenshots/coterie_policy_found.png")

        await browser.close()

if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "414861"
    asyncio.run(run(c))
