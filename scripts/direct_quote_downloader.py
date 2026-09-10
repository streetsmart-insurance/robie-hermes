import asyncio
import logging
import os
import re
import shutil
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_dl")

FINAL_PATHS = [
    "/tmp/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf",
    os.path.abspath("data/downloads/carrier_renewals/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf")
]

for p in FINAL_PATHS:
    os.makedirs(os.path.dirname(p), exist_ok=True)

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

        await page.goto("https://portal.bhhomestate.com/", wait_until="load")
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

        logger.info("Portal reached. Clicking Auto -> Manage Your Policy...")
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=15000) as new_page_info:
            await manage_link.click()
        
        myp_page = await new_page_info.value
        # Wait for callback to complete
        for _ in range(30):
            if "myp.bhhc.com" in myp_page.url and "auth.bhhc.com" not in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)
        logger.info(f"Manage Your Policy authenticated: {myp_page.url}")

        # Navigate directly to Quote Details for 18316236
        logger.info("Navigating to Quote_Details.aspx?ID=18316236...")
        await myp_page.goto("https://myp.bhhc.com/Forms/Quote_Details.aspx?ID=18316236", wait_until="load")
        await asyncio.sleep(4)
        logger.info(f"Quote Details page URL: {myp_page.url}")

        btn_proposal = myp_page.locator("#ctl00_Main_ContentPlaceHolder_btnPrintQuote")
        logger.info(f"Quote Proposal button count: {await btn_proposal.count()}")

        async with myp_page.expect_download(timeout=20000) as download_info:
            await btn_proposal.click()
            logger.info("Clicked Quote Proposal button, waiting for download...")

        download = await download_info.value
        temp_file = await download.path()
        logger.info(f"Downloaded by Playwright to: {temp_file} ({os.path.getsize(temp_file)} bytes)")

        # Read all bytes into memory
        with open(temp_file, "rb") as f_src:
            data = f_src.read()
        logger.info(f"Read {len(data)} bytes into memory.")

        for p_dest in FINAL_PATHS:
            with open(p_dest, "wb") as f_out:
                f_out.write(data)
                f_out.flush()
                os.fsync(f_out.fileno())
            logger.info(f"Wrote and synced {len(data)} bytes to {p_dest}")
            logger.info(f"Verification: {p_dest} exists={os.path.exists(p_dest)} size={os.path.getsize(p_dest)}")

        await asyncio.sleep(2)
        await browser.close()
        logger.info("Browser closed. All done!")

if __name__ == "__main__":
    asyncio.run(run())
