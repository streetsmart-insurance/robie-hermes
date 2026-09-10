import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("natgen_search")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({"width": 1600, "height": 1000})
            await page.goto("https://natgenagency.com/Search.aspx", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            # Inspect Search By options
            search_by_select = page.locator("select").first
            if await search_by_select.count() > 0:
                opts = await search_by_select.locator("option").all_inner_texts()
                logger.info(f"Search By Options: {opts}")
                
            # Search by Name: Smordoni
            logger.info("Searching by Smordoni...")
            input_box = page.locator("input[type='text']:visible").first
            await input_box.fill("Smordoni")
            await page.locator("input[type='submit']:visible, button:visible").filter(has_text="Search").first.click()
            await asyncio.sleep(5)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_search_smordoni.png")
            
            res_text = await page.evaluate("() => document.body.innerText")
            logger.info(f"Search Smordoni result text:\n{res_text[:500]}")
            
            # Also inspect PRODUCTS menu
            prod_menu = page.locator("a:has-text('PRODUCTS')").first
            if await prod_menu.count() > 0:
                await prod_menu.hover()
                await asyncio.sleep(1)
                await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_products_menu.png")
        finally:
            await page.close()

if __name__ == "__main__":
    asyncio.run(run())
