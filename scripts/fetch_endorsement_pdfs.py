import asyncio
import logging
import os
import re
import time
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor
from src.security.secrets_manager import SecretsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bhhc_docs")

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
        await asyncio.sleep(3)

        # 1. Click Underwriting Info Tab
        logger.info("Clicking Underwriting Info menu button...")
        try:
            await myp_page.evaluate("() => { const b = document.querySelector('.osano-cm-window'); if (b) b.remove(); }")
        except Exception:
            pass

        await myp_page.evaluate("""() => {
            setTimeout(() => {
                const btn = document.getElementById('ctl00_ctl00_mnuUWInfo_lnk');
                if (btn) btn.click();
            }, 50);
        }""")

        for _ in range(30):
            if "UWInfo" in myp_page.url:
                break
            await asyncio.sleep(1)

        await asyncio.sleep(3)
        logger.info(f"Current URL after clicking Underwriting Info: {myp_page.url}")

        uw_html = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/uw_info_clicked.html", "w") as f:
            f.write(uw_html)
        logger.info("Saved uw_info_clicked.html")

        # Extract all document rows and links from UW Info
        links = await myp_page.locator("a").all()
        logger.info(f"Total links on UW Info: {len(links)}")
        for l in links:
            t = (await l.inner_text()).strip()
            h = await l.get_attribute("href") or ""
            id_ = await l.get_attribute("id") or ""
            if any(k in t.lower() or k in h.lower() for k in ["endorsement", "end", "doc", "pdf", "change", "download", "view", "print", "2", "3"]):
                logger.info(f"UW Link: text='{t}', id='{id_}', href='{h}'")

        # Also check all tables on UW Info
        tables = await myp_page.locator("table").all()
        for idx, tbl in enumerate(tables):
            txt = await tbl.inner_text()
            if any(k in txt.lower() for k in ["endorsement", "doc", "date", "description", "view", "policy"]):
                logger.info(f"UW Table {idx}:\n{txt[:1000]}")

        # Check for subtabs under UW Info (e.g. Policy Docs, Notes, Inspections, etc.)
        subtabs = await myp_page.locator(".tabSelected, .tabUnselected, a[href*='UWInfo_']").all()
        for st in subtabs:
            st_text = (await st.inner_text()).strip()
            st_href = await st.get_attribute("href") or ""
            logger.info(f"UW Subtab: text='{st_text}', href='{st_href}'")

        # Now let's see if we can download Endorsement #2 and Endorsement #3
        # Look for buttons or links with 'Endorsement' or '2' or '3'
        end_elements = await myp_page.locator("a:has-text('Endorsement'), a:has-text('End 2'), a:has-text('End 3'), a:has-text('End #2'), a:has-text('End #3'), a:has-text('View'), a:has-text('Print')").all()
        logger.info(f"Found {len(end_elements)} potential endorsement download elements")

        for el in end_elements:
            txt = (await el.inner_text()).strip()
            href = await el.get_attribute("href") or ""
            id_ = await el.get_attribute("id") or ""
            logger.info(f"Candidate element: text='{txt}', id='{id_}', href='{href}'")
            if any(k in txt.lower() or k in href.lower() or k in id_.lower() for k in ["2", "3", "endorsement"]):
                safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"{txt}_{id_}")[:50]
                logger.info(f"Attempting download/click on candidate: {txt}")
                try:
                    async with myp_page.expect_download(timeout=8000) as dl_info:
                        if href.startswith("javascript:"):
                            await myp_page.evaluate(f"() => {{ {href.replace('javascript:', '')} }}")
                        else:
                            await el.click(no_wait_after=True)
                    dl = await dl_info.value
                    save_path = os.path.join(OUTPUT_DIR, f"{safe_name}.pdf")
                    await dl.save_as(save_path)
                    logger.info(f"DOWNLOADED: {save_path}")
                except Exception as ex:
                    logger.warning(f"expect_download failed: {ex}")
                    # Try popup
                    try:
                        async with context.expect_page(timeout=4000) as p_info:
                            if href.startswith("javascript:"):
                                await myp_page.evaluate(f"() => {{ {href.replace('javascript:', '')} }}")
                            else:
                                await el.click(no_wait_after=True)
                        pop = await p_info.value
                        logger.info(f"Popup opened: {pop.url}")
                        pop_html = await pop.content()
                        with open(os.path.join(OUTPUT_DIR, f"{safe_name}_pop.html"), "w") as pf:
                            pf.write(pop_html)
                        await pop.close()
                    except Exception as ex2:
                        logger.warning(f"Popup failed: {ex2}")

        # 2. Check Vehicles Tab
        logger.info("Clicking Policy Services menu then Vehicles...")
        await myp_page.evaluate("""() => {
            setTimeout(() => {
                const btn = document.getElementById('ctl00_ctl00_mnuPolServices_lnk');
                if (btn) btn.click();
            }, 50);
        }""")
        for _ in range(25):
            if "PolServices" in myp_page.url:
                break
            await asyncio.sleep(1)

        await myp_page.evaluate("""() => {
            setTimeout(() => {
                const btn = document.querySelector("a[href*='PolServices_Vehicles']");
                if (btn) btn.click();
            }, 50);
        }""")
        for _ in range(25):
            if "PolServices_Vehicles" in myp_page.url:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)

        veh_html = await myp_page.content()
        with open("data/screenshots/bhhc_endorsements/vehicles_clicked.html", "w") as f:
            f.write(veh_html)
        logger.info(f"Saved vehicles_clicked.html. URL: {myp_page.url}")

        await browser.close()
        logger.info("fetch_endorsement_pdfs run finished.")

if __name__ == "__main__":
    asyncio.run(run())
