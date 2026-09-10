import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_producer")

os.makedirs("data/screenshots/bhhc", exist_ok=True)

TARGET_POLICY = "02TRM066190-01"

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds["username"]
    password = creds["password"]

    async with async_playwright() as p:
        logger.info("Launching standalone Firefox...")
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
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
        # Wait for the producer page to load
        logger.info("Waiting for producer page to load completely...")
        await myp_page.wait_for_url("**/Home_Producer.aspx**", timeout=30000)
        await asyncio.sleep(5)
        logger.info(f"Loaded producer page: {myp_page.url}, Title: {await myp_page.title()}")
        await myp_page.screenshot(path="data/screenshots/bhhc/myp_producer_home.png")

        # Dump full text
        body_text = await myp_page.inner_text("body")
        logger.info(f"Producer page text:\n{body_text[:2500]}")

        # List all tables, inputs, forms
        inputs = await myp_page.locator("input, select, button").all()
        logger.info(f"Total form elements: {len(inputs)}")
        for elem in inputs[:15]:
            tag = await elem.evaluate("e => e.tagName")
            elem_id = await elem.get_attribute("id")
            elem_name = await elem.get_attribute("name")
            elem_type = await elem.get_attribute("type")
            elem_val = await elem.get_attribute("value")
            placeholder = await elem.get_attribute("placeholder")
            logger.info(f"Element: <{tag} id='{elem_id}' name='{elem_name}' type='{elem_type}' val='{elem_val}' placeholder='{placeholder}'>")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
