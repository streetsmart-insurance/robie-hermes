import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_end_targeted")

POLICY_NUMBER = "02APM066538-01"
OUTPUT_DIR = "/opt/renewal-automation-system/data/downloads/policy_changes"

async def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs("/opt/renewal-automation-system/data/screenshots/bhhc_endorsements", exist_ok=True)

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

        logger.info(f"Filling policy: {POLICY_NUMBER}...")
        await myp_page.fill("#ctl00_Main_ContentPlaceHolder_uctrlSearch_txtPolNum", POLICY_NUMBER)
        await asyncio.sleep(1)

        # Postback search using setTimeout to prevent evaluate lockup
        logger.info("Executing search postback...")
        await myp_page.evaluate("""() => {
            setTimeout(() => {
                __doPostBack('ctl00$Main_ContentPlaceHolder$uctrlSearch$btnView$lnkButton','');
            }, 50);
        }""")

        for i in range(25):
            await asyncio.sleep(2)
            if "PolServices" in myp_page.url:
                logger.info(f"Landed on PolServices: {myp_page.url}")
                break

        if "Home_Producer" in myp_page.url:
            logger.info("Navigating to PolServices_Summary.aspx directly...")
            await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_Summary.aspx", wait_until="commit", timeout=45000)
            await asyncio.sleep(3)

        # Step 1: Download Endorsement #2 (ctl09_btnPrint)
        logger.info("--- DOWNLOADING ENDORSEMENT #2 ---")
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_PolicyDocs.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)

        btn2 = myp_page.locator("#ctl00_ctl00_Main_ContentPlaceHolder_UWInfo_ContentPlaceHolder_oPolicyDocuments_repDocs_ctl09_btnPrint")
        if await btn2.count() > 0:
            logger.info("Found ctl09_btnPrint! Triggering download for Endorsement #2...")
            try:
                async with myp_page.expect_download(timeout=15000) as dl_info:
                    await btn2.click()
                dl2 = await dl_info.value
                path2 = os.path.join(OUTPUT_DIR, "Endorsement_2_02APM066538-01.pdf")
                await dl2.save_as(path2)
                logger.info(f"SUCCESS: Saved Endorsement #2 to {path2} (Size: {os.path.getsize(path2)} bytes)")
            except Exception as e:
                logger.error(f"Failed to download Endorsement #2: {e}")
        else:
            logger.error("ctl09_btnPrint NOT found on UWInfo_PolicyDocs page!")

        # Step 2: Download Endorsement #3 (ctl13_btnPrint)
        logger.info("--- DOWNLOADING ENDORSEMENT #3 ---")
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_PolicyDocs.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)

        btn3 = myp_page.locator("#ctl00_ctl00_Main_ContentPlaceHolder_UWInfo_ContentPlaceHolder_oPolicyDocuments_repDocs_ctl13_btnPrint")
        if await btn3.count() > 0:
            logger.info("Found ctl13_btnPrint! Triggering download for Endorsement #3...")
            try:
                async with myp_page.expect_download(timeout=15000) as dl_info:
                    await btn3.click()
                dl3 = await dl_info.value
                path3 = os.path.join(OUTPUT_DIR, "Endorsement_3_02APM066538-01.pdf")
                await dl3.save_as(path3)
                logger.info(f"SUCCESS: Saved Endorsement #3 to {path3} (Size: {os.path.getsize(path3)} bytes)")
            except Exception as e:
                logger.error(f"Failed to download Endorsement #3: {e}")
        else:
            logger.error("ctl13_btnPrint NOT found on UWInfo_PolicyDocs page!")

        # Step 3: Extract Active Vehicles
        logger.info("--- EXTRACTING ACTIVE VEHICLES ---")
        await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_Vehicles.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)
        veh_html = await myp_page.content()
        with open(os.path.join(OUTPUT_DIR, "active_vehicles.html"), "w") as vf:
            vf.write(veh_html)
        logger.info("Saved active_vehicles.html")

        await browser.close()
        logger.info("All tasks completed.")

if __name__ == "__main__":
    asyncio.run(run())
