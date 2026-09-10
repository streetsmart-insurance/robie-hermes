import asyncio
import time
import logging
from playwright.async_api import async_playwright
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("natgen_crawler")

async def run():
    logger.info("Starting E2E Autonomous National General Crawler...")
    interceptor = MultiInboxOTPInterceptor(inboxes=["carlo@streetsmart.insurance", "robie@streetsmart.insurance"])
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({"width": 1600, "height": 1000})
            logger.info("Navigating to NatGen login...")
            await page.goto("https://natgenagency.com/Login.aspx", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            # Step 1: User ID
            await page.locator("input:visible, input[type='text']:visible").first.fill("Carlof")
            await page.locator("button:visible, a:visible, input[type='submit']:visible").filter(has_text="SIGN IN").first.click()
            await asyncio.sleep(4)
            
            # Step 2: Password
            await page.locator("input[type='password']:visible").first.fill("moxry8-dihzyw-Bognec")
            await page.locator("button:visible, input[type='submit']:visible, a:visible").filter(has_text="SIGN IN").first.click()
            await asyncio.sleep(5)
            
            # Step 3: Trigger Email OTP
            email_opt = page.locator("#loginWith2faEmail")
            if await email_opt.count() == 0:
                logger.error(f"Could not find #loginWith2faEmail! Current URL: {page.url}")
                await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_e2e_err1.png")
                return
                
            trigger_time = int(time.time())
            logger.info(f"Triggering email OTP at timestamp {trigger_time}...")
            await email_opt.click()
            await asyncio.sleep(4)
            logger.info(f"Waiting on code input page: {page.url}")
            
            # Step 4: Intercept OTP
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
                logger.error("Failed to intercept valid 6-digit OTP code!")
                return
                
            # Step 5: Fill OTP
            code_input = page.locator("input[type='text']:visible, input[name*='code' i]:visible, input[id*='code' i]:visible").first
            await code_input.fill(code)
            
            # Check "Don't ask again on this device"
            dont_ask = page.locator("input[type='checkbox']").first
            if await dont_ask.count() > 0:
                await dont_ask.check()
                
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_e2e_code_entered.png")
            
            # Step 6: Click Verify
            verify_btn = page.locator("button:visible, input[type='submit']:visible").filter(has_text="Verify").first
            logger.info("Clicking Verify button...")
            await verify_btn.click()
            await asyncio.sleep(8)
            
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_e2e_dashboard.png")
            logger.info(f"Post-Verification NatGen URL: {page.url}")
            
            # Step 7: Search for Smordoni policy
            logger.info("Searching for policy PUP2437998 or Smordoni...")
            body_text = await page.evaluate("() => document.body.innerText")
            logger.info(f"Dashboard text preview: {body_text[:200]}")
            
            search_input = page.locator("input[placeholder*='Search' i], input[name*='search' i], input[id*='search' i], input[placeholder*='Policy' i]").first
            if await search_input.count() > 0:
                logger.info("Found search input, searching for PUP2437998...")
                await search_input.fill("PUP2437998")
                await page.keyboard.press("Enter")
                await asyncio.sleep(6)
                await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_policy_search_result.png")
                logger.info(f"Policy search result URL: {page.url}")
            else:
                logger.info("Checking for Policy Search link or menu...")
                policy_nav = page.locator("a:has-text('Policy'), a:has-text('Search'), a:has-text('Manage Policies')").first
                if await policy_nav.count() > 0:
                    await policy_nav.click()
                    await asyncio.sleep(4)
                    await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_policy_nav.png")
        finally:
            await page.close()

if __name__ == "__main__":
    asyncio.run(run())
