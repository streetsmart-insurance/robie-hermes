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
logger = logging.getLogger("test_select4")

TARGET_FILE = os.path.abspath("data/downloads/carrier_renewals/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf")
os.makedirs(os.path.dirname(TARGET_FILE), exist_ok=True)
os.makedirs("data/screenshots/bhhc", exist_ok=True)

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds["username"]
    password = creds["password"]

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )
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

        # Dismiss cookie banner
        cookie_btn = myp_page.locator("#onetrust-accept-btn-handler, button:has-text('Accept')")
        if await cookie_btn.count() > 0:
            await cookie_btn.first.click()
            await asyncio.sleep(1)

        logger.info("Clicking the ABC TRANSPIRATION row directly...")
        row = myp_page.locator("tr:has-text('ABC TRANSPIRATION')").first
        await row.click(force=True)

        logger.info("Waiting 10s after click...")
        await asyncio.sleep(10)
        logger.info(f"Page URL after click: {myp_page.url}")
        logger.info(f"Page Title: {await myp_page.title()}")
        await myp_page.screenshot(path="data/screenshots/bhhc/after_row_click.png")

        # Check all buttons / inputs on the loaded page
        buttons = await myp_page.locator("input[type='submit'], input[type='button'], button, a.btn").all()
        logger.info(f"Found {len(buttons)} button elements:")
        for b in buttons:
            val = await b.get_attribute("value")
            bid = await b.get_attribute("id")
            text = await b.inner_text()
            logger.info(f"Button: id='{bid}' value='{val}' text='{text}'")

        # If Quote Proposal button is present, click it and download!
        proposal_btn = myp_page.locator("#ctl00_Main_ContentPlaceHolder_btnPrintQuote, input[value='Quote Proposal'], input[value*='Proposal'], button:has-text('Proposal')")
        if await proposal_btn.count() > 0:
            logger.info("Found Proposal button! Triggering download...")
            async with myp_page.expect_download(timeout=30000) as dl_info:
                await proposal_btn.first.click(force=True)
            download = await dl_info.value
            temp_dl = await download.path()
            with open(temp_dl, "rb") as f_in:
                data = f_in.read()
            with open(TARGET_FILE, "wb") as f_out:
                f_out.write(data)
                f_out.flush()
                os.fsync(f_out.fileno())
            logger.info(f"SUCCESS: Saved {len(data)} bytes to {TARGET_FILE}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
