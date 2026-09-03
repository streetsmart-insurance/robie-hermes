import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright
from src.security.secrets_manager import secrets_mgr

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

        print("[1] Navigating to Coterie login...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Step 1: Identifier
        user_sel = page.locator("input[name='identifier'], input[name='username']")
        if await user_sel.count() > 0:
            print("[2] Submitting username...")
            await user_sel.first.fill(username)
            await page.locator("input[type='submit'], button[type='submit']").first.click()
            await page.wait_for_timeout(3000)

        # Step 2: Click Send me an email input button
        send_email_input = page.locator("input[value='Send me an email'], input[value*='email' i]")
        if await send_email_input.count() > 0:
            print("[3] Clicking 'Send me an email' button...")
            await send_email_input.first.click()
            await page.wait_for_timeout(3000)

        # Step 3: Click 'Enter a verification code instead'
        code_link = page.locator("a:has-text('Enter a verification code instead'), a.link:has-text('code')")
        if await code_link.count() > 0:
            print("[4] Clicking 'Enter a verification code instead' link...")
            await code_link.first.click()
            await page.wait_for_timeout(2000)

        await page.screenshot(path="data/screenshots/coterie_otp_ready.png")
        print("SCREENSHOT_READY: data/screenshots/coterie_otp_ready.png")

        # Let's see what inputs are on this page
        inputs = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('input, button, a')).map(el => ({
                tag: el.tagName,
                type: el.type || null,
                name: el.name || null,
                id: el.id || null,
                placeholder: el.placeholder || null,
                text: el.innerText || el.value || null
            }));
        }''')
        print("Page interactive elements:", inputs)

        await browser.close()

asyncio.run(run())
