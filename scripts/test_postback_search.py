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

POLICY_NUMBER = "02APM066538-01"

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

        # Dismiss Osano cookie banner
        logger.info("Dismissing cookie banner...")
        await myp_page.evaluate("() => { const b = document.querySelector('.osano-cm-window'); if (b) b.remove(); }")
        await asyncio.sleep(1)

        # Scroll to search section
        await myp_page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

        logger.info(f"Filling policy number: {POLICY_NUMBER}...")
        await myp_page.fill("#ctl00_Main_ContentPlaceHolder_uctrlSearch_txtPolNum", POLICY_NUMBER)
        await asyncio.sleep(1)

        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/before_search_click.png")

        logger.info("Executing search postback...")
        # Submit the postback and wait for response
        try:
            async with myp_page.expect_response(lambda r: "Home_Producer.aspx" in r.url, timeout=30000):
                await myp_page.evaluate("__doPostBack('ctl00$Main_ContentPlaceHolder$uctrlSearch$btnView$lnkButton','')")
        except Exception as e:
            logger.warning(f"expect_response exception: {e}")

        await asyncio.sleep(5)
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/after_search_postback.png")
        logger.info(f"Current URL: {myp_page.url}")

        # Remove cookie banner again if re-appeared
        await myp_page.evaluate("() => { const b = document.querySelector('.osano-cm-window'); if (b) b.remove(); }")

        # Check grid view container content
        grid_html = await myp_page.locator("#home_GridViewContainer").inner_html()
        logger.info(f"Grid container HTML:\n{grid_html}")

        # Check full body text
        full_text = await myp_page.inner_text("body")
        logger.info(f"Full body text excerpt:\n{full_text[:3000]}")

        # Check if policy link exists
        pol_link = myp_page.locator("a:has-text('02APM066538'), a:has-text('Smart Fiber')").first
        if await pol_link.count() > 0:
            link_text = await pol_link.inner_text()
            logger.info(f"Policy link found: '{link_text}'! Clicking...")
            try:
                async with myp_page.expect_navigation(timeout=20000):
                    await pol_link.click()
            except Exception:
                await pol_link.click(no_wait_after=True)
            await asyncio.sleep(5)
            await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/after_policy_click.png")
            logger.info(f"After policy click URL: {myp_page.url}")

            # Check tabs
            tabs = await myp_page.locator(".tabClass, nav a, a").evaluate_all("els => els.map(e => ({text: e.innerText, href: e.href}))")
            logger.info(f"Tabs/links on policy page: {[t for t in tabs if t.get('text', '').strip()]}")

            # Look for Underwriting Info tab
            uw_tab = myp_page.locator("a:has-text('Underwriting Info'), a:has-text('UW Info'), a:has-text('Underwriting')").first
            if await uw_tab.count() > 0:
                logger.info("Clicking Underwriting Info...")
                try:
                    async with myp_page.expect_navigation(timeout=20000):
                        await uw_tab.click()
                except Exception:
                    await uw_tab.click(no_wait_after=True)
                await asyncio.sleep(5)
                await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/underwriting_tab.png")
                logger.info(f"Underwriting tab URL: {myp_page.url}")
                uw_text = await myp_page.inner_text("body")
                logger.info(f"Underwriting tab text:\n{uw_text[:3000]}")

        await browser.close()
        logger.info("Search run completed.")

if __name__ == "__main__":
    asyncio.run(run())
