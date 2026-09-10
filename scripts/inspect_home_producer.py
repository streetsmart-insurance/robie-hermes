import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("inspect_producer")

async def run():
    sm = SecretsManager()
    creds = sm.get_login_pair("BHHC")
    username = creds.get("username") or "carlo@streetsmart.insurance"
    password = creds.get("password") or "Policy!2026Shield"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
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

        # Inspect portal links
        logger.info("Inspecting portal.bhhomestate.com menus...")
        sidebar_items = await page.locator(".nav-link, nav a, aside a").evaluate_all("els => els.map(e => ({text: e.innerText, href: e.href}))")
        logger.info(f"Sidebar items: {sidebar_items}")

        # Check Services
        srv = page.locator("text='Services'")
        if await srv.count() > 0:
            await srv.click()
            await asyncio.sleep(2)
            await page.screenshot(path="data/screenshots/bhhc_endorsements/portal_services.png")
            srv_links = await page.locator("a").evaluate_all("els => els.map(e => ({text: e.innerText, href: e.href}))")
            logger.info(f"Links after Services click: {[l for l in srv_links if l.get('text', '').strip()]}")

        # Now click Auto -> Manage Your Policy
        await page.click("text='Auto'")
        await asyncio.sleep(2)
        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=15000) as new_page_info:
            await manage_link.click()

        myp_page = await new_page_info.value
        for _ in range(30):
            if "Home_Producer.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(4)

        logger.info(f"Inspecting MYP Home: {myp_page.url}")
        content = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/home_producer.html", "w") as f:
            f.write(content)

        # Look for search, select, or dropdown elements
        selects = await myp_page.locator("select").evaluate_all("els => els.map(e => ({id: e.id, name: e.name, options: Array.from(e.options).map(o => o.text)}))")
        logger.info(f"Select elements on Home_Producer: {selects}")

        tables = await myp_page.locator("table").evaluate_all("els => els.map(e => ({id: e.id, className: e.className, rows: e.rows.length}))")
        logger.info(f"Tables on Home_Producer: {tables}")

        await browser.close()
        logger.info("Done.")

if __name__ == "__main__":
    asyncio.run(run())
