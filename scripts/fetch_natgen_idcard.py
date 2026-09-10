import asyncio
import os
import time
import logging
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("natgen_idcard")

async def run():
    logger.info("Starting Autonomous NatGen Login & ID Card Retrieval...")
    interceptor = MultiInboxOTPInterceptor(inboxes=["carlo@streetsmart.insurance", "robie@streetsmart.insurance"])
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        
        # Check if an existing NatGen page is open
        natgen_page = None
        for pg in ctx.pages:
            if "natgenagency.com" in pg.url and "Login" not in pg.url and "Register" not in pg.url:
                natgen_page = pg
                logger.info(f"Found active NatGen page: {pg.url}")
                break
                
        if not natgen_page:
            logger.info("No active NatGen session found. Logging in via new page...")
            natgen_page = await ctx.new_page()
            await natgen_page.set_viewport_size({"width": 1600, "height": 1000})
            await natgen_page.goto("https://natgenagency.com/Login.aspx", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            # Step 1: User ID
            await natgen_page.locator("input:visible, input[type='text']:visible").first.fill("Carlof")
            await natgen_page.locator("button:visible, a:visible, input[type='submit']:visible").filter(has_text="SIGN IN").first.click()
            await asyncio.sleep(4)
            
            # Step 2: Password
            await natgen_page.locator("input[type='password']:visible").first.fill("moxry8-dihzyw-Bognec")
            await natgen_page.locator("button:visible, input[type='submit']:visible, a:visible").filter(has_text="SIGN IN").first.click()
            await asyncio.sleep(5)
            
            # Step 3: Trigger Email OTP
            email_opt = natgen_page.locator("#loginWith2faEmail")
            if await email_opt.count() > 0:
                trigger_time = int(time.time())
                logger.info(f"Triggering email OTP at timestamp {trigger_time}...")
                await email_opt.click()
                await asyncio.sleep(4)
                
                logger.info("Intercepting OTP from carlo@streetsmart.insurance...")
                code = None
                for attempt in range(25):
                    await asyncio.sleep(3)
                    otp_res = interceptor.check_inbox_since(
                        inbox="carlo@streetsmart.insurance",
                        query="from:natgen.verification@ngic.com OR subject:verification",
                        since_timestamp=trigger_time - 5
                    )
                    if otp_res and otp_res.code and otp_res.code.isdigit() and len(otp_res.code) == 6:
                        code = otp_res.code
                        logger.info(f"Successfully intercepted 6-digit OTP: {code}")
                        break
                    else:
                        logger.info(f"  Polling attempt {attempt+1}... (Result: {otp_res.code if otp_res else 'None'})")
                        
                if not code:
                    logger.error("Failed to intercept OTP code!")
                    return
                    
                code_input = natgen_page.locator("input[type='text']:visible, input[name*='code' i]:visible, input[id*='code' i]:visible").first
                await code_input.fill(code)
                dont_ask = natgen_page.locator("input[type='checkbox']").first
                if await dont_ask.count() > 0:
                    await dont_ask.check()
                
                verify_btn = natgen_page.locator("button:visible, input[type='submit']:visible").filter(has_text="Verify").first
                if await verify_btn.count() == 0:
                    verify_btn = natgen_page.locator("button:visible, input[type='submit']:visible").filter(has_text="SUBMIT").first
                logger.info("Clicking Verify/Submit button...")
                await verify_btn.click()
                await asyncio.sleep(8)
                
        logger.info(f"Current page URL: {natgen_page.url}")
        
        # Now navigate to Jesus Solano policy
        logger.info("Searching for policy 2031936859...")
        # Direct URL or quick search
        search_box = natgen_page.locator("#ctl00_ctl00_QuickSearchControl_txtSearch, input[placeholder*='Search' i], input[name*='Search' i]").first
        if await search_box.count() > 0:
            await search_box.fill("2031936859")
            await natgen_page.keyboard.press("Enter")
            await asyncio.sleep(6)
            logger.info(f"URL after search: {natgen_page.url}")
        else:
            await natgen_page.goto("https://natgenagency.com/Policy/PolicySummary.aspx?PolicyNumber=2031936859")
            await asyncio.sleep(6)
            
        await natgen_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_solano_nav.png")
        
        # Navigate to ID Card request page
        logger.info("Navigating to ID Card Request page...")
        # Check if ID card link exists
        idcard_link = natgen_page.locator("a:has-text('ID Card'), a:has-text('Auto ID Card'), a[href*='IDCardRequest']").first
        if await idcard_link.count() > 0:
            await idcard_link.click()
            await asyncio.sleep(4)
        else:
            await natgen_page.goto("https://natgenagency.com/Summary/IDCardRequest.aspx")
            await asyncio.sleep(4)
            
        logger.info(f"ID Card page URL: {natgen_page.url}")
        await natgen_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_idcard_page.png")
        
        # Inspect the ID Card page controls
        controls = await natgen_page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input, button, a, select'))
                .filter(el => el.offsetParent !== null)
                .map(el => ({
                    tag: el.tagName,
                    id: el.id,
                    name: el.name,
                    value: el.value,
                    text: el.innerText ? el.innerText.trim() : ''
                }));
        }""")
        logger.info(f"Found {len(controls)} controls on ID card page:")
        for c in controls:
            if any(k in (c['id'] + ' ' + c['text'] + ' ' + c['value']).lower() for k in ['print', 'email', 'pdf', 'card', 'send', 'document', 'download']):
                logger.info(f"  {c}")
                
        # Also check if we can print the page or generate PDF
        pdf_path = "/opt/renewal-automation-system/data/documents/jesus_solano_idcard.pdf"
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        
        # Look for Print button / link
        print_btn = natgen_page.locator("#ctl00_ctl00_MainContent_MainPageContent_ucRequestDocument_PrintLinkButton, a:has-text('Print'), input[value*='Print' i]").first
        if await print_btn.count() > 0:
            logger.info("Found Print button, setting up download listener...")
            async with natgen_page.expect_download(timeout=10000) as download_info:
                try:
                    await print_btn.click()
                    download = await download_info.value
                    await download.save_as(pdf_path)
                    logger.info(f"Downloaded ID Card PDF directly to: {pdf_path}")
                except Exception as e:
                    logger.warning(f"Direct download error (might open popup or new tab): {e}")
                    
        # Check if a new tab opened
        for pg in ctx.pages:
            if "pdf" in pg.url.lower() or "report" in pg.url.lower():
                logger.info(f"Found PDF/Report tab: {pg.url}")
                await pg.pdf(path=pdf_path)
                logger.info(f"Saved PDF tab to {pdf_path}")
                
        # Also let's check Email tab to trigger official NatGen carrier dispatch
        email_tab = natgen_page.locator("a:has-text('Email'), #EmailTab, [href*='Email']").first
        if await email_tab.count() > 0:
            logger.info("Clicking Email tab...")
            await email_tab.click()
            await asyncio.sleep(2)
            await natgen_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_idcard_email_tab.png")
            
            # Check email field & send button
            email_input = natgen_page.locator("input[id*='txtEmail' i], input[name*='txtEmail' i]").first
            if await email_input.count() > 0:
                await email_input.fill("solano1777@gmail.com")
                send_email_btn = natgen_page.locator("input[value*='Send Email' i], button:has-text('Send Email'), input[id*='btnSendEmail' i]").first
                if await send_email_btn.count() > 0:
                    logger.info("Clicking Send Email button on NatGen portal...")
                    await send_email_btn.click()
                    await asyncio.sleep(4)
                    await natgen_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_idcard_email_sent.png")
                    logger.info("Official carrier email dispatched via NatGen!")

if __name__ == "__main__":
    asyncio.run(run())
