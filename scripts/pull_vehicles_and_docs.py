import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_veh_uw")

POLICY_NUMBER = "02APM066538-01"
OUTPUT_DIR = "data/downloads/policy_changes"

async def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs("data/screenshots/bhhc_endorsements", exist_ok=True)

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

        # Auto -> Manage Your Policy
        await page.click("text='Auto'")
        await asyncio.sleep(2)

        manage_link = page.locator("text='Manage Your Policy'")
        async with context.expect_page(timeout=20000) as new_page_info:
            await manage_link.click()

        myp_page = await new_page_info.value
        for _ in range(30):
            if "Home_Producer.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)

        # Dismiss Osano banner
        try:
            await myp_page.evaluate("() => { const b = document.querySelector('.osano-cm-window'); if (b) b.remove(); }")
        except Exception:
            pass

        logger.info(f"Searching policy: {POLICY_NUMBER}...")
        await myp_page.fill("#ctl00_Main_ContentPlaceHolder_uctrlSearch_txtPolNum", POLICY_NUMBER)
        await asyncio.sleep(1)

        # Use the exact form submission for search
        await myp_page.evaluate("__doPostBack('ctl00$Main_ContentPlaceHolder$uctrlSearch$btnView$lnkButton','')")

        # Wait for navigation or load state
        logger.info("Waiting for policy search to process...")
        for i in range(25):
            await asyncio.sleep(2)
            if "PolServices" in myp_page.url:
                logger.info(f"Redirected to PolServices at {i*2}s: {myp_page.url}")
                break

        # If not redirected, directly navigate to PolServices_Summary.aspx
        if "Home_Producer" in myp_page.url:
            logger.info("Directly navigating to PolServices_Summary.aspx...")
            await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_Summary.aspx", wait_until="commit", timeout=45000)
            await asyncio.sleep(4)

        logger.info(f"Active policy URL: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/pol_summary.png")

        # 1. Inspect Vehicles
        logger.info("Navigating to Vehicles page...")
        await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_Vehicles.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)
        logger.info(f"Vehicles URL: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/vehicles_page.png")
        veh_html = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/vehicles_active.html", "w") as f:
            f.write(veh_html)

        # Extract vehicle table text
        veh_tables = await myp_page.locator("table").all()
        for idx, tbl in enumerate(veh_tables):
            txt = await tbl.inner_text()
            if any(k in txt.lower() for k in ["vin", "ford", "trailer", "year", "make", "model", "f350", "f450", "f550"]):
                logger.info(f"Vehicles Table {idx}:\n{txt}")

        # 2. Inspect Underwriting Policy Docs
        logger.info("Navigating to Underwriting Policy Docs...")
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_PolicyDocs.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)
        logger.info(f"UW Policy Docs URL: {myp_page.url}")
        await myp_page.screenshot(path="data/screenshots/bhhc_endorsements/uw_docs_page.png")
        uw_html = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/uw_docs_active.html", "w") as f:
            f.write(uw_html)

        # Extract all document rows and links
        uw_links = await myp_page.locator("a").all()
        logger.info(f"UW Docs Link Count: {len(uw_links)}")
        for l in uw_links:
            t = (await l.inner_text()).strip()
            h = await l.get_attribute("href") or ""
            id_ = await l.get_attribute("id") or ""
            if any(k in t.lower() or k in h.lower() for k in ["endorse", "pdf", "change", "doc", "view", "print", "2", "3", "smart"]):
                logger.info(f"UW Doc Link: text='{t}', id='{id_}', href='{h}'")

        uw_tables = await myp_page.locator("table").all()
        for idx, tbl in enumerate(uw_tables):
            txt = await tbl.inner_text()
            if any(k in txt.lower() for k in ["endorsement", "document", "date", "description", "view", "policy"]):
                logger.info(f"UW Table {idx}:\n{txt}")

        # Download any endorsement links found
        target_links = await myp_page.locator("a:has-text('Endorsement'), a:has-text('End 2'), a:has-text('End 3'), a:has-text('End #2'), a:has-text('End #3'), a:has-text('View'), a:has-text('Print')").all()
        for l in target_links:
            txt = (await l.inner_text()).strip()
            href = await l.get_attribute("href") or ""
            id_ = await l.get_attribute("id") or ""
            logger.info(f"Target download candidate: text='{txt}', id='{id_}', href='{href}'")
            if any(k in txt.lower() or k in href.lower() or k in id_.lower() for k in ["2", "3", "endorsement"]):
                fname = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"{txt}_{id_}")[:50]
                try:
                    async with myp_page.expect_download(timeout=10000) as dl_info:
                        if href.startswith("javascript:"):
                            await myp_page.evaluate(f"() => {{ {href.replace('javascript:', '')} }}")
                        else:
                            await l.click(no_wait_after=True)
                    dl = await dl_info.value
                    save_path = os.path.join(OUTPUT_DIR, f"{fname}.pdf")
                    await dl.save_as(save_path)
                    logger.info(f"Successfully saved PDF: {save_path}")
                except Exception as ex:
                    logger.warning(f"Download failed for {txt}: {ex}")
                    try:
                        async with context.expect_page(timeout=5000) as p_info:
                            if href.startswith("javascript:"):
                                await myp_page.evaluate(f"() => {{ {href.replace('javascript:', '')} }}")
                            else:
                                await l.click(no_wait_after=True)
                        pop = await p_info.value
                        logger.info(f"Popup URL: {pop.url}")
                        pop_content = await pop.content()
                        with open(os.path.join(OUTPUT_DIR, f"{fname}_popup.html"), "w") as pf:
                            pf.write(pop_content)
                        await pop.close()
                    except Exception as ex2:
                        logger.warning(f"Popup failed: {ex2}")

        await browser.close()
        logger.info("Script finished.")

if __name__ == "__main__":
    asyncio.run(run())
