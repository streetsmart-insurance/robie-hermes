import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_aspire")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({"width": 1600, "height": 1000})
            await page.goto("https://app.maple-tech.com/hmf/login_link.aspx", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            logger.info("Calling new_openSystem() and watching for popup...")
            async with ctx.expect_page(timeout=15000) as popup_info:
                await page.evaluate("() => new_openSystem()")
                
            popup = await popup_info.value
            logger.info(f"Popup opened with URL: {popup.url}")
            await popup.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(6)
            logger.info(f"Popup final URL: {popup.url}")
            await popup.screenshot(path="/opt/renewal-automation-system/data/screenshots/maple_tech_popup.png")
            
            # Check main page as well
            await asyncio.sleep(4)
            logger.info(f"Main page URL: {page.url}")
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/maple_tech_main_after_popup.png")
        finally:
            await page.close()

if __name__ == "__main__":
    asyncio.run(run())
