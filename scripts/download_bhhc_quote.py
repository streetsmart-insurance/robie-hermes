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
logger = logging.getLogger("bhhc_quote")

TARGET_FILE = os.path.abspath("data/downloads/carrier_renewals/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf")
os.makedirs(os.path.dirname(TARGET_FILE), exist_ok=True)

QUOTE_NUM = "18316236"

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds["username"]
    password = creds["password"]

    async with async_playwright() as p:
        logger.info("Launching standalone Firefox in headless mode...")
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )
        page = await context.new_page()

        logger.info("Navigating to https://portal.bhhomestate.com/...")
        await page.goto("https://portal.bhhomestate.com/", wait_until="load")
        await asyncio.sleep(2)

        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            logger.info("Filling Okta credentials...")
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.click("#okta-signin-submit")
            await asyncio.sleep(4)

        send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
        if await send_code_btn.count() > 0:
            logger.info("Sending OTP code to email...")
            click_time = time.time()
            await send_code_btn.first.click()
            await asyncio.sleep(2)

            interceptor = MultiInboxOTPInterceptor()
            service = interceptor.get_service("carlo@streetsmart.insurance")
            code = None
            for attempt in range(25):
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
                            logger.info(f"Intercepted OTP: {code}")
                            break
            if code:
                otp_field = page.locator("input[name='credentials.passcode'], input[type='tel'], input[name='answer'], input.okta-form-input-field")
                await otp_field.first.fill(code)
                await page.locator("input[value='Verify'], button:has-text('Verify')").first.click()
                await asyncio.sleep(6)

        logger.info("Portal reached. Clicking Auto -> Manage Your Policy...")
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=20000) as new_page_info:
            await manage_link.click()
        
        myp_page = await new_page_info.value
        # Wait for Home_Producer.aspx
        for _ in range(30):
            if "Home_Producer.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(4)
        logger.info(f"Loaded producer page: {myp_page.url}")

        # Click Accept on cookies banner if present
        cookie_btn = myp_page.locator("button:has-text('Accept')")
        if await cookie_btn.count() > 0:
            logger.info("Dismissing cookie banner...")
            await cookie_btn.first.click()
            await asyncio.sleep(1)

        # Find row for ABC TRANSPIRATION LLC or quote 18316236
        row = myp_page.locator(f"tr:has-text('{QUOTE_NUM}')").first
        logger.info(f"Found quote row count: {await row.count()}")
        
        quote_link = row.locator(f"a:has-text('{QUOTE_NUM}')").first
        logger.info(f"Clicking quote link {QUOTE_NUM}...")
        await quote_link.click()

        # Wait for Quote Details
        for _ in range(20):
            if "Quote_Details.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)
        logger.info(f"Quote Details page loaded: {myp_page.url}")

        # Locate Quote Proposal button
        proposal_btn = myp_page.locator("#ctl00_Main_ContentPlaceHolder_btnPrintQuote, input[value='Quote Proposal']").first
        logger.info(f"Quote Proposal button count: {await proposal_btn.count()}")

        logger.info("Triggering Quote Proposal download...")
        async with myp_page.expect_download(timeout=25000) as dl_info:
            await proposal_btn.click()

        download = await dl_info.value
        temp_path = await download.path()
        logger.info(f"Playwright downloaded to: {temp_path} ({os.path.getsize(temp_path)} bytes)")

        # Write to destination
        with open(temp_path, "rb") as f_src:
            data = f_src.read()

        with open(TARGET_FILE, "wb") as f_dst:
            f_dst.write(data)
            f_dst.flush()
            os.fsync(f_dst.fileno())

        logger.info(f"SUCCESS: Saved {len(data)} bytes to {TARGET_FILE}")
        logger.info(f"File verification on disk: exists={os.path.exists(TARGET_FILE)} size={os.path.getsize(TARGET_FILE)}")

        await browser.close()
        logger.info("Done.")

if __name__ == "__main__":
    asyncio.run(run())
