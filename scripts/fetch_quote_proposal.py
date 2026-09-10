import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_pdf")

os.makedirs("data/screenshots/bhhc", exist_ok=True)
os.makedirs("data/downloads/carrier_renewals", exist_ok=True)

TARGET_PDF = "data/downloads/carrier_renewals/BHHC_ABC_TRANSPIRATION_LLC_Renewal_Quote_18316236.pdf"

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
        # Wait for the producer page to finish loading
        logger.info("Waiting for producer page to load completely...")
        try:
            await myp_page.wait_for_url("**/Home_Producer.aspx**", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(5)
        logger.info(f"Loaded producer page: {myp_page.url}")

        # Navigate directly to Quote Details URL
        logger.info("Navigating to https://myp.bhhc.com/Forms/Quote_Details.aspx?ID=18316236...")
        await myp_page.goto("https://myp.bhhc.com/Forms/Quote_Details.aspx?ID=18316236", wait_until="load")
        await asyncio.sleep(4)

        logger.info(f"Quote details URL: {myp_page.url}, Title: {await myp_page.title()}")
        await myp_page.screenshot(path="data/screenshots/bhhc/quote_details.png")

        # Inspect elements
        inputs = await myp_page.locator("a, input, button").all()
        for elem in inputs:
            txt = (await elem.inner_text()).strip() if await elem.count() > 0 else ""
            val = await elem.get_attribute("value") or ""
            href = await elem.get_attribute("href") or ""
            elem_id = await elem.get_attribute("id") or ""
            if any(k in (txt + val + href).lower() for k in ["quote", "proposal", "print", "doc", "download", "pdf"]):
                logger.info(f"Match: id='{elem_id}' text='{txt}' val='{val}' href='{href}'")

        # Now click on Quote Proposal or Print Quote
        # Let's check both
        target_btn = myp_page.locator("text='Quote Proposal', input[value*='Quote Proposal'], a:has-text('Quote Proposal'), text='Print Quote', input[value*='Print Quote']").first
        if await target_btn.count() > 0:
            logger.info(f"Clicking proposal button: {await target_btn.inner_text() if await target_btn.count() > 0 else 'btn'}")
            
            # Start waiting for download before clicking
            try:
                async with myp_page.expect_download(timeout=15000) as dl_info:
                    await target_btn.click()
                dl = await dl_info.value
                await dl.save_as(TARGET_PDF)
                logger.info(f"SUCCESS: Saved download to {TARGET_PDF} ({os.path.getsize(TARGET_PDF)} bytes)!")
            except Exception as dl_err:
                logger.info(f"Download event did not fire ({dl_err}), checking if opened in popup or new tab...")
                await asyncio.sleep(4)
                await myp_page.screenshot(path="data/screenshots/bhhc/after_quote_proposal_click.png")
                logger.info(f"Pages in context: {len(context.pages)}")
                for idx, p_cur in enumerate(context.pages):
                    logger.info(f"Page {idx}: {p_cur.url} (Title: {await p_cur.title()})")
                    if "pdf" in p_cur.url.lower() or "report" in p_cur.url.lower() or "document" in p_cur.url.lower():
                        res = await p_cur.request.get(p_cur.url)
                        with open(TARGET_PDF, "wb") as f:
                            f.write(await res.body())
                        logger.info(f"SUCCESS: Saved popup PDF to {TARGET_PDF} ({os.path.getsize(TARGET_PDF)} bytes)!")

        await browser.close()
        logger.info("Done.")

if __name__ == "__main__":
    asyncio.run(run())
