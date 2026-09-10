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
logger = logging.getLogger("bhhc_postback")

TARGET_FILE = os.path.abspath("data/downloads/carrier_renewals/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf")
os.makedirs(os.path.dirname(TARGET_FILE), exist_ok=True)

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds["username"]
    password = creds["password"]

    async with async_playwright() as p:
        logger.info("Launching standalone headless Firefox...")
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )
        page = await context.new_page()

        logger.info("Opening portal...")
        await page.goto("https://portal.bhhomestate.com/", wait_until="load")
        await asyncio.sleep(2)

        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            logger.info("Submitting Okta credentials...")
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.click("#okta-signin-submit")
            await asyncio.sleep(3)

        send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
        if await send_code_btn.count() > 0:
            logger.info("Requesting email OTP...")
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
                            logger.info(f"Intercepted OTP: {code}")
                            break
            if not code:
                logger.error("Failed to intercept OTP!")
                await browser.close()
                sys.exit(1)

            otp_field = page.locator("input[name='credentials.passcode'], input[type='tel'], input[name='answer'], input.okta-form-input-field")
            await otp_field.first.fill(code)
            await page.locator("input[value='Verify'], button:has-text('Verify')").first.click()
            await asyncio.sleep(6)

        logger.info("Navigating to Auto -> Manage Your Policy...")
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=20000) as new_page_info:
            await manage_link.click()
        
        myp_page = await new_page_info.value
        for _ in range(30):
            if "Home_Producer.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(4)
        logger.info(f"Producer home loaded: {myp_page.url}")

        # Execute postback for ABC TRANSPIRATION quote directly in page
        # ctl00$Main_ContentPlaceHolder$uctrlProducerQuotes$grdQuotes$ctl06$ctl01
        logger.info("Executing JavaScript postback for ABC Transpiration quote 18316236...")
        await myp_page.evaluate("__doPostBack('ctl00$Main_ContentPlaceHolder$uctrlProducerQuotes$grdQuotes$ctl06$ctl01','')")

        # Wait for Quote Details
        for _ in range(25):
            if "Quote_Details.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)
        logger.info(f"Quote Details page loaded: {myp_page.url}")

        # Locate proposal button
        btn = myp_page.locator("#ctl00_Main_ContentPlaceHolder_btnPrintQuote, input[value='Quote Proposal']").first
        logger.info(f"Proposal button count: {await btn.count()}")

        logger.info("Clicking Quote Proposal button with expect_download...")
        async with myp_page.expect_download(timeout=25000) as dl_info:
            await btn.click(force=True)

        download = await dl_info.value
        temp_dl = await download.path()
        logger.info(f"Downloaded by Playwright: {temp_dl} ({os.path.getsize(temp_dl)} bytes)")

        with open(temp_dl, "rb") as f_in:
            data = f_in.read()

        with open(TARGET_FILE, "wb") as f_out:
            f_out.write(data)
            f_out.flush()
            os.fsync(f_out.fileno())

        logger.info(f"SUCCESS: Saved {len(data)} bytes to {TARGET_FILE}")
        logger.info(f"VERIFIED on disk: exists={os.path.exists(TARGET_FILE)} size={os.path.getsize(TARGET_FILE)}")

        await browser.close()
        logger.info("Done!")

if __name__ == "__main__":
    asyncio.run(run())
