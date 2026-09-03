import asyncio
import os
import time
from pathlib import Path
from playwright.async_api import async_playwright
from src.security.secrets_manager import secrets_mgr

async def run():
    creds = secrets_mgr.get_login_pair("Coterie")
    username = creds.get("username", "carlo@streetsmart.insurance")
    password = creds.get("password", "dezrax-myqwy9-wYrrom")
    profile_dir = Path.home() / ".coterie_chrome_profile"
    otp_file = Path("data/otp_code.txt")
    if otp_file.exists():
        otp_file.unlink()

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"]
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        print("[1] Opening Coterie login...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        user_sel = page.locator("input[name='identifier'], input#okta-signin-username, input[name='username']")
        if await user_sel.count() > 0:
            print("[2] Entering username...")
            await user_sel.first.fill(username)
            next_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Next')")
            if await next_btn.count() > 0:
                await next_btn.click()
                await page.wait_for_timeout(3000)

        # Click Send me an email
        send_btn = page.locator("button:has-text('Send me an email'), a:has-text('Send me an email')")
        if await send_btn.count() > 0:
            print("[3] Clicking 'Send me an email'...")
            await send_btn.first.click()
            await page.wait_for_timeout(3000)

        # Click Enter a verification code instead
        code_link = page.get_by_text("Enter a verification code instead")
        if await code_link.count() > 0:
            print("[4] Clicking 'Enter a verification code instead'...")
            await code_link.first.click()
            await page.wait_for_timeout(2000)

        await page.screenshot(path="data/screenshots/coterie_waiting_for_otp.png")
        print("READY_FOR_OTP: Verification email sent! Write code to data/otp_code.txt")

        # Wait up to 180 seconds for OTP in data/otp_code.txt
        start = time.time()
        otp = None
        while time.time() - start < 180:
            if otp_file.exists():
                text = otp_file.read_text().strip()
                if len(text) >= 6:
                    otp = text[:6]
                    break
            await asyncio.sleep(1)

        if not otp:
            print("ERROR: Timeout waiting for OTP code.")
            await browser.close()
            return

        print(f"[5] Received OTP: {otp}. Submitting...")
        code_input = page.locator("input[name='credentials.passcode'], input[placeholder*='code' i], input[type='text'], input#okta-signin-passcode")
        if await code_input.count() > 0:
            await code_input.first.fill(otp)
            await page.wait_for_timeout(500)
            verify_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Verify')")
            if await verify_btn.count() > 0:
                await verify_btn.first.click()
                await page.wait_for_timeout(5000)

        await page.screenshot(path="data/screenshots/coterie_after_otp_submitted.png")
        print("Post-OTP URL:", page.url)

        # Check if password prompt appears
        pwd_box = page.locator("input[name='credentials.passcode'], input[type='password'], input#okta-signin-password")
        if await pwd_box.count() > 0:
            print("[6] Password prompt active. Submitting password...")
            await pwd_box.first.fill(password)
            verify_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Verify'), button:has-text('Sign In')")
            if await verify_btn.count() > 0:
                await verify_btn.first.click()
                await page.wait_for_timeout(8000)

        await page.screenshot(path="data/screenshots/coterie_logged_in_final.png")
        print("FINAL_URL:", page.url)

        # Search for policy CBB-00113127-02
        policy_no = "CBB-00113127-02"
        print(f"[7] Searching for policy {policy_no}...")
        search_box = page.locator("input[placeholder*='Search' i], input[type='search'], input[name*='search' i]")
        if await search_box.count() > 0:
            await search_box.first.fill(policy_no)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)

        # Look for policies link / table
        await page.screenshot(path="data/screenshots/coterie_policy_search_final.png")
        await page.context.storage_state(path="data/coterie_auth_state.json")
        print("SUCCESS: Coterie session saved to data/coterie_auth_state.json")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
