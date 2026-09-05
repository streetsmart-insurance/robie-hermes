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
logger = logging.getLogger("bhhc_download")

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

        logger.info("Clicking ABC TRANSPIRATION row...")
        row = myp_page.locator("tr:has-text('ABC TRANSPIRATION')").first
        await row.click(force=True)

        logger.info("Waiting for Bind_Financing.aspx...")
        await myp_page.wait_for_url("**/Bind_Financing.aspx**", timeout=30000)
        await asyncio.sleep(3)

        # Inspect "View Quote Packet" link
        packet_link = myp_page.locator("text='View Quote Packet'").first
        logger.info(f"Packet link count: {await packet_link.count()}")
        logger.info(f"Packet tag: {await packet_link.evaluate('e => e.tagName')}")
        logger.info(f"Packet outerHTML: {await packet_link.evaluate('e => e.outerHTML')}")

        # Check if clicking it opens a new page or triggers a download
        # We can handle both:
        download_task = None
        new_tab_task = None

        logger.info("Setting up listener for download or new tab...")
        try:
            async with context.expect_page(timeout=10000) as tab_info:
                # Also set up expect_download in background
                await packet_link.click(force=True)
            doc_tab = await tab_info.value
            await asyncio.sleep(3)
            logger.info(f"Opened new tab: {doc_tab.url}")
            # If it's a PDF URL or ASPX report
            # Check response or page content
            content = await doc_tab.content()
            if "pdf" in doc_tab.url.lower():
                resp = await context.request.get(doc_tab.url)
                body = await resp.body()
                with open(TARGET_FILE, "wb") as f:
                    f.write(body)
                logger.info(f"Saved {len(body)} bytes from new tab URL to {TARGET_FILE}")
            else:
                # Screenshot the tab
                await doc_tab.screenshot(path="data/screenshots/bhhc/doc_tab.png")
                logger.info(f"Tab screenshot saved. URL={doc_tab.url}")
        except Exception as e:
            logger.warning(f"New tab was not opened: {e}. Trying expect_download...")
            async with myp_page.expect_download(timeout=15000) as dl_info:
                await packet_link.click(force=True)
            download = await dl_info.value
            temp_dl = await download.path()
            with open(temp_dl, "rb") as f_in:
                data = f_in.read()
            with open(TARGET_FILE, "wb") as f_out:
                f_out.write(data)
                f_out.flush()
                os.fsync(f_out.fileno())
            logger.info(f"SUCCESS: Downloaded {len(data)} bytes to {TARGET_FILE}")

        logger.info(f"Target file status: exists={os.path.exists(TARGET_FILE)} size={os.path.getsize(TARGET_FILE) if os.path.exists(TARGET_FILE) else 0}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
