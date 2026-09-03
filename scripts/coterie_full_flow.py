import asyncio
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

        print("[1] Navigating to Coterie dashboard...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # Check if already logged in
        if "dashboard.coterieinsurance.com" in page.url and "login" not in page.url:
            print("Already authenticated! URL:", page.url)
        else:
            print("[2] Entering username...")
            user_sel = page.locator("input[name='identifier'], input#okta-signin-username, input[name='username']")
            if await user_sel.count() > 0:
                await user_sel.first.fill(username)
                next_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Next')")
                if await next_btn.count() > 0:
                    await next_btn.first.click()
                    await page.wait_for_timeout(4000)

            await page.screenshot(path="data/screenshots/coterie_flow_step2.png")
            print("Step 2 URL:", page.url)

            # Check if password prompt is here
            pwd_sel = page.locator("input[name='credentials.passcode'], input[type='password'], input#okta-signin-password")
            if await pwd_sel.count() > 0:
                print("[3] Password prompt found. Entering password...")
                await pwd_sel.first.fill(password)
                verify_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Verify'), button:has-text('Sign In')")
                if await verify_btn.count() > 0:
                    await verify_btn.first.click()
                    await page.wait_for_timeout(6000)

            await page.screenshot(path="data/screenshots/coterie_flow_step3.png")
            print("Step 3 URL:", page.url)

        await browser.close()

asyncio.run(run())
