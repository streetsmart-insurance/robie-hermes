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
logger = logging.getLogger("bhhc_pdf")

SAVE_PATH = os.path.abspath("data/downloads/carrier_renewals/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf")
os.makedirs(os.path.dirname(SAVE_PATH), exist_ok=True)
os.makedirs("data/screenshots/bhhc", exist_ok=True)

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
        # Wait for Home_Producer.aspx to load
        for _ in range(30):
            if "Home_Producer.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)
        logger.info(f"Loaded producer page: {myp_page.url}")

        # Find quote 18316236 link
        quote_link = myp_page.locator("a:has-text('18316236')").first
        logger.info("Clicking quote link 18316236...")
        await quote_link.click()
        await asyncio.sleep(4)
        logger.info(f"Quote details loaded: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc/quote_details_page.png")

        # Now click 'Quote Proposal'
        btn_proposal = myp_page.locator("#ctl00_Main_ContentPlaceHolder_btnPrintQuote, input[value='Quote Proposal']").first
        logger.info("Clicking Quote Proposal button...")

        download_event = asyncio.Event()
        downloaded_obj = None

        def on_download(download):
            nonlocal downloaded_obj
            downloaded_obj = download
            download_event.set()

        myp_page.on("download", on_download)

        # Also listen for new pages / popups
        popup_event = asyncio.Event()
        popup_page = None

        def on_page(new_pg):
            nonlocal popup_page
            popup_page = new_pg
            popup_event.set()

        context.on("page", on_page)

        await btn_proposal.click()
        logger.info("Clicked Quote Proposal. Waiting for response...")

        # Wait up to 15s for download or popup
        done, _ = await asyncio.wait(
            [asyncio.create_task(download_event.wait()), asyncio.create_task(popup_event.wait())],
            timeout=15,
            return_when=asyncio.FIRST_COMPLETED
        )

        if downloaded_obj:
            logger.info("Direct download event received!")
            tmp_path = await downloaded_obj.path()
            logger.info(f"Playwright downloaded to temp: {tmp_path}")
            shutil.copyfile(tmp_path, SAVE_PATH)
            logger.info(f"Copied to {SAVE_PATH} ({os.path.getsize(SAVE_PATH)} bytes)")
        elif popup_page:
            logger.info(f"Popup page opened: {popup_page.url}")
            await popup_page.wait_for_load_state("load")
            await asyncio.sleep(3)
            # Fetch content
            pdf_bytes = await popup_page.content()
            with open(SAVE_PATH, "wb") as f:
                f.write(pdf_bytes.encode("utf-8") if isinstance(pdf_bytes, str) else pdf_bytes)
            logger.info(f"Saved popup content to {SAVE_PATH} ({os.path.getsize(SAVE_PATH)} bytes)")
        else:
            logger.warning("Neither direct download nor popup triggered within timeout.")
            await myp_page.screenshot(path="data/screenshots/bhhc/proposal_result.png")
            logger.info(f"URL after click: {myp_page.url}")

        if os.path.exists(SAVE_PATH):
            logger.info(f"FINAL FILE VERIFICATION: {SAVE_PATH} exists, size = {os.path.getsize(SAVE_PATH)} bytes.")

        await browser.close()
        logger.info("Run finished.")

if __name__ == "__main__":
    asyncio.run(run())
