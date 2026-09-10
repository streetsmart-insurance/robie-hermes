import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

async def run(code=None):
    profile_dir = Path.home() / ".coterie_chrome_profile"
    username = "carlo@streetsmart.insurance"

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"]
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        if code:
            # If code is passed, go to current page and submit code
            print(f"Submitting 2FA code: {code}")
            await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
            await page.wait_for_timeout(2000)
            
            # Click Enter a verification code instead if present
            code_link = page.get_by_text("Enter a verification code instead")
            if await code_link.count() > 0:
                await code_link.first.click()
                await page.wait_for_timeout(2000)
                
            code_box = page.locator("input[name='credentials.passcode'], input[name='passcode'], input[type='text']")
            if await code_box.count() > 0:
                await code_box.first.fill(code)
                verify_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Verify')")
                if await verify_btn.count() > 0:
                    await verify_btn.first.click()
                    await page.wait_for_timeout(6000)
            await page.screenshot(path="data/screenshots/coterie_after_code_submit.png")
            print("Post-verify URL:", page.url)
        else:
            # Step 1: Trigger email
            print("Navigating to Coterie login...")
            await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
            await page.wait_for_timeout(2000)

            user_sel = page.locator("input[name='identifier'], input#okta-signin-username, input[name='username']")
            if await user_sel.count() > 0:
                await user_sel.first.fill(username)
                next_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Next')")
                if await next_btn.count() > 0:
                    await next_btn.first.click()
                    await page.wait_for_timeout(3000)

            send_btn = page.locator("button:has-text('Send me an email'), input[value*='Send me an email']")
            if await send_btn.count() > 0:
                print("Clicking 'Send me an email'...")
                await send_btn.first.click()
                await page.wait_for_timeout(3000)

            await page.screenshot(path="data/screenshots/coterie_email_triggered.png")
            print("Verification email sent to carlo@streetsmart.insurance!")

        await browser.close()

if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(c))
