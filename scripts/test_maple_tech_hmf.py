import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("maple_tech_hmf")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({"width": 1600, "height": 1000})
            url = "https://app.maple-tech.com/hmf/index.aspx?overrideSysWindow=true"
            logger.info(f"Navigating to Maple Tech: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/maple_tech_init.png")
            logger.info(f"Landing URL: {page.url}")
            
            # Inspect inputs
            inputs = await page.locator("input:visible").all()
            for i, inp in enumerate(inputs):
                itype = await inp.get_attribute("type") or ""
                iid = await inp.get_attribute("id") or ""
                iname = await inp.get_attribute("name") or ""
                logger.info(f"Input {i}: type={itype}, id={iid}, name={iname}")
                
            # Fill username and password
            user_input = page.locator("input[type='text']:visible, input#txtUserName, input[name*='User' i]:visible").first
            pwd_input = page.locator("input[type='password']:visible, input#txtPassword, input[name*='Pass' i]:visible").first
            
            if await user_input.count() > 0 and await pwd_input.count() > 0:
                logger.info("Filling Maple Tech credentials...")
                await user_input.fill("carloRA0007")
                await pwd_input.fill("Sonny101!")
                await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/maple_tech_filled.png")
                
                # Submit
                login_btn = page.locator("input[type='submit']:visible, button:visible, a:visible").filter(has_text="Log In").first
                if await login_btn.count() == 0:
                    login_btn = page.locator("input[type='submit']:visible, button:visible").first
                logger.info("Clicking login button...")
                await login_btn.click()
                await asyncio.sleep(6)
                
                logger.info(f"Post-Login URL: {page.url}")
                await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/maple_tech_post_login.png")
                
                # Check for policy search or search box
                body = await page.evaluate("() => document.body.innerText")
                logger.info(f"Body snippet:\n{body[:400]}")
        finally:
            await page.close()

if __name__ == "__main__":
    asyncio.run(run())
