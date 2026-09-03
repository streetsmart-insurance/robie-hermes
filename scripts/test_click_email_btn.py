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

        # Click the "Send me an email" element
        btn = page.get_by_role("button", name="Send me an email")
        if await btn.count() == 0:
            btn = page.get_by_text("Send me an email")
        
        print("Found Send me an email button count:", await btn.count())
        await btn.first.click()
        await page.wait_for_timeout(4000)
        await page.screenshot(path="data/screenshots/coterie_code_entry_screen.png")
        print("Screenshot taken: data/screenshots/coterie_code_entry_screen.png")
        await browser.close()

asyncio.run(run())
