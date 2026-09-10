import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_myp")

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds.get("username") or "carlo@streetsmart.insurance"
    password = creds.get("password") or "Policy!2026Shield"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        await page.goto("https://portal.bhhomestate.com/", wait_until="networkidle", timeout=45000)

        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.click("#okta-signin-submit")
            await asyncio.sleep(4)

        send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
        if await send_code_btn.count() > 0:
            click_time = time.time()
            await send_code_btn.first.click()
            await asyncio.sleep(2)

            interceptor = MultiInboxOTPInterceptor(["carlo@streetsmart.insurance", "sandy@streetsmart.insurance"])
            code = None
            for _ in range(25):
                await asyncio.sleep(3)
                for inbox in ["carlo@streetsmart.insurance", "sandy@streetsmart.insurance"]:
                    svc = interceptor.get_service(inbox)
                    if not svc:
                        continue
                    res = svc.users().messages().list(userId="me", q="from:noreply@bhhomestate.com newer_than:2m", maxResults=2).execute()
                    messages = res.get("messages", [])
                    if messages:
                        msg = svc.users().messages().get(userId="me", id=messages[0]["id"], format="full").execute()
                        if int(msg.get("internalDate", 0)) / 1000.0 >= click_time - 15:
                            body = interceptor.extract_body(msg.get("payload", {}))
                            m = re.search(r"\b(\d{6})\b", body)
                            if m:
                                code = m.group(1)
                                break
                    if code:
                        break
                if code:
                    break

            if code:
                otp_field = page.locator("input[name='credentials.passcode'], input[type='tel'], input[name='answer'], input.okta-form-input-field")
                await otp_field.first.fill(code)
                await page.locator("input[value='Verify'], button:has-text('Verify')").first.click()
                await asyncio.sleep(6)

        # Click Auto -> Manage Your Policy
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=15000) as new_page_info:
            await manage_link.click()

        myp_page = await new_page_info.value
        await myp_page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(4)

        # Try PolServices_TransHistory.aspx
        logger.info("Navigating to PolServices_TransHistory.aspx...")
        await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_TransHistory.aspx")
        await asyncio.sleep(3)
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/trans_history.png")
        logger.info(f"Trans History URL: {myp_page.url}")
        body1 = await myp_page.inner_text("body")
        logger.info(f"Trans History body: {body1[:1000]}")

        # Try UWInfo_Notes.aspx
        logger.info("Navigating to UWInfo_Notes.aspx...")
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_Notes.aspx")
        await asyncio.sleep(3)
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/uw_notes.png")
        logger.info(f"UW Notes URL: {myp_page.url}")
        body2 = await myp_page.inner_text("body")
        logger.info(f"UW Notes body: {body2[:1000]}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
