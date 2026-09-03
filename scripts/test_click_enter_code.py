import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from src.security.secrets_manager import secrets_mgr

async def run():
    creds = secrets_mgr.get_login_pair("Coterie")
    username = creds.get("username", "carlo@streetsmart.insurance")
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
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Enter username
        user_sel = "input[name='identifier'], input#okta-signin-username, input[name='username']"
        if await page.query_selector(user_sel):
            await page.fill(user_sel, username)
            next_btn = await page.query_selector("input[type='submit'], button[type='submit'], button:has-text('Next')")
            if next_btn:
                await next_btn.click()
                await page.wait_for_timeout(3000)

        # Click Send me an email if visible
        btn = page.get_by_text("Send me an email")
        if await btn.count() > 0:
            await btn.first.click()
            await page.wait_for_timeout(3000)

        # Click Enter a verification code instead
        code_link = page.get_by_text("Enter a verification code instead")
        if await code_link.count() > 0:
            await code_link.first.click()
            await page.wait_for_timeout(2000)

        await page.screenshot(path="data/screenshots/coterie_otp_input_box.png")
        print("Screenshot taken: data/screenshots/coterie_otp_input_box.png")
        await browser.close()

asyncio.run(run())
