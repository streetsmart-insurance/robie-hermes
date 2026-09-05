import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_links")

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

        await page.goto("https://portal.bhhomestate.com/", wait_until="networkidle")
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
        
        # Click Auto
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        rater_link = page.locator("text='Auto Rater'")
        
        logger.info(f"Manage Your Policy href: {await manage_link.get_attribute('href')}, target: {await manage_link.get_attribute('target')}")
        logger.info(f"Auto Rater href: {await rater_link.get_attribute('href')}, target: {await rater_link.get_attribute('target')}")
        
        # Click Manage Your Policy and wait for navigation or popup
        logger.info("Clicking Manage Your Policy...")
        async with context.expect_page(timeout=15000) as new_page_info:
            await manage_link.click()
        
        new_page = await new_page_info.value
        logger.info(f"Opened page URL: {new_page.url}")
        await new_page.wait_for_load_state("networkidle")
        logger.info(f"New page URL after load: {new_page.url}, Title: {await new_page.title()}")
        await new_page.screenshot(path="data/screenshots/bhhc/manage_policy_page.png")
        
        # Print text and inputs
        inputs = await new_page.locator("input").all()
        for i in inputs:
            logger.info(f"Input: type={await i.get_attribute('type')}, name={await i.get_attribute('name')}, id={await i.get_attribute('id')}, placeholder={await i.get_attribute('placeholder')}")
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
