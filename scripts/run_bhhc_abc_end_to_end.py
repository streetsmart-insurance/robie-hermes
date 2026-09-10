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
logger = logging.getLogger("bhhc_e2e")

TARGET_FILE = os.path.abspath("data/downloads/carrier_renewals/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf")
os.makedirs(os.path.dirname(TARGET_FILE), exist_ok=True)
os.makedirs("data/screenshots/bhhc", exist_ok=True)

async def main():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds.get("username") or "carlo@streetsmart.insurance"
    password = creds.get("password") or "Policy!2026Shield"

    async with async_playwright() as p:
        logger.info("Starting standalone headless Firefox...")
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )
        page = await context.new_page()

        logger.info("1. Navigating to https://portal.bhhomestate.com/...")
        await page.goto("https://portal.bhhomestate.com/", wait_until="load", timeout=45000)
        await asyncio.sleep(3)

        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            logger.info("2. Filling Okta credentials...")
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.click("#okta-signin-submit")
            await asyncio.sleep(4)

        # Wait for either dashboard or send_code_btn
        for _ in range(15):
            send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
            if await send_code_btn.count() > 0:
                logger.info("3. Triggering Okta Email OTP challenge...")
                click_time = time.time()
                await send_code_btn.first.click()
                await asyncio.sleep(3)

                logger.info("4. Polling Gmail for verification code from noreply@bhhomestate.com...")
                interceptor = MultiInboxOTPInterceptor()
                service = interceptor.get_service("carlo@streetsmart.insurance")
                code = None
                for attempt in range(25):
                    await asyncio.sleep(3)
                    res = service.users().messages().list(
                        userId="me",
                        q="from:noreply@bhhomestate.com newer_than:2m",
                        maxResults=2
                    ).execute()
                    messages = res.get("messages", [])
                    if messages:
                        msg = service.users().messages().get(userId="me", id=messages[0]["id"], format="full").execute()
                        internal_date = int(msg.get("internalDate", 0)) / 1000.0
                        if internal_date >= click_time - 15:
                            body = interceptor.extract_body(msg.get("payload", {}))
                            m = re.search(r"\b(\d{6})\b", body)
                            if m:
                                code = m.group(1)
                                logger.info(f"Successfully intercepted OTP: {code}")
                                break
                    logger.info(f"Waiting for OTP... (attempt {attempt+1}/25)")

                if not code:
                    logger.error("Failed to intercept OTP!")
                    await browser.close()
                    sys.exit(1)

                otp_field = page.locator("input[name='credentials.passcode'], input[type='tel'], input[name='answer'], input.okta-form-input-field")
                await otp_field.first.fill(code)
                verify_btn = page.locator("input[value='Verify'], button:has-text('Verify'), input[type='submit']")
                await verify_btn.first.click()
                await asyncio.sleep(8)
                break

            if "portal.bhhomestate.com" in page.url and await page.locator("text='Auto'").count() > 0:
                logger.info("Already logged in to portal!")
                break
            await asyncio.sleep(1)

        logger.info(f"5. Portal Dashboard reached: {page.url}")
        await page.screenshot(path="data/screenshots/bhhc/e2e_dashboard.png")

        # Click Auto
        auto_menu = page.locator("text='Auto'").first
        await auto_menu.click()
        await asyncio.sleep(3)

        # Click Manage Your Policy
        manage_link = page.locator("text='Manage Your Policy'").first
        logger.info("6. Opening Manage Your Policy...")
        async with context.expect_page(timeout=25000) as new_page_info:
            await manage_link.click()

        myp_page = await new_page_info.value
        # Wait for Home_Producer.aspx
        for _ in range(30):
            if "Home_Producer.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(5)
        logger.info(f"7. Producer Home page reached: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc/e2e_producer_home.png")

        # Wait for ABC TRANSPIRATION LLC or quote 18316236
        target_quote = myp_page.locator("a:has-text('18316236')").first
        for _ in range(15):
            if await target_quote.count() > 0:
                break
            await asyncio.sleep(1)

        logger.info(f"8. Target quote found (count: {await target_quote.count()}). Clicking quote...")
        await target_quote.click()

        # Wait for Quote Details
        for _ in range(20):
            if "Quote_Details.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(4)
        logger.info(f"9. Quote Details loaded: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc/e2e_quote_details.png")

        # Find Quote Proposal button
        proposal_btn = myp_page.locator("#ctl00_Main_ContentPlaceHolder_btnPrintQuote, input[value='Quote Proposal']").first
        logger.info(f"10. Quote Proposal button count: {await proposal_btn.count()}")

        logger.info("11. Clicking Quote Proposal and awaiting download...")
        async with myp_page.expect_download(timeout=30000) as dl_info:
            await proposal_btn.click()

        download = await dl_info.value
        temp_dl_path = await download.path()
        logger.info(f"12. Playwright finished download to: {temp_dl_path}")

        # Copy data
        with open(temp_dl_path, "rb") as src:
            pdf_data = src.read()

        logger.info(f"13. Read {len(pdf_data)} bytes from download. Writing to {TARGET_FILE}...")
        with open(TARGET_FILE, "wb") as dst:
            dst.write(pdf_data)
            dst.flush()
            os.fsync(dst.fileno())

        # Also write a second backup copy to data/downloads/carrier_renewals/ABC_TRANSPIRATION_BHHC_QUOTE.pdf
        backup_file = os.path.abspath("data/downloads/carrier_renewals/ABC_TRANSPIRATION_BHHC_QUOTE.pdf")
        with open(backup_file, "wb") as dst:
            dst.write(pdf_data)
            dst.flush()
            os.fsync(dst.fileno())

        logger.info(f"14. Verified primary: {TARGET_FILE} (size: {os.path.getsize(TARGET_FILE)} bytes)")
        logger.info(f"15. Verified backup: {backup_file} (size: {os.path.getsize(backup_file)} bytes)")

        await browser.close()
        logger.info("16. Process completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
