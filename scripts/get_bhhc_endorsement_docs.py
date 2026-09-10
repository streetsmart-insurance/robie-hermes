import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_endorsements")

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

        logger.info(f"Filling policy number: {POLICY_NUMBER}...")
        await myp_page.fill("#ctl00_Main_ContentPlaceHolder_uctrlSearch_txtPolNum", POLICY_NUMBER)
        await asyncio.sleep(1)

        logger.info("Triggering search postback...")
        await myp_page.evaluate("""() => {
            setTimeout(() => {
                const btn = document.getElementById('ctl00_Main_ContentPlaceHolder_uctrlSearch_btnView_lnkButton');
                if (btn) btn.click();
                else __doPostBack('ctl00$Main_ContentPlaceHolder$uctrlSearch$btnView$lnkButton','');
            }, 50);
        }""")

        for _ in range(40):
            if "PolServices_Summary.aspx" in myp_page.url:
                break
            await asyncio.sleep(1)

        logger.info(f"Reached Policy Summary: {myp_page.url}")
        await asyncio.sleep(2)

        # 1. Check Transaction History
        logger.info("Navigating to Transaction History...")
        await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_TransHistory.aspx", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        trans_html = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/trans_history.html", "w") as f:
            f.write(trans_html)
        logger.info("Saved trans_history.html")

        # Extract text from trans history table
        tables = await myp_page.locator("table").all()
        for idx, tbl in enumerate(tables):
            txt = await tbl.inner_text()
            if "Transaction" in txt or "Endorsement" in txt or "Date" in txt:
                logger.info(f"Transaction Table {idx}:\n{txt}")

        # 2. Check Vehicles Tab
        logger.info("Navigating to Vehicles Tab...")
        await myp_page.goto("https://myp.bhhc.com/Forms/PolServices_Vehicles.aspx", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        veh_html = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/vehicles.html", "w") as f:
            f.write(veh_html)
        logger.info("Saved vehicles.html")

        # 3. Check Underwriting Info Tab (Policy Docs)
        logger.info("Navigating to Underwriting Info (UWInfo_PolicyDocs.aspx)...")
        await myp_page.goto("https://myp.bhhc.com/Forms/UWInfo_PolicyDocs.aspx", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        try:
            await myp_page.evaluate("() => { const b = document.querySelector('.osano-cm-window'); if (b) b.remove(); }")
        except Exception:
            pass

        uw_html = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/uw_docs.html", "w") as f:
            f.write(uw_html)
        logger.info("Saved uw_docs.html")

        # Look for all downloadable links/buttons on UW Docs
        doc_links = await myp_page.locator("a").all()
        logger.info(f"Total links on UW Docs: {len(doc_links)}")
        found_docs = []
        for a in doc_links:
            text = (await a.inner_text()).strip()
            href = await a.get_attribute("href") or ""
            id_attr = await a.get_attribute("id") or ""
            if any(k in text.lower() or k in href.lower() or k in id_attr.lower() for k in ["endorsement", "end", "doc", "pdf", "change", "download", "view"]):
                logger.info(f"Potential document link: id='{id_attr}', text='{text}', href='{href}'")
                found_docs.append((a, text, href, id_attr))

        # Check for grid rows in UW Docs
        rows = await myp_page.locator("tr").all()
        logger.info(f"Total table rows in UW Docs: {len(rows)}")
        for r in rows:
            r_text = (await r.inner_text()).strip()
            if any(k in r_text.lower() for k in ["endorsement", "end", "policy change", "vehicle", "trailer", "02", "03"]):
                logger.info(f"Document Row: {r_text}")

        # Download documents
        # Let's inspect links that look like endorsements
        endorsement_links = [item for item in found_docs if any(k in item[1].lower() or k in item[2].lower() or k in item[3].lower() for k in ["endorsement", "end 2", "end #2", "end 3", "end #3", "change"])]
        if not endorsement_links:
            # Maybe all document links
            endorsement_links = [item for item in found_docs if "doc" in item[3].lower() or "view" in item[1].lower() or "print" in item[1].lower()]

        logger.info(f"Targeting {len(endorsement_links)} links for download...")
        for idx, (locator, text, href, id_attr) in enumerate(endorsement_links):
            safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"{text}_{id_attr}")[:50]
            logger.info(f"Attempting download for: {text} ({id_attr})")
            try:
                async with myp_page.expect_download(timeout=10000) as download_info:
                    if href.startswith("javascript:"):
                        await myp_page.evaluate(f"() => {{ {href.replace('javascript:', '')} }}")
                    else:
                        await locator.click(no_wait_after=True)
                download = await download_info.value
                dest_path = os.path.join(OUTPUT_DIR, f"{safe_name}.pdf")
                await download.save_as(dest_path)
                logger.info(f"SUCCESS: Saved download to {dest_path}")
            except Exception as e:
                logger.warning(f"Download via expect_download failed for {text}: {e}")
                # Sometimes it opens in a popup or new tab:
                try:
                    async with context.expect_page(timeout=5000) as popup_info:
                        if href.startswith("javascript:"):
                            await myp_page.evaluate(f"() => {{ {href.replace('javascript:', '')} }}")
                        else:
                            await locator.click(no_wait_after=True)
                    popup = await popup_info.value
                    logger.info(f"Opened popup: {popup.url}")
                    await asyncio.sleep(2)
                    # If popup is PDF or has embed
                    popup_html = await popup.content()
                    with open(os.path.join(OUTPUT_DIR, f"{safe_name}_popup.html"), "w") as pf:
                        pf.write(popup_html)
                    await popup.close()
                except Exception as e2:
                    logger.warning(f"Popup expectation failed: {e2}")

        await browser.close()
        logger.info("Script run complete.")

if __name__ == "__main__":
    asyncio.run(run())
