import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("maple_tech_aspire")

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
            await asyncio.sleep(2)
            
            user_input = page.locator("input#txtLogin, input[name='txtLogin']").first
            pwd_input = page.locator("input[name='txtPassword']").first
            
            await user_input.fill("carloRA0007")
            await pwd_input.fill("Sonny101!")
            submit_btn = page.locator("input[name='btnSubmit'], input[type='image']").first
            await submit_btn.click()
            await asyncio.sleep(5)
            
            logger.info(f"Page URL: {page.url}")
            
            # Look for Aspire link/button
            aspire_elem = page.locator("a:visible, input[type='image']:visible, img:visible").filter(has_text="ASPIRE").first
            if await aspire_elem.count() == 0:
                aspire_elem = page.locator("img[src*='aspire' i], a[href*='aspire' i]").first
                
            logger.info(f"Found Aspire button: {await aspire_elem.count() > 0}")
            
            # Prepare to catch popup or navigation
            async with ctx.expect_page(timeout=10000) as new_page_info:
                await aspire_elem.click()
            
            aspire_page = await new_page_info.value
            await aspire_page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)
            logger.info(f"Aspire Page URL: {aspire_page.url}")
            await aspire_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/maple_tech_aspire_dashboard.png")
            
            body = await aspire_page.evaluate("() => document.body.innerText")
            logger.info(f"Aspire Body Snippet:\n{body[:800]}")
            
            # Search for policy HONJ2025100027 or Benli
            search_inputs = await aspire_page.locator("input[type='text']:visible").all()
            for i, si in enumerate(search_inputs):
                sid = await si.get_attribute("id") or ""
                sname = await si.get_attribute("name") or ""
                logger.info(f"Search input {i}: id={sid}, name={sname}")
                
            await aspire_page.close()
        except Exception as e:
            logger.error(f"Error during crawl: {e}", exc_info=True)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/maple_tech_error.png")
        finally:
            await page.close()

if __name__ == "__main__":
    asyncio.run(run())
