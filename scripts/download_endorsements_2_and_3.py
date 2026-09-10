import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_download_end")

POLICY_NUMBER = "02APM066538-01"
OUTPUT_DIR = "/opt/renewal-automation-system/data/downloads/policy_changes"

async def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

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

        # Postback search
        await myp_page.evaluate("__doPostBack('ctl00$Main_ContentPlaceHolder$uctrlSearch$btnView$lnkButton','')")

        # Wait for summary page or navigate directly
        for _ in range(25):
            if "PolServices" in myp_page.url:
                break
            await asyncio.sleep(2)

        if "Home_Producer" in myp_page.url:
            await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_Summary.aspx", wait_until="commit", timeout=45000)
            await asyncio.sleep(3)

        # 1. Download Endorsements from Underwriting Info
        logger.info("Navigating to Underwriting Info...")
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_PolicyDocs.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)
        logger.info(f"UW Info URL: {myp_page.url}")

        async def download_doc(btn_id, postback_arg, out_filename):
            logger.info(f"Attempting download for {out_filename} via {btn_id}...")
            save_path = os.path.join(OUTPUT_DIR, out_filename)
            try:
                async with myp_page.expect_download(timeout=15000) as dl_info:
                    await myp_page.evaluate(f"() => {{ __doPostBack('{postback_arg}', ''); }}")
                dl = await dl_info.value
                await dl.save_as(save_path)
                logger.info(f"SUCCESS: Saved download to {save_path} (Size: {os.path.getsize(save_path)} bytes)")
                return True
            except Exception as ex:
                logger.warning(f"Download event timed out or failed for {out_filename}: {ex}")
                # Check if popup was opened
                try:
                    async with context.expect_page(timeout=5000) as p_info:
                        await myp_page.evaluate(f"() => {{ __doPostBack('{postback_arg}', ''); }}")
                    pop = await p_info.value
                    pop_content = await pop.content()
                    logger.info(f"Popup opened with URL: {pop.url}, len: {len(pop_content)}")
                    with open(save_path.replace(".pdf", ".html"), "w") as pf:
                        pf.write(pop_content)
                    await pop.close()
                    return True
                except Exception as ex2:
                    logger.warning(f"Popup expectation failed: {ex2}")
                    return False

        # Download Endorsement #2 (ctl09_btnPrint)
        ok2 = await download_doc(
            "ctl09_btnPrint",
            "ctl00$ctl00$Main_ContentPlaceHolder$UWInfo_ContentPlaceHolder$oPolicyDocuments$repDocs$ctl09$btnPrint",
            "Endorsement_2_02APM066538-01.pdf"
        )
        await asyncio.sleep(3)

        # If needed reload UWInfo_PolicyDocs before next download
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_PolicyDocs.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)

        # Download Endorsement #3 (ctl13_btnPrint)
        ok3 = await download_doc(
            "ctl13_btnPrint",
            "ctl00$ctl00$Main_ContentPlaceHolder$UWInfo_ContentPlaceHolder$oPolicyDocuments$repDocs$ctl13$btnPrint",
            "Endorsement_3_02APM066538-01.pdf"
        )
        await asyncio.sleep(3)

        # Also download Endorsement #1 (ctl08_btnPrint) for completeness
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_PolicyDocs.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)
        await download_doc(
            "ctl08_btnPrint",
            "ctl00$ctl00$Main_ContentPlaceHolder$UWInfo_ContentPlaceHolder$oPolicyDocuments$repDocs$ctl08$btnPrint",
            "Endorsement_1_02APM066538-01.pdf"
        )
        await asyncio.sleep(3)

        # 2. Extract Vehicles
        logger.info("Navigating to Vehicles page...")
        await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_Vehicles.aspx", wait_until="commit", timeout=45000)
        await asyncio.sleep(4)
        veh_content = await myp_page.content()
        with open(os.path.join(OUTPUT_DIR, "active_vehicles.html"), "w") as vf:
            vf.write(veh_content)
        logger.info("Saved active_vehicles.html")

        await browser.close()
        logger.info("Download session completed successfully.")

if __name__ == "__main__":
    asyncio.run(run())
