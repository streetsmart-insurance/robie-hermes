import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_endorsement_pull")

POLICY_NUMBER = "02APM066538-01"
INSURED_NAME = "Smart Fiber"

os.makedirs("data/screenshots/bhhc_endorsements", exist_ok=True)
os.makedirs("data/downloads/policy_changes", exist_ok=True)

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds.get("username") or "carlo@streetsmart.insurance"
    password = creds.get("password") or "Policy!2026Shield"

    logger.info(f"Starting BHHC session with {username}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )
        page = await context.new_page()

        await page.goto("https://portal.bhhomestate.com/", wait_until="networkidle", timeout=45000)

        if "auth.bhhc.com" in page.url or await page.locator("#okta-signin-username").count() > 0:
            await page.fill("#okta-signin-username", username)
            await page.fill("#okta-signin-password", password)
            await page.click("#okta-signin-submit")
            await asyncio.sleep(4)

        send_code_btn = page.locator("a:has-text('Send me the code'), button:has-text('Send me the code'), input[value='Send me the code']")
        if await send_code_btn.count() > 0:
            click_time = time.time()
            await send_code_btn.first.click()
            await asyncio.sleep(2)

            interceptor = MultiInboxOTPInterceptor(["carlo@streetsmart.insurance", "sandy@streetsmart.insurance"])
            code = None
            for _ in range(25):
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
                await browser.close()
                return

        # Click Auto -> Manage Your Policy
        logger.info("Accessing Manage Your Policy...")
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=20000) as new_page_info:
            await manage_link.click()

        myp_page = await new_page_info.value
        try:
            await myp_page.wait_for_url("**/Forms/Home_Producer.aspx*", timeout=45000)
        except Exception:
            pass
        await asyncio.sleep(4)

        logger.info(f"Loaded Home_Producer.aspx: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/producer_home.png")

        # Search for policy number
        logger.info(f"Searching for policy: {POLICY_NUMBER}...")
        await myp_page.fill("#ctl00_Main_ContentPlaceHolder_uctrlSearch_txtPolNum", POLICY_NUMBER)
        await asyncio.sleep(1)

        # Click View button with no_wait_after=True
        logger.info("Clicking View search button (no_wait_after=True)...")
        await myp_page.click("#ctl00_Main_ContentPlaceHolder_uctrlSearch_btnView_lnkButton", no_wait_after=True)
        await asyncio.sleep(6)

        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/search_results.png")
        logger.info(f"After search URL: {myp_page.url}")

        # Let's inspect page content
        body_text = await myp_page.inner_text("body")
        logger.info(f"Page body excerpt after search:\n{body_text[:2000]}")

        # Look for links in the grid or page
        links = await myp_page.locator("a").evaluate_all("els => els.map(e => ({text: e.innerText, href: e.href, id: e.id}))")
        logger.info(f"Links after search: {[l for l in links if l.get('text', '').strip()]}")

        # Find any row or link containing 02APM066538
        pol_link = myp_page.locator("a:has-text('02APM066538'), td:has-text('02APM066538'), span:has-text('02APM066538')")
        if await pol_link.count() > 0:
            logger.info(f"Found policy element: {await pol_link.first.inner_text()}")
            # If it's a link or inside a link, click it
            clickable = myp_page.locator("a:has-text('02APM066538'), a:has-text('Smart Fiber')").first
            if await clickable.count() > 0:
                await clickable.click(no_wait_after=True)
                await asyncio.sleep(5)
                await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/policy_details.png")
                logger.info(f"Policy details page URL: {myp_page.url}")

        # Look for Underwriting Info tab or link
        uw_link = myp_page.locator("a:has-text('Underwriting Info'), a:has-text('Underwriting'), a:has-text('UW Info')").first
        if await uw_link.count() > 0:
            logger.info("Found Underwriting Info tab, clicking...")
            await uw_link.click(no_wait_after=True)
            await asyncio.sleep(5)
            await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/underwriting_info.png")
            logger.info(f"Underwriting Info URL: {myp_page.url}")

            uw_links = await myp_page.locator("a").evaluate_all("els => els.map(e => ({text: e.innerText, href: e.href, id: e.id}))")
            logger.info(f"Links on Underwriting Info: {[l for l in uw_links if l.get('text', '').strip()]}")

        await browser.close()
        logger.info("Completed search test.")

if __name__ == "__main__":
    asyncio.run(run())
