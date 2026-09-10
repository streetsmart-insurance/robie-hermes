import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("search_hmf")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        
        # Look for the Aspire tab or navigate to it
        aspire_page = None
        for page in ctx.pages:
            if "maple-tech.com/hmf/directory" in page.url or "maple-tech.com" in page.url:
                aspire_page = page
                break
                
        if not aspire_page:
            logger.info("Opening new page to navigate to directory...")
            aspire_page = await ctx.new_page()
            await aspire_page.set_viewport_size({"width": 1600, "height": 1000})
            await aspire_page.goto("https://app.maple-tech.com/hmf/directory/index.aspx", wait_until="domcontentloaded")
        else:
            await aspire_page.set_viewport_size({"width": 1600, "height": 1000})
            
        await asyncio.sleep(2)
        logger.info(f"Target page URL: {aspire_page.url}")
        
        # Check frames
        logger.info(f"Page frames count: {len(aspire_page.frames)}")
        for i, frame in enumerate(aspire_page.frames):
            logger.info(f"Frame {i}: name='{frame.name}', url='{frame.url}'")
            
        # Click on Search on the left
        search_link = None
        for frame in aspire_page.frames:
            s = frame.locator("text='Search'").first
            if await s.count() > 0:
                logger.info(f"Found 'Search' in frame: {frame.name}")
                search_link = s
                break
                
        if not search_link:
            search_link = aspire_page.locator("text='Search'").first
            
        logger.info("Clicking 'Search'...")
        await search_link.click()
        await asyncio.sleep(4)
        
        await aspire_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/hmf_search_page.png")
        
        # Check all frames again after clicking Search
        for i, frame in enumerate(aspire_page.frames):
            logger.info(f"Post-click Frame {i}: name='{frame.name}', url='{frame.url}'")
            inputs = await frame.locator("input:visible").all()
            for j, inp in enumerate(inputs):
                iid = await inp.get_attribute("id") or ""
                iname = await inp.get_attribute("name") or ""
                logger.info(f"  Frame {frame.name} Input {j}: id={iid}, name={iname}")

if __name__ == "__main__":
    asyncio.run(run())
