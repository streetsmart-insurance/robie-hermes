import asyncio
import logging
import os
import re
import sys
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dump_tables")

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds["username"]
    password = creds["password"]

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        await page.goto("https://portal.bhhomestate.com/", wait_until="load")
        await asyncio.sleep(2)

        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.click("#okta-signin-submit")
            await asyncio.sleep(3)

        send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
        if await send_code_btn.count() > 0:
            click_time = time.time()
            await send_code_btn.first.click()
            await asyncio.sleep(2)

            interceptor = MultiInboxOTPInterceptor()
            service = interceptor.get_service("carlo@streetsmart.insurance")
            code = None
            for _ in range(25):
                await asyncio.sleep(3)
                res = service.users().messages().list(userId="me", q="from:noreply@bhhomestate.com newer_than:2m", maxResults=2).execute()
                messages = res.get("messages", [])
                if messages:
                    msg = service.users().messages().get(userId="me", id=messages[0]["id"], format="full").execute()
                    if int(msg.get("internalDate", 0)) / 1000.0 >= click_time - 15:
                        body = interceptor.extract_body(msg.get("payload", {}))
                        m = re.search(r"\b(\d{6})\b", body)
                        if m:
                            code = m.group(1)
                            break
            if code:
                otp_field = page.locator("input[name='credentials.passcode'], input[type='tel'], input[name='answer'], input.okta-form-input-field")
                await otp_field.first.fill(code)
                await page.locator("input[value='Verify'], button:has-text('Verify')").first.click()
                await asyncio.sleep(6)

        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=25000) as new_page_info:
            await manage_link.click()

        myp_page = await new_page_info.value
        await myp_page.wait_for_url("**/Home_Producer.aspx**", timeout=45000)
        await asyncio.sleep(3)

        # Print all elements containing 18316236
        elems = await myp_page.locator("*:has-text('18316236')").all()
        logger.info(f"Elements matching 18316236: {len(elems)}")
        for e in elems[-5:]:
            tag = await e.evaluate("el => el.tagName")
            outer = await e.evaluate("el => el.outerHTML")
            logger.info(f"Tag: {tag}, HTML: {outer[:300]}")

        # Search box on Home_Producer.aspx:
        # Notice in screenshot: "View Issued Policies: Search By Name, Billing Account, Policy Number, Phone Number, Policy State"
        # Is there a search box for quotes or policies?
        inputs = await myp_page.locator("input").all()
        for inp in inputs:
            inp_id = await inp.get_attribute("id")
            inp_name = await inp.get_attribute("name")
            inp_val = await inp.get_attribute("value")
            logger.info(f"Input: id='{inp_id}' name='{inp_name}' value='{inp_val}'")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
