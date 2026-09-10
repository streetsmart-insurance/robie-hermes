import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delete_cr")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        
        # Navigate to policies page
        await page.goto("https://app.ezlynx.com/web/account/21587333/policies", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)
        
        await page.screenshot(path="data/screenshots/ezlynx_policies_list.png")
        logger.info("Captured policies list screenshot.")
        
        # Find PAC00001215485
        # In EZLynx, policy rows often have an expandable arrow or actions menu
        # Let's inspect the page content
        text_content = await page.content()
        logger.info(f"Page content length: {len(text_content)}")
        
        # Look for Change Request indicator or buttons
        cr_elements = await page.locator("text='Change Request', text='Pending', text='Delete', text='Cancel', [title*='Change'], [title*='Delete']").all()
        logger.info(f"Found {len(cr_elements)} potential elements")
        for el in cr_elements[:15]:
            try:
                t = (await el.inner_text()).strip()
                logger.info(f"Element: tag={await el.evaluate('e => e.tagName')}, text='{t}'")
            except Exception:
                pass
                
        await page.close()

if __name__ == "__main__":
    asyncio.run(main())
