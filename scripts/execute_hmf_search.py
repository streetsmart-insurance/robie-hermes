import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("execute_hmf_search")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        
        aspire_page = None
        for page in ctx.pages:
            if "maple-tech.com/hmf/directory" in page.url:
                aspire_page = page
                break
                
        if not aspire_page:
            logger.error("No aspire directory page found")
            return
            
        main_frame = None
        for frame in aspire_page.frames:
            if frame.name == "fraTocMain":
                main_frame = frame
                break
                
        if not main_frame:
            logger.error("No fraTocMain found")
            return
            
        logger.info("Filling policy number HONJ2025100027...")
        await main_frame.locator("input#pno").fill("HONJ2025100027")
        
        logger.info("Clicking PERFORM SEARCH...")
        search_btn = main_frame.locator("text='PERFORM SEARCH'").first
        await search_btn.click()
        await asyncio.sleep(5)
        
        await aspire_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/hmf_search_results.png")
        
        # Check text in main_frame
        text = await main_frame.evaluate("() => document.body.innerText")
        logger.info(f"Results text snippet:\n{text[:1500]}")
        
        # Check links or rows
        links = await main_frame.locator("a:visible").all()
        for i, link in enumerate(links):
            ltxt = await link.inner_text()
            lhref = await link.get_attribute("href") or ""
            logger.info(f"Link {i}: text='{ltxt}', href='{lhref}'")

if __name__ == "__main__":
    asyncio.run(run())
