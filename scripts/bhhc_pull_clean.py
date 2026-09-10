import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_pull")

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
        await page.goto("https://portal.bhhomestate.com/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)

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
        for _ in range(40):
            if "Home_Producer.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)

        # Dismiss Osano cookie banner
        logger.info("Dismissing cookie banner...")
        try:
            await myp_page.evaluate("() => { const b = document.querySelector('.osano-cm-window'); if (b) b.remove(); }")
        except Exception:
            pass
        await asyncio.sleep(1)

        logger.info(f"Filling policy number: {POLICY_NUMBER}...")
        await myp_page.fill("#ctl00_Main_ContentPlaceHolder_uctrlSearch_txtPolNum", POLICY_NUMBER)
        await asyncio.sleep(1)

        logger.info("Executing search postback via evaluate...")
        await myp_page.evaluate("__doPostBack('ctl00$Main_ContentPlaceHolder$uctrlSearch$btnView$lnkButton','')")

        # Wait for navigation to complete by polling document.readyState
        logger.info("Waiting for page load state after postback...")
        for i in range(45):
            await asyncio.sleep(1)
            try:
                state = await myp_page.evaluate("document.readyState")
                if state == "complete":
                    # Check if GridView container or table has updated
                    logger.info(f"Page complete at {i+1}s. URL: {myp_page.url}")
                    break
            except Exception:
                continue

        await asyncio.sleep(2)
        try:
            await myp_page.evaluate("() => { const b = document.querySelector('.osano-cm-window'); if (b) b.remove(); }")
        except Exception:
            pass

        # Save HTML
        html_content = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/search_results.html", "w") as f:
            f.write(html_content)
        logger.info("Saved search_results.html")

        # Check for error
        try:
            err_msg = await myp_page.locator("#ctl00_Main_ContentPlaceHolder_uctrlSearch_lblError").inner_text()
            if err_msg.strip():
                logger.warning(f"Search error message: {err_msg}")
        except Exception:
            pass

        # Find any row or link containing 02APM066538
        pol_matches = await myp_page.locator("a:has-text('02APM066538'), a:has-text('Smart Fiber')").all()
        logger.info(f"Found {len(pol_matches)} matching policy links")
        for i, match in enumerate(pol_matches):
            txt = await match.inner_text()
            href = await match.get_attribute("href")
            logger.info(f"Match {i}: text='{txt}', href='{href}'")

        if not pol_matches:
            # Let's inspect all tables
            tables = await myp_page.locator("table").all()
            logger.info(f"Total tables found: {len(tables)}")
            for idx, tbl in enumerate(tables):
                t_txt = await tbl.inner_text()
                if "02APM066538" in t_txt or "Smart Fiber" in t_txt or "Policy" in t_txt:
                    logger.info(f"Table {idx} content:\n{t_txt[:1000]}")

        # If policy match found, click it!
        if pol_matches:
            logger.info("Clicking first policy match...")
            pol_link = pol_matches[0]
            href = await pol_link.get_attribute("href")
            if href and "javascript:__doPostBack" in href:
                m = re.search(r"__doPostBack\('([^']*)','([^']*)'\)", href)
                if m:
                    target, arg = m.group(1), m.group(2)
                    logger.info(f"Executing policy postback: {target}, {arg}")
                    await myp_page.evaluate(f"__doPostBack('{target}','{arg}')")
                else:
                    await pol_link.click(no_wait_after=True)
            else:
                await pol_link.click(no_wait_after=True)

            # Wait for policy details load
            for i in range(45):
                await asyncio.sleep(1)
                try:
                    if await myp_page.evaluate("document.readyState") == "complete":
                        break
                except Exception:
                    continue

            logger.info(f"Policy details page URL: {myp_page.url}")
            pol_html = await myp_page.content()
            with open("data/screenshots/bhhc_endorsements/policy_details.html", "w") as f:
                f.write(pol_html)
            logger.info("Saved policy_details.html")

            # Look for Underwriting Info tab
            uw_tab = myp_page.locator("a:has-text('Underwriting Info'), a:has-text('UW Info'), a:has-text('Underwriting')").first
            if await uw_tab.count() > 0:
                logger.info(f"Found UW tab: {await uw_tab.inner_text()}, clicking...")
                uw_href = await uw_tab.get_attribute("href")
                if uw_href and "javascript:__doPostBack" in uw_href:
                    m = re.search(r"__doPostBack\('([^']*)','([^']*)'\)", uw_href)
                    if m:
                        target, arg = m.group(1), m.group(2)
                        await myp_page.evaluate(f"__doPostBack('{target}','{arg}')")
                    else:
                        await uw_tab.click(no_wait_after=True)
                else:
                    await uw_tab.click(no_wait_after=True)

                for i in range(45):
                    await asyncio.sleep(1)
                    try:
                        if await myp_page.evaluate("document.readyState") == "complete":
                            break
                    except Exception:
                        continue

                logger.info(f"Underwriting tab URL: {myp_page.url}")
                uw_html = await myp_page.content()
                with open("data/screenshots/bhhc_endorsements/underwriting_info.html", "w") as f:
                    f.write(uw_html)
                logger.info("Saved underwriting_info.html")

        await browser.close()
        logger.info("Pull script complete.")

if __name__ == "__main__":
    asyncio.run(run())
