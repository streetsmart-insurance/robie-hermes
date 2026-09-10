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
logger = logging.getLogger("bhhc_endorsement")

POLICY_NUMBER = "02APM066538-01"
INSURED_NAME = "Smart Fiber"

os.makedirs("data/screenshots/bhhc_endorsements", exist_ok=True)
os.makedirs("data/downloads/policy_changes", exist_ok=True)

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds.get("username") or "carlo@streetsmart.insurance"
    password = creds.get("password") or "Policy!2026Shield"

    logger.info(f"Starting BHHC portal session with username: {username}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )
        page = await context.new_page()

        logger.info("Navigating to https://portal.bhhomestate.com/...")
        await page.goto("https://portal.bhhomestate.com/", wait_until="networkidle", timeout=45000)

        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            logger.info("Submitting Okta credentials...")
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.click("#okta-signin-submit")
            await asyncio.sleep(4)

        send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
        if await send_code_btn.count() > 0:
            click_time = time.time()
            logger.info("Clicking 'Send me the code' button...")
            await send_code_btn.first.click()
            await asyncio.sleep(2)

            interceptor = MultiInboxOTPInterceptor(["carlo@streetsmart.insurance", "sandy@streetsmart.insurance"])
            code = None
            for attempt in range(25):
                await asyncio.sleep(3)
                for inbox in ["carlo@streetsmart.insurance", "sandy@streetsmart.insurance"]:
                    svc = interceptor.get_service(inbox)
                    if not svc:
                        continue
                    res = svc.users().messages().list(userId="me", q="from:noreply@bhhomestate.com newer_than:2m", maxResults=2).execute()
                    messages = res.get("messages", [])
                    if messages:
                        msg = svc.users().messages().get(userId="me", id=messages[0]["id"], format="full").execute()
                        if int(msg.get("internalDate", 0)) / 1000.0 >= click_time - 15:
                            body = interceptor.extract_body(msg.get("payload", {}))
                            m = re.search(r"\b(\d{6})\b", body)
                            if m:
                                code = m.group(1)
                                logger.info(f"OTP intercepted from {inbox}: {code}")
                                break
                    if code:
                        break
                if code:
                    break

            if code:
                otp_field = page.locator("input[name='credentials.passcode'], input[type='tel'], input[name='answer'], input.okta-form-input-field")
                await otp_field.first.fill(code)
                await page.locator("input[value='Verify'], button:has-text('Verify')").first.click()
                await asyncio.sleep(6)
            else:
                logger.error("Failed to intercept OTP code within timeout!")
                await page.screenshot(path="data/screenshots/bhhc_endorsements/otp_failed.png")
                await browser.close()
                return

        logger.info(f"Dashboard reached: {page.url}")
        await page.screenshot(path="data/screenshots/bhhc_endorsements/1_dashboard.png")

        # Click Auto -> Manage Your Policy
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=15000) as new_page_info:
            await manage_link.click()

        myp_page = await new_page_info.value
        for _ in range(30):
            if "Home_Producer.aspx" in myp_page.url or "bhhc.com" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)
        logger.info(f"Loaded MYP page: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/2_myp_home.png")

        # Print inputs and links on MYP home
        inputs = await myp_page.locator("input").evaluate_all("els => els.map(e => ({id: e.id, name: e.name, type: e.type, value: e.value}))")
        logger.info(f"Inputs on MYP page: {inputs}")

        # Check 'Dashboard' tab
        dashboard_tab = myp_page.locator("a:has-text('Dashboard'), span:has-text('Dashboard')").first
        if await dashboard_tab.count() > 0:
            logger.info("Clicking Dashboard tab...")
            await dashboard_tab.click()
            await asyncio.sleep(4)
            await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/3_myp_dashboard.png")
            dash_inputs = await myp_page.locator("input").evaluate_all("els => els.map(e => ({id: e.id, name: e.name, type: e.type, value: e.value}))")
            logger.info(f"Inputs on Dashboard tab: {dash_inputs}")
            dash_links = await myp_page.locator("a").evaluate_all("els => els.map(e => ({text: e.innerText, href: e.href}))")
            logger.info(f"Links on Dashboard tab: {[l for l in dash_links if l.get('text', '').strip()]}")

        await browser.close()
        logger.info("Initial inspection completed.")

if __name__ == "__main__":
    asyncio.run(run())
