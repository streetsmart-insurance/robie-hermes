import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_search")

os.makedirs("data/screenshots/bhhc", exist_ok=True)
os.makedirs("data/downloads/carrier_renewals", exist_ok=True)

TARGET_POLICY = "02TRM066190-01"
TARGET_INSURED = "ABC TRANSPIRATION"

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

        logger.info(f"Portal dashboard reached: {page.url}")
        
        # Click Auto -> Manage Your Policy
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=15000) as new_page_info:
            await manage_link.click()
        
        myp_page = await new_page_info.value
        await myp_page.wait_for_load_state("load")
        await asyncio.sleep(4)
        logger.info(f"Manage Your Policy page loaded: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc/myp_initial.png")

        # Check all checkboxes (active, expired, quote, cancelled) to ensure we find everything
        for cb_id in ["active", "expired", "quote"]:
            cb = myp_page.locator(f"#{cb_id}")
            if await cb.count() > 0:
                is_checked = await cb.is_checked()
                if not is_checked:
                    logger.info(f"Checking filter checkbox: {cb_id}")
                    await cb.check()
                    await asyncio.sleep(1)

        # Type search query
        search_box = myp_page.locator("input[placeholder='Search...']")
        logger.info(f"Searching for {TARGET_POLICY}...")
        await search_box.fill(TARGET_POLICY)
        await search_box.press("Enter")
        await asyncio.sleep(4)
        await myp_page.screenshot(path="data/screenshots/bhhc/myp_search_policy.png")

        # Check results
        table_text = await myp_page.inner_text("body")
        logger.info(f"Search results excerpt:\n{table_text[:1500]}")

        # Check if rows appear
        rows = myp_page.locator("table tbody tr")
        row_count = await rows.count()
        logger.info(f"Found {row_count} matching table rows.")

        if row_count == 0 or TARGET_POLICY not in table_text:
            logger.info("Trying search by insured name ABC...")
            await search_box.fill("")
            await search_box.fill("ABC")
            await search_box.press("Enter")
            await asyncio.sleep(4)
            await myp_page.screenshot(path="data/screenshots/bhhc/myp_search_abc.png")
            table_text = await myp_page.inner_text("body")
            logger.info(f"ABC search results:\n{table_text[:1500]}")

        # Check for policy links or row clicks
        policy_link = myp_page.locator("table tbody tr a, table tbody tr").first
        if await policy_link.count() > 0:
            logger.info("Found result row/link, clicking...")
            await policy_link.click()
            await asyncio.sleep(5)
            await myp_page.screenshot(path="data/screenshots/bhhc/myp_policy_details.png")
            details_text = await myp_page.inner_text("body")
            logger.info(f"Policy details excerpt:\n{details_text[:2000]}")
            
            # Check for Documents tab or Renewal
            doc_tab = myp_page.locator("text='Documents', text='Policy Documents', text='Forms', text='Renewals'")
            if await doc_tab.count() > 0:
                logger.info(f"Found document/renewal tabs: {await doc_tab.all_inner_texts()}")
                await doc_tab.first.click()
                await asyncio.sleep(4)
                await myp_page.screenshot(path="data/screenshots/bhhc/myp_policy_documents.png")
                docs_text = await myp_page.inner_text("body")
                logger.info(f"Documents list:\n{docs_text[:2000]}")

        await browser.close()
        logger.info("Policy search completed.")

if __name__ == "__main__":
    asyncio.run(run())
